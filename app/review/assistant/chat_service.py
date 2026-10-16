"""One chat message from start to stored answer (#1463), and the messages running now.

`run_message` runs in its own thread (the HTTP stream only relays its events):

1. The question is stored at once, then `message_start` (so the chat and its list show it while
   it is answered).
2. The drawing set's money cap: a set that has used its budget is not asked (`stopped: cost_cap`).
3. The chat's folder: the set's latest PDFs copied into its uploads (again only when they changed),
   the GV review skill built into its skills folder the first time.
4. The sandbox: Docker, else macOS `sandbox-exec`; none is a plain error, never a guess.
5. The engine (`chat_engine.py`) streams and runs the tools; each model call is recorded where the
   Usage page reads it, labelled by chat (`template_id = chat:<id>`).
6. The answer is stored (always, whatever stopped it), then `error` (if any) and `message_end` with
   the stored message.

**One message at a time per chat** (`ChatRegistry.claim`); a second is refused (409) before its
stream starts. Only Stop (`POST /chats/{id}/cancel`) cancels: it sets the message's cancel flag and
kills the sandbox commands running for it. A screen going away changes nothing (#1479): the
message's events go into its `MessageFeed`, which any screen can follow again, and it is stored when
it ends, like claude.ai.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, monotonic_ns
from typing import Any, Final
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.review.assistant.chat_contract import ChatMessageOut
from app.review.assistant.chat_engine import (
    ChatEngine,
    EngineSettings,
    MessageOutcome,
    cap_text,
    cost_text,
)
from app.review.assistant.chat_feed import MessageFeed
from app.review.assistant.chat_findings import products_words
from app.review.assistant.chat_provider import ChatProvider
from app.review.assistant.chat_request import ChatModel
from app.review.assistant.chat_store import (
    add_assistant_message,
    add_file,
    add_user_message,
    file_url,
    files_of,
    history,
    message_out,
    messages_of,
    recorded_chat_spend,
    stored_chat_spend,
)
from app.review.assistant.chat_tools import ChatTools, ResultsLoader
from app.review.assistant.chat_unknowns import asks_for_the_rest, last_unknowns, work_list_note
from app.review.assistant.sandbox import UPLOADS, ChatWorkspace, Sandbox, safe_file_name
from app.runs.rates import ModelRates

__all__ = [
    "FULL_CHECK_INSTRUCTION",
    "ChatRegistry",
    "MessageJob",
    "RunningMessage",
    "SessionChatLedger",
    "UploadSource",
    "package_spent_micros",
    "run_message",
    "sync_uploads",
]

_log = logging.getLogger(__name__)

#: A claim whose message never started (its stream was never read) is stale after this long.
_STALE_CLAIM_SECONDS: Final = 60.0
_FALLBACK_SKILL: Final = """# GV review

The GV review skill could not be prepared on this server for this chat. Review the drawings with
the tools you have:

- Measurements are in inches; answer in inches.
- GV's markup (annotations GV reviewers added to the PDFs) is GV's notes, never the vendor's or the
  architect's numbers.
- You cannot change the review; the reviewer decides and signs off.
"""

type Emit = Callable[[str, dict[str, Any]], None]
#: Writes the GV review skill for a package revision into a folder (`skills_export`).
type SkillBuilder = Callable[..., None]
"""`(session, revision_id, dest, products=...)`: `products` only when the reviewer picked some."""


@dataclass(frozen=True, slots=True)
class UploadSource:
    """One PDF of the drawing set's latest revision, read only when the chat needs its bytes."""

    name: str
    sha256: str
    kind: str
    pages: int
    read: Callable[[], bytes]


# ---- the messages running now -------------------------------------------------------------------


