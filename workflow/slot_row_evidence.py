"""Build exact CT-WIDTH operands from one qualified slot-reader row.

This is deliberately separate from ``part_operands``: the manual run workflow remains the primary
path, and a slot row may not borrow any candidate or reviewer input from another row or page.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import SYSTEM_ACTOR, AuditCategory, emit
from app.evidence.sides import ReadingSides, reading_transform
from app.models.document import Document, DocumentVersion, Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.runs import REUSED_FROM_KEY, ExtractionRun, ModelInvocation
from evidence.canonical import (
    Authority,
    CorroborationLane,
    EvidenceStatus,
)
from evidence.canonical import (
    CanonicalObservation as DomainCanonicalObservation,
)
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.corroborate import independence_key
from evidence.gate import GateRefusal, seal
from evidence.polygon import Polygon
from extraction.slot_reader.bedrock import CLAUDE_SPAN_PROMPT_IDS, parse_stored_reader_answer
from extraction.slot_reader.labels import expand_label, plain_dimension
from extraction.slot_reader.seal import _CLAUDE_PAIR, normalise_text
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit
from verdict.operands import VerdictOperand
from workflow.slot_row_scope import (
    SlotRow,
    SlotRowQualification,
    SlotRowReading,
    candidate_is_sealed,
    candidate_value,
    qualify_slot_row,
)


def _verified_reader_answer(answer: object, expected: Fraction) -> Measurement | None:
    """Accept only an exact, deterministic parse of the stored reader text.

    Stacked/combined are visual description flags, not value authority. A row candidate reaches this
    function only after the slot reader's own seal marks it CORROBORATED; this check independently
    verifies that each stored raw answer parses to the exact sealed inch value.
    """
    if not isinstance(answer, dict):
        return None
    text = answer.get("text")
    if (
        answer.get("belongs") is not True
        or answer.get("readable") is not True
        or answer.get("no_dimension") is not False
        or not isinstance(text, str)
        or not text.strip()
        or not isinstance(answer.get("stacked"), bool)
        or not isinstance(answer.get("combined"), bool)
    ):
        return None
    normalized = normalise_text(text)
    reading = plain_dimension(normalized, allow_explicit_mm=True)
    if reading is None:
        expanded = expand_label(normalized)
        reading = None if expanded is None else expanded.value
    if reading is None or reading.unit is not Unit.INCH or reading.exact != expected:
        return None
    return reading


@dataclass(frozen=True, slots=True)
class SlotRowCheck:
    eligible: bool
    reason: str | None
    wall_config: str | None
    wall_reason: str | None
    wall_provenance: str | None
    operands: dict[str, VerdictOperand]
    observation_ids: tuple[UUID, ...]


def _row_positions(
    row: SlotRow,
) -> tuple[dict[int | None, ObservationCandidate], dict[UUID, str]] | str:
    """The row's width candidates by position, or why the row has none it can use."""
    candidates_by_position: dict[int | None, ObservationCandidate] = {}
    proposal_fields = {
        candidate_id: proposal.field_key for candidate_id, proposal in row.proposals.items()
    }
    for candidate in row.candidates:
        flags = candidate.ambiguity_flags or []
        slot = next(
            (flag.removeprefix("slot:") for flag in flags if flag.startswith("slot:")), None
        )
        if slot == "overall":
            position: int | None = None
        elif slot is not None and slot.isdigit():
            position = int(slot)
        else:
            continue
        field = proposal_fields.get(candidate.id)
        if field not in {
            "SHOP:countertop_overall_width",
            "SHOP:countertop_piece_width",
            "SHOP:cabinet_width",
            "SHOP:filler_width",
        }:
            continue
        if position in candidates_by_position:
            # Duplicate candidates at one position are not a tie-break opportunity.
            return "This row has more than one saved reading for a width position."
        candidates_by_position[position] = candidate
    return candidates_by_position, proposal_fields


