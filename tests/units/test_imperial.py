"""Verification for issue #40: exact parsing of client-authored inch values."""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.imperial import ImperialParseError, parse_imperial


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4", Fraction(4)),
        ("3/4", Fraction(3, 4)),
        ("38 3/4", Fraction(155, 4)),
        ("2.375", Fraction(19, 8)),
        ('5"', Fraction(5)),
        ('2.375"', Fraction(19, 8)),
        ('2 3/8"', Fraction(19, 8)),
        ("  1/16  ", Fraction(1, 16)),
    ],
)
def test_parse_imperial_returns_exact_fractions(text: str, expected: Fraction) -> None:
    """Every observed input form produces its exact value, never a float."""

    assert parse_imperial(text) == expected


def test_decimal_and_mixed_fraction_are_equivalent() -> None:
    """The equivalent forms explicitly required by issue #40 agree exactly."""

    assert parse_imperial('2.375"') == parse_imperial('2 3/8"')


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "3/0",
        "3//4",
        "38 3/",
        "38 /4",
        "about 4",
        "4 mm",
        "5 cm",
        "2 ft",
        '5""',
    ],
)
def test_parse_imperial_rejects_malformed_or_unknown_units(text: str) -> None:
    """Bad or non-inch text must abstain rather than becoming a guessed value."""

    with pytest.raises(ImperialParseError):
        parse_imperial(text)


@pytest.mark.parametrize("value", [None, 4, Fraction(3, 4)])
def test_parse_imperial_rejects_non_text_input(value: object) -> None:
    """The parser boundary accepts only the string type in its public signature."""

    with pytest.raises(ImperialParseError):
        parse_imperial(value)  # type: ignore[arg-type]