@dataclass(slots=True)
class RunningMessage:
    chat_id: UUID
    package_id: UUID
    message_id: UUID
    cancel: threading.Event = field(default_factory=threading.Event)
    claimed_at: float = field(default_factory=monotonic)
    started: bool = False
    engine: ChatEngine | None = None
    sandbox: Sandbox | None = None
    feed: MessageFeed = field(default_factory=MessageFeed)
    """Its events so far, for the screens following it (the sender's, a reload's, another tab's)."""

    def spent(self) -> int:
        engine = self.engine
        return 0 if engine is None else engine.spent

    def stop(self) -> None:
        """Cancel: the engine stops between steps and mid-stream; running commands are killed."""
        self.cancel.set()
        engine = self.engine
        if engine is not None:
            engine.stop_streams()
        sandbox = self.sandbox
        if sandbox is not None:
            try:
                sandbox.kill_all()
            except Exception:  # noqa: BLE001 - cancelling never fails loudly
                _log.warning("assistant chat could not stop its sandbox commands")


class ChatRegistry:
    """The messages being answered in this process, one per chat at most."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[UUID, RunningMessage] = {}

    def claim(self, chat_id: UUID, package_id: UUID, message_id: UUID) -> RunningMessage | None:
        """The chat's slot for a new message, or `None` when one is being answered."""
        with self._lock:
            current = self._runs.get(chat_id)
            if current is not None and (
                current.started or monotonic() - current.claimed_at < _STALE_CLAIM_SECONDS
            ):
                return None
            if current is not None:
                # A claim whose message never started: anything following it stops waiting.
                current.feed.close()
            run = RunningMessage(chat_id=chat_id, package_id=package_id, message_id=message_id)
            self._runs[chat_id] = run
            return run

    def release(self, run: RunningMessage) -> None:
        with self._lock:
            if self._runs.get(run.chat_id) is run:
                del self._runs[run.chat_id]

    def get(self, chat_id: UUID) -> RunningMessage | None:
        with self._lock:
            return self._runs.get(chat_id)

    def cancel(self, chat_id: UUID) -> bool:
        run = self.get(chat_id)
        if run is None:
            return False
        run.stop()
        return True

    def running_spent(self, package_id: UUID, *, exclude: RunningMessage | None = None) -> int:
        with self._lock:
            runs = [
                r for r in self._runs.values() if r.package_id == package_id and r is not exclude
            ]
        return sum(run.spent() for run in runs)


def package_spent_micros(
    session: Session,
    package_id: UUID,
    registry: ChatRegistry,
    *,
    exclude: RunningMessage | None = None,
) -> int:
    """What the drawing set's chats have spent: the stored messages plus the ones running now
    (except `exclude`), or the Usage page's recorded calls when those say more (a message that
    never finished), less what `exclude` itself recorded so far."""
    stored = stored_chat_spend(session, package_id)
    running = registry.running_spent(package_id, exclude=exclude)
    own = 0 if exclude is None else exclude.spent()
    recorded = recorded_chat_spend(session, package_id) - own
    return max(stored + running, recorded)


