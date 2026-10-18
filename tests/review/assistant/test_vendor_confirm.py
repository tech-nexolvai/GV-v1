"""The amber "Vendor to confirm" question (#1619), $0: one plain question per undecided item.

- every reason an item stays undecided gets its own specific question, citing the page and the
  values;
- the writing rule holds for all of them: never accuses, never says "AI" or GV's working words;
- an item with nothing to ask gets no question (it is listed as "Not applicable / no information").
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant.vendor_confirm import (
    BANNED,
    count_words,
    question_of,
    writes_well,
)


def _item(**changes: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 12,
        "vs_page": None,
        "item": "Counter overall width",
        "architect": "no width printed",
        "vendor": '63 3/4"',
        "status": "check",
        "source": "architect",
        "inches": {"architect": None, "vendor": "63.75"},
        "keep_reason": "architect_prints_nothing",
    }
    finding.update(changes)
    return finding


@pytest.mark.parametrize(
    ("finding", "question"),
    [
        (
            _item(),
            'The architect prints no size for the counter overall width. Confirm 63 3/4" on page '
            "12 fits the space on site.",
        ),
        (
            _item(
                decision_help={
                    "measures": "the counter run",
                    "estimate": {
                        "value_in": 58,
                        "plus_minus_in": 1,
                        "method": "measure_on_drawing at the architect's measured scale",
                    },
                    "likely": "can't tell",
                    "confirm": "the width on site",
                }
            ),
            'The architect prints no size for the counter overall width. Confirm 63 3/4" on page '
            "12 fits the space on site (the architect's drawing scales about 58\").",
        ),
        (
            _item(
                keep_reason="which_governs",
                architect='brand prototype 2\'-6" (30")',
                vendor='36"',
                inches={"architect": "30", "vendor": "36"},
                vs_page=13,
                item="Storage bay width",
            ),
            'The brand prototype shows 2\'-6" (30") on page 13; page 12 shows 36" for the '
            "storage bay width. Confirm with the designer which governs.",
        ),
        (
            _item(keep_reason="unreadable", vendor="partly cut off", inches={}),
            "Value illegible on page 12: reprint the counter overall width.",
        ),
        (
            _item(
                keep_reason="measures_something_else",
                architect="3'-2\"",
                vendor='38" including a 4" band',
                inches={"architect": "38", "vendor": "38"},
                item="Counter height",
            ),
            'What does the counter height measure? The architect prints 3\'-2" (38"); page 12 '
            'shows 38" including a 4" band. Confirm both run between the same points, and the '
            "finished size.",
        ),
        (
            _item(
                keep_reason="layout",
                item="Upper door layout",
                architect="4 lockers (two tiers) at left",
                vendor="uppers only; no lockers drawn",
            ),
            "The architect shows 4 lockers for the upper door layout; page 12 shows none. Confirm "
            "the layout.",
        ),
        (
            _item(
                keep_reason="vendor_disagrees",
                source="vendor_arithmetic",
                parts_sum="80.125",
                printed_total='80"',
                item="Base run",
            ),
            'On page 12 the parts of the base run add up to 80 1/8", and the overall prints 80". '
            "Confirm which size is intended.",
        ),
        (
            _item(keep_reason="needs_job_value", item="Sink centre line"),
            "Print the size of the sink centre line on page 12: the drawings do not give one to "
            "build or check it from.",
        ),
        # A difference code read but could not prove (no pairing proof): which size is intended.
        (
            _item(
                status="different",
                keep_reason=None,
                item="Outlet height",
                architect="3'-1\"",
                vendor='46"',
                inches={"architect": "37", "vendor": "46"},
                vs_page=13,
            ),
            "Page 12 shows 46\" for the outlet height; the architect's drawing on page 13 prints "
            '3\'-1" (37"). Confirm which size is intended.',
        ),
        # Millimetres with inches in brackets: the inches.
        (
            _item(
                status="different",
                keep_reason=None,
                item="Outlet to wall",
                architect="1'-2\"",
                vendor="407 [16]",
                inches={"architect": "14", "vendor": None},
            ),
            "Page 12 shows 16\" for the outlet to wall; the architect's drawing prints 1'-2\" "
            '(14"). Confirm which size is intended.',
        ),
    ],
)
def test_each_reason_gets_its_own_specific_question(finding: dict[str, Any], question: str) -> None:
    assert question_of(finding) == question
    assert writes_well(question)
    # Shown after "Confirm: ", so it never starts with "Confirm" itself.
    assert not question.startswith("Confirm")


def test_the_writing_rule_never_accuses_and_never_names_the_ai() -> None:
    for words in (
        "The vendor's value is wrong.",
        "An error on page 3.",
        "The AI read 46 here on page 3.",
        "Under GV's markup on page 3.",
        "Check before sending, page 3.",
    ):
        assert BANNED.search(words) and not writes_well(words), words
    assert writes_well("Confirm the sink model on page 3.")  # "model" is a millwork word
    assert not writes_well("Confirm the width.")  # it must cite a page
    # The finding's own words never bring a banned word into the question.
    noisy = _item(
        status="different",
        keep_reason=None,
        vendor="46\" (AI reading, partly under GV's yellow box)",
        architect="3'-1\" (wrong per the helper)",
        inches={"architect": "37", "vendor": "46"},
        item="Outlet height (error?)",
    )
    asked = question_of(noisy)
    assert asked is not None and writes_well(asked), asked
    assert "page 12" in asked.lower()
    # GV's own note on the vendor's sheet is never quoted as the vendor's value.
    trap = _item(
        status="different",
        keep_reason=None,
        item="Counter top tag",
        architect="CT-001",
        vendor="(ST-002-CUST) STONE TOP by GV",
        inches={},
        vs_page=13,
    )
    assert question_of(trap) == (
        "Print the counter top tag on page 12: the architect's drawing on page 13 gives CT-001."
    )


def test_the_finding_s_own_confirm_line_is_used_when_it_is_already_a_clean_question() -> None:
    helped = _item(
        decision_help={
            "measures": "x",
            "estimate": None,
            "likely": "can't tell",
            "confirm": 'Is the 4" band on page 12 a splash or the top build-up?',
        }
    )
    assert question_of(helped) == 'Is the 4" band on page 12 a splash or the top build-up?'
    # Written for the reviewer, or naming GV: the plain form instead.
    for confirm in ("Reviewer: confirm the width on page 12?", "Ask GV about page 12?"):
        other = _item(decision_help={**helped["decision_help"], "confirm": confirm})
        assert question_of(other) != confirm


def test_an_item_with_nothing_to_ask_gets_no_question() -> None:
    assert question_of(_item(source="completeness")) is None
    assert question_of(_item(vendor="", architect="")) is None


def test_counts_are_read_from_the_words_of_each_side() -> None:
    assert count_words("4 lockers (two tiers), three doors") == {"locker": 4, "door": 3}
    assert count_words("no lockers drawn") == {"locker": 0}
    assert count_words("2 adj. shelves each side") == {"shelf": 2}
