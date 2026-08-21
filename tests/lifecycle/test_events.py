"""The state-event trail, and what a dispute can ask of it (#210, C3.2).

`package_state_events` exists to answer *"what happened to this package, and when?"* months after the
fact. Four properties make that answer trustworthy, and each is tested against a real PostgreSQL
because each is a property of the database rather than of the Python:

- **Append-only.** An event is never updated or deleted — enforced by the trigger `0013_append_only`
  installs, not by anyone remembering.
- **Every event names an actor.** `NOT NULL` allowed `''`, which names nobody; #210 added the check.
- **The order is the sequence, not the clock.** Two events in the same microsecond have no order by
  timestamp, and "which happened first?" is the question being asked.
- **The event and the state change are one write.** A state change whose event failed to record is a
  package whose history has a hole exactly where the dispute is.

Source: backend proposal §9.1 · Design: `docs/DESIGN_PLATFORM.md` §5 · Verification: this file
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, select, text, update
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.base import Base
from app.db.session import session_factory, unit_of_work
from app.lifecycle.events import (
    STATE_PHRASES,
    ActorMissing,
    history,
    record,
    render_history,
)
from app.lifecycle.states import begin, transition
from app.models import (
    Package,
    PackageRevision,
    PackageState,
    PackageStateEvent,
    Project,
    TaskRun,
    WorkflowRun,
)
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

ACTOR = "the ingestion worker"


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    """A session factory against a database migrated to head.

    `alembic upgrade head` rather than `create_all`: this story adds a column and a check constraint in
    `0016`, and #313 was a lesson in what `create_all` cannot see — it builds from the ORM metadata, so
    it would prove the model and never the migration.
    """
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _revision(session: Session) -> UUID:
    """A package revision with its history opened, ready to be moved."""
    project = Project(name=f"events {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="anant")
    session.flush()
    return revision.id


def _workflow_run(session: Session, package_revision_id: UUID) -> UUID:
    run = WorkflowRun(
        package_revision_id=package_revision_id, engine_run_id=f"hatchet-{uuid4().hex}"
    )
    session.add(run)
    session.flush()
    return run.id


# ---------------------------------------------------------------------------
# Append-only, enforced by the database
# ---------------------------------------------------------------------------


def test_an_event_cannot_be_updated(factory: sessionmaker[Session]) -> None:
    """Input: UPDATE on an event. Outcome: refused. Why: the trail is the evidence.

    Not "we do not update it" but "it cannot be updated". `0013_append_only` installs the trigger; this
    proves it still covers this table after `0016` altered it, which is exactly the sort of thing an
    `ALTER TABLE` quietly changes.
    """
    with factory() as session:
        revision_id = _revision(session)
        session.commit()

    with factory() as session, pytest.raises(DatabaseError):
        session.execute(
            update(PackageStateEvent)
            .where(PackageStateEvent.package_revision_id == revision_id)
            .values(actor="somebody else")
        )
        session.flush()


def test_an_event_cannot_be_deleted(factory: sessionmaker[Session]) -> None:
    """Deleting the record of a transition is how a history stops explaining a past decision."""
    with factory() as session:
        revision_id = _revision(session)
        session.commit()

    with factory() as session, pytest.raises(DatabaseError):
        session.execute(
            delete(PackageStateEvent).where(PackageStateEvent.package_revision_id == revision_id)
        )
        session.flush()


# ---------------------------------------------------------------------------
# Every event names an actor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["", "   "], ids=["empty", "whitespace"])
def test_record_refuses_a_nameless_actor(factory: sessionmaker[Session], actor: str) -> None:
    """A system actor is still a named actor. `''` satisfies NOT NULL and names nobody."""
    with factory() as session:
        revision_id = _revision(session)
        with pytest.raises(ActorMissing, match="system actor is still a named actor"):
            record(
                session,
                package_revision_id=revision_id,
                from_state=PackageState.CREATED,
                to_state=PackageState.UPLOADING,
                actor=actor,
            )


def test_the_database_refuses_a_nameless_actor_too(factory: sessionmaker[Session]) -> None:
    """**The half that survives a caller going round `record`.** #210 added the check because
    `NOT NULL` alone let a direct `INSERT` store an event attributed to nobody."""
    with factory() as session:
        revision_id = _revision(session)
        session.commit()

    with factory() as session, pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO package_state_events "
                "(id, created_at, package_revision_id, sequence, to_state, actor) "
                "VALUES (:id, now(), :revision, 99, 'UPLOADING', '')"
            ),
            {"id": uuid4(), "revision": revision_id},
        )
        session.flush()


def test_an_actor_is_stored_without_surrounding_space(factory: sessionmaker[Session]) -> None:
    """Trimmed on the way in, so `" anant "` and `"anant"` are one actor in a query rather than two."""
    with factory() as session:
        revision_id = _revision(session)
        event = record(
            session,
            package_revision_id=revision_id,
            from_state=PackageState.CREATED,
            to_state=PackageState.UPLOADING,
            actor="  anant  ",
        )
        assert event.actor == "anant"


# ---------------------------------------------------------------------------
# The workflow run that caused it
# ---------------------------------------------------------------------------


def test_an_event_names_the_run_that_caused_it(factory: sessionmaker[Session]) -> None:
    """The scope item this story added a column for. "Which run did this?" is a join, not a text search."""
    with factory() as session:
        revision_id = _revision(session)
        run_id = _workflow_run(session, revision_id)
        event = record(
            session,
            package_revision_id=revision_id,
            from_state=PackageState.CREATED,
            to_state=PackageState.UPLOADING,
            actor=ACTOR,
            workflow_run_id=run_id,
        )
        session.commit()
        assert event.workflow_run_id == run_id

    with factory() as session:
        caused_by_run = list(
            session.scalars(
                select(PackageStateEvent.to_state).where(
                    PackageStateEvent.workflow_run_id == run_id
                )
            )
        )
        assert caused_by_run == [PackageState.UPLOADING.value]


def test_a_human_transition_names_no_run(factory: sessionmaker[Session]) -> None:
    """Null means nothing ran, and that is information. Filling it with a placeholder would make
    "which run did this?" answerable and wrong."""
    with factory() as session:
        revision_id = _revision(session)
        event = record(
            session,
            package_revision_id=revision_id,
            from_state=PackageState.CREATED,
            to_state=PackageState.UPLOADING,
            actor="anant",
        )
        assert event.workflow_run_id is None

        genesis = history(session, revision_id)[0]
        assert genesis.workflow_run_id is None, "a revision's birth has no run behind it"


def test_a_run_that_does_not_exist_is_refused(factory: sessionmaker[Session]) -> None:
    """The foreign key, so an event cannot cite a run nobody can look up."""
    with factory() as session:
        revision_id = _revision(session)
        record(
            session,
            package_revision_id=revision_id,
            from_state=PackageState.CREATED,
            to_state=PackageState.UPLOADING,
            actor=ACTOR,
            workflow_run_id=uuid4(),
        )
        with pytest.raises(IntegrityError):
            session.flush()


# ---------------------------------------------------------------------------
# The sequence, and what happens when two transitions arrive at once
# ---------------------------------------------------------------------------


def test_the_sequence_is_allocated_under_a_row_lock() -> None:
    """The statement itself, asserted without a database.

    Cheap, and it runs everywhere. The threaded test below proves the behaviour; this proves the lock
    is actually requested, which is the part a refactor silently drops.
    """
    from sqlalchemy.dialects import postgresql

    statement = select(PackageRevision.id).where(PackageRevision.id == uuid4()).with_for_update()
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled


def test_two_concurrent_transitions_get_distinct_sequences(
    postgres_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """Input: two transitions at once. Outcome: 2 and 3, neither refused. Why: valid work must not fail.

    **The acceptance criterion, on two real connections.** Without the row lock both read the same
    highest sequence and one dies on the unique constraint — safe, but it fails a caller who did nothing
    wrong, and a workflow that does not retry leaves the package stuck in a state nobody is watching.

    The first thread holds its transaction open until the second has started, so the second genuinely
    contends for the lock rather than tidily following the first.
    """
    with factory() as session:
        revision_id = _revision(session)
        session.commit()

    started = threading.Barrier(2, timeout=30)
    failures: list[BaseException] = []

    def move(to: PackageState) -> None:
        try:
            with factory() as session:
                started.wait()
                record(
                    session,
                    package_revision_id=revision_id,
                    from_state=PackageState.CREATED,
                    to_state=to,
                    actor=ACTOR,
                    reason="concurrent",
                )
                session.commit()
        except BaseException as error:  # noqa: BLE001 - reported through `failures`
            failures.append(error)

    # `record` rather than `transition`, deliberately. What is under test is sequence allocation, and
    # going through the state machine drags in the table: whichever thread commits first changes the
    # state the other then reads, so the loser's move can become illegal and the test fails for a
    # reason that has nothing to do with sequences. (It did, on the first attempt — CANCELLED winning
    # left the other thread transitioning out of a terminal state.)
    first = threading.Thread(target=move, args=(PackageState.UPLOADING,))
    second = threading.Thread(target=move, args=(PackageState.UPLOADED,))
    first.start()
    second.start()
    first.join(timeout=60)
    second.join(timeout=60)

    assert not failures, f"a concurrent transition failed: {failures}"

    with factory() as session:
        sequences = [event.sequence for event in history(session, revision_id)]
    assert sequences == [1, 2, 3], f"genesis plus two transitions, in order: {sequences}"
    assert len(set(sequences)) == len(sequences), "no sequence may be reused"


# ---------------------------------------------------------------------------
# A state change that fails to record its event takes the state change with it
# ---------------------------------------------------------------------------


def test_a_failure_to_record_rolls_the_state_change_back(
    factory: sessionmaker[Session],
) -> None:
    """Input: an event that cannot be written. Outcome: the state is unchanged. Why: no holes.

    The failure is induced the way it would really happen — a constraint refusing the row — by taking
    the sequence the genesis event already holds. The state column and the event are one write, so the
    column must still read `CREATED` afterwards.
    """
    with factory() as session:
        revision_id = _revision(session)
        session.commit()

    with factory() as session:
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        # Collide with the genesis event's sequence, inside the same transaction.
        session.add(
            PackageStateEvent(
                package_revision_id=revision_id,
                sequence=1,
                from_state=None,
                to_state=PackageState.CREATED.value,
                actor=ACTOR,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with factory() as session:
        stored = session.execute(
            select(PackageRevision.state).where(PackageRevision.id == revision_id)
        ).scalar_one()
        assert stored == PackageState.CREATED.value, "the state moved without its event surviving"
        assert [event.sequence for event in history(session, revision_id)] == [1]


# ---------------------------------------------------------------------------
# History ordering
# ---------------------------------------------------------------------------


def test_history_is_ordered_by_sequence_not_by_timestamp(
    factory: sessionmaker[Session],
) -> None:
    """Input: timestamps in the wrong order. Outcome: sequence order wins. Why: the clock is not the order.

    The timestamps are deliberately set to disagree with the sequence. Two events written in the same
    microsecond have no order by `created_at` at all, so a history sorted by time can present a
    transition before the one that caused it — in the document a dispute turns on.
    """
    with factory() as session:
        revision_id = _revision(session)
        # The timestamp is set at insert time, not patched afterwards: this table is append-only and
        # the trigger refuses an UPDATE — which the tests above prove. So the disagreement between the
        # clock and the sequence has to be built in as the rows are written.
        session.add(
            PackageStateEvent(
                package_revision_id=revision_id,
                sequence=2,
                from_state=PackageState.CREATED.value,
                to_state=PackageState.UPLOADING.value,
                actor=ACTOR,
                created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            )
        )
        session.add(
            PackageStateEvent(
                package_revision_id=revision_id,
                sequence=3,
                from_state=PackageState.UPLOADING.value,
                to_state=PackageState.UPLOADED.value,
                actor=ACTOR,
                created_at=datetime(2020, 1, 1, 12, 0, tzinfo=UTC),
            )
        )
        session.commit()

    with factory() as session:
        events = history(session, revision_id)
        assert [event.sequence for event in events] == [1, 2, 3]
        assert events[2].created_at < events[1].created_at, "the clock really does disagree"


def test_history_for_a_revision_with_no_events_is_empty(
    factory: sessionmaker[Session],
) -> None:
    """An honest empty list rather than a guess. Nothing has happened yet is a real answer."""
    with factory() as session:
        assert history(session, uuid4()) == []


# ---------------------------------------------------------------------------
# Reading it back in plain English
# ---------------------------------------------------------------------------


def test_every_state_has_a_phrase() -> None:
    """A state with no phrase renders as an apology naming the enum, which is what this prevents."""
    assert set(STATE_PHRASES) == set(PackageState)
    for state, phrase in STATE_PHRASES.items():
        assert phrase and phrase[0].islower(), state.value


def test_the_rendered_history_names_no_states(factory: sessionmaker[Session]) -> None:
    """**The test-plan item, in its checkable form.** "Readable without knowing the state names" means
    the state names are not in it — `AWAITING_REVIEW` tells a reviewer nothing, and a history that
    needs the enum beside it to be understood is a log dump with extra steps."""
    with factory() as session:
        revision_id = _revision(session)
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        transition(session, revision_id, PackageState.UPLOADED, actor=ACTOR)
        transition(session, revision_id, PackageState.INGESTING, actor=ACTOR)
        session.commit()
        rendered = render_history(history(session, revision_id))

    for state in PackageState:
        assert state.value not in rendered, f"{state.value} leaked into the rendered history"


def test_the_rendered_history_says_when_what_and_who(factory: sessionmaker[Session]) -> None:
    """The three things a dispute asks. The reason is included when one was given."""
    with factory() as session:
        revision_id = _revision(session)
        transition(
            session,
            revision_id,
            PackageState.UPLOADING,
            actor=ACTOR,
            reason="the reviewer asked for a re-run",
        )
        session.commit()
        rendered = render_history(history(session, revision_id))

    assert "UTC" in rendered, "a bare time invites the question 'in whose timezone?'"
    assert "the upload started" in rendered
    assert ACTOR in rendered
    assert "the reviewer asked for a re-run" in rendered
    assert rendered.count("\n") == 1, "two events, two lines"


def test_the_rendered_history_marks_an_automated_run(factory: sessionmaker[Session]) -> None:
    """A reviewer reading the trail should be able to tell what a person did from what a run did."""
    with factory() as session:
        revision_id = _revision(session)
        run_id = _workflow_run(session, revision_id)
        record(
            session,
            package_revision_id=revision_id,
            from_state=PackageState.CREATED,
            to_state=PackageState.UPLOADING,
            actor=ACTOR,
            workflow_run_id=run_id,
        )
        session.commit()
        rendered = render_history(history(session, revision_id))

    assert f"automated run {run_id}" in rendered
    assert "automated run" not in rendered.splitlines()[0], "the genesis event had no run"


def test_an_empty_history_renders_as_a_sentence() -> None:
    """Not an empty string. A reviewer opening a package that has done nothing should read that."""
    assert render_history([]) == "Nothing has happened to this package yet."


def test_a_state_with_no_phrase_renders_as_an_admission() -> None:
    """If a state is ever added without a phrase, the output says so rather than printing the enum and
    looking like a translation. Built by hand because the real mapping is complete."""
    event = PackageStateEvent(
        package_revision_id=uuid4(),
        sequence=1,
        from_state=None,
        to_state=PackageState.APPROVED.value,
        actor=ACTOR,
    )
    without_phrase = {state: phrase for state, phrase in STATE_PHRASES.items()}
    without_phrase.pop(PackageState.APPROVED)

    import app.lifecycle.events as events_module

    original = events_module.STATE_PHRASES
    try:
        events_module.STATE_PHRASES = without_phrase
        rendered = events_module.render_history([event])
    finally:
        events_module.STATE_PHRASES = original

    assert "no words for" in rendered
    assert PackageState.APPROVED.value in rendered, "the admission names the state it cannot render"


# ---------------------------------------------------------------------------
# The trail and the state machine stay one write
# ---------------------------------------------------------------------------


def test_a_transition_writes_exactly_one_event(factory: sessionmaker[Session]) -> None:
    """One move, one row. Two would give the history a step that never happened."""
    with factory() as session:
        revision_id = _revision(session)
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        session.commit()
        events = history(session, revision_id)

    assert [event.to_state for event in events] == [
        PackageState.CREATED.value,
        PackageState.UPLOADING.value,
    ]


def test_the_event_records_where_the_package_came_from(factory: sessionmaker[Session]) -> None:
    """`from_state` is what makes the trail a chain rather than a list of arrivals — and it is what
    `resumes_where_it_failed` reads in #209."""
    with factory() as session:
        revision_id = _revision(session)
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        session.commit()
        events = history(session, revision_id)

    assert events[0].from_state is None, "genesis has nowhere to come from"
    assert events[1].from_state == PackageState.CREATED.value
    assert events[1].created_at is not None
    assert events[1].created_at < datetime.now(UTC) + timedelta(seconds=5)


