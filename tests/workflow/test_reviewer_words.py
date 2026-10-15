"""Display wording cannot conceal the reason a row was withheld."""

import pytest


@pytest.mark.parametrize(
    "code,phrase",
    [
        ("counter-break", "tall appliance"),
        ("field-cut-included", "already includes"),
        ("vif", "site"),
        ("row-ambiguous", "which row"),
        ("row-partial", "incomplete"),
        ("row-choice-disagreement", "did not agree"),
        ("stone-short-of-ends", "panels"),
        ("stone-into-walls", "pockets"),
        ("reviewer-markup", "covered by reviewer markup"),
        ("incomplete-row", "incomplete"),
        ("wall-layout-needed", "wall layout"),
        ("not-asked", "no reader read this label"),
    ],
)
def test_reviewer_codes_have_plain_words(code: str, phrase: str) -> None:
    from vocabulary.reviewer_reasons import reviewer_reason

    assert phrase in reviewer_reason([code], None).lower()


def test_counter_break_outranks_only_one_reader() -> None:
    from vocabulary.reviewer_reasons import reviewer_reason

    assert "tall appliance" in reviewer_reason(
        ["row-hold:counter-break", "row-partial"], "only one reader"
    )


def test_a_held_rows_own_reason_outranks_not_asked() -> None:
    """#1114: a label the readers were never asked about shows the row's hold, not a reader gap."""
    from vocabulary.reviewer_reasons import reviewer_reason

    words = reviewer_reason(["row-hold:counter-break", "not-asked"], "the readers were not asked")
    assert words is not None and "tall appliance" in words
    assert "only one reader" not in words.lower()


def test_markup_never_quotes_interleaved_characters() -> None:
    from vocabulary.reviewer_reasons import reviewer_reason

    assert (
        reviewer_reason(["reviewer-markup"], "covered by reviewer markup; reviewer wrote Ab-0xZ")
        == "Covered by reviewer markup. Check the vendor's own number."
    )


def test_a_reader_unsure_about_a_label_is_told_in_plain_words() -> None:
    """#1110: an unsure reader holds the label for the reviewer; it is never "no label"."""
    from vocabulary.reviewer_reasons import reviewer_reason

    words = reviewer_reason(
        ["lane:glyphs", "unsure"], "a reader was not sure this label belongs to the marked span"
    )
    assert words == (
        "A reader was not sure this label belongs to the marked span; check it on the drawing."
    )
    assert "no dimension label" not in words.lower()


def test_a_gv_tick_is_named_plainly() -> None:
    """#1198: a number GV ticked is held; the words say GV ticked it and it is shown to you."""
    from vocabulary.reviewer_reasons import reviewer_reason

    words = reviewer_reason(["reviewer-markup", "gv-tick", "ink:gv"], "covered by reviewer markup")
    assert words is not None
    assert words.startswith("GV ticked this number (round 2), so it is shown to you.")
    assert "vendor's own number" in words


def test_a_row_with_a_tick_and_any_other_gv_mark_keeps_the_general_words() -> None:
    """A row's reason gathers every number's flags: one ticked number must not speak for another
    number GV boxed or corrected."""
    from vocabulary.reviewer_reasons import reviewer_reason

    words = reviewer_reason(["reviewer-markup", "gv-tick", "gv-other-mark"], None)
    assert words == "Covered by reviewer markup. Check the vendor's own number."


def test_a_tick_flag_alone_never_makes_a_hold() -> None:
    """The tick only words a hold that exists; it never creates or replaces one."""
    from vocabulary.reviewer_reasons import reviewer_reason

    assert reviewer_reason(["gv-tick"], None) is None
    assert reviewer_reason(["gv-tick", "row-hold:counter-break"], None) == reviewer_reason(
        ["row-hold:counter-break"], None
    )


def test_a_tick_beside_the_number_is_worded_as_beside() -> None:
    """#1335: a tick that touches neither the number's box nor a digit is said to be beside it,
    never "GV ticked this number"."""
    from vocabulary.reviewer_reasons import reviewer_reason

    words = reviewer_reason(["reviewer-markup", "gv-tick", "gv-tick-beside"], None)
    assert words is not None
    assert words.startswith("GV's tick is beside this number (round 2), so it is shown to you.")
    assert "vendor's own number" in words and "GV ticked" not in words


def test_a_row_with_one_number_ticked_and_one_tick_beside_says_beside() -> None:
    """A row gathers its numbers' flags: the weaker words win, so a tick on one number never
    claims another number whose tick is only beside it."""
    from vocabulary.reviewer_reasons import reviewer_reason

    words = reviewer_reason(["reviewer-markup", "gv-tick", "gv-tick", "gv-tick-beside"], None)
    assert words is not None and words.startswith("GV's tick is beside this number")


def test_beside_keeps_every_rule_of_the_tick_words() -> None:
    """Beside words a markup hold only, and only when GV's tick is GV's one mark in the row."""
    from vocabulary.reviewer_reasons import reviewer_reason

    general = "Covered by reviewer markup. Check the vendor's own number."
    assert reviewer_reason(["gv-tick", "gv-tick-beside"], None) is None
    assert (
        reviewer_reason(["reviewer-markup", "gv-tick", "gv-tick-beside", "gv-other-mark"], None)
        == general
    )
    assert reviewer_reason(["reviewer-markup", "gv-tick-beside"], None) == general
