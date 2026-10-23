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

**A server restart (#1514).** While a message runs, its blocks so far are saved to the chat folder's
`checkpoint.json` (replaced whole, atomically, a few times a second at most). A question left with no
reply and no message running for it in this process (the API restarted, or was killed) gets a reply
stored as `stopped: interrupted` with the checkpoint's blocks and what its calls cost
(`recover_interrupted`, at start-up and whenever the chat is read). On SIGTERM the registry stops
taking messages, gives running ones a short wait, then stores each one still running as interrupted
and stops it (`shutdown_chats`). Continue (`MessageJob.continue_from`) hands the interrupted work to
the next answer: a full check keeps its findings and covered pages and checks the rest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from time import monotonic, monotonic_ns
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import AssistantChatMessage
from app.review.assistant.chat_contract import ChatMessageOut
from app.review.assistant.chat_corrections import earlier_findings, earlier_findings_note
from app.review.assistant.chat_engine import (
    FULL_CHECK_INSTRUCTION,
    ChatEngine,
    EngineSettings,
    MessageOutcome,
    cap_text,
    cost_text,
)
from app.review.assistant.chat_feed import MessageFeed
from app.review.assistant.chat_findings import (
    live_findings,
    main_upload,
    page_ranges,
    products_words,
    read_listing,
)
from app.review.assistant.chat_harness import chat_harness, update_note
from app.review.assistant.chat_memory import add_note, notes_block, set_notes
from app.review.assistant.chat_provider import ChatProvider
from app.review.assistant.chat_request import ChatModel
from app.review.assistant.chat_store import (
    add_assistant_message,
    add_file,
    add_interrupted_message,
    add_user_message,
    chat_cost_since,
    file_url,
    files_of,
    history,
    message_out,
    messages_of,
    recorded_chat_spend,
    stored_chat_spend,
    unanswered_questions,
)
from app.review.assistant.chat_tools import (
    FABLE_NOTE,
    MAX_AGENTS,
    ChatTools,
    ResultsLoader,
    named_pages,
)
from app.review.assistant.chat_unknowns import asks_for_the_rest, last_unknowns, work_list_note
from app.review.assistant.full_check_plan import (
    MAX_JOB_PAGES,
    read_page_map,
    reading_of,
    scoped_plan,
)
from app.review.assistant.live_check import LiveCheck
from app.review.assistant.package_store import numbering
from app.review.assistant.plan_check import (
    MAX_DOUBTS,
    answer_note,
    doubts,
    plan_summary,
    read_answers,
    record_answers,
)
from app.review.assistant.product_scope import (
    PriceRule,
    ProductScope,
    final_blocks,
    no_rules_note,
    scoped_loader,
)
from app.review.assistant.sandbox import UPLOADS, ChatWorkspace, Sandbox, safe_file_name
from app.review.assistant.send_package import PackageIdentity
from app.review.assistant.sharpen import apply_sharpen
from app.review.assistant.vendor_consistency import page_texts
from app.runs.rates import ModelRates

__all__ = [
    "CHECKPOINT_FILE",
    "CONTINUE_TEXT",
    "FULL_CHECK_INSTRUCTION",
    "PAID_JOB_WORDS",
    "ChatRegistry",
    "MessageJob",
    "PaidJobRunning",
    "RunningMessage",
    "SessionChatLedger",
    "UploadSource",
    "continuable",
    "package_spent_micros",
    "recover_interrupted",
    "resume_of",
    "run_message",
    "set_pack_state",
    "shutdown_chats",
    "sync_uploads",
    "write_checkpoint",
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
    question_id: UUID | None = None
    """The stored question, once stored."""
    checkpoint: Path | None = None
    """Where its work so far is saved (`write_checkpoint`), once the question is stored."""
    resume: dict[str, Any] | None = None
    """What the checkpoint keeps besides the blocks: model, effort, thinking, mode, products."""
    factory: sessionmaker[Session] | None = None
    checkpointed_at: float = 0.0
    save_due: bool = False
    """A save was put off by the rate limit: a timer makes it, so a pause loses nothing."""
    store_lock: threading.Lock = field(default_factory=threading.Lock)
    """Held while its answer is stored: the answer, or (at shutdown) its interrupted reply."""
    finished: bool = False
    """Its answer is being (or was) stored the ordinary way."""
    abandoned: bool = False
    """Stored as interrupted at shutdown: its own answer is then never stored."""
    live: LiveCheck | None = None
    """A full check's live state (#1710): drawings, phases, decided findings, stopping."""
    paid: bool = False
    """A paid full check (#1770): one at a time on this server, whatever the chat."""

    def spent(self) -> int:
        engine = self.engine
        return 0 if engine is None else engine.spent

    def stop(self) -> None:
        """Cancel: the engine stops between steps and mid-stream; running commands are killed."""
        self.cancel.set()
        live = self.live
        if live is not None:
            try:
                live.stopping()  # the live screen: "Stopping…", the drawings still finishing
            except Exception:  # noqa: BLE001 - cancelling never fails loudly
                _log.warning("assistant chat could not show the stop")
        engine = self.engine
        if engine is not None:
            engine.stop_streams()
        sandbox = self.sandbox
        if sandbox is not None:
            try:
                sandbox.kill_all()
            except Exception:  # noqa: BLE001 - cancelling never fails loudly
                _log.warning("assistant chat could not stop its sandbox commands")


class PaidJobRunning(RuntimeError):
    """Another paid full check runs now (`ChatRegistry.claim`, #1770): one paid job at a time."""

    def __init__(self, other: RunningMessage) -> None:
        super().__init__(PAID_JOB_WORDS)
        self.other = other


#: The refusal of a second paid full check (#1770, "one paid job at a time" per account).
PAID_JOB_WORDS: Final = (
    "Another full check is running now (in another chat). Only one full check runs at a time, "
    "so the money it spends is never doubled: wait for it to finish, or stop it, then start this "
    "one."
)


class ChatRegistry:
    """The messages being answered in this process, one per chat at most, and one paid full check
    at most across every chat (#1770: one paid job at a time per account)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[UUID, RunningMessage] = {}
        self._closing = False

    @property
    def closing(self) -> bool:
        """The server is stopping: no new message is taken."""
        with self._lock:
            return self._closing

    def close(self) -> list[RunningMessage]:
        """Take no new message from now on; the messages running now."""
        with self._lock:
            self._closing = True
            return list(self._runs.values())

    def runs(self) -> list[RunningMessage]:
        with self._lock:
            return list(self._runs.values())

    def claim(
        self, chat_id: UUID, package_id: UUID, message_id: UUID, *, paid: bool = False
    ) -> RunningMessage | None:
        """The chat's slot for a new message, or `None` when one is being answered (or the server
        is stopping). `paid`: a full check that makes paid calls; raises `PaidJobRunning` while
        another one runs in any chat (#1770)."""
        with self._lock:
            if self._closing:
                return None
            current = self._runs.get(chat_id)
            if current is not None and _holds(current):
                return None
            if paid:
                other = next(
                    (
                        run
                        for run in self._runs.values()
                        if run.paid and run.chat_id != chat_id and _holds(run)
                    ),
                    None,
                )
                if other is not None:
                    raise PaidJobRunning(other)
            if current is not None:
                # A claim whose message never started: anything following it stops waiting.
                current.feed.close()
            run = RunningMessage(
                chat_id=chat_id, package_id=package_id, message_id=message_id, paid=paid
            )
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


def _holds(run: RunningMessage) -> bool:
    """Whether a claimed message still holds its slot: it started, or was claimed only now."""
    return run.started or monotonic() - run.claimed_at < _STALE_CLAIM_SECONDS


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


#: One copy of a folder's PDFs at a time (`sync_uploads`): the plan check asked again while the
#: first ask still copies a big set waits for it, instead of copying the set again beside it.
_SYNC_LOCKS: dict[Path, threading.Lock] = {}
_SYNC_LOCKS_GUARD: Final = threading.Lock()


def sync_uploads(
    workspace: ChatWorkspace, sources: Sequence[UploadSource]
) -> tuple[list[dict[str, Any]], bool]:
    """`_sync_uploads`, one at a time for the same folder (`_SYNC_LOCKS`)."""
    with _SYNC_LOCKS_GUARD:
        lock = _SYNC_LOCKS.setdefault(workspace.root.resolve(), threading.Lock())
    with lock:
        return _sync_uploads(workspace, sources)


def _sync_uploads(
    workspace: ChatWorkspace, sources: Sequence[UploadSource]
) -> tuple[list[dict[str, Any]], bool]:
    """The chat's uploads made equal to the drawing set's latest PDFs (read-only, their uploaded
    names). Returns the listing and whether it changed (it is rewritten only then).

    **Each PDF goes in through the front door** (`extraction/working_copy.py`, #1724): what the
    chat, its set pack and its outputs read is the standard working copy (rotation turned into the
    page, the visible area at the origin, the user unit applied), shown exactly as the original.
    The original stays untouched in the set's storage. A file already in that shape is copied byte
    for byte; one that was changed is listed with its working copy's `working_sha256` and what was
    done (`working_changes`, `pages_not_read`), so its set pack is made from the working copy."""
    manifest = workspace.root / "uploads.json"
    # Loaded by name: the front door opens PDFs, which the control plane never imports.
    front_door = import_module("extraction.working_copy")
    version = front_door.WORKING_COPY_VERSION
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
        and [(item.get("name"), item.get("sha256"), item.get("intake")) for item in current]
        == [(item["name"], item["sha256"], version) for item in wanted]
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
        item["intake"] = version
        try:
            working = front_door.working_copy(data)
        except front_door.WorkingCopyRefused as refused:
            # Copied as it is: the readers then refuse it with their own plain reasons.
            item["intake_problem"] = str(refused)
        else:
            # What the original says about itself, for the set pack's health check.
            item["intake_facts"] = working.facts
            if working.changed:
                data = working.data
                item["working_sha256"] = working.sha256
                item["working_changes"] = list(working.changes)
                if working.not_read:
                    item["pages_not_read"] = [
                        {"page": page, "why": why} for page, why in working.not_read
                    ]
        path = uploads / item["name"]
        path.write_bytes(data)
        path.chmod(0o444)
        item["size"] = len(data)
    uploads.chmod(0o555)
    manifest.write_text(json.dumps(wanted, ensure_ascii=False), encoding="utf-8")
    return wanted, True


def start_set_pack(
    workspace: ChatWorkspace, chat_root: Path, listing: Sequence[Mapping[str, Any]]
) -> ChatWorkspace:
    """The chat's workspace with its set pack (`set_pack.py`), whose building starts in the
    background when it is not made yet: the message never waits for it."""
    if not listing:
        return workspace
    try:
        pack = import_module("app.review.assistant.set_pack")
        folder: Path = pack.pack_folder(chat_root, listing)
        pack.ensure_pack(folder, workspace.uploads, listing)
    except Exception as failed:  # noqa: BLE001 - the chat works without the pack
        _log.warning("assistant set pack could not start: %s", type(failed).__name__)
        return workspace
    return replace(workspace, set_pack=folder.resolve())


def _png_size(path: Path) -> tuple[int, int] | None:
    """A PNG's width and height from its header (no picture library)."""
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


#: The set pack's page pictures' dpi (`set_pack.RENDER_DPI`; that module is not imported here).
_PACK_DPI: Final = 100


#: Where the plan check copies a drawing set's PDFs (per set) to read its set pack before any chat
#: exists; the pack itself is the one the chats share (`set-packs/<key>`).
PLAN_CHECK_FOLDER: Final = "plan-check"


def _plan_root(chat_root: Path, package_id: UUID) -> Path:
    return chat_root / PLAN_CHECK_FOLDER / str(package_id)


def plan_page_picture(chat_root: Path, package_id: UUID, page: int) -> Path | None:
    """The vendor's page picture in the set's pack (drawings only, 100 dpi), if made."""
    workspace = ChatWorkspace(_plan_root(chat_root, package_id))
    listing = read_listing(workspace)
    main = main_upload(listing)
    if main is None:
        return None
    try:
        pack = import_module("app.review.assistant.set_pack")
        folder: Path = pack.pack_folder(chat_root, listing)
        picture: Path = folder / "pages" / pack.stem(str(main["name"])) / f"p-{page:03d}.png"
    except Exception:  # noqa: BLE001 - no picture, the question still shows
        return None
    return picture if picture.is_file() else None


def full_check_plan(
    chat_root: Path,
    package_id: UUID,
    uploads: Callable[[], Sequence[UploadSource]],
    picture_url: Callable[[int], str],
    products: Sequence[str] | None = None,
    price: Callable[[int], int] | None = None,
    missing_values: Sequence[str] = (),
) -> dict[str, Any]:
    """The plan check before a full check (#1569): code's plan in one line and the 0-3 questions
    where it is unsure of the page map (`plan_check.doubts`), each with its page's picture and the
    drawing marked; with `price` (content pages in scope -> millionths of $, #1823), the full
    check's fixed price for `products` (`None`: every active product) and the pages it counts:
    the same count the full check plans (`full_check_plan.scoped_plan`). The set's latest PDFs
    are copied to the set's plan-check folder (only when they changed) and the set pack is started
    when it is not made yet, so it works before the set's first chat; `building` until the page
    map is made. With the page map, the pre-flight health report too (`preflight_words`).
    Never an AI call."""
    workspace = ChatWorkspace.open(_plan_root(chat_root, package_id))
    listing, _changed = sync_uploads(workspace, uploads())
    if not listing:
        return {"state": "none", "summary": None, "doubts": [], "more": 0}
    workspace = start_set_pack(workspace, chat_root, listing)
    folder = workspace.set_pack
    reading = reading_words(folder)
    page_map = read_page_map(folder)
    if folder is None or page_map is None:
        state = read_status_state(folder)
        out: dict[str, Any] = {
            "state": "none" if state == "failed" or folder is None else "building",
            "summary": None,
            "doubts": [],
            "more": 0,
            "reading": reading,
        }
        broken = broken_words(listing, folder if state == "failed" else None)
        if broken is not None:
            # A locked, damaged or empty file is said plainly before Run, never a silent skip.
            out["preflight"] = broken
        return out
    main = main_upload(listing)
    main_file = None if main is None else str(main["name"])
    found = doubts(page_map, main_file, read_answers(folder), limit=None)
    shown = []
    for doubt in found[:MAX_DOUBTS]:
        out = doubt.out()
        picture = plan_page_picture(chat_root, package_id, doubt.page)
        size = None if picture is None else _png_size(picture)
        out["picture_url"] = None if picture is None else picture_url(doubt.page)
        out["box"] = None
        if size is not None and doubt.box is not None:
            scale = _PACK_DPI / 72
            x0, y0, x1, y1 = doubt.box
            out["box"] = [
                round(max(0.0, min(1.0, value * scale / extent)), 4)
                for value, extent in ((x0, size[0]), (y0, size[1]), (x1, size[0]), (y1, size[1]))
            ]
        del out["file"]
        shown.append(out)
    answer: dict[str, Any] = {
        "state": "ready",
        "summary": plan_summary(page_map, main_file),
        "doubts": shown,
        "more": max(0, len(found) - MAX_DOUBTS),
        "reading": reading,
    }
    plan, _all = scoped_plan(
        page_map,
        main_file,
        MAX_AGENTS,
        ProductScope.of(products),
        lambda files: page_texts(folder, files),
    )
    if price is not None and plan is not None:
        answer["price_pages"] = plan.pages
        answer["price_usd"] = cost_text(price(plan.pages))
    try:
        answer["preflight"] = preflight_words(
            folder,
            listing,
            main_file,
            page_map,
            skipped=() if plan is None else plan.skipped,
            products=products or (),
            missing_values=missing_values,
        )
    except Exception as failed:  # noqa: BLE001 - the plan shows without the health report
        _log.warning("assistant pre-flight report failed: %s", type(failed).__name__)
    return answer


def broken_words(
    listing: Sequence[Mapping[str, Any]], failed_pack: Path | None
) -> dict[str, Any] | None:
    """The pre-flight stops of an upload whose page map cannot be made: a locked, damaged or empty
    file (`intake_problem`, no pages), or the reading failed. `None` when there is none."""
    from app.review.assistant import preflight

    reading = reading_of(failed_pack) if failed_pack is not None else None
    report = preflight.build(
        preflight.Inputs(
            listing=listing,
            reading_state="failed" if failed_pack is not None else "none",
            reading_why=None if reading is None else reading.why,
        )
    )
    if not report.stops:
        return None
    return {**report.out(), "lines": [line.out() for line in report.stops], "state": "ready"}


def preflight_words(
    folder: Path,
    listing: Sequence[Mapping[str, Any]],
    main_file: str | None,
    page_map: Mapping[str, Any],
    *,
    skipped: Sequence[tuple[int, str]] = (),
    products: Sequence[str] = (),
    missing_values: Sequence[str] = (),
) -> dict[str, Any]:
    """The pre-flight health report (`preflight.py`) as the Run popover shows it: `working`
    while code's pairing estimate with a separate architect's file is being worked out in the
    background (`preflight_pairs`, started here once the reading is done), else `ready`. $0."""
    from app.review.assistant import preflight

    roles = page_map.get("upload_roles") if isinstance(page_map, Mapping) else None
    across = isinstance(roles, Mapping) and "architect" in roles.values()
    working = False
    pairs = None
    if across and reading_of(folder).state == "ready":
        estimates = import_module("app.review.assistant.preflight_pairs")
        state, pairs = estimates.cached(folder)
        if state != "ready":
            pairs = None
            working = estimates.start(folder, main_file)
    files = [str(name) for name in page_map.get("files") or [] if name] or (
        [main_file] if main_file else []
    )
    inputs = preflight.gather(
        folder,
        listing=listing,
        main_file=main_file,
        texts=page_texts(folder, files),
        pairs=pairs,
        skipped=skipped,
        missing_values=missing_values,
        products=products,
    )
    report = preflight.build(inputs)
    return {**report.out(), "state": "working" if working else "ready"}


def reading_words(folder: Path | None) -> dict[str, Any] | None:
    """The plan's `reading` (#1872): how many of the set's pages are read; `None` with no pack."""
    reading = reading_of(folder)
    if reading.state not in ("reading", "ready", "failed"):
        return None
    return {
        "state": reading.state,
        "done": reading.done,
        "total": reading.total,
        "unreadable": len(reading.unreadable),
        "why": reading.why,
    }


def set_pack_state(chat_root: Path, package_id: UUID, chat_ids: Sequence[UUID] = ()) -> str | None:
    """Where the drawing set's set pack is, for the Documents list and the sidebar: `building`
    (its page map is not made yet: the plan check's `building`), `ready` (the page map is made),
    `failed`, or `None` (no copy of the set's PDFs has been made yet, so nothing was started).

    Read-only and cheap: it never copies a PDF or starts a pack (`full_check_plan` does). It reads
    the uploads last copied for the plan check, then for the given chats (newest first), and the
    first pack of theirs that was started (its `page_map.json`, `status.json`). A newer upload
    is seen once the plan check or a chat copies it."""
    pack = import_module("app.review.assistant.set_pack")
    roots = [_plan_root(chat_root, package_id), *(chat_root / str(chat) for chat in chat_ids)]
    for root in roots:
        listing = read_listing(ChatWorkspace(root))
        if not listing:
            continue
        folder: Path = pack.pack_folder(chat_root, listing)
        if (folder / "page_map.json").is_file():
            return "ready"
        if not folder.is_dir():
            continue  # a copy whose pack was never started (or an older pack format)
        return "failed" if read_status_state(folder) == "failed" else "building"
    return None


def read_status_state(folder: Path | None) -> str | None:
    """The set pack's state (`building`, `ready`, `failed`), from its `status.json`."""
    if folder is None:
        return None
    try:
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return status.get("state") if isinstance(status, dict) else None


def _set_notes_text(
    session: Session,
    job: MessageJob,
    stored: Sequence[AssistantChatMessage],
    *,
    first: bool,
) -> str | None:
    """The set notes for this message (#1548): the newest at a chat's start (notes on the pages
    the question names first), then the ones added since the chat's last message. Never stops a
    message: a failure gives no notes."""
    try:
        if first:
            rows = set_notes(session, job.package_id)
        else:
            since = max(
                (message.created_at for message in stored if message.role == "assistant"),
                default=None,
            )
            rows = set_notes(session, job.package_id, since=since, leave_out_chat=job.chat_id)
        return notes_block(rows, pages=named_pages(job.text or ""), new=not first)
    except Exception:  # noqa: BLE001 - the chat goes on without its notes
        _log.exception("assistant chat set notes could not be read")
        return None


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


def full_check_label(products: Sequence[str], effort: str) -> str:
    """The question a full check shows: "Full check · Countertops · High effort"."""
    return f"Full check \u00b7 {products_words(products)} \u00b7 {effort.capitalize()} effort"


def focus_line(products: Sequence[str], unruled: Sequence[str] = ()) -> str:
    """What the lead is told of the products (a strict scope, Anant 2026-10-18); `unruled`: the
    products in scope with no published GV rule yet."""
    scope = ProductScope.of(products)
    words = scope.label.lower()
    rules = (
        f"GV's rules for them are in /mnt/skills/gv-review/rules.md; read "
        f"/mnt/skills/gv-review/products/<id>.md for each ({', '.join(scope.products)})."
    )
    note = no_rules_note([p for p in scope.products if p in unruled])
    if note is not None:
        rules += f" {note} Say so in your answer."
    if scope.every:
        return f"Products checked in this chat: {words} (all of them). {rules}"
    return (
        f"Products checked in this chat: {words} only. Compare and report only {words}: an item "
        "of another product, or of no product, is outside this check and code drops it (never "
        f"report it, never mention it as a problem). {rules}"
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
    max_parallel_agents: int = MAX_AGENTS
    mode: str = "chat"
    products: tuple[str, ...] | None = None
    """The products picked in the composer, else the set's own (`None`: not stated)."""
    continue_from: UUID | None = None
    """Continue (#1514): the interrupted answer whose work this message goes on from."""
    drawn_helpers: bool = False
    """A full check's drawn-things helper per page group (#1569)."""
    roster: bool = False
    """A full check as a fixed-price roster of specialists, no lead (`full_check_roster`)."""
    full_check_cap_micros: int | None = None
    """The roster full check's upper bound until its price is known (the set's budget, or a lower
    `cap_usd`): the per-answer message cap never cuts a full check below its own price (#1823).
    `None`: the message cap."""
    price_rule: PriceRule = field(default_factory=PriceRule)
    """The full check's fixed price: base + per content page in scope, at least a minimum
    (#1823)."""
    numbers_effort: str = "low"
    """The roster's numbers page helpers' effort (`GV_ASSISTANT_NUMBERS_HELPER_EFFORT`)."""
    numbers_pages: int = MAX_JOB_PAGES
    """The roster's numbers page helpers' most pages each (`GV_ASSISTANT_NUMBERS_HELPER_PAGES`)."""
    pairing_proof: str = "ai_pair_plus_code"
    """What proves a pairing for a red cloud (`full_check_roster`)."""
    round_pick_cap_micros: int = 0
    """The roster's hard cap for the cheap picks on the round-only leftovers (#1861); 0: off (the
    default, Anant 2026-10-22)."""
    full_check_mode: str = "roster"
    """Which full check runs: `roster` or `simple` (`simple_check/`, #1665)."""
    simple: Mapping[str, Any] | None = None
    """The simple check's options (`simple_check.run.SimpleOptions.of`), with the pilot's size."""
    page_match_model: str | None = None
    preflight_model: str | None = None
    """The supervisor's model (`plan_supervisor.py`); `None`: off."""
    run_anyway: bool = False
    """The reviewer chose Run anyway after a pre-flight stop."""
    """The AI page matcher's model for separate uploads (#1798); `None`: off."""
    whole_look_model: str | None = None
    """The whole-drawing look's model (`whole_look`); `None`: off."""
    whole_look_effort: str = "medium"
    set_index_model: str | None = None
    """The set index's model for separate uploads (`set_index.py`); `None`: off."""
    plan_answers: Mapping[str, str] | None = None
    """The reviewer's answers to the plan check (#1569), kept with the set pack before the run."""
    sharpen_model: bool = False
    """#1551: a question no code rule settles is sharpened by the cheap model (off: code rules
    only, and no spec otherwise)."""
    sharpen_deadline_seconds: float = 1.5
    task_edit: Mapping[str, Any] | None = None
    """#1551: the task spec the reviewer corrected on the screen ("Understood as … Edit")."""
    page_cache_set_mb: int = 300
    page_cache_total_mb: int = 1000
    """#1575: the hi-res page caches' size per set and for all sets, kept after each message."""
    page_cache_workers: int = 3
    page_cache_memory_mb: int = 256
    """#1575: pages the hi-res cache draws at once, and its decoded tiles kept in memory."""
    unruled: tuple[str, ...] = ()
    """The products in scope with no published GV rule yet: the answer says so plainly."""
    identity: PackageIdentity = field(default_factory=PackageIdentity)
    """Who and what the full check's send-ready package is about (#1622)."""


_NO_SANDBOX: Final = (
    "The secure sandbox (Docker) is not running, so the AI cannot open the drawings. Start Docker "
    "Desktop (with the gv-assistant-sandbox image built), then ask again."
)
_NO_UPLOADS: Final = (
    "The drawing set's PDFs could not be copied into the chat's computer. Please ask again; if it "
    "keeps happening, tell your admin."
)
_NOT_SAVED: Final = (
    "This message could not be saved, so it was not sent to the AI. Please try again."
)


def _budget_used(job: MessageJob) -> str:
    return (
        f"This drawing set has used its ${cap_text(job.package_cap_micros)} AI budget, so the AI "
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
        live = getattr(run, "live", None)
        if live is not None:
            live.observe(event, data)
        _maybe_checkpoint(run, event)

    def publish_live(event: str, data: dict[str, Any]) -> None:
        # The live state's own events: no checkpoint (they change no block), never observed.
        feed.publish(event, data)
        if emit is not None:
            emit(event, data)

    if getattr(job, "mode", None) == "full_check":
        breaker = getattr(job.provider, "breaker", None)
        run.live = LiveCheck(
            publish_live,
            mode=job.full_check_mode,
            service=lambda: getattr(breaker, "reason", None),
        )
    try:
        configure_page_cache(workers=job.page_cache_workers, memory_mb=job.page_cache_memory_mb)
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
        start_page_cache_trim(
            job.chat_root, set_mb=job.page_cache_set_mb, total_mb=job.page_cache_total_mb
        )


def configure_page_cache(*, workers: int, memory_mb: int) -> None:
    """Hand the hi-res page cache its two sizes from `Settings` (#1575), before the message draws
    anything. Loaded by name: `app/api/` never imports the drawing code. Never raises."""
    try:
        import_module("app.review.assistant.page_cache").configure(
            workers=workers, memory_mb=memory_mb
        )
    except Exception as failed:  # noqa: BLE001 - the cache keeps its own sizes
        _log.warning("assistant page cache sizes not set: %s", type(failed).__name__)


#: The hi-res page caches' clean-up (#1575) starts after a message, at most this often (seconds).
_TRIM_EVERY_S: Final = 60.0
_trim_guard: Final = threading.Lock()
_trim_started: float | None = None


def start_page_cache_trim(
    chat_root: Path, *, set_mb: int, total_mb: int
) -> threading.Thread | None:
    """Start the hi-res page caches' clean-up in the background, unless one started in the last
    minute. Never raises: the caches are only a speed-up."""
    global _trim_started
    with _trim_guard:
        now = monotonic()
        if _trim_started is not None and now - _trim_started < _TRIM_EVERY_S:
            return None
        _trim_started = now
    thread = threading.Thread(
        target=trim_page_caches,
        args=(chat_root,),
        kwargs={"set_mb": set_mb, "total_mb": total_mb},
        name="gv-page-cache-trim",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        return None
    return thread


def trim_page_caches(chat_root: Path, *, set_mb: int, total_mb: int) -> None:
    """Keep the set packs' hi-res page caches within `set_mb` per set and `total_mb` for all, and
    drop the cache of a set no chat or plan check uses any more (`set_pack.trim_hires`)."""
    try:
        pack = import_module("app.review.assistant.set_pack")
        done = pack.trim_hires(
            chat_root,
            live_pack_keys(chat_root, pack.pack_key),
            set_bytes=set_mb * 1_000_000,
            total_bytes=total_mb * 1_000_000,
        )
    except Exception as failed:  # noqa: BLE001 - only a cache: the chat works without it
        _log.warning("assistant page cache clean-up failed: %s", type(failed).__name__)
        return
    if done.pages:
        _log.info(
            "assistant page cache clean-up: removed %d pages (%.0f MB); %.0f MB kept",
            done.pages,
            done.freed / 1e6,
            done.held / 1e6,
        )


def live_pack_keys(
    chat_root: Path, key_of: Callable[[Sequence[Mapping[str, Any]]], str]
) -> frozenset[str] | None:
    """The set packs a chat or a plan check still points at (from each one's `uploads.json`), or
    `None` when one of those files could not be read (half-written, say): then no set counts as
    gone."""
    keys: set[str] = set()
    manifests = [
        *chat_root.glob("*/uploads.json"),
        *chat_root.glob(f"{PLAN_CHECK_FOLDER}/*/uploads.json"),
    ]
    for manifest in manifests:
        try:
            listing = json.loads(manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
        if not isinstance(listing, list):
            return None
        items = [item for item in listing if isinstance(item, dict)]
        if items:
            keys.add(key_of(items))
    return frozenset(keys)


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
        if job.mode == "full_check" and job.continue_from is not None:
            shown = f"Continue the full check \u00b7 {shown.split(' \u00b7 ', 1)[-1]}"
        with job.factory() as session:
            asked = add_user_message(
                session,
                chat_id=job.chat_id,
                created_by=job.created_by,
                text=shown,
                mode=job.mode,
                products=job.products,
            )
            session.commit()
            run.question_id = asked.id
    except Exception:  # noqa: BLE001 - told plainly; nothing was sent
        _log.exception("assistant chat could not store the question")
        _safe(emit, "error", {"message": _NOT_SAVED})
        return
    # From here a server restart keeps the work so far (#1514).
    run.factory = job.factory
    run.resume = {
        "model": job.model.id,
        "effort": job.effort,
        "thinking": job.thinking,
        "mode": job.mode,
        "products": None if job.products is None else list(job.products),
        # How Continue picks it up (`resume_of`): a simple check replays its saved answers.
        "full_check_mode": job.full_check_mode if job.mode == "full_check" else None,
    }
    run.checkpoint = job.chat_root / str(job.chat_id) / CHECKPOINT_FILE
    _maybe_checkpoint(run, "message_start", force=True)
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
    started = getattr(run, "live", None)
    if started is not None:
        started.start()  # the full check's start time and "getting ready"

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
    workspace = start_set_pack(workspace, job.chat_root, listing)
    if job.plan_answers and workspace.set_pack is not None:
        # The plan check's answers (#1569), kept with the set's page map before the plan is made.
        try:
            record_answers(workspace.set_pack, job.plan_answers, job.created_by)
        except OSError as failed:
            _log.warning("assistant chat plan answers not kept: %s", type(failed).__name__)
        # And as the set's notes (#1548), so later chats on the set read the reviewer's word too.
        try:
            with job.factory() as session:
                for doubt_id, value in job.plan_answers.items():
                    said = answer_note(doubt_id, value)
                    if said is not None:
                        add_note(
                            session,
                            package_id=job.package_id,
                            note=said[0],
                            page=said[1],
                            source="decision",
                            chat_id=job.chat_id,
                            created_by=job.created_by,
                        )
                session.commit()
        except Exception:  # noqa: BLE001 - the answers are kept beside the page map anyway
            _log.exception("assistant chat plan answers not added to the set notes")
    prepare_skill(workspace, job.factory, job.revision_id, job.skill_builder, job.products)
    sandbox = job.open_sandbox(workspace)
    if sandbox is None:
        stop_with("", "error", _NO_SANDBOX)
        return
    run.sandbox = sandbox
    with job.factory() as session:
        replay = history(session, job.chat_id)
        stored = messages_of(session, job.chat_id)
        # "Check the rest": the last answer's unknowns become this answer's work list.
        work_list = None
        if job.mode != "full_check" and asks_for_the_rest(job.text):
            work_list = work_list_note(
                last_unknowns(message.blocks for message in stored if message.role == "assistant")
            )
        # The set's memory (#1548): its notes at a chat's start, then the ones added since.
        notes = _set_notes_text(session, job, stored, first=not replay)
        # Follow-up corrections (#1548): the earlier answers' findings, newest versions.
        earlier_found = earlier_findings(stored)
        answer_number = sum(1 for message in stored if message.role == "assistant") + 1
        # Continue (#1514): the work of the answer a server restart cut off.
        earlier = (
            None
            if job.continue_from is None
            else _interrupted(session, job.chat_id, job.continue_from)
        )
    # The lead's system prompt and tools: the ones the chat began with (#1577), so the history's
    # thinking stays valid after a restart on a newer harness; a newer prompt comes as a note.
    harness = chat_harness(workspace.root, replayed=bool(replay))
    user_content: list[dict[str, Any]] = []
    # The files are listed in the first message the model sees (an earlier message that got no
    # answer is not in its history), and again when the set's PDFs changed.
    if not replay or changed:
        user_content.append(
            {"type": "text", "text": uploaded_files_text(listing, changed=changed and bool(replay))}
        )
    newer = update_note(harness, replay)
    if newer is not None:
        user_content.append({"type": "text", "text": newer})
    focus = None
    if job.products is not None:
        marker = workspace.root / "chat_products.json"
        wanted = json.dumps(list(job.products))
        before = marker.read_text(encoding="utf-8") if marker.exists() else None
        if before != wanted or not replay:
            focus = focus_line(job.products, job.unruled)
        marker.write_text(wanted, encoding="utf-8")
    if focus is not None:
        user_content.append({"type": "text", "text": focus})
    if notes is not None:
        user_content.append({"type": "text", "text": notes})
    earlier_note = (
        earlier_findings_note(earlier_found) if replay and job.mode != "full_check" else None
    )
    if earlier_note is not None:
        user_content.append({"type": "text", "text": earlier_note})
    if work_list is not None:
        user_content.append({"type": "text", "text": work_list})
    if earlier is not None:
        user_content.append({"type": "text", "text": continue_note(*earlier, mode=job.mode)})
    if job.model.short == "fable":
        user_content.append({"type": "text", "text": FABLE_NOTE})
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

    def remember_note(note: str, page: int | None) -> None:
        with job.factory() as session:
            add_note(
                session,
                package_id=job.package_id,
                note=note,
                page=page,
                source="ai",
                chat_id=job.chat_id,
                message_id=run.message_id,
                created_by=job.created_by,
            )
            session.commit()

    def read_picture(picture_id: str) -> bytes:
        return (workspace.pictures / f"{UUID(picture_id)}.png").read_bytes()

    engine = ChatEngine(
        settings=EngineSettings(
            model=job.model,
            effort=job.effort,
            thinking=job.thinking,
            lead_max_turns=job.lead_max_turns,
            helper_max_turns=job.helper_max_turns,
            max_parallel_agents=job.max_parallel_agents,
            # The roster's cap is the set's budget until it has planned the pages, then its own
            # fixed price (#1823); the simple check has no fixed price, only the per-answer
            # runaway guard (Anant, 2026-10-19).
            message_cap_micros=(
                job.full_check_cap_micros
                if job.mode == "full_check"
                and job.roster
                and job.full_check_cap_micros
                and job.full_check_mode != "simple"
                else job.message_cap_micros
            ),
            package_cap_micros=job.package_cap_micros,
            rates=job.rates,
            mode=job.mode,
            products=job.products or (),
            drawn_helpers=job.drawn_helpers,
            roster=job.roster,
            price_rule=job.price_rule,
            numbers_effort=job.numbers_effort,
            numbers_pages=job.numbers_pages,
            pairing_proof=job.pairing_proof,
            round_pick_cap_micros=job.round_pick_cap_micros,
            full_check_mode=job.full_check_mode,
            simple=_simple_of(job, run, workspace.root, workspace.set_pack),
            page_match_model=job.page_match_model,
            preflight_model=job.preflight_model,
            run_anyway=job.run_anyway,
            whole_look_model=job.whole_look_model,
            whole_look_effort=job.whole_look_effort,
            set_index_model=job.set_index_model,
            identity=job.identity,
            numbering=numbering(job.factory, job.package_id, job.chat_id, run.message_id),
        ),
        provider=job.provider,
        tools=ChatTools(
            workspace=workspace,
            sandbox=sandbox,
            page_count=max((int(item.get("pages") or 0) for item in listing), default=0),
            # GV Review's recorded results cut to the products in scope (`product_scope`).
            results=scoped_loader(job.results, ProductScope.of(job.products)),
            register_file=register_file,
            uploads=listing,
            remember_note=remember_note,
        ),
        ledger=SessionChatLedger(job.factory, job.revision_id, job.chat_id),
        emit=lambda event, data: _safe(emit, event, data),
        history=replay,
        user_content=user_content,
        package_spent=package_spent,
        read_picture=read_picture,
        cancel=run.cancel,
        lead_system=harness.system,
        lead_tools=harness.tools,
    )
    run.engine = engine
    engine.live = getattr(run, "live", None)
    engine.book.earlier = dict(earlier_found)
    engine.answer_number = answer_number
    if earlier is not None and job.mode == "full_check" and job.full_check_mode != "simple":
        # A simple check's Continue replays its own saved answers instead (`_simple_of`): the
        # interrupted findings would be listed twice.
        _blocks, _question = earlier
        coverage = _last_coverage(_blocks)
        # GV Review's recorded differences are seeded again by the check itself.
        try:
            engine.carry_over(
                [f for f in live_findings(_blocks) if not f.get("record_id")], coverage
            )
            # The strings and readings it recorded too, so code's string check sees them (#1827).
            engine.carry_ledger(_blocks)
            # A roster check then compares only the pages the earlier answer left (#1821).
            engine.continuing = True
        except Exception:  # noqa: BLE001 - the check goes on; it just redoes more
            _log.exception("assistant chat could not carry over the interrupted findings")
    if job.mode == "chat" and job.continue_from is None:
        # #1551: the question sharpened into a task spec before any tool or helper runs.
        apply_sharpen(
            engine,
            history=replay,
            products=job.products,
            use_model=job.sharpen_model,
            edit=job.task_edit,
            deadline_seconds=job.sharpen_deadline_seconds,
        )
    if run.cancel.is_set():
        stop_with("", "cancelled")
        return
    outcome = engine.run()
    if job.mode == "full_check" and engine.settings.roster and job.full_check_mode != "simple":
        # A roster full check's own price is its cap (#1823): stored as the answer's cap.
        outcome.cap_micros = engine.settings.message_cap_micros
    if outcome.stopped == "cancelled":
        sandbox.kill_all()
    _say_service_stop(job.provider, outcome)
    # The hard filter's backstop: no out-of-scope finding is stored, whatever reached the blocks.
    outcome.blocks, dropped = final_blocks(
        outcome.blocks, engine.product_scope, engine.book.out_of_scope
    )
    if dropped:
        _log.warning(
            "assistant chat product scope (%s): %d out-of-scope findings dropped",
            ",".join(engine.product_scope.products),
            dropped,
        )
    # A product with no GV rule yet is said plainly, never checked against nothing in silence.
    note = no_rules_note([p for p in engine.product_scope.products if p in job.unruled])
    if note is not None and outcome.stopped not in ("cancelled", "error"):
        outcome.blocks.append({"type": "text", "text": note})
        outcome.text = f"{outcome.text}\n\n{note}" if outcome.text else note
    finish(outcome)


def _say_service_stop(provider: object, outcome: MessageOutcome) -> None:
    """When the AI service stopped this answer's calls (`ServiceBreaker`: it refused the key, or
    could not be reached), the answer says why in plain words, once, whatever else it shows (a
    full check still gives code's findings and files)."""
    breaker = getattr(provider, "breaker", None)
    reason = getattr(breaker, "reason", None)
    if not isinstance(reason, str) or outcome.stopped == "cancelled":
        return
    if reason == outcome.error:
        return
    if outcome.stopped == "error" and outcome.error is None:
        outcome.error = reason
        return
    said = f"**AI calls stopped:** {reason}"
    if any(block.get("text") == said for block in outcome.blocks):
        return
    outcome.blocks.append({"type": "text", "text": said})
    outcome.text = f"{outcome.text}\n\n{said}" if outcome.text else said


def _finish(job: MessageJob, run: RunningMessage, emit: Emit, outcome: MessageOutcome) -> None:
    """The answer stored, then `error` (if any) and `message_end` with the stored message. Not
    when shutdown already stored it as interrupted (#1514): a question never gets two replies."""
    live = getattr(run, "live", None)
    if live is not None:
        try:
            live.finish(outcome.stopped)  # the live card's last event: done
        except Exception:  # noqa: BLE001 - the answer is stored whatever the live screen says
            _log.exception("assistant chat live state did not finish")
    with run.store_lock:
        if run.abandoned:
            return
        run.finished = True
        _store_answer(job, run, emit, outcome)


def _store_answer(
    job: MessageJob, run: RunningMessage, emit: Emit, outcome: MessageOutcome
) -> None:
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
                message_cap_micros=(
                    job.message_cap_micros if outcome.cap_micros is None else outcome.cap_micros
                ),
                package_cap_micros=job.package_cap_micros,
                cap_hit=outcome.cap_hit if outcome.stopped == "cost_cap" else None,
                mode=job.mode,
                products=job.products,
            )
            session.commit()
            stored = message_out(message, files_of(session, job.chat_id))
        _drop_checkpoint(run)
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


# ---- a server restart (#1514) -------------------------------------------------------------------

#: The running message's work so far, in its chat's folder (one message runs per chat at most).
CHECKPOINT_FILE: Final = "checkpoint.json"
#: The follow-up the screen's Continue sends.
CONTINUE_TEXT: Final = "Continue from where you stopped"
#: Saved at most this often while words stream, and at most this often after a step or finding.
_CHECKPOINT_EVERY: Final = 2.0
_CHECKPOINT_SOON: Final = 0.5
_CHECKPOINT_EVENTS: Final = frozenset(
    {"step_end", "finding", "finding_dropped", "coverage", "agent_end", "thinking_end", "file"}
)
_SAVE_LOCK: Final = threading.Lock()
#: How long a stopping server waits for running answers to end by themselves.
SHUTDOWN_WAIT_SECONDS: Final = 10.0
#: How much of the interrupted answer's words a Continue hands over.
_CARRIED_TEXT: Final = 6000
_CARRIED_LINES: Final = 60


def write_checkpoint(run: RunningMessage) -> None:
    """The message's blocks so far and what it spent, replacing the last save in one step (a
    crash mid-write leaves the previous save whole)."""
    path, resume, question = run.checkpoint, run.resume, run.question_id
    if path is None or resume is None or question is None:
        return
    engine = run.engine
    data = {
        **resume,
        "message_id": str(run.message_id),
        "question_id": str(question),
        "blocks": [] if engine is None else engine.stored_blocks(),
        "cost_micros": run.spent(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    run.checkpointed_at = monotonic()


def _maybe_checkpoint(run: RunningMessage, event: str, *, force: bool = False) -> None:
    """Saved now, or (rate-limited) once the wait is over: what happened is never left unsaved
    for longer than `_CHECKPOINT_EVERY`, even when the answer then pauses (a slow call)."""
    if run.checkpoint is None or run.abandoned:
        return
    wait = (_CHECKPOINT_SOON if event in _CHECKPOINT_EVENTS else _CHECKPOINT_EVERY) - (
        monotonic() - run.checkpointed_at
    )
    if not force and wait > 0:
        with _SAVE_LOCK:
            if run.save_due:
                return
            run.save_due = True
        timer = threading.Timer(wait, _due_checkpoint, args=(run,))
        timer.daemon = True
        timer.start()
        return
    _save(run)


def _due_checkpoint(run: RunningMessage) -> None:
    with _SAVE_LOCK:
        run.save_due = False
    if run.checkpoint is None or run.abandoned or run.finished:
        return
    _save(run)


def _save(run: RunningMessage) -> None:
    try:
        write_checkpoint(run)
    except Exception:  # noqa: BLE001 - a save that fails never stops the answer
        _log.warning("assistant chat could not save its checkpoint", exc_info=True)


def _drop_checkpoint(run: RunningMessage) -> None:
    if run.checkpoint is not None:
        run.checkpoint.unlink(missing_ok=True)


def _read_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _last_coverage(blocks: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    for block in reversed(blocks):
        if block.get("type") == "coverage" and isinstance(block.get("coverage"), Mapping):
            return dict(block["coverage"])
    return None


def _done_pages(coverage: Mapping[str, Any] | None) -> tuple[list[int], list[int]]:
    """The pages accounted for, and the ones not (never opened, no note)."""
    done: list[int] = []
    left: list[int] = []
    for entry in (coverage or {}).get("pages") or []:
        page = entry.get("page")
        if not isinstance(page, int):
            continue
        (left if entry.get("status") == "not_opened" and not entry.get("note") else done).append(
            page
        )
    return done, left


def resume_of(
    blocks: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    products: Sequence[str] | None,
    simple: bool = False,
) -> dict[str, Any]:
    """How Continue picks up an interrupted answer, and what it will do again, in plain words.
    `simple`: a simple full check, which replays its saved answers (#1769)."""
    findings = [f for f in live_findings(blocks) if not f.get("record_id")]
    done, left = _done_pages(_last_coverage(blocks))
    steps = sum(1 for block in blocks if block.get("type") == "step")
    if mode == "full_check" and simple:
        redo = SIMPLE_CONTINUE_WORDS
    elif mode == "full_check":
        one = len(findings) == 1
        kept = (
            f"The {len(findings)} finding{'' if one else 's'} already made "
            f"{'is' if one else 'are'} kept"
            if findings
            else "No finding was made yet"
        )
        if done:
            them = (
                f"the page already done (page {done[0]}) is"
                if len(done) == 1
                else f"the {len(done)} pages already done are"
            )
            pages = f"; {them} not opened again" + (
                f", and page{'' if len(left) == 1 else 's'} {page_ranges(left)} "
                f"{'is' if len(left) == 1 else 'are'} checked now."
                if left
                else "."
            )
        else:
            pages = "; no page was recorded as done yet, so every page is checked."
        redo = (
            f"Continue runs the full check again from where it stopped. {kept}{pages} GV Review's "
            "recorded differences are checked again, and so is the step that was running when "
            "the server restarted."
        )
    else:
        redo = (
            "Continue asks the AI to go on from what it had written and the "
            f"{steps} step{'' if steps == 1 else 's'} it had done; only the step that was "
            "running when the server restarted (and what came after it) is done again."
        )
    return {
        "mode": "full_check" if mode == "full_check" else "chat",
        "products": None if products is None else list(products),
        "findings": len(findings),
        "pages_done": len(done),
        "redo": redo,
    }


#: What Continue does for a simple full check (#1769).
SIMPLE_CONTINUE_WORDS: Final = (
    "Continue uses again, at no cost, every drawing answer this check had already received, and "
    "compares only the drawings it had no answer for; code then checks every answer again and "
    "makes the outputs."
)


def _simple_of(
    job: MessageJob, run: RunningMessage, root: Path, set_pack: Path | None
) -> Mapping[str, Any] | None:
    """The simple full check's options for this answer: the answer's id (its run folder keeps it),
    and for a Continue, the interrupted answer's run replayed with the drawings it has no answer
    for compared now (`replay` + `compare_missing`, #1769): a restart never pays twice."""
    if job.simple is None or job.mode != "full_check" or job.full_check_mode != "simple":
        return job.simple
    options = {**job.simple, "message_id": str(run.message_id)}
    if job.continue_from is not None and not options.get("replay"):
        try:
            earlier = import_module("app.review.assistant.simple_check.run").run_of_message(
                root, job.continue_from, set_pack
            )
        except Exception:  # noqa: BLE001 - then the whole check runs again
            _log.exception("assistant chat could not find the interrupted check's answers")
            earlier = None
        if earlier is not None:
            options.update(replay=earlier, compare_missing=True)
    return options


def _settled(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The saved blocks as a stopped answer shows them: a step or helper still running when the
    server stopped is shown as not finished."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        block = json.loads(json.dumps(block))
        step = block.get("step") if block.get("type") == "step" else None
        if isinstance(step, dict):
            if step.get("ok") is None:
                step["ok"] = False
            for agent in step.get("agents") or []:
                if isinstance(agent, dict) and agent.get("ok") is None:
                    agent["ok"] = False
        out.append(block)
    return out


def _store_interrupted(
    session: Session,
    question: AssistantChatMessage,
    saved: Mapping[str, Any] | None,
) -> AssistantChatMessage | None:
    """The interrupted reply to `question` (None when its place is taken: a reply exists)."""
    blocks = _settled(saved.get("blocks") or []) if saved else []
    # The question keeps how it was asked (#1513); the checkpoint says it too.
    mode = str((saved or {}).get("mode") or question.mode or "chat")
    products = (saved or {}).get("products") if saved else question.products
    recorded = chat_cost_since(session, question.chat_id, question.created_at)
    cost = max(recorded, int(saved.get("cost_micros") or 0) if saved else 0)
    message_id = uuid4()
    if saved and saved.get("message_id"):
        try:
            message_id = UUID(str(saved["message_id"]))
        except ValueError:
            pass
    try:
        with session.begin_nested():
            message = add_interrupted_message(
                session,
                message_id=message_id,
                question=question,
                model=str(saved["model"]) if saved and saved.get("model") else None,
                effort=str(saved["effort"]) if saved and saved.get("effort") else None,
                thinking=bool(saved["thinking"]) if saved and "thinking" in saved else None,
                blocks=[
                    *blocks,
                    {
                        "type": "resume",
                        "resume": resume_of(
                            blocks,
                            mode=mode,
                            products=products,
                            simple=(saved or {}).get("full_check_mode") == "simple",
                        ),
                    },
                ],
                cost_micros=cost,
                mode=mode,
                products=products,
            )
    except IntegrityError:
        return None
    return message


def recover_interrupted(
    factory: sessionmaker[Session],
    registry: ChatRegistry,
    chat_root: Path,
    chat_ids: Sequence[UUID] | None = None,
) -> int:
    """Every question left with no reply and no message running for it in this process gets its
    interrupted reply (with the chat's checkpoint when it was for that question). Returns how
    many were stored. Safe to run any time: a running message is skipped, and a reply that is
    stored meanwhile takes the place first (the row is then refused, not doubled)."""
    stored = 0
    with factory() as session:
        for question in unanswered_questions(session, chat_ids):
            if registry.get(question.chat_id) is not None:
                continue
            path = chat_root / str(question.chat_id) / CHECKPOINT_FILE
            saved = _read_checkpoint(path)
            if saved is not None and saved.get("question_id") != str(question.id):
                saved = None
            if _store_interrupted(session, question, saved) is None:
                continue
            session.commit()
            stored += 1
            if saved is not None:
                path.unlink(missing_ok=True)
            _log.info("assistant chat answer stored as interrupted by a restart")
    return stored


def _abandon(run: RunningMessage) -> None:
    """At shutdown: a message still running is stored as interrupted, its screens get that
    message, and it is stopped (no more calls are paid for)."""
    with run.store_lock:
        if run.finished or run.abandoned:
            return
        run.abandoned = True
        factory, question_id = run.factory, run.question_id
        stored: ChatMessageOut | None = None
        if factory is not None and question_id is not None:
            try:
                write_checkpoint(run)
            except Exception:  # noqa: BLE001 - the last save stands
                _log.warning("assistant chat could not save its last checkpoint", exc_info=True)
            saved = None if run.checkpoint is None else _read_checkpoint(run.checkpoint)
            try:
                with factory() as session:
                    question = session.get(AssistantChatMessage, question_id)
                    message = (
                        None if question is None else _store_interrupted(session, question, saved)
                    )
                    if message is not None:
                        session.commit()
                        stored = message_out(message, files_of(session, run.chat_id))
                        _drop_checkpoint(run)
            except Exception:  # noqa: BLE001 - the next start stores it from the checkpoint
                _log.exception("assistant chat could not store an interrupted answer")
        if stored is not None:
            run.feed.publish("message_end", {"message": stored.model_dump(mode="json")})
    run.feed.close()
    run.stop()


def shutdown_chats(registry: ChatRegistry, *, wait_seconds: float = SHUTDOWN_WAIT_SECONDS) -> int:
    """SIGTERM: take no new message, save each running one, give them `wait_seconds` to end by
    themselves, then store each one still running as interrupted and stop it. Returns how many
    were stored as interrupted."""
    running = registry.close()
    for run in running:
        _maybe_checkpoint(run, "shutdown", force=True)
    deadline = monotonic() + max(0.0, wait_seconds)
    while monotonic() < deadline and any(registry.get(run.chat_id) is run for run in running):
        threading.Event().wait(0.1)
    left = [run for run in running if registry.get(run.chat_id) is run]
    for run in left:
        _abandon(run)
    return sum(1 for run in left if run.abandoned)


def continuable(message: AssistantChatMessage) -> bool:
    """Whether Continue may go on from `message`: a server restart interrupted it (#1514), or it
    is a full check that did not finish and said how to continue (its `resume` block, #1821)."""
    if message.stopped == "interrupted":
        return True
    return (
        message.role == "assistant"
        and message.mode == "full_check"
        and any(
            isinstance(block, Mapping) and block.get("type") == "resume"
            for block in message.blocks or []
        )
    )


def _interrupted(
    session: Session, chat_id: UUID, message_id: UUID
) -> tuple[list[dict[str, Any]], str] | None:
    """The blocks of the answer Continue goes on from (`continuable`) and its question."""
    messages = messages_of(session, chat_id)
    for at, message in enumerate(messages):
        if message.id == message_id and continuable(message):
            question = next((m.text for m in reversed(messages[:at]) if m.role == "user"), "")
            return [dict(b) for b in message.blocks or [] if isinstance(b, Mapping)], question
    return None


def continue_note(blocks: Sequence[Mapping[str, Any]], question: str, *, mode: str) -> str:
    """The interrupted answer's work, handed to the lead in words (it is not in the history)."""
    findings = [f for f in live_findings(blocks) if not f.get("record_id")]
    if mode == "full_check":
        done, left = _done_pages(_last_coverage(blocks))
        lines = [
            (
                "GV Review note (by code): a server restart interrupted the last full check. The "
                f"{len(findings)} findings it made are already in this check (shown above; check "
                "any you doubt, but do not report them again)."
            )
        ]
        if done:
            lines.append(
                f"Pages already accounted for (recorded in coverage; do not open them again): "
                f"{page_ranges(done)}."
            )
        if left:
            lines.append(
                f"Work list: pages {page_ranges(left)} were not done yet. Check these, report "
                "their findings and record their coverage."
            )
        elif not done:
            lines.append("No page was recorded as done yet: check the whole set.")
        return "\n".join(lines)
    words = "\n\n".join(
        str(block.get("text") or "").strip()
        for block in blocks
        if block.get("type") == "text" and str(block.get("text") or "").strip()
    )
    if len(words) > _CARRIED_TEXT:
        words = words[:_CARRIED_TEXT] + " \u2026"
    steps = [
        f"- {block['step'].get('label') or block['step'].get('tool')}"
        for block in blocks
        if block.get("type") == "step" and isinstance(block.get("step"), Mapping)
    ][:_CARRIED_LINES]
    found = [
        f"- {f.get('id')} page {f.get('page')}: {f.get('item')}: architect {f.get('architect')}; "
        f"vendor {f.get('vendor')} ({f.get('status')})"
        for f in findings[:_CARRIED_LINES]
    ]
    parts = [
        (
            "GV Review note (by code): a server restart interrupted your last answer before it "
            "finished, so it is not in your history. Continue from where it stopped: do not redo "
            "the work listed here; finish the answer."
        ),
        f"The question was: {question}",
    ]
    if steps:
        parts.append(
            "Steps it had done (files it made in /home/claude are still there):\n"
            + "\n".join(steps)
        )
    if found:
        parts.append("Findings it had reported:\n" + "\n".join(found))
    if words:
        parts.append("What it had written so far:\n" + words)
    return "\n\n".join(parts)
