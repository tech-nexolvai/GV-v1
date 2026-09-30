"""Build the evidence-grounded internal redline an output stage may record.

The redline has a deliberately narrower placement rule than an ordinary report: a mark exists
only when the *stored finding* links through its sealed ``VerdictInput`` to a typed canonical observation
whose stored polygon belongs to a recorded page transform.  Raw candidates, trace display text,
and reviewer-entered values have no route into this module.  They may still be described in the
summary page, but they cannot acquire a plausible-looking location by accident.

This is presentation after the deterministic check has completed.  It reads the verdict and its
evidence links; it never calls the rule engine or changes a finding.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from uuid import UUID

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    PackageRevisionDocument,
    Page,
    SourceArtifact,
)
from app.models.evidence import CanonicalObservation
from app.models.verdicts import CheckRun, VerdictInput
from app.models.verdicts import Finding as FindingRow
from evidence.coordinates import PageTransform
from reports.redline import (
    RedlineFinding,
    RedlinePackage,
    RedlinePage,
    ReportMode,
    render_redline,
)
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
from storage.store import ArtifactStore, StoredArtifact
from verdict.outcomes import Outcome, Severity, is_decision

__all__ = ["RedlineOutput", "render_evidence_grounded_redline"]


@dataclass(frozen=True, slots=True)
class RedlineOutput:
    """The one optional redline artifact and an honest reason when none can be placed."""

    artifact: StoredArtifact | None
    reason: str | None


def render_evidence_grounded_redline(
    session: Session,
    store: ArtifactStore,
    *,
    package_revision_id: UUID,
    findings: Sequence[tuple[FindingRow, CheckRun, str, str]],
) -> RedlineOutput:
    """Render one internal redline only when a finding has a typed, stored location.

    ``findings`` is the current output-stage query: the immutable row, the check run that owns its
    engine version, and the human rule id.  A caller cannot pass a raw candidate or a hand-made
    evidence reference, which keeps redline placement on the same evidence boundary as operands.
    """
    if not findings:
        return RedlineOutput(None, "this revision has no live findings")

    references = _typed_references(session, findings)
    if not any(references.values()):
        return RedlineOutput(
            None,
            "no live finding is backed by a typed canonical reading with a recorded location",
        )

    package = _source_package(session, store, package_revision_id)
    if package is None:
        return RedlineOutput(
            None,
            "the source drawing pages or their recorded transforms are unavailable for redline placement",
        )

    rendered = tuple(
        _stored_finding(row, run, rule_id, snapshot_id, references.get(row.id, ()))
        for row, run, rule_id, snapshot_id in findings
    )
    # Internal mode is intentional: generation precedes approval.  The normal artifact-download
    # gate still refuses any output before sign-off, and vendor publication remains the separately
    # approved route in reports.publication.
    return RedlineOutput(render_redline(package, rendered, ReportMode.INTERNAL, store), None)


def _typed_references(
    session: Session, findings: Sequence[tuple[FindingRow, CheckRun, str, str]]
) -> dict[UUID, tuple[str, ...]]:
    """Return only typed canonical locations actually linked to each stored finding.

    The join through the sealed ``VerdictInput`` is the guard. A trace's ``evidence_ref`` is display data
    and might describe a literal/manual input; drawing a mark from it would be a placement claim
    without a linked typed reading.
    """
    ids = [row.id for row, _, _, _ in findings]
    page_by_id = {
        page.id: page
        for page in session.scalars(
            select(Page)
            .join(CanonicalObservation, CanonicalObservation.page_id == Page.id)
            .join(VerdictInput, VerdictInput.canonical_observation_id == CanonicalObservation.id)
            .join(CheckRun, CheckRun.id == VerdictInput.check_run_id)
            .join(FindingRow, FindingRow.check_run_id == CheckRun.id)
            .where(FindingRow.id.in_(ids))
        ).unique()
    }
    grouped: dict[UUID, list[str]] = defaultdict(list)
    rows = session.execute(
        select(FindingRow.id, CanonicalObservation)
        .join(
            CheckRun,
            FindingRow.check_run_id == CheckRun.id,
        )
        .join(VerdictInput, VerdictInput.check_run_id == CheckRun.id)
        .join(
            CanonicalObservation,
            VerdictInput.canonical_observation_id == CanonicalObservation.id,
        )
        .where(FindingRow.id.in_(ids))
        .order_by(FindingRow.id, CanonicalObservation.created_at, CanonicalObservation.id)
    ).all()
    for finding_id, observation in rows:
        page = page_by_id.get(observation.page_id)
        if (
            page is None
            or page.document_version_id != observation.document_version_id
            or observation.coordinate_space != "stored"
            or not observation.semantic_type
        ):
            continue
        # The model check constraint protects this in normal operation.  Re-validating at the
        # output boundary means a corrupt/manual database row becomes an unplaced summary item,
        # never a box around an invented region.
        try:
            from rules.semantic_types import SemanticType

            SemanticType(observation.semantic_type)
        except ValueError:
            continue
        grouped[finding_id].append(
            json.dumps(
                {
                    "document_version_id": str(observation.document_version_id),
                    "page": page.index,
                    "polygon": observation.polygon,
                    "space": observation.coordinate_space,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return {finding_id: tuple(dict.fromkeys(refs)) for finding_id, refs in grouped.items()}


def _source_package(
    session: Session, store: ArtifactStore, package_revision_id: UUID
) -> RedlinePackage | None:
    """Assemble the revision's immutable PDFs and page transforms in one stable source package."""
    sources = session.execute(
        select(DocumentVersion.id, DocumentVersion.sha256, SourceArtifact.storage_key)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .join(Document, Document.id == PackageRevisionDocument.document_id)
        .join(SourceArtifact, SourceArtifact.id == DocumentVersion.source_artifact_id)
        .where(
            PackageRevisionDocument.package_revision_id == package_revision_id,
            Document.kind.in_((DocumentKind.ARCHITECTURAL.value, DocumentKind.SHOP.value)),
        )
        .order_by(PackageRevisionDocument.created_at, DocumentVersion.id)
    ).all()
    if not sources:
        return None

    pages_by_version: dict[UUID, list[Page]] = defaultdict(list)
    for page in session.scalars(
        select(Page)
        .where(Page.document_version_id.in_([version_id for version_id, _, _ in sources]))
        .order_by(Page.document_version_id, Page.index)
    ):
        pages_by_version[page.document_version_id].append(page)

    writer = PdfWriter()
    redline_pages: list[RedlinePage] = []
    source_index = 0
    for version_id, digest, key in sources:
        try:
            source = store.get(key).read()
        except (ArtifactCorrupt, IntegrityRecordMissing, OSError):
            return None
        if hashlib.sha256(source).hexdigest() != digest:
            return None
        try:
            reader = PdfReader(BytesIO(source))
        except PdfReadError:
            return None
        # A PDF with no pages cannot establish a drawing location.  It is harmless to carry no
        # pages into the source package; another valid document can still produce a bounded redline.
        writer.append(reader)
        for page in pages_by_version.get(
            version_id, ()
        ):  # Page rows may be absent after an early stop.
            if page.index >= len(reader.pages):
                continue
            transform = _page_transform(page)
            if transform is None:
                continue
            redline_pages.append(
                RedlinePage(
                    document_version_id=version_id,
                    page=page.index,
                    source_index=source_index + page.index,
                    transform=transform,
                )
            )
        source_index += len(reader.pages)

    if not redline_pages or source_index == 0:
        return None
    output = BytesIO()
    writer.write(output)
    return RedlinePackage(
        package_revision_id=package_revision_id,
        source_pdf=output.getvalue(),
        pages=tuple(redline_pages),
    )


