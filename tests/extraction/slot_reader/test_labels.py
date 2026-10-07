"""Worded and summed labels, expanded from agreed text by exact arithmetic (#992).

Verification for `extraction/slot_reader/labels.py`. Every value is invented; no client value.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from extraction.slot_reader.labels import (
    Expansion,
    expand_label,
    plain_dimension,
    row_hold,
)
from units.measurement import Unit


@pytest.mark.parametrize(
    ("text", "value", "parts"),
    [
        ('4"+1" Filler', Fraction(5), (Fraction(4), Fraction(1))),
        ('4"+1"', Fraction(5), (Fraction(4), Fraction(1))),
        ('4" + 1" FILLER', Fraction(5), (Fraction(4), Fraction(1))),
        ('1 1/2"+3/4"', Fraction(9, 4), (Fraction(3, 2), Fraction(3, 4))),
        ('2"+2"+1"', Fraction(5), (Fraction(2), Fraction(2), Fraction(1))),
    ],
)
def test_a_sum_is_one_piece_of_the_exact_total(
    text: str, value: Fraction, parts: tuple[Fraction, ...]
) -> None:
    expanded = expand_label(text)
    assert expanded is not None
    assert expanded.how is Expansion.SUM
    assert expanded.value.exact == value and expanded.value.unit is Unit.INCH
    assert expanded.parts == parts
    assert expanded.value.raw_text == text


@pytest.mark.parametrize(
    ("text", "value", "count"),
    [
        ('96"(6EQ)', 96, 6),
        ('96" (6 EQ)', 96, 6),
        ('120"(8eq)', 120, 8),
        ('60 1/2"(2EQ)', Fraction(121, 2), 2),
    ],
)
def test_equal_shares_are_one_entry_of_their_total(
    text: str, value: Fraction | int, count: int
) -> None:
    expanded = expand_label(text)
    assert expanded is not None
    assert expanded.how is Expansion.EQUAL_SHARES
    assert expanded.value.exact == Fraction(value)
    assert expanded.shares == count


@pytest.mark.parametrize(
    "text",
    [
        '4+1"',  # a part without its inch mark is not assumed to be inches
        "4+1",
        '4"+1" Panel',  # any other word: the person
        '4"+1" Filler extra',
        '4"-1"',
        '96"(1EQ)',  # one share is not a count of equal cabinets
        "(6EQ)",
        '(6EQ) 96"',
        '96" 6EQ',
        '30" (INCLUDING FIELD CUT)',
        '30"+2" VIF',
        "",
        '14 3/8"',  # plain: `plain_dimension`'s, not an expansion
    ],
)
def test_anything_else_is_not_expanded(text: str) -> None:
    assert expand_label(text) is None


def test_plain_dimensions_are_unchanged() -> None:
    value = plain_dimension('14 3/8"')
    assert value is not None and value.exact == Fraction(115, 8)
    assert plain_dimension("30") is None
    assert plain_dimension('4"+1"') is None


def test_field_cut_and_vif_hold_the_row_with_the_admins_reasons() -> None:
    included = row_hold(['12"', '30" (INCLUDING  FIELD CUT)'])
    assert included is not None and included.code == "field-cut-included"
    assert included.reason == "width already includes the field cut"
    vif = row_hold(["990 [39]VIF"])
    assert vif is not None and vif.code == "vif"
    assert vif.reason == "VIF: provisional, verify in field"
    assert row_hold(["990 [39] vif"]) is not None


def test_field_cut_is_named_before_vif_and_neither_hides_in_a_word() -> None:
    both = row_hold(["VIF", "INCLUDING FIELD CUT"])
    assert both is not None and both.code == "field-cut-included"
    assert row_hold(["VIFS", "MOTIVIF", '12"', "FIELD CUT"]) is None
    assert row_hold([]) is None
