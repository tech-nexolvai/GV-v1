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
    NOTHING_ON_THAT_PAGE,
    NOTHING_TO_DECIDE_THERE,
    YOUR_DECISION,
    answer_for_question,
    blockers_answer,
    fallback_answer,
    judging_answer,
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


def _evidence(draft: Draft) -> list[str]:
    from app.review.assistant.answers import publish

    answer = publish(draft, SNAPSHOT, mode="records_only", question="q", checked=True)
    return [item.kind for item in answer.evidence]


def test_why_did_page_4_fail_is_one_sentence_and_the_card_carries_the_values() -> None:
    draft = answer_for_question(SNAPSHOT, "Why did page 4 fail?")
    assert draft is not None
    assert _text(draft) == (
        "The countertop on page 4 needs correction; the printed overall does not match the pieces "
        "below it plus the field cut [[0]]."
    )
    assert _evidence(draft) == ["countertop"]
    assert ("open_queue_item", "C1") in draft.actions


def test_what_is_left_is_the_sign_off_status_and_the_evidence_lists_the_items() -> None:
    draft = blockers_answer(SNAPSHOT)
    assert _text(draft) == "Sign-off is blocked: 3 findings still need your decision."
    assert _evidence(draft) == ["blockers"]
    assert draft.actions == (("open_queue_item", "C1"),)


def test_nothing_has_run_says_so_rather_than_all_clear() -> None:
    assert "No checks have run" in _text(blockers_answer(empty_snapshot()), empty_snapshot())


def test_pages_without_a_countertop_are_a_count_and_the_evidence_lists_them() -> None:
    draft = answer_for_question(SNAPSHOT, "Which pages have no countertop?")
    assert draft is not None
    assert _text(draft) == "1 page has no countertop."
    assert _evidence(draft) == ["no_countertop_pages"]
    assert NO_COUNTERTOP_REASON.rstrip(".") not in _text(draft)


def test_rows_not_checked_are_a_count_and_the_evidence_lists_them() -> None:
    draft = answer_for_question(SNAPSHOT, "Which countertops were not checked?")
    assert draft is not None
    assert _text(draft) == "1 second countertop was not checked."
    assert _evidence(draft) == ["rows_not_checked"]
    assert SECOND_ROW_REASON.rstrip(".") not in _text(draft)


def test_why_a_page_needs_me_is_the_outcome_and_the_hold() -> None:
    draft = answer_for_question(SNAPSHOT, "Why does page 7 need me?")
    assert draft is not None
    text = _text(draft)
    assert text == (
        "The countertop on page 7 needs your decision; it is held because the two AIs picked "
        "different countertop lines on this page, so nothing was read [[0]]."
    )
    assert "architect" not in text.casefold()
    assert "review queue" not in text


def test_the_architect_line_only_when_asked() -> None:
    draft = answer_for_question(SNAPSHOT, "What about the architect check on page 7?")
    assert draft is not None
    assert "Not compared".casefold() in _text(draft).casefold()


def test_a_generic_label_is_left_out_and_a_distinguishing_one_kept() -> None:
    from uuid import uuid4

    base = countertop_results()
    second = base.items[0].model_copy(
        update={"row_id": uuid4(), "finding_id": uuid4(), "page_number": 9, "label": "Run E"}
    )
    review = snapshot(Inputs(countertops=base.model_copy(update={"items": (*base.items, second)})))
    draft = judging_answer(review, 9)
    text = _text(draft, review)
    assert "(Sample run C)" in text and "(Run E)" in text
    generic = base.items[0].model_copy(update={"label": "Countertop row on page 4"})
    alone = snapshot(
        Inputs(countertops=base.model_copy(update={"items": (generic, *base.items[1:])}))
    )
    assert "Countertop row on page" not in _text(judging_answer(alone, 4), alone)


def test_a_page_with_no_records_says_so() -> None:
    assert answer_for_question(SNAPSHOT, "What about page 12?") == Draft(text=NOTHING_ON_THAT_PAGE)


def test_a_question_code_cannot_answer_returns_none() -> None:
    assert answer_for_question(SNAPSHOT, "Who is the vendor's project manager?") is None


def test_the_focus_record_answers_a_vague_question() -> None:
    draft = answer_for_question(SNAPSHOT, "Tell me about this one", Focus(record_id=str(ROW_PASS)))
    assert draft is not None
    assert "carried over from the earlier run" in _text(draft)


def test_the_fallback_is_what_needs_you_with_one_line_saying_so() -> None:
    draft = fallback_answer(SNAPSHOT, "Who drew it?", cited=("C9", "C2", "P4"))
    check(draft, SNAPSHOT, by_model=False)
    text = render(draft.text, SNAPSHOT).text
    assert text.startswith("I couldn't check a free answer to that; here is what needs you.")
    assert render(draft.text, SNAPSHOT).citations == ("C1", "C2", "F1")
    assert draft.evidence[0] == "blockers"


def test_the_architect_outcome_is_its_own_placeholder() -> None:
    base = countertop_results()
    page_4 = base.items[0].model_copy(
        update={"architect": ArchitectResultOut(outcome=Outcome.PASS, reason="Matches.")}
    )
    review = snapshot(
        Inputs(countertops=base.model_copy(update={"items": (page_4, *base.items[1:])}))
    )
    draft = answer_for_question(review, "What does the architect check say on page 4?")
    assert draft is not None
    assert "the architect check looks right (Matches) [[0]]" in _text(draft, review)


# ---- requests to judge -------------------------------------------------------------------------


def test_judging_a_page_states_its_outcome_and_whose_decision_it_is() -> None:
    draft = judging_answer(SNAPSHOT, 4)
    text = _text(draft)
    assert text == f"The countertop on page 4 needs correction [[0]]. {YOUR_DECISION}"
    assert draft.actions == (("open_queue_item", "C1"),)


def test_judging_a_page_with_a_second_countertop_mentions_it() -> None:
    draft = judging_answer(SNAPSHOT, 9)
    text = _text(draft)
    assert "looks right; you confirmed it (carried over from the earlier run) [[0]]" in text
    assert "also has a second countertop that was not checked" in text
    assert SECOND_ROW_REASON.rstrip(".") not in text  # the evidence carries the readers' words
    assert "rows_not_checked" in draft.evidence
    # Page 9 needs nothing: no queue button to an unrelated record, only the page.
    assert draft.actions == (("open_page", "P9"),)


def test_judging_a_page_with_no_countertop_says_so() -> None:
    draft = judging_answer(SNAPSHOT, 3)
    text = _text(draft)
    assert text.startswith("Page 3 [[0]] has no countertop.")
    assert text.endswith(NOTHING_TO_DECIDE_THERE)
    assert draft.evidence == ("no_countertop_pages",)
    assert draft.actions == (("open_page", "P3"),)


def test_judging_a_page_the_records_do_not_have_points_nowhere() -> None:
    draft = judging_answer(SNAPSHOT, 12)
    assert draft == Draft(text=NOTHING_ON_THAT_PAGE)


def test_judging_with_no_page_is_the_sign_off_status_and_whose_decision_it_is() -> None:
    draft = judging_answer(SNAPSHOT, None)
    assert (
        _text(draft) == f"Sign-off is blocked: 3 findings still need your decision. {YOUR_DECISION}"
    )
    assert draft.evidence == ("blockers",)
