"""The view-scale check on one sealed reading: rejects, witnesses, or says nothing.

Verification for `extraction/slot_reader/view_scale.py` (and `veto.outside_band`, which it shares
with the row veto). Every value is invented; no client value. The view is drawn at 1/4" per point:
a 12" dimension is 48 pt long.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from extraction.slot_reader.veto import DRAWN_LENGTH_BAND, outside_band
from extraction.slot_reader.view_scale import ScaleVerdict, view_scale_check

SCALE = Fraction(1, 4)


def drawn_for(inches: Fraction | int) -> Fraction:
    return Fraction(inches) / SCALE


def test_a_reading_on_its_drawn_length_is_witnessed() -> None:
    assert view_scale_check(Fraction(68), drawn_for(68), SCALE) is ScaleVerdict.WITNESSED


def test_an_upside_down_misread_is_vetoed() -> None:
    """68 drawn, 89 read: 31 % off the drawing."""
    assert view_scale_check(Fraction(89), drawn_for(68), SCALE) is ScaleVerdict.VETOED


def test_exactly_a_tenth_away_is_inside_the_band() -> None:
    expected = Fraction(40)
    assert view_scale_check(Fraction(44), drawn_for(expected), SCALE) is ScaleVerdict.WITNESSED
    assert view_scale_check(Fraction(36), drawn_for(expected), SCALE) is ScaleVerdict.WITNESSED


def test_just_past_a_tenth_is_vetoed_both_ways() -> None:
    expected = Fraction(40)
    tiny = Fraction(1, 1000)
    assert view_scale_check(Fraction(44) + tiny, drawn_for(expected), SCALE) is ScaleVerdict.VETOED
    assert view_scale_check(Fraction(36) - tiny, drawn_for(expected), SCALE) is ScaleVerdict.VETOED


def test_the_band_is_the_row_vetos_band() -> None:
    assert DRAWN_LENGTH_BAND == Fraction(1, 10)
    assert not outside_band(Fraction(11), Fraction(10))
    assert outside_band(Fraction(11) + Fraction(1, 10**9), Fraction(10))


@pytest.mark.parametrize(("drawn", "scale"), [(None, SCALE), (drawn_for(12), None), (None, None)])
def test_no_line_or_no_scale_says_nothing(drawn: Fraction | None, scale: Fraction | None) -> None:
    assert view_scale_check(Fraction(12), drawn, scale) is ScaleVerdict.NO_SCALE


def test_a_float_is_refused_anywhere() -> None:
    with pytest.raises(TypeError):
        view_scale_check(12.0, drawn_for(12), SCALE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        view_scale_check(Fraction(12), 48.0, SCALE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        view_scale_check(Fraction(12), drawn_for(12), 0.25)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        outside_band(12.0, Fraction(12))  # type: ignore[arg-type]


def test_a_value_drawn_length_or_scale_that_is_not_positive_is_a_mistake() -> None:
    with pytest.raises(ValueError):
        view_scale_check(Fraction(0), drawn_for(12), SCALE)
    with pytest.raises(ValueError):
        view_scale_check(Fraction(12), Fraction(0), SCALE)
    with pytest.raises(ValueError):
        view_scale_check(Fraction(12), drawn_for(12), Fraction(-1, 4))
    with pytest.raises(ValueError):
        outside_band(Fraction(12), Fraction(0))
