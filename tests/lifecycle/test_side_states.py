"""Side states: a package that stopped says why, and never looks reviewed (#212, C3.4).

Two of this story's four acceptance criteria were already true when it started — #209 made `CANCELLED`
terminal and approval unreachable from every side state. Those are asserted here rather than
re-implemented, and the tests say which mechanism holds them, because a test that restates a property
without naming its source reads as the reason it holds.

The two that needed building:

- **Retryable or permanent by table, not by guess.** Nothing decided it before, so every call site
  would have decided for itself.
- **`NEEDS_INPUT` names what input.** A package waiting for an answer nobody can identify has stopped
  silently while wearing a state name.

Source: backend proposal §9.1, §9.4 · Design: `docs/DESIGN_PLATFORM.md` §5, §6.3 · Verification: this file
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, select
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.lifecycle.events import history, render_history
from app.lifecycle.side_states import (
    FAILURE_CLASSIFICATION,
    SIDE_STATE_DESCRIPTIONS,
    FailureClass,
    InputNotNamed,
    cancel,
    classify,
    enter_failure,
    enter_needs_input,
    is_a_side_state,
)
from app.lifecycle.states import (
    PROCESSING_STATES,
    SIDE_STATES,
    TRANSITIONS,
    IllegalTransition,
    begin,
    transition,
)
from app.models import Package, PackageRevision, PackageState, Project
from storage.hashing import ArtifactCorrupt
from storage.store import ArtifactConflict
from workflow.outbox import OutboxDispatchError

pytest_plugins = ("tests.app.postgres_fixture",)

ACTOR = "the ingestion worker"


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _revision(session: Session, state: PackageState) -> UUID:
    """A revision sitting in `state`, history opened."""
    project = Project(name=f"side {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor=ACTOR)
    if state is not PackageState.CREATED:
        session.execute(
            PackageRevision.__table__.update()
            .where(PackageRevision.id == revision.id)
            .values(state=state.value)
        )
    session.flush()
    return revision.id


def _state(session: Session, revision_id: UUID) -> str:
    return session.execute(
        select(PackageRevision.state).where(PackageRevision.id == revision_id)
    ).scalar_one()


# ---------------------------------------------------------------------------
# Classification is a table, not a guess
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("slow"), FailureClass.RETRYABLE),
        (ConnectionError("dropped"), FailureClass.RETRYABLE),
        (OutboxDispatchError(0, ((uuid4(), RuntimeError("nope")),)), FailureClass.RETRYABLE),
        (ArtifactCorrupt("bad bytes"), FailureClass.PERMANENT),
        (ArtifactConflict("already there"), FailureClass.PERMANENT),
        (ValueError("nonsense"), FailureClass.PERMANENT),
    ],
    ids=["timeout", "connection", "dispatch", "corrupt", "conflict", "value"],
)
def test_the_table_decides(error: BaseException, expected: FailureClass) -> None:
    """Input: an exception. Outcome: its class. Why: one answer, not one per call site.

    Two callers deciding differently about the same error is how a package gets retried for ever in one
    code path and abandoned in another.
    """
    assert classify(error) is expected


def test_an_unclassified_failure_is_permanent() -> None:
    """**Anant's call, and the direction that fails safely.**

    A permanent failure is a visible outcome somebody investigates (§6.3: *"exhaustion is a visible
    outcome"*). A wrongly retryable one is a loop that spends paid model calls on work that can never
    succeed and reports nothing — the harder failure to notice, so it is the one to design against.
    """

    class SomethingNobodyClassified(Exception):
        pass

    assert classify(SomethingNobodyClassified()) is FailureClass.PERMANENT


def test_a_subclass_inherits_its_parents_class() -> None:
    """The MRO walk. Adding one entry must not silently unclassify its children — and this is what
    makes listing both `OperationalError` and `DBAPIError` safe rather than contradictory."""

    class SlowerThanUsual(TimeoutError):
        pass

    assert classify(SlowerThanUsual()) is FailureClass.RETRYABLE
    assert classify(OperationalError("stmt", None, Exception())) is FailureClass.RETRYABLE


@pytest.mark.parametrize(
    "kind", [IntegrityError, DataError, ProgrammingError], ids=lambda k: k.__name__
)
def test_a_database_error_a_retry_cannot_fix_is_permanent(kind: type[Exception]) -> None:
    """**Found by CodeRabbit on #378, and it was the bug the permanent default exists to prevent.**

    All three inherit from `DBAPIError`, which was in the table as retryable — so a unique-constraint
    violation, an invalid value and a malformed statement all classified as retryable and would have
    retried for ever. One broad entry meant to be helpful undid the whole point of the default.

    `DBAPIError` is deliberately not in the table now; these three are named instead.
    """
    error = kind("INSERT INTO packages VALUES (%(v)s)", {"v": "x"}, Exception("dup key"))
    assert classify(error) is FailureClass.PERMANENT


def test_classification_reads_no_message() -> None:
    """The test-plan item. Two errors of one type classify the same however differently they are
    worded, so nothing here depends on the text of a message somebody may reword."""
    assert classify(ArtifactCorrupt("a")) is classify(ArtifactCorrupt("totally different wording"))


def test_extraction_exceptions_are_deliberately_absent() -> None:
    """**The import boundary, asserted so nobody 'fixes' it by adding them.**

    `app/api/packages.py` imports `app.lifecycle`, and #208's guard forbids `app/api/` from reaching
    `extraction/`. Naming `NovaTimeoutError` here would fail C2.6 — so `extraction/` maps its own
    exceptions and passes the class in, which is where that knowledge belongs anyway.
    """
    named = {kind.__module__ for kind in FAILURE_CLASSIFICATION}
    assert not [
        module for module in named if module.startswith(("extraction", "retrieval", "reports"))
    ]


# ---------------------------------------------------------------------------
# Entering a failure
# ---------------------------------------------------------------------------


def test_a_retryable_error_lands_in_failed_retryable(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        enter_failure(session, revision_id, actor=ACTOR, error=TimeoutError("the model was slow"))
        session.flush()
        assert _state(session, revision_id) == PackageState.FAILED_RETRYABLE.value


def test_a_permanent_error_lands_in_failed_permanent(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        enter_failure(session, revision_id, actor=ACTOR, error=ArtifactCorrupt("bad bytes"))
        session.flush()
        assert _state(session, revision_id) == PackageState.FAILED_PERMANENT.value


def test_a_caller_may_pass_the_class_itself(factory: sessionmaker[Session]) -> None:
    """The path `extraction/` uses, since its exceptions cannot be named in the table."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        enter_failure(
            session,
            revision_id,
            actor=ACTOR,
            failure_class=FailureClass.RETRYABLE,
            reason="the model timed out (classified by extraction/)",
        )
        session.flush()
        assert _state(session, revision_id) == PackageState.FAILED_RETRYABLE.value


