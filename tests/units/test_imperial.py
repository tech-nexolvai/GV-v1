"""Verification for issue #40: exact parsing of client-authored inch values."""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.imperial import ImperialParseError, format_inches, parse_imperial


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


# ---------------------------------------------------------------------------
# Rendering, which is the other half of parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(7, 2), "3 1/2"),
        (Fraction(155, 4), "38 3/4"),
        (Fraction(4), "4"),
        (Fraction(1, 4), "1/4"),
        (Fraction(0), "0"),
        (Fraction(-7, 2), "-3 1/2"),
    ],
)
def test_format_inches_writes_what_a_drawing_writes(value: Fraction, expected: str) -> None:
    """`7/2` is correct arithmetic and the wrong thing to show a reviewer.

    Dimensions are called out on drawings as whole-and-fraction. Somebody checking a report against
    a page should not have to convert in their head before they can tell whether the two agree.
    """
    assert format_inches(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        Fraction(7, 2),
        Fraction(155, 4),
        Fraction(4),
        Fraction(1, 4),
        Fraction(4920, 127),
        # Negatives were the case the claim failed on: `format_inches` wrote `-3 1/2` and
        # `parse_imperial` refused it, so the round-trip docstring was false for exactly the values a
        # derived dimension produces when a subtraction comes out negative.
        Fraction(-7, 2),
        Fraction(-1, 4),
        Fraction(-4),
    ],
)
def test_everything_it_writes_can_be_read_back(value: Fraction) -> None:
    """The pair must not drift into two different ideas of an inch. Round-tripping is what stops a
    renderer quietly rounding: a value that came back different would fail here."""
    assert parse_imperial(format_inches(value)) == value


def test_formatting_changes_how_a_number_is_written_and_never_what_it_is() -> None:
    """4920/127 is the exact conversion of 984 mm. It has no tidy mixed form, and the renderer must
    not invent one — it writes `38 94/127` rather than rounding to a sixteenth."""
    assert format_inches(Fraction(4920, 127)) == "38 94/127"


def test_a_non_fraction_is_refused() -> None:
    """A float here would mean binary rounding reached a reviewer-facing number."""
    with pytest.raises(ImperialParseError):
        format_inches(3.5)  # type: ignore[arg-type]
