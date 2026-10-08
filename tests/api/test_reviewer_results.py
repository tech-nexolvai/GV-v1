"""Reviewer results preserve outcomes, row identity and explicit sign-off decisions."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.slot_rows import list_slot_rows
from app.main import API_PREFIX
from app.models import CheckRun, Finding, Package, PackageRevision, ReviewAction
from app.models.review import ReviewActionKind
from app.review.approval import UnaddressedReviewRequired, approve_package
from app.review.session import open_session, record_action
from tests.api.test_review_api import _client, _finding, _revision, session
from tests.api.test_slot_rows import _package_rows, _run_current_checks
from tests.review.test_approval import reviewer

pytest_plugins = ("tests.app.postgres_fixture",)
__all__ = ["session"]


def result(db: Session, outcome: str) -> Finding:
    revision = _revision(db, uuid4())
    first = _finding(db, revision)
    parent = db.get(CheckRun, first.check_run_id)
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=parent.rule_snapshot_id,
        engine_version="synthetic",
    )
    db.add(run)
    db.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome,
        severity="FLAG",
        trace={},
        parameter_set_versions={},
    )
    db.add(finding)
    db.flush()
    return finding


@pytest.mark.parametrize("outcome", ["REVIEW_REQUIRED", "NOT_FOUND"])
@pytest.mark.parametrize("action", ["confirm", "dismiss"])
@pytest.mark.parametrize("note", [None, "", "   "])
def test_abstention_decision_needs_a_nonblank_note(
    session: Session, outcome: str, action: str, note: str | None
) -> None:
    finding = result(session, outcome)
    package = session.get(
        Package, session.get(PackageRevision, finding.package_revision_id).package_id
    )
    review = open_session(
        session, package_revision_id=finding.package_revision_id, reviewer="reviewer"
    )
    session.commit()
    response = _client(session, package.project_id).post(
        f"{API_PREFIX}/projects/{package.project_id}/review-sessions/{review.id}/actions",
        json={"finding_id": str(finding.id), "action": action, "note": note},
    )
    assert response.status_code == 422, response.text
    assert "note" in response.text.lower()
    assert not session.scalars(
        select(ReviewAction).where(ReviewAction.finding_id == finding.id)
    ).all()


@pytest.mark.parametrize("outcome", ["FAIL", "REVIEW_REQUIRED", "NOT_FOUND"])
def test_each_unaddressed_outcome_blocks_approval(session: Session, outcome: str) -> None:
    finding = result(session, outcome)
    review = open_session(
        session, package_revision_id=finding.package_revision_id, reviewer="reviewer"
    )
    with pytest.raises(UnaddressedReviewRequired, match=str(finding.id)):
        approve_package(session, principal=reviewer(), review_session_id=review.id)


@pytest.mark.parametrize("outcome", ["REVIEW_REQUIRED", "NOT_FOUND"])
@pytest.mark.parametrize("action", [ReviewActionKind.CONFIRM, ReviewActionKind.DISMISS])
def test_review_note_preserves_the_recorded_outcome(
    session: Session, outcome: str, action: ReviewActionKind
) -> None:
    finding = result(session, outcome)
    review = open_session(
        session, package_revision_id=finding.package_revision_id, reviewer="reviewer"
    )
    saved = record_action(
        session,
        review_session_id=review.id,
        finding_id=finding.id,
        action=action,
        actor="reviewer",
        note="Checked the supporting detail",
    )
    session.refresh(finding)
    assert finding.outcome == outcome
    assert saved.note == "Checked the supporting detail"
    assert saved.actor == "reviewer"


def test_row_and_finding_return_the_same_normalized_location(
    session: Session, tmp_path: Path
) -> None:
    project, package, anchors = _package_rows(session, wall_source="vendor-drawing-clues")
    rows = list_slot_rows(reviewer(), session, project, package).rows
    assert rows[0].row_location.page_number == 1
    assert rows[0].row_location.coordinate_space == "stored"
    assert all(0 <= float(v) <= 1 for point in rows[0].row_location.polygon for v in point)
    session.commit()
    _run_current_checks(session, package, tmp_path)
    client = _client(session, project)
    response = client.get(f"{API_PREFIX}/projects/{project}/packages/{package}/findings")
    assert response.status_code == 200, response.text
    first = next(
        item
        for item in response.json()["items"]
        if item["scope_row_candidate_id"] == str(anchors[0])
    )
    assert first["row_location"] == rows[0].row_location.model_dump(mode="json")
    chain = client.get(
        f"{API_PREFIX}/projects/{project}/packages/{package}/findings/{first['id']}/chain"
    )
    assert chain.status_code == 200, chain.text
    assert chain.json()["scope_row_candidate_id"] == str(anchors[0])
    assert chain.json()["row_location"] == first["row_location"]


def test_every_cursor_page_is_complete_over_fifty_findings(session: Session) -> None:
    from tests.api.test_findings_query import _finding as finding_for
    from tests.api.test_findings_query import _snapshot
    from verdict.outcomes import Outcome

    project = uuid4()
    revision = _revision(session, project)
    snapshot = _snapshot(session)
    expected = {
        str(finding_for(session, revision, snapshot, Outcome.NOT_FOUND).id) for _ in range(55)
    }
    session.commit()
    client = _client(session, project)
    url = f"{API_PREFIX}/projects/{project}/packages/{revision.package_id}/findings"
    first = client.get(url).json()
    assert len(first["items"]) == 50
    second = client.get(url, params={"cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    assert {item["id"] for item in first["items"] + second["items"]} == expected


def test_readiness_counts_current_findings_and_decrements_only_after_actions(
    session: Session,
) -> None:
    finding = result(session, "NOT_FOUND")
    revision = session.get(PackageRevision, finding.package_revision_id)
    package = session.get(Package, revision.package_id)
    session.commit()
    client = _client(session, package.project_id)
    url = f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}/approval-readiness"
    body = client.get(url).json()
    assert not body["can_approve"]
    assert body["blocking_findings"] == 2
    review = open_session(session, package_revision_id=revision.id, reviewer="reviewer")
    for item in session.scalars(select(Finding).where(Finding.package_revision_id == revision.id)):
        record_action(
            session,
            review_session_id=review.id,
            finding_id=item.id,
            action=ReviewActionKind.DISMISS,
            actor="reviewer",
            note="Not checkable; detail requested",
        )
    session.commit()
    body = client.get(url).json()
    assert body["can_approve"] and body["blocking_findings"] == 0


def test_multiple_rows_have_distinct_stable_labels(session: Session) -> None:
    from app.models import ObservationCandidate

    project, package, anchors = _package_rows(session)
    anchor = session.get(ObservationCandidate, anchors[0])
    other = ObservationCandidate(
        document_version_id=anchor.document_version_id,
        page_id=anchor.page_id,
        extraction_run_id=anchor.extraction_run_id,
        raw_text="synthetic second row",
        polygon=anchor.polygon,
        coordinate_space=anchor.coordinate_space,
        ambiguity_flags=["slot-reader", "slot:0", "row-rank:3", "row-slot-count:1"],
    )
    session.add(other)
    session.flush()
    rows = list_slot_rows(reviewer(), session, project, package).rows
    labels = [row.label for row in rows if row.page_number == 1]
    assert labels == ["Countertop row 1.1 on page 1", "Countertop row 1.2 on page 1"]
    assert [row.label for row in list_slot_rows(reviewer(), session, project, package).rows] == [
        row.label for row in rows
    ]


def test_pre_read_hold_explains_the_actual_row_hold(session: Session) -> None:
    project, package, _ = _package_rows(
        session, unsealed_all=True, extra_hold="row-hold:counter-break"
    )
    rows = list_slot_rows(reviewer(), session, project, package).rows
    assert "tall appliance" in rows[0].held_reason
    assert "only one reader" not in rows[0].held_reason
