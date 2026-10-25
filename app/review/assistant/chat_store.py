"""The saved chats (#1463), append-only: chats, their messages and the files they hand over.

Nothing here updates or deletes a row. A chat's title is its first message's opening words, read
from that row. A message's `seq` is the next number in its chat; the unique `(chat_id, seq)` keeps
two writers from taking the same place. The history a turn replays is every earlier answer's
`api_content`, in order, exactly as stored (`json`, so the keys keep their order).

**Stored answers are checked against the read model (harness pre-check, change 11).** Before an
answer is stored (`add_assistant_message`, `add_interrupted_message`), its blocks are read back
through the same model that serves them (`message_out` → `ChatMessageOut`). A block the reader would
refuse (#1934: `settled.was = "same"`; #1940: a label's `route = "ocr"`) is logged by its field and
the answer is still stored (a paid answer is never lost), marked with `stored_problems` in its
accuracy (else trust) block. Reading is tolerant too: a stored field the model refuses is left out
of what is shown (logged), never a 500 for the whole chat. Each stored answer is also stamped with
the running code's commit (`code_version`, change 7) in its trust and accuracy blocks.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    AssistantChat,
    AssistantChatFile,
    AssistantChatMessage,
    AssistantFindingDecision,
    ModelInvocation,
    Package,
    PackageRevision,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
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
from app.review.assistant.code_version import code_version
from app.review.assistant.impact import blocks_with_impacts
from app.review.assistant.proof import proof_counts
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
    "latest_full_checks",
    "message_out",
    "package_name",
    "recorded_chat_spend",
    "recorded_worker_spend",
    "review_results",
    "stored_chat_spend",
    "stored_problems",
    "unanswered_questions",
]

_log = logging.getLogger(__name__)

TITLE_CHARS: Final = 60
_MICROS: Final = Decimal(1_000_000)
#: The prompt ids of every chat call (lead and helpers, both providers) on the Usage page.
CHAT_PROMPT_PREFIX: Final = "review-assistant-chat-"
#: The blocks that carry an answer's stamps: `code_version` on both; `stored_problems` on the first.
_STAMPED: Final = ("accuracy", "trust")
#: Rounds of leaving a refused field out before a stored answer is given up as unreadable.
_MAX_REPAIRS: Final = 500

# The commit is read once, as the process starts (`code_version`).
code_version()


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
        blocks=_stamped(blocks),
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
    _check_on_write(message)
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
        blocks=_stamped(blocks),
        api_content=[],
        cost_micros=cost_micros,
        stopped="interrupted",
        mode=mode,
        products=None if products is None else list(products),
    )
    _check_on_write(message)
    session.add(message)
    session.flush()
    return message


def _stamped(blocks: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """`blocks` with the running code's commit in each trust and accuracy block (change 7)."""
    version = code_version()
    if version is None or not blocks:
        return blocks
    out: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind in _STAMPED and isinstance(block.get(kind), Mapping):
            block = {**block, kind: {**block[kind], "code_version": version}}
        out.append(block)
    return out


def stored_problems(message: AssistantChatMessage) -> list[str]:
    """The fields of a message the read model (`ChatMessageOut`, as `message_out` builds it)
    refuses, as `blocks.3.finding.settled.was: literal_error`; empty when it reads cleanly."""
    if message.role == "user":
        return []
    try:
        ChatMessageOut.model_validate(_assistant_payload(message, ()))
    except ValidationError as failed:
        return [_error_words(error) for error in failed.errors()]
    return []


def _error_words(error: Any) -> str:
    """Where and what, never the value (it may be a drawing's words)."""
    return f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('type')}"


