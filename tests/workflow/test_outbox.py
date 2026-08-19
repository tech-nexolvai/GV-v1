"""The transactional outbox, proved against a real database (#213, C4.1).

The claim being tested is narrow and worth stating exactly: **a business change and the intent to
start a workflow commit together or not at all.** Everything else in this file exists to stop that
claim being read as more than it is — delivery is at-least-once, a crash re-delivers, and a row
nobody drains has to be visible rather than silent.

None of it can be tested with a mock session. A fake that "rolls back" because the test told it to
proves the test author's belief and nothing about PostgreSQL, so every test here that says anything
about atomicity runs against a real database and skips without one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.base import Base, Immutable, immutable_table_names, utc_now
from app.db.session import session_factory, unit_of_work
from app.models import OutboxEntry, Project
from workflow.outbox import (
    OutboxDispatchError,
    dispatch_committed,
    enqueue,
    stuck_entries,
)

WORKFLOW = "review_package"


class RecordingStarter:
    """A workflow engine stand-in that remembers what it was asked to start.

    `fail_on` names the workflows it refuses, so a test can make starting fail for one row and not
    another. `crash_on` raises `KeyboardInterrupt` instead — not an ordinary failure but a model of
    the process being killed mid-dispatch, which is the case the at-least-once claim is about.
    """

    def __init__(self, *, fail_on: frozenset[str] = frozenset(), crash_on: str | None = None):
        self.calls: list[tuple[str, Mapping[str, object], str]] = []
        self._fail_on = fail_on
        self._crash_on = crash_on

    def __call__(
        self, *, workflow: str, payload: Mapping[str, object], idempotency_key: str
    ) -> None:
        self.calls.append((workflow, dict(payload), idempotency_key))
        if workflow == self._crash_on:
            raise KeyboardInterrupt("the dispatcher process was killed")
        if workflow in self._fail_on:
            raise RuntimeError(f"the engine refused {workflow}")

    @property
    def keys(self) -> list[str]:
        return [key for _, _, key in self.calls]

    @property
    def workflows(self) -> list[str]:
        return [workflow for workflow, _, _ in self.calls]


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    """A session factory against a database migrated to head."""

    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _entries(session: Session) -> list[OutboxEntry]:
    return list(session.scalars(select(OutboxEntry).order_by(OutboxEntry.created_at)))


def _only(session: Session) -> OutboxEntry:
    return session.scalars(select(OutboxEntry)).one()


def _age(session: Session, entry_id: UUID, by: timedelta) -> None:
    """Backdate a row so a threshold test does not have to wait for real time to pass.

    Written as a plain `UPDATE`, which also demonstrates the point made in the migration: this table
    is not append-only, and it must not be, because the dispatcher has to write to it.
    """

    session.execute(
        update(OutboxEntry).where(OutboxEntry.id == entry_id).values(created_at=utc_now() - by)
    )


# ---------------------------------------------------------------------------
# Input guards — no database needed
# ---------------------------------------------------------------------------


def test_enqueue_rejects_an_empty_workflow() -> None:
    """A blank name is a row that can never be dispatched, and it would look like a stuck row
    forever rather than like the bug it is."""

    with pytest.raises(ValueError, match="non-empty"):
        enqueue(Session(), workflow="   ", payload={})


def test_enqueue_rejects_a_float_in_the_payload() -> None:
    """`AGENTS.md` §6 allows no approximate number to be persisted, and JSONB would keep one
    without complaint."""

    with pytest.raises(TypeError, match="float"):
        enqueue(Session(), workflow=WORKFLOW, payload={"width_mm": 812.8})


def test_enqueue_finds_a_float_nested_deep_in_the_payload() -> None:
    """A guard that only looked at the top level would miss every realistic case — the measurement
    is always inside something."""

    with pytest.raises(TypeError, match=r"payload\['items'\]\[1\]\['width'\]"):
        enqueue(
            Session(),
            workflow=WORKFLOW,
            payload={"items": [{"width": 1}, {"width": 812.8}]},
        )


def test_enqueue_accepts_ints_and_exact_strings() -> None:
    """The guard must be able to say yes: `True` is not a float, and an exact decimal string is the
    supported way to carry a fractional value."""

    session = Session()
    enqueue(session, workflow=WORKFLOW, payload={"pages": 4, "ok": True, "width_mm": "812.8"})
    assert len(session.new) == 1


def test_dispatch_rejects_a_useless_limit(factory: sessionmaker[Session]) -> None:
    """A limit of zero would poll, claim nothing and report success — a dispatcher that looks
    healthy and delivers nothing is the failure this design replaces."""

    with pytest.raises(ValueError, match="at least 1"):
        dispatch_committed(factory, RecordingStarter(), limit=0)


def test_stuck_entries_rejects_a_non_positive_threshold(factory: sessionmaker[Session]) -> None:
    """A zero threshold returns every undispatched row, which is normal operation. An alert that
    fires constantly is an alert nobody reads."""

    with unit_of_work(factory) as session, pytest.raises(ValueError, match="positive"):
        stuck_entries(session, timedelta(0))


# ---------------------------------------------------------------------------
# The atomicity claim — the only reason this module exists
# ---------------------------------------------------------------------------


def test_the_business_row_and_the_outbox_row_commit_together(
    factory: sessionmaker[Session],
) -> None:
    """One transaction, both rows. The positive half of the claim."""

    with unit_of_work(factory) as session:
        project = Project(name="atomic")
        session.add(project)
        enqueue(session, workflow=WORKFLOW, payload={"project": str(project.id)})

    with unit_of_work(factory) as session:
        assert session.scalars(select(Project)).one().name == "atomic"
        assert _only(session).workflow == WORKFLOW


def test_an_induced_failure_after_the_business_write_leaves_neither_row(
    factory: sessionmaker[Session],
) -> None:
    """The acceptance criterion, stated as a test: crash after the business write and after the
    enqueue, before the commit, and *neither* survives.

    This is the whole mechanism. If the outbox row could outlive a rolled-back business change we
    would start a workflow for a package that does not exist — the same orphan the dual write
    produces, just pointing the other way.
    """

    class InducedFailure(Exception):
        pass

    with pytest.raises(InducedFailure), unit_of_work(factory) as session:
        project = Project(name="doomed")
        session.add(project)
        enqueue(session, workflow=WORKFLOW, payload={"project": str(project.id)})
        session.flush()  # both rows are really in the database, and still uncommitted
        raise InducedFailure("the process died before commit")

    with unit_of_work(factory) as session:
        assert session.scalars(select(Project)).all() == []
        assert _entries(session) == []


def test_enqueue_does_not_commit_so_the_callers_rollback_discards_it(
    factory: sessionmaker[Session],
) -> None:
    """Stated the other way round from the test above, without an exception involved: the caller
    rolls back deliberately, and the row goes with it. `enqueue` owns no transaction."""

    session = factory()
    try:
        enqueue(session, workflow=WORKFLOW, payload={})
        session.flush()
        assert len(_entries(session)) == 1
        session.rollback()
    finally:
        session.close()

    with unit_of_work(factory) as session:
        assert _entries(session) == []


def test_enqueue_starts_nothing(factory: sessionmaker[Session]) -> None:
    """It cannot start anything — it is not given anything that could. The test records the
    intention anyway, because "enqueue also kicks off the workflow" is the exact shortcut that would
    put the dual write back."""

    starter = RecordingStarter()
    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})
    assert starter.calls == []

    with unit_of_work(factory) as session:
        assert _only(session).dispatched_at is None
        assert _only(session).attempts == 0


def test_dispatch_cannot_see_a_row_that_has_not_committed(
    factory: sessionmaker[Session],
) -> None:
    """ "Dispatch committed" is literal. A row inside somebody's open transaction is invisible to the
    dispatcher's transaction, so no workflow can start for a business change that has not landed —
    and no coordination is needed to arrange that."""

    starter = RecordingStarter()
    writer = factory()
    try:
        enqueue(writer, workflow=WORKFLOW, payload={})
        writer.flush()
        assert dispatch_committed(factory, starter) == 0
        assert starter.calls == []
        writer.commit()
    finally:
        writer.close()

    assert dispatch_committed(factory, starter) == 1
    assert starter.workflows == [WORKFLOW]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_dispatch_starts_the_workflow_and_marks_the_row(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={"package": "abc"})

    starter = RecordingStarter()
    assert dispatch_committed(factory, starter) == 1

    with unit_of_work(factory) as session:
        entry = _only(session)
        assert entry.dispatched_at is not None
        assert entry.dispatched_at.tzinfo is not None
        assert entry.attempts == 1
        assert starter.calls == [(WORKFLOW, {"package": "abc"}, str(entry.id))]


def test_the_idempotency_key_is_the_rows_own_id(factory: sessionmaker[Session]) -> None:
    """At-least-once is only harmless because the start is idempotent, and the key has to be stable
    across processes and restarts. The row id is the one value that is — a clock or a counter is
    not."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})
    with unit_of_work(factory) as session:
        entry_id = _only(session).id

    starter = RecordingStarter()
    dispatch_committed(factory, starter)
    assert starter.keys == [str(entry_id)]
    assert UUID(starter.keys[0]) == entry_id


