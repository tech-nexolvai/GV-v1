"""The assistant knows the simple review list and can point to one item (#1352, W6).

Synthetic records only, no model call. What matters: the list (header counts, items with kind,
page, why and best guess, failures, not-checked) reaches the records and the prompt; an item is
named only through placeholders code fills; the `open_review_item` button goes only to an item
that still waits for the reviewer; and the guard still lets no model words decide, confirm or
state a value. GV's own words ("reviewer wrote …") never reach the records.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.review.assistant.answers import publish
from app.review.assistant.contract import (
    AnswerEvent,
    AssistantRequest,
    Draft,
    OpenReviewItemAction,
)
from app.review.assistant.guard import GuardRejected, check
from app.review.assistant.intent import is_decision_request, is_judging_question
from app.review.assistant.model import ANSWER_SCHEMA, SYSTEM_PROMPT, parse_answer
from app.review.assistant.placeholders import Slot, UnknownPlaceholder, record_fields, value_of
from app.review.assistant.records import ReviewItemRecord, prompt_records
from app.review.assistant.service import AssistantRuntime, stream_answer
from tests.review.assistant.synthetic import (
    FAILURE_KEY,
    ITEM_BY_HAND_KEY,
    ITEM_DONE_KEY,
    ITEM_GUESS_KEY,
    NOT_CHECKED_KEY,
    snapshot,
    snapshot_with_review_list,
)

REVIEW = snapshot_with_review_list()


def _item(key: str) -> ReviewItemRecord:
    return next(item for item in REVIEW.review_items if item.record_id == key)


def _fill(text: str) -> str:
    key, _, field_name = text.strip("{}").partition(".")
    return value_of(REVIEW, Slot(key, field_name, None, 0, 0))


# --- records --------------------------------------------------------------------------------------


def test_the_review_list_reaches_the_records_open_first() -> None:
    rows = [
        (item.id, item.group, item.kind, item.page_number, item.status, item.needs_you)
        for item in REVIEW.review_items
    ]
    assert rows == [
        ("R1", "item", "number", 4, "open", True),
        ("R2", "item", "number", 7, "open", True),
        ("R3", "item", "other_result", 9, "done", False),
        ("R4", "failure", "failure", 4, "open", True),
        ("R5", "not_checked", "not_checked", 5, "open", True),
    ]
    assert [item.record_id for item in REVIEW.review_items] == [
        ITEM_GUESS_KEY,
        ITEM_BY_HAND_KEY,
        ITEM_DONE_KEY,
        FAILURE_KEY,
        NOT_CHECKED_KEY,
    ]
    assert REVIEW.review_item("R2") is REVIEW.review_items[1]
    assert REVIEW.review_item_by_key(FAILURE_KEY) is REVIEW.review_items[3]


def test_the_header_counts_come_through_as_the_list_says() -> None:
    header = REVIEW.review_header
    assert header is not None
    assert (header.open, header.failures, header.not_checked, header.checked_by_you) == (3, 1, 1, 1)
    assert (header.numbers, header.confirmed_by_ai, header.can_finalise) == (12, 9, False)


def test_the_best_guess_is_kept_with_its_source_and_never_a_value() -> None:
    guessed, by_hand = _item(ITEM_GUESS_KEY), _item(ITEM_BY_HAND_KEY)
    assert guessed.best_guess is not None
    assert (guessed.best_guess.display, guessed.best_guess.exact) == ("12 1/2", "25/2")
    assert guessed.best_guess.source == "agreed_text"
    assert by_hand.best_guess is None


def test_gv_written_words_never_reach_the_records_or_the_prompt() -> None:
    review = snapshot_with_review_list(gv_words_in_why=True)
    item = next(item for item in review.review_items if item.record_id == ITEM_GUESS_KEY)
    assert "reviewer wrote" not in item.why and "31" not in item.why
    assert "reviewer wrote" not in prompt_records(review)


def test_the_prompt_carries_the_list_with_short_ids_and_no_keys() -> None:
    document = json.loads(prompt_records(REVIEW))
    listed = document["review_list"]
    assert listed["header"]["open"] == 3
    assert listed["header"]["not_checked"] == 1
    assert [item["id"] for item in listed["items"]] == ["R1", "R2", "R3"]
    assert [item["id"] for item in listed["failures"]] == ["R4"]
    assert [item["id"] for item in listed["not_checked"]] == ["R5"]
    first = listed["items"][0]
    assert first["page"] == 4 and first["what"] == "Width of piece 2"
    assert first["best_guess_to_confirm"] == "12 1/2"
    assert first["needs_reviewer"] is True
    assert "best_guess_to_confirm" not in listed["items"][1]
    text = prompt_records(REVIEW)
    assert ITEM_GUESS_KEY not in text and "Sample Reviewer" not in text


def test_without_a_review_list_nothing_changes() -> None:
    plain = snapshot()
    assert plain.review_items == () and plain.review_header is None
    assert "review_list" not in json.loads(prompt_records(plain))
    assert not any(key.startswith("R") for key in record_fields(plain))
    assert "review_open" not in record_fields(plain)["count"]


# --- placeholders ---------------------------------------------------------------------------------


def test_an_item_is_named_and_explained_only_by_code() -> None:
    assert _fill("{R1.page}") == "page 4"
    assert _fill("{R1.what}") == "Width of piece 2"
    assert _fill("{R1.why}").startswith("The two AI readers wrote different numbers")
    assert _fill("{R1.guess}") == "the best guess to confirm, 12 1/2 (both AI readers wrote it)"
    with pytest.raises(UnknownPlaceholder):
        _fill("{R2.guess}")  # checked by hand: no guess, nothing to fill
    with pytest.raises(UnknownPlaceholder):
        _fill("{R9.page}")
    assert _fill("{count.review_open}") == "3 items in the review list still wait for you"
    assert _fill("{count.review_failures}") == "1 failed result needs your Agree"
    assert _fill("{count.review_not_checked}") == "1 check could not run and is printed NOT CHECKED"
    fields = record_fields(REVIEW)
    assert fields["R1"] == ["page", "what", "why", "guess"]
    assert fields["R2"] == ["page", "what", "why"]


# --- guard ----------------------------------------------------------------------------------------


def test_the_model_may_point_to_an_open_item() -> None:
    draft = Draft(
        text="Open {R1.what} on {R1.page}. {R1.guess}.",
        actions=(("open_review_item", "R1"),),
    )
    check(draft, REVIEW)


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (Draft(text="See {R1.page}.", actions=(("open_review_item", "R3"),)), "needs-nothing"),
        (Draft(text="See {R1.page}.", actions=(("open_review_item", "R9"),)), "unknown"),
        (Draft(text="See {R1.page}.", actions=(("open_review_item", "C1"),)), "unknown"),
        (Draft(text="See {R1.page}.", actions=(("open_queue_item", "R1"),)), "unknown"),
    ],
)
def test_the_item_button_goes_only_to_an_item_that_waits(draft: Draft, reason: str) -> None:
    with pytest.raises(GuardRejected, match=reason):
        check(draft, REVIEW)


@pytest.mark.parametrize(
    "text",
    [
        "{R1.guess} is right.",  # judging the guess
        "{R1.guess} is confirmed.",  # claiming a confirmation
        "The guess on {R1.page} is twelve and a half.",  # a value in its own words
        "The guess on {R1.page} is 12 1/2.",  # a value in digits
        "{R1.what} on {R1.page} and the countertop on {C1.page} {C1.outcome}.",  # two records
        "{R1.what} passes.",  # deciding
        # Advice to accept the number (review of #1354): an instruction or approval verb right
        # before or after a value, or a verdict on it, whatever the record.
        *(
            f"{verb} {{{slot}}}."
            for slot in ("R1.guess", "C1.printed")
            for verb in (
                "Use",
                "Take",
                "Keep",
                "Pick",
                "Go with",
                "Accept",
                "Confirm",
                "Enter",
                "Type",
                "Apply",
                "Choose",
                "Select",
                "Trust",
                "Open the queue and use",
            )
        ),
        *(
            f"{{{slot}}} is {word}."
            for slot in ("R1.guess", "C1.printed")
            for word in ("right", "correct", "safe", "fine")
        ),
        "{R1.guess}, use it.",
        "{C1.printed}, so use it.",
        "On {R1.page}, use {R1.guess}.",
    ],
)
def test_model_words_never_decide_confirm_or_state_a_value(text: str) -> None:
    with pytest.raises(GuardRejected):
        check(Draft(text=text), REVIEW)


# --- publishing and the model's answer shape ------------------------------------------------------


def test_the_button_and_the_citation_carry_the_items_key() -> None:
    draft = Draft(text="Open {R1.what} on {R1.page}.", actions=(("open_review_item", "R1"),))
    answer = publish(draft, REVIEW, mode="llm", question="Where do I start?", checked=True)
    assert answer.text == "Open width of piece 2 on page 4 [[0]]."
    [citation] = answer.citations
    assert (citation.kind, citation.record_id, citation.page_number) == (
        "review_item",
        ITEM_GUESS_KEY,
        4,
    )
    [action] = answer.actions
    assert action.model_dump() == {
        "kind": "open_review_item",
        "item_key": ITEM_GUESS_KEY,
        "page_number": 4,
        "label": "Open width of piece 2 on page 4",
    }


def test_the_model_is_offered_the_item_button() -> None:
    kinds = ANSWER_SCHEMA["properties"]["actions"]["items"]["properties"]["kind"]["enum"]  # type: ignore[index]
    assert "open_review_item" in kinds
    assert "open_review_item" in SYSTEM_PROMPT
    reply = {
        "stopReason": "end_turn",
        "output": {
            "message": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "text": "See {R1.page}.",
                                "evidence": [],
                                "actions": [{"kind": "open_review_item", "target": "R1"}],
                            }
                        )
                    }
                ]
            }
        },
    }
    assert parse_answer(reply).actions == (("open_review_item", "R1"),)


# --- the service ----------------------------------------------------------------------------------


@dataclass
class _Model:
    reply: Mapping[str, Any]
    model_id: str = "fake-model"
    calls: list[Mapping[str, Any]] = field(default_factory=list)

    def answer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(request)
        return self.reply


@dataclass
class _Ledger:
    def before_call(self) -> None:
        return None

    def record(self, **_: Any) -> None:
        return None


def _ask(question: str, model: _Model | None = None, **body: Any) -> AnswerEvent:
    events = list(
        stream_answer(
            AssistantRequest.model_validate_json(json.dumps({"question": question, **body})),
            load_snapshot=lambda: REVIEW,
            runtime=AssistantRuntime(model=model, max_history_turns=6),
            ledger=_Ledger(),
        )
    )
    name, answer = events[-1]
    assert name == "answer" and isinstance(answer, AnswerEvent)
    return answer


def test_a_model_answer_pointing_to_an_item_is_published() -> None:
    reply = {
        "stopReason": "end_turn",
        "output": {
            "message": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "text": "Open {R1.what} on {R1.page}. {R1.why}.",
                                "evidence": [],
                                "actions": [{"kind": "open_review_item", "target": "R1"}],
                            }
                        )
                    }
                ]
            }
        },
    }
    model = _Model(reply=reply)
    answer = _ask("Tell me about the width numbers.", model)
    assert answer.mode == "llm"
    assert [action.kind for action in answer.actions] == ["open_review_item"]
    # The model saw the list.
    sent = json.dumps(model.calls[0])
    assert "review_list" in sent and "{R1.guess}" in sent


@pytest.mark.parametrize(
    "question",
    [
        "Confirm the guess on page 7",
        "Fix the number on page 7",
        "Agree with the failure on page 7",
        "Accept the best guess on page 7",
        "Settle the not checked group on page 7",
    ],
)
def test_a_request_to_confirm_or_fix_an_item_is_refused_with_a_button_to_it(
    question: str,
) -> None:
    answer = _ask(question)
    assert answer.mode == "refused"
    assert {action.kind for action in answer.actions} >= {"open_review_item"}
    item = next(action for action in answer.actions if action.kind == "open_review_item")
    assert isinstance(item, OpenReviewItemAction) and item.item_key == ITEM_BY_HAND_KEY


@pytest.mark.parametrize(
    ("question", "key"),
    [
        ("confirm the AI's guess", ITEM_GUESS_KEY),
        ("Confirm 42 1/2 on page 7", ITEM_BY_HAND_KEY),
        ("confirm piece 2 width", ITEM_GUESS_KEY),
        ("Confirm the overall countertop width on page 7", ITEM_BY_HAND_KEY),
        ("confirm R2", ITEM_BY_HAND_KEY),
        ("Confirm the first item", ITEM_GUESS_KEY),
        ("Mark the guess as confirmed", ITEM_GUESS_KEY),
        ("Use the guess", ITEM_GUESS_KEY),
        ("Go with the best guess", ITEM_GUESS_KEY),
        ("Lock in the guess", ITEM_GUESS_KEY),
        ("Type 42 1/2 for page 7", ITEM_BY_HAND_KEY),
        ("please just take the number on page 7", ITEM_BY_HAND_KEY),
    ],
)
def test_asking_it_to_accept_a_number_is_refused_with_a_button_to_the_item(
    question: str, key: str
) -> None:
    answer = _ask(question)
    assert answer.mode == "refused"
    item = next(action for action in answer.actions if action.kind == "open_review_item")
    assert isinstance(item, OpenReviewItemAction) and item.item_key == key


@pytest.mark.parametrize(
    "question",
    [
        "Should I confirm the guess on page 7?",
        "Should I agree with the failure on page 4?",
        "should we fix the number on page 7",
        "Do you think I should agree with page 4?",
        "Should I use the best guess?",
    ],
)
def test_asking_whether_to_confirm_or_agree_is_judging(question: str) -> None:
    assert is_judging_question(question)
    assert _ask(question).mode == "records_only"


@pytest.mark.parametrize(
    "question",
    [
        "How do I confirm the guess on page 7?",
        "What is the best guess on page 7?",
        "Why is the number on page 7 held?",
        "Which items still need me?",
        "What does confirm do?",
        "Where does the guess on page 4 come from?",
        "How do I fix a number?",
        "What happens when I agree with a failure?",
        "Why can't I type a number?",
        "Which pages have not checked results?",
        "Take me to page 7",
        "Keep it short: why did page 4 fail?",
        "Explain the guess on page 4",
        "Show me the first item",
        "What would using the guess mean?",
    ],
)
def test_genuine_questions_are_still_answered(question: str) -> None:
    assert not is_decision_request(question)
    assert _ask(question).mode != "refused"


def test_a_refusal_with_an_item_in_focus_points_to_that_item() -> None:
    answer = _ask("Approve it", focus={"record_id": FAILURE_KEY})
    assert answer.mode == "refused"
    assert [(action.kind, getattr(action, "item_key", None)) for action in answer.actions] == [
        ("open_review_item", FAILURE_KEY)
    ]