def test_passing_both_or_neither_is_refused(factory: sessionmaker[Session]) -> None:
    """Both could disagree about whether to retry, which is the decision the table exists to make in
    one place. Neither leaves nothing to decide from."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        with pytest.raises(ValueError, match="not both and not neither"):
            enter_failure(
                session,
                revision_id,
                actor=ACTOR,
                error=TimeoutError("x"),
                failure_class=FailureClass.PERMANENT,
            )
        with pytest.raises(ValueError, match="not both and not neither"):
            enter_failure(session, revision_id, actor=ACTOR)


def test_the_reason_says_what_happened_and_how_it_was_classified(
    factory: sessionmaker[Session],
) -> None:
    """ "Failed" with neither is a state a reviewer can see and not act on."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.MATCHING)
        enter_failure(
            session, revision_id, actor=ACTOR, error=ArtifactCorrupt("page 4 is unreadable")
        )
        session.flush()

        reason = history(session, revision_id)[-1].reason or ""
        assert "a retry cannot fix" in reason
        assert "page 4 is unreadable" in reason
        assert "ArtifactCorrupt" in reason


def test_a_state_that_cannot_fail_refuses(factory: sessionmaker[Session]) -> None:
    """Only our own work can fail (#209). `CREATED` has nothing of ours running, so there is no failure
    of ours to record."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.CREATED)
        with pytest.raises(IllegalTransition):
            enter_failure(session, revision_id, actor=ACTOR, error=TimeoutError("x"))


# ---------------------------------------------------------------------------
# NEEDS_INPUT names the input
# ---------------------------------------------------------------------------


def test_needs_input_records_what_is_needed(factory: sessionmaker[Session]) -> None:
    """**The acceptance criterion.** In plain English a reviewer can act on, in the trail they read."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.VALIDATING_EVIDENCE)
        enter_needs_input(
            session,
            revision_id,
            actor=ACTOR,
            needed="the cabinet schedule for wall B is missing",
        )
        session.flush()

        assert _state(session, revision_id) == PackageState.NEEDS_INPUT.value
        reason = history(session, revision_id)[-1].reason or ""
        assert "the cabinet schedule for wall B is missing" in reason


