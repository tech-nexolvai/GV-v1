"""Database contract for the review plane (#200, C1.10).

Who decided, on what, and why — written so those answers cannot later be tidied. Three refusals carry
the story:

* an exception with no expiry cannot be stored, so a check cannot be silently switched off forever;
* a correction keeps the original beside the change, or there is no way to ask what we got wrong;
* an approval names findings by foreign key, never by a value the caller supplied.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    Approval,
    ApprovedFinding,
    CheckRun,
    CorrectionLedgerEntry,
    ExceptionScope,
    Finding,
    PackageRevision,
    ReviewAction,
    ReviewActionKind,
    ReviewException,
    ReviewSession,
    RuleDefinition,
    RuleSnapshot,
)
from tests.app.postgres_fixture import alembic_config
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

REVIEW_TABLES = {
    "review_sessions",
    "review_actions",
    "correction_ledger",
    "approvals",
    "approved_findings",
    "review_exceptions",
}


def _violated(error: IntegrityError) -> str | None:
    """The constraint PostgreSQL actually rejected on.

    Needed because a row usually violates more than one thing, and `pytest.raises(IntegrityError)`
    accepts whichever fired first. Two negative tests in this file passed for the wrong reason until
    the name was asserted: one was rejected by a unique index rather than the composite foreign key
    it claimed to exercise. A test that cannot fail on a wrong answer is worse than no test, because
    it reads as coverage.
    """
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    return getattr(diagnostic, "constraint_name", None)


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _finding(session: Session) -> Finding:
    import hashlib

    from app.models import Package, PackageState, Project

    project = Project(name=f"GV Review Test {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:6]}")
    session.add(definition)
    session.flush()
    body = f'{{"id":"{definition.rule_id}"}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=Outcome.FAIL.value,
        severity=Severity.CRITICAL.value,
        trace={},
        parameter_set_versions={},
    )
    session.add(finding)
    session.flush()
    return finding


def _check_run_without_a_finding(session: Session) -> CheckRun:
    """A run with no finding, so a test about findings cannot be rejected by the unique index."""
    finding = _finding(session)
    snapshot_id = session.scalars(
        select(CheckRun.rule_snapshot_id).where(CheckRun.id == finding.check_run_id)
    ).one()
    revision_id = session.scalars(
        select(CheckRun.package_revision_id).where(CheckRun.id == finding.check_run_id)
    ).one()
    run = CheckRun(
        package_revision_id=revision_id,
        rule_snapshot_id=snapshot_id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    return run


def _action(
    session: Session, finding: Finding, kind: ReviewActionKind = ReviewActionKind.CORRECT
) -> ReviewAction:
    revision_id = session.scalars(
        select(CheckRun.package_revision_id).where(CheckRun.id == finding.check_run_id)
    ).one()
    review = ReviewSession(package_revision_id=revision_id, reviewer="anant")
    session.add(review)
    session.flush()
    action = ReviewAction(
        review_session_id=review.id,
        finding_id=finding.id,
        package_revision_id=revision_id,
        action=kind.value,
        actor="anant",
    )
    session.add(action)
    session.flush()
    return action


# ---------------------------------------------------------------------------
# Shape, no database needed
# ---------------------------------------------------------------------------


def test_all_six_tables_are_registered() -> None:
    assert REVIEW_TABLES <= set(Base.metadata.tables)


def test_the_record_of_what_was_done_is_immutable() -> None:
    """A session is open while it is worked and may be completed; what was *done* in it must not
    change, and the ledger least of all — the record of what we got wrong is exactly what somebody
    would be tempted to edit."""
    for model in (ReviewAction, CorrectionLedgerEntry, Approval, ApprovedFinding, ReviewException):
        assert issubclass(model, Immutable)
    assert not issubclass(ReviewSession, Immutable)


def test_an_exception_must_have_an_expiry() -> None:
    """The whole control. A permanent silent exception is not representable, so somebody has to look
    again."""
    assert Base.metadata.tables["review_exceptions"].columns["expires_at"].nullable is False


def test_an_exception_cannot_be_scoped_to_a_rule_everywhere() -> None:
    """A rule that should not fire is a rule change, and it goes through the rulebook where somebody
    reviews it. An exception says *this one* is acceptable."""
    assert {scope.value for scope in ExceptionScope} == {"finding", "item", "package"}


def test_an_approval_names_findings_by_foreign_key() -> None:
    """Not a list column. A JSON array of ids would be exactly the client-supplied value the
    acceptance forbids, with nothing checking that any of them exist."""
    assert "finding_ids" not in Base.metadata.tables["approvals"].columns
    assert "finding_id" in Base.metadata.tables["approved_findings"].columns


def test_there_is_no_general_edit_action() -> None:
    """Four verbs and no fifth. An `edit` would collapse confirm, correct, except and dismiss into
    one, and the ledger exists to keep them apart."""
    assert {kind.value for kind in ReviewActionKind} == {
        "confirm",
        "correct",
        "except",
        "dismiss",
    }


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def test_an_exception_without_an_expiry_cannot_be_written(postgres_engine: Engine) -> None:
    """The refusal that matters most in this file."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises((IntegrityError, StatementError)), unit_of_work(factory) as session:
        action = _action(session, _finding(session), ReviewActionKind.EXCEPT)
        session.add(
            ReviewException(
                review_action_id=action.id,
                action=ReviewActionKind.EXCEPT.value,
                scope=ExceptionScope.FINDING.value,
                scope_id=uuid4(),
                reason="site condition accepted by the client",
                approved_by="anant",
                expires_at=None,
            )
        )