def _row_readings(
    row: SlotRow, candidates_by_position: dict[int | None, ObservationCandidate]
) -> tuple[list[SlotRowReading], dict[int | None, Measurement]]:
    """Each position's sealed reading, or the value a reviewer saved against this exact row."""
    decision = row.decision
    typed = {} if decision is None else decision.measurements
    readings: list[SlotRowReading] = []
    values: dict[int | None, Measurement] = {}
    for position in (None, *range(row.piece_count)):
        selected_candidate: ObservationCandidate | None = candidates_by_position.get(position)
        if selected_candidate is not None and candidate_is_sealed(selected_candidate):
            value = candidate_value(selected_candidate)
            if value is None:
                continue
            reading = SlotRowReading(
                candidate_id=selected_candidate.id,
                position=position,
                value_numerator=value.numerator,
                value_denominator=value.denominator,
                unit="in",
                status=selected_candidate.corroboration_status,
                lane=selected_candidate.corroboration_lane,
                flags=frozenset(selected_candidate.ambiguity_flags or ()),
            )
            readings.append(reading)
            values[position] = Measurement(value, Unit.INCH, selected_candidate.raw_text)
            continue
        # A reader suggestion that did not seal is not an operand. It may be shown as a proposal,
        # but only a reviewer value saved against this same row can fill this position.
        key = "countertop_width" if position is None else f"piece_widths:{position}"
        saved = typed.get(key)
        if not isinstance(saved, dict):
            continue
        try:
            numerator = int(saved["numerator"])
            denominator = int(saved["denominator"])
            unit = str(saved["unit"])
            exact = Fraction(numerator, denominator)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if denominator <= 0 or unit != Unit.INCH.value:
            continue
        assert decision is not None
        readings.append(
            SlotRowReading(
                candidate_id=decision.id,
                position=position,
                value_numerator=exact.numerator,
                value_denominator=exact.denominator,
                unit=unit,
                status=EvidenceStatus.HUMAN_CONFIRMED.value,
                lane="HUMAN",
                flags=frozenset({"human-saved-for-row"}),
            )
        )
        values[position] = Measurement(exact, Unit.INCH, str(saved.get("display", "")))
    return readings, values


def _qualify(row: SlotRow, readings: list[SlotRowReading]) -> SlotRowQualification:
    return qualify_slot_row(
        tuple(readings),
        expected_piece_count=row.piece_count,
        held_reason=row.held_reason,
        shop_document=row.held_reason != "This is not the vendor drawing.",
    )


