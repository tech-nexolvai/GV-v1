"""The drawn-length veto: the drawing rejects big misreads and never supplies a value (#992).

Verification for `extraction/slot_reader/veto.py`. Every value is invented; no client value. The
row is drawn at 1/4" per point: a 12" piece is 48 pt long.
"""

from __future__ import annotations

import inspect
from fractions import Fraction

import pytest

from extraction.slot_reader.veto import (
    DRAWN_LENGTH_BAND,
    DRAWN_LENGTH_REASON,
    DrawnReading,
    drawn_length_vetoes,
)

SCALE = Fraction(1, 4)


def piece(
    index: int,
    value: Fraction | int,
    true_inches: Fraction | int | None = None,
    *,
    stacked: bool = False,
) -> DrawnReading:
    """A piece read as `value`, drawn as long as `true_inches` (default: the value) at SCALE."""
    true = Fraction(value if true_inches is None else true_inches)
    return DrawnReading(index, Fraction(value), true / SCALE, stacked)


def overall(value: Fraction | int, true_inches: Fraction | int | None = None) -> DrawnReading:
    true = Fraction(value if true_inches is None else true_inches)
    return DrawnReading(None, Fraction(value), true / SCALE, False)


def test_the_band_is_the_admins_tenth() -> None:
    assert DRAWN_LENGTH_BAND == Fraction(1, 10)


def test_a_stacked_three_quarter_read_as_three_and_three_quarters_is_rejected() -> None:
    row = [piece(0, Fraction(15, 4), Fraction(3, 4), stacked=True), piece(1, 24), piece(2, 30)]
    # The correct overall (54 3/4, drawn so) is kept: it is checked against the pieces' scale.
    assert drawn_length_vetoes(row, overall(Fraction(219, 4))) == {0: DRAWN_LENGTH_REASON}


def test_a_lost_first_digit_is_rejected() -> None:
    row = [piece(0, 24), piece(1, Fraction(3, 2), Fraction(63, 2)), piece(2, 18), piece(3, 30)]
    vetoes = drawn_length_vetoes(row, None)
    assert vetoes[1] == DRAWN_LENGTH_REASON


def test_correct_readings_six_percent_off_the_drawing_are_kept() -> None:
    """E3: correct readings sit within 6.4 % of drawn × scale; none may be rejected."""
    row = [
        piece(0, Fraction(3, 4), Fraction(3, 4) * Fraction(106, 100), stacked=True),
        piece(1, 24, 24 * Fraction(94, 100)),
        piece(2, 30, 30 * Fraction(103, 100)),
        piece(3, Fraction(3, 4), Fraction(3, 4) * Fraction(94, 100)),
    ]
    assert drawn_length_vetoes(row, overall(Fraction(111, 2))) == {}


def test_fewer_than_two_scale_pieces_vetoes_nothing() -> None:
    wrong = piece(0, Fraction(15, 4), Fraction(3, 4))
    assert drawn_length_vetoes([wrong, piece(1, 24)], None) == {}
    # Stacked pieces never set the scale: two of them leave one scale piece.
    stacked = [
        piece(0, Fraction(15, 4), Fraction(3, 4), stacked=True),
        piece(1, Fraction(1, 2), stacked=True),
        piece(2, 24),
    ]
    assert drawn_length_vetoes(stacked, None) == {}
    assert drawn_length_vetoes([], overall(36)) == {}


def test_the_overall_is_checked_but_never_sets_the_scale() -> None:
    row = [piece(0, 12), piece(1, 24), piece(2, 12)]
    assert drawn_length_vetoes(row, overall(48)) == {}
    assert drawn_length_vetoes(row, overall(4, 48)) == {None: DRAWN_LENGTH_REASON}
    # A wrong overall cannot make a correct piece look wrong.
    assert drawn_length_vetoes(row, overall(480, 48)) == {None: DRAWN_LENGTH_REASON}


def test_a_big_misread_can_also_hold_the_small_pieces_it_skews() -> None:
    """Leave-one-out, as E3 measured it: the misread is never in its own scale, but it is in its
    neighbours'. In a short row it can push them out of the band too — which only holds them back,
    and the row was going to the person anyway."""
    row = [piece(0, Fraction(15, 4), Fraction(3, 4)), piece(1, 3), piece(2, 2)]
    assert set(drawn_length_vetoes(row, None)) == {0, 1, 2}


def test_the_veto_returns_only_reasons_never_a_value() -> None:
    assert set(inspect.signature(drawn_length_vetoes).parameters) == {"pieces", "overall"}
    row = [piece(0, 12), piece(1, 24), piece(2, 1, 12)]
    before = list(row)
    result = drawn_length_vetoes(row, None)
    assert result[2] == DRAWN_LENGTH_REASON
    assert all(isinstance(reason, str) for reason in result.values())
    assert row == before, "the readings are untouched"


def test_drawn_lengths_and_values_are_exact() -> None:
    with pytest.raises(TypeError):
        DrawnReading(0, 12.0, Fraction(48), False)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DrawnReading(0, Fraction(12), Fraction(0), False)