def _page_transform(page: Page) -> PageTransform | None:
    """Rebuild only the transform extraction recorded; never infer a crop box."""
    if page.media_box is None or page.crop_box is None:
        return None
    try:
        media = tuple(Decimal(value) for value in page.media_box)
        crop = tuple(Decimal(value) for value in page.crop_box)
        if len(media) != 4 or len(crop) != 4:
            return None
        return PageTransform(
            dpi=72,
            rotation=page.rotation,
            media_box=(media[0], media[1], media[2], media[3]),
            crop_box=(crop[0], crop[1], crop[2], crop[3]),
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def _stored_finding(
    row: FindingRow,
    run: CheckRun,
    rule_id: str,
    snapshot_id: str,
    references: Iterable[str],
) -> RedlineFinding:
    """Present the recorded verdict without parsing its display text back into a value."""
    outcome = Outcome(row.outcome)
    trace_reason = row.trace.get("reason")
    reason = (
        row.reason
        or (trace_reason if isinstance(trace_reason, str) else None)
        or "No reason was recorded."
    )
    return RedlineFinding(
        rule_id=rule_id,
        outcome=outcome,
        severity=Severity(row.severity),
        reason=reason,
        snapshot_id=snapshot_id,
        engine_version=run.engine_version,
        # The exact calculation is already retained in the structured finding/report. The redline
        # never rebuilds an operand or intermediate from display text just to repeat a number.
        calculation_available=is_decision(outcome),
        evidence_refs=tuple(references),
    )
