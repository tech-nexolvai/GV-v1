"""Which sealed slot readings are offered to which form field (#987).

**Offered, never saved.** A proposal links a sealed candidate to a field of the reviewer's form;
the reviewer still saves the form, and saving is what makes a measurement (#965's rule, unchanged).

**The overall goes with its chain, never alone.** It is offered to `SHOP:countertop_overall_width`
only when it sealed, the row is trusted, and the whole chain under it is offered too. Measured on
the first verification run (#987): on a kitchenette elevation the line whose ends coincide with
the chain is the wall-to-wall dimension, its pieces run through a range and a fridge space, and the
printed number is not a countertop width at all. Its text sealed correctly; offered alone, it would
have put a wall's width in the countertop field. Code cannot tell the two lines apart, and a piece
of unknown kind is exactly where an appliance space hides — so an unnamed or unsealed piece holds
the overall back with the pieces, for the person to confirm.

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
    held: tuple[tuple[int | None, str], ...]
    """Sealed readings that are not offered, `(slot index or None for the overall, why)`."""


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
    overall_sealed = overall is not None and overall.state is LabelState.SEALED
    blockers = [
        piece
        for piece in pieces
        if piece.outcome.state is not LabelState.SEALED or piece.kind.kind not in _FIELDS
    ]
    if not pieces or blockers:
        held: list[tuple[int | None, str]] = []
        first = blockers[0].index + 1 if blockers else None
        if overall_sealed:
            held.append(
                (
                    None,
                    (
                        f"held back: piece {first} under this overall needs a look first; "
                        "confirm this is the countertop's width"
                        if first is not None
                        else "no pieces found under this overall"
                    ),
                )
            )
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
    if overall_sealed:
        proposals.append(SlotFieldProposal(OVERALL_FIELD, 0, None))
    positions: dict[str, int] = {}
    for piece in pieces:
        field = _FIELDS[piece.kind.kind]
        position = positions.get(field, 0)
        positions[field] = position + 1
        proposals.append(SlotFieldProposal(field, position, piece.index))
    return SlotMapping(tuple(proposals), ())


def _held_all(pieces: Sequence[PieceReading], reason: str) -> list[tuple[int | None, str]]:
    return [(piece.index, reason) for piece in pieces if piece.outcome.state is LabelState.SEALED]
