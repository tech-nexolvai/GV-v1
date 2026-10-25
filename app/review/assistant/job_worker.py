"""The assistant's worker (#1871): runs the chat's messages and the set packs the API queued.

    python scripts/assistant_worker.py --watch     # the supervisor: claims jobs, one process each
    python -m app.review.assistant.job_worker --run <job id>   # one job (the supervisor starts it)

**The supervisor** (`Supervisor`) claims queued jobs from `assistant_jobs` (`FOR UPDATE SKIP
LOCKED`), at most `message_slots` messages and `pack_slots` set packs running at once (counted over
every worker, by live heartbeats), and starts each one in **a process of its own**, so a job that
crashes (a PDF that kills the renderer, a memory blow-up) ends only that job. It watches its
processes: one that ends while its job is still running had died, and its job is stored as
interrupted at once (`job_board.reap_dead`: the reply with the work its checkpoint saved, and
Continue). It also finds jobs whose heartbeat stopped (a process killed together with an earlier
supervisor) and does the same; a set pack whose build died is tried once more, then fails plainly.
SIGTERM: no new job; each running job is stored as interrupted (Continue) and stopped, as a stopping
API did before (`shutdown_chats`); a supervisor killed outright leaves its jobs running (they keep
their own heartbeat and finish).

**One job** (`run_job`): a message is rebuilt exactly as the API's route builds it
(`assistant_chats._message`: the same checks, settings, caps, price, scope, plan answers, Continue)
and answered by the same `chat_service.run_message`: every agent, tool, money rule and guard is
unchanged; only where it runs and where its events go change. Its events go to
`assistant_job_events` (`JobFeedWriter`); every second its heartbeat writes what it has spent and
its live card, and reads Stop. A set pack is `set_pack.ensure_pack(..., background=False)` in this
process. A message's own set pack is never built in the message's process: it is a `set_pack` job
too (`job_board.pack_starter`), so two processes never build one pack.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models.assistant_job import AssistantJob
from app.review.assistant import chat_service
from app.review.assistant.job_board import (
    DEFAULT_STALE_SECONDS,
    JobFeedWriter,
    WorkerRegistry,
    claim,
    finish,
    heartbeat,
    pack_starter,
    reap_dead,
    running_count,
    worker_id,
)

__all__ = ["HEARTBEAT_EVERY_S", "Supervisor", "main", "run_job"]

_log = logging.getLogger(__name__)

type Factory = sessionmaker[Session]

#: How often a job's process writes its heartbeat (spend, live card) and reads Stop.
HEARTBEAT_EVERY_S: Final = 1.0
#: How often the supervisor looks for work and for dead jobs.
POLL_S: Final = 0.5
#: How many times a set pack's build is started before it fails for good.
PACK_ATTEMPTS: Final = 2
#: How long a stopping supervisor waits for its jobs to store themselves as interrupted.
STOP_WAIT_S: Final = 30.0
_REPO: Final = Path(__file__).resolve().parents[3]


# ---- one job ---------------------------------------------------------------------------------------


class _Beat:
    """The job's heartbeat thread: every `HEARTBEAT_EVERY_S`, what the message spent and its live
    card (when they changed), and Stop (`run.stop()` once, as the API's Stop did)."""

    def __init__(self, factory: Factory, job_id: UUID, run: Any = None) -> None:
        self._factory = factory
        self._job_id = job_id
        self.run = run
        self._stop = threading.Event()
        self._live_sent: str | None = None
        self._stopped_run = False
        self._thread = threading.Thread(target=self._loop, name="gv-job-heartbeat", daemon=True)

    def start(self) -> _Beat:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def beat(self) -> None:
        run = self.run
        spent = None if run is None else run.spent()
        live: Any = None
        if run is not None and run.live is not None:
            snapshot = {"snapshot": run.live.snapshot(), "counts": run.live.counts()}
            text = json.dumps(snapshot, sort_keys=True, default=str)
            if text != self._live_sent:
                live, self._live_sent = json.loads(text), text
        with self._factory() as session:
            asked = heartbeat(session, self._job_id, spent=spent, live=live)
            session.commit()
        if asked and run is not None and not self._stopped_run:
            self._stopped_run = True
            _log.warning("assistant job %s: Stop asked", self._job_id)
            run.stop()

    def _loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_EVERY_S):
            try:
                self.beat()
            except Exception:  # noqa: BLE001 - a missed beat is only late
                _log.warning("assistant job heartbeat failed", exc_info=True)


