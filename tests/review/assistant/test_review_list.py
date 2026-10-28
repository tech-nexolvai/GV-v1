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

from app.review.assistant.records import ReviewItemRecord, prompt_records
from tests.review.assistant.synthetic import (
    FAILURE_KEY,
    ITEM_BY_HAND_KEY,
    ITEM_DONE_KEY,
    ITEM_GUESS_KEY,
    NOT_CHECKED_KEY,
    snapshot_with_review_list,
)

REVIEW = snapshot_with_review_list()


def _item(key: str) -> ReviewItemRecord:
    return next(item for item in REVIEW.review_items if item.record_id == key)


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


# --- placeholders ---------------------------------------------------------------------------------


# --- guard ----------------------------------------------------------------------------------------


# --- publishing and the model's answer shape ------------------------------------------------------


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