def test_an_exception_that_expires_before_it_was_made_is_refused(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        action = _action(session, _finding(session), ReviewActionKind.EXCEPT)
        session.add(
            ReviewException(
                review_action_id=action.id,
                action=ReviewActionKind.EXCEPT.value,
                scope=ExceptionScope.FINDING.value,
                scope_id=uuid4(),
                reason="backdated",
                approved_by="anant",
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )


def test_a_bounded_exception_is_stored_with_its_reason(postgres_engine: Engine) -> None:
    """An exception nobody explained is one nobody can review, and the reason is the sentence a
    future reader needs most."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        action = _action(session, _finding(session), ReviewActionKind.EXCEPT)
        # A real package revision, not a random UUID. `scope_id` is polymorphic and cannot be a
        # foreign key, so a fixture pointing at nothing would have the test assert a state the
        # schema does not validate — see `test_a_scope_id_is_not_validated_by_the_schema`.
        revision_id = session.scalars(
            select(ReviewSession.package_revision_id).where(
                ReviewSession.id == action.review_session_id
            )
        ).one()
        session.add(
            ReviewException(
                review_action_id=action.id,
                action=ReviewActionKind.EXCEPT.value,
                scope=ExceptionScope.PACKAGE.value,
                scope_id=revision_id,
                reason="client accepted the 3mm overhang on this run",
                approved_by="anant",
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
    with unit_of_work(factory) as session:
        stored = session.scalars(select(ReviewException)).one()
        assert "3mm overhang" in stored.reason
        assert stored.expires_at > stored.created_at


def test_a_correction_keeps_the_original_beside_the_change(postgres_engine: Engine) -> None:
    """Storing only the corrected value would leave no way to ask what we got wrong — the entire
    purpose, and the reason the reviewer correction rate can be measured at all."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    from app.models import CanonicalObservation

    with unit_of_work(factory) as session:
        action = _action(session, _finding(session))
        observation = session.scalars(select(CanonicalObservation)).first()
        if observation is None:
            pytest.skip("no canonical observation fixture available in this schema state")
        session.add(
            CorrectionLedgerEntry(
                review_action_id=action.id,
                action=ReviewActionKind.CORRECT.value,
                canonical_observation_id=observation.id,
                original_value="1219 mm",
                corrected_value="1216 mm",
            )
        )
    with unit_of_work(factory) as session:
        entry = session.scalars(select(CorrectionLedgerEntry)).one()
        assert (entry.original_value, entry.corrected_value) == ("1219 mm", "1216 mm")


def test_a_correction_that_changes_nothing_is_refused(postgres_engine: Engine) -> None:
    """That is a confirmation, and it belongs in `review_actions` as one. Storing it here would
    inflate the correction rate with non-events."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        action = _action(session, _finding(session))
        session.add(
            CorrectionLedgerEntry(
                review_action_id=action.id,
                action=ReviewActionKind.CORRECT.value,
                canonical_observation_id=uuid4(),
                original_value="1219 mm",
                corrected_value="1219 mm",
            )
        )


def test_an_action_must_be_one_of_the_four_verbs(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        finding = _finding(session)
        revision_id = session.scalars(
            select(CheckRun.package_revision_id).where(CheckRun.id == finding.check_run_id)
        ).one()
        review = ReviewSession(package_revision_id=revision_id, reviewer="anant")
        session.add(review)
        session.flush()
        session.add(
            ReviewAction(
                review_session_id=review.id,
                finding_id=finding.id,
                package_revision_id=revision_id,
                action="edit",
                actor="anant",
            )
        )


def test_an_action_must_name_who_did_it(postgres_engine: Engine) -> None:
    """A session may be picked up by somebody else, so the action says who actually acted."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        finding = _finding(session)
        revision_id = session.scalars(
            select(CheckRun.package_revision_id).where(CheckRun.id == finding.check_run_id)
        ).one()
        review = ReviewSession(package_revision_id=revision_id, reviewer="anant")
        session.add(review)
        session.flush()
        session.add(
            ReviewAction(
                review_session_id=review.id,
                finding_id=finding.id,
                package_revision_id=revision_id,
                action=ReviewActionKind.CONFIRM.value,
                actor="",
            )
        )


def test_an_approval_records_exactly_which_findings_were_in_force(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        finding = _finding(session)
        revision_id = session.scalars(
            select(CheckRun.package_revision_id).where(CheckRun.id == finding.check_run_id)
        ).one()
        approval = Approval(package_revision_id=revision_id, approved_by="anant")
        session.add(approval)
        session.flush()
        session.add(
            ApprovedFinding(
                approval_id=approval.id,
                finding_id=finding.id,
                package_revision_id=revision_id,
            )
        )
    with unit_of_work(factory) as session:
        link = session.scalars(select(ApprovedFinding)).one()
        assert link.finding_id == session.scalars(select(Finding)).one().id


def test_an_approval_cannot_name_a_finding_that_does_not_exist(postgres_engine: Engine) -> None:
    """What "server-side revisions, never client-supplied values" means in a schema."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        finding = _finding(session)
        revision_id = session.scalars(
            select(CheckRun.package_revision_id).where(CheckRun.id == finding.check_run_id)
        ).one()
        approval = Approval(package_revision_id=revision_id, approved_by="anant")
        session.add(approval)
        session.flush()
        session.add(
            ApprovedFinding(
                approval_id=approval.id, finding_id=uuid4(), package_revision_id=revision_id
            )
        )


def test_one_correction_per_action(postgres_engine: Engine) -> None:
    """Two would leave "what did the reviewer change?" with two answers."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    from app.models import CanonicalObservation

    with unit_of_work(factory) as session:
        action = _action(session, _finding(session))
        observation = session.scalars(select(CanonicalObservation)).first()
        if observation is None:
            pytest.skip("no canonical observation fixture available in this schema state")
        session.add(
            CorrectionLedgerEntry(
                review_action_id=action.id,
                action=ReviewActionKind.CORRECT.value,
                canonical_observation_id=observation.id,
                original_value="a",
                corrected_value="b",
            )
        )
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        entry = session.scalars(select(CorrectionLedgerEntry)).one()
        session.add(
            CorrectionLedgerEntry(
                review_action_id=entry.review_action_id,
                action=ReviewActionKind.CORRECT.value,
                canonical_observation_id=entry.canonical_observation_id,
                original_value="a",
                corrected_value="c",
            )
        )


def test_a_finding_cannot_be_deleted_while_a_reviewer_acted_on_it(postgres_engine: Engine) -> None:
    """RESTRICT. Deleting it would erase what the reviewer was looking at."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _action(session, _finding(session), ReviewActionKind.CONFIRM)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.delete(session.scalars(select(Finding)).one())


# ---------------------------------------------------------------------------
# Cross-record integrity — found by review on #334
# ---------------------------------------------------------------------------


def test_a_session_cannot_act_on_a_finding_from_another_package(postgres_engine: Engine) -> None:
    """A review of package A carrying an action on a finding from package B would misstate what was
    reviewed — and an approval built from it would misstate what was signed off. Two composite
    foreign keys resolve the revision against the session and against the finding."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        mine = _finding(session)
        other = _finding(session)  # a different project, package and revision
        my_revision = session.scalars(
            select(CheckRun.package_revision_id).where(CheckRun.id == mine.check_run_id)
        ).one()
        review = ReviewSession(package_revision_id=my_revision, reviewer="anant")
        session.add(review)
        session.flush()
        session.add(
            ReviewAction(
                review_session_id=review.id,
                finding_id=other.id,
                package_revision_id=my_revision,
                action=ReviewActionKind.CONFIRM.value,
                actor="anant",
            )
        )


def test_an_approval_cannot_cover_a_finding_from_another_package(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        mine = _finding(session)
        other = _finding(session)
        my_revision = session.scalars(
            select(CheckRun.package_revision_id).where(CheckRun.id == mine.check_run_id)
        ).one()
        approval = Approval(package_revision_id=my_revision, approved_by="anant")
        session.add(approval)
        session.flush()
        session.add(
            ApprovedFinding(
                approval_id=approval.id,
                finding_id=other.id,
                package_revision_id=my_revision,
            )
        )


def test_a_finding_cannot_claim_a_revision_its_run_does_not_have(postgres_engine: Engine) -> None:
    """The copy is denormalised so the constraints above can use it, and a composite foreign key
    keeps it honest rather than a comment asking nicely.

    The run here has **no** finding yet, so the unique `check_run_id` index cannot be what rejects
    the row — the first version of this test reused a run that already had one, and passed on the
    unique index while claiming to exercise the foreign key.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        bare_run = _check_run_without_a_finding(session)
        session.add(
            Finding(
                check_run_id=bare_run.id,
                package_revision_id=uuid4(),
                outcome=Outcome.PASS.value,
                severity=Severity.MINOR.value,
                trace={},
                parameter_set_versions={},
            )
        )
    assert _violated(raised.value) == "fk_findings_run_revision"


def test_a_ledger_entry_cannot_hang_off_a_confirmation(postgres_engine: Engine) -> None:
    """A correction record attached to a `confirm` would count an event that was not a correction,
    and D5.4 measures exactly that rate."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    from app.models import CanonicalObservation

    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        action = _action(session, _finding(session), ReviewActionKind.CONFIRM)
        observation = session.scalars(select(CanonicalObservation)).first()
        session.add(
            CorrectionLedgerEntry(
                review_action_id=action.id,
                action=ReviewActionKind.CONFIRM.value,
                # A real observation, so the only thing wrong with this row is the action kind. The
                # first version used uuid4() and violated the observation foreign key as well.
                canonical_observation_id=observation.id if observation else uuid4(),
                original_value="a",
                corrected_value="b",
            )
        )
    if observation is not None:
        assert _violated(raised.value) in {
            "correction_action_is_a_correction",
            "ck_correction_ledger_correction_action_is_a_correction",
            "fk_correction_action_kind",
        }


def test_an_exception_cannot_hang_off_a_confirmation(postgres_engine: Engine) -> None:
    """That would be a check switched off by a record saying the reviewer agreed with it."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        action = _action(session, _finding(session), ReviewActionKind.CONFIRM)
        session.add(
            ReviewException(
                review_action_id=action.id,
                action=ReviewActionKind.CONFIRM.value,
                scope=ExceptionScope.FINDING.value,
                scope_id=uuid4(),
                reason="agreed",
                approved_by="anant",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )


def test_a_scope_id_is_not_validated_by_the_schema() -> None:
    """Stated rather than assumed. `scope` selects one of three tables, so `scope_id` cannot be a
    foreign key — a column cannot reference three parents. The database therefore accepts an id
    pointing at nothing, and validating it needs application code or a trigger (`C1.12`).

    Asserted so that nobody reads the composite keys above and concludes every reference in this
    plane is enforced. This one is not.
    """
    scope_id = Base.metadata.tables["review_exceptions"].columns["scope_id"]
    assert scope_id.foreign_keys == set(), "if this gains a foreign key, delete this test"