def _shim(settings: Any, store: Any) -> Any:
    """What `assistant_chats._message` reads off a request: the app's settings and store."""
    from app.api.dependencies import ARTIFACT_STORE_STATE

    state = SimpleNamespace(settings=settings)
    setattr(state, ARTIFACT_STORE_STATE, store)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def default_store(settings: Any) -> Any:
    """The artifact store the API reads uploads from (development: `GV_DEV_STORAGE`, as
    `scripts/dev_server.py` wires it). Only read here: no ticket is ever signed."""
    from storage.local import LocalStore

    del settings
    return LocalStore(root=Path(os.environ.get("GV_DEV_STORAGE", ".dev-storage")).resolve())


def run_job(
    job_id: UUID,
    *,
    factory: Factory,
    settings: Any,
    runtime: Any = None,
    store: Any = None,
    stopping: threading.Event | None = None,
) -> str:
    """Run one claimed job to its end; returns its final status. `runtime` and `store`: the API's
    (`chat_runtime`, the artifact store), made from `settings` when not given (a test gives fakes).
    `stopping`: set to store a running message as interrupted and stop it (SIGTERM)."""
    with factory() as session:
        job = session.get(AssistantJob, job_id)
        if job is None:
            return "failed"
        kind, request = job.kind, dict(job.request)
    if kind == "set_pack":
        return _run_pack(factory, job_id, request)
    return _run_message(
        factory, job_id, request, settings=settings, runtime=runtime, store=store, stopping=stopping
    )


def _refusal(failed: BaseException) -> str:
    detail = getattr(failed, "detail", None)
    return str(detail) if detail else "The assistant could not start this message. Ask again."


def _run_message(
    factory: Factory,
    job_id: UUID,
    request: Mapping[str, Any],
    *,
    settings: Any,
    runtime: Any,
    store: Any,
    stopping: threading.Event | None,
) -> str:
    from fastapi import HTTPException

    from app.api import assistant_chats as routes
    from app.auth import Principal, Role
    from app.review.assistant.chat_contract import ChatMessageIn

    feed = JobFeedWriter(factory, job_id)
    beat = _Beat(factory, job_id).start()
    try:
        store = store if store is not None else default_store(settings)
        shim = _shim(settings, store)
        who = request["principal"]
        principal = Principal(
            id=str(who["id"]),
            roles=frozenset(Role(role) for role in who.get("roles") or ()),
            projects=frozenset(UUID(str(project)) for project in who.get("projects") or ()),
        )
        body = ChatMessageIn.model_validate(request["body"])
        runtime = runtime if runtime is not None else routes.chat_runtime(shim)
        registry = WorkerRegistry(factory, job_id)
        with factory() as session:
            message = routes._message(
                shim, UUID(str(request["chat_id"])), body, principal, session, runtime, registry
            )
    except (HTTPException, KeyError, ValueError) as refused:
        _log.warning("assistant job %s refused: %s", job_id, _refusal(refused))
        feed.publish("error", {"message": _refusal(refused)})
        feed.close()
        beat.stop()
        with factory() as session:
            finish(session, job_id, "failed", error=_refusal(refused))
            session.commit()
        return "failed"
    run = message.run
    run.message_id = job_id
    run.feed = feed
    beat.run = run
    answered = threading.Thread(
        target=chat_service.run_message,
        args=(message.job, run),
        name="assistant-chat-message",
        daemon=True,
    )
    answered.start()
    while answered.is_alive():
        answered.join(0.5)
        if stopping is not None and stopping.is_set():
            # SIGTERM: stored as interrupted (Continue), its screens told, its calls stopped.
            chat_service.shutdown_chats(registry, wait_seconds=0)
            answered.join(5)
            beat.stop()
            with factory() as session:
                finish(session, job_id, "interrupted", error="the worker was stopped")
                session.commit()
            return "interrupted"
    beat.stop()
    try:
        beat.beat()  # the last spend and live card
    except Exception:  # noqa: BLE001 - the stored answer has them
        _log.warning("assistant job's last heartbeat failed", exc_info=True)
    with factory() as session:
        finish(session, job_id, "done")
        session.commit()
    return "done"


