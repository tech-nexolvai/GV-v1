"""`units.notation` — how the client's drawings write a dimension (#733).

Every notation here is on the 17-page client set, written by the vendor or by GV's reviewers. The
values are synthetic of the same shape; no client dimension appears in this file.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound


def _inches(written: str) -> Fraction:
    return normalise_to_inches(canonical_notation(written)[0]).exact


@pytest.mark.parametrize(
    ("written", "inches"),
    [
        ('10 1/4"', "41/4"),  # the plain form — worked before, must keep working
        ('10-1/4"', "41/4"),  # hyphenated, as GV's reviewers write it: 20 of 21 recovered (#733)
        ('10-11/16"', "171/16"),
        ("300 [12]", "12"),  # dual unit: the bracketed inch is the value (Q12)
        ("300 [11 1/2]", "23/2"),
        ('300mm [12"]', "12"),  # the same token with its units spelled out, as a model returns it
        ("300 [12″]", "12"),  # typographic double-prime inside the bracket
        ('2" (VIF)', "2"),  # a site note is not part of the number
        ('100 1/4" (4EQ)', "401/4"),
        ("6'-0\"", "72"),  # feet-inches keeps its hyphen
        ("2' - 10\"", "34"),
        ("8'-6''", "102"),  # two apostrophes for the inch mark — a model's typewriter spelling
        ("2'-10-1/2\"", "69/2"),  # feet, then a hyphenated fraction
        ("12″", "12"),
    ],
)
def test_every_notation_on_the_client_sheets_reaches_an_exact_value(
    written: str, inches: str
) -> None:
    assert _inches(written) == Fraction(inches)


@pytest.mark.parametrize("written", ['10 1/4"+6"', '2"+3"', '2"+3"(filler) ', "12″+3″"])
def test_a_compound_is_recognised_rather_than_rewritten(written: str) -> None:
    """Two dimensions and an operator have no single value to rewrite into."""
    assert is_compound(written)


@pytest.mark.parametrize("written", ["2' - 10\"", "6'-0\"", '10-1/4"', "300 [12]"])
def test_feet_inches_and_hyphens_are_not_mistaken_for_compounds(written: str) -> None:
    """The operator must follow an inch mark; a foot mark's hyphen is feet-inches."""
    assert not is_compound(written)


def test_a_dual_token_hands_back_its_millimetres_as_corroboration_only() -> None:
    token, mm = canonical_notation("300 [11 1/2]")

    assert token == '11 1/2"'
    assert mm == "300"


def test_a_value_with_no_unit_is_still_refused() -> None:
    """Canonicalising must never supply a unit: `106 3/4` stays unit-less and unparsed."""
    with pytest.raises(UnitNormalisationError):
        normalise_to_inches(canonical_notation("10 3/4")[0])


@pytest.mark.parametrize("written", ['10-1/4"', "300 [12]", '2" (VIF)', "8'-6''"])
def test_nothing_is_computed(written: str) -> None:
    """Every rewrite drops or re-spaces characters. None adds, converts or rounds a number.

    Held by checking that the digits of the value appear, in order, in what was written — a rewrite
    that did arithmetic would produce digits the drawing never printed.
    """
    token, _mm = canonical_notation(written)
    digits = "".join(ch for ch in token if ch.isdigit())
    source = "".join(ch for ch in written if ch.isdigit())

    assert digits in source
