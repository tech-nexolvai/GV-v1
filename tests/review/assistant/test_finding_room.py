"""#1827: code's own findings never lose to a book the AI filled, and nothing is left out silently.

A 60-page roster run filled its finding book with 300 helper findings; every finding code made at the
finish (the vendor's strings that don't add up, code's own pairs, the consistency and finish checks)
was then skipped without a word. Here: code's findings have their own room, the AI's refused
findings are said, a Continue carries the earlier answer's reading ledger, and an item whose
architect end may be a bulkhead is never red on two blind picks.

Every drawing value here is made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_findings import (
    CODE_FINDINGS_ROOM,
    MAX_FINDINGS,
    FindingBook,
)
from app.review.assistant.full_check_advocate import (
    STRONG_PICK,
    Check,
    Extent,
    decide,
    finding_words,
    room_end,
)
from app.review.assistant.sandbox import SandboxRefused
from tests.review.assistant.chat_fakes import ScriptedProvider
from tests.review.assistant.test_chat_engine import Harness
from tests.review.assistant.test_chat_findings import book, finding_input, workspace_with_pdf

#: A same-size item with no boxes: quick to add, never a red.
_SAME: dict[str, Any] = finding_input(
    arch_box=None, vendor_box=None, status="same", architect='37"', vendor='37"', why=None
)


def _filled(tmp_path: Path) -> FindingBook:
    """A book holding the AI's whole allowance of findings."""
    findings = book(tmp_path)
    for _ in range(MAX_FINDINGS):
        findings.report(dict(_SAME), "helper:1")
    return findings


def _string(page: int = 2, total: str = '50"') -> dict[str, Any]:
    """A vendor string whose parts (12" + 24" + 12" = 48") do not add up to `total`."""
    return {
        "page": page,
        "item": "Dimension string (elevation)",
        "category": "other",
        "architect": '48"',
        "vendor": total,
        "status": "different",
        "why": "Vendor's parts don't add up.",
        "verified": False,
        "source": "vendor_arithmetic",
        "parts": ['12"', '24"', '12"'],
        "printed_total": total,
    }


# ---- the room ------------------------------------------------------------------------------------


def test_a_book_the_ai_filled_still_takes_codes_own_findings(tmp_path: Path) -> None:
    findings = _filled(tmp_path)
    with pytest.raises(SandboxRefused, match=f"at most {MAX_FINDINGS} findings"):
        findings.report(dict(_SAME), "helper:2")
    # Code's string check, its pairs and its finish checks are added after the AI's findings.
    for maker in ("ledger", "pairs", "consistency", "architect_items"):
        added = findings.add_code_finding(_string(), created_by=maker)
        assert added is not None and added["created_by"] == maker
    # An AI finding carried over (a Continue) shares the AI's room: refused, and counted.
    assert findings.add_code_finding(dict(_SAME), room="ai") is None
    assert findings.left_out_for_room == {"ai": 2}


def test_codes_findings_never_take_the_ais_room(tmp_path: Path) -> None:
    findings = book(tmp_path)
    for _ in range(20):
        assert findings.add_code_finding(_string(), created_by="ledger") is not None
    for _ in range(MAX_FINDINGS):
        findings.report(dict(_SAME), "helper:1")  # the AI's whole allowance still fits
    assert len(findings.live()) == MAX_FINDINGS + 20


def test_codes_own_room_has_an_end_and_says_so(tmp_path: Path) -> None:
    findings = book(tmp_path)
    for _ in range(MAX_FINDINGS + CODE_FINDINGS_ROOM):
        findings.add_code_finding(dict(_SAME), created_by="pairs")
    assert findings.add_code_finding(_string(), created_by="ledger") is None
    assert findings.left_out_for_room == {"code": 1}


# ---- through the engine --------------------------------------------------------------------------


def _harness(tmp_path: Path) -> Harness:
    workspace_with_pdf(tmp_path, pages=3)
    harness = Harness(tmp_path, ScriptedProvider(lead=[]))
    harness.engine.tools.uploads = [
        {"name": "Vendor set.pdf", "pages": 3, "kind": "the vendor's shop drawings"}
    ]
    harness.engine.book.page_count = 3
    return harness


def test_a_string_recorded_after_the_book_filled_is_still_codes_finding(tmp_path: Path) -> None:
    engine = _harness(tmp_path).engine
    for _ in range(MAX_FINDINGS):
        engine.book.report(dict(_SAME), "helper:1")
    text, is_error = engine.reading_ledger.run_tool(
        "record_string",
        {
            "page": 2,
            "view": "elevation",
            "side": "vendor",
            "parts": ['12"', '24"', '12"'],
            "printed_total": '50"',
        },
        by="helper:1",
    )
    assert not is_error, text
    engine.add_code_findings()
    strings = [f for f in engine.book.live() if f.get("source") == "vendor_arithmetic"]
    assert len(strings) == 1 and strings[0]["printed_total"] == '50"'
    assert strings[0]["created_by"] == "ledger"
    assert engine.code_checks_failed == []


def test_codes_findings_left_out_for_room_make_the_check_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import chat_findings

    monkeypatch.setattr(chat_findings, "CODE_FINDINGS_ROOM", 0)
    engine = _harness(tmp_path).engine
    for _ in range(MAX_FINDINGS):
        engine.book.report(dict(_SAME), "helper:1")
    engine.reading_ledger.run_tool(
        "record_string",
        {"page": 2, "side": "vendor", "parts": ['12"', '24"'], "printed_total": '40"'},
        by="helper:1",
    )
    engine.add_code_findings()
    assert engine.book.left_out_for_room == {"code": 1}
    assert len(engine.code_checks_failed) == 1  # named: its red may be missing