def test_the_needed_input_appears_in_the_rendered_history(factory: sessionmaker[Session]) -> None:
    """It goes in the state event's reason precisely so it reaches the reviewer — `render_history`
    already prints reasons, so nothing new has to be built to show it."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.VALIDATING_EVIDENCE)
        enter_needs_input(session, revision_id, actor=ACTOR, needed="wall B's cabinet schedule")
        session.flush()
        rendered = render_history(history(session, revision_id))

    assert "wall B's cabinet schedule" in rendered
    assert "NEEDS_INPUT" not in rendered, "the state name is not what a reviewer reads"


@pytest.mark.parametrize("needed", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_needs_input_without_naming_the_input_is_refused(
    factory: sessionmaker[Session], needed: str
) -> None:
    """A package waiting for an unnamed answer cannot be acted on, and cannot be told apart from one
    that is simply stuck. Silence reading as progress."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.VALIDATING_EVIDENCE)
        with pytest.raises(InputNotNamed, match="what input is needed"):
            enter_needs_input(session, revision_id, actor=ACTOR, needed=needed)


def test_the_needed_input_is_stored_without_surrounding_space(
    factory: sessionmaker[Session],
) -> None:
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.RUNNING_CHECKS)
        enter_needs_input(session, revision_id, actor=ACTOR, needed="   a tolerance for CT-014   ")
        session.flush()
        reason = history(session, revision_id)[-1].reason or ""
        assert reason.endswith("a tolerance for CT-014")


# ---------------------------------------------------------------------------
# Cancellation is terminal
# ---------------------------------------------------------------------------


def test_a_cancelled_package_cannot_silently_resume(factory: sessionmaker[Session]) -> None:
    """**The acceptance criterion, held by #209's table rather than by this module.**

    `CANCELLED` has no outgoing edge at all, so resuming is not a state change anybody can make. It
    takes a new revision, which leaves the cancellation visible instead of replacing it.
    """
    assert TRANSITIONS[PackageState.CANCELLED] == frozenset()

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        cancel(session, revision_id, actor="anant", reason="the client withdrew the package")
        session.flush()

        for target in (
            PackageState.EXTRACTING,
            PackageState.AWAITING_REVIEW,
            PackageState.APPROVED,
        ):
            with pytest.raises(IllegalTransition):
                transition(session, revision_id, target, actor=ACTOR)


def test_cancelling_without_a_reason_is_refused(factory: sessionmaker[Session]) -> None:
    """Somebody decided to stop this package, and the decision is part of the record. "Cancelled" on
    its own tells the next reader nothing."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        with pytest.raises(ValueError, match="requires a reason"):
            cancel(session, revision_id, actor="anant", reason="  ")


def test_a_review_outcome_cannot_be_cancelled(factory: sessionmaker[Session]) -> None:
    """A signed-off approval is a decision that was taken; cancelling it afterwards would rewrite it.
    #209's table has `APPROVED` leaving only by being superseded."""
    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.APPROVED)
        with pytest.raises(IllegalTransition):
            cancel(session, revision_id, actor="anant", reason="changed our minds")


# ---------------------------------------------------------------------------
# No side state ever looks reviewed
# ---------------------------------------------------------------------------


def test_no_side_state_can_reach_approved() -> None:
    """**The acceptance criterion.** Approval is the signature at the end of the process, not a way of
    closing a failure. Held by #209's table; asserted here because this is the story that claims it.
    """
    for state in SIDE_STATES:
        assert PackageState.APPROVED not in TRANSITIONS[state], state.value


def test_every_side_state_is_described_in_plain_english() -> None:
    """A reviewer reading a stopped package should not have to look a state name up."""
    assert set(SIDE_STATE_DESCRIPTIONS) == set(SIDE_STATES) | {PackageState.SUPERSEDED}
    for state, description in SIDE_STATE_DESCRIPTIONS.items():
        assert description and description[0].islower(), state.value
        assert state.value not in description, "a description that names the state explains nothing"


def test_the_side_states_are_the_ones_the_design_names() -> None:
    """§5 lists five. If this and the design disagree, one is wrong and a stuck package should not be
    how that is discovered."""
    assert {state.value for state in SIDE_STATE_DESCRIPTIONS} == {
        "FAILED_RETRYABLE",
        "FAILED_PERMANENT",
        "NEEDS_INPUT",
        "CANCELLED",
        "SUPERSEDED",
    }


@pytest.mark.parametrize("state", sorted(PackageState), ids=lambda s: s.value)
def test_is_a_side_state_agrees_with_the_table(state: PackageState) -> None:
    """One predicate, so a caller asking "has this stopped?" does not reimplement the set."""
    expected = state in set(SIDE_STATES) | {PackageState.SUPERSEDED}
    assert is_a_side_state(state) is expected
    if is_a_side_state(state):
        assert state not in PROCESSING_STATES


def test_a_failure_does_not_commit(factory: sessionmaker[Session]) -> None:
    """The caller owns the transaction, as everywhere else in `app/lifecycle/`."""
    revision_id: UUID
    with factory() as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        session.commit()

    with factory() as session:
        enter_failure(session, revision_id, actor=ACTOR, error=TimeoutError("x"))
        session.rollback()

    with factory() as session:
        assert _state(session, revision_id) == PackageState.EXTRACTING.value
