"""A person's answers about a countertop row's held labels, as the width check counts them (#1339).

**Light on purpose.** The API reaches this (`app/api/slot_rows.py`, `app/review/evidence_actions.py`)
and `tests/api/test_no_heavy_work.py` keeps it away from reader code, so it imports no extraction
module. `workflow/slot_row_evidence.py` seals the widths from what this returns.

- **Which label is where.** A row's width labels carry `slot:<n>` (`slot:overall` for the overall);
  `row_positions` takes the ones the readers offered to the form (as the check always has), and
  `own_slot_labels` every label of the row, sealed or held, so a held label with no form proposal
  can still be answered.
- **A person's answer** about a held label is a `HUMAN_CONFIRMED` observation citing that very label
  (`app/review/evidence_actions.py`: the person confirmed the AI's reading, or fixed it). The newest
  answer wins; one citing another row's label, or of another width type, never counts.
- **No typed widths.** A value typed on the row's review decision fills nothing (confirm-only,
  2026-10-15).
- **The "incomplete" hold.** When the readers did not seal every label, the reader marks every label
  of the row `row-partial` and the row is held. Once every position has a sealed reading or a
  person's answer, that hold is resolved; every other hold (the line finder's `row-hold:`,
  `row-ambiguous`, a check hold, "not the vendor drawing") stays.

Source: issue #1339. Verification: `tests/review/test_held_reading_actions.py`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from fractions import Fraction
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import (
    CanonicalObservation,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.review.review_items import vendor_ink_only
from evidence.canonical import CorroborationLane, EvidenceStatus
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Unit
from workflow.slot_row_scope import (
    SlotRow,
    SlotRowQualification,
    SlotRowReading,
    candidate_is_sealed,
    candidate_value,
    qualify_slot_row,
)

__all__ = [
    "PERSON_ANSWER_FLAG",
    "ROW_PARTIAL",
    "answered_labels",
    "own_slot_labels",
    "person_answers",
    "qualify_row",
    "row_hold_after_people",
    "row_positions",
    "row_readings",
    "slot_position",
    "width_semantic",
]


def row_positions(
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


#: A position filled by a person's answer about this row's own held label (#1339). Carried with
#: `human-saved-for-row`, the flag `qualify_slot_row` reads for a person's value on this row.
PERSON_ANSWER_FLAG: Final = "person-answered-held-reading"

#: The hold a Claude-mode reading puts on every label of a row when the readers did not seal every
#: label (`workflow/slot_reader.py`): the only hold people's answers resolve.
ROW_PARTIAL: Final = "row-partial"

#: The rest of a row's holds: never resolved by answering its labels (`slot_row_scope`).
_OTHER_ROW_HOLDS: Final = ("row-hold:", "check-hold:")
_NOT_THE_VENDOR_DRAWING: Final = "This is not the vendor drawing."


def slot_position(candidate: ObservationCandidate) -> tuple[bool, int | None]:
    """`(True, position)` for a width label of a row (`None` = the overall), else `(False, None)`."""
    slot = next(
        (
            flag.removeprefix("slot:")
            for flag in candidate.ambiguity_flags or ()
            if flag.startswith("slot:")
        ),
        None,
    )
    if slot == "overall":
        return True, None
    if slot is not None and slot.isdigit():
        return True, int(slot)
    return False, None


def width_semantic(position: int | None) -> SemanticType:
    """What a row's label at `position` is: the overall, or one piece (as `_seal_row` seals it)."""
    return (
        SemanticType.COUNTERTOP_OVERALL_WIDTH
        if position is None
        else SemanticType.COUNTERTOP_PIECE_WIDTH
    )


def own_slot_labels(row: SlotRow) -> dict[int | None, ObservationCandidate]:
    """Each width position's one label on this row, sealed or held, with or without a form proposal.

    A position holding two labels is left out: which one a person answered could not be told, so
    neither fills it (fail closed, as `_row_positions` refuses duplicates).
    """
    found: dict[int | None, list[ObservationCandidate]] = {}
    for candidate in row.candidates:
        is_slot, position = slot_position(candidate)
        if not is_slot or (position is not None and not 0 <= position < row.piece_count):
            continue
        found.setdefault(position, []).append(candidate)
    return {position: labels[0] for position, labels in found.items() if len(labels) == 1}


def person_answers(session: Session, row: SlotRow) -> dict[int | None, CanonicalObservation]:
    """For each held label of this row, the newest answer a person gave about it (#1339).

    Only a held label whose value is positively the vendor's own number (`vendor_ink_only`) is
    answered. An answer is a `HUMAN_CONFIRMED` observation citing this row's own label at that position, of
    that position's width type, on the label's own page and drawing, the vendor's side, in inches
    and above zero. Only the newest such answer counts. A sealed label is never answered here (its
    readers' seal is its evidence), and nothing citing another row's label is even looked at.
    """
    labels = own_slot_labels(row)
    answered = answered_labels(session, (row,))
    return {
        position: answered[candidate.id]
        for position, candidate in labels.items()
        if candidate.id in answered
    }


