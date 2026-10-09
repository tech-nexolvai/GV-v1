"""When a label's reading counts (#987). Verification for `extraction/slot_reader/seal.py`.

Every value here is invented; no client value.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.geometry.rows import Box
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.runs import Lane, PlannedLabel
from extraction.slot_reader.seal import (
    Belongs,
    LabelOutcome,
    LabelState,
    ReaderAnswer,
    normalise_text,
    owner_outcome,
    parse_belongs,
    plain_dimension,
    seal_label,
)

QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
VENDOR = InkAt(InkClass.VENDOR, "")
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
TEXT = replace(GLYPH, lane=Lane.TEXT, text='14 3/8"')


def answer(model: str, text: str, **flags: bool | Belongs) -> ReaderAnswer:
    values: dict[str, bool | Belongs] = {
        "readable": True,
        "no_dimension": False,
        "stacked": False,
        "combined": False,
    }
    values.update(flags)
    return ReaderAnswer(model_id=model, text=text, **values)


def seal(
    label: PlannedLabel = GLYPH,
    answers: tuple[ReaderAnswer, ...] = (answer(KIMI, '14 3/8"'), answer(QWEN, '14 3/8"')),
    *,
    ink: InkAt | None = VENDOR,
    stacked_by_bar: bool = False,
    allow_stacked: bool = False,
    row_ambiguity: str | None = None,
    allow_claude_pair: bool = False,
    held_before_reading: str | None = None,
) -> LabelOutcome:
    return seal_label(
        label,
        answers=answers,
        ink=ink,
        stacked_by_bar=stacked_by_bar,
        allow_stacked=allow_stacked,
        row_ambiguity=row_ambiguity,
        allow_claude_pair=allow_claude_pair,
        held_before_reading=held_before_reading,
    )


def test_two_makers_identical_text_seals_with_the_value_parsed_from_the_text() -> None:
    outcome = seal()

    assert outcome.state is LabelState.SEALED
    assert outcome.value is not None and outcome.value.exact == Fraction(115, 8)
    assert outcome.sealed_text == '14 3/8"'


def test_quote_glyphs_and_line_breaks_are_the_only_things_forgiven() -> None:
    sealed = seal(answers=(answer(KIMI, "250\n[9 7/8]"), answer(QWEN, "250 [9 7/8]")))
    assert sealed.state is LabelState.SEALED and sealed.value is not None
    assert sealed.value.exact == Fraction(79, 8)
    curly = seal(answers=(answer(KIMI, "14 3/8”"), answer(QWEN, '14 3/8"')))
    assert curly.state is LabelState.SEALED
    # A missing space is a different number, never forgiven.
    differ = seal(answers=(answer(KIMI, '14 3/8"'), answer(QWEN, '143/8"')))
    assert differ.state is LabelState.REVIEW and differ.reason_code == "readers-differ"


def test_text_lane_needs_the_files_text_and_the_reader_to_agree() -> None:
    sealed = seal(TEXT, (answer(QWEN, '14 3/8"'),))
    assert sealed.state is LabelState.SEALED
    assert dict(sealed.reader_texts) == {"pdf-text-layer": '14 3/8"', QWEN: '14 3/8"'}

    differ = seal(TEXT, (answer(QWEN, '14 5/8"'),))
    assert differ.state is LabelState.REVIEW and differ.reason_code == "readers-differ"
    assert differ.value is None
    missing = seal(TEXT, ())
    assert missing.state is LabelState.REVIEW and missing.reason_code == "one-reader-missing"


def test_same_maker_agreement_never_seals() -> None:
    with pytest.raises(ValueError, match="different makers"):
        seal(answers=(answer(KIMI, '2"'), answer("moonshotai.kimi-k3", '2"')))
    with pytest.raises(ValueError, match="different makers"):
        seal(answers=(answer("acme.unknown-a", '2"'), answer("acme.unknown-b", '2"')))


def test_approved_claude_pair_is_provisional_until_the_drawing_witness_runs() -> None:
    outcome = seal(
        answers=(answer(OPUS, '2"'), answer(SONNET, '2"')),
        allow_claude_pair=True,
    )

    assert outcome.state is LabelState.PROVISIONAL
    assert outcome.value is None
    assert outcome.suggestion is not None and outcome.suggestion.exact == Fraction(2)

    with pytest.raises(ValueError, match="different makers"):
        seal(answers=(answer(OPUS, '2"'), answer(SONNET, '2"')))


def test_claude_pair_parses_explicit_mm_inch_label_but_legacy_does_not() -> None:
    answers = (answer(OPUS, "305 mm [12]"), answer(SONNET, "305 mm [12]"))
    legacy = seal(answers=(answer(KIMI, "305 mm [12]"), answer(QWEN, "305 mm [12]")))
    assert legacy.state is LabelState.REVIEW and legacy.reason_code == "not-plain"

    claude = seal(answers=answers, allow_claude_pair=True)
    assert claude.state is LabelState.PROVISIONAL
    assert claude.suggestion is not None and claude.suggestion.exact == Fraction(12)


@pytest.mark.parametrize(
    ("text", "inches"),
    [("305 [12]", Fraction(12)), ("25 [1]", Fraction(1)), ("70 [2 3/4]", Fraction(11, 4))],
)
def test_claude_pair_parses_the_bare_mm_inch_form_even_when_both_call_it_combined(
    text: str, inches: Fraction
) -> None:
    """Proof run 2026-10-08: every bare mm [inch] label was held as "words or a sum"
    because both readers ticked "combined" (two numbers) and only the `mm`-spelt form was allowed.
    """
    answers = (answer(OPUS, text, combined=True), answer(SONNET, text, combined=True))
    outcome = seal(answers=answers, allow_claude_pair=True)
    assert outcome.state is LabelState.PROVISIONAL
    assert outcome.suggestion is not None and outcome.suggestion.exact == inches


def test_claude_pair_holds_a_dual_label_whose_mm_and_inch_halves_disagree() -> None:
    answers = (answer(OPUS, "305 [10]"), answer(SONNET, "305 [10]"))
    outcome = seal(answers=answers, allow_claude_pair=True)
    assert outcome.state is LabelState.REVIEW
    assert outcome.reason_code == "mm-inch-disagree"


def test_the_legacy_pair_still_holds_a_bare_mm_inch_label_called_combined() -> None:
    answers = (answer(KIMI, "305 [12]", combined=True), answer(QWEN, "305 [12]", combined=True))
    assert seal(answers=answers).state is LabelState.REVIEW


@pytest.mark.parametrize(
    ("text", "expected"), [('4"+1"', Fraction(5)), ('96"(6 EQ)', Fraction(96))]
)
def test_claude_pair_expands_only_the_approved_exact_label_forms(
    text: str, expected: Fraction
) -> None:
    outcome = seal(
        answers=(
            answer(OPUS, text, combined=True),
            answer(SONNET, text, combined=True),
        ),
        allow_claude_pair=True,
    )

    assert outcome.state is LabelState.PROVISIONAL
    assert outcome.suggestion is not None and outcome.suggestion.exact == expected
    assert any(flag.startswith("expanded:") for flag in outcome.flags)


def test_claude_pair_disagreement_stays_with_the_reviewer() -> None:
    outcome = seal(
        answers=(answer(OPUS, '2"'), answer(SONNET, '3"')),
        allow_claude_pair=True,
    )

    assert outcome.state is LabelState.REVIEW
    assert outcome.reason_code == "readers-differ"
    assert outcome.value is None


def test_claude_pair_requires_both_readers_to_claim_span_ownership() -> None:
    absent = seal(
        answers=(
            answer(OPUS, "", belongs=False, readable=False, no_dimension=True),
            answer(SONNET, "", belongs=False, readable=False, no_dimension=True),
        ),
        allow_claude_pair=True,
    )
    disputed = seal(
        answers=(answer(OPUS, "", belongs=False, readable=False), answer(SONNET, '2"')),
        allow_claude_pair=True,
    )

    assert absent.state is LabelState.NOT_A_DIMENSION
    assert disputed.state is LabelState.REVIEW
    assert disputed.value is None


def test_a_label_on_the_reviewers_ink_never_seals_and_carries_no_value() -> None:
    for ink in (InkAt(InkClass.GV, '15"'), InkAt(InkClass.COVERED, '15"')):
        outcome = seal(ink=ink)
        assert outcome.state is LabelState.REVIEW
        assert outcome.reason_code == "reviewer-markup"
        assert outcome.reason == 'covered by reviewer markup; reviewer wrote 15"'
        assert outcome.value is None and outcome.suggestion is None


def test_a_label_at_the_edge_never_seals() -> None:
    outcome = seal(replace(GLYPH, touches_edge=True))
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "edge"
    assert outcome.value is None and outcome.suggestion is None


def test_a_crowded_label_never_seals() -> None:
    outcome = seal(replace(GLYPH, crowded=True))
    assert outcome.state is LabelState.REVIEW and outcome.value is None


def test_unchecked_ink_holds_the_reading_back() -> None:
    outcome = seal(ink=None)
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "ink-unchecked"
    assert outcome.suggestion is not None, "the person still gets the reading to check"


def test_a_stacked_fraction_goes_to_the_person_unless_the_admin_allows_it() -> None:
    for outcome in (
        seal(stacked_by_bar=True),
        seal(replace(GLYPH, text_stacked=True)),
        seal(answers=(answer(KIMI, '14 3/8"', stacked=True), answer(QWEN, '14 3/8"'))),
    ):
        assert outcome.state is LabelState.REVIEW and outcome.reason_code == "stacked"
    allowed = seal(stacked_by_bar=True, allow_stacked=True)
    assert allowed.state is LabelState.SEALED and "stacked" in allowed.flags


def test_other_words_and_counts_never_seal_even_when_agreed() -> None:
    for text in ('4" Panel', '96" 6 EQ', "(6EQ)", '4+1"', '96"(1EQ)'):
        outcome = seal(answers=(answer(KIMI, text, combined=True), answer(QWEN, text)))
        assert outcome.state is LabelState.REVIEW and outcome.reason_code == "not-plain"
        assert outcome.sealed_text == text, "the agreed words stay as evidence of the kind"
        assert outcome.value is None


def test_an_agreed_sum_or_equal_shares_seals_as_one_exact_value() -> None:
    """#992: the two worded forms the admin approved, expanded from the agreed text alone."""
    for text, value, how in (
        ('4"+1" Filler', Fraction(5), "sum"),
        ('1 1/2"+3/4"', Fraction(9, 4), "sum"),
        ('96"(6EQ)', Fraction(96), "equal-shares"),
        ('96" (6 EQ)', Fraction(96), "equal-shares"),
    ):
        outcome = seal(
            answers=(answer(KIMI, text, combined=True), answer(QWEN, text, combined=True))
        )
        assert outcome.state is LabelState.SEALED, text
        assert outcome.value is not None and outcome.value.exact == value
        assert f"expanded:{how}" in outcome.flags


