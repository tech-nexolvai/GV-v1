"""Which sealed slot readings are offered to which form field (#987).

**Offered, never saved.** A proposal links a sealed candidate to a field of the reviewer's form;
the reviewer still saves the form, and saving is what makes a measurement (#965's rule, unchanged).

**The overall goes with its chain, never alone.** It is offered to `SHOP:countertop_overall_width`
only when it sealed, the row is trusted, and the whole chain under it is offered too (since #992,
offered as `SHOP:countertop_piece_width`, whatever the pieces' kinds). Measured on
the first verification run (#987): on a kitchenette elevation the line whose ends coincide with
the chain is the wall-to-wall dimension, its pieces run through a range and a fridge space, and the
printed number is not a countertop width at all. Its text sealed correctly; offered alone, it would
have put a wall's width in the countertop field. Code cannot tell the two lines apart, so the
overall is never offered without every piece under it. Until #992 an *unnamed* piece also held it
back; since the admin's 2026-10-07 decision (the width is the sum of all the row's pieces, kinds not
needed) only an unsealed or vetoed piece, a held row or an uncertain row does. Such a row is then
offered whole, and its pieces adding up to a wall-to-wall line is CT-WIDTH-001's to judge with the
wall layout's field cut, not this module's.

**A fully sealed row offers every piece, whatever its kind** (#992; the admin decided on
2026-10-07 that the countertop's width is the sum of *all* the pieces in its row, so kinds are not
needed for it). When every slot and the overall sealed, the overall goes to its field and every
piece, left to right, to `SHOP:countertop_piece_width` — and nothing to the cabinet or filler lists,
because CT-WIDTH-001 (v1.1.0, #993) sends a countertop that has piece widths and either list to
review by design. One unsealed or vetoed slot, a held row or an uncertain row, and no piece is
offered there: a shorter chain must never become a shorter sum.

**The kind fields go only as a whole chain, and only without a sealed overall.** Left to right,
each to `SHOP:cabinet_width` or `SHOP:filler_width` by its kind — but only when every piece of the
row sealed, every kind is cabinet or filler, and the row was not offered as piece widths. One
unknown, unsealed or missing piece and none is offered: the form has no field for an unknown
piece, so offering the rest would let a saved form add up a shorter chain than
the drawing has, and a shorter sum can happen to match the overall. Each held piece keeps its value
as a candidate with its reason, and an unknown one carries the "what is it?" question for the
screen (Phase 5).

**"Pieces add up" is never computed here** — not to pick a reading, not to check one. Whether they
add up is CT-WIDTH-001's question, in exact arithmetic, after a person has saved the form.

Source: issues #987, #992 · Verification: `tests/extraction/slot_reader/test_kinds_and_mapping.py`
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
    "PIECE_FIELD",
    "WHAT_IS_IT",
    "PieceReading",
    "SlotFieldProposal",
    "SlotMapping",
    "map_row",
]

OVERALL_FIELD: Final = "SHOP:countertop_overall_width"
#: CT-WIDTH-001's many-valued `piece_widths` (semantic type `countertop_piece_width`): every piece of
#: the countertop row, left to right, of any kind (#992).
PIECE_FIELD: Final = "SHOP:countertop_piece_width"
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
    row_hold: str | None = None,
    allow_partial_proposals: bool = False,
) -> SlotMapping:
    """The form-field proposals for one row, and why each sealed piece not offered was held.

    `pieces` must be the row's every slot, in order: a missing slot is a missing piece. `row_hold`
    is a reason the whole countertop goes to the person (`labels.row_hold`): nothing is offered.
    """
    if [piece.index for piece in pieces] != list(range(len(pieces))):
        raise ValueError("the row's pieces must be every slot, left to right, from 0")
    if row_ambiguity is not None:
        return SlotMapping((), tuple(_held_all(pieces, "not sure this row is the countertop")))
    overall_sealed = overall is not None and overall.state is LabelState.SEALED
    if row_hold is not None:
        waiting = _held_all(pieces, row_hold)
        if overall_sealed:
            waiting.insert(0, (None, row_hold))
        return SlotMapping((), tuple(waiting))
    unsealed = [piece for piece in pieces if piece.outcome.state is not LabelState.SEALED]
    named = all(piece.kind.kind in _FIELDS for piece in pieces)
    if pieces and not unsealed and overall_sealed:
        # The whole row sealed: the overall and every piece, left to right, whatever its kind
        # (#992) — the countertop's width needs the pieces, not their names. Never the cabinet
        # or filler lists beside them: CT-WIDTH-001 (v1.1.0, #993) sends a countertop with piece
        # widths *and* a cabinet or filler list to review, by design.
        proposals = [SlotFieldProposal(OVERALL_FIELD, 0, None)]
        proposals.extend(
            SlotFieldProposal(PIECE_FIELD, position, piece.index)
            for position, piece in enumerate(pieces)
        )
        return SlotMapping(tuple(proposals), ())
    if allow_partial_proposals and pieces and (unsealed or (not overall_sealed and not named)):
        # A sealed piece is still a useful form proposal when a neighbour needs the reviewer.
        # Keep its *drawing position*: compacting [sealed, gap, sealed] into [sealed, sealed]
        # would silently shift the second width onto the missing piece. The overall remains held
        # until the row is complete, and the check stage independently requires a confirmed run
        # for a partial Claude row (so this sparse list can never become a shorter operand).
        proposals = [
            SlotFieldProposal(PIECE_FIELD, piece.index, piece.index)
            for piece in pieces
            if piece.outcome.state is LabelState.SEALED
        ]
        partial_held: list[tuple[int | None, str]] = []
        first_unsealed = next(
            (piece.index + 1 for piece in pieces if piece.outcome.state is not LabelState.SEALED),
            None,
        )
        if overall_sealed:
            partial_held.append(
                (
                    None,
                    (
                        f"held back: piece {first_unsealed} under this overall needs a look first; "
                        "confirm this is the countertop's width"
                        if first_unsealed is not None
                        else "held back: the overall needs a look before this row can be checked"
                    ),
                )
            )
        partial_held.extend(
            (piece.index, piece.outcome.reason or "needs a value")
            for piece in pieces
            if piece.outcome.state is not LabelState.SEALED
        )
        return SlotMapping(tuple(proposals), tuple(partial_held))
    if pieces and not unsealed and named:
        return SlotMapping(tuple(_named(pieces)), ())
    blockers = [
        piece
        for piece in pieces
        if piece.outcome.state is not LabelState.SEALED or piece.kind.kind not in _FIELDS
    ]
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
    return SlotMapping((), tuple(held))


def _named(pieces: Sequence[PieceReading]) -> list[SlotFieldProposal]:
    """Each piece, left to right, to the cabinet or filler field its kind names."""
    proposals: list[SlotFieldProposal] = []
    positions: dict[str, int] = {}
    for piece in pieces:
        field = _FIELDS[piece.kind.kind]
        position = positions.get(field, 0)
        positions[field] = position + 1
        proposals.append(SlotFieldProposal(field, position, piece.index))
    return proposals


def _held_all(pieces: Sequence[PieceReading], reason: str) -> list[tuple[int | None, str]]:
    return [(piece.index, reason) for piece in pieces if piece.outcome.state is LabelState.SEALED]
