"""Records-only answers (#1128): code-written templates, filled and cited like a model's.

They answer the starter questions when the assistant is off, replace a dropped model answer and
answer requests to judge, so every one must pass the guard (as code: placeholders, one record per
sentence, no digit outside a placeholder) and render with its facts cited.
"""

from __future__ import annotations

import pytest

from app.review.assistant.contract import Draft, Focus
from app.review.assistant.guard import check
from app.review.assistant.placeholders import render
from app.review.assistant.records import ReviewSnapshot
from app.review.assistant.records_only import (
    NO_ANSWER_IN_RECORDS,
    NOTHING_ON_THAT_PAGE,
    NOTHING_TO_DECIDE_THERE,
    YOUR_DECISION,
    answer_for_question,
    blockers_answer,
    fallback_answer,
    judging_answer,
    records_answer,
)
from app.review.assistant.starters import starters
from app.schemas.visual_ui import ArchitectResultOut
from tests.review.assistant.synthetic import (
    NO_COUNTERTOP_REASON,
    ROW_PASS,
    SECOND_ROW_REASON,
    Inputs,
    countertop_results,
    empty_snapshot,
    snapshot,
)
from verdict.outcomes import Outcome

SNAPSHOT = snapshot()
QUESTIONS = (
    "Why did page 4 fail?",
    "What is left before sign-off?",
    "Which pages have no countertop?",
    "Why does page 7 need me?",
    "Which countertops were not checked?",
    "What about page 5?",
    "Explain sheet 9",
    "Can I sign off?",
    "Anything still blocking?",
    "page 12?",
)


def _text(draft: Draft, review: ReviewSnapshot = SNAPSHOT) -> str:
    check(draft, review, by_model=False)
    return render(draft.text, review).text


@pytest.mark.parametrize("review", [snapshot(), empty_snapshot()], ids=["full", "nothing-run"])
@pytest.mark.parametrize("question", QUESTIONS)
def test_every_records_only_answer_passes_the_guard(review: ReviewSnapshot, question: str) -> None:
    draft = answer_for_question(review, question)
    assert draft is not None, question
    check(draft, review, by_model=False)
    render(draft.text, review)


@pytest.mark.parametrize("review", [snapshot(), empty_snapshot()], ids=["full", "nothing-run"])
def test_every_starter_has_a_records_only_answer(review: ReviewSnapshot) -> None:
    for question in starters(review):
        draft = answer_for_question(review, question)
        assert draft is not None, question
        check(draft, review, by_model=False)


def test_why_did_page_4_fail_is_its_countertops_facts_cited() -> None:
    draft = answer_for_question(SNAPSHOT, "Why did page 4 fail?")
    assert draft is not None
    text = _text(draft)
    assert text.startswith("The countertop on page 4 (Sample run A) needs correction [[0]]:")
    assert '- The printed overall, 84 1/2" [[0]]\n- The needed overall, 85" [[0]]' in text
    assert render(draft.text, SNAPSHOT).citations == ("C1",)
    assert ("open_queue_item", "C1") in draft.actions


def test_what_is_left_lists_every_record_that_needs_the_reviewer() -> None:
    draft = blockers_answer(SNAPSHOT)
    rendered = render(draft.text, SNAPSHOT)
    assert rendered.citations == ("C1", "C2", "F1")
    assert rendered.text.startswith("Sign-off is blocked: 3 findings still need your decision.")
    assert "The Sink centre line check on page 5 is waiting on a value [[2]]" in rendered.text
    assert "blockers" in rendered.groups


def test_nothing_has_run_says_so_rather_than_all_clear() -> None:
    assert "No checks have run" in _text(blockers_answer(empty_snapshot()), empty_snapshot())


def test_pages_without_a_countertop_are_listed_with_their_reason() -> None:
    draft = answer_for_question(SNAPSHOT, "Which pages have no countertop?")
    assert draft is not None
    text = _text(draft)
    assert text.startswith("1 page has no countertop:")
    assert NO_COUNTERTOP_REASON.rstrip(".") in text