def test_a_sum_the_readers_print_differently_is_not_expanded() -> None:
    outcome = seal(answers=(answer(KIMI, '4"+1" Filler'), answer(QWEN, '4"+1"')))
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "readers-differ"


def test_a_field_cut_or_vif_label_never_seals_and_names_why() -> None:
    for text, code in (('30" (INCLUDING FIELD CUT)', "field-cut-included"), ("990 [39]VIF", "vif")):
        outcome = seal(answers=(answer(KIMI, text), answer(QWEN, text)))
        assert outcome.state is LabelState.REVIEW and outcome.reason_code == code
        assert outcome.value is None and outcome.suggestion is None


def test_a_plain_text_a_reader_calls_combined_still_waits() -> None:
    outcome = seal(answers=(answer(KIMI, '14 3/8"', combined=True), answer(QWEN, '14 3/8"')))
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "not-plain"


def test_a_bare_number_has_no_unit_and_never_seals() -> None:
    outcome = seal(answers=(answer(KIMI, "30"), answer(QWEN, "30")))
    assert outcome.state is LabelState.REVIEW and outcome.value is None


def test_a_reader_that_is_unsure_or_abstains_holds_the_reading() -> None:
    unsure = seal(answers=(answer(KIMI, '2"', readable=False), answer(QWEN, '2"')))
    assert unsure.state is LabelState.REVIEW and unsure.reason_code == "unreadable"
    alone = seal(answers=(answer(QWEN, '2"'),))
    assert alone.state is LabelState.REVIEW and alone.reason_code == "one-reader-missing"
    assert alone.reason == "only one reader"


