"""The package state machine: what it refuses, and that nothing can go round it (#209, C3.1).

`docs/DESIGN_PLATFORM.md` §5 says the value is in what is *not* allowed, so that is what these tests
are about. Two properties carry real weight:

- **A package cannot reach `AWAITING_REVIEW` without checks having run.** `AGENTS.md` §2.2: silence
  must never read as completion. A reviewer looking at a package whose checks never ran sees no
  failures and concludes there are none.
- **A package cannot be approved out of a side state.** Approval is the signature at the end of the
  process, not a way of closing a failure.

The illegal transitions are tested as the **full cross-product** rather than a sample. A state machine
tested by example is one where the interesting edge — the one somebody adds later without meaning to —
is the edge nobody wrote a case for.

The guard at the bottom is the one that makes the rest worth anything: it walks every module under
`app/` and `workflow/` and fails if any of them outside `app/lifecycle/` assigns to the state column or
constructs a `PackageStateEvent`. Without it, this module is a suggestion.

Source: backend proposal §9.1 · Design: `docs/DESIGN_PLATFORM.md` §5 · Verification: this file
"""

from __future__ import annotations

import ast
from itertools import pairwise
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.session import session_factory, unit_of_work
from app.lifecycle.states import (
    ENTRY_CONDITIONS,
    FAILURE_STATES,
    PROCESSING_STATES,
    RESUMABLE_STATES,
    REVIEW_OUTCOMES,
    SIDE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    EntryConditionUnmet,
    IllegalTransition,
    UnknownRevision,
    begin,
    render_transition_table,
    transition,
)
from app.models import (
    CheckRun,
    Package,
    PackageRevision,
    PackageState,
    PackageStateEvent,
    Project,
    RuleDefinition,
    RuleSnapshot,
)

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTOR = "anant"

#: Where a state or a state event may be written. Everything else is audited against it.
LIFECYCLE_PACKAGE = "app/lifecycle"


# ---------------------------------------------------------------------------
# The table, with no database in sight
# ---------------------------------------------------------------------------


def test_every_state_appears_in_the_table() -> None:
    """A state missing from the table has no rules at all, and would read as terminal by accident."""
    assert set(TRANSITIONS) == set(PackageState)


def test_the_happy_path_is_the_one_the_design_draws() -> None:
    """§5's diagram, asserted edge by edge. If this file and the design disagree, one of them is wrong
    and it should not be discovered by a package getting stuck."""
    line = [
        PackageState.CREATED,
        PackageState.UPLOADING,
        PackageState.UPLOADED,
        PackageState.INGESTING,
        PackageState.EXTRACTING,
        PackageState.MATCHING,
        PackageState.VALIDATING_EVIDENCE,
        PackageState.RUNNING_CHECKS,
        PackageState.GENERATING_OUTPUTS,
        PackageState.AWAITING_REVIEW,
    ]
    for current, following in pairwise(line):
        assert following in TRANSITIONS[current], f"{current.value} -> {following.value} is missing"

    assert PackageState.APPROVED in TRANSITIONS[PackageState.AWAITING_REVIEW]
    assert PackageState.CHANGES_REQUESTED in TRANSITIONS[PackageState.AWAITING_REVIEW]


def test_no_state_can_skip_a_step_on_the_happy_path() -> None:
    """The forward edge is to the *next* state and nothing further along.

    A machine that let `EXTRACTING` reach `RUNNING_CHECKS` would run checks against evidence nothing
    matched or validated, and every downstream record would look perfectly normal.
    """
    line = [
        PackageState.CREATED,
        PackageState.UPLOADING,
        PackageState.UPLOADED,
        PackageState.INGESTING,
        PackageState.EXTRACTING,
        PackageState.MATCHING,
        PackageState.VALIDATING_EVIDENCE,
        PackageState.RUNNING_CHECKS,
        PackageState.GENERATING_OUTPUTS,
        PackageState.AWAITING_REVIEW,
    ]
    for index, current in enumerate(line):
        for later in line[index + 2 :]:
            assert (
                later not in TRANSITIONS[current]
            ), f"{current.value} can skip straight to {later.value}"


def test_terminal_states_lead_nowhere() -> None:
    """A superseded revision is the record a later one replaced; reopening it rewrites what was
    already reported."""
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset(), state.value


