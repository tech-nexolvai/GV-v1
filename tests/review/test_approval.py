"""Package approval and change-request safety properties (#231, D4.3)."""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.lifecycle.states import IllegalTransition
from app.models.package import PackageRevision, PackageState
from app.models.review import ApprovedFinding, ReviewActionKind
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding
from app.review.approval import (
    ApprovalNotAuthorised,
    DriverFindingRequired,
    FindingOutsideReview,
    UnaddressedReviewRequired,
    approve_package,
    request_changes,
)
from app.review.session import open_session, record_action
from tests.review.test_session import _finding, _revision


def reviewer(name: str = "anant") -> Principal:
    return Principal(name, frozenset({Role.REVIEWER}))


def _review_required_finding(db: Session, revision: PackageRevision) -> Finding:
    """Create the abstaining input directly; immutable findings are never edited after insertion."""
    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:6]}")
    db.add(definition)
    db.flush()
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
    db.add(snapshot)
    db.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    db.add(run)
    db.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome="REVIEW_REQUIRED",
        severity="CRITICAL",
        trace={},
        parameter_set_versions={},
    )
    db.add(finding)
    db.flush()
    return finding


def test_approval_pins_the_server_selected_finding_rows(postgres_engine: Engine) -> None:
    """Input: two addressed findings. Outcome: APPROVED and both exact finding ids are pinned."""
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        revision = _revision(db)
        first = _finding(db, revision)
        second = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="anant")

        decision = approve_package(db, principal=reviewer(), review_session_id=review.id)

        assert set(decision.finding_ids) == {first.id, second.id}
        assert decision.state_event.to_state == PackageState.APPROVED.value
        assert set(
            db.scalars(
                select(ApprovedFinding.finding_id).where(
                    ApprovedFinding.approval_id == decision.approval.id
                )
            ).all()
        ) == {first.id, second.id}


def test_unaddressed_review_required_cannot_be_approved(postgres_engine: Engine) -> None:
    """Input: REVIEW REQUIRED with no human action. Outcome: refusal, never silent approval."""
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        revision = _revision(db)
        finding = _review_required_finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="anant")

        with pytest.raises(UnaddressedReviewRequired, match=str(finding.id)):
            approve_package(db, principal=reviewer(), review_session_id=review.id)


def test_an_explicit_action_addresses_review_required(postgres_engine: Engine) -> None:
    """Input: REVIEW REQUIRED plus named dismissal. Outcome: approval may proceed and remains traced."""
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        revision = _revision(db)
        finding = _review_required_finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="anant")
        record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=ReviewActionKind.DISMISS,
            actor="anant",
            note="reviewed against the drawing",
        )

        decision = approve_package(db, principal=reviewer(), review_session_id=review.id)

        assert decision.state_event.to_state == PackageState.APPROVED.value


def test_change_request_records_its_driver_findings(postgres_engine: Engine) -> None:
    """Input: two package findings. Outcome: CHANGES_REQUESTED event names both deterministically."""
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        revision = _revision(db)
        first = _finding(db, revision)
        second = _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="keyur")

        decision = request_changes(
            db,
            principal=reviewer("keyur"),
            review_session_id=review.id,
            finding_ids=[second.id, first.id],
        )

        assert decision.state_event.to_state == PackageState.CHANGES_REQUESTED.value
        assert decision.state_event.reason is not None
        assert str(first.id) in decision.state_event.reason
        assert str(second.id) in decision.state_event.reason


def test_change_request_needs_a_driver(postgres_engine: Engine) -> None:
    """Input: empty driver list. Outcome: refusal instead of an unexplained terminal decision."""
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        revision = _revision(db)
        review = open_session(db, package_revision_id=revision.id, reviewer="anant")

        with pytest.raises(DriverFindingRequired):
            request_changes(
                db,
                principal=reviewer(),
                review_session_id=review.id,
                finding_ids=[],
            )


def test_change_request_refuses_a_finding_from_another_revision(postgres_engine: Engine) -> None:
    """Input: foreign finding id. Outcome: refusal; one package cannot cite another's problem."""
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        mine = _revision(db)
        theirs = _revision(db)
        foreign = _finding(db, theirs)
        review = open_session(db, package_revision_id=mine.id, reviewer="anant")

        with pytest.raises(FindingOutsideReview, match=str(foreign.id)):
            request_changes(
                db,
                principal=reviewer(),
                review_session_id=review.id,
                finding_ids=[foreign.id],
            )


def test_approval_from_a_side_state_is_impossible(postgres_engine: Engine) -> None:
    """Input: NEEDS_INPUT package. Outcome: lifecycle table refuses APPROVED."""
    factory = session_factory(postgres_engine)
    with factory.begin() as db:
        revision = _revision(db, state=PackageState.NEEDS_INPUT)
        _finding(db, revision)
        review = open_session(db, package_revision_id=revision.id, reviewer="anant")

        with pytest.raises(IllegalTransition, match="NEEDS_INPUT"):
            approve_package(db, principal=reviewer(), review_session_id=review.id)


def test_unauthorised_principal_is_refused_before_database_access() -> None:
    """Input: rule administrator and unusable DB. Outcome: authorisation refusal comes first."""

    class UnusableSession:
        def get(self, model: object, identity: object) -> object:
            raise AssertionError((model, identity))

    with pytest.raises(ApprovalNotAuthorised):
        approve_package(  # type: ignore[arg-type]
            UnusableSession(),
            principal=Principal("rule-author", frozenset({Role.RULE_ADMIN})),
            review_session_id=uuid4(),
        )
