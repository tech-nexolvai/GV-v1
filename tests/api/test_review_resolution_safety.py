"""A review verb alone cannot resolve a live check (#1026 safety review D1–D6)."""

from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store
from app.models import (
    CanonicalObservation,
    Finding,
    FindingEvidence,
    PackageRevision,
    ReviewSession,
)
from app.models.review import ReviewActionKind
from app.review.approval import _findings, approval_readiness
from app.review.evidence_actions import correct_evidence
from app.review.session import open_session, record_action
from app.review.signed_record import ReviewDisposition
from tests.api.test_findings_query import _finding, _snapshot
from tests.api.test_review_api import _client, _revision, session
from tests.api.test_slot_rows import _package_rows, _reader_support, _run_current_checks
from tests.review.test_approval import reviewer
from verdict.outcomes import Outcome

pytest_plugins = ("tests.app.postgres_fixture",)
__all__ = ["session"]


def _review_client(db: Session, project: UUID) -> TestClient:
    client = _client(db, project)
    # Approval writes database records only; artifact access is not part of these refusals.
    client.app.dependency_overrides[get_artifact_store] = lambda: None
    return client


def _case(
    db: Session, outcome: Outcome = Outcome.FAIL
) -> tuple[UUID, PackageRevision, Finding, ReviewSession, TestClient]:
    project = uuid4()
    revision = _revision(db, project)
    finding = _finding(db, revision, _snapshot(db), outcome)
    review = open_session(db, package_revision_id=revision.id, reviewer="anant")
    db.commit()
    return project, revision, finding, review, _review_client(db, project)


def test_confirmed_fail_is_a_problem_for_the_vendor_without_a_note(session: Session) -> None:
    _, revision, finding, review, _ = _case(session)
    action = record_action(
        session,
        review_session_id=review.id,
        finding_id=finding.id,
        actor="anant",
        action=ReviewActionKind.CONFIRM,
    )
    assert approval_readiness(session, revision.id).blocking_finding_ids == ()
    words = ReviewDisposition(
        action_id=action.id, action="confirm", reviewer="anant", at=action.created_at
    ).wording("FAIL")
    assert "problem for the vendor" in words.lower()
    assert "FAIL" in words


def test_correction_blocks_until_new_live_run_and_new_finding(
    session: Session, tmp_path: Path
) -> None:
    from app.models import ObservationCandidate, VerdictInput

    _, package, anchors = _package_rows(session, wall_source="code-ink-and-both-ais")
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ):
        _reader_support(session, candidate)
    first = _run_current_checks(session, package, tmp_path)
    finding = next(item for item in first if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "FAIL"
    review = open_session(
        session, package_revision_id=finding.package_revision_id, reviewer="anant"
    )
    observation = session.scalar(
        select(CanonicalObservation)
        .join(VerdictInput, VerdictInput.canonical_observation_id == CanonicalObservation.id)
        .where(VerdictInput.check_run_id == finding.check_run_id)
    )
    assert observation is not None
    session.add(
        FindingEvidence(
            finding_id=finding.id, canonical_observation_id=observation.id, role="operand"
        )
    )
    session.flush()
    decision = correct_evidence(
        session,
        principal=reviewer(),
        review_session_id=review.id,
        finding_id=finding.id,
        observation_id=observation.id,
        corrected_value=Fraction(observation.value_numerator, observation.value_denominator) + 1,
    )
    assert decision.action.action == "correct"
    assert (
        finding.id in approval_readiness(session, finding.package_revision_id).blocking_finding_ids
    )
    # A later one-click confirmation must not bypass the required rerun either.
    record_action(
        session,
        review_session_id=review.id,
        finding_id=finding.id,
        action=ReviewActionKind.CONFIRM,
        actor="anant",
    )
    assert (
        finding.id in approval_readiness(session, finding.package_revision_id).blocking_finding_ids
    )
    second = _run_current_checks(session, package, tmp_path)
    replacement = next(item for item in second if item.scope_row_candidate_id == anchors[0])
    assert replacement.id != finding.id
    assert replacement.check_run_id != finding.check_run_id
    assert finding.id not in {item.id for item in _findings(session, finding.package_revision_id)}
    assert (
        replacement.id
        in approval_readiness(session, finding.package_revision_id).blocking_finding_ids
    )
