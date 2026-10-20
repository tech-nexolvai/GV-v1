"""The saved chats (#1463), append-only: chats, their messages and the files they hand over.

Nothing here updates or deletes a row. A chat's title is its first message's opening words, read
from that row. A message's `seq` is the next number in its chat; the unique `(chat_id, seq)` keeps
two writers from taking the same place. The history a turn replays is every earlier answer's
`api_content`, in order, exactly as stored (`json`, so the keys keep their order).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AssistantChat,
    AssistantChatFile,
    AssistantChatMessage,
    ModelInvocation,
    Package,
    PackageRevision,
)
from app.review.assistant.chat_contract import (
    ChatFileOut,
    ChatMessageOut,
    ChatSummaryOut,
    ChatSummaryReviewOut,
)
from app.review.assistant.chat_corrections import corrections_by_answer, with_corrections
from app.review.assistant.chat_decisions import (
    latest_decisions_of,
    package_block,
    with_decisions,
)
from app.review.assistant.chat_trust import rescored
from app.review.assistant.impact import blocks_with_impacts
from app.review.assistant.review_action import CheckState, review_action

__all__ = [
    "TITLE_CHARS",
    "add_assistant_message",
    "add_file",
    "add_interrupted_message",
    "add_user_message",
    "chat_cost_since",
    "chat_summaries",
    "chat_title",
    "create_chat",
    "file_out",
    "history",
    "message_out",
    "package_name",
    "recorded_chat_spend",
    "review_results",
    "stored_chat_spend",
    "unanswered_questions",
]

TITLE_CHARS: Final = 60
_MICROS: Final = Decimal(1_000_000)
#: The prompt ids of every chat call (lead and helpers, both providers) on the Usage page.
CHAT_PROMPT_PREFIX: Final = "review-assistant-chat-"


def chat_title(text: str) -> str:
    """A chat's title from its first message: the first 60 characters, cut at a word."""
    words = re.sub(r"\s+", " ", text).strip()
    if len(words) <= TITLE_CHARS:
        return words
    cut = words[:TITLE_CHARS]
    if words[TITLE_CHARS] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-")


def package_name(package: Package) -> str:
    vendor = (package.vendor or "").strip()
    return vendor or "Untitled drawing set"


def create_chat(session: Session, *, package_id: UUID, created_by: str) -> AssistantChat:
    chat = AssistantChat(package_id=package_id, created_by=created_by[:200])
    session.add(chat)
    session.flush()
    return chat


def _next_seq(session: Session, chat_id: UUID) -> int:
    highest = session.scalar(
        select(func.max(AssistantChatMessage.seq)).where(AssistantChatMessage.chat_id == chat_id)
    )
    return int(highest or 0) + 1


def add_user_message(
    session: Session,
    *,
    chat_id: UUID,
    created_by: str,
    text: str,
    mode: str | None = None,
    products: Sequence[str] | None = None,
) -> AssistantChatMessage:
    message = AssistantChatMessage(
        chat_id=chat_id,
        seq=_next_seq(session, chat_id),
        role="user",
        created_by=created_by[:200],
        text=text,
        mode=mode,
        products=None if products is None else list(products),
    )
    session.add(message)
    session.flush()
    return message


def add_assistant_message(
    session: Session,
    *,
    message_id: UUID,
    chat_id: UUID,
    created_by: str,
    model: str,
    effort: str,
    thinking: bool,
    text: str,
    blocks: list[dict[str, Any]],
    api_content: list[dict[str, Any]],
    cost_micros: int,
    stopped: str | None,
    error: str | None,
    message_cap_micros: int | None = None,
    package_cap_micros: int | None = None,
    cap_hit: str | None = None,
    mode: str | None = None,
    products: Sequence[str] | None = None,
) -> AssistantChatMessage:
    message = AssistantChatMessage(
        id=message_id,
        chat_id=chat_id,
        seq=_next_seq(session, chat_id),
        role="assistant",
        created_by=created_by[:200],
        model=model,
        effort=effort,
        thinking=thinking,
        text=text,
        blocks=blocks,
        api_content=api_content,
        cost_micros=cost_micros,
        stopped=stopped,
        error=error,
        message_cap_micros=message_cap_micros,
        package_cap_micros=package_cap_micros,
        cap_hit=cap_hit,
        mode=mode,
        products=None if products is None else list(products),
    )
    session.add(message)
    session.flush()
    return message


