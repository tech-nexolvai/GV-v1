"""Which sealed slot readings are offered to which form field (#987).

**Offered, never saved.** A proposal links a sealed candidate to a field of the reviewer's form;
the reviewer still saves the form, and saving is what makes a measurement (#965's rule, unchanged).

**The overall** goes to `SHOP:countertop_overall_width` when it sealed and the row is trusted.

**The pieces go only as a whole chain.** Left to right, each to `SHOP:cabinet_width` or
`SHOP:filler_width` by its kind — but only when every piece of the row sealed and every kind is
cabinet or filler. One unknown, unsealed or missing piece and none is offered: the form has no
field for an unknown piece, so offering the rest would let a saved form add up a shorter chain than
the drawing has, and a shorter sum can happen to match the overall. Each held piece keeps its value
as a candidate with its reason, and an unknown one carries the "what is it?" question for the
screen (Phase 5).

**"Pieces add up" is never computed here** — not to pick a reading, not to check one. Whether they
add up is CT-WIDTH-001's question, in exact arithmetic, after a person has saved the form.

Source: issue #987 · Verification: `tests/extraction/slot_reader/test_mapping.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from extraction.slot_reader.kinds import KindProposal, PieceKind
from extraction.slot_reader.seal import LabelState, OwnerOutcome

__all__ = [
    "CABINET_FIELD",
    "FILLER_FIELD",
    "OVERALL_FIELD",
    "WHAT_IS_IT",
    "PieceReading",
    "SlotFieldProposal",
    "SlotMapping",
    "map_row",
]

OVERALL_FIELD: Final = "SHOP:countertop_overall_width"
CABINET_FIELD: Final = "SHOP:cabinet_width"
FILLER_FIELD: Final = "SHOP:filler_width"

#: The question an unknown piece carries to the screen.
WHAT_IS_IT: Final = "what is it? filler, cabinet, appliance space or something else"

_FIELDS: Final = {PieceKind.CABINET: CABINET_FIELD, PieceKind.FILLER: FILLER_FIELD}


@dataclass(frozen=True, slots=True)
class PieceReading:
    index: int
    """The slot's place in the row, left to right, from 0."""
    outcome: OwnerOutcome
    kind: KindProposal


@dataclass(frozen=True, slots=True)
class SlotFieldProposal:
    field_key: str
    position: int
    slot_index: int | None
    """The piece's slot, or `None` for the overall."""


@dataclass(frozen=True, slots=True)
class SlotMapping:
    proposals: tuple[SlotFieldProposal, ...]
    held: tuple[tuple[int, str], ...]
    """Sealed pieces that are not offered, `(slot index, why)`."""


def map_row(
    overall: OwnerOutcome | None,
    pieces: Sequence[PieceReading],
    *,
    row_ambiguity: str | None,
) -> SlotMapping:
    """The form-field proposals for one row, and why each sealed piece not offered was held.

    `pieces` must be the row's every slot, in order: a missing slot is a missing piece.
    """
    if [piece.index for piece in pieces] != list(range(len(pieces))):
        raise ValueError("the row's pieces must be every slot, left to right, from 0")
    proposals: list[SlotFieldProposal] = []
    if row_ambiguity is not None:
        return SlotMapping((), tuple(_held_all(pieces, "not sure this row is the countertop")))
    if overall is not None and overall.state is LabelState.SEALED:
        proposals.append(SlotFieldProposal(OVERALL_FIELD, 0, None))
    blockers = [
        piece
        for piece in pieces
        if piece.outcome.state is not LabelState.SEALED or piece.kind.kind not in _FIELDS
    ]
    if not pieces or blockers:
        held: list[tuple[int, str]] = []
        first = blockers[0].index + 1 if blockers else None
        for piece in pieces:
            if piece.outcome.state is not LabelState.SEALED:
                continue
            if piece.kind.kind is PieceKind.UNKNOWN:
                held.append((piece.index, f"{WHAT_IS_IT} ({piece.kind.evidence})"))
            elif piece.kind.kind not in _FIELDS:
                held.append(
                    (piece.index, f"{piece.kind.kind.value.replace('_', ' ')}: review the value")
                )
            else:
                held.append(
                    (
                        piece.index,
                        (
                            f"held back: piece {first} of this row needs a look first, so the "
                            "pieces are not offered yet"
                        ),
                    )
                )
        return SlotMapping(tuple(proposals), tuple(held))
    positions: dict[str, int] = {}
    for piece in pieces:
        field = _FIELDS[piece.kind.kind]
        position = positions.get(field, 0)
        positions[field] = position + 1
        proposals.append(SlotFieldProposal(field, position, piece.index))
    return SlotMapping(tuple(proposals), ())


def _held_all(pieces: Sequence[PieceReading], reason: str) -> list[tuple[int, str]]:
    return [(piece.index, reason) for piece in pieces if piece.outcome.state is LabelState.SEALED]