def _run_pack(factory: Factory, job_id: UUID, request: Mapping[str, Any]) -> str:
    beat = _Beat(factory, job_id).start()
    error: str | None = None
    try:
        pack = import_module("app.review.assistant.set_pack")
        pack.ensure_pack(
            Path(request["folder"]),
            Path(request["uploads"]),
            list(request.get("listing") or []),
            background=False,
        )
    except Exception as failed:  # noqa: BLE001 - the pack's own status says it failed
        _log.exception("assistant set pack job failed")
        status, error = "failed", type(failed).__name__
    else:
        status = "done"
    finally:
        beat.stop()
    with factory() as session:
        finish(session, job_id, status, error=error)
        session.commit()
    return status


# ---- the supervisor ----------------------------------------------------------------------------------


def job_command(job_id: UUID) -> list[str]:
    """The command of one job's own process."""
    return [sys.executable, "-m", "app.review.assistant.job_worker", "--run", str(job_id)]


def _child_environment() -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(_REPO), *(p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p)]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    return env


class Supervisor:
    """Claims jobs and runs each in its own process (see the module)."""

    def __init__(
        self,
        factory: Factory,
        chat_root: Path,
        *,
        message_slots: int = 2,
        pack_slots: int = 1,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        start: Callable[[UUID], subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self._factory = factory
        self._chat_root = chat_root
        self._slots = {"message": message_slots, "set_pack": pack_slots}
        self._stale = stale_seconds
        self._start = start or self._spawn
        self.children: dict[UUID, tuple[str, subprocess.Popen[bytes]]] = {}
        self.me = worker_id()

    @staticmethod
    def _spawn(job_id: UUID) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            job_command(job_id),
            cwd=str(_REPO),
            env=_child_environment(),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def tick(self) -> None:
        """One look: dead jobs handled, finished processes collected, new jobs started."""
        self._collect()
        self._reap_stale()
        for kind, slots in self._slots.items():
            with self._factory() as session:
                free = slots - running_count(session, kind, self._stale)
                jobs = claim(session, self.me, kinds=[kind], limit=free)
                claimed = [job.id for job in jobs]
                session.commit()
            for job_id in claimed:
                try:
                    self.children[job_id] = (kind, self._start(job_id))
                    _log.warning("assistant worker: started %s job %s", kind, job_id)
                except Exception:  # noqa: BLE001 - the job is ended plainly, never left running
                    _log.exception("assistant worker could not start job %s", job_id)
                    self._dead(job_id, kind, "its process could not be started")

    def _collect(self) -> None:
        for job_id, (kind, child) in list(self.children.items()):
            code = child.poll()
            if code is None:
                continue
            del self.children[job_id]
            if code != 0:
                _log.warning("assistant worker: job %s process ended with code %s", job_id, code)
            self._dead(job_id, kind, f"its process ended (code {code})")

    def _dead(self, job_id: UUID, kind: str, why: str) -> None:
        """A job whose process is gone: nothing happens when it had ended normally."""
        if kind == "message":
            reap_dead(self._factory, self._chat_root, job_id, None, why=why)
        else:
            self._pack_dead(job_id, why)

    def _reap_stale(self) -> None:
        with self._factory() as session:
            stale = session.execute(
                select(AssistantJob.id, AssistantJob.kind).where(
                    AssistantJob.status == "running",
                    AssistantJob.heartbeat_at < func.now() - timedelta(seconds=self._stale),
                )
            ).all()
        for job_id, kind in stale:
            if job_id in self.children:
                continue  # its own process is alive: only slow (collected when it ends)
            if kind == "message":
                reap_dead(self._factory, self._chat_root, job_id, self._stale, why="it stopped")
            else:
                self._pack_dead(job_id, "its build stopped")

    def _pack_dead(self, job_id: UUID, why: str) -> None:
        with self._factory() as session:
            job = session.get(AssistantJob, job_id)
            if job is None or job.status != "running":
                return
            if job.attempts < PACK_ATTEMPTS:
                session.execute(
                    update(AssistantJob)
                    .where(AssistantJob.id == job_id, AssistantJob.status == "running")
                    .values(status="queued", claimed_by=None, updated_at=func.now())
                )
                session.commit()
                _log.warning("assistant worker: set pack job %s tried again (%s)", job_id, why)
                return
            request = dict(job.request)
            finish(session, job_id, "failed", error=why)
            session.commit()
        _log.warning("assistant worker: set pack job %s failed (%s)", job_id, why)
        try:
            pack = import_module("app.review.assistant.set_pack")
            folder = Path(request["folder"])
            folder.mkdir(parents=True, exist_ok=True)
            pack._set_status(
                folder,
                "failed",
                [],
                files=[str(item.get("name")) for item in request.get("listing") or []],
                reason=f"the set's reading {why}",
            )
        except Exception:  # noqa: BLE001 - the pack is read as not ready either way
            _log.warning("assistant worker: the failed pack's status not written", exc_info=True)

    def stop(self, wait_seconds: float = STOP_WAIT_S) -> None:
        """Every running job asked to store itself as interrupted (SIGTERM), then waited for."""
        for _kind, child in self.children.values():
            try:
                child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = threading.Event()
        waited = 0.0
        while self.children and waited < wait_seconds:
            self._collect()
            deadline.wait(0.2)
            waited += 0.2
        for _kind, child in self.children.values():
            child.kill()
        self._collect()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop goes on (the database may be restarting)
                _log.exception("assistant worker loop failed; trying again")
            stop.wait(POLL_S)
        self.stop()


# ---- the command line --------------------------------------------------------------------------------


def _settings() -> Any:
    from app.config import Settings

    return Settings()  # type: ignore[call-arg]


def _factory(settings: Any) -> Factory:
    from app.db.session import engine_from_settings

    return sessionmaker(bind=engine_from_settings(settings), expire_on_commit=False)


def stop_handler(stopping: threading.Event) -> Callable[[int, Any], None]:
    """SIGTERM / Ctrl-C: `stopping` is set from a short thread, never from the handler itself.

    The handler runs in the main thread, which may be inside `stopping.wait()` holding the event's
    own lock (not re-entrant): an `Event.set()` there waits for itself forever, and the worker then
    ignores every later SIGTERM while a new one starts beside it."""

    def on_signal(signum: int, frame: Any) -> None:
        del signum, frame
        threading.Thread(target=stopping.set, name="assistant-worker-stop", daemon=True).start()

    return on_signal


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true", help="claim and run jobs until stopped")
    mode.add_argument("--run", metavar="JOB_ID", help="run one claimed job (the supervisor's)")
    parser.add_argument("--message-slots", type=int, default=None)
    parser.add_argument("--pack-slots", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = _settings()
    factory = _factory(settings)
    stopping = threading.Event()
    on_signal = stop_handler(stopping)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if args.run:
        job_id = UUID(args.run)
        # A message's own set pack is a job too: never built beside another build of it.
        chat_service.use_pack_starter(pack_starter(factory, created_by="worker"))
        status = run_job(job_id, factory=factory, settings=settings, stopping=stopping)
        return 0 if status in ("done", "failed", "interrupted") else 1
    supervisor = Supervisor(
        factory,
        Path(settings.assistant_chat_root).expanduser(),
        message_slots=args.message_slots or settings.assistant_worker_message_slots,
        pack_slots=args.pack_slots or settings.assistant_worker_pack_slots,
        stale_seconds=float(settings.assistant_worker_stale_seconds),
    )
    _log.warning(
        "assistant worker %s watching (messages %d at once, set packs %d)",
        supervisor.me,
        supervisor._slots["message"],
        supervisor._slots["set_pack"],
    )
    supervisor.run(stopping)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by running the module
    sys.exit(main())