def _seal_row(
    session: Session,
    row: SlotRow,
    candidates_by_position: dict[int | None, ObservationCandidate],
    proposal_fields: dict[UUID, str],
    values: dict[int | None, Measurement],
) -> tuple[dict[str, VerdictOperand], tuple[UUID, ...]] | str:
    """Seal every width of a qualified row, or say which one could not be sealed.

    The one place a vendor row's widths become verdict operands: the width check (CT-WIDTH-001)
    and the architect check (CT-ARCH-WIDTH-001, #1054) both take them from here, so the vendor's
    side of the two checks is the same evidence, sealed the same way.
    """
    decision = row.decision
    operands: dict[str, VerdictOperand] = {}
    observation_ids: list[UUID] = []
    overall_candidate = candidates_by_position.get(None)
    if overall_candidate is not None and candidate_is_sealed(overall_candidate):
        observation = _canonical_for_candidate(
            session,
            overall_candidate,
            proposal_fields[overall_candidate.id],
            semantic=SemanticType.COUNTERTOP_OVERALL_WIDTH,
        )
        if observation is None:
            return "The overall label has missing, conflicting or duplicate saved evidence. Review this row; no reading was selected."
        operand = seal(_domain_observation(session, observation), "countertop_width")
        if isinstance(operand, GateRefusal):
            return operand.detail
        operands["countertop_width"] = replace(operand, evidence_observation_id=str(observation.id))
        observation_ids.append(observation.id)
    else:
        if decision is None or None not in values:
            return "The overall width is not sealed or saved by a reviewer for this row."
        operands["countertop_width"] = _human_operand("countertop_width", values[None], decision.id)

    piece_operands: list[Measurement] = []
    piece_sources: list[tuple[Measurement, UUID | None, UUID | None, EvidenceStatus]] = []
    for position in range(row.piece_count):
        selected_candidate = candidates_by_position.get(position)
        if selected_candidate is None or not candidate_is_sealed(selected_candidate):
            if decision is None or position not in values:
                return f"Piece {position + 1} is not sealed or saved by a reviewer for this row."
            measurement = values[position]
            piece_operands.append(measurement)
            piece_sources.append((measurement, None, decision.id, EvidenceStatus.HUMAN_CONFIRMED))
            continue
        field = proposal_fields[selected_candidate.id]
        observation = _canonical_for_candidate(
            session,
            selected_candidate,
            field,
            semantic=SemanticType.COUNTERTOP_PIECE_WIDTH,
        )
        if observation is None:
            return f"Piece {position + 1} has missing, conflicting or duplicate saved evidence. Review this row; no reading was selected."
        operand = seal(_domain_observation(session, observation), f"piece_width_{position + 1}")
        if isinstance(operand, GateRefusal) or not isinstance(operand.value, Measurement):
            return "A piece width did not pass the evidence gate."
        piece_operands.append(operand.value)
        piece_sources.append((operand.value, observation.id, None, operand.status))
        observation_ids.append(observation.id)

    if not piece_operands or any(not isinstance(value, Measurement) for value in piece_operands):
        return "Every piece width is required for this row."
    human_piece = any(source[3] is EvidenceStatus.HUMAN_CONFIRMED for source in piece_sources)
    decision_id = None if decision is None or not human_piece else str(decision.id)
    operands["piece_widths"] = VerdictOperand(
        name="piece_widths",
        value=tuple(piece_operands),
        status=EvidenceStatus.HUMAN_CONFIRMED if human_piece else EvidenceStatus.CORROBORATED,
        source=DocumentRole.SHOP.value,
        # A tuple has multiple independent origins; the indexed audit operands below preserve each.
        evidence_observation_id=None,
        row_review_decision_id=decision_id,
    )
    for position, (measurement, observation_id, review_id, status) in enumerate(piece_sources):
        name = f"piece_widths[{position}]"
        operands[name] = VerdictOperand(
            name=name,
            value=measurement,
            status=status,
            source=DocumentRole.SHOP.value,
            evidence_observation_id=None if observation_id is None else str(observation_id),
            row_review_decision_id=None if review_id is None else str(review_id),
        )
    return operands, tuple(observation_ids)


def slot_row_check(session: Session, row: SlotRow) -> SlotRowCheck:
    """Return only qualified row operands plus the row's independently established wall choice."""
    positions = _row_positions(row)
    if isinstance(positions, str):
        return SlotRowCheck(False, positions, None, None, None, {}, ())
    candidates_by_position, proposal_fields = positions
    readings, values = _row_readings(row, candidates_by_position)
    qualification = _qualify(row, readings)
    layout, wall_reason, wall_note = _row_wall(row)
    if not qualification.eligible:
        return SlotRowCheck(False, qualification.reason, layout, wall_reason, wall_note, {}, ())
    if layout is None:
        return SlotRowCheck(
            False,
            wall_note or "Choose this row's wall layout.",
            None,
            wall_reason,
            wall_note,
            {},
            (),
        )
    sealed = _seal_row(session, row, candidates_by_position, proposal_fields, values)
    if isinstance(sealed, str):
        return SlotRowCheck(False, sealed, layout, None, wall_note, {}, ())
    operands, observation_ids = sealed
    return SlotRowCheck(True, None, layout, None, wall_note, operands, observation_ids)


@dataclass(frozen=True, slots=True)
class VendorRowOperands:
    """The vendor row's sealed widths, without the wall layout the width check also needs."""

    eligible: bool
    reason: str | None
    operands: dict[str, VerdictOperand]
    """`countertop_width` and `piece_widths[i]`, exactly as the width check seals them."""


def vendor_row_operands(session: Session, row: SlotRow) -> VendorRowOperands:
    """The row's widths sealed exactly as CT-WIDTH-001 seals them, for a check that needs no walls.

    The architect check (#1054) compares the vendor's printed widths with the architect's; the wall
    layout decides the width check's field cut and has nothing to say about that. Everything else
    is the width check's own path: the same row qualification, the same canonical evidence, the same
    gate, the same reviewer-typed values for this row only.
    """
    positions = _row_positions(row)
    if isinstance(positions, str):
        return VendorRowOperands(False, positions, {})
    candidates_by_position, proposal_fields = positions
    readings, values = _row_readings(row, candidates_by_position)
    qualification = _qualify(row, readings)
    if not qualification.eligible:
        return VendorRowOperands(False, qualification.reason, {})
    sealed = _seal_row(session, row, candidates_by_position, proposal_fields, values)
    if isinstance(sealed, str):
        return VendorRowOperands(False, sealed, {})
    return VendorRowOperands(True, None, sealed[0])


