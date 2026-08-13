"""Verification for issue #41: exact parsing of dual dimensions."""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.dual import DualDimensionParseError, parse_dual
from units.measurement import Measurement, Unit


@pytest.mark.parametrize(
    ("text", "primary_text", "primary_exact", "alternate_text", "alternate_exact"),
    [
        ("984 [38 3/4]", "984", Fraction(984), "38 3/4", Fraction(155, 4)),
        ("6012 [236 3/4]", "6012", Fraction(6012), "236 3/4", Fraction(947, 4)),
        ("51 [2]", "51", Fraction(51), "2", Fraction(2)),
        ("864 [34]", "864", Fraction(864), "34", Fraction(34)),
    ],
)
def test_parse_dual_preserves_both_drawing_readings_exactly(
    text: str,
    primary_text: str,
    primary_exact: Fraction,
    alternate_text: str,
    alternate_exact: Fraction,
) -> None:
    """Real drawing fixtures retain both independently authored readings."""

    result = parse_dual(text)

    assert result.primary == Measurement(primary_exact, Unit.MM, primary_text)
    assert result.alternate == Measurement(alternate_exact, Unit.INCH, alternate_text)


def test_single_unit_dimension_has_no_alternate() -> None:
    """A missing alternate is valid and must not cause a false error."""

    result = parse_dual("984")

    assert result.primary == Measurement(Fraction(984), Unit.MM, "984")
    assert result.alternate is None


def test_surrounding_whitespace_is_ignored() -> None:
    """Whitespace outside the dimension is not part of either source token."""

    result = parse_dual("  51 [2]  ")

    assert result.primary.raw_text == "51"
    assert result.alternate is not None
    assert result.alternate.raw_text == "2"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "[]",
        "[38 3/4]",
        "984 []",
        "984 [38 3/4",
        "984 38 3/4]",
        "984 [3/0]",
        "984 [about 38 3/4]",
        "984 [38 3/4] extra",
        "984 [38] [3/4]",
        "about 984 [38 3/4]",
    ],
)
def test_parse_dual_rejects_malformed_tokens(text: str) -> None:
    """Malformed dimensions fail explicitly instead of being partially guessed."""

    with pytest.raises(DualDimensionParseError):
        parse_dual(text)


@pytest.mark.parametrize("value", [None, 984, Fraction(984)])
def test_parse_dual_rejects_non_text_input(value: object) -> None:
    """The parser boundary accepts only drawing text."""

    with pytest.raises(DualDimensionParseError):
        parse_dual(value)  # type: ignore[arg-type]
