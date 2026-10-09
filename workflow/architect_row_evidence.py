"""The vendor-vs-architect check's two exact lists for one countertop row (CT-ARCH-WIDTH-001, #1054).

`workflow/architect_row_plan.py` decides whether a row has anything to compare and which pairs. This
module seals both sides of each pair:

* **The architect's value** comes from the architect's own text, read by code (#1052): a candidate
  from the `architect-text` route, unheld, with a value that its printed text reads back to exactly,
  no qualifier word printed beside it, both ends on the drawn casework outline, inside a drawing
  whose role is confirmed as the architect's (`app/evidence/sides.py`). Never the vendor-ink
  fallback, never a reviewer's coloured markup, never millimetres. It is sealed through the evidence
  gate as one exact reading plus its drawn-length witness (`CorroborationLane.DRAWN_LENGTH`).
* **The vendor's value** is the row's width exactly as the width check (CT-WIDTH-001) seals it:
  `workflow.slot_row_evidence.vendor_row_operands`, the same function, not a copy.

Nothing here decides pass or fail; the rule's exact arithmetic does.

Source: issue #1054 · Verification: `tests/workflow/test_architect_row_evidence.py`
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import SYSTEM_ACTOR, AuditCategory, emit
from app.evidence.sides import ReadingSides, reading_transform
from app.models.document import Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun
from evidence.canonical import Authority, CorroborationLane, EvidenceStatus
from evidence.canonical import CanonicalObservation as DomainCanonicalObservation
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.gate import GateRefusal, seal
from evidence.polygon import Polygon
from extraction.architect.labels import read_label
from units.measurement import Measurement, Unit
from verdict.operands import VerdictOperand
from vocabulary.semantic_types import DocumentRole, SemanticType
from workflow.architect_pairing_records import ARCHITECT_EXTRACTOR
from workflow.architect_row_plan import (
    ArchitectRowPlan,
    Disposition,
    pair_label,
)
from workflow.slot_row_evidence import vendor_row_operands
from workflow.slot_row_scope import SlotRow

__all__ = [
    "ArchitectRowOperands",
    "architect_candidate_refusal",
    "architect_row_operands",
]


@dataclass(frozen=True, slots=True)
class ArchitectRowOperands:
    """The two lists the rule compares, or why they could not be built."""

    eligible: bool
    reason: str | None
    operands: dict[str, VerdictOperand]


def architect_row_operands(
    session: Session, row: SlotRow, plan: ArchitectRowPlan
) -> ArchitectRowOperands:
    """Seal both sides of every compared pair, or say which one could not be used."""
    if plan.disposition is not Disposition.COMPARE or not plan.pairs:
        return ArchitectRowOperands(False, plan.reason, {})
    vendor = vendor_row_operands(session, row)
    if not vendor.eligible:
        return ArchitectRowOperands(
            False,
            f"The vendor's row is not ready to compare: {vendor.reason or 'review this row.'}",
            {},
        )
    operands: dict[str, VerdictOperand] = {}
    architect_values: list[Measurement] = []
    vendor_values: list[Measurement] = []
    human_vendor = False
    for pair in plan.pairs:
        label = pair_label(pair.kind, pair.vendor_slot)
        semantic = (
            SemanticType.COUNTERTOP_OVERALL_WIDTH
            if pair.vendor_slot is None
            else SemanticType.COUNTERTOP_PIECE_WIDTH
        )
        architect = _architect_operand(
            session, row, pair.architect_candidate_id, semantic=semantic, name=pair.architect_name
        )
        if isinstance(architect, str):
            return ArchitectRowOperands(
                False,
                f"The architect's dimension paired with the vendor's {label} cannot be used: "
                f"{architect} The reviewer decides.",
                {},
            )
        source_name = (
            "countertop_width" if pair.vendor_slot is None else f"piece_widths[{pair.vendor_slot}]"
        )
        vendor_operand = vendor.operands.get(source_name)
        if vendor_operand is None or not isinstance(vendor_operand.value, Measurement):
            return ArchitectRowOperands(
                False, f"The vendor's {label} is not sealed for this row.", {}
            )
        if vendor_operand.value.unit is not Unit.INCH:
            return ArchitectRowOperands(
                False, f"The vendor's {label} is not in inches; inches decide.", {}
            )
        assert isinstance(architect.value, Measurement)
        operands[pair.architect_name] = architect
        operands[pair.vendor_name] = replace(vendor_operand, name=pair.vendor_name)
        architect_values.append(architect.value)
        vendor_values.append(vendor_operand.value)
        human_vendor = human_vendor or vendor_operand.status is EvidenceStatus.HUMAN_CONFIRMED
    decision = row.decision
    operands["architect_widths"] = VerdictOperand(
        name="architect_widths",
        value=tuple(architect_values),
        status=EvidenceStatus.CORROBORATED,
        source=DocumentRole.ARCH.value,
        # A tuple has one origin per member; the named per-pair operands above carry each.
        evidence_observation_id=None,
    )
    operands["vendor_widths"] = VerdictOperand(
        name="vendor_widths",
        value=tuple(vendor_values),
        status=EvidenceStatus.HUMAN_CONFIRMED if human_vendor else EvidenceStatus.CORROBORATED,
        source=DocumentRole.SHOP.value,
        evidence_observation_id=None,
        row_review_decision_id=(
            str(decision.id) if human_vendor and decision is not None else None
        ),
    )
    return ArchitectRowOperands(True, None, operands)


def _architect_operand(
    session: Session,
    row: SlotRow,
    candidate_id: UUID,
    *,
    semantic: SemanticType,
    name: str,
) -> VerdictOperand | str:
    """The architect's printed value, sealed — or why it may not be used, in plain words."""
    candidate = session.get(ObservationCandidate, candidate_id)
    if candidate is None:
        return "the pairing names a dimension that is not stored."
    refusal = architect_candidate_refusal(session, row, candidate)
    if refusal is not None:
        return refusal
    observation = _architect_canonical(session, candidate, semantic)
    if isinstance(observation, str):
        return observation
    operand = seal(_domain(session, observation), name)
    if isinstance(operand, GateRefusal):
        return f"{operand.detail}."
    return replace(operand, evidence_observation_id=str(observation.id))