def _row_wall(row: SlotRow) -> tuple[str | None, str | None, str | None]:
    source: str | None = None
    layout: str | None = None
    held = False
    reason: str | None = None
    if row.wall_candidate is not None:
        flags = row.wall_candidate.ambiguity_flags or []
        layout_flag = next((flag for flag in flags if flag.startswith("walls-sealed:")), None)
        source_flag = next((flag for flag in flags if flag.startswith("wall-source:")), None)
        held = any(flag.startswith("walls-held:") for flag in flags)
        layout = None if layout_flag is None else layout_flag.removeprefix("walls-sealed:")
        source = None if source_flag is None else source_flag.removeprefix("wall-source:")
        reason = row.wall_candidate.review_reason
    confirmed = None if row.decision is None else row.decision.wall_config
    from workflow.slot_row_scope import effective_row_wall

    value, why = effective_row_wall(
        layout=layout,
        source=source,
        held=held,
        reviewer_confirmed=confirmed,
    )
    provenance = (
        f"Wall layout {value} confirmed by the reviewer for this row ({row.decision.confirmed_by} "
        f"on {row.decision.created_at.isoformat()})."
        if value is not None and row.decision is not None and confirmed is not None
        else (
            f"Wall layout {value} established from vendor drawing clues for this row."
            if value is not None
            else reason
        )
    )
    return value, why, provenance


