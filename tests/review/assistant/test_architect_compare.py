"""The assistant answers "what is different from the architect?" from `architect_compare`.

Synthetic records only, no model call. What matters: the comparison list reaches the records as
read-only entries (never an item to answer, never a queue button); "different" ones come first; the
answer is written by code from placeholders and passes the same guard; and a list without the
section (an older package, or a backend that has not sent it yet) changes nothing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.review.assistant.guard import check
from app.review.assistant.placeholders import render
from app.review.assistant.records import ReviewSnapshot, build_snapshot, prompt_records
from app.review.assistant.records_only import (
    answer_for_question,
    architect_difference_answer,
    is_architect_difference_question,
)
from tests.review.assistant.synthetic import Inputs, review_list


def _entry(index: int, result: str, **extra: object) -> dict[str, object]:
    paired = result in ("same", "different", "rule")
    return {
        "key": f"arch:sample-{index}",
        "page": 3 + index,
        "view": "Sample elevation A",
        "architect_text": '30"',
        "architect_inches": "30/1",
        "vendor_text": ('30 1/2"' if result == "different" else '30"') if paired else None,
        "vendor_inches": ("61/2" if result == "different" else "30/1") if paired else None,
        "result": result,
        "rule_id": "CT-SAMPLE-001" if result == "rule" else None,
        "rule_outcome": "PASS" if result == "rule" else None,
        "why": {
            "different": "The vendor's number is half an inch more than the architect's.",
            "unpaired": "No vendor number sits at this spot.",
            "unread": "The architect's number could not be read.",
        }.get(result, "Compared exactly."),
        "finding_id": None,
        "architect_ref": None,
        "vendor_ref": None,
        **extra,
    }


ENTRIES = [
    _entry(0, "same"),
    _entry(1, "rule"),
    _entry(2, "different"),
    _entry(3, "unpaired"),
    _entry(4, "same"),
    _entry(5, "unread"),
]


def _snapshot(
    entries: list[dict[str, object]] | None, counts: dict[str, int] | None = None
) -> ReviewSnapshot:
    listed = review_list()
    if entries is not None:
        # The backend adds these fields; until its types land the records read them tolerantly.
        object.__setattr__(listed, "architect_compare", entries)
        if counts is not None:
            object.__setattr__(listed.header, "architect_compare_counts", SimpleNamespace(**counts))
    given = Inputs()
    return build_snapshot(
        given.countertops,
        given.readiness,
        given.findings,
        given.decisions,
        checks_have_run=given.checks_have_run,
        review_items=listed,
    )


def test_comparisons_reach_the_records_different_first_and_never_need_you() -> None:
    snapshot = _snapshot(ENTRIES)
    compared = [item for item in snapshot.review_items if item.group == "architect"]
    assert [item.kind for item in compared] == [
        "architect_different",
        "architect_unpaired",
        "architect_unread",
        "architect_rule",
    ]
    assert [item.record_id for item in compared] == [
        "arch:sample-2",
        "arch:sample-3",
        "arch:sample-5",
        "arch:sample-1",
    ]
    assert all(not item.needs_you and item.status == "done" for item in compared)
    assert compared[0].what == 'Vendor 30 1/2" vs architect 30"'
    assert compared[3].what.startswith(
        'The vendor 30" against architect 30", checked by CT-SAMPLE-001'
    )
    assert snapshot.totals["architect_compared"] == 6
    assert snapshot.totals["architect_different"] == 1
    assert snapshot.totals["architect_need_you"] == 2
    # The model sees them as a separate, read-only group.
    document = json.loads(prompt_records(snapshot))
    listed = document["review_list"]["architect_compare"]
    assert [entry["result"] for entry in listed] == ["different", "unpaired", "unread", "rule"]


def test_the_header_counts_are_used_when_sent() -> None:
    counts = {"same": 80, "different": 3, "rule": 6, "unpaired": 4, "unread": 1}
    snapshot = _snapshot(ENTRIES, counts)
    assert snapshot.totals["architect_compared"] == 94
    assert snapshot.totals["architect_need_you"] == 5


def test_what_is_different_is_answered_from_the_records_and_passes_the_guard() -> None:
    snapshot = _snapshot(ENTRIES)
    question = "What is different from the architect?"
    assert is_architect_difference_question(question)
    draft = answer_for_question(snapshot, question)
    assert draft is not None
    check(draft, snapshot, by_model=False)
    text = render(draft.text, snapshot).text
    assert "6 architect numbers were compared with the vendor's" in text
    assert "1 architect number is different from the vendor's" in text
    assert 'Vendor 30 1/2" vs architect 30" on page 5' in text
    assert "half an inch more" in text
    assert "2 architect numbers have no vendor number beside them or were not read" in text
    assert draft.actions == (("open_page", "P5"),)
    # Records only: no decision words of the assistant's own.
    for word in ("approve", "accept", "sign off", "should"):
        assert word not in text.casefold()


def test_nothing_different_says_so() -> None:
    snapshot = _snapshot([_entry(0, "same"), _entry(1, "same")])
    draft = architect_difference_answer(snapshot)
    assert draft is not None
    check(draft, snapshot, by_model=False)
    text = render(draft.text, snapshot).text
    assert "no architect number is different from the vendor's" in text.casefold()
    assert draft.actions == ()


def test_without_the_section_nothing_changes() -> None:
    for entries in (None, []):
        snapshot = _snapshot(entries)
        assert not [item for item in snapshot.review_items if item.group == "architect"]
        assert "architect_compared" not in snapshot.totals
        assert architect_difference_answer(snapshot) is None
    # An older list still answers the same question the way it did before.
    assert answer_for_question(_snapshot(None), "What is different from the architect?") == (
        answer_for_question(_snapshot([]), "What is different from the architect?")
    )


def test_a_page_question_keeps_the_page_answer() -> None:
    assert not is_architect_difference_question("Is page 4 different from the architect?")


def test_an_open_comparison_waits_for_the_reviewer_and_gets_only_the_list_button() -> None:
    from app.review.assistant.contract import Draft

    entries = [_entry(2, "different", status="open", actions=["agree", "not_this"])]
    snapshot = _snapshot(entries)
    (item,) = [entry for entry in snapshot.review_items if entry.group == "architect"]
    assert item.needs_you and item.status == "open"
    # The guard lets the review-list button point at it, because it is open.
    check(
        Draft(text="{" + item.id + ".what}.", actions=(("open_review_item", item.id),)),
        snapshot,
        by_model=False,
    )
    # A rechecking one does not wait.
    rechecking = _snapshot([_entry(2, "different", status="rechecking")])
    assert not next(e for e in rechecking.review_items if e.group == "architect").needs_you