def test_a_glyph_label_no_reader_answered_is_not_called_one_reader() -> None:
    """#1114: zero answers on a drawn label is its own case, never "only one reader"."""
    outcome = seal(answers=())
    assert outcome.state is LabelState.REVIEW and outcome.value is None
    assert outcome.reason_code == "not-asked"
    assert outcome.reason is not None and "only one reader" not in outcome.reason
    assert "not-asked" in outcome.flags


@pytest.mark.parametrize("label", [GLYPH, TEXT], ids=["glyph", "text"])
def test_a_label_on_a_page_held_before_reading_carries_the_pages_hold(
    label: PlannedLabel,
) -> None:
    """#1114: the page waited before any reader was asked; its labels say so, still held."""
    outcome = seal(label, (), held_before_reading="a made-up bay sits in this row")
    assert outcome.state is LabelState.REVIEW and outcome.value is None
    assert outcome.reason_code == "not-asked"
    assert outcome.reason == (
        "the readers were not asked; this row waits: a made-up bay sits in this row"
    )


def test_a_text_label_whose_one_reader_did_not_answer_still_says_only_one_reader() -> None:
    """The file's text is one source; a reader asked but silent leaves one, as before."""
    outcome = seal(TEXT, ())
    assert outcome.reason_code == "one-reader-missing" and outcome.reason == "only one reader"