def add_interrupted_message(
    session: Session,
    *,
    message_id: UUID,
    question: AssistantChatMessage,
    model: str | None,
    effort: str | None,
    thinking: bool | None,
    blocks: list[dict[str, Any]],
    cost_micros: int,
    mode: str | None = None,
    products: Sequence[str] | None = None,
) -> AssistantChatMessage:
    """The reply a server restart cut off (#1514): `stopped: interrupted`, the work saved so far.

    Its `seq` is the question's plus one, never "the next free one": when the real reply (or a
    newer question) already took that place, the unique `(chat_id, seq)` refuses this row, so a
    question never gets two replies and a reply never lands after a later question. Nothing is
    replayed from it (`api_content` stays empty): Continue hands its work over in words."""
    message = AssistantChatMessage(
        id=message_id,
        chat_id=question.chat_id,
        seq=question.seq + 1,
        role="assistant",
        created_by=question.created_by,
        model=model,
        effort=effort,
        thinking=thinking,
        text="",
        blocks=blocks,
        api_content=[],
        cost_micros=cost_micros,
        stopped="interrupted",
        mode=mode,
        products=None if products is None else list(products),
    )
    session.add(message)
    session.flush()
    return message


def unanswered_questions(
    session: Session, chat_ids: Sequence[UUID] | None = None
) -> list[AssistantChatMessage]:
    """Each chat's last message when it is a question: one with no reply after it (of the given
    chats, else of all)."""
    last = select(
        AssistantChatMessage.chat_id.label("chat_id"),
        func.max(AssistantChatMessage.seq).label("seq"),
    ).group_by(AssistantChatMessage.chat_id)
    if chat_ids is not None:
        last = last.where(AssistantChatMessage.chat_id.in_(list(chat_ids)))
    tail = last.subquery()
    return list(
        session.scalars(
            select(AssistantChatMessage)
            .join(
                tail,
                (tail.c.chat_id == AssistantChatMessage.chat_id)
                & (tail.c.seq == AssistantChatMessage.seq),
            )
            .where(AssistantChatMessage.role == "user")
            .order_by(AssistantChatMessage.created_at)
        )
    )


def chat_cost_since(session: Session, chat_id: UUID, since: datetime) -> int:
    """What the chat's calls recorded since `since` cost (the Usage page's rows, `chat:<id>`)."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(ModelInvocation.cost_micros), 0)).where(
                ModelInvocation.template_id == f"chat:{chat_id}",
                ModelInvocation.created_at >= since,
            )
        )
        or 0
    )


def add_file(
    session: Session,
    *,
    file_id: UUID,
    chat_id: UUID,
    message_id: UUID,
    name: str,
    size: int,
    mime: str,
    sha256: str,
) -> AssistantChatFile:
    row = AssistantChatFile(
        id=file_id,
        chat_id=chat_id,
        message_id=message_id,
        name=name[:255],
        size=size,
        mime=mime[:127],
        sha256=sha256,
    )
    session.add(row)
    session.flush()
    return row


def messages_of(session: Session, chat_id: UUID) -> list[AssistantChatMessage]:
    return list(
        session.scalars(
            select(AssistantChatMessage)
            .where(AssistantChatMessage.chat_id == chat_id)
            .order_by(AssistantChatMessage.seq)
        )
    )


def files_of(session: Session, chat_id: UUID) -> list[AssistantChatFile]:
    return list(
        session.scalars(
            select(AssistantChatFile)
            .where(AssistantChatFile.chat_id == chat_id)
            .order_by(AssistantChatFile.created_at, AssistantChatFile.id)
        )
    )


def history(session: Session, chat_id: UUID) -> list[dict[str, Any]]:
    """Every earlier answer's API messages, in order, as stored."""
    replay: list[dict[str, Any]] = []
    for message in messages_of(session, chat_id):
        if message.role == "assistant" and message.api_content:
            replay.extend(message.api_content)
    return replayable_messages(replay)


