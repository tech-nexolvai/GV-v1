"""A review verb alone cannot resolve a live check (#1026 safety review D1–D6)."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store
from app.main import API_PREFIX
from app.models import (
    CanonicalObservation,
    CheckRun,
    Finding,
    FindingEvidence,
    PackageRevision,
    ReviewAction,
    ReviewSession,
)
from app.models.review import ReviewActionKind, ReviewException
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


def _assert_blocked(
    client: TestClient,
    project: UUID,
    revision: PackageRevision,
    review: ReviewSession,
    finding: Finding,
) -> None:
    readiness = client.get(
        f"{API_PREFIX}/projects/{project}/packages/{revision.package_id}/approval-readiness"
    )
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["blocking_finding_ids"] == [str(finding.id)]
    assert readiness.json()["can_approve"] is False
    response = client.post(f"{API_PREFIX}/projects/{project}/review-sessions/{review.id}/approve")
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("action", ["correct", "except"])
def test_bare_special_action_is_refused_and_does_not_unlock_signoff(
    session: Session, action: str
) -> None:
    project, revision, finding, review, client = _case(session)
    response = client.post(
        f"{API_PREFIX}/projects/{project}/review-sessions/{review.id}/actions",
        json={"finding_id": str(finding.id), "action": action},
    )
    assert response.status_code == 422, response.text
    _assert_blocked(client, project, revision, review, finding)


@pytest.mark.parametrize("action", ["correct", "except"])
def test_legacy_action_without_its_supporting_record_never_unlocks_signoff(
    session: Session, action: str
) -> None:
    project, revision, finding, review, client = _case(session)
    session.add(
        ReviewAction(
            review_session_id=review.id,
            finding_id=finding.id,
            package_revision_id=revision.id,
            action=action,
            actor="anant",
            note="Synthetic unsupported legacy action",
        )
    )
    session.commit()
    _assert_blocked(client, project, revision, review, finding)


@pytest.mark.parametrize("expired,wrong_scope", [(True, False), (False, True), (False, False)])
def test_only_matching_unexpired_exception_resolves_the_finding(
    session: Session, expired: bool, wrong_scope: bool
) -> None:
    project, revision, finding, review, client = _case(session)
    now = datetime.now(UTC)
    action = ReviewAction(
        review_session_id=review.id,
        finding_id=finding.id,
        package_revision_id=revision.id,
        action="except",
        actor="anant",
        note="Synthetic exception",
        created_at=now - timedelta(days=2),
    )
    session.add(action)
    session.flush()
    session.add(
        ReviewException(
            review_action_id=action.id,
            scope="finding",
            scope_id=uuid4() if wrong_scope else finding.id,
            reason="Synthetic exception",
            approved_by="anant",
            created_at=now - timedelta(days=2),
            expires_at=now + timedelta(days=-1 if expired else 1),
        )
    )
    session.commit()
    if expired or wrong_scope:
        _assert_blocked(client, project, revision, review, finding)
    else:
        assert approval_readiness(session, revision.id).can_approve


@pytest.mark.parametrize("note", [None, "", "   "])
def test_dismissing_fail_requires_a_note(session: Session, note: str | None) -> None:
    project, revision, finding, review, client = _case(session)
    response = client.post(
        f"{API_PREFIX}/projects/{project}/review-sessions/{review.id}/actions",
        json={"finding_id": str(finding.id), "action": "dismiss", "note": note},
    )
    assert response.status_code == 422, response.text
    _assert_blocked(client, project, revision, review, finding)


def test_fail_dismissal_note_is_recorded_in_the_signed_words(session: Session) -> None:
    project, revision, finding, review, client = _case(session)
    response = client.post(
        f"{API_PREFIX}/projects/{project}/review-sessions/{review.id}/actions",
        json={
            "finding_id": str(finding.id),
            "action": "dismiss",
            "note": "Detail cannot be checked",
        },
    )
    assert response.status_code == 201, response.text
    action = response.json()
    words = ReviewDisposition(
        action_id=action["id"],
        action=action["action"],
        reviewer=action["actor"],
        at=action["created_at"],
        note=action["note"],
    ).wording("FAIL")
    assert "Detail cannot be checked" in words and "FAIL" in words
    assert approval_readiness(session, revision.id).can_approve


def test_confirmed_fail_is_a_problem_for_the_vendor_without_a_note(session: Session) -> None:
    _, revision, finding, review, _ = _case(session)
    action = record_action(
        session,
        review_session_id=review.id,
        finding_id=finding.id,
        actor="anant",
        action=ReviewActionKind.CONFIRM,
    )
    assert approval_readiness(session, revision.id).can_approve
    words = ReviewDisposition(
        action_id=action.id, action="confirm", reviewer="anant", at=action.created_at
    ).wording("FAIL")
    assert "problem for the vendor" in words.lower()
    assert "FAIL" in words


def test_approval_cannot_use_a_session_from_another_project(session: Session) -> None:
    project, _, _, _, _ = _case(session)
    _, _, foreign, foreign_review, _ = _case(session)
    record_action(
        session,
        review_session_id=foreign_review.id,
        finding_id=foreign.id,
        action=ReviewActionKind.CONFIRM,
        actor="anant",
    )
    session.commit()
    response = _review_client(session, project).post(
        f"{API_PREFIX}/projects/{project}/review-sessions/{foreign_review.id}/approve"
    )
    assert response.status_code == 404, response.text


def test_correction_blocks_until_new_live_run_and_new_finding(
    session: Session, tmp_path: Path
) -> None:
    from app.models import ObservationCandidate, VerdictInput

    _, package, anchors = _package_rows(session, wall_source="vendor-drawing-clues")
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


def test_missing_input_reason_is_plain_without_changing_stored_reason(session: Session) -> None:
    project, revision, original, _, client = _case(session, Outcome.NOT_FOUND)
    reason = "the final operation could not resolve required value 'shop_cabinets'"
    parent = session.get(CheckRun, original.check_run_id)
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=parent.rule_snapshot_id,
        engine_version="synthetic",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome="NOT_FOUND",
        severity="FLAG",
        trace={},
        parameter_set_versions={},
        reason=reason,
    )
    session.add(finding)
    session.commit()
    items = client.get(
        f"{API_PREFIX}/projects/{project}/packages/{revision.package_id}/findings"
    ).json()["items"]
    shown = next(item for item in items if item["id"] == str(finding.id))
    assert "shop_cabinets" not in shown["reviewer_reason"]
    assert "cabinet" in shown["reviewer_reason"].lower()
    chain = client.get(
        f"{API_PREFIX}/projects/{project}/packages/{revision.package_id}/findings/{finding.id}/chain"
    )
    assert chain.status_code == 200, chain.text
    assert "shop_cabinets" not in chain.json()["reviewer_reason"]
    session.refresh(finding)
    assert finding.reason == reason
