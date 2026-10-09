"""Publish all three files for one frozen approval; never run a check or a model."""

from __future__ import annotations

from io import BytesIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.visual_countertops import _countertop_results_for_revision
from app.models.package import PackageRevision
from app.models.review import Approval
from app.models.signed_exports import (
    ApprovalExportBundle,
    ApprovalExportFailure,
    ApprovalExportSnapshot,
)
from app.models.verdicts import OutputArtifact, OutputArtifactKind
from app.review.signed_exports import SignedExportRefused, finding_rows, load_snapshot
from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
from reports.spreadsheet import (
    StoredFinding,
    WorkbookSignoff,
    write_stored_workbook,
)
from storage.store import ArtifactStore
from workflow.redline_outputs import render_evidence_grounded_redline


def record_publication_failure(
    factory: sessionmaker[Session], approval_id: UUID, error: Exception
) -> None:
    """After rollback, record failure separately without leaking an exception's source bytes."""
    with factory.begin() as db:
        snapshot = db.scalar(
            select(ApprovalExportSnapshot).where(ApprovalExportSnapshot.approval_id == approval_id)
        )
        if snapshot is not None:
            db.add(
                ApprovalExportFailure(
                    snapshot_id=snapshot.id,
                    package_revision_id=snapshot.package_revision_id,
                    error_type=type(error).__name__,
                )
            )


def generate_signed_outputs(
    db: Session, store: ArtifactStore, approval_id: UUID
) -> ApprovalExportBundle:
    """One transaction publishes the complete bundle. Retries keep its approval and facts."""
    snapshot = db.scalar(
        select(ApprovalExportSnapshot)
        .where(ApprovalExportSnapshot.approval_id == approval_id)
        .with_for_update()
    )
    if snapshot is None:
        raise SignedExportRefused("request the signed exports for this approval first")
    payload = load_snapshot(db, snapshot)
    existing = db.scalar(
        select(ApprovalExportBundle).where(ApprovalExportBundle.snapshot_id == snapshot.id)
    )
    if existing is not None:
        return existing
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise SignedExportRefused("approval not found")
    rows = finding_rows(db, approval)
    from workflow.stages import WORKBOOK_MEDIA_TYPE, _delta_text

    findings = tuple(
        StoredFinding(
            rule_id=definition.rule_id,
            outcome=finding.outcome,
            severity=finding.severity,
            snapshot_id=rule.snapshot_id,
            engine_version=run.engine_version,
            trace=finding.trace,
            reason=finding.reason,
            delta=_delta_text(finding),
            variant=finding.variant,
            notes=None if finding.notes is None else tuple(finding.notes),
            scope_label=finding.scope_label,
        )
        for finding, run, rule, definition in rows
    )
    revision = db.get(PackageRevision, payload.package_revision_id)
    if revision is None:
        raise SignedExportRefused("approved revision is not available")
    countertop_results = _countertop_results_for_revision(db, revision.package_id, revision)
    countertop_labels = tuple(
        f"Page {item.page_number} — {item.label}" for item in countertop_results.items
    )
    redline = render_evidence_grounded_redline(
        db,
        store,
        package_revision_id=payload.package_revision_id,
        findings=tuple((f, r, d.rule_id, s.snapshot_id) for f, r, s, d in rows),
        changed_values=payload.changed_values,
        signed_review=payload.review,
        countertop_labels=countertop_labels,
    )
    if redline.artifact is None:
        raise SignedExportRefused(f"signed drawing unavailable: {redline.reason}")
    pdf = write_findings_pdf(
        FindingsPdfInput(
            package_revision_id=payload.package_revision_id,
            revision_number=payload.revision_number,
            vendor=payload.vendor,
            findings=findings,
            countertop_results=countertop_results.items,
            changed_values=payload.changed_values,
            signed_review=payload.review,
            pages_without_countertop=countertop_results.pages_without_countertop,
        )
    )
    workbook = write_stored_workbook(
        findings,
        countertop_results=countertop_results.items,
        pages_without_countertop=countertop_results.pages_without_countertop,
        changed_values=payload.changed_values,
        signed_review=payload.review,
        signoff=WorkbookSignoff(
            approved_by=payload.review.approved_by,
            approved_at=payload.review.approved_at.isoformat(),
        ),
    )
    import hashlib

    artifacts = {}
    for kind, content, media, suffix in (
        (OutputArtifactKind.FINDINGS_PDF, pdf, "application/pdf", "pdf"),
        (OutputArtifactKind.FINDINGS_WORKBOOK, workbook, WORKBOOK_MEDIA_TYPE, "xlsx"),
        (
            OutputArtifactKind.REDLINE,
            store.get(redline.artifact.key).read(),
            "application/pdf",
            "pdf",
        ),
    ):
        sha = hashlib.sha256(content).hexdigest()
        key = f"signed-outputs/{approval_id}/{kind.value}/{sha}.{suffix}"
        saved = store.put(key, BytesIO(content), content_type=media)
        if saved.sha256 != sha:
            raise SignedExportRefused("signed output storage hash does not match")
        artifact = OutputArtifact(
            package_revision_id=payload.package_revision_id,
            kind=kind.value,
            storage_key=saved.key,
            sha256=sha,
            media_type=media,
            findings=len(findings),
            size=len(content),
        )
        db.add(artifact)
        artifacts[kind] = artifact.id
    db.flush()
    load_snapshot(db, snapshot)
    bundle = ApprovalExportBundle(
        snapshot_id=snapshot.id,
        package_revision_id=payload.package_revision_id,
        pdf_id=artifacts[OutputArtifactKind.FINDINGS_PDF],
        workbook_id=artifacts[OutputArtifactKind.FINDINGS_WORKBOOK],
        redline_id=artifacts[OutputArtifactKind.REDLINE],
    )
    db.add(bundle)
    db.flush()
    return bundle