def test_a_continue_carries_the_earlier_answers_strings_and_readings(tmp_path: Path) -> None:
    engine = _harness(tmp_path).engine
    box = {"page": 2, "x0": 10.0, "y0": 20.0, "x1": 90.0, "y1": 30.0, "file": "Vendor set.pdf"}
    blocks = [
        {"type": "finding", "finding": {"id": "F1"}},
        {
            "type": "trust",
            "trust": {
                "ledger_entries": {
                    "strings": [
                        {
                            "id": "S4",
                            "page": 2,
                            "view": "elevation",
                            "side": "vendor",
                            "parts": ['12"', '24"', '12"'],
                            "printed_total": '50"',
                            "box": box,
                            "by": "helper:3",
                            "result": "does_not_add_up",
                            "rereads": 1,
                        },
                    ],
                    "readings": [
                        {
                            "id": "R2",
                            "page": 3,
                            "view": "plan",
                            "side": "vendor",
                            "item": "Vanity top",
                            "kind": "width",
                            "value": '37"',
                            "box": None,
                            "by": "helper:2",
                        },
                    ],
                }
            },
        },
    ]
    assert engine.carry_ledger(blocks) == 2
    (string,) = engine.reading_ledger.strings
    assert string.by == "helper:3" and string.result == "does_not_add_up"
    assert string.printed_total == '50"' and string.box is not None
    (reading,) = engine.reading_ledger.readings
    assert reading.item == "Vanity top" and reading.value == '37"'
    # The finish's string check sees the carried string as one of this answer's own.
    engine.add_code_findings()
    assert [f["printed_total"] for f in engine.book.live() if f["created_by"] == "ledger"] == [
        '50"'
    ]


def test_a_continue_with_every_page_compared_launches_no_helper() -> None:
    from app.review.assistant import full_check_roster

    # A Continue whose earlier answer compared every page plans no helper: nothing is launched
    # (it raised IndexError before, and the whole Continue failed).
    assert full_check_roster._Run._launched(None, []) == []  # type: ignore[arg-type]


def test_an_answer_with_no_ledger_carries_nothing(tmp_path: Path) -> None:
    engine = _harness(tmp_path).engine
    assert engine.carry_ledger([{"type": "trust", "trust": {"ledger_entries": None}}]) == 0
    assert engine.reading_ledger.strings == ()


# ---- picks alone never prove an end that may be room context -------------------------------------

_SPANS = {"from": "floor", "to": "other"}
_PICK = {
    "pick": "p1.D7",
    "vendor_spans": _SPANS,
    "architect_spans": _SPANS,
    "vendor_ink_black": "yes",
    STRONG_PICK: True,
}
_CANNOT: tuple[Extent, str] = ("cannot_tell", "a dimension line's ends were not found")
_SAME_ENDS: tuple[Extent, str] = ("same_extent", "the ends land together")
_COMMON: dict[str, Any] = {
    "mode": "ai_pair_plus_code",
    "checks": [Check(n, "pass") for n in ("ink", "annotation", "roles", "axis", "round")],
    "printed": True,
    "helper_pick": "p1.D7",
    "code_paired": False,
    "advocate": _PICK,
}


@pytest.mark.parametrize(
    ("finding", "thing"),
    [
        (
            {
                "item": "Area 4: overall height of unit",
                "decision_help": {
                    "measures": "Architect: floor to top of bulkhead above the unit. Vendor: floor to "
                    "top of the unit."
                },
            },
            "the bulkhead",
        ),
        ({"item": "Area 4: overall height", "why": "The 9' may run to the soffit."}, "the soffit"),
        (
            {
                "item": "Area 4: overall height",
                "decision_help": {"confirm": "Is the beam by others?"},
            },
            "the beam",
        ),
        (
            {"item": "Area 4: overall height", "why": "Its top is a box above the unit."},
            "a box above the unit",
        ),
    ],
)
def test_an_architect_end_that_may_be_room_context_is_never_red_on_picks(
    finding: dict[str, Any], thing: str
) -> None:
    words = finding_words(finding)
    assert room_end(item=finding["item"], words=words) == thing
    verdict = decide(extent=_CANNOT, item=finding["item"], words=words, **_COMMON)
    assert verdict.result == "objects"
    assert f"the architect's dimension may run to {thing}, not the unit: confirm" in verdict.reason
    # Code's geometry that shows the same ends still judges it as before.
    proved = decide(
        extent=_SAME_ENDS,
        item=finding["item"],
        words=words,
        **_COMMON,
    )
    assert proved.result == "passed"


@pytest.mark.parametrize(
    "finding",
    [
        {
            "item": "Base cabinet: width",
            "why": "Vendor 30 1/2 against the architect's 30.",
            "decision_help": {"measures": "Both: cabinet side to cabinet side."},
        },
        # A helper's own doubt about where the vendor's value starts (`different_extent`) is
        # what the blind picks settle: a true Wrong stays red (key s3-08's pattern).
        {
            "item": "Buffet: floor to upper outlet centreline",
            "check_reason": "different_extent",
            "decision_help": {
                "measures": "vendor 50 may start at the top of the counter; "
                "architect 4'-0\" runs from the floor to the outlet"
            },
        },
    ],
)
def test_millwork_words_alone_still_pass_on_the_strong_pick(finding: dict[str, Any]) -> None:
    verdict = decide(extent=_CANNOT, item=finding["item"], words=finding_words(finding), **_COMMON)
    assert verdict.result == "passed" and verdict.answers["pairing"] == "proved_by_two_picks"
