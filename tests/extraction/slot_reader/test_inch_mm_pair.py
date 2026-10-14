"""A label of two numbers, one in brackets: which is the inch, and when neither is (#1263).

Verification for `extraction/slot_reader/labels.py` (`plain_dimension`, `mm_inch_refusal`) and the
seal's use of it. Every value is invented; no client value.

The rule: a label with one number outside brackets and one inside is read as inches only when the
pair corroborates, one being the other times 25.4 within the two printed roundings. The inch number
is then the value, whichever side it is printed on. Neither, or both: held. Millimetres never give
the value; they only show which number is the inch.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from extraction.slot_reader.labels import mm_inch_refusal, odd_fraction, plain_dimension
from extraction.slot_reader.seal import LabelState, normalise_text
from tests.extraction.slot_reader.test_seal import (
    GLYPH,
    KIMI,
    OPUS,
    QWEN,
    SONNET,
    TEXT,
    answer,
    seal,
)
from units.dual import DualDimension
from units.measurement import Measurement, Unit
from units.policy import Consistency, check_dual


def _inches(text: str, *, allow_explicit_mm: bool = False) -> Fraction | None:
    value = plain_dimension(normalise_text(text), allow_explicit_mm=allow_explicit_mm)
    if value is None:
        return None
    assert value.unit is Unit.INCH
    assert value.raw_text == normalise_text(text)
    return value.exact


@pytest.mark.parametrize(
    ("text", "inches"),
    [
        # mm [in], the form the vendors print most: unchanged.
        ("610 [24]", Fraction(24)),
        ("648 [25 1/2]", Fraction(51, 2)),
        ("250 [9 7/8]", Fraction(79, 8)),
        ("19 [3/4]", Fraction(3, 4)),
        ("421 [16 9/16]", Fraction(265, 16)),
        ('610 [24"]', Fraction(24)),
        ("629 [24-3/4]", Fraction(99, 4)),
        # The vendor's inch is the length rounded to the fraction it prints: 1449 mm is 57.05".
        ("1449 [57]", Fraction(57)),
        # in [mm]: the inch is outside, so it is the value (#1263: `25 [648]` was 648 inches).
        ("25 [648]", Fraction(25)),
        ('25" [635]', Fraction(25)),
        ("25 1/2 [648]", Fraction(51, 2)),
        ('24 3/4" [629]', Fraction(99, 4)),
        ("24-3/4 [629]", Fraction(99, 4)),
        ('24-3/4" [629]', Fraction(99, 4)),
        ("3/4 [19]", Fraction(3, 4)),
        # A line break between the two numbers (the label printed on two lines).
        ("648\n[25 1/2]", Fraction(51, 2)),
        ("25\n[635]", Fraction(25)),
    ],
)
def test_a_corroborated_pair_is_read_as_its_inch_number_on_either_side(
    text: str, inches: Fraction
) -> None:
    assert _inches(text) == inches
    assert mm_inch_refusal(normalise_text(text)) is None


@pytest.mark.parametrize(
    "text",
    [
        "305 [10]",  # 10" is 254 mm
        "25 [600]",  # 25" is 635 mm, and 600" is not 25 mm
        "330 [12 1/2]",
        "53 [7]",  # a cut-off `178 [7]`
        "81 [31 1/4]",  # a cut-off `794 [31 1/4]`
        "9 [2 1/2]",
        "104 [43]",
        "25 [25]",
    ],
)
def test_a_pair_that_does_not_corroborate_is_held_with_a_reason(text: str) -> None:
    assert _inches(text) is None
    reason = mm_inch_refusal(text)
    assert reason is not None and "millimetre" in reason


@pytest.mark.parametrize(
    "text",
    [
        '635" [25]',  # marked inches outside, but it is the bracket's number times 25.4
        '25 [635"]',  # marked inches inside, but it is the millimetres
        '25" [635"]',  # both marked inches
        "635 mm [25 mm]",
    ],
)
def test_a_mark_that_contradicts_the_pair_holds_it(text: str) -> None:
    assert _inches(text, allow_explicit_mm=True) is None
    assert mm_inch_refusal(text, allow_explicit_mm=True) is not None


def test_the_word_mm_is_still_only_read_for_the_claude_pair() -> None:
    for text in ("635 mm [25]", '635mm [25"]', "25 [635 mm]", '25" [635mm]'):
        assert _inches(text) is None, text
        assert _inches(text, allow_explicit_mm=True) == Fraction(25), text


@pytest.mark.parametrize(
    "text",
    [
        "648",  # millimetres only, or no unit: never assumed to be inches
        "648 mm",
        "[648]",
        "[25 1/2]",
        "[648] [25 1/2]",
        "1124 [44 1 1/4]",  # a stacked fraction read as two
        "648 [25 1/2] 3",
        "648 [",
        "2'-0\" [610]",
    ],
)
def test_anything_else_with_a_bracket_or_mm_alone_is_never_a_value(text: str) -> None:
    assert _inches(text) is None
    assert _inches(text, allow_explicit_mm=True) is None


@pytest.mark.parametrize(
    ("text", "inches"),
    [('25"', Fraction(25)), ('24-3/4"', Fraction(99, 4)), ("30", None), ('163/8"', None)],
)
def test_a_single_number_keeps_todays_reading(text: str, inches: Fraction | None) -> None:
    assert _inches(text) == inches


def test_a_fraction_no_drawing_prints_is_named_on_either_side() -> None:
    assert odd_fraction('78 [31/8"]') is not None
    assert plain_dimension('78 [31/8"]') is None
    assert odd_fraction('31/8" [78]') is not None


def test_the_rounding_allowance_is_the_dual_check_the_evidence_lane_uses() -> None:
    """One rule, not two: `units.policy.check_dual` (the evidence lane's) decides corroboration.

    Every invented mm [in] pair on a grid is read here exactly when `check_dual` calls its halves
    consistent, and the swapped in [mm] form gives the same answer.
    """
    for inch_text in ("1", "12", "24 1/2", "3/4", "17 5/16", "9 7/8", "40 3/8", "64"):
        whole, _, fraction = inch_text.partition(" ")
        inches = Fraction(whole) + (Fraction(fraction) if fraction else 0)
        centre = inches * Fraction(254, 10)
        for offset in range(-16, 17):
            mm = int(centre) + offset
            if mm <= 0:
                continue
            dual = DualDimension(
                Measurement(Fraction(mm), Unit.MM, str(mm)),
                Measurement(inches, Unit.INCH, inch_text),
            )
            consistent = check_dual(dual) is Consistency.CONSISTENT_WITHIN_ROUNDING
            expected = inches if consistent else None
            assert _inches(f"{mm} [{inch_text}]") == expected, (mm, inch_text)
            assert _inches(f"{inch_text} [{mm}]") == expected, (inch_text, mm)


# The seal: what the reader pair's agreed text becomes.


def test_an_agreed_in_mm_label_seals_as_its_inch_number() -> None:
    outcome = seal(GLYPH, (answer(KIMI, "25 [648]"), answer(QWEN, "25 [648]")))
    assert outcome.state is LabelState.SEALED
    assert outcome.value is not None and outcome.value.exact == Fraction(25)


def test_an_agreed_pair_that_does_not_corroborate_goes_to_the_person_for_every_pair() -> None:
    legacy = seal(GLYPH, (answer(KIMI, "305 [10]"), answer(QWEN, "305 [10]")))
    assert legacy.state is LabelState.REVIEW and legacy.value is None
    assert legacy.reason_code == "mm-inch-disagree"
    claude = seal(
        GLYPH, (answer(OPUS, "25 [600]"), answer(SONNET, "25 [600]")), allow_claude_pair=True
    )
    assert claude.state is LabelState.REVIEW and claude.reason_code == "mm-inch-disagree"
    assert claude.suggestion is None


def test_the_claude_pair_reads_an_in_mm_label_even_when_both_call_it_combined() -> None:
    for text in ("25 [648]", '25" [635]'):
        answers = (answer(OPUS, text, combined=True), answer(SONNET, text, combined=True))
        outcome = seal(GLYPH, answers, allow_claude_pair=True)
        assert outcome.state is LabelState.PROVISIONAL, text
        assert outcome.suggestion is not None and outcome.suggestion.exact == Fraction(25), text


def test_the_files_own_pair_that_does_not_corroborate_is_named_before_any_reader() -> None:
    outcome = seal(replace(TEXT, text="305 [10]"), ())
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "mm-inch-disagree"