def test_a_review_outcome_leaves_only_by_being_superseded() -> None:
    """`APPROVED` and `CHANGES_REQUESTED` are decisions that were taken. Cancelling one afterwards
    would rewrite a completed review, so supersession is the only exit."""
    for state in REVIEW_OUTCOMES:
        assert TRANSITIONS[state] == frozenset({PackageState.SUPERSEDED}), state.value


def test_approval_is_unreachable_from_every_side_state() -> None:
    """**The acceptance criterion.** Approval is the signature at the end of the process, not a way of
    closing a failure or a cancellation."""
    for state in SIDE_STATES:
        assert PackageState.APPROVED not in TRANSITIONS[state], f"{state.value} can reach APPROVED"


def test_only_our_own_work_can_fail() -> None:
    """`CREATED` and `UPLOADING` cannot fail: nothing of ours is running, so there is no failure of
    ours to record. They can still be cancelled or superseded."""
    for state in PackageState:
        can_fail = bool(TRANSITIONS[state] & FAILURE_STATES)
        assert can_fail == (state in PROCESSING_STATES), state.value


def test_a_permanent_failure_does_not_resume() -> None:
    """That is what permanent means. Recovery is a new revision."""
    assert not TRANSITIONS[PackageState.FAILED_PERMANENT] & PROCESSING_STATES
    assert PackageState.FAILED_PERMANENT not in RESUMABLE_STATES


def test_supersede_reaches_every_state_that_is_not_already_final() -> None:
    """§5: a new document revision supersedes the prior package revision, and does not ask what state
    it had reached — `APPROVED` included."""
    for state in PackageState:
        if state in TERMINAL_STATES:
            continue
        assert PackageState.SUPERSEDED in TRANSITIONS[state], state.value


def test_the_table_renders_for_review() -> None:
    """The criterion asks for data that can be rendered and reviewed. A machine somebody has to
    reconstruct from `if` statements is one nobody checks against the design."""
    rendered = render_transition_table()

    assert rendered.startswith("| State | May move to | Notes |")
    for state in PackageState:
        assert f"`{state.value}`" in rendered
    assert "terminal" in rendered
    assert "entry: checks_have_run" in rendered


# ---------------------------------------------------------------------------
# Setting up a revision in a chosen state
# ---------------------------------------------------------------------------


def _revision(session: Session, state: PackageState) -> UUID:
    """A package revision sitting in `state`, with its history opened.

    Writes the column directly, which production code may not do — that is the whole point of the
    guard at the bottom of this file, and it audits `app/` and `workflow/`, not the tests. A test that
    could only reach a state through the machine could not test the machine.
    """
    project = Project(name=f"lifecycle {uuid4()}")
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
            update(PackageRevision)
            .where(PackageRevision.id == revision.id)
            .values(state=state.value)
        )
    session.flush()
    return revision.id


