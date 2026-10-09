"""An architect's label: the number, the words printed with it, and whether it may be used (#1052).

Verification for: `extraction/architect/labels.py`. Synthetic labels only; no client value.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from extraction.architect.labels import Qualifier, read_label, split_label


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3' - 6\"", Fraction(42)),
        ("3’ − 6\"", Fraction(42)),
        ("3'6\"", Fraction(42)),
        ("0'-6\"", Fraction(6)),
        ("11\"", Fraction(11)),
        ("2'-0 1/2\"", Fraction(49, 2)),
        ("2'-0½\"", Fraction(49, 2)),
    ],
)
def test_a_plain_dimension_label_reads_exactly(text: str, expected: Fraction) -> None:
    reading = read_label(text)

    assert reading.held_reason is None
    assert reading.inches == expected
    assert reading.text == text


@pytest.mark.parametrize(
    ("text", "qualifier"),
    [
        ("3'-6\" VIF", Qualifier.VIF),
        ("3'-6\" (VIF)", Qualifier.VIF),
        ("3'-6\" V.I.F.", Qualifier.VIF),
        ("±3'-6\"", Qualifier.PLUS_MINUS),
        ("3'-6\" +/-", Qualifier.PLUS_MINUS),
        ("3'-6\" HOLD", Qualifier.HOLD),
    ],
)
def test_a_qualifier_that_says_not_firm_holds_the_label_and_is_kept(
    text: str, qualifier: Qualifier
) -> None:
    """Never stripped: the flag is kept, the number is printed for a person, and it is not usable."""
    reading = read_label(text)

    assert qualifier in reading.qualifiers
    assert reading.printed_inches == Fraction(42)
    assert reading.inches is None
    assert reading.held_reason is not None and "not firm" in reading.held_reason


@pytest.mark.parametrize(
    ("text", "qualifier"),
    [
        ("3'-6\" EQ", Qualifier.EQ),
        ("3'-6\" TYP", Qualifier.TYP),
        ("3'-6\" TYP.", Qualifier.TYP),
        ("3'-6\" CLR", Qualifier.CLR),
        ("3'-6\" MIN", Qualifier.MIN),
        ("3'-6\" MAX", Qualifier.MAX),
        ("3'-6\" AFF", Qualifier.AFF),
        ("3'-6\" NOM", Qualifier.NOM),
    ],
)
def test_other_qualifiers_are_flags_on_a_usable_number(text: str, qualifier: Qualifier) -> None:
    reading = read_label(text)

    assert reading.qualifiers == frozenset({qualifier})
    assert reading.inches == Fraction(42)


@pytest.mark.parametrize(
    "text",
    [
        "6\" WOODEN BLOCKING",  # a note, not a dimension label
        "3'-6\" 2'-0\"",  # two dimensions in one label
        "EQ",  # no number at all
        "3'-1 1/3\"",  # thirds are not drawn
        "11 1/10\"",  # neither are tenths, in inches alone either
        "3'-14\"",  # the inch part of feet-and-inches is under a foot
        "36",  # a bare number has no unit
    ],
)
def test_anything_else_is_held_with_a_reason_and_has_no_value(text: str) -> None:
    reading = read_label(text)

    assert reading.inches is None
    assert reading.held_reason


def test_split_keeps_the_dimension_as_printed() -> None:
    split = split_label("2’ − 1\" TYP")

    assert split.dimension == "2' - 1\""
    assert split.qualifiers == frozenset({Qualifier.TYP})
    assert split.other_words == ()
