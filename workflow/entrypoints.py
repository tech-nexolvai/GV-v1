"""The two long-running processes: the worker that runs stages, and the dispatcher that drains the
outbox (#415, F3.1).

C4 built every piece of the durable seam and shipped no process that runs them. `build_worker()` returned
a worker nobody started, and `dispatch_committed()` was called only from its own test. The second was the
serious one: **the outbox was written and never drained**, so a package looked accepted — API returned
success, row committed — and no work ever ran. A queue that only grows is the quietest kind of outage.

**These modules hold no business logic, on purpose.** They read settings, install a signal handler, and
call something that already exists. Everything worth testing about *what* happens is in `workflow/review.py`
and `workflow/outbox.py`, and putting a decision here would put it somewhere no test looks.

**A missing setting exits non-zero rather than starting.** A worker with no token that starts anyway is
worse than one that refuses: an orchestrator sees a running process, reports the service healthy, and the
queue silently does not move. Refusing is the honest answer to "can you do the job".

**`SIGTERM` finishes the pass in flight, then stops.** A dispatch interrupted halfway is not a lost
workflow — `dispatch_committed()` uses one transaction, so a kill rolls the whole batch back and the rows
stay undispatched. But it does mean the batch is dispatched again, and the cheap way to avoid that is
simply not to interrupt it. So the flag is checked between passes and never inside one.

Source: backend proposal §9.1–§9.4 · Design: `docs/DESIGN_PLATFORM.md` §6.1, §6.2 ·
Verification: `tests/workflow/test_entrypoints.py`
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from app.telemetry.tracing import carrier
from workflow.outbox import OutboxDispatchError, WorkflowStarter, dispatch_committed

if TYPE_CHECKING:  # pragma: no cover - annotations only, keeps gRPC and the ORM out of import
    from types import FrameType

    from sqlalchemy.orm import Session, sessionmaker

    from app.config import Settings

__all__ = [
    "EXIT_MISCONFIGURED",
    "EXIT_OK",
    "OUTBOX_ROW_METADATA_KEY",
    "Shutdown",
    "hatchet_starter",
    "run_dispatcher",
    "run_worker",
    "settings_or_none",
]

logger = logging.getLogger("gv.workflow.entrypoints")

#: A clean stop: the process was asked to shut down and finished what it was doing.
EXIT_OK: Final = 0

#: The process could not start because a required setting is missing or invalid.
#:
#: Distinct from a crash, so an operator reading an exit code can tell "you configured me wrong" from
#: "I broke". Restarting the first will never help; restarting the second sometimes does.
EXIT_MISCONFIGURED: Final = 78

#: Where the outbox row id is recorded on the workflow run.
#:
#: **This is traceability, not deduplication, and the difference matters.** The engine offers no
#: per-trigger idempotency key: `RunsClient.create` takes a name, an input and metadata, and Hatchet's
#: idempotency is declared on a workflow as a CEL expression — which `workflow/review.py` does not use.
#: So dispatching the same row twice really does create two workflow runs.
#:
#: What makes that safe is `workflow/idempotency.py`: each stage claims its work in PostgreSQL before
#: doing it, so the second run finds every stage already claimed and does nothing. Idempotency lives in
#: the database, which is where `AGENTS.md` §3 says business truth lives. Recording the row id here is
#: what lets a person answer "which outbox row started this run", and nothing more than that.
OUTBOX_ROW_METADATA_KEY: Final = "gv_outbox_row"


class Shutdown:
    """A stop flag wired to `SIGTERM` and `SIGINT`.

    An `Event` rather than a bare boolean because the dispatcher waits on it between passes: a stop then
    interrupts the wait immediately instead of leaving the process alive for the rest of the interval.
    A container runtime that sends `SIGTERM` and kills after ten seconds should not need those ten
    seconds.

    Handlers are installed on construction and only when running on the main thread; `signal.signal`
    raises anywhere else, and a test that builds one of these in a thread should not fail for that.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        if threading.current_thread() is threading.main_thread():
            for received in (signal.SIGTERM, signal.SIGINT):
                signal.signal(received, self._request_stop)

    def _request_stop(self, signum: int, frame: FrameType | None) -> None:
        """Record the request and return. Deliberately does no work.

        A signal handler runs between bytecodes, so anything substantial here — a database call, a log
        flush — happens at an arbitrary point in whatever was executing. Setting a flag is safe; the loop
        acts on it when it is somewhere sensible.
        """
        del frame
        logger.info("received signal %s; will stop after the current pass", signum)
        self._event.set()

    @property
    def requested(self) -> bool:
        """Whether a stop has been asked for."""
        return self._event.is_set()

    def wait(self, seconds: float) -> None:
        """Wait, unless a stop arrives first."""
        self._event.wait(seconds)

    def request_stop(self) -> None:
        """Ask for a stop without a signal — how a test ends a loop."""
        self._event.set()


def settings_or_none() -> Settings | None:
    """Settings read from the environment, or `None` with the reason already logged.

    Returns rather than raises so both entrypoints can turn it into an exit code without each catching
    Pydantic's error type. The message is the field list Pydantic produces, which names the missing
    variables — more use to whoever is fixing it than a sentence of ours would be.
    """
    from pydantic import ValidationError

    from app.config import Settings

    try:
        return Settings()  # type: ignore[call-arg]  # every value comes from the environment
    except ValidationError as invalid:
        logger.error(
            "cannot start: the configuration is incomplete or invalid. Fix these and try again:\n%s",
            invalid,
        )
        return None