def test_an_already_dispatched_row_is_never_started_again(factory: sessionmaker[Session]) -> None:
    """Not a guarantee against duplicates — the test below shows one — but the steady state must not
    re-send everything on every poll."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})

    starter = RecordingStarter()
    assert dispatch_committed(factory, starter) == 1
    assert dispatch_committed(factory, starter) == 0
    assert len(starter.calls) == 1


def test_dispatch_honours_its_limit_and_takes_the_oldest_first(
    factory: sessionmaker[Session],
) -> None:
    for index in range(3):
        with unit_of_work(factory) as session:
            enqueue(session, workflow=f"w{index}", payload={})

    starter = RecordingStarter()
    assert dispatch_committed(factory, starter, limit=2) == 2
    assert starter.workflows == ["w0", "w1"]
    assert dispatch_committed(factory, starter, limit=2) == 1
    assert starter.workflows == ["w0", "w1", "w2"]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_a_crash_between_starting_and_committing_re_delivers(
    factory: sessionmaker[Session],
) -> None:
    """The at-least-once claim, demonstrated rather than asserted.

    The starter records the call and then the process "dies" — a `KeyboardInterrupt`, which is what
    an abrupt kill actually looks like in Python and is deliberately not caught. The transaction
    rolls back, so the row is undispatched *and* its attempt count is gone, even though the engine
    really was asked to start the workflow. The next poll asks again.

    Duplicate delivery is therefore real and expected. It is safe only because the key is the same
    both times, which is what lets C4.2 turn the second start into a no-op. This test asserting the
    key is identical is the whole reason it is worth writing.
    """

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})

    crasher = RecordingStarter(crash_on=WORKFLOW)
    with pytest.raises(KeyboardInterrupt):
        dispatch_committed(factory, crasher)

    with unit_of_work(factory) as session:
        entry = _only(session)
        assert entry.dispatched_at is None, "a crash must not look like a delivery"
        assert entry.attempts == 0, "the whole transaction rolled back, attempts included"

    survivor = RecordingStarter()
    assert dispatch_committed(factory, survivor) == 1
    assert survivor.keys == crasher.keys, "re-delivery must reuse the key, or it is not harmless"


def test_a_failing_start_records_the_attempt_and_leaves_the_row_for_the_next_poll(
    factory: sessionmaker[Session],
) -> None:
    """An ordinary failure is not a crash: the engine said no, the process is fine, and the attempt
    is worth keeping. The row stays undispatched and `attempts` climbs, which is what makes a row
    that keeps failing look different from one nobody has polled."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})

    starter = RecordingStarter(fail_on=frozenset({WORKFLOW}))
    with pytest.raises(OutboxDispatchError) as raised:
        dispatch_committed(factory, starter)
    assert raised.value.dispatched == 0
    assert isinstance(raised.value.failures[0][1], RuntimeError)

    with unit_of_work(factory) as session:
        assert _only(session).dispatched_at is None
        assert _only(session).attempts == 1

    with pytest.raises(OutboxDispatchError):
        dispatch_committed(factory, starter)
    with unit_of_work(factory) as session:
        assert _only(session).attempts == 2, "a retried row must show that it was retried"


