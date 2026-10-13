"""A fraction's spelling never decides agreement, and a fraction no drawing prints never seals (#1199).

Verification for `extraction/slot_reader/seal.py` (`normalise_text`, the "fraction-not-drawn" hold),
`extraction/slot_reader/labels.py` (`plain_dimension`) and `units/normalise.py`
(`fold_fractions`, `drawn_fraction_refusal`). Every value here is invented; no client value.

- `½` *is* `1/2`: Unicode defines it, so folding changes the spelling, never the value. Two readers
  that wrote `88½"` and `88 1/2"` read the same label.
- `163/8"` is no fraction a drawing prints (the top is not smaller than the bottom). Read as written
  it is 20 3/8"; the drawing almost certainly says something else. It goes to a person, even when
  both readers wrote it.
- LaTeX, a squeezed number and a fraction split over a line break are never repaired.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.geometry.rows import Box
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.labels import expand_label, plain_dimension
from extraction.slot_reader.runs import Lane, PlannedLabel
from extraction.slot_reader.seal import (
    LabelOutcome,
    LabelState,
    ReaderAnswer,
    normalise_text,
    seal_label,
)

KIMI = "us.moonshotai.kimi-k3"
QWEN = "qwen.qwen3-vl-235b-a22b"
OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
_BOX = Box(Decimal(10), Decimal(10), Decimal(20), Decimal(15))
GLYPH = PlannedLabel(
    box=_BOX,
    crop=_BOX,
    lane=Lane.GLYPHS,
    text=None,
    text_stacked=False,
    has_digit=True,
    touches_edge=False,
    ambiguous_slot=False,
    crowded=False,
    ticks_in_crop=True,
    path_boxes=(),
)


def _answer(model: str, text: str) -> ReaderAnswer:
    return ReaderAnswer(
        model_id=model, text=text, readable=True, no_dimension=False, stacked=False, combined=False
    )


def _two_makers(first: str, second: str) -> LabelOutcome:
    return seal_label(
        GLYPH,
        answers=(_answer(KIMI, first), _answer(QWEN, second)),
        ink=InkAt(InkClass.VENDOR, ""),
        stacked_by_bar=False,
        allow_stacked=False,
        row_ambiguity=None,
    )


def _claude_pair(first: str, second: str) -> LabelOutcome:
    """The reading lane in production: Opus and Sonnet on the same crop."""
    return seal_label(
        GLYPH,
        answers=(_answer(OPUS, first), _answer(SONNET, second)),
        ink=InkAt(InkClass.VENDOR, ""),
        stacked_by_bar=False,
        allow_stacked=False,
        row_ambiguity=None,
        allow_claude_pair=True,
    )


# --- The spelling of a fraction ---------------------------------------------------------------------


def test_a_fraction_character_and_its_spelled_form_agree() -> None:
    sealed = _two_makers('88½"', '88 1/2"')

    assert sealed.state is LabelState.SEALED
    assert sealed.value is not None and sealed.value.exact == Fraction(177, 2)
    assert sealed.sealed_text == '88 1/2"'
    # Each reader's own characters are still what the person is shown.
    assert dict(sealed.reader_texts) == {KIMI: '88½"', QWEN: '88 1/2"'}


def test_the_claude_pair_agrees_on_a_fraction_character_and_still_waits_for_the_witness() -> None:
    outcome = _claude_pair('31⅞"', '31 7/8"')

    assert outcome.state is LabelState.PROVISIONAL
    assert outcome.value is None
    assert outcome.suggestion is not None and outcome.suggestion.exact == Fraction(255, 8)


def test_two_identical_fraction_characters_are_plain_now() -> None:
    """Before #1199 the character gate refused `½` before the units code (which knows it) ran, so
    even two identical `41¾"` readings were held as "not plain"."""
    outcome = _two_makers('41¾"', '41¾"')

    assert outcome.state is LabelState.SEALED
    assert outcome.value is not None and outcome.value.exact == Fraction(167, 4)


@pytest.mark.parametrize(
    ("written", "folded"),
    [
        ("⅞", "7/8"),
        ('⅞"', '7/8"'),
        ('31⅞"', '31 7/8"'),
        ('88 ½"', '88 1/2"'),
        ('16 3⁄8"', '16 3/8"'),  # the fraction slash
        ("12¼”", '12 1/4"'),
        ("330 [13½]", "330 [13 1/2]"),
    ],
)
def test_fraction_characters_fold_to_their_own_digits(written: str, folded: str) -> None:
    assert normalise_text(written) == folded


def test_a_different_value_still_disagrees() -> None:
    differ = _two_makers('88½"', '88 1/4"')

    assert differ.state is LabelState.REVIEW and differ.reason_code == "readers-differ"
    assert differ.value is None


def test_a_digit_after_a_fraction_character_is_never_joined_into_its_bottom() -> None:
    """`½2` is not one over twenty-two, and `⅓2` is not one over thirty-two."""
    assert normalise_text('½2"') == '1/2 2"'
    assert normalise_text('⅓2"') == '1/3 2"'
    assert plain_dimension(normalise_text('⅓2"')) is None
    outcome = _two_makers('⅓2"', '⅓2"')
    assert outcome.state is LabelState.REVIEW and outcome.value is None


# --- A fraction no drawing prints -------------------------------------------------------------------


@pytest.mark.parametrize("seal", [_two_makers, _claude_pair])
def test_a_squeezed_fraction_both_readers_wrote_goes_to_the_person_with_the_reason(seal) -> None:
    outcome = seal('163/8"', '163/8"')

    assert outcome.state is LabelState.REVIEW
    assert outcome.reason_code == "fraction-not-drawn"
    assert outcome.reason is not None and "163/8" in outcome.reason
    assert "space" in outcome.reason
    # Never 20 3/8", neither as a value nor as a suggestion.
    assert outcome.value is None and outcome.suggestion is None


@pytest.mark.parametrize(
    "written",
    [
        '5/4"',  # improper
        '8/8"',  # top equal to bottom
        '3/5"',  # fifths are not drawn
        '16 3/5"',
        '16 5/4"',
        '1615/16"',  # 161 5/16 or 16 15/16: a person reads it
        '11 1/10"',
        '1/128"',  # finer than a sixty-fourth
        "330 [123/8]",  # the inch half of a dual label
    ],
)
def test_improper_or_odd_fractions_are_refused(written: str) -> None:
    assert plain_dimension(written, allow_explicit_mm=True) is None
    outcome = _claude_pair(written, written)
    assert outcome.state is LabelState.REVIEW
    assert outcome.reason_code == "fraction-not-drawn"
    assert outcome.value is None and outcome.suggestion is None


def test_a_squeezed_part_of_a_sum_is_never_expanded() -> None:
    assert expand_label('163/8"+1"') is None
    assert expand_label('16 3/8"+1"') is not None


def test_a_squeezed_fraction_in_the_files_own_text_is_held_with_the_reason() -> None:
    label = replace(GLYPH, lane=Lane.TEXT, text='163/8"')
    outcome = seal_label(
        label,
        answers=(),
        ink=InkAt(InkClass.VENDOR, ""),
        stacked_by_bar=False,
        allow_stacked=False,
        row_ambiguity=None,
    )
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "fraction-not-drawn"


def test_the_normal_spelling_still_seals() -> None:
    sealed = _two_makers('16 3/8"', '16 3/8"')
    assert sealed.state is LabelState.SEALED
    assert sealed.value is not None and sealed.value.exact == Fraction(131, 8)
    pair = _claude_pair('16 3/8"', '16 3/8"')
    assert pair.state is LabelState.PROVISIONAL
    assert pair.suggestion is not None and pair.suggestion.exact == Fraction(131, 8)


# --- Never repaired ---------------------------------------------------------------------------------


def test_latex_is_never_cleaned_up() -> None:
    """A recorded "do not": `\\frac` stays as written, so it neither agrees nor parses."""
    differ = _claude_pair(r'16\frac{3}{8}"', '16 3/8"')
    assert differ.state is LabelState.REVIEW and differ.reason_code == "readers-differ"

    both = _claude_pair(r'16\frac{3}{8}"', r'16\frac{3}{8}"')
    assert both.state is LabelState.REVIEW and both.reason_code == "not-plain"
    assert both.value is None and both.suggestion is None
    assert normalise_text(r'16\frac{3}{8}"') == r'16\frac{3}{8}"'


@pytest.mark.parametrize("broken", ['3"\n4', '3\n4"', '16 3\n8"'])
def test_a_fraction_split_over_a_line_break_is_never_joined(broken: str) -> None:
    differ = _claude_pair(broken, '3/4"')
    assert differ.state is LabelState.REVIEW and differ.reason_code == "readers-differ"

    both = _claude_pair(broken, broken)
    assert both.state is LabelState.REVIEW
    assert both.value is None and both.suggestion is None
    assert "/" not in normalise_text(broken)


# --- Regression: the forms that sealed before still seal --------------------------------------------


@pytest.mark.parametrize(
    ("written", "inches"),
    [
        ('14 3/8"', Fraction(115, 8)),
        ('2"', Fraction(2)),
        ('3/4"', Fraction(3, 4)),
        ('16-1/4"', Fraction(65, 4)),
        ("3'-6 1/2\"", Fraction(85, 2)),
        ("250 [9 7/8]", Fraction(79, 8)),
        ('76 [3"]', Fraction(3)),
        ('24 63/64"', Fraction(24 * 64 + 63, 64)),
        ('2.375"', Fraction(19, 8)),
    ],
)
def test_valid_forms_still_seal(written: str, inches: Fraction) -> None:
    value = plain_dimension(written, allow_explicit_mm=True)
    assert value is not None and value.exact == inches
    outcome = _two_makers(written, written)
    if outcome.state is LabelState.SEALED:
        assert outcome.value is not None and outcome.value.exact == inches
    else:
        # A dual label is a Claude-pair form only; it was "not plain" for other makers before too.
        assert "[" in written and outcome.reason_code == "not-plain"
    pair = _claude_pair(written, written)
    assert pair.state is LabelState.PROVISIONAL
    assert pair.suggestion is not None and pair.suggestion.exact == inches


def test_the_approved_worded_forms_still_expand() -> None:
    summed = _two_makers('4"+1" Filler', '4"+1" Filler')
    assert summed.state is LabelState.SEALED
    assert summed.value is not None and summed.value.exact == Fraction(5)
    shares = _two_makers('96"(6EQ)', '96"(6EQ)')
    assert shares.state is LabelState.SEALED
    assert shares.value is not None and shares.value.exact == Fraction(96)


# --- #1199 review fixes -----------------------------------------------------------------------------


def test_the_fraction_numerator_one_character_is_held_for_a_person() -> None:
    """`⅟` is "one over", written before a bottom: `8⅟16"` is never folded into a value."""
    assert normalise_text('8⅟16"') == '8 1/ 16"'
    outcome = _claude_pair('8⅟16"', '8⅟16"')
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "not-plain"
    assert outcome.value is None and outcome.suggestion is None


def test_a_hyphen_before_a_fraction_character_is_still_a_different_spelling() -> None:
    differ = _claude_pair('8-½"', '8 1/2"')
    assert differ.state is LabelState.REVIEW and differ.reason_code == "readers-differ"


@pytest.mark.parametrize("odd", ['１６ ３/８"', '16 3/8"́', '1̲6 3/8"'])
def test_full_width_digits_and_combining_marks_are_never_forgiven(odd: str) -> None:
    differ = _claude_pair(odd, '16 3/8"')
    assert differ.state is LabelState.REVIEW and differ.reason_code == "readers-differ"
    both = _claude_pair(odd, odd)
    assert both.state is LabelState.REVIEW and both.reason_code == "not-plain"
    assert both.value is None and both.suggestion is None


@pytest.mark.parametrize("text", ['457 / 18"', "24/36", '3/5 BASE"'])
def test_text_that_is_not_a_dimension_is_not_called_an_odd_fraction(text: str) -> None:
    outcome = _claude_pair(text, text)
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "not-plain"


def test_an_improper_fraction_alone_does_not_mention_a_missing_space() -> None:
    outcome = _claude_pair('5/4"', '5/4"')
    assert outcome.reason_code == "fraction-not-drawn"
    assert outcome.reason is not None and "space" not in outcome.reason
