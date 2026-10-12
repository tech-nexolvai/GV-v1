"""The transactional outbox: how a business change and a workflow start stop being two writes.

## The problem

Writing package state to PostgreSQL and starting a Hatchet workflow are two writes to two systems
with no shared transaction. Either can fail after the other succeeded, and both failures are silent:
a package nothing is working on, or a workflow for a package that was never written. Retrying the
pair does not fix it, because a retry is two writes again.

## What this actually guarantees

**The business change and the intent to start a workflow commit together, or neither does.**
`enqueue()` only ever adds a row to the caller's open transaction. It opens no transaction of its
own, it commits nothing, and it starts nothing. If the caller's transaction rolls back — an
exception, a lost connection, a killed process — the outbox row goes with it. That is the whole
mechanism, and it works for one reason: the row and the business change are in the same PostgreSQL
transaction. **If they can ever end up in different transactions, this is not an outbox and it
guarantees nothing.** So `enqueue()` takes the caller's `Session` and never a session factory.

**Delivery is at-least-once. It is not exactly-once, and no outbox can make it so.**
`dispatch_committed()` calls the workflow engine *before* it stamps `dispatched_at`. If the process
dies in between — after the engine accepted the start, before the transaction committed — the row is
still undispatched and the next poll starts that workflow again. That ordering is chosen
deliberately: the other order (stamp first, start second) would lose the start entirely, and a
duplicate start is a recoverable annoyance where a lost one is a package stuck forever with nobody
looking at it.

Duplicates are only harmless because starting is idempotent — C4.2, keyed so a repeat is a no-op
returning the prior result. The outbox row's own id is passed to the starter as `idempotency_key`,
because it is the one value that is stable across restarts, processes and retries. **Take the
idempotency away and at-least-once becomes at-least-once execution of real work**, which for paid
model calls and half-written evidence is not a shrug.

## What this does not guarantee

- It does not stop somebody calling the workflow engine directly. Nothing in Python can. This module
  is the only supported path, and "no workflow starts without an outbox row" holds exactly as far as
  every caller uses it. Review is the enforcement; this docstring is the statement of intent.
- It does not bound how late delivery is. A dispatcher that is not running delivers nothing, and the
  rows simply accumulate. That is why `stuck_entries()` exists — an outbox nobody is draining must be
  loud, because "silently stuck" is the exact failure this design was chosen to replace.
- It does not retry with backoff. `attempts` counts hand-offs tried. It gives up on a row only for a
  refusal no retry can change (`_permanent`: the lifecycle table has no such move), and only
  after `max_refused_attempts` of those refusals (`refused_attempts`, counted apart from other
  failures), recording `abandoned_at` and the reason (`abandoned_entries()` lists them)
  (#1182). Every other failure is retried on the next poll, as before.

Source: `docs/DESIGN_PLATFORM.md` §6.1; backend proposal §9.2–§9.4; `AGENTS.md` §6.
Verification: `tests/workflow/test_outbox.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import utc_now
from app.db.session import unit_of_work
from app.models.outbox import OutboxEntry
from app.telemetry.tracing import carrier, incoming_context, traced

__all__ = [
    "MAX_REFUSED_ATTEMPTS",
    "OutboxDispatchError",
    "OutboxEntry",
    "WorkflowStarter",
    "abandoned_entries",
    "dispatch_committed",
    "enqueue",
    "stuck_entries",
]


#: How many permanent refusals a row may get before the dispatcher gives up on it; other failures
#: are not counted. More than one, because a revision may still be on its way to a state the work
#: can start from (a check run asked for while the reading is finishing); at the default two-second
#: poll this is about forty seconds.
MAX_REFUSED_ATTEMPTS = 20


def _permanent(failure: BaseException) -> bool:
    """Whether `failure`, or anything it was raised from, is a refusal no retry can change.

    That is `IllegalTransition`: the lifecycle table has no move from the revision's state to the one
    the work needs (a check run asked for after sign-off: `APPROVED` never moves to
    `RUNNING_CHECKS`). Before #1182 such a row was retried on every poll for ever (116 attempts in
    five minutes in the 2026-10-11 audit). `EntryConditionUnmet` is deliberately not one: it means
    "not yet", and a later poll may succeed.
    """
    # Imported here: `app.lifecycle` imports this module.
    from app.lifecycle.states import IllegalTransition

    seen: set[int] = set()
    current: BaseException | None = failure
    while current is not None and id(current) not in seen:
        if isinstance(current, IllegalTransition):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class WorkflowStarter(Protocol):
    """Whatever actually starts a workflow — the Hatchet client, in time.

    Injected rather than imported so this module holds no opinion about the engine, and so the
    induced-failure tests can make starting fail on demand. `AGENTS.md` §3 is explicit that
    PostgreSQL owns business truth and the engine owns execution; the outbox is the seam, and a seam
    that imports one side is not a seam.

    `idempotency_key` is the outbox row id. The starter must treat a repeat of the same key as a
    no-op, because this module will hand it the same key again after a crash.
    """

    def __call__(
        self, *, workflow: str, payload: Mapping[str, object], idempotency_key: str
    ) -> None: ...


class OutboxDispatchError(Exception):
    """Starting failed for at least one row — raised *after* the successful rows were committed.

    Failing loudly rather than returning a count and swallowing the rest: a dispatcher that quietly
    reports "0 dispatched" forever is the silently-stuck outbox this whole design exists to replace.
    The rows that did go out stay dispatched, the rows that failed keep their incremented `attempts`
    and remain undispatched, so nothing is lost either way.
    """

    def __init__(self, dispatched: int, failures: tuple[tuple[UUID, Exception], ...]) -> None:
        self.dispatched = dispatched
        """Rows successfully handed to the engine and committed before this was raised."""

        self.failures = failures
        """`(outbox row id, the exception the starter raised)`, in the order they were attempted."""

        super().__init__(
            f"{len(failures)} outbox row(s) could not be started "
            f"({dispatched} succeeded): {failures[0][1]!r}"
        )
        self.__cause__ = failures[0][1]


def _reject_inexact_numbers(value: object, path: str) -> None:
    """Refuse a float anywhere in the payload, at any depth.

    `AGENTS.md` §6 allows no approximate number to be persisted, and JSONB would accept one without
    complaint. A payload carries identifiers and keys, so a float in it is far more likely to be a
    measurement that has already lost precision than anything intended. Use an int, or the exact
    text of the number as a string.
    """

    if isinstance(value, float):
        raise TypeError(
            f"{path} is a float ({value!r}). Persisted numbers must be exact — pass an int, or the "
            "exact text of the value as a string."
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_inexact_numbers(item, f"{path}[{key!r}]")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_inexact_numbers(item, f"{path}[{index}]")


def enqueue(session: Session, *, workflow: str, payload: Mapping[str, object]) -> UUID:
    """Record the intent to start a workflow, in the caller's transaction. Starts nothing.

    Call this beside the business write, inside the same `unit_of_work`. Do not commit here, do not
    hand this function a fresh session, and do not "just start the workflow too" — each of those
    reintroduces the dual write this exists to remove.

    It deliberately does not flush. A flush would put the row in front of the database early for no
    benefit; the caller's commit is the only moment that matters, and it is the moment the row and
    the business change become durable together.

    Returns the entry's id, so a caller can hand it back as a handle to the work it just accepted
    (#208, C2.6). This needs no flush of its own: `app/db/base.py` assigns identity in `__init__`
    rather than at INSERT, precisely so an id is available before the row reaches the database.

    **The id names the enqueued work, not a workflow run.** Nothing has started yet, and the run id
    does not exist until a dispatcher hands the row to the engine after commit. A caller returning
    this to a client must not call it a run id — see `app/api/background.py`.
    """

    if not workflow.strip():
        raise ValueError("workflow must be a non-empty name")
    _reject_inexact_numbers(payload, "payload")
    # Captured here, in the caller's transaction, because *here* is where the work was asked for. The
    # dispatcher runs later in another process; a context captured there would produce a trace that
    # begins at a background poll and answers none of the questions a trace exists to answer.
    #
    # Empty when nothing is being traced — a cron job, a test — and stored as NULL rather than as an
    # empty object, so "no trace" and "a trace with nothing in it" are not the same row.
    context = carrier()
    entry = OutboxEntry(
        workflow=workflow,
        payload=dict(payload),
        trace_context=context or None,
        dispatched_at=None,
        attempts=0,
    )
    session.add(entry)
    return entry.id


def dispatch_committed(
    factory: sessionmaker[Session],
    start: WorkflowStarter,
    *,
    limit: int = 100,
    max_refused_attempts: int = MAX_REFUSED_ATTEMPTS,
) -> int:
    """Start the workflows for rows that are already committed, and return how many went out.

    Only committed rows are visible to this transaction, which is what makes "after commit" true
    without any coordination: a row still inside somebody's open transaction cannot be read here, so
    a workflow can never be started for a business change that has not landed.

    `FOR UPDATE SKIP LOCKED` is what lets several dispatchers run at once — a row another dispatcher
    holds is stepped over rather than waited on. Under contention a poll can return fewer than
    `limit` rows even when more are pending, because PostgreSQL applies the limit before the lock;
    the next poll picks them up.

    The whole batch shares one transaction, and the order inside it is the guarantee: increment
    `attempts`, call the engine, and only then stamp `dispatched_at`. Kill the process anywhere in
    that window and everything rolls back, including rows already started in this batch — they will
    be started again. That is at-least-once, and it is safe only because starting is idempotent
    (C4.2); the row id goes to the starter as `idempotency_key` so the repeat is recognised.

    A starter that raises an ordinary exception is treated as a failed attempt, not a crash: the row
    keeps its incremented `attempts`, stays undispatched, and the poll moves on to the next row so
    one bad payload cannot block the queue behind it. The failures are then raised as
    `OutboxDispatchError` once the successes are safely committed.

    **A row refused for good is given up, not retried for ever (#1182).** When the starter's failure
    is permanent (`_permanent`) and the row has now been refused that way `max_refused_attempts`
    times (`refused_attempts`; other failures, like an engine outage, never count), the row gets `abandoned_at` and the refusal's words in `abandoned_reason`, and is never polled again.
    It is still reported in this pass's `OutboxDispatchError`, so the giving up is loud once.
    """

    if limit < 1:
        raise ValueError("limit must be at least 1")
    if max_refused_attempts < 1:
        raise ValueError("max_refused_attempts must be at least 1")

    dispatched = 0
    failures: list[tuple[UUID, Exception]] = []
    with unit_of_work(factory) as session:
        entries = session.scalars(
            select(OutboxEntry)
            .where(OutboxEntry.dispatched_at.is_(None), OutboxEntry.abandoned_at.is_(None))
            # `created_at` is assigned in Python and two rows can share a microsecond; the id
            # settles the tie so the order is total and the poll is reproducible.
            .order_by(OutboxEntry.created_at, OutboxEntry.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for entry in entries:
            entry.attempts += 1
            # The starter is called *inside* the row's own trace, so whatever it hands the engine is
            # injected from the request's context rather than the dispatcher's. That is what makes the
            # workflow a continuation of the request instead of a sibling of it — and it is why the
            # propagation lives here rather than in `hatchet_starter`, which would otherwise have to be
            # told the context and could forget.
            parent = incoming_context(entry.trace_context or {})
            try:
                with traced(
                    "outbox.dispatch",
                    parent=parent,
                    workflow_run_id=str(entry.id),
                ):
                    start(
                        workflow=entry.workflow,
                        payload=entry.payload,
                        idempotency_key=str(entry.id),
                    )
            except Exception as failure:  # noqa: BLE001 - re-raised below, after the commit
                failures.append((entry.id, failure))
                if _permanent(failure):
                    entry.refused_attempts += 1
                    if entry.refused_attempts >= max_refused_attempts:
                        entry.abandoned_at = utc_now()
                        entry.abandoned_reason = (
                            f"given up after {entry.refused_attempts} refusals: "
                            f"{type(failure).__name__}: {failure}"
                        )[:1000]
                continue
            entry.dispatched_at = utc_now()
            dispatched += 1

    if failures:
        raise OutboxDispatchError(dispatched, tuple(failures))
    return dispatched


def stuck_entries(session: Session, older_than: timedelta) -> list[OutboxEntry]:
    """Undispatched rows older than `older_than`, oldest first — the query to alert on.

    A row here means a business change committed and its workflow never started. The cause is
    usually dull (no dispatcher is running) and occasionally not (the engine is refusing one
    payload), but either way somebody has to be told, because the package is sitting in a state
    nothing will move it out of.

    `older_than` must be positive. A zero threshold would return every row that has not been
    dispatched in the last instant, which is normal operation, and an alert that fires constantly is
    an alert nobody reads.
    """

    if older_than <= timedelta(0):
        raise ValueError("older_than must be a positive interval")

    rows: Sequence[OutboxEntry] = session.scalars(
        select(OutboxEntry)
        .where(
            OutboxEntry.dispatched_at.is_(None),
            # A row given up with its reason recorded is no longer waiting for anything (#1182).
            OutboxEntry.abandoned_at.is_(None),
            OutboxEntry.created_at <= utc_now() - older_than,
        )
        .order_by(OutboxEntry.created_at, OutboxEntry.id)
    ).all()
    return list(rows)


def abandoned_entries(session: Session) -> list[OutboxEntry]:
    """Rows the dispatcher gave up on (#1182), oldest first, each with its `abandoned_reason`.

    Not in `stuck_entries`, because nothing will ever move them: they are answered, with a reason.
    They are still work somebody asked for that did not happen, so this is the list an operator
    reads next to the stuck one.
    """
    rows: Sequence[OutboxEntry] = session.scalars(
        select(OutboxEntry)
        .where(OutboxEntry.abandoned_at.is_not(None))
        .order_by(OutboxEntry.abandoned_at, OutboxEntry.id)
    ).all()
    return list(rows)
