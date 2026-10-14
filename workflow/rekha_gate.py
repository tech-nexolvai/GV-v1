"""Rekha's gate: the row both readers chose must carry a printed number on its pieces (#1228).

Runs in `workflow/slot_reader.read_slot_pages` right after the row choice, before any label, walls
or counter-break question is planned for the row. Code only; it can only stop. Its result is a
`workflow.pariksha_gates.GateResult` (the record), kept on the row's anchor or on the page's
row-choice record.

Source: issue #1228. Verification: `tests/workflow/test_pariksha_gates.py`; $0 replay on the stored
runs (kept local).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Final

from extraction.geometry.rows import CountertopRowCandidate, LabelKind, SlotLabel
from workflow.pariksha_gates import (
    REKHA_NO_NUMBERS,
    REKHA_NO_NUMBERS_REASON,
    REKHA_OVERALL_KEPT,
    REKHA_OVERALL_KEPT_REASON,
    REKHA_PASS,
    REKHA_PASS_REASON,
    REKHA_READ_THROUGH_KEPT,
    REKHA_READ_THROUGH_KEPT_REASON,
    REKHA_ROW_GATE,
    Agent,
    GateDecision,
    GateResult,
    GateScope,
)

__all__ = ["CENTRED_SHARE", "numbered_pieces", "rekha_row_gate"]

#: How near a label's middle must be to its piece's middle, as a share of the piece's width, to
#: be that piece's printed dimension. Drafting prints a dimension's number at the middle of its
#: span; a material tag, a dash or a neighbouring view's number that the row's band happens to
#: reach stands off to one side. Measured on the stored runs (#1228, $0 replay): on every row both
#: readers agreed on, the most centred possible number stood at most 0.095 of its piece's width
#: from the middle; on the stone-outline line, the nearest stood 0.128 off. 0.11 sits between.
#: With pieces narrower than their label skipped (`numbered_pieces`), the review's replay of 37
#: stored databases (42 distinct agreed rows, AI Sets 1-3 only) stops 0 of 38 real rows and 4 of 4
#: wrong lines.
CENTRED_SHARE: Final = Fraction(11, 100)

#: Marks that make a text label read as a dimension even with letters in it (`2" BLACK`, `[24]VIF`).
_DIMENSION_MARKS: Final = frozenset("\"'”″′’[")


# --- Rekha's gate ----------------------------------------------------------------------------------


def _could_be_a_number(label: SlotLabel) -> bool:
    """Whether code sees a label that may be a printed dimension.

    A run of glyph strokes may be (its characters are unknown until a reader reads them), and so
    may a stacked fraction set as text. Other text must hold a digit; text that also holds letters
    is a code or a tag (a material, cabinet or sink code) unless it carries an inch, foot or
    bracket mark (`2" BLACK`, `[24]VIF`)."""
    if label.kind is LabelKind.GLYPHS or label.stacked:
        return True
    printed = " ".join(label.lines)
    if not any(character.isdigit() for character in printed):
        return False
    if any(character.isalpha() for character in printed):
        return any(mark in printed for mark in _DIMENSION_MARKS)
    return True


def _centred(label: SlotLabel, x0: Decimal, x1: Decimal) -> bool:
    """Whether the label's middle stands within `CENTRED_SHARE` of the span's width of the span's
    middle (exact arithmetic on the page's own points)."""
    width = Fraction(x1 - x0)
    if width <= 0:
        return False
    offset = abs(Fraction(label.box.x0 + label.box.x1) / 2 - Fraction(x0 + x1) / 2)
    return offset <= CENTRED_SHARE * width


def _numbered(labels: Sequence[SlotLabel], x0: Decimal, x1: Decimal) -> bool:
    return any(_could_be_a_number(label) and _centred(label, x0, x1) for label in labels)


def _wider_than(label: SlotLabel, x0: Decimal, x1: Decimal) -> bool:
    """Whether the label is wider than the span it stands in."""
    return label.box.x1 - label.box.x0 > x1 - x0


def numbered_pieces(row: CountertopRowCandidate) -> tuple[int, ...]:
    """The pieces (slot indices) on which code sees a printed number centred on the piece.

    A label wider than its piece is never that piece's number (#1228 review, rule R10): on a sliver
    piece (a couple of points wide, between two close ticks) any stroke standing over it is
    "centred" by chance. A material-tag band passed that way; a real dimension of a sliver piece
    is printed outside it with a leader, and the row's other pieces carry their own numbers."""
    return tuple(
        slot.index
        for slot in row.slots
        if _numbered(
            [label for label in slot.labels if not _wider_than(label, slot.x0, slot.x1)],
            slot.x0,
            slot.x1,
        )
    )


def rekha_row_gate(
    row: CountertopRowCandidate,
    *,
    read_through_possible: bool = False,
    page_number: int | None = None,
) -> GateResult:
    """Rekha's hand-off: the row both readers chose must carry a printed number on its pieces.

    **Rule.** Code looks at each piece band of the chosen line (`CountertopRowCandidate.slots` and
    the labels the row builder found in them). A piece carries a printed number when one of its
    labels may be a number (`_could_be_a_number`), is no wider than the piece, and stands at the
    middle of the piece (`CENTRED_SHARE`). The line passes when at least one piece does. A line no piece of which
    does (a box around the stone top's own outline, or around material tags) is stopped: "the
    chosen line has no printed numbers; the reviewer picks the row".

    **Never loses what the reviewer gets.** Two cases are recorded and read as before
    (`GateDecision.RECORDED`) instead of stopped: the line's overall carries a printed number of its
    own (its reading is offered today), or the vendor's equal-shares chain could give the line's
    widths (`read_through_possible`, #1086).
    """
    scope_id = None if page_number is None else str(page_number)
    if numbered_pieces(row):
        return GateResult(
            Agent.REKHA,
            REKHA_ROW_GATE,
            GateScope.ROW,
            GateDecision.PASS,
            REKHA_PASS,
            REKHA_PASS_REASON,
            scope_id,
        )
    if row.overall is not None and _numbered(row.overall.labels, row.overall.x0, row.overall.x1):
        return GateResult(
            Agent.REKHA,
            REKHA_ROW_GATE,
            GateScope.ROW,
            GateDecision.RECORDED,
            REKHA_OVERALL_KEPT,
            REKHA_OVERALL_KEPT_REASON,
            scope_id,
        )
    if read_through_possible:
        return GateResult(
            Agent.REKHA,
            REKHA_ROW_GATE,
            GateScope.ROW,
            GateDecision.RECORDED,
            REKHA_READ_THROUGH_KEPT,
            REKHA_READ_THROUGH_KEPT_REASON,
            scope_id,
        )
    return GateResult(
        Agent.REKHA,
        REKHA_ROW_GATE,
        GateScope.ROW,
        GateDecision.STOP,
        REKHA_NO_NUMBERS,
        REKHA_NO_NUMBERS_REASON,
        scope_id,
    )