def _check_on_write(message: AssistantChatMessage) -> None:
    """Reads a new answer back through the read model before it is stored: a refused field is
    logged and the answer marked (`stored_problems` in its accuracy, else trust, block); it is
    stored anyway, so a paid answer is never lost. Never raises."""
    try:
        found = stored_problems(message)
    except Exception:  # noqa: BLE001 - the check never stops an answer being stored
        _log.exception(
            "assistant answer %s could not be checked against the read model", message.id
        )
        return
    if not found:
        return
    _log.error(
        "assistant answer %s: stored with fields the read model refuses (shown without them): %s",
        message.id,
        "; ".join(found[:20]),
    )
    blocks = list(message.blocks or [])
    for kind in _STAMPED:
        at = next(
            (
                n
                for n, block in enumerate(blocks)
                if isinstance(block, dict)
                and block.get("type") == kind
                and isinstance(block.get(kind), Mapping)
            ),
            None,
        )
        if at is not None:
            block = blocks[at]
            blocks[at] = {**block, kind: {**block[kind], "stored_problems": found[:50]}}
            message.blocks = blocks
            return


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
    decisions_held: int | None = None,
) -> AssistantChatFile:
    """One file of a message. `decisions_held` (#1803): for the files made again from the
    reviewer's decisions, how many decision rows they were made from (`package_outdated`)."""
    row = AssistantChatFile(
        id=file_id,
        chat_id=chat_id,
        message_id=message_id,
        name=name[:255],
        size=size,
        mime=mime[:127],
        sha256=sha256,
        decisions_held=decisions_held,
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
    decisions: int | None,
) -> bool | None:
    """Whether the answer's newest send-ready package (#1622) was made from fewer decision rows
    than the answer has now (#1803); `None` without a package or a decision."""
    made = [row for row in files if row.message_id == message.id and row.mime == "application/zip"]
    if not decisions or not made:
        return None
    newest = max(made, key=lambda row: (row.created_at, str(row.id)))
    return (newest.decisions_held or 0) < decisions


def _usd(micros: int | None) -> str | None:
    if micros is None:
        return None
    return str((Decimal(micros) / _MICROS).quantize(Decimal("0.01")))


def message_out(
    message: AssistantChatMessage,
    files: Iterable[AssistantChatFile],
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
    corrections: Mapping[str, Mapping[str, Any]] | None = None,
    decisions_count: int | None = None,
) -> ChatMessageOut:
    """A stored message for the screen. An answer's accuracy is scored again from its stored
    findings with today's calibration (#1513; nothing is written). `decisions`: the answer's
    latest decision per finding id (#1521), added to its findings. `corrections`: later messages'
    newest correction per finding id (#1548, `chat_corrections`): each such finding is shown at its
    newest version, marked as updated (or dropped) in a later message. `decisions_count`: how
    many decision rows the answer has (#1803): a package made from fewer is out of date."""
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
    payload = _assistant_payload(message, files, decisions, corrections, decisions_count)
    try:
        return ChatMessageOut.model_validate(payload)
    except ValidationError as failed:
        return _read_tolerantly(message, payload, failed)