def hatchet_starter(settings: Settings) -> WorkflowStarter:
    """A `WorkflowStarter` that asks Hatchet to run a workflow by name.

    By *name*, which is why this uses `runs.create` — the SDK marks it an escape hatch and prefers
    `Workflow.run`, but the outbox stores the workflow name as a string precisely so it holds no
    reference to the engine's objects. A seam that imported the workflow would not be a seam.

    The call triggers and returns; it does not wait for the run to finish. Waiting would hold the
    dispatcher's transaction open for the length of a package review.
    """
    from workflow.hatchet_app import hatchet_client

    client = hatchet_client(settings)

    def start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        # The trace context goes in the metadata, not the input. Same reason the outbox keeps it in its
        # own column: the input is the workflow's contract, and a `traceparent` appearing in it would be
        # an argument the workflow could read.
        #
        # Injected from whatever context is active, which `dispatch_committed` has already made the
        # enqueueing request's. Nothing here needs to know that — it just propagates what it is inside,
        # which is the one arrangement that cannot forget.
        metadata: dict[str, str] = {OUTBOX_ROW_METADATA_KEY: idempotency_key}
        metadata.update(carrier())
        client.runs.create(
            workflow_name=workflow,
            input=dict(payload),
            additional_metadata=metadata,
        )

    return start


def run_dispatcher(
    settings: Settings,
    *,
    factory: sessionmaker[Session],
    start: WorkflowStarter,
    shutdown: Shutdown | None = None,
    max_passes: int | None = None,
) -> int:
    """Poll the outbox until asked to stop. Returns the process exit code.

    One pass is one `dispatch_committed()` call. Between passes it waits `outbox_poll_seconds`; a stop
    request cuts the wait short but never interrupts a pass, because a pass is one transaction and
    interrupting it just means dispatching the same rows again.

    **A failed row does not stop the loop.** `dispatch_committed()` raises `OutboxDispatchError` after
    committing the rows that did go out, so the failure is logged and the next pass continues. The
    alternative — exiting — would let one unstartable payload stop delivery for every other package,
    which is the failure the outbox exists to prevent.

    `max_passes` bounds the loop for tests. `None` means "until stopped", and a test that forgot to stop
    would otherwise hang rather than fail — which is a lesson this repository has already paid for once.
    """
    stopper = shutdown or Shutdown()
    interval = settings.outbox_poll_seconds
    limit = settings.outbox_batch_limit
    logger.info(
        "outbox dispatcher starting: polling every %ss, at most %s rows per pass", interval, limit
    )

    passes = 0
    while not stopper.requested and (max_passes is None or passes < max_passes):
        passes += 1
        try:
            dispatched = dispatch_committed(factory, start, limit=limit)
        except OutboxDispatchError as partial:
            # Logged per pass rather than aggregated: an operator watching this needs to see that rows
            # are failing *now*, and how many still went out, which is the difference between a bad
            # payload and an engine that is down.
            logger.error(
                "pass %s: %s row(s) dispatched, %s failed: %s",
                passes,
                partial.dispatched,
                len(partial.failures),
                "; ".join(f"{row}: {error}" for row, error in partial.failures),
            )
        else:
            # Every pass is logged, including the empty ones. "0 rows" is the evidence that the
            # dispatcher is alive and the queue is genuinely empty — the state that used to be
            # indistinguishable from no dispatcher at all.
            logger.info("pass %s: %s row(s) dispatched", passes, dispatched)

        if stopper.requested or (max_passes is not None and passes >= max_passes):
            break
        stopper.wait(interval)

    logger.info("outbox dispatcher stopped after %s pass(es)", passes)
    return EXIT_OK


def run_worker(settings: Settings, *, factory: sessionmaker[Session]) -> int:
    """Build the worker from settings and block on it. Returns the process exit code.

    `worker.start()` blocks until the engine disconnects or the process is signalled, and the SDK
    installs its own signal handling for graceful shutdown — so this does not add a `Shutdown`. Wrapping
    the SDK's handling in ours would give two things trying to stop one worker, and the SDK's is the one
    that knows how to drain a task in flight.

    A missing token is refused before the worker is built. `Hatchet()` would raise anyway, but the
    message it gives is a Pydantic validation error about `ClientConfig`, which reads like a bug in this
    code rather than a missing environment variable.
    """
    if not settings.hatchet_token:
        logger.error(
            "cannot start the worker: GV_HATCHET_TOKEN is empty. A worker with no token cannot reach "
            "the engine, and starting anyway would report a healthy process that runs nothing."
        )
        return EXIT_MISCONFIGURED

    from workflow.hatchet_app import build_worker
    from workflow.stages import DatabaseStages

    # **The worker gets the real stages.** It resolved `NoStages()` until now, because nothing was
    # ever passed — so a deployed worker ran the whole pipeline and recorded that it had implemented
    # none of it. `DatabaseStages` implements one stage for real and keeps `NoStages`' answer for the
    # other five, so what is built runs and what is not still says so.
    worker = build_worker(settings, factory=factory, stages=DatabaseStages())
    logger.info("worker starting")
    worker.start()
    logger.info("worker stopped")
    return EXIT_OK
