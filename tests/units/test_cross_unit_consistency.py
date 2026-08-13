"""Verification for issue #42: rounding-aware dual-unit corroboration.

Source: ``docs/V1_RESEARCH_AND_PLAN.md`` sections F1 and F2.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from units.dual import DualDimension, parse_dual
from units.errors import UnknownRoundingError
from units.measurement import Measurement, Unit
from units.policy import Consistency, check_dual, rounding_band


@pytest.mark.parametrize(
    "text",
    [
        "6012 [236 3/4]",
        "2025 [79 3/4]",
        "1968 [77 1/2]",
        "984 [38 3/4]",
        "864 [34]",
        "100 [4]",
    ],
)
def test_real_drawing_pairs_are_consistent_within_authored_rounding(text: str) -> None:
    """Every measured row in the F1 table is a valid independently rounded pair."""

    assert check_dual(parse_dual(text)) is Consistency.CONSISTENT_WITHIN_ROUNDING


def test_transcription_error_is_inconsistent() -> None:
    """A one-inch transcription error is far outside the derived rounding allowance."""

    assert check_dual(parse_dual("984 [39 3/4]")) is Consistency.INCONSISTENT


def test_single_unit_token_is_not_corroborated_or_conflicting() -> None:
    """Missing alternate evidence is its own state, never false support or conflict."""

    result = check_dual(parse_dual("984"))

    assert result is Consistency.NOT_CORROBORATED
    assert result is not Consistency.CONSISTENT_WITHIN_ROUNDING
    assert result is not Consistency.INCONSISTENT
    assert len(set(Consistency)) == 3


@pytest.mark.parametrize(
    ("measurement", "expected"),
    [
        (Measurement(Fraction(984), Unit.MM, "984"), Decimal("0.5")),
        (Measurement(Fraction(984), Unit.MM, "984.0"), Decimal("0.05")),
        (Measurement(Fraction(155, 4), Unit.INCH, "38 3/4"), Decimal("3.175")),
        (Measurement(Fraction(19, 8), Unit.INCH, "2.375"), Decimal("0.01270")),
        (Measurement(Fraction(34), Unit.INCH, '34"'), Decimal("12.7")),
    ],
)
def test_rounding_band_is_derived_from_written_precision(
    measurement: Measurement, expected: Decimal
) -> None:
    """Whole, fractional and decimal tokens each imply their own quantum."""

    assert rounding_band(measurement) == expected


def test_decimal_band_comes_from_authored_places_not_fraction_denominator() -> None:
    """The written thousandth must not be widened to the reduced fraction's eighth."""

    decimal = Measurement(Fraction(19, 8), Unit.INCH, "2.375")
    mixed = Measurement(Fraction(19, 8), Unit.INCH, "2 3/8")

    assert rounding_band(decimal) == Decimal("0.01270")
    assert rounding_band(mixed) == Decimal("1.5875")


def test_computed_measurement_has_no_authored_rounding_band() -> None:
    """Arithmetic removes raw_text, so a caller cannot invent precision afterward."""

    computed = Measurement(Fraction(1), Unit.MM, "1") + Measurement(Fraction(2), Unit.MM, "2")

    assert computed.raw_text is None
    with pytest.raises(UnknownRoundingError):
        rounding_band(computed)


def test_check_dual_propagates_unknown_authored_precision() -> None:
    """An unbanded authored reading cannot be silently accepted as corroboration."""

    dimension = DualDimension(
        primary=Measurement(Fraction(984), Unit.MM, None),
        alternate=Measurement(Fraction(155, 4), Unit.INCH, "38 3/4"),
    )

    with pytest.raises(UnknownRoundingError):
        check_dual(dimension)