def _canonical_for_candidate(
    session: Session,
    candidate: ObservationCandidate,
    field: str,
    *,
    semantic: SemanticType,
) -> CanonicalObservation | None:
    expected_fields = (
        {"SHOP:countertop_overall_width"}
        if semantic is SemanticType.COUNTERTOP_OVERALL_WIDTH
        else {
            "SHOP:countertop_piece_width",
            "SHOP:cabinet_width",
            "SHOP:filler_width",
        }
    )
    if (
        field not in expected_fields
        or not candidate_is_sealed(candidate)
        or candidate.value_numerator is None
        or candidate.value_denominator is None
        or candidate.unit != Unit.INCH.value
    ):
        return None
    assert candidate.value_numerator is not None
    assert candidate.value_denominator is not None and candidate.value_denominator > 0
    supports = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == candidate.extraction_run_id
        )
    ).all()
    raw_by_reader: list[tuple[str, ObservationCandidate]] = []
    seen_readers: set[str] = set()
    for support in supports:
        flags = set(support.ambiguity_flags or ())
        if "slot-reader-support" not in flags or f"supports:{candidate.id}" not in flags:
            continue
        reader_flag = next((flag for flag in flags if flag.startswith("reader-id:")), None)
        if reader_flag is None:
            return None
        reader_id = reader_flag.removeprefix("reader-id:")
        seen_readers.add(reader_id)
        # Normalised first, as sealing did (#1090): one reader's `2”` and the other's `2"` are the
        # same label, and the parsers only know the straight marks.
        text = normalise_text(support.raw_text)
        measurement = plain_dimension(text, allow_explicit_mm=True)
        if measurement is None:
            expanded = expand_label(text)
            measurement = None if expanded is None else expanded.value
        if (
            measurement is None
            or measurement.unit is not Unit.INCH
            or measurement.exact != Fraction(candidate.value_numerator, candidate.value_denominator)
            or support.value_numerator != candidate.value_numerator
            or support.value_denominator != candidate.value_denominator
            or support.unit != Unit.INCH.value
            or support.page_id != candidate.page_id
            or support.document_version_id != candidate.document_version_id
            or support.extraction_run_id != candidate.extraction_run_id
            or support.polygon != candidate.polygon
        ):
            return None
        raw_by_reader.append((reader_id, support))
    if len(raw_by_reader) < 2:
        materialized = _materialize_stored_reader_answers(
            session, candidate, already_seen=seen_readers
        )
        raw_by_reader.extend(materialized)
    if (
        len(raw_by_reader) != 2
        or len({reader_id for reader_id, _support in raw_by_reader}) != 2
        or (
            independence_key("bedrock-slot-reader", raw_by_reader[0][0])
            == independence_key("bedrock-slot-reader", raw_by_reader[1][0])
            and frozenset(reader_id for reader_id, _support in raw_by_reader) != _CLAUDE_PAIR
        )
    ):
        return None

    # The canonical is supported by the two per-reader child candidates, not necessarily by the
    # row's aggregate candidate. Check both shapes: older human confirmation can support the parent
    # directly, while the automatic lane records each reader child. A stale or mismatched canonical
    # is a row-level refusal, never an exception that aborts the entire check stage.
    support_ids = [support.id for _reader_id, support in raw_by_reader]
    existing_rows = (
        session.execute(
            select(CanonicalObservation)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(
                EvidenceSupportingCandidate.candidate_id.in_({candidate.id, *support_ids}),
                CanonicalObservation.semantic_type == semantic.value,
            )
            .distinct()
            .order_by(CanonicalObservation.created_at, CanonicalObservation.id)
        )
        .scalars()
        .all()
    )
    if existing_rows:
        if len(existing_rows) != 1:
            return None
        existing = existing_rows[0]
        if (
            existing.document_version_id != candidate.document_version_id
            or existing.page_id != candidate.page_id
            or existing.value_numerator != candidate.value_numerator
            or existing.value_denominator != candidate.value_denominator
            or existing.unit != Unit.INCH.value
            or existing.status
            not in {
                EvidenceStatus.CORROBORATED.value,
                EvidenceStatus.HUMAN_CONFIRMED.value,
            }
        ):
            return None
        return existing

    page = session.get(Page, candidate.page_id)
    extraction = session.get(ExtractionRun, candidate.extraction_run_id)
    if page is None or extraction is None:
        return None
    transform = reading_transform(page, extraction)
    if transform is None:
        return None
    role = ReadingSides(session).of(candidate)
    if not isinstance(role, DocumentRole) or role is not DocumentRole.SHOP:
        # A row-scoped check consumes the slot reader's vendor-ink evidence only. It does not
        # compare this file with an architect drawing, so a view-role refusal must not force a
        # reviewer click when the vendor upload itself and the vendor ink are both explicit.
        # The ordinary/manual evidence path still uses ReadingSides unchanged.
        flags = set(candidate.ambiguity_flags or ())
        vendor_slot = (
            "slot-reader" in flags and "ink:vendor" in flags and "reviewer-markup" not in flags
        )
        kind = session.scalar(
            select(Document.kind)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(DocumentVersion.id == candidate.document_version_id)
        )
        if not vendor_slot or kind != "shop":
            return None
        role = DocumentRole.SHOP
    value = Measurement(
        Fraction(candidate.value_numerator, candidate.value_denominator),
        Unit.INCH,
        candidate.raw_text,
    )
    stored_points = tuple(
        transform.to_stored(ImagePoint(int(point[0]), int(point[1]))) for point in candidate.polygon
    )
    canonical = CanonicalObservation(
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        document_role=role.value,
        polygon=[[str(point.x), str(point.y)] for point in stored_points],
        coordinate_space="stored",
        semantic_type=semantic.value,
        value_numerator=value.exact.numerator,
        value_denominator=value.exact.denominator,
        unit=value.unit.value,
        status=EvidenceStatus.CORROBORATED.value,
        authority=Authority.AUTHORITATIVE.value,
        evidence_crop_uri=None,
    )
    session.add(canonical)
    session.flush()
    session.add_all(
        [
            EvidenceSupportingCandidate(
                canonical_observation_id=canonical.id,
                candidate_id=support_id,
                role="primary" if index == 0 else "corroborating",
            )
            for index, support_id in enumerate(support_ids)
        ]
    )
    session.add(
        EvidenceCorroborationLane(
            canonical_observation_id=canonical.id,
            lane=CorroborationLane.SECOND_READER.value,
        )
    )
    emit(
        session,
        category=AuditCategory.EVIDENCE_QUALIFICATION,
        actor=SYSTEM_ACTOR,
        target_id=canonical.id,
        target_type="canonical_observation",
    )
    session.flush()
    return canonical


