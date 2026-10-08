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


def test_markup_never_quotes_interleaved_characters() -> None:
    from vocabulary.reviewer_reasons import reviewer_reason

    assert (
        reviewer_reason(["reviewer-markup"], "covered by reviewer markup; reviewer wrote Ab-0xZ")
        == "Covered by reviewer markup. Check the vendor's own number."
    )
