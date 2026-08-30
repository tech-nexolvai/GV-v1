"""Reading millimetre and feet dimensions into inches, exactly.

Source: `docs/decisions/CALL_2026_08_25_INPUTS.md` N3 (convert mm and feet to inches at read time;
yards out of scope), `docs/CLIENT_FACTS.md` Q12 (inches govern, mm never decides) and Q2 (exact
match, no tolerance band).
Verification for: `units/normalise.py`.

The tests worth reading are the last two groups: that nothing is rounded, and that an unknown unit is
refused rather than assumed to be inches.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.measurement import Unit
from units.normalise import (
    INCHES_PER_FOOT,
    INCHES_PER_MM,
    UnitNormalisationError,
    inches_from_feet,
    inches_from_mm,
    normalise_to_inches,
)

# ---------------------------------------------------------------------------
# Feet — the clean case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("3'", Fraction(36)),
        ("3 ft", Fraction(36)),
        ("3ft", Fraction(36)),
        ("3 feet", Fraction(36)),
        ("3'-6\"", Fraction(42)),
        ("3'-6", Fraction(42)),
        ("3'-6 1/2\"", Fraction(85, 2)),
        ("3' 6 1/2", Fraction(85, 2)),
        ("0'-7 3/8", Fraction(59, 8)),
    ],
)
def test_feet_and_inches_convert_exactly(token: str, expected: Fraction) -> None:
    """Twelve inches to the foot is a definition, so nothing is lost either way."""
    measurement = normalise_to_inches(token)

    assert measurement.exact == expected
    assert measurement.unit is Unit.INCH


def test_the_original_token_is_kept() -> None:
    """A reviewer is shown what the drawing said, not only what it became.

    `3'-6"` and `42` are the same length and not the same evidence: one of them is what somebody can
    go and find on the page.
    """
    assert normalise_to_inches("3'-6\"").raw_text == "3'-6\""


# ---------------------------------------------------------------------------
# Millimetres
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("984 mm", Fraction(4920, 127)),
        ("984mm", Fraction(4920, 127)),
        ("610 MM", Fraction(3050, 127)),
    ],
)
def test_millimetres_convert_exactly(token: str, expected: Fraction) -> None:
    assert normalise_to_inches(token).exact == expected


def test_a_converted_millimetre_value_is_not_rounded_to_a_drawn_inch() -> None:
    """**The consequence of N3 meeting Q2, stated as a test so nobody discovers it in production.**

    984 mm is exactly 4920/127 inches — about 38.7402. A drawing that means that almost certainly
    writes `38 3/4`, which is 155/4. They are not equal, there is no tolerance band in V1, so the
    comparison FAILS.

    That is the honest answer. Rounding the converted value to the nearest sixteenth would make this
    pass, and it would do so by deciding what the drawing *meant* — a guess on the number that gets
    cut. Under Q4 everything is flagged for a reviewer anyway; a flag reading "these do not agree
    exactly" is true, where a PASS arithmetic did not earn is not.

    If this test ever fails because rounding was added, that is the conversation to have, loudly.
    """
    converted = normalise_to_inches("984 mm").exact
    drawn = Fraction(155, 4)

    assert converted != drawn
    assert converted == Fraction(4920, 127)
    # Close enough that somebody will be tempted; far enough that exact match refuses it.
    assert abs(converted - drawn) < Fraction(1, 100)


# ---------------------------------------------------------------------------
# Inches passed through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ('38 3/4"', Fraction(155, 4)),
        ("38 3/4", Fraction(155, 4)),
        ("4", Fraction(4)),
        ("2.375", Fraction(19, 8)),
    ],
)
def test_plain_inch_tokens_pass_through_unchanged(token: str, expected: Fraction) -> None:
    assert normalise_to_inches(token).exact == expected


# ---------------------------------------------------------------------------
# What it refuses, and why refusing matters more than converting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["5 yd", "2 yards", "7 YARD", "1 yds"])
def test_yards_are_refused_by_decision_rather_than_ignored(token: str) -> None:
    """N3 puts yards out of scope. Refused explicitly, not left to fall through the unknown-unit
    branch, so the error says *why* — an omission and a decision read identically otherwise."""
    with pytest.raises(UnitNormalisationError, match="out of scope"):
        normalise_to_inches(token)


@pytest.mark.parametrize("token", ["banana", "38 3/4 cm", "12 m", "", "   ", "38-3/4-mm-ish"])
def test_an_unreadable_dimension_raises_rather_than_returning_something(token: str) -> None:
    """**The important one.**

    The tempting failure mode is to strip the unit nobody recognised and read the number as inches.
    `38 3/4 cm` would then pass as 38 3/4 inches — a real number, correctly parsed, meaning a length
    fifteen inches longer than the drawing says. A dimension whose unit cannot be established is an
    ambiguity, and the caller turns it into an abstention.
    """
    with pytest.raises(UnitNormalisationError):
        normalise_to_inches(token)


def test_a_non_string_is_refused() -> None:
    with pytest.raises(UnitNormalisationError):
        normalise_to_inches(984)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The bare converters
# ---------------------------------------------------------------------------


def test_the_ratios_are_exact_fractions_and_never_floats() -> None:
    """A float here would put binary rounding into the operand that decides — ADR-0001."""
    assert INCHES_PER_MM == Fraction(5, 127)
    assert INCHES_PER_FOOT == Fraction(12)
    assert isinstance(inches_from_mm(984), Fraction)
    assert isinstance(inches_from_feet(3), Fraction)


def test_the_bare_converters_agree_with_the_parser() -> None:
    """Two ways to the same number, so a caller holding a value rather than a token gets no
    different answer."""
    assert inches_from_mm(984) == normalise_to_inches("984 mm").exact
    assert inches_from_feet(3) == normalise_to_inches("3 ft").exact


def test_conversion_round_trips_through_the_measurement_helper() -> None:
    """`Measurement.to` converts the other way with the same ratio; agreeing with it is what keeps
    this module from becoming a second, subtly different definition of an inch."""
    from units.measurement import Measurement

    as_mm = Measurement(Fraction(984), Unit.MM, "984")

    assert as_mm.to(Unit.INCH).exact == normalise_to_inches("984 mm").exact
