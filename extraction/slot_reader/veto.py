"""The drawing as a check on a sealed reading: reject only, never a value (#992).

**What it catches.** Two readers can agree on the same wrong text — a stacked `3/4"` read as
`3 3/4"`, a first digit lost on a tick. The drawing itself disagrees: shop drawings are drawn to
scale, so a piece's drawn length times the row's scale lands near its printed width. E3 (2026-10-07,
both client sets, 76 correct readings, 161 simulated misreads) found every correct reading within
6.4 % of that (3/4" pieces; 2.7 % for anything over 1"), and a ±10 % band rejected **no correct
reading and all 104 big misreads**. It does *not* catch fraction misreads (an eighth read as a half): they sit
inside the drawing's own error, which is why this is a veto and never a reader.

**The rule, approved by the admin.** For each sealed reading in a row — a piece or the overall — the
scale is derived exactly, as a `Fraction` of inches per point, from the *other* sealed, non-stacked
pieces: the sum of their values over the sum of their drawn lengths. Leave-one-out, as E3 measured
it: a misread piece in its own scale would pull the scale towards itself. The overall never sets the
scale (it is what the pieces are checked against later). With fewer than two such pieces there is
no scale and nothing is vetoed. A reading more than 10 % away from its drawn length × scale is
handed back to the person: "doesn't match the drawn length". A big misread is in its neighbours'
scales, so in a short row it can push correct neighbours out of the band too; that only holds them
back, and a row with one held piece offers nothing anyway.

**It never sets or changes a value**, never seals anything, and never picks between readings. Its
only output is which sealed readings to hold back.

**Which pieces scale (#1107).** A piece is left out of its neighbours' scale only when *code*
confirms it is a stacked fraction (the file's text, or the fraction-bar detector): a reader's
`stacked` answer alone never removes a piece, as a single reader's answer may add a hold but never
weaken a check. Where no scale can be derived for a reading, `drawn_length_witnessed` leaves it out,
so the stage records that its drawn length was not checked (`vocabulary/drawn_length.py`).

Source: issue #992 · Verification: `tests/extraction/slot_reader/test_veto.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

__all__ = [
    "DRAWN_LENGTH_BAND",
    "DRAWN_LENGTH_REASON",
    "MINIMUM_SCALE_PIECES",
    "DrawnReading",
    "drawn_length_vetoes",
    "drawn_length_witnessed",
]

#: The admin's band (2026-10-07): a reading more than a tenth away from drawn × scale is rejected.
DRAWN_LENGTH_BAND: Final = Fraction(1, 10)
#: The fewest other sealed, non-stacked pieces a scale is derived from.
MINIMUM_SCALE_PIECES: Final = 2
DRAWN_LENGTH_REASON: Final = "doesn't match the drawn length"


@dataclass(frozen=True, slots=True)
class DrawnReading:
    """One sealed reading of a row, beside the length the drawing gives it."""

    index: int | None
    """The piece's slot, left to right from 0; `None` for the overall."""
    value: Fraction
    """The sealed value, in inches."""
    drawn: Fraction
    """Its drawn length on the page, in points, exactly."""
    stacked: bool
    """A stacked fraction *code* confirmed (#1107): checked like any reading, but never part of a
    scale. A reader's `stacked` answer alone is not this."""

    def __post_init__(self) -> None:
        if not isinstance(self.value, Fraction) or not isinstance(self.drawn, Fraction):
            raise TypeError("a drawn reading is exact: Fraction value and drawn length")
        if self.drawn <= 0:
            raise ValueError("a drawn length is positive")


def _scale(reading: DrawnReading, pieces: Sequence[DrawnReading]) -> Fraction | None:
    """The row's scale for `reading`, from the *other* sealed, non-stacked pieces; `None` if none."""
    sources = [piece for piece in pieces if piece is not reading and not piece.stacked]
    if len(sources) < MINIMUM_SCALE_PIECES:
        return None
    return sum((piece.value for piece in sources), Fraction(0)) / sum(
        (piece.drawn for piece in sources), Fraction(0)
    )


def _checked(
    pieces: Sequence[DrawnReading], overall: DrawnReading | None
) -> list[tuple[DrawnReading, Fraction]]:
    """Each reading the drawing can check, with the length the drawing expects of it."""
    if overall is not None and overall.index is not None:
        raise ValueError("the overall has no slot index")
    if any(piece.index is None for piece in pieces):
        raise ValueError("a piece has a slot index")
    expected: list[tuple[DrawnReading, Fraction]] = []
    for reading in (*pieces, *((overall,) if overall is not None else ())):
        scale = _scale(reading, pieces)
        if scale is None:
            continue
        length = reading.drawn * scale
        if length > 0:
            expected.append((reading, length))
    return expected


def drawn_length_vetoes(
    pieces: Sequence[DrawnReading], overall: DrawnReading | None
) -> dict[int | None, str]:
    """The sealed readings the drawing rejects, by slot (`None` for the overall), with the reason.

    `pieces` holds only the row's *sealed* pieces; an unsealed one is not a reading to check or to
    scale by. An empty result changes nothing.
    """
    return {
        reading.index: DRAWN_LENGTH_REASON
        for reading, expected in _checked(pieces, overall)
        if abs(reading.value - expected) > DRAWN_LENGTH_BAND * expected
    }


def drawn_length_witnessed(
    pieces: Sequence[DrawnReading], overall: DrawnReading | None
) -> frozenset[int | None]:
    """The sealed readings the drawing could check at all, by slot (`None` for the overall) (#1107).

    A reading missing here had no scale: its drawn length was not checked, which the stage records.
    A vetoed reading is here — it was checked.
    """
    return frozenset(reading.index for reading, _expected in _checked(pieces, overall))
