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
    """A stacked fraction: checked like any reading, but never part of a scale."""

    def __post_init__(self) -> None:
        if not isinstance(self.value, Fraction) or not isinstance(self.drawn, Fraction):
            raise TypeError("a drawn reading is exact: Fraction value and drawn length")
        if self.drawn <= 0:
            raise ValueError("a drawn length is positive")


def drawn_length_vetoes(
    pieces: Sequence[DrawnReading], overall: DrawnReading | None
) -> dict[int | None, str]:
    """The sealed readings the drawing rejects, by slot (`None` for the overall), with the reason.

    `pieces` holds only the row's *sealed* pieces; an unsealed one is not a reading to check or to
    scale by. An empty result changes nothing.
    """
    if overall is not None and overall.index is not None:
        raise ValueError("the overall has no slot index")
    if any(piece.index is None for piece in pieces):
        raise ValueError("a piece has a slot index")
    vetoes: dict[int | None, str] = {}
    for reading in (*pieces, *((overall,) if overall is not None else ())):
        sources = [piece for piece in pieces if piece is not reading and not piece.stacked]
        if len(sources) < MINIMUM_SCALE_PIECES:
            continue
        scale = sum((piece.value for piece in sources), Fraction(0)) / sum(
            (piece.drawn for piece in sources), Fraction(0)
        )
        expected = reading.drawn * scale
        if expected <= 0:
            continue
        if abs(reading.value - expected) > DRAWN_LENGTH_BAND * expected:
            vetoes[reading.index] = DRAWN_LENGTH_REASON
    return vetoes