def _materialize_stored_reader_answers(
    session: Session,
    candidate: ObservationCandidate,
    *,
    already_seen: set[str],
) -> list[tuple[str, ObservationCandidate]]:
    """Promote this run's already-stored attempts to immutable evidence candidates.

    The first replayable build predates the explicit support-candidate rows. Its private invocation
    records still bind each successful answer to the exact candidate id, page and packet hash, so the
    check may reconstitute those two raw answers without calling a model or modifying an old row.
    """
    flags = set(candidate.ambiguity_flags or ())
    row_rank = next((flag for flag in flags if flag.startswith("row-rank:")), None)
    if row_rank is None:
        return []
    page = session.get(Page, candidate.page_id)
    if page is None:
        return []
    invocations = session.scalars(
        select(ModelInvocation).where(
            ModelInvocation.extraction_run_id == candidate.extraction_run_id,
            ModelInvocation.prompt_id.in_(CLAUDE_SPAN_PROMPT_IDS),
            ModelInvocation.outcome == "ok",
            ModelInvocation.private_raw_response.is_not(None),
        )
    ).all()
    by_reader: dict[str, ObservationCandidate] = {}
    for invocation in invocations:
        packet = invocation.reader_question_packet
        raw = invocation.private_raw_response
        if not isinstance(packet, dict) or raw is None or invocation.model_id in already_seen:
            continue
        packet_candidates = packet.get("candidate_ids")
        if (
            not isinstance(packet_candidates, list)
            or str(candidate.id) not in packet_candidates
            or packet.get("document_version_id") != str(candidate.document_version_id)
            or packet.get("page_index") != page.index
        ):
            continue
        expected_hash = packet.get("packet_sha256")
        # A reused answer's packet (#1112) also names the call it reused; the hash is the question's.
        unsigned_packet = {
            key: value
            for key, value in packet.items()
            if key not in ("packet_sha256", REUSED_FROM_KEY)
        }
        actual_hash = hashlib.sha256(
            json.dumps(unsigned_packet, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if expected_hash != actual_hash:
            continue
        try:
            answer = parse_stored_reader_answer(raw)
        except (TypeError, ValueError):
            continue
        if candidate.value_numerator is None or candidate.value_denominator is None:
            continue
        expected = Fraction(candidate.value_numerator, candidate.value_denominator)
        reading = _verified_reader_answer(answer, expected)
        if reading is None:
            continue
        text = str(answer["text"])
        support = ObservationCandidate(
            id=uuid4(),
            document_version_id=candidate.document_version_id,
            page_id=candidate.page_id,
            extraction_run_id=candidate.extraction_run_id,
            raw_text=text,
            value_numerator=reading.exact.numerator,
            value_denominator=reading.exact.denominator,
            unit=Unit.INCH.value,
            polygon=candidate.polygon,
            coordinate_space="image",
            ambiguity_flags=[
                "slot-reader-support",
                f"supports:{candidate.id}",
                f"reader-id:{invocation.model_id}",
                f"reader-invocation:{invocation.id}",
                row_rank,
                f"slot-reader-page:{page.index}",
            ],
        )
        session.add(support)
        session.flush()
        by_reader[invocation.model_id] = support
    return list(by_reader.items())


def _domain_observation(session: Session, row: CanonicalObservation) -> DomainCanonicalObservation:
    supporting = tuple(
        str(value)
        for value in session.scalars(
            select(EvidenceSupportingCandidate.candidate_id).where(
                EvidenceSupportingCandidate.canonical_observation_id == row.id
            )
        )
    )
    page = session.get(Page, row.page_id)
    if page is None:
        raise ValueError("the candidate page disappeared")
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
        corroborated_by=(CorroborationLane.SECOND_READER,),
        conflicts_with=(),
        evidence_crop_uri=row.evidence_crop_uri,
    )


def _human_operand(name: str, value: Measurement, decision_id: UUID) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,
        status=EvidenceStatus.HUMAN_CONFIRMED,
        source="USER_INPUT",
        evidence_ref=str(decision_id),
        row_review_decision_id=str(decision_id),
    )