def architect_candidate_refusal(
    session: Session, row: SlotRow | None, candidate: ObservationCandidate
) -> str | None:
    """Why this candidate is not an architect's value this check may use; `None` when it is.

    Also asked by `app/verdicts/record.py`'s row guard (with `row=None`, which checks the page
    itself), so the two cannot disagree about what an architect operand is.
    """
    run = session.get(ExtractionRun, candidate.extraction_run_id)
    flags = set(candidate.ambiguity_flags or ())
    if run is None or run.extractor != ARCHITECT_EXTRACTOR or "architect-reader" not in flags:
        return "it was not read from the architect's own text by code."
    if "reviewer-markup" in flags or any(flag.startswith("ink:") for flag in flags):
        return "it is not the architect's black text."
    if row is not None and (
        candidate.page_id != row.anchor.page_id
        or candidate.document_version_id != row.anchor.document_version_id
    ):
        return "it is on a different sheet from the vendor's row."
    held = next(
        (flag.removeprefix("arch-held:") for flag in flags if flag.startswith("arch-held:")), None
    )
    if held is not None:
        return f"it is held ({held})."
    if candidate.value_numerator is None or candidate.value_denominator is None:
        return "it has no usable value."
    if candidate.unit != Unit.INCH.value or candidate.value_denominator <= 0:
        return "it is not in inches; inches decide."
    qualifiers = sorted(
        flag.removeprefix("arch-qualifier:") for flag in flags if flag.startswith("arch-qualifier:")
    )
    if qualifiers:
        return f"the architect prints {', '.join(qualifiers)} with it, so it is not a firm width."
    if "arch-ticks-on-outline:yes" not in flags:
        return "its ends do not sit on the drawn casework (a centre line or an unclear end)."
    value = Fraction(candidate.value_numerator, candidate.value_denominator)
    reading = read_label(candidate.raw_text)
    if reading.inches is None or reading.inches != value:
        return "its printed text does not read back to the stored value exactly."
    side = ReadingSides(session).of(candidate, confirmed_views_only=True)
    if side is not DocumentRole.ARCH:
        return "it is not inside a drawing confirmed as the architect's."
    if ReadingSides(session).same_file_as_both_sides(candidate.document_version_id):
        return "the same file was uploaded as both drawings."
    return None


