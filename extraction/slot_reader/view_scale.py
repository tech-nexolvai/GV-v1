"""The drawing as a check on one sealed reading, by its view's scale: reject, witness, or nothing.

**Why this exists beside `veto.py`.** The slot reader checks a sealed piece against its own row: the
other sealed pieces of that row give the scale (leave-one-out). A number the review assistant reads
alone (a pen-stroke label on an architect's view, Step 2 of the 2026-11-05 rebuild plan) has no row
of sealed neighbours. Its view does have a scale: code fits one from the view's own printed
dimensions and their lines (`app/review/assistant/scale_fit.py`). So the same question is asked
with that scale instead: does the sealed value land near its drawn length times the view's inches
per point?

**The same band, the same comparison** (`veto.outside_band`, the admin's ±10 % of 2026-10-07): a
value more than a tenth away from drawn × scale is `VETOED`; inside it (exactly a tenth away is
inside) it is `WITNESSED`. With no drawn length (code found no dimension line of the label's own)
or no scale (the view has no fit), the drawing says nothing: `NO_SCALE`. E3 measured that the band
rejects every big misread and no correct reading; it does not catch a fraction misread (an eighth
read as a half), which sits inside the drawing's own error.

**It never sets, changes or picks a value.** Its only outputs are "the drawing rejects it", "the
drawing agrees" and "the drawing cannot tell". Exact arithmetic only: `Fraction` in, no float.

Source: rebuild plan v1 (2026-11-05) section 5 · Verification:
`tests/extraction/slot_reader/test_view_scale.py`
"""

from __future__ import annotations

from enum import StrEnum
from fractions import Fraction

from extraction.slot_reader.veto import outside_band

__all__ = ["ScaleVerdict", "view_scale_check"]


class ScaleVerdict(StrEnum):
    """What the drawing's view scale says about one sealed reading."""

    VETOED = "vetoed"
    """More than the band away from its drawn length at the view's scale: a misread, held."""
    WITNESSED = "witnessed"
    """Within the band: the drawing is a second, different kind of reading that agrees."""
    NO_SCALE = "no_scale"
    """No drawn length or no view scale: nothing was checked."""


def view_scale_check(
    value: Fraction, drawn: Fraction | None, scale: Fraction | None
) -> ScaleVerdict:
    """A sealed `value` (inches) against its label's `drawn` length (points, exact) at the view's
    `scale` (inches per point, exact). `None` for either: `NO_SCALE`.

    Raises on a float anywhere, a value that is not positive, or a drawn length or scale that is not
    positive: those are a caller's mistake, never "no scale"."""
    if not isinstance(value, Fraction):
        raise TypeError("a sealed value is exact: a Fraction of inches")
    if value <= 0:
        raise ValueError("a sealed value is positive")
    for name, given in (("drawn", drawn), ("scale", scale)):
        if given is None:
            continue
        if not isinstance(given, Fraction):
            raise TypeError(f"{name} is exact: a Fraction")
        if given <= 0:
            raise ValueError(f"{name} is positive")
    if drawn is None or scale is None:
        return ScaleVerdict.NO_SCALE
    if outside_band(value, drawn * scale):
        return ScaleVerdict.VETOED
    return ScaleVerdict.WITNESSED