def answered_labels(session: Session, rows: Iterable[SlotRow]) -> dict[UUID, CanonicalObservation]:
    """`person_answers` for many rows in one read, keyed by the answered label's id (Set 3 QA).

    The width check counts exactly these (`row_readings`), so the review list and the countertop
    results read the same answers: a number a person answered is never asked again.
    """
    held: dict[UUID, tuple[int | None, ObservationCandidate]] = {}
    for row in rows:
        for position, candidate in own_slot_labels(row).items():
            # Only a label whose value is positively the vendor's own number takes an answer (W1's
            # rule, `vendor_ink_only`): an answer citing a label under GV's markup, with two labels
            # or with unchecked ink, from any path, never fills the row (defence in depth, #1339).
            if not candidate_is_sealed(candidate) and vendor_ink_only(
                candidate.ambiguity_flags or ()
            ):
                held[candidate.id] = (position, candidate)
    if not held:
        return {}
    answers: dict[UUID, CanonicalObservation] = {}
    for canonical, cited in session.execute(
        select(CanonicalObservation, EvidenceSupportingCandidate.candidate_id)
        .join(
            EvidenceSupportingCandidate,
            EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
        )
        .where(
            EvidenceSupportingCandidate.candidate_id.in_(list(held)),
            CanonicalObservation.status == EvidenceStatus.HUMAN_CONFIRMED.value,
        )
        .order_by(CanonicalObservation.created_at.desc(), CanonicalObservation.id.desc())
    ).all():
        if cited in answers:
            continue  # the newest answer about this label already stands
        position, label = held[cited]
        if (
            canonical.semantic_type != width_semantic(position).value
            or canonical.page_id != label.page_id
            or canonical.document_version_id != label.document_version_id
            or canonical.document_role != DocumentRole.SHOP.value
            or canonical.unit != Unit.INCH.value
            or canonical.value_denominator <= 0
            or Fraction(canonical.value_numerator, canonical.value_denominator) <= 0
        ):
            continue
        answers[cited] = canonical
    return answers


def row_hold_after_people(session: Session, row: SlotRow) -> str | None:
    """The row's hold once people's answers about its held labels are counted (#1339).

    The row's own hold, unless the only hold is `row-partial` (the readers did not seal every
    label) and every position now has a sealed reading or a person's answer: then it is resolved.
    Any other hold stays.
    """
    return _hold_after(row, row_readings(session, row, _sealed_positions(row))[0])


def _sealed_positions(row: SlotRow) -> dict[int | None, ObservationCandidate]:
    positions = row_positions(row)
    return {} if isinstance(positions, str) else positions[0]


def _hold_after(row: SlotRow, readings: list[SlotRowReading]) -> str | None:
    held = row.held_reason
    if held is None or held == _NOT_THE_VENDOR_DRAWING:
        return held
    flags = {flag for candidate in row.candidates for flag in candidate.ambiguity_flags or ()}
    if (
        ROW_PARTIAL not in flags
        or "row-ambiguous" in flags
        or any(flag.startswith(_OTHER_ROW_HOLDS) for flag in flags)
        or not any(PERSON_ANSWER_FLAG in reading.flags for reading in readings)
    ):
        return held
    positions = [reading.position for reading in readings]
    if len(positions) != len(set(positions)) or set(positions) != {None, *range(row.piece_count)}:
        return held
    return None


def row_readings(
    session: Session,
    row: SlotRow,
    candidates_by_position: dict[int | None, ObservationCandidate],
) -> tuple[list[SlotRowReading], dict[int | None, CanonicalObservation]]:
    """Each position's sealed reading, or a person's answer about this row's own held label there.

    A reader suggestion that did not seal is never an operand: only a person's answer about that
    very label fills its position (`person_answers`). A value typed on the row's review decision
    fills nothing (confirm-only, #1339).
    """
    answers = person_answers(session, row)
    labels = own_slot_labels(row)
    readings: list[SlotRowReading] = []
    for position in (None, *range(row.piece_count)):
        selected_candidate: ObservationCandidate | None = candidates_by_position.get(position)
        if selected_candidate is not None and candidate_is_sealed(selected_candidate):
            value = candidate_value(selected_candidate)
            if value is None:
                continue
            readings.append(
                SlotRowReading(
                    candidate_id=selected_candidate.id,
                    position=position,
                    value_numerator=value.numerator,
                    value_denominator=value.denominator,
                    unit="in",
                    status=selected_candidate.corroboration_status,
                    lane=selected_candidate.corroboration_lane,
                    flags=frozenset(selected_candidate.ambiguity_flags or ()),
                )
            )
            continue
        answer = answers.get(position)
        if answer is None:
            continue
        readings.append(
            SlotRowReading(
                candidate_id=labels[position].id,
                position=position,
                value_numerator=answer.value_numerator,
                value_denominator=answer.value_denominator,
                unit=answer.unit,
                status=EvidenceStatus.HUMAN_CONFIRMED.value,
                lane=CorroborationLane.HUMAN.value,
                flags=frozenset({"human-saved-for-row", PERSON_ANSWER_FLAG}),
            )
        )
    return readings, answers


def qualify_row(row: SlotRow, readings: list[SlotRowReading]) -> SlotRowQualification:
    held = _hold_after(row, readings)
    if held is None and row.held_reason is not None:
        # People answered every label the readers held: the "incomplete" mark on the sealed
        # labels is resolved with the row's hold, and nothing else about them changes.
        readings = [replace(reading, flags=reading.flags - {ROW_PARTIAL}) for reading in readings]
    return qualify_slot_row(
        tuple(readings),
        expected_piece_count=row.piece_count,
        held_reason=held,
        shop_document=row.held_reason != _NOT_THE_VENDOR_DRAWING,
    )