def test_both_readers_saying_no_dimension_is_not_a_dimension() -> None:
    outcome = seal(
        answers=(answer(KIMI, "", no_dimension=True), answer(QWEN, "", no_dimension=True))
    )
    assert outcome.state is LabelState.NOT_A_DIMENSION
    words = seal(replace(TEXT, text="DRAWER", has_digit=False), ())
    assert words.state is LabelState.NOT_A_DIMENSION


def test_the_row_being_uncertain_holds_every_reading() -> None:
    outcome = seal(row_ambiguity="another row on the page fits as well")
    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "row-ambiguous"


def test_plain_dimension_reads_only_the_text() -> None:
    assert plain_dimension('2"') is not None
    assert plain_dimension("250 [9 7/8]") is not None
    assert plain_dimension("[9 7/8]") is None
    assert plain_dimension('96"(6EQ)') is None
    assert normalise_text("1″ \n x") == '1" x'


def _outcome(state: LabelState, value: str | None = None) -> LabelOutcome:
    measured = plain_dimension(value) if value else None
    return LabelOutcome(state, measured, None, value, None, None, InkClass.VENDOR, (), ())


def test_one_slot_takes_one_reading_never_a_choice_between_two() -> None:
    word = _outcome(LabelState.NOT_A_DIMENSION)
    single = owner_outcome([word, _outcome(LabelState.SEALED, '3"')])
    assert single.state is LabelState.SEALED and single.label_index == 1

    two = owner_outcome([_outcome(LabelState.SEALED, '3"'), _outcome(LabelState.REVIEW)])
    assert two.state is LabelState.REVIEW and two.value is None and two.reason_code == "two-labels"

    none = owner_outcome([word])
    assert none.state is LabelState.REVIEW and none.reason_code == "no-label"
    assert owner_outcome([]).reason_code == "no-label"


# --- #1110: unsure is never evidence of absence ---------------------------------------------------


def test_two_readers_unsure_about_a_label_send_it_to_the_reviewer_never_no_label() -> None:
    """v3 told a reader to answer "does not belong" when merely unsure, and two such answers
    counted as "no dimension here", which is what starts the equal-shares read-through."""
    outcome = seal(
        answers=(
            answer(OPUS, "", belongs=Belongs.UNSURE, readable=False),
            answer(SONNET, "", belongs=Belongs.UNSURE, readable=False),
        ),
        allow_claude_pair=True,
    )

    assert outcome.state is LabelState.REVIEW
    assert outcome.reason_code == "unsure"
    assert outcome.reason == "a reader was not sure this label belongs to the marked span"
    assert outcome.value is None
    piece = owner_outcome([outcome])
    assert piece.state is LabelState.REVIEW and piece.reason_code == "unsure"
    assert piece.reason_code != "no-label"


@pytest.mark.parametrize("other_no_dimension", [True, False])
def test_one_unsure_reader_holds_the_label_whatever_the_other_says(
    other_no_dimension: bool,
) -> None:
    other = (
        answer(SONNET, "", belongs=Belongs.NO, readable=False, no_dimension=True)
        if other_no_dimension
        else answer(SONNET, '2"')
    )
    outcome = seal(
        answers=(answer(OPUS, "", belongs=Belongs.UNSURE, readable=False), other),
        allow_claude_pair=True,
    )

    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "unsure"
    assert outcome.value is None


