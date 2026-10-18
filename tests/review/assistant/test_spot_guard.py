"""Nothing reaches the vendor that code cannot find on the drawing (#1650).

Made-up findings and labels (no client values): no PDF, no network, no paid call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.review.assistant.spot_guard import dropped, unbacked

BOX = {"page": 3, "x0": 10.0, "y0": 10.0, "x1": 40.0, "y1": 20.0, "file": "set.pdf"}
LABELS: dict[int, list[dict[str, Any]]] = {
    3: [
        {"id": "D1", "side": "architect", "inches": "37", "text": "3' - 1\""},
        {"id": "D2", "side": "vendor", "inches": "47.5", "text": '47 1/2"'},
    ]
}


def labels(file: str | None, page: int) -> Sequence[Mapping[str, Any]] | None:
    return LABELS.get(page)


def finding(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "F1",
        "page": 3,
        "vs_page": None,
        "item": "Bar: counter height",
        "architect": "3' - 1\"",
        "vendor": '47 1/2"',
        "status": "different",
        "source": "architect",
        "arch_box": None,
        "vendor_box": None,
        "witnesses": [],
    }
    base.update(changes)
    return base


def test_sizes_code_finds_printed_on_their_page_are_kept_without_a_spot() -> None:
    assert unbacked(finding(), labels) is None


def test_a_size_code_cannot_find_and_no_spot_is_an_internal_note() -> None:
    reason = unbacked(finding(vendor='49 1/4"'), labels)
    assert reason is not None and reason.startswith("Internal note, not for the vendor")
    assert "Bar: counter height" in reason and '49 1/4"' in reason and "page 3" in reason
    # A spot on the drawing (its picture) backs it: what was read there can be checked.
    assert unbacked(finding(vendor='49 1/4"', vendor_box=BOX), labels) is None


def test_words_with_no_spot_are_never_shown_to_the_vendor() -> None:
    """A GV rule result whose architect side is an internal pointer, and a completeness copy whose
    vendor side only says "shown": neither states a value code can find, and neither has a spot."""
    rule = finding(source="rules", architect="see GV Review's record", witnesses=["rule"])
    copy = finding(source="completeness", status="check", architect="not found", vendor="shown")
    assert unbacked(rule, labels) is not None
    assert unbacked(copy, labels) is not None
    assert [fid for fid, _why in dropped([rule, {**copy, "id": "F2"}], labels)] == ["F1", "F2"]


def test_absences_code_made_values_and_located_items_are_kept() -> None:
    missing = finding(status="check", vendor="not shown on the vendor")
    assert unbacked({**missing, "arch_box": BOX}, labels) is None
    # GV's rule requirement and code's sum are code's own values, not printed ones; a rule result
    # that states its requirement is code's even with no spot (GV's rule engine read the value).
    assert unbacked(finding(source="rules", architect='68"', vendor_box=BOX), labels) is None
    rule = finding(source="rules", architect='68"', vendor='72"', witnesses=["rule"])
    assert unbacked(rule, labels) is None
    summed = finding(source="vendor_arithmetic", architect='61 1/8"', vendor_box=BOX)
    assert unbacked(summed, labels) is None
    # A code witness that located the values, or a code label on a side.
    assert unbacked(finding(vendor='49 1/4"', witnesses=["printed"]), labels) is None
    assert unbacked(finding(vendor='49 1/4"', dims={"vendor": {"id": "D9"}}), labels) is None
    # Right items are not the vendor's business here.
    assert unbacked(finding(vendor='49 1/4"', status="same"), labels) is None


def test_a_page_whose_labels_are_not_known_is_never_held_against_a_size() -> None:
    assert unbacked(finding(page=9, vendor='49 1/4"', architect='37"'), labels) is None
    # Words are never a value, labels or not.
    assert unbacked(finding(page=9, architect="see GV Review's record"), labels) is not None


def test_the_architect_side_is_looked_for_on_its_own_page() -> None:
    LABELS[5] = []  # page 5's labels are known: none prints the architect's value
    other = finding(page=5, vs_page=3, vendor_box={**BOX, "page": 5})
    assert unbacked(other, labels) is None
    assert unbacked({**other, "vs_page": None}, labels) is not None
