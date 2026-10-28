"""The handed-over record is the approval's record, never a stale or partial report."""

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CheckRun, Finding, Page
from app.models.review import Approval, ApprovedFinding, ReviewActionKind
from app.review.session import open_session, record_action
from app.review.signed_exports import request_signed_exports
from storage.local import LocalStore
from tests.api.test_v1_loop import PROJECT, _uploaded
from tests.api.test_v1_loop import client as client_fixture
from tests.api.test_v1_loop import session as session_fixture
from tests.api.test_v1_loop import store as store_fixture
from tests.review.test_session import _finding
from workflow.signed_outputs import generate_signed_outputs

session = session_fixture
store = store_fixture
client = client_fixture


def _legacy(db: Session, storage: LocalStore) -> tuple[Any, Approval, list[Finding]]:
    package_id, revision, _ = _uploaded(db, storage)
    revision.state = "APPROVED"
    from app.models import DocumentVersion

    version = db.scalars(select(DocumentVersion)).one()
    db.add(
        Page(
            document_version_id=version.id,
            index=0,
            content_hash="a" * 64,
            width_pt=200,
            height_pt=100,
            rotation=0,
            has_vector_text=True,
            media_box=["0", "0", "200", "100"],
            crop_box=["0", "0", "200", "100"],
        )
    )
    failed = _finding(db, revision)
    findings = [failed]
    run = db.get(CheckRun, failed.check_run_id)
    assert run is not None
    for _ in range(8):
        other = CheckRun(
            package_revision_id=revision.id,
            rule_snapshot_id=run.rule_snapshot_id,
            engine_version=run.engine_version,
        )
        db.add(other)
        db.flush()
        finding = Finding(
            check_run_id=other.id,
            package_revision_id=revision.id,
            outcome="NOT_FOUND",
            severity="FLAG",
            trace={},
            parameter_set_versions={},
            reason="Synthetic source information not supplied",
        )
        db.add(finding)
        db.flush()
        findings.append(finding)
    review = open_session(db, package_revision_id=revision.id, reviewer="ana@example.com")
    for finding in findings:
        record_action(
            db,
            review_session_id=review.id,
            finding_id=finding.id,
            action=(
                ReviewActionKind.CONFIRM if finding.outcome == "FAIL" else ReviewActionKind.DISMISS
            ),
            actor="ana@example.com",
            note="Not checkable: synthetic source missing",
        )
    approval = Approval(package_revision_id=revision.id, approved_by="ana@example.com")
    db.add(approval)
    db.flush()
    db.add_all(
        ApprovedFinding(approval_id=approval.id, package_revision_id=revision.id, finding_id=f.id)
        for f in findings
    )
    review.completed_at = approval.created_at
    db.commit()
    return package_id, approval, findings


def test_later_approval_cannot_download_an_earlier_bundle(
    session: Session, store: LocalStore, client: Any
) -> None:
    package_id, approval, findings = _legacy(session, store)
    request_signed_exports(session, approval.id)
    generate_signed_outputs(session, store, approval.id)
    later = Approval(
        package_revision_id=approval.package_revision_id,
        approved_by="second reviewer",
        created_at=approval.created_at + timedelta(seconds=1),
    )
    session.add(later)
    session.flush()
    session.add_all(
        ApprovedFinding(
            approval_id=later.id, finding_id=f.id, package_revision_id=approval.package_revision_id
        )
        for f in findings
    )
    session.commit()
    response = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/report.pdf")
    assert response.status_code == 409
    assert "not been requested" in response.text