def test_unsure_holds_even_when_the_unsure_reader_also_ticked_no_dimension() -> None:
    outcome = seal(
        answers=(
            answer(OPUS, "", belongs=Belongs.UNSURE, readable=False, no_dimension=True),
            answer(SONNET, "", belongs=Belongs.NO, readable=False, no_dimension=True),
        ),
        allow_claude_pair=True,
    )

    assert outcome.state is LabelState.REVIEW and outcome.reason_code == "unsure"


def test_two_explicit_no_dimension_answers_are_still_not_a_dimension() -> None:
    by_flag = seal(
        answers=(
            answer(OPUS, "", belongs=Belongs.NO, readable=False, no_dimension=True),
            answer(SONNET, "", belongs=Belongs.NO, readable=False, no_dimension=True),
        ),
        allow_claude_pair=True,
    )
    by_no = seal(
        answers=(
            answer(OPUS, "", belongs=Belongs.NO, readable=False),
            answer(SONNET, "", belongs=Belongs.NO, readable=False),
        ),
        allow_claude_pair=True,
    )

    assert by_flag.state is LabelState.NOT_A_DIMENSION
    assert by_no.state is LabelState.NOT_A_DIMENSION
    assert owner_outcome([by_flag]).reason_code == "no-label"
    assert owner_outcome([by_no]).reason_code == "no-label"


def test_a_no_that_still_copies_a_text_is_not_an_explicit_no_dimension() -> None:
    outcome = seal(
        answers=(
            answer(OPUS, '2"', belongs=Belongs.NO),
            answer(SONNET, "", belongs=Belongs.NO, readable=False),
        ),
        allow_claude_pair=True,
    )

    assert outcome.state is LabelState.REVIEW
    assert outcome.reason_code != "no-dimension"


def test_v3_boolean_answers_read_as_they_always_did() -> None:
    """Stored v1 to v3 answers: true is yes, false is no, and two falses with empty text are still
    "no dimension" as before."""
    assert parse_belongs(True) is Belongs.YES
    assert parse_belongs(False) is Belongs.NO
    assert parse_belongs("unsure") is Belongs.UNSURE
    for bad in ("true", 1, None, "maybe"):
        with pytest.raises(ValueError):
            parse_belongs(bad)
    old_false = ReaderAnswer(OPUS, "", False, False, False, False, False)  # type: ignore[arg-type]
    old_true = ReaderAnswer(OPUS, '2"', True, False, False, False, True)  # type: ignore[arg-type]
    assert old_false.belongs is Belongs.NO and old_true.belongs is Belongs.YES
    assert old_true.usable and not old_false.usable

    both_false = seal(
        answers=(
            ReaderAnswer(OPUS, "", False, False, False, False, False),  # type: ignore[arg-type]
            ReaderAnswer(SONNET, "", False, False, False, False, False),  # type: ignore[arg-type]
        ),
        allow_claude_pair=True,
    )
    assert both_false.state is LabelState.NOT_A_DIMENSION


def test_only_a_stacked_fraction_code_confirms_is_flagged_stacked_by_code() -> None:
    """#1107. Input: a label code finds stacked (fraction bar, or the file's text), and one a reader
    alone calls stacked. Outcome: both keep `stacked` for the record; only code's gets
    `stacked-by-code`, the one flag that takes a piece out of its row's drawn-length scale."""
    from extraction.slot_reader.seal import STACKED_BY_CODE

    for by_code in (
        seal(stacked_by_bar=True, allow_stacked=True),
        seal(replace(GLYPH, text_stacked=True), allow_stacked=True),
    ):
        assert by_code.state is LabelState.SEALED
        assert "stacked" in by_code.flags and STACKED_BY_CODE in by_code.flags
    reader_only = seal(
        answers=(answer(KIMI, '14 3/8"', stacked=True), answer(QWEN, '14 3/8"')),
        allow_stacked=True,
    )
    assert reader_only.state is LabelState.SEALED
    assert "stacked" in reader_only.flags
    assert STACKED_BY_CODE not in reader_only.flags
    plain = seal()
    assert "stacked" not in plain.flags and STACKED_BY_CODE not in plain.flags
