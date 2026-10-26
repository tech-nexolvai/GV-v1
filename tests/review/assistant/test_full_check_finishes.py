"""#1817: a roster full check finishes its own work while money is left.

- A reply cut at its length limit while it held tool calls runs them and is sent on (three
  helpers of a paid run ended with no report so).
- The output allowances leave room for thinking plus the reply; a reply shape's answer is asked
  for once more when cut, and a report still cut has its pages said Not compared.
- The brief states the helper's real call allowance, and code (not the lead's guess about money)
  sends every page still not compared to one more closer pass while money is left.
- The tag table is read in page groups and compared together; the reader mends an unescaped inch
  mark inside a string and nothing else.

Every drawing value here is made up.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_engine, full_check_roster
from app.review.assistant.chat_engine import (
    RECORD_MAX_TOKENS,
    ROSTER_HELPER_MAX_TOKENS,
    ROSTER_WRAP_UP_MAX_TOKENS,
    SHAPED_HELPER_MAX_TOKENS,
)
from app.review.assistant.chat_tools import helper_footer
from app.review.assistant.full_check_roster import tag_page_groups
from app.review.assistant.reply_json import inch_marks_escaped, json_of_reply
from tests.review.assistant.chat_fakes import reply, text, tool
from tests.review.assistant.test_full_check_fanout import _first_text, _steps
from tests.review.assistant.test_full_check_roster import (
    SONNET,
    _clean_ink,
    _Lazy,
    _Roster,
    _roster,
)

#: The models' real output ceiling (Sonnet 5.5 and Opus 5.5: 128K tokens).
_MODEL_MAX_OUTPUT = 128_000


def _agents(outcome: Any) -> list[Mapping[str, Any]]:
    return [
        agent
        for block in outcome.blocks
        if block.get("type") == "step"
        for agent in block["step"].get("agents") or []
    ]


# ---- the output allowances ---------------------------------------------------------------------


def test_the_output_allowances_leave_room_for_thinking_and_the_reply() -> None:
    # A dozen tool calls after adaptive thinking did not fit in 6000 (the paid run of 2026-10-20).
    assert 16_000 <= ROSTER_HELPER_MAX_TOKENS <= _MODEL_MAX_OUTPUT
    assert ROSTER_HELPER_MAX_TOKENS <= SHAPED_HELPER_MAX_TOKENS <= _MODEL_MAX_OUTPUT
    assert RECORD_MAX_TOKENS >= 12_000 and ROSTER_WRAP_UP_MAX_TOKENS >= 6_000
    # The money held for one Sonnet 5.5 call at the roster's allowance stays small.
    assert ROSTER_HELPER_MAX_TOKENS * 10 / 1_000_000 <= 0.2  # dollars at $10 per million


class _Tags(_Roster):
    """The tag reader's table: cut at its length limit the first `cuts` times."""

    def __init__(self, cuts: int) -> None:
        super().__init__()
        self.cuts = cuts

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if not brief.startswith("TAGS:"):
            return super().helper(params, brief)
        if self.calls.get("tags", 0) < self.cuts:
            self._seen("tags", params)
            return reply(
                text('```json\n{"tags":[{"page":2,"side":"vendor","item":"Area 1: top","ta'),
                stop="max_tokens",
                model=SONNET,
            )
        return super().helper(params, brief)


def test_a_cut_tag_table_is_asked_for_once_more_with_the_full_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Tags(cuts=1)
    outcome = _roster(tmp_path, script).run()
    tags = script.params["tags"]
    assert len(tags) == 2
    assert {p["max_tokens"] for p in tags} == {SHAPED_HELPER_MAX_TOKENS}
    assert "Send the whole answer again" in json.dumps(tags[1]["messages"][-1])
    # The answer sent again is read and compared by code: the tag is on the findings.
    findings = [b["finding"] for b in outcome.blocks if b["type"] == "finding"]
    assert any(f["item"] == "Area 1: top: tag" for f in findings)
    assert "tag table could not be read" not in outcome.text


def test_a_tag_table_cut_twice_is_a_failed_step_never_a_piece_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Tags(cuts=5)
    outcome = _roster(tmp_path, script).run()
    assert script.calls["tags"] == 2  # asked once more, never again
    findings = [b["finding"] for b in outcome.blocks if b["type"] == "finding"]
    assert not any(str(f["item"]).endswith(": tag") for f in findings)
    assert "send the cloud drawing" not in outcome.text  # the tag step broke: not for sending


