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

from extraction.slot_reader import labels
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
from units.measurement import Unit


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
        # GV's order, mm first: within 3 mm of 25.4 times the inches (Anant, 2026-10-15).
        ("1449 [57]", Fraction(57)),  # 1.2 mm
        ("1527 [60]", Fraction(60)),  # 3 mm exactly: inclusive
        # in [mm]: the inch is outside, so it is the value, where the mm is 25.4 times it to the
        # mm's own last digit (#1263: `25 [648]` was 648 inches).
        ("25 [635]", Fraction(25)),
        ('25" [635]', Fraction(25)),
        ("26 [660.4]", Fraction(26)),
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
        "1528 [60]",  # mm first, 4 mm off: more than 3 mm (Anant, 2026-10-15)
        "648 [25]",  # `648 [25 1/2]` with its stacked 1/2 lost: 13 mm off
        "38 [1]",  # `38 [1 1/2]` with its 1/2 lost: 12.6 mm off
        "25 [648]",  # inches first: 648 is not 25 x 25.4 = 635 to the mm
        "25 [636]",  # inches first: 1 mm off is more than the mm's own rounding
        "0 [0]",  # both readings match, and a 0" width is never a value
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


@pytest.mark.parametrize(
    "text",
    [
        # A GV label (mm first) whose mm was cut to its last digit must never read inches first.
        "2 [54]",  # `1372 [54]`
        "1 [28]",  # `711 [28]`
        "4 [97]",  # `2464 [97]`
        "0 [13]",  # `330 [13]`: a 0" width
    ],
)
def test_a_cut_off_mm_is_never_read_as_the_inches(text: str) -> None:
    assert _inches(text) is None
    assert _inches(text, allow_explicit_mm=True) is None
    assert mm_inch_refusal(text) is not None


def _inch_text(value: Fraction) -> str:
    whole, part = divmod(value, 1)
    if not part:
        return str(whole)
    return f"{whole} {part}" if whole else str(part)


def test_no_cut_off_or_lost_fraction_of_a_true_label_gives_a_wrong_value() -> None:
    """Every sixteenth of an inch up to 12 feet, printed as the vendor prints it (`mm [in]`, the
    mm to the nearest whole mm), then damaged as a reader damages it: the mm cut to any shorter run
    of its digits, or the inches' fraction of 3/16" or more lost. Each reads
    as the true inches or is held (#1263 review: `1372 [54]` cut to `2 [54]` sealed as 2").

    A damaged text that is itself another length's true label (`2 [51]`, 2" printed inches first)
    cannot be told from it by any rule, and is left out."""
    sweep = range(1, 144 * 16 + 1)
    true_labels = set()
    for sixteenths in sweep:
        inches = Fraction(sixteenths, 16)
        mm = str(round(inches * Fraction(254, 10)))
        true_labels |= {f"{mm} [{_inch_text(inches)}]", f"{_inch_text(inches)} [{mm}]"}
    for sixteenths in sweep:
        inches = Fraction(sixteenths, 16)
        mm = str(round(inches * Fraction(254, 10)))
        written = _inch_text(inches)
        damaged = {
            f"{mm[start:end]} [{written}]"
            for start in range(len(mm))
            for end in range(start + 1, len(mm) + 1)
            if mm[start:end] != mm
        }
        whole = inches.numerator // inches.denominator
        if inches - whole >= Fraction(3, 16):
            # A lost 1/16" or 1/8" is 1.6 or 3.2 mm: inside or at the edge of the 3 mm Anant
            # decided for GV's order, so it cannot be told from the vendor's rounding. 3/16" and up
            # (4.8 mm, less the mm's own half millimetre) is always outside it.
            damaged.add(f"{mm} [{whole}]")
        for text in damaged - true_labels:
            assert _inches(text) in (None, inches), (text, inches)


def test_the_mm_side_is_never_a_fraction() -> None:
    """`25 1/2 [1]`: 1 mm is not 25 1/2" either way, and reading `25 1/2` as millimetres could
    never be right; nor may `1` become the inches against a fraction of a millimetre."""
    assert _inches("25 1/2 [1]") is None
    assert _inches("1 [25 1/2]") is None
    assert _inches("3/4 [1]") is None


def test_both_readings_matching_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real pair of positive numbers matches both ways, so the guard is checked with a match
    rule that accepts everything: two readings, two different inch values, so none is chosen."""
    monkeypatch.setattr(labels, "_corroborates", lambda *_args, **_kwargs: True)
    assert _inches("25 [635]") is None
    assert mm_inch_refusal("25 [635]") is not None
    assert _inches("0 [0]") is None


def test_a_zero_inch_value_from_a_pair_is_never_accepted() -> None:
    """`0.2 [0]`: 0.2 mm is within 3 mm of 0", the only reading that matches, and still no value."""
    assert _inches("0.2 [0]") is None
    assert mm_inch_refusal("0.2 [0]") is not None
    assert _inches("0 [0]") is None
    assert mm_inch_refusal("0 [0]") is not None


def test_the_two_allowances_are_three_mm_for_gvs_order_and_the_mm_digit_for_inches_first() -> None:
    """mm first (GV's order): within 3 mm of 25.4 times the inches, inclusive (Anant, 2026-10-15).
    Inches first: within half the mm's last printed digit, the mm's own rounding only, so a GV
    label whose mm lost digits is never read the other way round. Exact fractions throughout."""
    for inch_text in ("1", "12", "24 1/2", "3/4", "17 5/16", "9 7/8", "40 3/8", "64"):
        whole, _, fraction = inch_text.partition(" ")
        inches = Fraction(whole) + (Fraction(fraction) if fraction else 0)
        centre = inches * Fraction(254, 10)
        for offset in range(-16, 17):
            mm = int(centre) + offset
            if mm <= 0:
                continue
            gap = abs(mm - centre)
            gv_order = inches if gap <= 3 else None
            inches_first = inches if gap <= Fraction(1, 2) else None
            assert _inches(f"{mm} [{inch_text}]") == gv_order, (mm, inch_text)
            assert _inches(f"{inch_text} [{mm}]") == inches_first, (inch_text, mm)


# The seal: what the reader pair's agreed text becomes.


def test_an_agreed_in_mm_label_seals_as_its_inch_number() -> None:
    outcome = seal(GLYPH, (answer(KIMI, "25 [635]"), answer(QWEN, "25 [635]")))
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
    for text in ("25 [635]", '25" [635]'):
        answers = (answer(OPUS, text, combined=True), answer(SONNET, text, combined=True))
        outcome = seal(GLYPH, answers, allow_claude_pair=True)
        assert outcome.state is LabelState.PROVISIONAL, text
        assert outcome.suggestion is not None and outcome.suggestion.exact == Fraction(25), text


def test_the_files_own_pair_that_does_not_corroborate_is_named_before_any_reader() -> None:
    outcome = seal(replace(TEXT, text="305 [10]"), ())
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "mm-inch-disagree"
