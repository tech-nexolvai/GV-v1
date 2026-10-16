"""Publish all three files for one frozen approval; never run a check or a model.

Everything the files print comes from the approval's frozen snapshot or from immutable rows the
snapshot re-verifies (findings, their operands and evidence, the uploaded drawings): the countertop
section too, since #1182.
"""

from __future__ import annotations

from io import BytesIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.review import Approval
from app.models.signed_exports import (
    ApprovalExportBundle,
    ApprovalExportFailure,
    ApprovalExportSnapshot,
)
from app.models.verdicts import OutputArtifact, OutputArtifactKind
from app.review.signed_exports import (
    SignedExportRefused,
    finding_rows,
    frozen_countertop_results,
    load_snapshot,
)
from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
from reports.redline import ArchitectCompare, LabelLayout, ReviewerMark
from reports.spreadsheet import (
    StoredFinding,
    WorkbookSignoff,
    write_stored_workbook,
)
from storage.store import ArtifactStore
from workflow.redline_outputs import (
    ai_review_input,
    architect_compare_input,
    compare_for_render,
    redline_marks,
    render_evidence_grounded_redline,
)


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


def frozen_compare_input(
    payload: object,
) -> tuple[ArchitectCompare | None, tuple[ReviewerMark, ...]]:
    """What the signed cloud PDF compares with the architect, from the sign-off snapshot only:
    the AI page review frozen there (`ai_review`) when it has rows, else the direct compare frozen
    there (`architect_compare`: its items and counts); never both. A snapshot with neither is
    drawn exactly as before."""
    frozen_compare = getattr(payload, "architect_compare", None)
    return compare_for_render(
        ai_review_input(getattr(payload, "ai_review", None)),
        lambda: (
            (None, ())
            if frozen_compare is None
            else architect_compare_input(frozen_compare.items, frozen_compare.counts)
        ),
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
    # The countertop section is the one frozen at sign-off, never rebuilt from the live database
    # (#1182): every page of one signed document says what was approved.
    countertop_results = frozen_countertop_results(db, payload)
    countertop_labels = tuple(
        f"Page {item.page_number} — {item.label}" for item in countertop_results.items
    )
    # A format-2 snapshot (#1363) carries a person's marks and the results printed NOT CHECKED;
    # a format-1 snapshot carries neither, and its cloud PDF is drawn exactly as before.
    marks = payload.reviewer_marks
    architect, agreed = frozen_compare_input(payload)
    shown_marks = None if marks is None else redline_marks(marks)
    if agreed:
        shown_marks = (*(shown_marks or ()), *agreed)
    # Format 3 (QA 2026-10-16): tags only on the drawing, and a cover that lists what is clouded
    # as wrong and the architect compare; the old compare's leftover rows are left out, and said
    # once, while it was switched off at sign-off. Formats 1 and 2 are drawn exactly as before.
    tags = payload.format_version >= 3
    drawn_rows = rows
    cover = None
    if tags:
        from reports.redline import review_cover_for
        from workflow.draft_cloud_render import OLD_COMPARE_RULE, old_compare_left_out_words

        left_out = frozenset({OLD_COMPARE_RULE}) if payload.old_compare_off else frozenset()
        drawn_rows = [row for row in rows if row[3].rule_id not in left_out]
        hidden = len(rows) - len(drawn_rows)
        cover = review_cover_for(
            architect,
            left_out_rules=left_out,
            left_out_note=old_compare_left_out_words(hidden) if hidden else None,
        )
    redline = render_evidence_grounded_redline(
        db,
        store,
        package_revision_id=payload.package_revision_id,
        findings=tuple((f, r, d.rule_id, s.snapshot_id) for f, r, s, d in drawn_rows),
        changed_values=payload.changed_values,
        signed_review=payload.review,
        countertop_labels=countertop_labels,
        reviewer_marks=shown_marks,
        not_checked=frozenset(() if marks is None else marks.not_checked_finding_ids),
        architect=architect,
        label_layout=LabelLayout.TAGS if tags else LabelLayout.AS_BEFORE,
        review_cover=cover,
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
            rows_not_checked=countertop_results.rows_not_checked,
            drawings_not_read=countertop_results.drawings_not_read,
            reading_note=countertop_results.reading_note,
        )
    )
    workbook = write_stored_workbook(
        findings,
        countertop_results=countertop_results.items,
        pages_without_countertop=countertop_results.pages_without_countertop,
        rows_not_checked=countertop_results.rows_not_checked,
        drawings_not_read=countertop_results.drawings_not_read,
        reading_note=countertop_results.reading_note,
        changed_values=payload.changed_values,
        signed_review=payload.review,
        signoff=WorkbookSignoff(
            approved_by=payload.review.approved_by,
            approved_at=payload.review.approved_at.isoformat(),
            revision_number=payload.revision_number,
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
