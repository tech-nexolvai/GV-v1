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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
from app.review.reviewer_marks import FrozenReviewerMarks
from evidence.coordinates import PageTransform
from reports.redline import (
    NOT_CHECKED,
    ArchitectAnswer,
    ArchitectCompare,
    ArchitectCompareCounts,
    ArchitectDifference,
    RedlineFinding,
    RedlinePackage,
    RedlinePage,
    ReportMode,
    ReviewerMark,
    ReviewerMarkKind,
    VendorClearance,
    render_redline,
)
from reports.signed_review import SignedReview
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
from storage.store import ArtifactStore, StoredArtifact
from verdict.outcomes import Outcome, Severity, is_decision
from workflow.changed_values import ChangedValues

__all__ = [
    "NOT_CHECKED",
    "RedlineOutput",
    "architect_compare_input",
    "redline_marks",
    "render_draft_redline",
    "render_evidence_grounded_redline",
]


def redline_marks(frozen: FrozenReviewerMarks) -> tuple[ReviewerMark, ...]:
    """The frozen marks (`app/review/reviewer_marks.py`) as the renderer draws them (#1363)."""
    return tuple(
        ReviewerMark(
            kind=ReviewerMarkKind(mark.kind),
            words=mark.words,
            subject=mark.subject,
            details=mark.details,
            evidence_ref=mark.place,
            unplaced_reason=mark.unplaced_reason,
        )
        for mark in frozen.marks
    )


def _field(value: object, name: str) -> object:
    """A field of a backend value: a Pydantic model, a dataclass or a plain mapping."""
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _count(counts: object, name: str) -> int:
    value = _field(counts, name)
    return value if isinstance(value, int) else 0


#: A person's answer on a "different" result, as the review records may spell it.
_ANSWERS: dict[str, ArchitectAnswer] = {
    "agree": ArchitectAnswer.AGREED,
    "agreed": ArchitectAnswer.AGREED,
    "vendor_wrong": ArchitectAnswer.AGREED,
    "not_a_difference": ArchitectAnswer.NOT_A_DIFFERENCE,
    "not_different": ArchitectAnswer.NOT_A_DIFFERENCE,
}