# ---- a reply cut while it held tool calls --------------------------------------------------------


class _CutWithCalls(_Roster):
    """The numbers helper of pages 2-3: its first reply holds its findings and its coverage, and
    is cut at its length limit after them (a last call cut in the middle of its input)."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        mine = (
            brief.startswith("OBJECTIVE:")
            and not brief.startswith("OBJECTIVE: the drawn things")
            and "on pages 2" in brief
        )
        if mine and _steps(params) == 0:
            self._seen("cut", params)
            found = {
                "page": 2,
                "item": "Top on page 2",
                "category": "countertop",
                "status": "different",
                "verified": False,
                "source": "architect",
                "vendor_dim": "D1",
                "architect_dim": "D2",
            }
            covered = {"pages": [{"page": p, "status": "different"} for p in (2, 3)]}
            return reply(
                tool("f2", "report_finding", found),
                tool("c2", "set_coverage", covered),
                stop="max_tokens",
                model=SONNET,
            )
        if mine:
            self._seen("after_cut", params)
        return super().helper(params, brief)


@pytest.mark.usefixtures("read_twice")
def test_a_reply_cut_with_tool_calls_runs_them_and_is_sent_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clean_ink(monkeypatch)
    script = _CutWithCalls()
    with caplog.at_level(logging.WARNING):
        outcome = _roster(tmp_path, script).run()
    assert script.calls.get("cut") == 1 and script.calls.get("after_cut")
    after = script.params["after_cut"][0]
    results = after["messages"][-1]["content"]
    ran = {block["tool_use_id"] for block in results if block.get("type") == "tool_result"}
    assert ran == {"f2", "c2"}  # both calls answered, none lost
    assert "cut at its length limit" in json.dumps(results)
    assert not any("no written report" in r.getMessage() for r in caplog.records)
    findings = [b["finding"] for b in outcome.blocks if b["type"] == "finding"]
    assert any(f["item"] == "Top on page 2" for f in findings)
    plan_1 = next(a for a in _agents(outcome) if a.get("id") == "plan-1")
    assert plan_1["ok"] and "CHECKLIST p2" in plan_1["report"]


class _AlwaysCut(_Roster):
    """The numbers helper of pages 2-3 never ends a report inside its length limit: its words
    stop after the first page's CHECKLIST line."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        mine = (
            brief.startswith("OBJECTIVE:")
            and not brief.startswith("OBJECTIVE: the drawn things")
            and "on pages 2" in brief
        )
        if not mine:
            return super().helper(params, brief)
        self._seen("cut", params)
        if _steps(params) == 0:
            return super().helper(params, brief)  # its findings and coverage, recorded
        words = "Done.\nCHECKLIST p2: architect 1/1; notes 0/0; strings 0/0; not compared: none\nCH"
        return reply(text(words), stop="max_tokens", model=SONNET)


def test_a_report_still_cut_has_its_pages_said_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clean_ink(monkeypatch)
    script = _AlwaysCut()
    harness = _roster(tmp_path, script)
    harness.engine.settings = dataclasses.replace(harness.engine.settings, helper_max_turns=3)
    noted: list[tuple[int, str]] = []
    original = harness.engine._note_coverage

    def note(page: int, words: str) -> None:
        noted.append((page, words))
        original(page, words)

    monkeypatch.setattr(harness.engine, "_note_coverage", note)
    with caplog.at_level(logging.WARNING):
        harness.run()
    said = [r.getMessage() for r in caplog.records if "report cut at its length" in r.getMessage()]
    assert said, "the cut report is said in the log"
    cut_pages = {page for page, words in noted if "report was cut at its length limit" in words}
    assert cut_pages == {3}  # page 2's line came through; page 3's was cut off


# ---- what the helpers are told about money, and the pages code sends on -------------------------