def test_one_unstartable_row_does_not_block_the_queue_behind_it(
    factory: sessionmaker[Session],
) -> None:
    """A single bad payload at the head of the queue must not stop every package behind it. The
    successes commit; the failure is raised afterwards so it cannot be missed."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow="poison", payload={})
    with unit_of_work(factory) as session:
        enqueue(session, workflow="healthy", payload={})

    starter = RecordingStarter(fail_on=frozenset({"poison"}))
    with pytest.raises(OutboxDispatchError) as raised:
        dispatch_committed(factory, starter)

    assert raised.value.dispatched == 1
    assert len(raised.value.failures) == 1
    with unit_of_work(factory) as session:
        by_workflow = {entry.workflow: entry for entry in _entries(session)}
        assert by_workflow["healthy"].dispatched_at is not None
        assert by_workflow["poison"].dispatched_at is None


def test_the_dispatch_error_names_the_row_and_keeps_the_cause(
    factory: sessionmaker[Session],
) -> None:
    """An alert has to say which row, or somebody has to go find it by hand."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})
    with unit_of_work(factory) as session:
        entry_id = _only(session).id

    with pytest.raises(OutboxDispatchError) as raised:
        dispatch_committed(factory, RecordingStarter(fail_on=frozenset({WORKFLOW})))
    assert raised.value.failures[0][0] == entry_id
    assert isinstance(raised.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# Two dispatchers at once
# ---------------------------------------------------------------------------


def test_two_dispatchers_never_claim_the_same_row(factory: sessionmaker[Session]) -> None:
    """`FOR UPDATE SKIP LOCKED`, proved with two real connections rather than by reading the SQL.

    The second dispatcher runs from inside the first one's starter, so it is genuinely concurrent:
    the outer transaction is open and holding the row lock at the moment the inner poll runs. It
    must find nothing rather than start the same workflow twice or block until the outer commits.
    """

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})

    inner = RecordingStarter()
    claimed_by_the_second_dispatcher: list[int] = []

    def outer_start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        del workflow, payload, idempotency_key
        claimed_by_the_second_dispatcher.append(dispatch_committed(factory, inner))

    assert dispatch_committed(factory, outer_start) == 1
    assert claimed_by_the_second_dispatcher == [0]
    assert inner.calls == []