def replayable_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stored API messages as they may be sent again. Answers stored before the harness audit of
    2026-10-19 could hold what the API refuses in a request, so every later question of that chat
    failed: an empty message (an empty reply), an empty text block, or a thinking block with no
    signature (a stream cut in the middle). Those parts are left out; every other message goes
    back exactly as stored (nothing changes for a chat without them)."""
    replay: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            kept = [
                block
                for block in content
                if not (
                    isinstance(block, Mapping)
                    and (
                        (block.get("type") == "text" and not block.get("text"))
                        or (block.get("type") == "thinking" and not block.get("signature"))
                    )
                )
            ]
            if not kept:
                continue
            if len(kept) != len(content):
                message = {**message, "content": kept}
        elif not content:
            continue
        replay.append(dict(message))
    return replay


def first_question(messages: Sequence[AssistantChatMessage]) -> str | None:
    return next((message.text for message in messages if message.role == "user"), None)


def file_url(chat_id: UUID, file_id: UUID) -> str:
    return f"/api/v1/chats/{chat_id}/files/{file_id}"


def file_out(row: AssistantChatFile) -> ChatFileOut:
    return ChatFileOut(
        id=row.id,
        name=row.name,
        size=row.size,
        mime=row.mime,
        url=file_url(row.chat_id, row.id),
    )


def package_outdated(
    message: AssistantChatMessage,
    files: Iterable[AssistantChatFile],
    decided_at: datetime | None,
) -> bool | None:
    """Whether the answer's newest send-ready package (#1622) was made before its newest decision
    (#1803); `None` without a package or a decision."""
    made = [
        row.created_at
        for row in files
        if row.message_id == message.id and row.mime == "application/zip"
    ]
    if decided_at is None or not made:
        return None
    return max(made) < decided_at


def _usd(micros: int | None) -> str | None:
    if micros is None:
        return None
    return str((Decimal(micros) / _MICROS).quantize(Decimal("0.01")))


def message_out(
    message: AssistantChatMessage,
    files: Iterable[AssistantChatFile],
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
    corrections: Mapping[str, Mapping[str, Any]] | None = None,
    decided_at: datetime | None = None,
) -> ChatMessageOut:
    """A stored message for the screen. An answer's accuracy is scored again from its stored
    findings with today's calibration (#1513; nothing is written). `decisions`: the answer's
    latest decision per finding id (#1521), added to its findings. `corrections`: later messages'
    newest correction per finding id (#1548, `chat_corrections`): each such finding is shown at its
    newest version, marked as updated (or dropped) in a later message. `decided_at`: when the
    answer was last decided on (#1803): a package made before it is out of date."""
    if message.role == "user":
        return ChatMessageOut.model_validate(
            {
                "id": message.id,
                "role": "user",
                "created_at": message.created_at,
                "text": message.text,
                "mode": message.mode,
                "products": message.products,
            }
        )
    blocks = blocks_with_impacts(
        with_decisions(with_corrections(message.blocks, corrections), decisions) or []
    )
    return ChatMessageOut.model_validate(
        {
            "id": message.id,
            "role": "assistant",
            "created_at": message.created_at,
            "text": message.text,
            "model": message.model,
            "effort": message.effort,
            "thinking": message.thinking,
            "blocks": blocks,
            "files": [file_out(row) for row in files if row.message_id == message.id],
            "package_outdated": package_outdated(message, files, decided_at),
            "cost_usd": _usd(message.cost_micros),
            "stopped": message.stopped,
            "error": message.error,
            "message_cap_usd": _usd(message.message_cap_micros),
            "package_cap_usd": _usd(message.package_cap_micros),
            "cap_hit": message.cap_hit,
            "accuracy": rescored(
                next(
                    (
                        block.get("accuracy")
                        for block in reversed(message.blocks or [])
                        if isinstance(block, dict) and block.get("type") == "accuracy"
                    ),
                    None,
                ),
                message.blocks,
            ),
            "resume": next(
                (
                    block.get("resume")
                    for block in reversed(message.blocks or [])
                    if isinstance(block, dict) and block.get("type") == "resume"
                ),
                None,
            ),
            "mode": message.mode,
            "products": message.products,
            "scope": _scope_of(message.blocks or []),
            "understood": next(
                (
                    block.get("understood")
                    for block in message.blocks or []
                    if isinstance(block, dict) and block.get("type") == "understood"
                ),
                None,
            ),
            # #1622: the full check's review action, from the same findings as the screen.
            "review_action": _review_action(message, blocks),
        }
    )


def _review_action(
    message: AssistantChatMessage, blocks: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """A full check's review result (#1622) once its package is made: its findings as shown (with
    the reviewer's latest decisions) and the state kept with the package."""
    if message.mode != "full_check":
        return None
    kept = package_block(message.blocks)
    if kept is None:
        return None
    findings = [
        block["finding"]
        for block in blocks
        if block.get("type") == "finding" and isinstance(block.get("finding"), Mapping)
    ]
    return review_action(findings, CheckState.from_dict(kept.get("state"))).as_dict()


def _scope_of(blocks: Sequence[Any]) -> dict[str, Any] | None:
    """An answer's scope (#1520): its `scope` block, else its trust report's."""
    found: object = None
    for block in reversed(blocks):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "scope":
            found = block.get("scope")
            break
        if block.get("type") == "trust" and found is None:
            found = (block.get("trust") or {}).get("scope")
    return found if isinstance(found, dict) else None


def chat_summaries(
    session: Session,
    package_id: UUID,
    running: Mapping[UUID, Mapping[str, Any]] | None = None,
) -> list[ChatSummaryOut]:
    """The package's chats with at least one message, newest activity first. A chat never used
    (an empty "New chat" made before chats were made on the first message) is left out, not
    deleted: the table is append-only. Each says (#1710) its newest question's mode, whether a
    message is being answered now (`running`: chat id -> its live counts, `{}` for a question)
    and its newest full check's review result."""
    chats = list(
        session.scalars(select(AssistantChat).where(AssistantChat.package_id == package_id))
    )
    if not chats:
        return []
    ids = [chat.id for chat in chats]
    stats = {
        row.chat_id: row
        for row in session.execute(
            select(
                AssistantChatMessage.chat_id,
                func.max(AssistantChatMessage.created_at).label("last"),
                func.count().filter(AssistantChatMessage.role == "user").label("turns"),
            )
            .where(AssistantChatMessage.chat_id.in_(ids))
            .group_by(AssistantChatMessage.chat_id)
        )
    }
    firsts: dict[UUID, str] = {}
    modes: dict[UUID, str | None] = {}
    for chat_id, text, mode in session.execute(
        select(AssistantChatMessage.chat_id, AssistantChatMessage.text, AssistantChatMessage.mode)
        .where(AssistantChatMessage.chat_id.in_(ids), AssistantChatMessage.role == "user")
        .order_by(AssistantChatMessage.chat_id, AssistantChatMessage.seq)
    ):
        firsts.setdefault(chat_id, text)
        modes[chat_id] = mode  # the newest question's (ordered by seq)
    results = review_results(session, ids)
    out: list[ChatSummaryOut] = []
    for chat in chats:
        row = stats.get(chat.id)
        if row is None:
            continue
        first = firsts.get(chat.id)
        live = (running or {}).get(chat.id)
        mode = modes.get(chat.id)
        out.append(
            ChatSummaryOut(
                id=chat.id,
                title=chat.title or (chat_title(first) if first else None),
                created_at=chat.created_at,
                last_message_at=row.last,
                turns=int(row.turns),
                mode="full_check" if mode == "full_check" else "chat" if mode == "chat" else None,
                running=live is not None,
                drawings_done=None if live is None else live.get("drawings_done"),
                drawings_total=None if live is None else live.get("drawings_total"),
                review_result=results.get(chat.id),
            )
        )
    out.sort(key=lambda item: (item.last_message_at or item.created_at, item.id), reverse=True)
    return out


def review_results(session: Session, chat_ids: Sequence[UUID]) -> dict[UUID, ChatSummaryReviewOut]:
    """Each chat's newest full check's review result (#1710, for the chat list), as its answer
    shows it (`message_out`): its findings at their newest versions (a later answer's corrections),
    with the reviewer's latest decisions, and the state kept with its package. A chat whose newest
    full check has no package yet (stopped before it, or older) has none."""
    if not chat_ids:
        return {}
    newest: dict[UUID, tuple[int, UUID]] = {}
    for chat_id, message_id, seq in session.execute(
        select(AssistantChatMessage.chat_id, AssistantChatMessage.id, AssistantChatMessage.seq)
        .where(
            AssistantChatMessage.chat_id.in_(chat_ids),
            AssistantChatMessage.role == "assistant",
            AssistantChatMessage.mode == "full_check",
        )
        .order_by(AssistantChatMessage.chat_id, AssistantChatMessage.seq)
    ):
        newest[chat_id] = (seq, message_id)
    if not newest:
        return {}
    answers = {
        row.id: row
        for row in session.scalars(
            select(AssistantChatMessage).where(
                AssistantChatMessage.id.in_([m for _seq, m in newest.values()])
            )
        )
    }
    # Later answers in the same chats may correct a full check's findings (#1548).
    later: dict[UUID, list[AssistantChatMessage]] = {}
    for row in session.scalars(
        select(AssistantChatMessage)
        .where(
            AssistantChatMessage.chat_id.in_(list(newest)),
            AssistantChatMessage.role == "assistant",
        )
        .order_by(AssistantChatMessage.chat_id, AssistantChatMessage.seq)
    ):
        if row.seq > newest[row.chat_id][0]:
            later.setdefault(row.chat_id, []).append(row)
    decided = latest_decisions_of(session, list(answers))
    out: dict[UUID, ChatSummaryReviewOut] = {}
    for chat_id, (_seq, message_id) in newest.items():
        message = answers.get(message_id)
        if message is None or package_block(message.blocks) is None:
            continue
        corrections = corrections_by_answer(later.get(chat_id, [])).get(str(message_id))
        blocks = with_decisions(
            with_corrections(message.blocks, corrections), decided.get(message_id)
        )
        found = _review_action(message, blocks or [])
        if found is not None:
            out[chat_id] = ChatSummaryReviewOut(action=found["action"], words=found["words"])
    return out


def stored_chat_spend(session: Session, package_id: UUID) -> int:
    """What the package's finished chat messages cost (their stored totals)."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(AssistantChatMessage.cost_micros), 0))
            .join(AssistantChat, AssistantChat.id == AssistantChatMessage.chat_id)
            .where(AssistantChat.package_id == package_id)
        )
        or 0
    )


def recorded_chat_spend(session: Session, package_id: UUID) -> int:
    """What the Usage page's rows say the package's chat calls cost (priced when they were
    recorded): a cross-check that also counts a message that never finished."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(ModelInvocation.cost_micros), 0))
            .join(PackageRevision, PackageRevision.id == ModelInvocation.package_revision_id)
            .where(
                PackageRevision.package_id == package_id,
                ModelInvocation.prompt_id.like(f"{CHAT_PROMPT_PREFIX}%"),
            )
        )
        or 0
    )
