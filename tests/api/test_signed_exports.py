"""The handed-over record is the approval's record, never a stale or partial report."""

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CheckRun, Finding, OutputArtifact, Page
from app.models.review import Approval, ApprovedFinding, ReviewAction, ReviewActionKind
from app.models.signed_exports import ApprovalExportBundle, ApprovalExportSnapshot
from app.review.session import open_session, record_action
from app.review.signed_exports import canonical, deterministic_facts, request_signed_exports
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


def _text(content: bytes) -> str:
    return " ".join(
        " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(content)).pages).split()
    )


def test_legacy_explicit_request_publishes_all_three_and_never_changes_outcomes(
    session: Session, store: LocalStore, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, approval, findings = _legacy(session, store)
    base = f"/api/v1/projects/{PROJECT}/packages/{package_id}"
    before = canonical(deterministic_facts(session, approval))
    assert client.get(base + "/signed-exports").json()["status"] == "not_requested"
    assert session.scalar(select(ApprovalExportSnapshot)) is None
    # Old artifacts exist and look plausible, but are not an approved publication.
    for kind in ("findings_pdf", "findings_workbook", "redline"):
        saved = store.put(
            "before/" + kind, io.BytesIO(b"before review"), content_type="application/pdf"
        )
        session.add(
            OutputArtifact(
                package_revision_id=approval.package_revision_id,
                kind=kind,
                storage_key=saved.key,
                sha256=saved.sha256,
                media_type="application/pdf",
                findings=9,
            )
        )
    session.commit()
    for endpoint in ("report", "report.pdf", "redline.pdf"):
        assert client.get(base + "/" + endpoint).status_code == 409
    requested = client.post(base + "/signed-exports")
    assert requested.status_code == 202, requested.text
    assert requested.json()["status"] == "preparing"
    from workflow.changed_values import ChangedValues

    monkeypatch.setattr(
        "workflow.changed_values.changed_values_for_revision",
        lambda *args, **kwargs: ChangedValues(
            "available", None, ("LATER SETTING MUST NOT REPLACE PINNED REPORT",), ()
        ),
    )
    for endpoint in ("report", "report.pdf", "redline.pdf"):
        assert client.get(base + "/" + endpoint).status_code == 409
    bundle = generate_signed_outputs(session, store, approval.id)
    session.commit()
    assert generate_signed_outputs(session, store, approval.id).id == bundle.id
    session.commit()
    assert len(session.scalars(select(ApprovalExportBundle)).all()) == 1
    assert client.get(base + "/signed-exports").json()["status"] == "ready"
    for endpoint in ("report.pdf", "redline.pdf"):
        response = client.get(base + "/" + endpoint)
        assert response.status_code == 200, response.text
        text = _text(response.content)
        assert text.count("Not checked: dismissed by reviewer ana@example.com") == 8
        assert "FAIL: confirmed by reviewer ana@example.com" in text
        assert "Reason: Not checkable: synthetic source missing" in text
        assert str(approval.id) in text and "9 findings covered" in text
        assert "Signed off by ana@example.com" in text
        assert "outcome (unchanged): PASS" not in text
        assert "LATER SETTING MUST NOT REPLACE PINNED REPORT" not in text
    response = client.get(base + "/report")
    assert response.status_code == 200
    book = load_workbook(io.BytesIO(response.content))
    review_text = " ".join(str(c.value or "") for row in book["Signed review"] for c in row)
    assert review_text.count("Not checked: dismissed by reviewer ana@example.com") == 8
    assert "FAIL: confirmed by reviewer ana@example.com" in review_text
    assert str(approval.id) in review_text and "9 findings covered" in review_text
    assert "PASS" not in review_text
    assert "LATER SETTING MUST NOT REPLACE PINNED REPORT" not in str(
        [list(sheet.values) for sheet in book]
    )
    assert canonical(deterministic_facts(session, approval)) == before
    assert [f.outcome for f in findings] == ["FAIL"] + ["NOT_FOUND"] * 8
    # A later run cannot change the earlier publication or its pinned contents.
    earlier_pdf = client.get(base + "/report.pdf").content
    run = session.get(CheckRun, findings[0].check_run_id)
    assert run is not None
    run.superseded_at = datetime.now(UTC)
    session.commit()
    assert client.get(base + "/report.pdf").content == earlier_pdf


def test_legacy_ambiguous_history_refuses_plainly(
    session: Session, store: LocalStore, client: Any
) -> None:
    package_id, _approval, findings = _legacy(session, store)
    action = session.scalars(
        select(ReviewAction).where(ReviewAction.finding_id == findings[0].id)
    ).one()
    session.add(
        ReviewAction(
            review_session_id=action.review_session_id,
            package_revision_id=action.package_revision_id,
            finding_id=action.finding_id,
            action="dismiss",
            actor=action.actor,
            note="Second action at an indistinguishable time",
            created_at=action.created_at,
        )
    )
    session.commit()
    response = client.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/signed-exports")
    assert response.status_code == 409
    assert "ambiguous" in response.text
    assert session.scalar(select(ApprovalExportSnapshot)) is None


def test_render_failure_never_publishes_partial_files(
    session: Session, store: LocalStore, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, approval, _ = _legacy(session, store)
    request_signed_exports(session, approval.id)
    session.commit()

    def broken(*args: Any, **kwargs: Any) -> bytes:
        raise OSError("synthetic workbook render failure")

    monkeypatch.setattr("workflow.signed_outputs.write_stored_workbook", broken)
    with pytest.raises(OSError, match="synthetic"):
        generate_signed_outputs(session, store, approval.id)
    session.rollback()
    assert session.scalar(select(ApprovalExportBundle)) is None
    from sqlalchemy.orm import sessionmaker

    from workflow.signed_outputs import record_publication_failure

    record_publication_failure(
        sessionmaker(bind=session.get_bind()), approval.id, OSError("private source text")
    )
    response = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/signed-exports")
    assert response.json()["status"] == "failed"
    assert "private source text" not in response.text
    for endpoint in ("report", "report.pdf", "redline.pdf"):
        assert (
            client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/{endpoint}").status_code
            == 409
        )


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
