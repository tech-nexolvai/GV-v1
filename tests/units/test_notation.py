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
        # Curly quotes and a real minus: how a PDF font sets a straight `'`, `"` and `-` (formats
        # phase 1, read from the client's stamps).
        ('2’ -5"', "29"),
        ("2' -5”", "29"),
        ('3’ − 6"', "42"),
        ("36”", "36"),
        ("1’–0”", "12"),
        ("8’-6’’", "102"),  # two curly quotes for the inch mark, as two straight ones are read
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


@pytest.mark.parametrize(
    "written", ["2' - 10\"", "6'-0\"", '10-1/4"', "300 [12]", '3’ − 6"', "2’ -5”"]
)
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


# ---------------------------------------------------------------------------
# Fraction symbols and the fraction slash (#907)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "inches"),
    [
        ('10½"', "21/2"),  # a model writes the one character for a half
        ('10 ½"', "21/2"),  # with a space already there, no second space is added
        ('10-½"', "21/2"),  # hyphenated, then joined as a hyphenated fraction is
        ('7¾"', "31/4"),
        ('9⅞"', "79/8"),
        ('6 5⁄16"', "101/16"),  # the fraction slash between two digits
        ('5⁄16"', "5/16"),
        ("300 [11½]", "23/2"),  # inside a dual token's bracket
    ],
)
def test_a_fraction_symbol_is_read_as_the_fraction_it_is(written: str, inches: str) -> None:
    """**#907.** A reader that writes `½` or the fraction slash read the label right; before this
    its reading had no value, lost to formatting."""
    assert _inches(written) == Fraction(inches)


def test_every_fraction_symbol_is_written_as_unicode_decomposes_it() -> None:
    """**Taken from the standard, not a table.** For every character Unicode tags as a vulgar
    fraction, the digits written in its place are its own decomposition — numerator, fraction slash,
    denominator — with the slash as `/`. A wrong digit cannot come from here."""
    import sys
    import unicodedata

    symbols = [
        chr(code)
        for code in range(sys.maxunicode + 1)
        if unicodedata.decomposition(chr(code)).startswith("<fraction>")
    ]
    assert "½" in symbols and "⅞" in symbols, "the standard's fraction characters were not found"
    for symbol in symbols:
        expected = unicodedata.normalize("NFKD", symbol).replace("⁄", "/")
        assert canonical_notation(f'{symbol}"')[0] == f'{expected}"', symbol
        assert canonical_notation(f'4{symbol}"')[0] == f'4 {expected}"', symbol


@pytest.mark.parametrize(
    "written",
    [
        r"10\frac{1}{2}",  # LaTeX is not the drawing's notation and is never rewritten
        r'10 \frac{1}{2}"',
        r"\tfrac{3}{8}",
        '10 ¹⁄₂"',  # superscript and subscript digits are not digits the parser reads
    ],
)
def test_nothing_but_the_fraction_symbols_and_the_slash_is_rewritten(written: str) -> None:
    """**Only those two.** A `\\frac` stays exactly as written and has no value; so does a fraction
    built from superscript and subscript digits, whose slash alone is rewritten."""
    token, _mm = canonical_notation(written)
    assert "\\" not in written or token == written
    with pytest.raises(UnitNormalisationError):
        normalise_to_inches(token)


def test_a_fraction_symbol_alone_is_still_a_bare_fraction() -> None:
    """`½"` alone is a half inch, written as `1/2"`, which the vision shape check refuses as a
    bare fraction exactly as it refuses a typed `1/2"` — a dropped whole number must not pass."""
    from extraction.models.validation import _reading_refusal

    assert canonical_notation('½"')[0] == '1/2"'
    refusal = _reading_refusal('½"')
    assert refusal is not None and "no whole number" in refusal


def test_a_reading_with_no_fraction_symbol_is_left_alone() -> None:
    """The rule touches nothing else: a token with neither character comes back as it went in."""
    from units.notation import _fraction_symbols

    for written in ['10 1/4"', "300 [12]", "8'-6''", "abc", "", "10/4"]:
        assert _fraction_symbols(written) == written