def test_rows_not_checked_are_listed() -> None:
    draft = answer_for_question(SNAPSHOT, "Which countertops were not checked?")
    assert draft is not None
    assert SECOND_ROW_REASON.rstrip(".") in _text(draft)


def test_why_a_page_needs_me_quotes_the_hold() -> None:
    draft = answer_for_question(SNAPSHOT, "Why does page 7 need me?")
    assert draft is not None
    assert "It is held because the two AIs picked different countertop lines" in _text(draft)


def test_a_page_with_no_records_says_so() -> None:
    assert answer_for_question(SNAPSHOT, "What about page 12?") == Draft(text=NOTHING_ON_THAT_PAGE)


def test_a_question_code_cannot_answer_returns_none() -> None:
    assert answer_for_question(SNAPSHOT, "Who is the vendor's project manager?") is None


def test_the_focus_record_answers_a_vague_question() -> None:
    draft = answer_for_question(SNAPSHOT, "Tell me about this one", Focus(record_id=str(ROW_PASS)))
    assert draft is not None
    assert "carried over from the earlier run" in _text(draft)


def test_the_fallback_prefers_the_records_the_model_named() -> None:
    draft = fallback_answer(SNAPSHOT, "anything", cited=("C9", "C2", "P4"))
    assert render(draft.text, SNAPSHOT).citations == ("C2",)
    check(draft, SNAPSHOT, by_model=False)


def test_the_fallback_says_plainly_when_nothing_can_be_checked() -> None:
    assert fallback_answer(SNAPSHOT, "Who drew it?").text == NO_ANSWER_IN_RECORDS


def test_the_architect_outcome_is_its_own_placeholder() -> None:
    base = countertop_results()
    page_4 = base.items[0].model_copy(
        update={"architect": ArchitectResultOut(outcome=Outcome.PASS, reason="Matches.")}
    )
    review = snapshot(
        Inputs(countertops=base.model_copy(update={"items": (page_4, *base.items[1:])}))
    )
    draft = records_answer(review, ["C1"])
    assert draft is not None
    assert "- The architect check looks right (Matches) [[0]]" in _text(draft, review)


# ---- requests to judge -------------------------------------------------------------------------


def test_judging_a_page_states_its_outcome_and_whose_decision_it_is() -> None:
    draft = judging_answer(SNAPSHOT, 4)
    text = _text(draft)
    assert text.startswith("The countertop on page 4 (Sample run A) needs correction [[0]].")
    assert text.endswith(YOUR_DECISION)
    assert draft.actions == (("open_queue_item", "C1"),)


def test_judging_a_page_with_a_second_countertop_mentions_it() -> None:
    draft = judging_answer(SNAPSHOT, 9)
    text = _text(draft)
    assert "looks right; you confirmed it (carried over from the earlier run) [[0]]" in text
    assert SECOND_ROW_REASON.rstrip(".") in text
    # Page 9 needs nothing: no queue button to an unrelated record, only the page.
    assert draft.actions == (("open_page", "P9"),)


def test_judging_a_page_with_no_countertop_says_so() -> None:
    draft = judging_answer(SNAPSHOT, 3)
    text = _text(draft)
    assert NO_COUNTERTOP_REASON.rstrip(".") in text
    assert text.endswith(NOTHING_TO_DECIDE_THERE)
    assert draft.actions == (("open_page", "P3"),)


def test_judging_a_page_the_records_do_not_have_points_nowhere() -> None:
    draft = judging_answer(SNAPSHOT, 12)
    assert draft == Draft(text=NOTHING_ON_THAT_PAGE)


def test_judging_with_no_page_lists_what_needs_the_reviewer() -> None:
    draft = judging_answer(SNAPSHOT, None)
    text = _text(draft)
    assert "These are still open in the queue:" in text
    assert render(draft.text, SNAPSHOT).citations == ("C1", "C2", "F1")
    assert text.endswith(YOUR_DECISION)
