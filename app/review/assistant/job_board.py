"""The assistant's job board (#1871): jobs and their progress in PostgreSQL, for the API and the
worker alike.

The API writes a job row (`enqueue_message`, in the request's transaction, after every check it
makes before a stream starts) and reads its progress back (`JobRegistry`, `JobFeedReader`); the
worker (`job_worker.py`) claims it (`claim`, `FOR UPDATE SKIP LOCKED`), runs it in a process of its
own and writes its events (`JobFeedWriter`), heartbeat, spend and live card. Nothing about a running
job is kept in the API's memory, so the API may run as several processes, and restarting it never
stops a job.

**The two rules hold across every process** by the table's partial unique indexes: one message
running per chat (a second is refused 409 `message_running`), one paid full check at a time across
all chats (`paid_job_running`, #1770).

**A dead job** (its process was killed: no heartbeat for `stale_seconds`) is stored as interrupted
with the work its checkpoint saved, exactly as after an API restart before (#1514), and its screens
get that answer as `message_end`; the screen then offers Continue. Whoever notices first does it
(the worker's loop, or the API reading the chat), once: the row moves `running -> interrupted` in
one conditional update, and only the process that moved it stores the reply.

This module is loaded by the API: it imports no drawing code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import AssistantChatMessage
from app.models.assistant_job import ACTIVE_STATUSES, AssistantJob, AssistantJobEvent
from app.review.assistant.chat_feed import DEFAULT_MAX_BYTES, MessageFeed
from app.review.assistant.chat_service import (
    ChatRegistry,
    PaidJobRunning,
    RunningMessage,
    recover_interrupted,
)
from app.review.assistant.chat_store import files_of, message_out
from app.review.assistant.live_check import LiveCheck

__all__ = [
    "DEFAULT_STALE_SECONDS",
    "ChatBusy",
    "JobFeedReader",
    "JobFeedWriter",
    "JobRegistry",
    "PaidBusy",
    "claim",
    "enqueue_message",
    "enqueue_pack",
    "finish",
    "heartbeat",
    "pack_starter",
    "reap_dead",
    "worker_id",
]

_log = logging.getLogger(__name__)

#: A running job whose heartbeat is older than this is dead (its process was killed).
DEFAULT_STALE_SECONDS: Final = 60.0
#: How often a follower reads the job's new events.
FOLLOW_EVERY_S: Final = 0.25
#: How often the writer sends the events kept since its last write.
WRITE_EVERY_S: Final = 0.25
_DELTAS: Final = frozenset({"text_delta", "thinking_delta"})
_ALWAYS_KEPT: Final = frozenset({"message_end", "error"})
_ENDED: Final = frozenset({"done", "interrupted", "failed"})

type Factory = sessionmaker[Session]
type Event = tuple[str, dict[str, Any]]


def worker_id() -> str:
    """This process, as a job's `claimed_by`."""
    return f"{socket.gethostname()}:{os.getpid()}"


class ChatBusy(RuntimeError):
    """A message of this chat is queued or running."""


class PaidBusy(RuntimeError):
    """Another paid full check is queued or running (#1770)."""


# ---- writing jobs --------------------------------------------------------------------------------


def _active(kind: str) -> Any:
    return (AssistantJob.kind == kind) & AssistantJob.status.in_(ACTIVE_STATUSES)


def active_message(session: Session, chat_id: UUID) -> AssistantJob | None:
    """The chat's queued or running message job."""
    return session.scalars(
        select(AssistantJob).where(_active("message"), AssistantJob.chat_id == chat_id)
    ).first()


def active_messages(session: Session, package_id: UUID | None = None) -> list[AssistantJob]:
    """Every queued or running message job (of a drawing set, when given)."""
    query = select(AssistantJob).where(_active("message"))
    if package_id is not None:
        query = query.where(AssistantJob.package_id == package_id)
    return list(session.scalars(query.order_by(AssistantJob.created_at)))


def paid_running(session: Session) -> AssistantJob | None:
    """The paid full check queued or running now, in any chat."""
    return session.scalars(
        select(AssistantJob).where(
            AssistantJob.paid.is_(True), AssistantJob.status.in_(ACTIVE_STATUSES)
        )
    ).first()