def _assistant_payload(
    message: AssistantChatMessage,
    files: Iterable[AssistantChatFile],
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
    corrections: Mapping[str, Mapping[str, Any]] | None = None,
    decisions_count: int | None = None,
) -> dict[str, Any]:
    """What `message_out` validates for an answer."""
    blocks = blocks_with_impacts(
        with_decisions(with_corrections(message.blocks, corrections), decisions) or []
    )
    return {
        "id": message.id,
        "role": "assistant",
        "created_at": message.created_at,
        "text": message.text,
        "model": message.model,
        "effort": message.effort,
        "thinking": message.thinking,
        "blocks": blocks,
        "files": [file_out(row) for row in files if row.message_id == message.id],
        "package_outdated": package_outdated(message, files, decisions_count),
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


def _read_tolerantly(
    message: AssistantChatMessage, payload: Mapping[str, Any], failed: ValidationError
) -> ChatMessageOut:
    """A stored answer the read model refuses, shown without the refused fields (each one left
    out, then its parent when it was required, then the whole block), logged; never a 500 for
    the whole chat (#1934, #1940). Raises the first failure when nothing can be left out."""
    shown: dict[str, Any] = {**payload, "blocks": copy.deepcopy(payload.get("blocks"))}
    left_out: list[str] = []
    error: ValidationError = failed
    for _round in range(_MAX_REPAIRS):
        if not _leave_out(shown, error.errors(), left_out):
            break
        try:
            out = ChatMessageOut.model_validate(shown)
        except ValidationError as again:
            error = again
            continue
        _log.error(
            "assistant answer %s: the read model refuses stored fields; shown without: %s",
            message.id,
            "; ".join(dict.fromkeys(left_out[:20])),
        )
        return out
    raise failed


def _leave_out(shown: dict[str, Any], errors: Sequence[Any], left_out: list[str]) -> bool:
    """Leaves out one refused field per block (and each refused top-level field) in `shown`;
    whether anything was left out."""
    blocks = shown.get("blocks")
    done: set[int] = set()
    dropped: set[int] = set()
    changed = False
    for error in errors:
        loc = tuple(error.get("loc", ()))
        if not loc:
            continue
        if loc[0] != "blocks":
            if loc[0] in shown and shown[loc[0]] is not None:
                shown[loc[0]] = None  # an optional top-level field (accuracy, resume, scope …)
                left_out.append(_error_words(error))
                changed = True
            continue
        if not isinstance(blocks, list) or len(loc) < 2 or not isinstance(loc[1], int):
            continue
        index = loc[1]
        if index in done or not 0 <= index < len(blocks):
            continue
        done.add(index)
        left_out.append(_error_words(error))
        block = blocks[index]
        tag = block.get("type") if isinstance(block, dict) else None
        # The union's tag (`finding`) follows the index in `loc`: it is not a key.
        path = loc[3:] if len(loc) > 2 and loc[2] == tag else loc[2:]
        if not path or not isinstance(block, dict) or not _cut(block, path):
            dropped.add(index)
        changed = True
    if dropped and isinstance(blocks, list):
        shown["blocks"] = [block for n, block in enumerate(blocks) if n not in dropped]
    return changed


def _cut(node: dict[str, Any], path: Sequence[Any]) -> bool:
    """Removes the value at `path` inside `node` (a nested union's tags, and a key that is
    missing, are skipped, so a missing required field removes its parent); `False` when the path
    names nothing below `node`."""
    trail: list[tuple[Any, Any]] = []
    current: Any = node
    for segment in path:
        keyed = isinstance(current, dict) and isinstance(segment, str) and segment in current
        listed = (
            isinstance(current, list) and isinstance(segment, int) and 0 <= segment < len(current)
        )
        if keyed or listed:
            trail.append((current, segment))
            current = current[segment]
    if not trail:
        return False
    container, key = trail[-1]
    if isinstance(container, list):
        container.pop(key)
    else:
        del container[key]
    return True


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


#: The newest full checks' summaries already worked out (`latest_full_checks`), by what they
#: depend on: the answer, its decision rows and the chat's later answers. Every one of those tables is
#: append-only, so a key that matches can never be stale; a new decision or answer is a new key.
_SUMMARIES: dict[tuple[UUID, int, int], dict[str, Any]] = {}
_SUMMARIES_LOCK: Final = threading.Lock()
#: Enough for every set on a page several times over; the oldest entries go first.
_SUMMARIES_KEPT: Final = 512


def latest_full_checks(session: Session, package_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
    """Each drawing set's newest full check, for the Documents list and the sidebar: its chat,
    when it ran, why it stopped (if it did), its review result (once its package is made) and its
    counts as its result screen shows them (Wrong / Right / Ask the architect / Not compared; a
    reviewer's "Not a problem" is counted in none). Its findings are read at their newest versions
    (later answers' corrections) with the reviewer's latest decisions, like `review_results`.

    One query finds every set's newest full check with what its summary depends on (how many
    decision rows it has, the chat's newest answer), without its blocks. A summary worked out
    before for the same key is reused (`_SUMMARIES`); only the others are read and worked out, with
    three more queries for all of them together. Never a query per set. A set with no full check
    is absent."""
    if not package_ids:
        return {}
    ranked = (
        select(
            AssistantChat.package_id,
            AssistantChatMessage.id.label("message_id"),
            func.row_number()
            .over(
                partition_by=AssistantChat.package_id,
                order_by=(AssistantChatMessage.created_at.desc(), AssistantChatMessage.seq.desc()),
            )
            .label("rank"),
        )
        .join(AssistantChat, AssistantChat.id == AssistantChatMessage.chat_id)
        .where(
            AssistantChat.package_id.in_(list(package_ids)),
            AssistantChatMessage.role == "assistant",
            AssistantChatMessage.mode == "full_check",
        )
        .subquery()
    )
    later = aliased(AssistantChatMessage)
    decisions = (
        select(func.count())
        .where(AssistantFindingDecision.message_id == AssistantChatMessage.id)
        .correlate(AssistantChatMessage)
        .scalar_subquery()
    )
    last_seq = (
        select(func.coalesce(func.max(later.seq), 0))
        .where(later.chat_id == AssistantChatMessage.chat_id, later.role == "assistant")
        .correlate(AssistantChatMessage)
        .scalar_subquery()
    )
    heads = session.execute(
        select(
            ranked.c.package_id,
            AssistantChatMessage.id,
            AssistantChatMessage.chat_id,
            AssistantChatMessage.seq,
            AssistantChatMessage.created_at,
            AssistantChatMessage.stopped,
            decisions.label("decisions"),
            last_seq.label("last_seq"),
        )
        .join(AssistantChatMessage, AssistantChatMessage.id == ranked.c.message_id)
        .where(ranked.c.rank == 1)
    ).all()
    if not heads:
        return {}
    keys = {
        head.id: (head.id, int(head.decisions), max(int(head.last_seq), head.seq)) for head in heads
    }
    with _SUMMARIES_LOCK:
        known = {key: _SUMMARIES[key] for key in keys.values() if key in _SUMMARIES}
    missing = [head for head in heads if keys[head.id] not in known]
    if missing:
        worked = _work_out_full_checks(session, missing)
        with _SUMMARIES_LOCK:
            for head in missing:
                _SUMMARIES[keys[head.id]] = known[keys[head.id]] = worked[head.id]
            while len(_SUMMARIES) > _SUMMARIES_KEPT:
                del _SUMMARIES[next(iter(_SUMMARIES))]
    return {
        head.package_id: {
            "chat_id": head.chat_id,
            "message_id": head.id,
            "ran_at": head.created_at,
            "stopped": head.stopped,
            **known[keys[head.id]],
        }
        for head in heads
    }


def _work_out_full_checks(session: Session, heads: Sequence[Any]) -> dict[UUID, dict[str, Any]]:
    """The review result and counts of these newest full checks (`latest_full_checks`)."""
    answers = {
        row.id: row
        for row in session.scalars(
            select(AssistantChatMessage).where(
                AssistantChatMessage.id.in_([head.id for head in heads])
            )
        )
    }
    later: dict[UUID, list[AssistantChatMessage]] = {}
    for row in session.scalars(
        select(AssistantChatMessage)
        .where(
            or_(
                *(
                    and_(
                        AssistantChatMessage.chat_id == head.chat_id,
                        AssistantChatMessage.seq > head.seq,
                    )
                    for head in heads
                )
            ),
            AssistantChatMessage.role == "assistant",
        )
        .order_by(AssistantChatMessage.chat_id, AssistantChatMessage.seq)
    ):
        later.setdefault(row.chat_id, []).append(row)
    decided = latest_decisions_of(session, list(answers))
    out: dict[UUID, dict[str, Any]] = {}
    for message in answers.values():
        corrections = corrections_by_answer(later.get(message.chat_id, [])).get(str(message.id))
        blocks = with_decisions(
            with_corrections(message.blocks, corrections), decided.get(message.id)
        )
        shown = blocks or []
        counts = proof_counts(
            block["finding"]
            for block in shown
            if block.get("type") == "finding" and isinstance(block.get("finding"), Mapping)
        )
        found = _review_action(message, shown)
        out[message.id] = {
            "review_result": (
                None if found is None else {"action": found["action"], "words": found["words"]}
            ),
            "counts": {
                "wrong": counts["clouded"],
                "right": counts["right"],
                "ask_architect": counts["rule_note"],
                "likely_problem": counts["likely_problem"],
                "not_compared": counts["not_compared"],
            },
        }
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


def recorded_worker_spend(session: Session, package_id: UUID) -> int:
    """What GV Review's own reading of the package cost (the worker's paid readers at upload, the
    AI page review), from the Usage page's rows (#1770): every recorded call of the package's
    revisions, or of their workflow runs, that is not a chat call. The chat's set budget does not
    count it; the per-set meter shows it beside the chats' spend."""
    revisions = select(PackageRevision.id).where(PackageRevision.package_id == package_id)
    runs = (
        select(ExtractionRun.id)
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(WorkflowRun.package_revision_id.in_(revisions))
    )
    return int(
        session.scalar(
            select(func.coalesce(func.sum(ModelInvocation.cost_micros), 0)).where(
                or_(
                    ModelInvocation.extraction_run_id.in_(runs),
                    ModelInvocation.package_revision_id.in_(revisions),
                ),
                ModelInvocation.prompt_id.not_like(f"{CHAT_PROMPT_PREFIX}%"),
            )
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
