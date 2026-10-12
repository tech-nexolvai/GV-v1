"""The outbox gives up on a row that can never start, and the worker drops a closed review's run (#1182).

The audit (2026-10-11): a check run asked for after sign-off was refused by the worker on every poll
("a package revision in APPROVED may not move to RUNNING_CHECKS") and retried 116 times in five
minutes, for ever. Two fixes, tested here: the local worker drops such a run once with a recorded
reason, and the dispatcher stops retrying any row refused for good after a capped number of tries.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.lifecycle.states import EntryConditionUnmet, IllegalTransition
from app.models import OutboxEntry, Package, PackageRevision, PackageState, Project
from app.models.runs import TaskRun
from tests.app.postgres_fixture import alembic_config
from workflow.outbox import (
    MAX_REFUSED_ATTEMPTS,
    OutboxDispatchError,
    abandoned_entries,
    dispatch_committed,
    enqueue,
    stuck_entries,
)

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _one_row(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        enqueue(session, workflow="run_checks", payload={"package_revision_id": str(uuid4())})


def _refusing(error: Exception) -> object:
    def start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        raise error

    return start


def _poll(factory: sessionmaker[Session], start: object, times: int, **options: int) -> None:
    for _ in range(times):
        try:
            dispatch_committed(factory, start, **options)  # type: ignore[arg-type]
        except OutboxDispatchError:
            pass


def _row(factory: sessionmaker[Session]) -> OutboxEntry:
    with factory() as session:
        return session.scalars(select(OutboxEntry)).one()


def test_a_row_refused_for_good_is_given_up_after_the_cap_with_its_reason(
    factory: sessionmaker[Session],
) -> None:
    """Outcome: tried exactly `cap` times, then `abandoned_at` and the refusal's words, never
    dispatched, never tried again and no longer reported as stuck."""
    _one_row(factory)
    refusal = IllegalTransition("a package revision in APPROVED may not move to RUNNING_CHECKS")

    _poll(factory, _refusing(refusal), times=3, max_refused_attempts=3)
    given_up = _row(factory)
    _poll(factory, _refusing(refusal), times=2, max_refused_attempts=3)
    after = _row(factory)

    assert given_up.attempts == 3
    assert given_up.abandoned_at is not None and given_up.dispatched_at is None
    assert given_up.abandoned_reason == (
        "given up after 3 refusals: IllegalTransition: a package revision in APPROVED may not "
        "move to RUNNING_CHECKS"
    )
    assert after.attempts == 3
    with factory() as session:
        assert stuck_entries(session, timedelta(microseconds=1)) == []


def test_before_the_cap_the_row_is_still_retried(factory: sessionmaker[Session]) -> None:
    _one_row(factory)

    _poll(factory, _refusing(IllegalTransition("not from here")), times=2, max_refused_attempts=3)

    row = _row(factory)
    assert (row.attempts, row.abandoned_at) == (2, None)


def test_a_refusal_raised_from_inside_another_error_counts_too(
    factory: sessionmaker[Session],
) -> None:
    _one_row(factory)
    try:
        raise IllegalTransition("APPROVED may not move to RUNNING_CHECKS")
    except IllegalTransition as cause:
        wrapped = RuntimeError("the stage failed")
        wrapped.__cause__ = cause

    _poll(factory, _refusing(wrapped), times=1, max_refused_attempts=1)

    assert _row(factory).abandoned_at is not None


@pytest.mark.parametrize(
    "error",
    [EntryConditionUnmet("not yet"), RuntimeError("engine unreachable")],
    ids=["not-yet", "engine-down"],
)
def test_any_other_failure_is_retried_for_ever_as_before(
    factory: sessionmaker[Session], error: Exception
) -> None:
    """A "not yet" or an engine that is down may succeed on a later poll, so it is never given up."""
    _one_row(factory)

    _poll(factory, _refusing(error), times=4, max_refused_attempts=2)

    row = _row(factory)
    assert (row.attempts, row.abandoned_at, row.abandoned_reason) == (4, None, None)


def test_only_refusals_count_towards_giving_up_not_other_failures(
    factory: sessionmaker[Session],
) -> None:
    """Input: 25 engine outages, then refusals. Outcome: the first refusal does not give the row up
    (25 attempts were outages); it is given up on the cap-th refusal, and the reason counts those.
    """
    _one_row(factory)
    refusal = IllegalTransition("APPROVED may not move to RUNNING_CHECKS")

    _poll(factory, _refusing(RuntimeError("engine down")), times=25, max_refused_attempts=3)
    _poll(factory, _refusing(refusal), times=1, max_refused_attempts=3)
    after_one = _row(factory)
    _poll(factory, _refusing(refusal), times=2, max_refused_attempts=3)
    given_up = _row(factory)

    assert (after_one.attempts, after_one.refused_attempts, after_one.abandoned_at) == (26, 1, None)
    assert (given_up.attempts, given_up.refused_attempts) == (28, 3)
    assert given_up.abandoned_at is not None
    assert str(given_up.abandoned_reason).startswith("given up after 3 refusals:")


def test_given_up_rows_are_listed_for_an_operator(factory: sessionmaker[Session]) -> None:
    _one_row(factory)
    _one_row(factory)
    _poll(factory, _refusing(IllegalTransition("never")), times=1, max_refused_attempts=1)

    with factory() as session:
        listed = abandoned_entries(session)

    assert len(listed) == 2
    assert all(row.abandoned_reason for row in listed)


def test_the_default_cap_is_bounded() -> None:
    assert 1 < MAX_REFUSED_ATTEMPTS <= 50


def test_a_useless_cap_is_refused(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ValueError, match="max_refused_attempts"):
        dispatch_committed(factory, _refusing(RuntimeError()), max_refused_attempts=0)  # type: ignore[arg-type]


def test_the_database_refuses_a_row_both_dispatched_and_given_up(
    factory: sessionmaker[Session],
) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.db.base import utc_now

    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add(
            OutboxEntry(
                workflow="run_checks",
                payload={},
                attempts=1,
                dispatched_at=utc_now(),
                abandoned_at=utc_now(),
                abandoned_reason="both",
            )
        )


# ---------------------------------------------------------------------------
# The local worker drops a closed review's queued run, once, with the reason
# ---------------------------------------------------------------------------


def _revision(session: Session, state: PackageState) -> PackageRevision:
    project = Project(name="closed review worker")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic vendor")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=state.value)
    session.add(revision)
    session.commit()
    return revision


@pytest.mark.parametrize(
    "state", [PackageState.APPROVED, PackageState.CHANGES_REQUESTED, PackageState.SUPERSEDED]
)
def test_the_worker_drops_a_check_run_queued_for_a_closed_review(
    factory: sessionmaker[Session], state: PackageState
) -> None:
    """Outcome: the run answers "dropped" with the plain reason instead of raising; one task row
    records it; the revision's state is untouched; a redelivery records nothing more."""
    import scripts.drain_outbox as worker

    key = str(uuid4())
    with factory() as session:
        revision = _revision(session, state)
        answer = worker._run_checks(session, revision.id, key)
        again = worker._run_checks(session, revision.id, key)
        session.commit()
        outcomes = list(
            session.scalars(select(TaskRun.outcome).where(TaskRun.task_type == "run_checks"))
        )
        session.refresh(revision)

        assert answer["ran"] is False and again == answer
        assert str(answer["reason"]).startswith("dropped: ")
        assert outcomes == [f"dropped: {state.value}"]
        assert revision.state == state.value


def test_through_the_outbox_the_closed_reviews_run_is_delivered_once_not_retried(
    factory: sessionmaker[Session],
) -> None:
    """The audit's loop, end to end: a run queued on an approved review, drained by the worker's own
    consumer. Outcome: one attempt, delivered, nothing left to poll."""
    import scripts.drain_outbox as worker

    with factory() as session:
        revision = _revision(session, PackageState.APPROVED)
        enqueue(
            session,
            workflow="run_checks",
            payload={"package_revision_id": str(revision.id), "discriminators": {}},
        )
        session.commit()

    def start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        with factory() as session:
            worker._consume(
                session,
                workflow=workflow,
                payload=payload,
                idempotency_key=idempotency_key,
                timings=None,
            )
            session.commit()

    assert dispatch_committed(factory, start) == 1
    assert dispatch_committed(factory, start) == 0
    row = _row(factory)
    assert (row.attempts, row.dispatched_at is not None, row.abandoned_at) == (1, True, None)