def test_a_locked_row_is_skipped_and_the_next_one_is_taken(
    factory: sessionmaker[Session],
) -> None:
    """Skipping is not the same as stopping. A second dispatcher must get on with the rest of the
    queue while the first holds one row, which is the entire point of `SKIP LOCKED` over a plain
    `FOR UPDATE`."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow="first", payload={})
    with unit_of_work(factory) as session:
        enqueue(session, workflow="second", payload={})

    inner = RecordingStarter()

    def outer_start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        del workflow, payload, idempotency_key
        assert dispatch_committed(factory, inner) == 1

    assert dispatch_committed(factory, RecordingStarter(), limit=1) == 1
    # The outer poll above already delivered "first"; run the nested case on a fresh pair.
    with unit_of_work(factory) as session:
        enqueue(session, workflow="third", payload={})

    assert dispatch_committed(factory, outer_start, limit=1) == 1
    assert inner.workflows == ["third"]

    with unit_of_work(factory) as session:
        assert all(entry.dispatched_at is not None for entry in _entries(session))


# ---------------------------------------------------------------------------
# A stuck row is visible
# ---------------------------------------------------------------------------


def test_stuck_entries_finds_an_undispatched_row_past_the_threshold(
    factory: sessionmaker[Session],
) -> None:
    """A committed business change whose workflow never started. Usually it means no dispatcher is
    running; either way somebody has to be told, because nothing will move that package on."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})
    with unit_of_work(factory) as session:
        _age(session, _only(session).id, timedelta(hours=2))

    with unit_of_work(factory) as session:
        stuck = stuck_entries(session, timedelta(hours=1))
        assert [entry.workflow for entry in stuck] == [WORKFLOW]


def test_stuck_entries_ignores_a_row_that_is_merely_recent(
    factory: sessionmaker[Session],
) -> None:
    """Undispatched is normal for a moment. Reporting every fresh row would make the alert useless
    within a day."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})
    with unit_of_work(factory) as session:
        assert stuck_entries(session, timedelta(hours=1)) == []


def test_stuck_entries_ignores_a_row_that_was_delivered(factory: sessionmaker[Session]) -> None:
    """Old and dispatched is not stuck. A query that flagged it would drown the real ones."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})
    dispatch_committed(factory, RecordingStarter())
    with unit_of_work(factory) as session:
        _age(session, _only(session).id, timedelta(days=7))
    with unit_of_work(factory) as session:
        assert stuck_entries(session, timedelta(hours=1)) == []


