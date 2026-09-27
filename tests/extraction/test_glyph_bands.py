"""Catching a stacked fraction the reader absorbed into the digits (#541).

The case: `28 3/4"` came back from a real crop as `284`, and the seam accepted it — correctly, since
`284` parses as a dimension. No validator reading the string can catch that. The drawing can, and
these are the tests that it does.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from extraction.glyph_bands import (
    BandSeparationError,
    reading_must_contain_a_fraction,
    stacked_pairs,
)

#: `28 3/4` as a plotter draws it: `2` and `8` on the baseline, then `3` above `4` with a bar.
#: y increases upward, as PDF space does.
STACKED = [
    (Decimal(0), Decimal(0), Decimal(6), Decimal(10)),  # 2
    (Decimal(7), Decimal(0), Decimal(13), Decimal(10)),  # 8
    (Decimal(16), Decimal(6), Decimal(21), Decimal(11)),  # 3, raised
    (Decimal(16), Decimal(0), Decimal(21), Decimal(5)),  # 4, dropped
]

#: `2 - 10` on one line: four glyphs, every one sharing the baseline.
ONE_LINE = [
    (Decimal(0), Decimal(0), Decimal(6), Decimal(10)),
    (Decimal(7), Decimal(0), Decimal(13), Decimal(10)),
    (Decimal(15), Decimal(0), Decimal(21), Decimal(10)),
    (Decimal(22), Decimal(0), Decimal(28), Decimal(10)),
]

SEPARATION = Decimal("0.5")


def test_a_label_on_one_line_holds_no_stacked_pair() -> None:
    """The false positive that would matter.

    A wide dimension label flagged as stacked would refuse a correct reading, and the guard would be
    worse than nothing. No two glyphs of `2' - 10"` compete for one position on the line, however
    far apart the label runs.
    """
    assert stacked_pairs(ONE_LINE, separation_pt=SEPARATION) == 0
    assert not reading_must_contain_a_fraction(ONE_LINE, separation_pt=SEPARATION)


def test_a_stacked_fraction_is_found_through_its_whole_number() -> None:
    """**The case the first implementation got wrong**, kept as the headline test.

    Projecting every glyph onto the across-baseline axis and counting groups finds *one* band here,
    because the full-height `2` and `8` span the numerator's row and the denominator's both — the
    whole number bridges the gap the fraction makes. A fixture containing only `3/4` would have
    passed that implementation and shipped it.
    """
    assert stacked_pairs(STACKED, separation_pt=SEPARATION) == 1
    assert reading_must_contain_a_fraction(STACKED, separation_pt=SEPARATION)


def test_a_descender_is_not_a_stacked_pair() -> None:
    """A comma dips below its neighbours and is not a second row.

    It overlaps them along the line, so the along-baseline test alone would accept it; it is the
    requirement of clear space *across* the baseline that refuses it.
    """
    descender = [*ONE_LINE, (Decimal(27), Decimal(-3), Decimal(30), Decimal(2))]

    assert stacked_pairs(descender, separation_pt=SEPARATION) == 0


def test_the_separation_decides_and_is_not_guessed_here() -> None:
    """The same glyphs are stacked or not depending on the number, which is why it is an argument.

    `#541` is explicit that the threshold cannot be set from the one sheet we hold. This pins that
    the module takes no view: raise the separation past the gap and the stack stops being one.
    """
    assert stacked_pairs(STACKED, separation_pt=Decimal("0.5")) == 1
    assert stacked_pairs(STACKED, separation_pt=Decimal(3)) == 0


def test_a_rotated_label_is_measured_across_its_own_baseline() -> None:
    """Both drawings we hold carry rotated dimension text, so this is the common case.

    `3' - 1"` runs vertically on `AI_Set_2.pdf` and `724 [28 1/2]` runs vertically on
    `demo_pair/shop.pdf`. Measured down the page, a quarter-turned stack looks like two glyphs side
    by side — which would silently switch the guard off exactly where it is needed.
    """
    turned = [(y, x, top, right) for (x, y, right, top) in STACKED]

    assert stacked_pairs(turned, separation_pt=SEPARATION, rotation_degrees=90) == 1
    # Read as upright, the same glyphs give a different answer entirely — the argument is load
    # bearing, not decoration.
    assert stacked_pairs(turned, separation_pt=SEPARATION, rotation_degrees=0) != 1


def test_an_upright_label_read_as_turned_would_refuse_a_correct_reading() -> None:
    """The dangerous direction, and the reason the rotation argument is not cosmetic.

    Every glyph of `2' - 10"` shares the baseline, so read a quarter turn out they all overlap
    "along" and every gap between them becomes clear space "across". The guard then demands a
    fraction of an ordinary label and refuses a correct reading — a false abstention on every
    dimension on the sheet, which is worse than not having the guard.
    """
    assert stacked_pairs(ONE_LINE, separation_pt=SEPARATION, rotation_degrees=0) == 0
    assert stacked_pairs(ONE_LINE, separation_pt=SEPARATION, rotation_degrees=90) > 0


def test_one_stacked_pair_is_enough() -> None:
    """A simple fraction contributes exactly one, so asking for more would miss `28 3/4`."""
    assert reading_must_contain_a_fraction(STACKED, separation_pt=SEPARATION)


def test_fewer_than_two_glyphs_cannot_stack() -> None:
    assert stacked_pairs([], separation_pt=SEPARATION) == 0
    assert stacked_pairs([STACKED[0]], separation_pt=SEPARATION) == 0
    assert not reading_must_contain_a_fraction([], separation_pt=SEPARATION)


@pytest.mark.parametrize("bad", [Decimal(0), Decimal(-1)])
def test_a_separation_that_is_not_a_distance_is_refused(bad: Decimal) -> None:
    """Zero would make every glyph its own band and every reading suspect."""
    with pytest.raises(BandSeparationError, match="positive"):
        stacked_pairs(STACKED, separation_pt=bad)


def test_a_float_separation_is_refused() -> None:
    """ADR-0001. The threshold reaches a comparison, so it is exact or it is refused."""
    with pytest.raises(BandSeparationError, match="finite Decimal"):
        stacked_pairs(STACKED, separation_pt=0.5)  # type: ignore[arg-type]