# ---- the Usage page's rows ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionChatLedger:
    """Each call recorded at once in its own session, labelled by chat (`chat:<id>`)."""

    factory: sessionmaker[Session]
    package_revision_id: UUID | None
    chat_id: UUID

    def record(
        self,
        *,
        model_key: str,
        prompt_id: str,
        started_ns: int,
        input_tokens: int,
        output_tokens: int,
        stop_reason: str | None,
        error: BaseException | None,
    ) -> None:
        if self.package_revision_id is None:
            return
        from app.runs.invocations import BedrockConverseInvocationRecorder

        response = (
            None
            if error is not None and not (input_tokens or output_tokens)
            else {
                "stopReason": stop_reason,
                "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
            }
        )
        with self.factory() as session:
            try:
                BedrockConverseInvocationRecorder(session, self.package_revision_id).record(
                    model_id=model_key,
                    prompt_id=prompt_id,
                    template_id=f"chat:{self.chat_id}",
                    started_ns=started_ns,
                    response=response,
                    error=error,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise


# ---- the chat's folder --------------------------------------------------------------------------


def _size_words(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{max(1, round(size / 1000))} KB"


def sync_uploads(
    workspace: ChatWorkspace, sources: Sequence[UploadSource]
) -> tuple[list[dict[str, Any]], bool]:
    """The chat's uploads made equal to the drawing set's latest PDFs (read-only, their uploaded
    names). Returns the listing and whether it changed (it is rewritten only then)."""
    manifest = workspace.root / "uploads.json"
    names: list[str] = []
    wanted: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        chosen = safe_file_name(source.name, f"document-{index}.pdf")
        while chosen in names:
            chosen = f"{Path(chosen).stem}-{index}.pdf"
        names.append(chosen)
        wanted.append(
            {"name": chosen, "sha256": source.sha256, "pages": source.pages, "kind": source.kind}
        )
    try:
        current = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    except ValueError:
        current = None
    same = (
        isinstance(current, list)
        and [(item.get("name"), item.get("sha256")) for item in current]
        == [(item["name"], item["sha256"]) for item in wanted]
        and all((workspace.uploads / item["name"]).is_file() for item in wanted)
    )
    if same:
        assert isinstance(current, list)
        return current, False
    uploads = workspace.uploads
    uploads.mkdir(parents=True, exist_ok=True)
    uploads.chmod(0o755)
    for old in uploads.iterdir():
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
        else:
            old.chmod(0o644) if not old.is_symlink() else None
            old.unlink()
    for item, source in zip(wanted, sources, strict=True):
        data = source.read()
        if hashlib.sha256(data).hexdigest() != source.sha256:
            raise ValueError("a stored PDF does not match its recorded hash")
        path = uploads / item["name"]
        path.write_bytes(data)
        path.chmod(0o444)
        item["size"] = len(data)
    uploads.chmod(0o555)
    manifest.write_text(json.dumps(wanted, ensure_ascii=False), encoding="utf-8")
    return wanted, True


def uploaded_files_text(listing: Sequence[dict[str, Any]], *, changed: bool) -> str:
    """The `<uploaded_files>` block of a chat's first message (and of a message after the set's
    PDFs changed)."""
    lines = [
        f'<file path="{UPLOADS}/{item["name"]}" pages="{item.get("pages", "?")}" '
        f'size="{_size_words(int(item.get("size") or 0))}" kind="{item.get("kind", "")}"/>'
        for item in listing
    ]
    if not lines:
        lines = ["(none: this drawing set has no PDFs yet)"]
    block = "<uploaded_files>\n" + "\n".join(lines) + "\n</uploaded_files>"
    if changed:
        return "The drawing set's PDFs have changed since earlier in this chat:\n" + block
    return block


def _readable(folder: Path) -> None:
    """The skill folder readable by the sandbox's unprivileged user."""
    for directory, dirs, files in os.walk(folder):
        Path(directory).chmod(0o755)
        for name in files:
            (Path(directory) / name).chmod(0o644)
        del dirs


def prepare_skill(
    workspace: ChatWorkspace,
    factory: sessionmaker[Session],
    revision_id: UUID | None,
    builder: SkillBuilder | None,
    products: Sequence[str] | None = None,
) -> None:
    """The GV review skill in the chat's skills folder: built once per chat, and again when the
    reviewer picks other products (their rules come first in `rules.md`)."""
    skill = workspace.skills
    marker = workspace.root / "skill_products.json"
    wanted = None if products is None else json.dumps(sorted(products))
    try:
        had = marker.read_text(encoding="utf-8") if marker.exists() else None
    except OSError:
        had = None
    if (skill / "SKILL.md").is_file() and (wanted is None or had == wanted):
        return
    skill.mkdir(parents=True, exist_ok=True)
    built = False
    if builder is not None and revision_id is not None:
        try:
            with factory() as session:
                if products is None:
                    builder(session, revision_id, skill)
                else:
                    builder(session, revision_id, skill, products=list(products))
            built = (skill / "SKILL.md").is_file()
        except Exception as failed:  # noqa: BLE001 - the chat still works without the skill
            _log.warning("assistant chat skill could not be built: %s", type(failed).__name__)
    if built and wanted is not None:
        marker.write_text(wanted, encoding="utf-8")
    if not (skill / "SKILL.md").is_file():
        (skill / "SKILL.md").write_text(_FALLBACK_SKILL, encoding="utf-8")
    _readable(skill)


#: The full check's own instruction (CONTRACT4): the reviewer presses a button, never types it.
FULL_CHECK_INSTRUCTION: Final = """Full check of this drawing set (the reviewer pressed Run full check). Read /mnt/skills/gv-review/SKILL.md and rules.md first, and call review_results for the whole set.

Review the WHOLE set: GV's rules for the picked products first, then compare everything on the vendor's drawings with the architect's (brand prototype differences as their own group). Report EVERY item you compare with report_finding, the right ones too (status same), with boxes. Check each finding the app made from GV Review's recorded results. Brief helpers to report findings and set_coverage, and every dimension string they read as a finding with parts and printed_total (source vendor_arithmetic) so the app adds it up. Record every page with set_coverage. Verify leads. End with 2-4 short lines of notes, no tables: the app draws the cloud drawing, the report and the lists."""


def full_check_label(products: Sequence[str], effort: str) -> str:
    """The question a full check shows: "Full check · Countertops · High effort"."""
    return f"Full check \u00b7 {products_words(products)} \u00b7 {effort.capitalize()} effort"


def focus_line(products: Sequence[str]) -> str:
    words = products_words(products).lower()
    return (
        f"Focus for this chat: {words}. GV's rules for them come first in "
        "/mnt/skills/gv-review/rules.md."
    )


def default_skill_builder() -> SkillBuilder:
    """The skills builder's `build_skill_folder` (#1465), loaded when a chat first needs it."""
    from app.review.assistant.skills_export import build_skill_folder

    return build_skill_folder


# ---- one message --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageJob:
    """Everything one message needs, gathered by the route before its stream starts."""

    chat_id: UUID
    package_id: UUID
    revision_id: UUID | None
    created_by: str
    text: str
    model: ChatModel
    effort: str
    thinking: bool
    chat_root: Path
    factory: sessionmaker[Session]
    provider: ChatProvider
    registry: ChatRegistry
    lead_max_turns: int
    helper_max_turns: int
    message_cap_micros: int
    package_cap_micros: int
    rates: Callable[[str], ModelRates | None]
    uploads: Callable[[], Sequence[UploadSource]]
    results: ResultsLoader | None
    open_sandbox: Callable[[ChatWorkspace], Sandbox | None]
    skill_builder: SkillBuilder | None = None
    mode: str = "chat"
    products: tuple[str, ...] | None = None
    """The products picked in the composer, else the set's own (`None`: not stated)."""


_NO_SANDBOX: Final = (
    "The chat's computer (the sandbox) is not running on this server, so Claude cannot open the "
    "drawings. Start Docker Desktop (with the gv-assistant-sandbox image built), then ask again."
)
_NO_UPLOADS: Final = (
    "The drawing set's PDFs could not be copied into the chat's computer. Please ask again; if it "
    "keeps happening, tell your admin."
)
_NOT_SAVED: Final = (
    "This message could not be saved, so it was not sent to Claude. Please try again."
)


def _budget_used(job: MessageJob) -> str:
    return (
        f"This drawing set has used its ${cap_text(job.package_cap_micros)} AI budget, so Claude "
        "was not asked. Your admin can raise GV_ASSISTANT_PACKAGE_CAP_USD."
    )


def run_message(job: MessageJob, run: RunningMessage, emit: Emit | None = None) -> None:
    """One message, start to stored answer. Never raises; always ends with `message_end`, except
    when even the question could not be stored (then `error`, and the stream ends). Its events go
    into `run.feed` (and to `emit`, when given); the feed closes when it ends."""
    feed = run.feed

    def publish(event: str, data: dict[str, Any]) -> None:
        feed.publish(event, data)
        if emit is not None:
            emit(event, data)

    try:
        _run_message(job, run, publish)
    except Exception:  # noqa: BLE001 - the stream still ends cleanly
        _log.exception("assistant chat message failed before it could be stored")
        _safe(
            publish,
            "error",
            {"message": "Something went wrong in the assistant. Please ask again."},
        )
    finally:
        # Closed before the slot is freed: a screen that finds no running message then finds the
        # stored one.
        feed.close()
        job.registry.release(run)


def _safe(emit: Emit, event: str, data: dict[str, Any]) -> None:
    try:
        emit(event, data)
    except Exception:  # noqa: BLE001 - a gone screen never stops the answer being stored
        _log.debug("assistant chat event could not be sent")


def _run_message(job: MessageJob, run: RunningMessage, emit: Emit) -> None:
    run.started = True
    # The question is stored before `message_start`, so a chat list read when the screen sees
    # `message_start` already shows the chat, titled by its first question.
    try:
        shown = (
            full_check_label(job.products or (), job.effort)
            if job.mode == "full_check"
            else job.text
        )
        with job.factory() as session:
            add_user_message(session, chat_id=job.chat_id, created_by=job.created_by, text=shown)
            session.commit()
    except Exception:  # noqa: BLE001 - told plainly; nothing was sent
        _log.exception("assistant chat could not store the question")
        _safe(emit, "error", {"message": _NOT_SAVED})
        return
    _safe(
        emit,
        "message_start",
        {
            "message_id": str(run.message_id),
            "model": job.model.id,
            "effort": job.effort,
            "thinking": job.thinking,
        },
    )

    def finish(outcome: MessageOutcome) -> None:
        _finish(job, run, emit, outcome)

    def stop_with(
        text: str, stopped: str, error: str | None = None, cap_hit: str | None = None
    ) -> None:
        if text:
            _safe(emit, "text_delta", {"index": 0, "text": text})
        finish(
            MessageOutcome(
                text=text,
                blocks=[{"type": "text", "text": text}] if text else [],
                api_content=[],
                # Nothing was asked yet: what this message spent is what its run spent (nothing).
                cost_micros=run.spent(),
                stopped=stopped,
                error=error,
                cap_hit=cap_hit,
            )
        )

    def package_spent() -> int:
        with job.factory() as session:
            return package_spent_micros(session, job.package_id, job.registry, exclude=run)

    if package_spent() >= job.package_cap_micros:
        stop_with(_budget_used(job), "cost_cap", cap_hit="package")
        return
    workspace = ChatWorkspace.open(job.chat_root / str(job.chat_id))
    try:
        listing, changed = sync_uploads(workspace, job.uploads())
    except Exception as failed:  # noqa: BLE001 - told plainly
        _log.warning("assistant chat could not copy the PDFs: %s", type(failed).__name__)
        stop_with("", "error", _NO_UPLOADS)
        return
    prepare_skill(workspace, job.factory, job.revision_id, job.skill_builder, job.products)
    sandbox = job.open_sandbox(workspace)
    if sandbox is None:
        stop_with("", "error", _NO_SANDBOX)
        return
    run.sandbox = sandbox
    with job.factory() as session:
        replay = history(session, job.chat_id)
        # "Check the rest": the last answer's unknowns become this answer's work list.
        work_list = None
        if job.mode != "full_check" and asks_for_the_rest(job.text):
            work_list = work_list_note(
                last_unknowns(
                    message.blocks
                    for message in messages_of(session, job.chat_id)
                    if message.role == "assistant"
                )
            )
    user_content: list[dict[str, Any]] = []
    # The files are listed in the first message the model sees (an earlier message that got no
    # answer is not in its history), and again when the set's PDFs changed.
    if not replay or changed:
        user_content.append(
            {"type": "text", "text": uploaded_files_text(listing, changed=changed and bool(replay))}
        )
    focus = None
    if job.products is not None:
        marker = workspace.root / "chat_products.json"
        wanted = json.dumps(list(job.products))
        before = marker.read_text(encoding="utf-8") if marker.exists() else None
        if before != wanted or not replay:
            focus = focus_line(job.products)
        marker.write_text(wanted, encoding="utf-8")
    if focus is not None:
        user_content.append({"type": "text", "text": focus})
    if work_list is not None:
        user_content.append({"type": "text", "text": work_list})
    question = FULL_CHECK_INSTRUCTION if job.mode == "full_check" else job.text
    user_content.append({"type": "text", "text": question})

    def register_file(file_id: str, name: str, size: int, mime: str, sha256: str) -> dict[str, Any]:
        with job.factory() as session:
            add_file(
                session,
                file_id=UUID(file_id),
                chat_id=job.chat_id,
                message_id=run.message_id,
                name=name,
                size=size,
                mime=mime,
                sha256=sha256,
            )
            session.commit()
        shown = {
            "id": file_id,
            "name": name,
            "size": size,
            "mime": mime,
            "url": file_url(job.chat_id, UUID(file_id)),
        }
        _safe(emit, "file", {"file": shown})
        return shown

    def read_picture(picture_id: str) -> bytes:
        return (workspace.pictures / f"{UUID(picture_id)}.png").read_bytes()

    engine = ChatEngine(
        settings=EngineSettings(
            model=job.model,
            effort=job.effort,
            thinking=job.thinking,
            lead_max_turns=job.lead_max_turns,
            helper_max_turns=job.helper_max_turns,
            message_cap_micros=job.message_cap_micros,
            package_cap_micros=job.package_cap_micros,
            rates=job.rates,
            mode=job.mode,
            products=job.products or (),
        ),
        provider=job.provider,
        tools=ChatTools(
            workspace=workspace,
            sandbox=sandbox,
            page_count=max((int(item.get("pages") or 0) for item in listing), default=0),
            results=job.results,
            register_file=register_file,
            uploads=listing,
        ),
        ledger=SessionChatLedger(job.factory, job.revision_id, job.chat_id),
        emit=lambda event, data: _safe(emit, event, data),
        history=replay,
        user_content=user_content,
        package_spent=package_spent,
        read_picture=read_picture,
        cancel=run.cancel,
    )
    run.engine = engine
    if run.cancel.is_set():
        stop_with("", "cancelled")
        return
    outcome = engine.run()
    if outcome.stopped == "cancelled":
        sandbox.kill_all()
    finish(outcome)


def _finish(job: MessageJob, run: RunningMessage, emit: Emit, outcome: MessageOutcome) -> None:
    """The answer stored, then `error` (if any) and `message_end` with the stored message."""
    stored: ChatMessageOut | None = None
    started = monotonic_ns()
    try:
        with job.factory() as session:
            message = add_assistant_message(
                session,
                message_id=run.message_id,
                chat_id=job.chat_id,
                created_by=job.created_by,
                model=job.model.id,
                effort=job.effort,
                thinking=job.thinking,
                text=outcome.text,
                blocks=outcome.blocks,
                api_content=outcome.api_content,
                cost_micros=outcome.cost_micros,
                stopped=outcome.stopped,
                error=outcome.error,
                message_cap_micros=job.message_cap_micros,
                package_cap_micros=job.package_cap_micros,
                cap_hit=outcome.cap_hit if outcome.stopped == "cost_cap" else None,
            )
            session.commit()
            stored = message_out(message, files_of(session, job.chat_id))
    except Exception:  # noqa: BLE001 - told plainly
        _log.exception("assistant chat could not store the answer")
        _safe(
            emit,
            "error",
            {"message": "The answer could not be saved. It is shown here but not in the chat."},
        )
    _log.info(
        "assistant chat message ended: %s, %d blocks, %d micros, stored in %d ms",
        outcome.stopped or "done",
        len(outcome.blocks),
        outcome.cost_micros,
        (monotonic_ns() - started) // 1_000_000,
    )
    if outcome.error:
        _safe(emit, "error", {"message": outcome.error})
    _safe(emit, "usage", {"cost_usd": cost_text(outcome.cost_micros)})
    if stored is None:
        return
    _safe(emit, "message_end", {"message": stored.model_dump(mode="json")})