def test_the_brief_states_the_real_call_allowance(tmp_path: Path) -> None:
    from tests.review.assistant.chat_fakes import ScriptedProvider
    from tests.review.assistant.test_chat_engine import Harness

    harness = Harness(tmp_path, ScriptedProvider(lead=[]))
    workspace = harness.engine.tools.workspace
    chat = helper_footer("Check pages 2-3.", workspace, pages=[2, 3])
    # The chat's helpers: no number code does not enforce (#1819).
    assert "tool calls" not in chat and "the app tells you when they run out" in chat
    roster = helper_footer("LEAD REVIEW", workspace, pages=list(range(2, 18)), calls=48)
    assert "about 48 reading calls" in roster and "Budget: about" not in roster
    assert "never skip a page or stop early" in roster


def test_the_lead_review_is_told_its_own_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Lazy()  # pages are left open, so the lead review takes them
    _roster(tmp_path, script).run()
    briefs = [_first_text(p) for kind in script.params.values() for p in kind]
    lead = [b for b in briefs if b.startswith("LEAD REVIEW")]
    assert lead and all("Budget: about" not in b for b in lead)
    assert all("your money run out" in b for b in lead)


def test_pages_still_not_compared_after_the_lead_go_to_one_more_closer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paid run of 2026-10-20: the lead marked pages "not opened: Budget" with money left.
    Code sends them on; the AI's guess about money decides nothing."""
    _clean_ink(monkeypatch)
    script = _Lazy()  # its pages are never compared, by the helpers nor by the gap closers
    outcome = _roster(tmp_path, script).run()
    again = [a for a in _agents(outcome) if str(a.get("id")).startswith("again-")]
    assert again, "a closer took the pages still open after the lead review"
    assert all(str(a["description"]).startswith("Close what is still open on") for a in again)
    assert {a["id"] for a in again} == {"again-2", "again-3", "again-4", "again-5"}


def test_no_closer_after_the_lead_when_the_money_is_really_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    real_room = full_check_roster._Run._room

    def room(self: Any, phase: str) -> int:
        if phase == "lead" and getattr(self, "lead_taken", None):
            return full_check_roster.MIN_GAP_RUN_MICROS - 1  # after the lead: nearly nothing
        return int(real_room(self, phase))

    monkeypatch.setattr(full_check_roster._Run, "_room", room)
    outcome = _roster(tmp_path, _Lazy()).run()
    assert not [a for a in _agents(outcome) if str(a.get("id")).startswith("again-")]


def test_a_lead_note_of_budget_is_a_page_not_compared() -> None:
    coverage = {
        "pages": [
            {"page": 12, "status": "not_opened", "note": "Budget", "by": "helper:35"},
            {"page": 13, "status": "same", "note": None, "by": "helper:2"},
        ]
    }
    assert set(full_check_roster.unfinished_pages([12, 13], coverage)) == {12}


# ---- the tag table in page groups ---------------------------------------------------------------


def test_the_tag_table_is_read_in_page_groups_never_splitting_an_area() -> None:
    groups = [(3, 4, 5, 6, 7), (8, 9, 11, 12, 13), (15, 16, 17), (3, 4, 5, 6, 7), (19, 20, 23)]
    assert tag_page_groups(groups, most=10) == [
        (3, 4, 5, 6, 7, 8, 9, 11, 12, 13),
        (15, 16, 17, 19, 20, 23),
    ]
    assert tag_page_groups([tuple(range(1, 25))], most=10) == [tuple(range(1, 25))]
    assert tag_page_groups([]) == []


def test_two_readers_tables_are_compared_together() -> None:
    from types import SimpleNamespace

    shown: list[dict[str, Any]] = []
    reported: list[dict[str, Any]] = []

    def report(given: dict[str, Any], by: str) -> tuple[dict[str, Any], list[str]]:
        reported.append(given)
        return {**given, "id": f"F{len(reported)}"}, []

    engine = SimpleNamespace(
        book=SimpleNamespace(report=report), _show_finding=lambda f: shown.append(f)
    )
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    first = json.dumps(
        {"tags": [{"page": 4, "side": "architect", "item": "Bar: top", "tag": "QZ-7", "box": None}]}
    )
    second = json.dumps(
        {"tags": [{"page": 5, "side": "vendor", "item": "Bar: top", "tag": "QZ-8", "box": None}]}
    )
    readers = [SimpleNamespace(pages=(2, 3, 4)), SimpleNamespace(pages=(5, 6))]
    run._apply_tags([(readers[0], first), (readers[1], second)])
    assert [f["status"] for f in reported] == ["different"] and run.failed_steps == []
    run._apply_tags([(readers[0], first), (readers[1], '```json\n{"tags":[{"pa')])
    assert run.failed_steps == ["the tag table of pages 5-6 could not be read"]


