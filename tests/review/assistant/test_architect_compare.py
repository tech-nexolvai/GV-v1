"""The assistant answers "what is different from the architect?" from `architect_compare`.

Synthetic records only, no model call. What matters: the comparison list reaches the records as
read-only entries (never an item to answer, never a queue button); "different" ones come first; the
answer is written by code from placeholders and passes the same guard; and a list without the
section (an older package, or a backend that has not sent it yet) changes nothing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.review.assistant.records import ReviewSnapshot, build_snapshot, prompt_records
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