def enqueue_message(
    session: Session,
    *,
    job_id: UUID,
    chat_id: UUID,
    package_id: UUID,
    created_by: str,
    paid: bool,
    request: Mapping[str, Any],
) -> AssistantJob:
    """The message's job row, added to the caller's transaction (the caller commits). Raises
    `ChatBusy` when the chat has a message queued or running, `PaidBusy` when `paid` and another
    paid full check is (the partial unique indexes decide, whatever process asks)."""
    job = AssistantJob(
        id=job_id,
        kind="message",
        status="queued",
        chat_id=chat_id,
        package_id=package_id,
        paid=paid,
        request=dict(request),
        created_by=created_by,
        attempts=0,
        spent_micros=0,
        last_seq=0,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        if active_message(session, chat_id) is not None:
            raise ChatBusy(str(chat_id)) from None
        raise PaidBusy(str(chat_id)) from None
    return job


def enqueue_pack(
    session: Session, *, pack_key: str, request: Mapping[str, Any], created_by: str
) -> bool:
    """A set pack's build job, unless one is queued or running for the same pack (then False).
    Added to the caller's transaction (the caller commits)."""
    try:
        with session.begin_nested():
            session.add(
                AssistantJob(
                    kind="set_pack",
                    status="queued",
                    pack_key=pack_key,
                    paid=False,
                    request=dict(request),
                    created_by=created_by,
                    attempts=0,
                    spent_micros=0,
                    last_seq=0,
                )
            )
            session.flush()
    except IntegrityError:
        return False
    return True


def request_cancel(session: Session, chat_id: UUID) -> bool:
    """Stop (the reviewer's): the chat's message job is flagged; its process stops it within a
    second. False when nothing is queued or running."""
    done = session.execute(
        update(AssistantJob)
        .where(_active("message"), AssistantJob.chat_id == chat_id)
        .values(cancel_requested_at=func.now(), updated_at=func.now())
    )
    return bool(getattr(done, "rowcount", 0))


def claim(session: Session, worker: str, *, kinds: Sequence[str], limit: int) -> list[AssistantJob]:
    """Up to `limit` queued jobs of `kinds`, oldest first, moved to `running` for `worker` (the
    caller commits). `SKIP LOCKED`: two workers never claim the same job."""
    if limit <= 0:
        return []
    rows = list(
        session.scalars(
            select(AssistantJob)
            .where(AssistantJob.status == "queued", AssistantJob.kind.in_(list(kinds)))
            .order_by(AssistantJob.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for job in rows:
        job.status = "running"
        job.claimed_by = worker
        job.attempts += 1
        session.flush()
        session.execute(
            update(AssistantJob)
            .where(AssistantJob.id == job.id)
            .values(claimed_at=func.now(), heartbeat_at=func.now(), updated_at=func.now())
        )
    return rows


def running_count(session: Session, kind: str, stale_seconds: float) -> int:
    """The jobs of `kind` running now with a live heartbeat (any worker)."""
    fresh = func.now() - timedelta(seconds=stale_seconds)
    return int(
        session.scalar(
            select(func.count())
            .select_from(AssistantJob)
            .where(
                AssistantJob.kind == kind,
                AssistantJob.status == "running",
                AssistantJob.heartbeat_at >= fresh,
            )
        )
        or 0
    )


def heartbeat(
    session: Session, job_id: UUID, *, spent: int | None = None, live: Any = None
) -> bool:
    """The job is alive (with what it spent and its live card, when given). Returns whether Stop
    was asked. The caller commits."""
    values: dict[str, Any] = {"heartbeat_at": func.now(), "updated_at": func.now()}
    if spent is not None:
        values["spent_micros"] = int(spent)
    if live is not None:
        values["live"] = live
    session.execute(
        update(AssistantJob)
        .where(AssistantJob.id == job_id, AssistantJob.status == "running")
        .values(**values)
    )
    asked = session.scalar(
        select(AssistantJob.cancel_requested_at).where(AssistantJob.id == job_id)
    )
    return asked is not None


def finish(
    session: Session,
    job_id: UUID,
    status: str,
    *,
    error: str | None = None,
    only_from: Iterable[str] = ("running",),
) -> bool:
    """The job ended (`done`, `interrupted`, `failed`), unless it had already moved on. The caller
    commits. Returns whether this call moved it."""
    done = session.execute(
        update(AssistantJob)
        .where(AssistantJob.id == job_id, AssistantJob.status.in_(list(only_from)))
        .values(status=status, error=error, finished_at=func.now(), updated_at=func.now())
    )
    return bool(getattr(done, "rowcount", 0))


def append_events(session: Session, job_id: UUID, first_seq: int, events: Sequence[Event]) -> int:
    """`events` as rows `first_seq`, `first_seq + 1`, … and the job's `last_seq` (the caller
    commits). Returns the last seq written."""
    seq = first_seq - 1
    for event, data in events:
        seq += 1
        session.add(AssistantJobEvent(job_id=job_id, seq=seq, event=event, data=data))
    if events:
        session.flush()
        session.execute(
            update(AssistantJob)
            .where(AssistantJob.id == job_id)
            .values(last_seq=seq, updated_at=func.now())
        )
    return seq


def events_after(session: Session, job_id: UUID, seq: int) -> list[tuple[int, str, dict[str, Any]]]:
    rows = session.execute(
        select(AssistantJobEvent.seq, AssistantJobEvent.event, AssistantJobEvent.data)
        .where(AssistantJobEvent.job_id == job_id, AssistantJobEvent.seq > seq)
        .order_by(AssistantJobEvent.seq)
    ).all()
    return [(int(row[0]), str(row[1]), dict(row[2] or {})) for row in rows]


# ---- the worker's side of a message's events -----------------------------------------------------


def _merge(kept: list[Event], event: str, data: dict[str, Any]) -> int:
    """`MessageFeed`'s merging: consecutive deltas of one block are one event, a newer `usage`
    replaces the one before it. Returns the bytes added."""
    last = kept[-1] if kept else None
    if (
        event in _DELTAS
        and last is not None
        and last[0] == event
        and last[1].get("index") == data.get("index")
    ):
        text = str(data.get("text") or "")
        kept[-1] = (event, {**last[1], "text": str(last[1].get("text") or "") + text})
        return len(text)
    if event == "usage" and last is not None and last[0] == "usage":
        kept[-1] = (event, dict(data))
        return 0
    kept.append((event, dict(data)))
    return len(json.dumps(data, ensure_ascii=False))


class JobFeedWriter(MessageFeed):
    """A running message's feed in the worker: each event is written to `assistant_job_events`
    (merged and sent a few times a second, in order), where the API's followers read it. Past
    `max_bytes` only `message_end` and `error` are written (the stored answer is authoritative, as
    with `MessageFeed`). `close` writes what is left before it returns."""

    def __init__(
        self,
        factory: Factory,
        job_id: UUID,
        *,
        every: float = WRITE_EVERY_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        super().__init__(max_bytes=max_bytes)
        self._factory = factory
        self._job_id = job_id
        self._every = every
        self._pending: list[Event] = []
        self._pending_lock = threading.Lock()
        self._written = 0
        self._bytes = 0
        self._seq = 0
        self._write_lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="gv-job-feed", daemon=True)
        self._thread.start()

    @property
    def seq(self) -> int:
        """The last event's `seq` written."""
        return self._seq

    def publish(self, event: str, data: dict[str, Any]) -> None:
        with self._pending_lock:
            if self._stopped.is_set():
                return
            if self._bytes > self._max_bytes and event not in _ALWAYS_KEPT:
                return
            self._bytes += _merge(self._pending, event, data)

    def close(self) -> None:
        with self._pending_lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
        self._thread.join(timeout=30)
        self.flush()
        super().close()

    def flush(self) -> None:
        """Write the events kept since the last write (in order, never two writers at once)."""
        with self._write_lock:
            with self._pending_lock:
                batch, self._pending = self._pending, []
            if not batch:
                return
            for attempt in range(3):
                try:
                    with self._factory() as session:
                        self._seq = append_events(session, self._job_id, self._seq + 1, batch)
                        session.commit()
                    return
                except Exception:  # noqa: BLE001 - a lost connection: try again, then log
                    if attempt == 2:
                        _log.exception("assistant job events could not be written")
                        return
                    threading.Event().wait(0.5 * (attempt + 1))

    def _loop(self) -> None:
        while not self._stopped.wait(self._every):
            self.flush()


# ---- the API's side: following a job, and the running messages -----------------------------------


def _merged(rows: Sequence[tuple[int, str, dict[str, Any]]]) -> list[tuple[int, Event]]:
    """A replay's rows with consecutive deltas merged (one event per streamed block)."""
    kept: list[Event] = []
    seqs: list[int] = []
    for seq, event, data in rows:
        before = len(kept)
        _merge(kept, event, data)
        if len(kept) == before:
            seqs[-1] = seq
        else:
            seqs.append(seq)
    return list(zip(seqs, kept, strict=True))


class JobFeedReader(MessageFeed):
    """A message job's events read from the table, for a screen: every event so far, then each
    new one, until the job ends (the same events, in the same order, as the worker sent them)."""

    def __init__(self, factory: Factory, job_id: UUID, *, every: float = FOLLOW_EVERY_S) -> None:
        super().__init__()
        self._factory = factory
        self._job_id = job_id
        self._every = every

    def _read(self, after: int) -> tuple[str | None, list[tuple[int, str, dict[str, Any]]]]:
        with self._factory() as session:
            # The status first: a job that has ended wrote every event before it ended.
            status = session.scalar(
                select(AssistantJob.status).where(AssistantJob.id == self._job_id)
            )
            return status, events_after(session, self._job_id, after)

    async def follow(self) -> AsyncIterator[Event]:
        after = 0
        first = True
        while True:
            status, rows = await asyncio.to_thread(self._read, after)
            if first:
                shown = _merged(rows)
                first = False
            else:
                shown = [(seq, (event, data)) for seq, event, data in rows]
            for seq, item in shown:
                after = seq
                yield item
            if status is None or (status in _ENDED and not rows):
                return
            await asyncio.sleep(self._every)


class _RemoteLive:
    """A job's live card as its last heartbeat wrote it (`LiveCheck.snapshot()`, `counts()`)."""

    def __init__(self, live: Mapping[str, Any]) -> None:
        self._live = live

    def snapshot(self) -> dict[str, Any]:
        return dict(self._live.get("snapshot") or {})

    def counts(self) -> dict[str, Any]:
        return dict(self._live.get("counts") or {})


def _remote(job: AssistantJob, factory: Factory) -> RunningMessage:
    """A queued or running job as the API's routes read a running message: its ids, its live
    card, its spend, and its events (`feed.follow()`)."""
    assert job.chat_id is not None and job.package_id is not None
    run = RunningMessage(
        chat_id=job.chat_id, package_id=job.package_id, message_id=job.id, paid=job.paid
    )
    run.started = True
    run.feed = JobFeedReader(factory, job.id)
    if job.live:
        run.live = cast(LiveCheck, _RemoteLive(job.live))
    run.engine = cast(Any, _Spent(job.spent_micros))
    return run


@dataclass(frozen=True, slots=True)
class _Spent:
    """What `RunningMessage.spent()` reads off an engine."""

    spent: int

    def stop_streams(self) -> None:
        return None


class JobRegistry(ChatRegistry):
    """The API's running messages when the worker runs them (#1871): the ones running in this
    process (a code-only check, `consistency_check`) plus every queued or running job of the
    board. Stop asks the job's process to stop; spend counts the jobs' heartbeats. Shutting the API
    down (`close`) concerns only this process's own: a job keeps running.

    `engine` gives it the app's database when first needed (`bind`: a given one)."""

    def __init__(
        self,
        chat_root: Path,
        *,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        engine: Callable[[], Engine] | None = None,
    ) -> None:
        super().__init__()
        self._factory: Factory | None = None
        self._engine = engine
        self._chat_root = chat_root
        self.stale_seconds = stale_seconds

    def bind(self, engine: Engine) -> None:
        if self._factory is None or self._factory.kw.get("bind") is not engine:
            self._factory = sessionmaker(bind=engine, expire_on_commit=False)

    @property
    def factory(self) -> Factory | None:
        """Sessions on the app's database (`engine`, read when first needed), else `None`."""
        if self._factory is None and self._engine is not None:
            self.bind(self._engine())
        return self._factory

    def _jobs(self, *, chat_id: UUID | None = None) -> list[AssistantJob]:
        factory = self.factory
        if factory is None:
            return []
        with factory() as session:
            if chat_id is not None:
                job = active_message(session, chat_id)
                jobs = [] if job is None else [job]
            else:
                jobs = active_messages(session)
            alive = []
            for job in jobs:
                if self._dead(session, job):
                    reap_dead(factory, self._chat_root, job.id, self.stale_seconds)
                    continue
                alive.append(job)
            return alive

    def _dead(self, session: Session, job: AssistantJob) -> bool:
        if job.status != "running" or job.heartbeat_at is None:
            return False
        now = session.scalar(select(func.now()))
        return bool(
            now is not None and now - job.heartbeat_at > timedelta(seconds=self.stale_seconds)
        )

    def get(self, chat_id: UUID) -> RunningMessage | None:
        local = super().get(chat_id)
        if local is not None:
            return local
        jobs = self._jobs(chat_id=chat_id)
        factory = self.factory
        return None if not jobs or factory is None else _remote(jobs[0], factory)

    def runs(self) -> list[RunningMessage]:
        local = super().runs()
        factory = self.factory
        if factory is None:
            return local
        seen = {run.chat_id for run in local}
        return [*local, *(_remote(job, factory) for job in self._jobs() if job.chat_id not in seen)]

    def claim(
        self, chat_id: UUID, package_id: UUID, message_id: UUID, *, paid: bool = False
    ) -> RunningMessage | None:
        if self._jobs(chat_id=chat_id):
            return None
        factory = self.factory
        if paid and factory is not None:
            with factory() as session:
                other = paid_running(session)
                if other is not None and other.chat_id != chat_id:
                    raise PaidJobRunning(_remote(other, factory))
        return super().claim(chat_id, package_id, message_id, paid=paid)

    def cancel(self, chat_id: UUID) -> bool:
        local = ChatRegistry.get(self, chat_id)  # this process's own (not `self.get`: a job's)
        if local is not None:
            local.stop()
            return True
        factory = self.factory
        if factory is None:
            return False
        with factory() as session:
            asked = request_cancel(session, chat_id)
            session.commit()
        return asked

    def running_spent(self, package_id: UUID, *, exclude: RunningMessage | None = None) -> int:
        local = super().running_spent(package_id, exclude=exclude)
        factory = self.factory
        if factory is None:
            return local
        with factory() as session:
            jobs = active_messages(session, package_id)
        skip = None if exclude is None else exclude.message_id
        return local + sum(job.spent_micros for job in jobs if job.id != skip)

    def close(self) -> list[RunningMessage]:
        """Shutting down: this process's own messages only (a job runs on in the worker)."""
        with self._lock:
            self._closing = True
            return list(self._runs.values())


class WorkerRegistry(ChatRegistry):
    """A job's own process's registry: its one message, and the set's other jobs' spend (so the
    set's budget sees every running answer, whatever process runs it)."""

    def __init__(self, factory: Factory, job_id: UUID) -> None:
        super().__init__()
        self._factory = factory
        self._job_id = job_id

    def running_spent(self, package_id: UUID, *, exclude: RunningMessage | None = None) -> int:
        local = super().running_spent(package_id, exclude=exclude)
        try:
            with self._factory() as session:
                jobs = active_messages(session, package_id)
        except Exception:  # noqa: BLE001 - the recorded calls still count them
            _log.warning("assistant job: other jobs' spend not read", exc_info=True)
            return local
        return local + sum(job.spent_micros for job in jobs if job.id != self._job_id)


# ---- dead jobs -----------------------------------------------------------------------------------


def reap_dead(
    factory: Factory,
    chat_root: Path,
    job_id: UUID,
    stale_seconds: float | None = DEFAULT_STALE_SECONDS,
    *,
    why: str = "its process stopped",
) -> bool:
    """A message job whose process died: moved to `interrupted` (once, whoever asks first; with
    `stale_seconds`, only when its heartbeat is that old), its reply stored as interrupted with the
    work its checkpoint saved (Continue), and its screens sent that reply. Returns whether this
    call did it."""
    with factory() as session:
        query = update(AssistantJob).where(
            AssistantJob.id == job_id,
            AssistantJob.kind == "message",
            AssistantJob.status.in_(ACTIVE_STATUSES),
        )
        if stale_seconds is not None:
            query = query.where(
                AssistantJob.heartbeat_at < func.now() - timedelta(seconds=stale_seconds)
            )
        moved = session.execute(
            query.values(
                status="interrupted", error=why, finished_at=func.now(), updated_at=func.now()
            )
        )
        if not getattr(moved, "rowcount", 0):
            session.rollback()
            return False
        row = session.execute(
            select(AssistantJob.chat_id, AssistantJob.last_seq).where(AssistantJob.id == job_id)
        ).one()
        session.commit()
    chat_id: UUID | None = row[0]
    last_seq = int(row[1] or 0)
    if chat_id is None:
        return True
    _log.warning("assistant job %s interrupted: %s", job_id, why)
    try:
        recover_interrupted(factory, ChatRegistry(), chat_root, [chat_id])
    except Exception:  # noqa: BLE001 - the chat is repaired again when it is next read
        _log.exception("assistant job: its interrupted reply could not be stored")
    with factory() as session:
        stored = session.get(AssistantChatMessage, job_id)
        if stored is None:
            # The question was never stored (the job died before it started): nothing to show.
            data: dict[str, Any] = {
                "message": "The check stopped before it started (its process ended). Ask again."
            }
            event = "error"
        else:
            out = message_out(stored, files_of(session, chat_id))
            data = {"message": out.model_dump(mode="json")}
            event = "message_end"
        append_events(session, job_id, last_seq + 1, [(event, data)])
        session.commit()
    return True


# ---- set packs -----------------------------------------------------------------------------------


def pack_starter(
    factory: Factory, *, created_by: str = "assistant"
) -> Callable[[Path, Path, Sequence[Mapping[str, Any]]], None]:
    """`chat_service.start_set_pack`'s way to build a pack when the worker runs it (#1871): unless
    the pack is ready (and of this code's version) or a build of it is queued or running, the pack's
    folder says `building` (as `set_pack.ensure_pack` says it, so a full check waits for it) and a
    `set_pack` job is written. Never builds anything in this process."""

    def start(folder: Path, uploads: Path, listing: Sequence[Mapping[str, Any]]) -> None:
        pack = import_module("app.review.assistant.set_pack")
        status = pack.read_status(folder)
        if status.get("state") == "ready" and status.get("version") == pack.PACK_VERSION:
            return
        if not listing:
            return
        key = folder.name
        with factory() as session:
            running = session.scalar(
                select(AssistantJob.id).where(_active("set_pack"), AssistantJob.pack_key == key)
            )
            if running is not None:
                return
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o755)
            pack._set_status(folder, "building", [], files=[str(item["name"]) for item in listing])
            if enqueue_pack(
                session,
                pack_key=key,
                request={
                    "folder": str(folder),
                    "uploads": str(uploads),
                    "listing": [dict(item) for item in listing],
                },
                created_by=created_by,
            ):
                session.commit()

    return start


def wait_for(condition: Callable[[], bool], seconds: float, every: float = 0.2) -> bool:
    """`condition` became true within `seconds` (for tests and the worker's shutdown)."""
    ends = monotonic() + seconds
    while monotonic() < ends:
        if condition():
            return True
        threading.Event().wait(every)
    return condition()