def _architect_canonical(
    session: Session, candidate: ObservationCandidate, semantic: SemanticType
) -> CanonicalObservation | str:
    """The one canonical observation for this architect value, found or created."""
    assert candidate.value_numerator is not None and candidate.value_denominator is not None
    existing = (
        session.execute(
            select(CanonicalObservation)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(
                EvidenceSupportingCandidate.candidate_id == candidate.id,
                CanonicalObservation.semantic_type == semantic.value,
            )
            .distinct()
            .order_by(CanonicalObservation.created_at, CanonicalObservation.id)
        )
        .scalars()
        .all()
    )
    if existing:
        if len(existing) != 1:
            return "it has more than one saved record."
        found = existing[0]
        lanes = set(
            session.scalars(
                select(EvidenceCorroborationLane.lane).where(
                    EvidenceCorroborationLane.canonical_observation_id == found.id
                )
            )
        )
        if (
            found.document_role != DocumentRole.ARCH.value
            or found.document_version_id != candidate.document_version_id
            or found.page_id != candidate.page_id
            or found.value_numerator != candidate.value_numerator
            or found.value_denominator != candidate.value_denominator
            or found.unit != Unit.INCH.value
            or found.status != EvidenceStatus.CORROBORATED.value
            or lanes != {CorroborationLane.DRAWN_LENGTH.value}
        ):
            return "its saved record does not match it."
        return found
    page = session.get(Page, candidate.page_id)
    extraction = session.get(ExtractionRun, candidate.extraction_run_id)
    if page is None or extraction is None:
        return "its page is not stored."
    transform = reading_transform(page, extraction)
    if transform is None:
        return "its page was read before its position could be recorded."
    stored = tuple(
        transform.to_stored(ImagePoint(int(point[0]), int(point[1]))) for point in candidate.polygon
    )
    observation = CanonicalObservation(
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        document_role=DocumentRole.ARCH.value,
        polygon=[[str(point.x), str(point.y)] for point in stored],
        coordinate_space="stored",
        semantic_type=semantic.value,
        value_numerator=candidate.value_numerator,
        value_denominator=candidate.value_denominator,
        unit=Unit.INCH.value,
        status=EvidenceStatus.CORROBORATED.value,
        authority=Authority.AUTHORITATIVE.value,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=observation.id, candidate_id=candidate.id, role="primary"
        )
    )
    session.add(
        EvidenceCorroborationLane(
            canonical_observation_id=observation.id,
            lane=CorroborationLane.DRAWN_LENGTH.value,
        )
    )
    emit(
        session,
        category=AuditCategory.EVIDENCE_QUALIFICATION,
        actor=SYSTEM_ACTOR,
        target_id=observation.id,
        target_type="canonical_observation",
    )
    session.flush()
    return observation


def _domain(session: Session, row: CanonicalObservation) -> DomainCanonicalObservation:
    supporting = tuple(
        str(value)
        for value in session.scalars(
            select(EvidenceSupportingCandidate.candidate_id).where(
                EvidenceSupportingCandidate.canonical_observation_id == row.id
            )
        )
    )
    lanes = tuple(
        CorroborationLane(value)
        for value in session.scalars(
            select(EvidenceCorroborationLane.lane)
            .where(EvidenceCorroborationLane.canonical_observation_id == row.id)
            .order_by(EvidenceCorroborationLane.lane)
        )
    )
    page = session.get(Page, row.page_id)
    if page is None:
        raise ValueError("the observation's page disappeared")
    return DomainCanonicalObservation(
        document_version_id=row.document_version_id,
        document_role=DocumentRole(row.document_role),
        page=page.index,
        polygon=Polygon(
            points=tuple(StoredPoint(x=Decimal(str(x)), y=Decimal(str(y))) for x, y in row.polygon),
            space="stored",
            document_version_id=row.document_version_id,
            page=page.index,
        ),
        semantic_type=SemanticType(row.semantic_type),
        value=Measurement(
            Fraction(row.value_numerator, row.value_denominator), Unit(row.unit), None
        ),
        status=EvidenceStatus(row.status),
        authority=Authority(row.authority),
        supported_by=supporting,
        corroborated_by=lanes,
        conflicts_with=(),
        evidence_crop_uri=row.evidence_crop_uri,
    )