# ---- the reply reader: the two real failure shapes, made-up content ---------------------------


def test_an_unescaped_inch_mark_inside_a_string_is_read() -> None:
    reply_text = (
        '```json\n{"proofs":[{"finding_id":"F9","vendor_dim":"D3","architect_dim":null,'
        '"outcome":"cannot_prove","why":"The vendor\'s 41 1/4\\" is D3. The architect prints '
        '5\'-2" and 4\'-0" here, none equal to 41 1/2\\"."}]}\n```'
    )
    with pytest.raises(ValueError):
        json.loads(reply_text.split("\n", 1)[1].rsplit("\n", 1)[0])  # the strict read refuses it
    answer = json_of_reply(reply_text)
    assert isinstance(answer, dict)
    why = answer["proofs"][0]["why"]
    assert "5'-2\" and 4'-0\" here" in why and why.endswith('41 1/2".')


@pytest.mark.parametrize(
    "valid",
    [
        '{"a": "x", "b": ["y", "z"], "c": {"d": "e"}}',
        '{"why": "it prints 5\'-2\\" here"}',
        '"just a string"',
        "[]",
    ],
)
def test_valid_json_is_never_changed_by_the_mend(valid: str) -> None:
    assert inch_marks_escaped(valid) == valid


@pytest.mark.parametrize(
    "broken",
    [
        # The tag table cut at its length limit in the middle of a row: never a piece of it.
        (
            '```json\n{"tags":[\n{"page":3,"side":"vendor","item":"Area 1: top","tag":"QZ-1",'
            '"box":{"page":3,"x0":1,"y0":2,"x1":3,"y1":4}},\n{"page":4,"side":"vendor","item":"Ar'
        ),
        # A stray quote where a string may end (before a comma): not mended, refused.
        '{"why": "it prints 5\'-2", then more", "outcome": "cannot_prove"}',
        # Two answers stay refused, mended or not.
        '{"why": "5\'-2" wide"} and {"why": "6\'-0" wide"}',
    ],
)
def test_a_cut_or_doubtful_reply_is_still_refused(broken: str) -> None:
    with pytest.raises(ValueError):
        json_of_reply(broken)


def test_the_cut_notes_ask_for_the_whole_answer() -> None:
    assert "whole answer again" in chat_engine._SHAPE_CUT_NOTE
    assert "CHECKLIST" in chat_engine._REPORT_CUT_NOTE


# ---- a GV filler note reaches the findings book -------------------------------------------------


def test_a_filler_range_note_is_accepted_by_the_real_findings_book(tmp_path: Path) -> None:
    """The paid run logged "ledger finding skipped: architect is empty" four times: GV's
    filler-range notes had an empty architect side, which the real book refuses."""
    from app.review.assistant import filler_range
    from tests.review.assistant.test_filler_range import _label, _one

    (made,) = _one([_label("D3", '5" FILLER', "5", "vendor")])
    assert made["architect"] == filler_range.NO_ARCHITECT_SIZE
    book = _roster(tmp_path, _Roster()).engine.book
    added = book.add_code_finding(made, created_by=filler_range.CREATED_BY)
    assert added is not None and added["architect"] == filler_range.NO_ARCHITECT_SIZE


def test_a_later_not_opened_never_hides_a_page_another_reader_compared(tmp_path: Path) -> None:
    book = _roster(tmp_path, _Roster()).engine.book
    book.set_coverage({"pages": [{"page": 2, "status": "different"}]}, "helper:1")
    book.set_coverage(
        {"pages": [{"page": 2, "status": "not_opened", "note": "Budget"}]}, "helper:9"
    )
    entry = next(p for p in book.coverage_map()["pages"] if p["page"] == 2)
    assert (entry["status"], entry["by"], entry["note"]) == ("different", "helper:1", "Budget")
    # The note still says the page was not finished: a later look takes it.
    assert 2 in full_check_roster.unfinished_pages([2], book.coverage_map())
    # Its own reader may still correct itself, and any other status replaces as before.
    book.set_coverage({"pages": [{"page": 2, "status": "not_opened"}]}, "helper:1")
    assert next(p for p in book.coverage_map()["pages"] if p["page"] == 2)["status"] == "not_opened"