def test_a_row_that_keeps_failing_shows_up_as_stuck_with_its_attempts(
    factory: sessionmaker[Session],
) -> None:
    """The case that matters most: the dispatcher *is* running, and this one row cannot be started.
    Without `attempts` it would be indistinguishable from a row nobody has polled yet."""

    with unit_of_work(factory) as session:
        enqueue(session, workflow=WORKFLOW, payload={})
    starter = RecordingStarter(fail_on=frozenset({WORKFLOW}))
    for _ in range(3):
        with pytest.raises(OutboxDispatchError):
            dispatch_committed(factory, starter)

    with unit_of_work(factory) as session:
        _age(session, _only(session).id, timedelta(hours=2))
    with unit_of_work(factory) as session:
        stuck = stuck_entries(session, timedelta(hours=1))
        assert len(stuck) == 1
        assert stuck[0].attempts == 3


# ---------------------------------------------------------------------------
# The schema refuses the states the code must never produce
# ---------------------------------------------------------------------------


def _raw_insert(session: Session, **values: Any) -> None:
    """Insert straight through the table, past `enqueue`'s Python guards, so the assertion is about
    the database and not about the validation in front of it."""

    row: dict[str, Any] = {
        "id": uuid4(),
        "created_at": utc_now(),
        "payload": {},
        "dispatched_at": None,
        "attempts": 0,
    }
    session.execute(insert(OutboxEntry).values(row | values))


def _violated(raised: pytest.ExceptionInfo[IntegrityError]) -> str:
    """The constraint PostgreSQL actually rejected on.

    Asserting on the message text would pass for any check on the table, so a negative test could
    keep passing while failing for a different reason than the one it names.
    """

    constraint = raised.value.orig.diag.constraint_name  # type: ignore[union-attr]
    assert constraint is not None
    return constraint


def test_the_database_refuses_a_blank_workflow(factory: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        _raw_insert(session, workflow="")
    assert _violated(raised) == "ck_outbox_entries_outbox_entry_workflow"


def test_the_database_refuses_a_negative_attempt_count(factory: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        _raw_insert(session, workflow=WORKFLOW, attempts=-1)
    assert _violated(raised) == "ck_outbox_entries_outbox_entry_attempts"


def test_the_database_refuses_a_delivery_with_no_attempt_behind_it(
    factory: sessionmaker[Session],
) -> None:
    """The dispatcher increments `attempts` before it calls the engine and stamps `dispatched_at`
    only after the engine returned. This makes that ordering a fact about the schema: a row claiming
    it was delivered without a single hand-off is not representable, so a future dispatcher that
    stamps first cannot quietly turn at-least-once into at-most-once."""

    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        _raw_insert(session, workflow=WORKFLOW, dispatched_at=utc_now(), attempts=0)
    assert _violated(raised) == "ck_outbox_entries_outbox_entry_dispatched_needs_attempt"


def test_every_constraint_name_fits_in_postgres() -> None:
    """PostgreSQL truncates an identifier over 63 bytes, and SQLAlchemy hash-suffixes one it expects
    to be truncated — either way the installed name stops matching the declared one, and the tests
    above would be asserting a name that does not exist."""

    table = Base.metadata.tables["outbox_entries"]
    too_long = [
        constraint.name
        for constraint in (*table.constraints, *table.indexes)
        if constraint.name is not None and len(str(constraint.name)) > 63
    ]
    assert too_long == []


def test_the_outbox_is_deliberately_not_append_only() -> None:
    """Almost everything here is `Immutable`, and applying the habit to this table would break it:
    #202's trigger refuses `UPDATE`, and the dispatcher has to stamp `dispatched_at` on the row it
    just delivered. The outbox would be permanently undeliverable, and the symptom would be a
    package stuck forever — the failure this story exists to prevent.
    """

    assert not issubclass(OutboxEntry, Immutable)
    assert "outbox_entries" not in immutable_table_names()