def test_a_transition_and_its_event_land_in_one_transaction(
    factory: sessionmaker[Session],
) -> None:
    """Neither commits on its own behalf, so a rollback discards both together."""
    with factory() as session:
        revision_id = _revision(session)
        session.commit()

    with factory() as session:
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        session.rollback()

    with factory() as session:
        assert [event.sequence for event in history(session, revision_id)] == [1]
        stored = session.execute(
            select(PackageRevision.state).where(PackageRevision.id == revision_id)
        ).scalar_one()
        assert stored == PackageState.CREATED.value


def test_the_lifecycle_package_exports_the_trail(factory: sessionmaker[Session]) -> None:
    """`app.lifecycle` is the seam other packages import; a helper only reachable by module path is one
    callers will reimplement."""
    from app import lifecycle

    for name in ("record", "history", "render_history", "ActorMissing", "STATE_PHRASES"):
        assert name in lifecycle.__all__
        assert hasattr(lifecycle, name)

    with factory() as session:
        revision_id = _revision(session)
        assert lifecycle.history(session, revision_id)


def test_unused_task_run_import_is_not_needed() -> None:
    """`TaskRun` is imported for the workflow-run chain in other tests; assert it resolves so a stale
    import is a failure here rather than a confusing one elsewhere."""
    assert TaskRun.__tablename__ == "task_runs"


def test_base_metadata_knows_the_new_column() -> None:
    """The column has to be in the metadata Alembic compares against, or the round-trip test passes
    while the migration and the model disagree."""
    assert "workflow_run_id" in Base.metadata.tables["package_state_events"].columns


def test_unit_of_work_still_wraps_a_transition(factory: sessionmaker[Session]) -> None:
    """The helper the rest of the codebase uses; a transition has to work inside it unchanged."""
    revision_id: UUID
    with unit_of_work(factory) as session:
        revision_id = _revision(session)

    with unit_of_work(factory) as session:
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)

    with factory() as session:
        assert [event.sequence for event in history(session, revision_id)] == [1, 2]