def _record_a_check(session: Session, package_revision_id: UUID) -> None:
    """One `CheckRun` for the revision, which is what `checks_have_run` reads."""
    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:6]}")
    session.add(definition)
    session.flush()
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{uuid4().hex}",
        version="1.0.0",
        canonical_json="{}",
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    session.add(
        CheckRun(
            package_revision_id=package_revision_id,
            rule_snapshot_id=snapshot.id,
            engine_version="1.0.0",
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Illegal transitions: the full cross-product, not a sample
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current", list(PackageState), ids=lambda state: state.value)
def test_every_transition_the_table_forbids_raises(
    postgres_engine: Engine, current: PackageState
) -> None:
    """Input: every state not in the table's row. Outcome: IllegalTransition. Why: no fallback.

    All 17 targets attempted from all 17 states — 289 pairs across this parametrisation. A state
    machine tested by example is one where the edge somebody adds later by accident is the edge nobody
    wrote a case for.
    """
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    forbidden = [state for state in PackageState if state not in TRANSITIONS[current]]

    for target in forbidden:
        with unit_of_work(factory) as session:
            revision_id = _revision(session, current)
            with pytest.raises(IllegalTransition) as refusal:
                transition(session, revision_id, target, actor=ACTOR)
            message = str(refusal.value)
            assert current.value in message and target.value in message, message


@pytest.mark.parametrize("current", list(PackageState), ids=lambda state: state.value)
def test_every_transition_the_table_allows_is_taken(
    postgres_engine: Engine, current: PackageState
) -> None:
    """The table has to be able to say yes, or the refusals above prove nothing.

    Entry conditions are satisfied first where the target has them, so what is under test is the edge
    rather than the condition — those get their own tests below.
    """
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    for target in sorted(TRANSITIONS[current]):
        with unit_of_work(factory) as session:
            revision_id = _revision(session, current)
            if target is PackageState.AWAITING_REVIEW:
                _record_a_check(session, revision_id)
            if target in PROCESSING_STATES and current in RESUMABLE_STATES:
                # Make the history say this is where it failed, which is what the condition reads.
                session.add(
                    PackageStateEvent(
                        package_revision_id=revision_id,
                        sequence=99,
                        from_state=target.value,
                        to_state=current.value,
                        actor=ACTOR,
                        reason="failed here",
                    )
                )
                session.flush()

            event = transition(session, revision_id, target, actor=ACTOR, reason="onward")
            session.flush()

            assert event.from_state == current.value
            assert event.to_state == target.value
            stored = session.execute(
                select(PackageRevision.state).where(PackageRevision.id == revision_id)
            ).scalar_one()
            assert stored == target.value, "the column must agree with the event"


# ---------------------------------------------------------------------------
# The two properties that carry the weight
# ---------------------------------------------------------------------------


def test_awaiting_review_is_unreachable_without_checks(postgres_engine: Engine) -> None:
    """**The property this module exists for.** A reviewer seeing a package whose checks never ran sees
    no failures and concludes there are none — `AGENTS.md` §2.2, silence reading as completion."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.GENERATING_OUTPUTS)
        with pytest.raises(EntryConditionUnmet, match="checks_have_run"):
            transition(session, revision_id, PackageState.AWAITING_REVIEW, actor=ACTOR)


def test_awaiting_review_is_reachable_once_checks_have_run(postgres_engine: Engine) -> None:
    """The condition has to be able to pass, or it is a wall rather than a check."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.GENERATING_OUTPUTS)
        _record_a_check(session, revision_id)
        event = transition(session, revision_id, PackageState.AWAITING_REVIEW, actor=ACTOR)
        assert event.to_state == PackageState.AWAITING_REVIEW.value


def test_the_refusal_explains_the_condition_rather_than_naming_it(
    postgres_engine: Engine,
) -> None:
    """A refusal saying only `checks_have_run` sends the reader to the source. The condition carries
    its own plain-English description for exactly this."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.GENERATING_OUTPUTS)
        with pytest.raises(EntryConditionUnmet) as refusal:
            transition(session, revision_id, PackageState.AWAITING_REVIEW, actor=ACTOR)

    assert "reads as none" in str(refusal.value)


def test_an_unmet_condition_is_not_an_illegal_transition(postgres_engine: Engine) -> None:
    """Different exceptions, because "never" and "not yet" call for different responses. A caller may
    retry the second; retrying the first for ever is a stuck package nobody is watching."""
    assert not issubclass(EntryConditionUnmet, IllegalTransition)
    assert not issubclass(IllegalTransition, EntryConditionUnmet)


# ---------------------------------------------------------------------------
# Resumption cannot skip the steps in between
# ---------------------------------------------------------------------------


def test_a_resume_may_not_skip_the_states_it_failed_before(postgres_engine: Engine) -> None:
    """**The hole in the table, closed.** `TRANSITIONS` must let `FAILED_RETRYABLE` reach every
    processing state, so on its own it permits resuming further along than the failure. A failure in
    `EXTRACTING` resuming at `RUNNING_CHECKS` would skip matching and evidence validation, and the
    checks would then run against evidence nothing validated — with `AWAITING_REVIEW`'s own condition
    none the wiser, because checks really would have run.
    """
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        transition(session, revision_id, PackageState.FAILED_RETRYABLE, actor=ACTOR, reason="boom")
        session.flush()

        # The table allows this edge; the condition is the only thing that refuses it.
        assert PackageState.RUNNING_CHECKS in TRANSITIONS[PackageState.FAILED_RETRYABLE]
        with pytest.raises(EntryConditionUnmet, match="resumes_where_it_failed"):
            transition(session, revision_id, PackageState.RUNNING_CHECKS, actor=ACTOR)


def test_a_resume_at_the_state_that_failed_is_allowed(postgres_engine: Engine) -> None:
    """The condition reads the event log, so the state it failed at is the state it may return to."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.EXTRACTING)
        transition(session, revision_id, PackageState.FAILED_RETRYABLE, actor=ACTOR, reason="boom")
        session.flush()
        event = transition(session, revision_id, PackageState.EXTRACTING, actor=ACTOR)
        assert event.to_state == PackageState.EXTRACTING.value


def test_needs_input_resumes_under_the_same_rule(postgres_engine: Engine) -> None:
    """`NEEDS_INPUT` is a resumable state too, and the same reasoning applies to it."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.MATCHING)
        transition(session, revision_id, PackageState.NEEDS_INPUT, actor=ACTOR, reason="ask")
        session.flush()
        with pytest.raises(EntryConditionUnmet, match="resumes_where_it_failed"):
            transition(session, revision_id, PackageState.GENERATING_OUTPUTS, actor=ACTOR)
        assert transition(session, revision_id, PackageState.MATCHING, actor=ACTOR) is not None


def test_a_resume_with_no_recorded_failure_is_refused(postgres_engine: Engine) -> None:
    """No recorded entry into the failure state means the history supports no resume target at all.
    Refusing is the safe direction — the alternative is trusting a caller's word about where it was.
    """
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        # Placed straight into the failure state, so nothing records how it got there.
        revision_id = _revision(session, PackageState.FAILED_RETRYABLE)
        with pytest.raises(EntryConditionUnmet, match="resumes_where_it_failed"):
            transition(session, revision_id, PackageState.EXTRACTING, actor=ACTOR)


# ---------------------------------------------------------------------------
# The event log
# ---------------------------------------------------------------------------


def test_the_history_is_numbered_in_order(postgres_engine: Engine) -> None:
    """Sequence numbers are what give a package's history one order. `package_state_events` carries a
    unique constraint on `(revision, sequence)`, so two concurrent transitions cannot share one."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.CREATED)
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        transition(session, revision_id, PackageState.UPLOADED, actor=ACTOR)
        session.flush()

        sequences = list(
            session.scalars(
                select(PackageStateEvent.sequence)
                .where(PackageStateEvent.package_revision_id == revision_id)
                .order_by(PackageStateEvent.sequence)
            )
        )
        assert sequences == [1, 2, 3], "genesis plus two transitions"


def test_the_genesis_event_is_the_only_one_with_no_from_state(postgres_engine: Engine) -> None:
    """`from_state IS NULL` is a revision's birth, which no transition can produce — there is no prior
    state to come from."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.CREATED)
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        session.flush()

        without_from = list(
            session.scalars(
                select(PackageStateEvent.sequence).where(
                    PackageStateEvent.package_revision_id == revision_id,
                    PackageStateEvent.from_state.is_(None),
                )
            )
        )
        assert without_from == [1]


def test_a_history_cannot_be_opened_twice(postgres_engine: Engine) -> None:
    """A second genesis would give one revision two beginnings, and its history two orders."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.CREATED)
        with pytest.raises(IllegalTransition, match="already has a recorded history"):
            begin(session, revision_id, actor=ACTOR)


def test_a_revision_that_does_not_exist_is_refused(postgres_engine: Engine) -> None:
    """Reported as its own failure rather than as an illegal transition: there is no state to leave."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session, pytest.raises(UnknownRevision):
        transition(session, uuid4(), PackageState.UPLOADING, actor=ACTOR)


def test_the_current_state_is_read_from_the_database(postgres_engine: Engine) -> None:
    """Not taken from the caller. A caller that told us where it thought the package was would be
    authorising its own move, and the interesting failures are the ones where it is wrong."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    with unit_of_work(factory) as session:
        revision_id = _revision(session, PackageState.AWAITING_REVIEW)
        # Whatever the caller believes, the row says AWAITING_REVIEW, so UPLOADING is refused.
        with pytest.raises(IllegalTransition, match="AWAITING_REVIEW"):
            transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)


def test_nothing_is_committed_by_a_transition(postgres_engine: Engine) -> None:
    """The caller owns the transaction, exactly as with `workflow.outbox.enqueue`, so a state change
    and the outbox row that justifies it land together or not at all."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)

    revision_id: UUID
    with factory() as session:
        revision_id = _revision(session, PackageState.CREATED)
        session.commit()

    with factory() as session:
        transition(session, revision_id, PackageState.UPLOADING, actor=ACTOR)
        session.rollback()

    with factory() as session:
        stored = session.execute(
            select(PackageRevision.state).where(PackageRevision.id == revision_id)
        ).scalar_one()
        assert stored == PackageState.CREATED.value, "the rollback must have discarded the move"


# ---------------------------------------------------------------------------
# Nothing goes round this module
# ---------------------------------------------------------------------------


def _python_files(package: str) -> list[Path]:
    return sorted((REPO_ROOT / package).rglob("*.py"))


def _writes_state_outside_lifecycle() -> list[str]:
    """Every place outside `app/lifecycle/` that changes a package's state or history.

    Two shapes, because there are two ways to do it: assigning to a `state` attribute or keyword, and
    constructing a `PackageStateEvent`. Both are what `transition` exists to be the only source of.
    """
    offenders: list[str] = []
    for package in ("app", "workflow"):
        for path in _python_files(package):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative.startswith((LIFECYCLE_PACKAGE, "app/models/")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    called = node.func
                    name = getattr(called, "id", None) or getattr(called, "attr", None)
                    if name == "PackageStateEvent":
                        offenders.append(f"{relative}:{node.lineno} constructs PackageStateEvent")
                    # `.values(state=...)` is a SQL UPDATE of the column, which is how
                    # `transition` itself writes it. Anywhere else it is the same bypass as an
                    # attribute assignment.
                    if name == "values" and any(
                        keyword.arg == "state" for keyword in node.keywords
                    ):
                        offenders.append(f"{relative}:{node.lineno} updates the state column")
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "state":
                            offenders.append(f"{relative}:{node.lineno} assigns to .state")
    return offenders


def test_no_module_outside_the_lifecycle_writes_a_package_state() -> None:
    """**The acceptance criterion, and what makes every test above worth something.**

    `transition` being the only way a state changes is a property of the whole tree, not of this
    module — a second writer somewhere else means the table is a suggestion. `app/models/` is skipped
    because it *declares* the column, and `PackageRevision(state=...)` at creation is allowed: a
    revision is born in `CREATED` and `begin` records that birth.
    """
    offenders = _writes_state_outside_lifecycle()
    assert not offenders, (
        "these write a package state outside app/lifecycle/:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse app.lifecycle.transition (or begin, for a revision's first event). The transition "
        "table is only a rule while it is the only way through."
    )


def test_the_guard_catches_a_write_it_should(tmp_path: Path) -> None:
    """The audit asserting nothing would look identical on a green run.

    Rewritten against a throwaway tree rather than by planting a file in `app/`, which would leave the
    repository failing its own guard if this test died before cleaning up.
    """
    package = tmp_path / "app" / "somewhere"
    package.mkdir(parents=True)
    (package / "sneaky.py").write_text(
        "def move(revision):\n    revision.state = 'APPROVED'\n", encoding="utf-8"
    )

    global REPO_ROOT
    original = REPO_ROOT
    try:
        REPO_ROOT = tmp_path
        offenders = _writes_state_outside_lifecycle()
    finally:
        REPO_ROOT = original

    assert any("assigns to .state" in offender for offender in offenders), offenders


def test_the_guard_catches_a_state_event_construction(tmp_path: Path) -> None:
    """The other shape. Writing the history directly bypasses the table just as completely."""
    package = tmp_path / "app" / "somewhere"
    package.mkdir(parents=True)
    (package / "sneaky.py").write_text(
        "def move(session):\n    session.add(PackageStateEvent(to_state='APPROVED'))\n",
        encoding="utf-8",
    )

    global REPO_ROOT
    original = REPO_ROOT
    try:
        REPO_ROOT = tmp_path
        offenders = _writes_state_outside_lifecycle()
    finally:
        REPO_ROOT = original

    assert any("constructs PackageStateEvent" in offender for offender in offenders), offenders


def test_the_entry_conditions_are_data_beside_the_table() -> None:
    """Named predicates listed as data, not inline checks — so a refusal can say which one failed and
    a reader can see what guards what without following the code."""
    for state, conditions in ENTRY_CONDITIONS.items():
        assert conditions, f"{state.value} has an empty condition tuple"
        for condition in conditions:
            assert condition.name and condition.describe
            assert callable(condition.holds)

    assert PackageState.AWAITING_REVIEW in ENTRY_CONDITIONS
    assert set(PROCESSING_STATES) <= set(
        ENTRY_CONDITIONS
    ), "every processing state is a possible resume target and needs the resume condition"
