"""The read-number tool for a labelled value code cannot read (#1781). Synthetic facts, $0.

`read_number` (#1746) used to run only for values with no label; a labelled value whose inches code
could not read was greyed at once. Now the tool is asked at the label, and only its own two-route
agreement ("read") gives the value: the pair then goes through every check and the blind look like
any other, and the resolve loop's re-verification sees the value. Anything else stays Not compared,
with the tool's reason.
"""

from __future__ import annotations

from typing import Any

from app.review.assistant.read_number import Reading, Request
from app.review.assistant.simple_check import resolve
from app.review.assistant.simple_check.verify import Pack, verify_answer, verify_pair, with_blind
from tests.review.assistant import test_simple_verify as tsv


def _pack(side: str = "vendor") -> Pack:
    """The shared synthetic set with one label's value unreadable to code (no inches)."""
    docs = tsv._documents()
    page = 2 if side == "vendor" else 1
    for entry in docs["dims"]["pages"]:
        if entry["page"] == page:
            entry["labels"][0] = {**entry["labels"][0], "inches": None}
    return Pack.from_documents(main_file=tsv.FILE, **docs)


class _Number:
    """A scripted read-number tool: what it answers, and what it was asked."""

    def __init__(self, outcome: str, value: str | None = None, reason: str = "") -> None:
        self.answer = Reading(outcome, value=value, text=None, reason=reason)  # type: ignore[arg-type]
        self.asked: list[Request] = []

    def __call__(self, request: Request) -> Reading:
        self.asked.append(request)
        return self.answer


def _verify(pack: Pack, number: Any) -> Any:
    result = {
        "work_item": tsv.ROUND_2.id,
        "pairs": [tsv._pair("p2.D1", "p1.D1")],
        "other_items": [],
        "unaccounted_labels": [],
    }
    found = verify_answer(result, tsv.ROUND_2, pack, ink=lambda label: tsv.BLACK, number=number)
    assert len(found.items) == 1
    return found.items[0]


def test_a_labelled_value_code_cannot_read_is_read_by_the_tool_then_needs_the_blind_look() -> None:
    pack = _pack()
    number = _Number("read", "45.25", "read by two routes")
    found = _verify(pack, number)
    assert [(r.file, r.page, r.side, r.label) for r in number.asked] == [
        (tsv.FILE, 2, "vendor", "D1")
    ]
    assert number.asked[0].box == (100.0, 100.0, 130.0, 110.0)
    assert found.outcome == "candidate" and found.needs_blind
    assert found.vendor["inches"] == "45.25"
    assert found.vendor["read_number"]["outcome"] == "read"
    assert with_blind(found, tsv._agrees(found)).outcome == "red"
    assert with_blind(found, None, "not made").outcome == "not_compared"


def test_the_architects_unreadable_label_is_read_too() -> None:
    number = _Number("read", "42")
    found = _verify(_pack("architect"), number)
    assert [r.side for r in number.asked] == ["architect"]
    assert found.outcome == "candidate"


def test_a_value_the_tool_cannot_read_either_stays_not_compared_with_its_reason() -> None:
    pack = _pack()
    number = _Number("unread", reason="the two readers disagree")
    found = _verify(pack, number)
    assert found.outcome == "not_compared" and found.reason_code == "unreadable"
    assert "the read-number tool: the two readers disagree" in found.reason
    # Asked once: the re-verification of the same label does not ask again.
    again = verify_pair(tsv._pair("p2.D1", "p1.D1"), tsv.ROUND_2, pack, number=number)
    assert again.outcome == "not_compared" and len(number.asked) == 1
    refused = resolve.eligible(found, tsv._pair("p2.D1", "p1.D1"), tsv.ROUND_2, pack)
    assert isinstance(refused, resolve.Refusal) and refused.guard == "unreadable_label"


def test_the_resolve_loops_reverification_sees_the_value_the_tool_read() -> None:
    pack = _pack()
    _verify(pack, _Number("read", "45.25"))
    # The loop re-verifies with no tool at hand: the pack's label holds the reading.
    again = verify_pair(tsv._pair("p2.D1", "p1.D1"), tsv.ROUND_2, pack, ink=lambda l: tsv.BLACK)
    assert again.outcome == "candidate"


def test_without_the_tool_nothing_changes() -> None:
    found = _verify(_pack(), None)
    assert found.outcome == "not_compared"
    assert found.reason == "code could not read the value as a size"