def _vendor_place(vendor_ref: object) -> tuple[str | None, str | None]:
    """The vendor dimension's place as a finding's evidence reference, or why there is none."""
    if vendor_ref is None:
        return None, "the vendor's dimension has no recorded place on the drawing"
    version, number, polygon = (
        _field(vendor_ref, "document_version_id"),
        _field(vendor_ref, "page_number"),
        _field(vendor_ref, "polygon"),
    )
    if version is None or not isinstance(number, int) or number < 1 or not polygon:
        return None, "the vendor's dimension's recorded place is incomplete"
    return (
        json.dumps(
            {
                "document_version_id": str(version),
                "page": number - 1,
                "polygon": [[str(x), str(y)] for x, y in polygon],  # type: ignore[attr-defined]
                "space": "stored",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        None,
    )


def architect_compare_input(
    items: Iterable[object], counts: object
) -> tuple[ArchitectCompare | None, tuple[ReviewerMark, ...]]:
    """The direct compare (`app/review/architect_compare.architect_compare_for_revision`'s items
    and counts) as the renderer draws it, and the reviewer marks of a person who agreed.

    `None` when the compare has not run (no item), so nothing about it is printed. A "different"
    result a person agreed with stays clouded and gets the person's mark there (the existing
    reviewer-mark style); one a person said is not a difference is listed, not clouded. Every
    other result is only counted. An answer this does not know is refused, never dropped.
    """
    listed = list(items)
    if not listed:
        return None, ()
    different: list[ArchitectDifference] = []
    marks: list[ReviewerMark] = []
    for item in listed:
        if _field(item, "result") != "different":
            continue
        page, view = _field(item, "page"), _field(item, "view")
        subject = f"Page {page}" + (f", {view}" if isinstance(view, str) and view else "")
        place, reason = _vendor_place(_field(item, "vendor_ref"))
        said = _field(item, "answer")
        answer = None if said is None else _ANSWERS.get(str(getattr(said, "value", said)))
        if said is not None and answer is None:
            raise ValueError(f"unknown answer on an architect compare result: {said!r}")
        who = _field(item, "answered_by")
        difference = ArchitectDifference(
            architect_text=str(_field(item, "architect_text") or "not read"),
            vendor_text=str(_field(item, "vendor_text") or "not read"),
            subject=subject,
            evidence_ref=place,
            unplaced_reason=reason,
            answer=answer,
            answered_by=None if who is None else str(who),
        )
        different.append(difference)
        if answer is ArchitectAnswer.AGREED:
            marks.append(
                ReviewerMark(
                    kind=ReviewerMarkKind.CONFIRMED,
                    words=f"Agreed by {who}: vendor is wrong",
                    subject=f"Compared with the architect, {subject}",
                    details=(difference.words,),
                    evidence_ref=place,
                    unplaced_reason=reason,
                )
            )
    return (
        ArchitectCompare(
            counts=ArchitectCompareCounts(
                same=_count(counts, "same"),
                different=_count(counts, "different"),
                for_a_person=_count(counts, "unpaired") + _count(counts, "unread"),
                by_a_rule=_count(counts, "rule"),
            ),
            different=tuple(different),
        ),
        tuple(marks),
    )


def _printed_not_checked(finding: RedlineFinding) -> RedlineFinding:
    """A result whose check could not run: `❍ NOT CHECKED` and its reason after `NOT CHECKED: `,
    never shown as judged. The stored verdict is not changed."""
    return replace(finding, reason=f"{NOT_CHECKED}: {finding.reason}", not_checked=True)


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
    changed_values: ChangedValues | None = None,
    signed_review: SignedReview | None = None,
    countertop_labels: Sequence[str] = (),
    reviewer_marks: Sequence[ReviewerMark] | None = None,
    not_checked: frozenset[UUID] = frozenset(),
    architect: ArchitectCompare | None = None,
) -> RedlineOutput:
    """Render one internal redline only when a finding has a typed, stored location.

    ``findings`` is the current output-stage query: the immutable row, the check run that owns its
    engine version, and the human rule id.  A caller cannot pass a raw candidate or a hand-made
    evidence reference, which keeps redline placement on the same evidence boundary as operands.
    """
    if not findings:
        return RedlineOutput(None, "this revision has no live findings")

    references = _typed_references(session, findings)
    if not any(references.values()) and signed_review is None:
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
        _printed_not_checked(finding) if row.id in not_checked else finding
        for row, finding in (
            (row, _stored_finding(row, run, rule_id, snapshot_id, references.get(row.id, ())))
            for row, run, rule_id, snapshot_id in findings
        )
    )
    # Internal mode is intentional: generation precedes approval.  The normal artifact-download
    # gate still refuses any output before sign-off, and vendor publication remains the separately
    # approved route in reports.publication.
    return RedlineOutput(
        render_redline(
            package,
            rendered,
            ReportMode.INTERNAL if signed_review is None else ReportMode.VENDOR,
            store,
            changed_values=changed_values,
            clearance=(
                None
                if signed_review is None
                else VendorClearance(
                    approval_id=signed_review.approval_id,
                    approved_by=signed_review.approved_by,
                    approved_at=signed_review.approved_at,
                )
            ),
            signed_review=signed_review,
            countertop_labels=countertop_labels,
            reviewer_marks=reviewer_marks,
            architect=architect,
        ),
        None,
    )


def render_draft_redline(
    session: Session,
    store: ArtifactStore,
    *,
    package_revision_id: UUID,
    findings: Sequence[tuple[FindingRow, CheckRun, str, str]],
    not_checked: frozenset[UUID] = frozenset(),
    changed_values: ChangedValues | None = None,
    reviewer_marks: Sequence[ReviewerMark] | None = None,
    architect: ArchitectCompare | None = None,
) -> RedlineOutput:
    """The draft cloud PDF's drawing pages, before sign-off (#1340): always INTERNAL mode.

    The same placement rule as the signed redline: a mark only from a typed, stored location. Unlike
    it, a draft is made even when nothing can be placed, because its listing pages still account
    for every result, and a reviewer asking "what will be clouded?" deserves that answer too.

    A result in `not_checked` is printed `❍ NOT CHECKED`, with its reason after `NOT CHECKED: `,
    so the draft never shows a check that did not run as if it had been judged. The stored verdict
    is not changed. `reviewer_marks` are the same marks the signed cloud PDF draws (#1363).
    """
    if not findings:
        return RedlineOutput(None, "this revision has no live findings")
    references = _typed_references(session, findings)
    package = _source_package(session, store, package_revision_id)
    if package is None:
        return RedlineOutput(
            None,
            "the source drawing pages or their recorded transforms are unavailable for redline placement",
        )
    rendered = []
    for row, run, rule_id, snapshot_id in findings:
        finding = _stored_finding(row, run, rule_id, snapshot_id, references.get(row.id, ()))
        if row.id in not_checked:
            finding = _printed_not_checked(finding)
        rendered.append(finding)
    return RedlineOutput(
        render_redline(
            package,
            tuple(rendered),
            ReportMode.INTERNAL,
            store,
            changed_values=changed_values,
            reviewer_marks=reviewer_marks,
            architect=architect,
        ),
        None,
    )


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
    comparison_value = row.trace.get("comparison")
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
        scope_label=row.scope_label,
        comparison=comparison_value if isinstance(comparison_value, str) else None,
        notes=tuple(row.notes or ()),
    )
