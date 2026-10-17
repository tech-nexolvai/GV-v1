"""A full check's second helper per page group: the drawn things (#1569).

Scripted replies, a synthetic page map and a real tiny PDF: no network, no paid call, no Docker.

- Code plans two helpers per job: the numbers (first, as before) and the drawn things (Sonnet,
  low effort, its own money cap), each with its own short brief and checklist line.
- At most `max_parallel_agents` helpers run at once; the rest queue.
- When both helpers of a group have reported, their duplicates (same page and item) are folded;
  a disagreement keeps the stronger status and goes to the lead to verify first.
- The drawn-things helper never records coverage (the numbers helper owns it).
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import finding_critic, full_check_plan
from app.review.assistant.chat_engine import FULL_CHECK_INSTRUCTION
from app.review.assistant.chat_tools import HELPER_SYSTEM_PROMPT
from app.review.assistant.full_check_brief import (
    LENS_FIELDS,
    LENS_SCOPE,
    NUMBERS_NOTE,
    PRINCIPLE,
    checklist_gaps,
    checklist_send_back,
    helper_brief,
)
from app.review.assistant.full_check_lenses import item_key, lens_folds
from app.review.assistant.full_check_plan import (
    DRAWN_JOB_CAP_MICROS,
    DRAWN_TOTAL_CAP_MICROS,
    drawn_job_cap,
    plan_jobs,
    with_lenses,
)
from app.review.assistant.full_check_specialists import HELPER_CORE
from tests.review.assistant.chat_fakes import SONNET, ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS, Harness
from tests.review.assistant.test_chat_findings import workspace_with_pdf
from tests.review.assistant.test_full_check_fanout import (
    FILE,
    HAIKU,
    RECORDS,
    _box,
    _first_text,
    _lead_first,
    _pack,
    _set_map,
    _steps,
)


@pytest.fixture(autouse=True)
def _reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")


def _helper_system(text: str) -> bool:
    """A helper's system prompt: the shared one, or a helper kind's (`full_check_specialists`)."""
    return text == HELPER_SYSTEM_PROMPT or text.startswith(HELPER_CORE)


# ---- the plan and the briefs -------------------------------------------------------------------


def test_two_lenses_per_job_numbers_first_with_partners_and_a_money_cap() -> None:
    plan = plan_jobs(_set_map(areas=5), FILE, parallel=8)
    assert plan is not None and len(plan.jobs) == 4

    both = with_lenses(plan)

    numbers, drawn = both.jobs[:4], both.jobs[4:]
    assert [job.lens for job in numbers] == ["numbers"] * 4
    assert [job.lens for job in drawn] == ["drawn"] * 4
    assert [job.number for job in both.jobs] == list(range(1, 9))
    for first, second in zip(numbers, drawn, strict=True):
        assert first.pages == second.pages and first.reference == second.reference
        assert first.partner == second.number and second.partner == first.number
        assert first.cap_micros is None and second.cap_micros == DRAWN_JOB_CAP_MICROS
    assert drawn[0].description == "Pages 2-3 · drawn things · 1. Area 1"
    assert numbers[0].description == "Pages 2-3 · numbers · 1. Area 1"
    # Pages are counted once whatever the lenses; the skipped pages are kept.
    assert both.pages == plan.pages and both.skipped == plan.skipped
    # Many jobs share the total cap.
    assert drawn_job_cap(4) == DRAWN_JOB_CAP_MICROS
    assert drawn_job_cap(20) == DRAWN_TOTAL_CAP_MICROS // 20


def test_each_lens_has_its_own_short_brief_and_checklist_line() -> None:
    page_map = _set_map(areas=1)
    numbers = helper_brief(
        [2, 3], file_name="/mnt/user-data/uploads/x.pdf", page_map=page_map, lens="numbers"
    )
    drawn = helper_brief(
        [2, 3], file_name="/mnt/user-data/uploads/x.pdf", page_map=page_map, lens="drawn"
    )
    single = helper_brief([2, 3], file_name="/mnt/user-data/uploads/x.pdf", page_map=page_map)

    # One principle for every lens; each lens says in one line what is its own.
    for brief, lens in ((numbers, "numbers"), (drawn, "drawn"), (single, "both")):
        assert PRINCIPLE in brief and LENS_SCOPE[lens] in brief
    assert "finish tags" in drawn and "Never a size" in drawn
    assert numbers.endswith(NUMBERS_NOTE) and "LENS: numbers." in numbers
    assert "CHECKLIST p<N>: architect a/b; strings a/b; not compared" in numbers
    assert drawn.startswith("OBJECTIVE: the drawn things on pages 2, 3 of ")
    assert "CHECKLIST p<N>: notes a/b; counts a/b; hardware a/b; not compared" in drawn
    assert "do not call set_coverage" in drawn and "Never report a dimension" in drawn
    assert "LEADS from GV's own page review" not in drawn
    # The prompt audit's style (#1562): no shouting, the reviewer's round words, reasons given.
    assert "EVERY" not in drawn and "DRAWN THINGS" not in drawn
    assert "still different" not in drawn and "Fixed, or Still wrong" in drawn
    assert "no pass/fail words (code records results, and a reviewer signs off)" in drawn
    assert "CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared" in single
    with pytest.raises(ValueError, match="unknown lens"):
        helper_brief([2], file_name="x", lens="colour")


def test_each_lens_checklist_is_read_back_with_its_own_counts() -> None:
    drawn_line = "CHECKLIST p2: notes 2/2; counts 3/3; hardware 1/1; not compared: none"
    assert checklist_gaps(drawn_line, [2], "drawn") == {}
    assert checklist_gaps(drawn_line, [2], "numbers") == {2: "no count for architect, strings"}
    short = "CHECKLIST p2: notes 1/2; counts 3/3; hardware 0/1; not compared: none"
    assert "notes, hardware not all done" in checklist_gaps(short, [2], "drawn")[2]
    numbers_line = "CHECKLIST p2: architect 4/4; strings 2/2; not compared: none"
    assert checklist_gaps(numbers_line, [2], "numbers") == {}
    assert checklist_gaps(numbers_line, [2]) == {2: "no count for notes"}
    assert LENS_FIELDS["both"] == ("architect", "notes", "strings")
    note = checklist_send_back({2: "no CHECKLIST line"}, "drawn")
    assert note is not None and "each piece of hardware" in note


# ---- folding the two helpers' duplicates --------------------------------------------------------


def _found(
    fid: str, page: int, item: str, status: str = "different", **more: Any
) -> dict[str, Any]:
    return {"id": fid, "page": page, "item": item, "status": status, **more}


def test_duplicates_fold_by_page_and_item_and_a_disagreement_keeps_the_stronger() -> None:
    numbers = [
        _found("F1", 4, "Kitchenette: bin drawer fronts", "different"),
        _found("F2", 5, "Kitchenette: overall width"),
        _found("F3", 6, "Pantry: shelves", "same"),
        _found("F4", 7, "Bar: legs", round=1),
    ]
    drawn = [
        _found("F9", 4, "kitchenette - the bin drawer fronts", "different"),
        _found("F10", 6, "Pantry: shelves", "different"),
        _found("F11", 5, "Kitchenette: pulls"),
        _found("F12", 7, "Bar: legs", round=2),
        _found("F13", 9, "Kitchenette: bin drawer fronts"),
    ]

    folds = lens_folds(numbers, drawn)

    assert [(f.keep, f.drop, f.disagree) for f in folds] == [
        ("F1", "F9", False),  # the same item and status: the numbers helper's stays
        ("F10", "F3", True),  # Wrong beats Right: the drawn-things helper's stays
    ]
    assert folds[1].statuses == ("same", "different")
    assert "both helpers of this page reported it" in folds[0].reason
    assert "disagree (same vs different)" in folds[1].reason
    assert item_key("Kitchenette: the bin-drawer fronts") == (
        "kitchenette",
        "bin",
        "drawer",
        "fronts",
    )


# ---- end to end, scripted ----------------------------------------------------------------------


class _LensScript:
    """The non-lead calls of a two-lens full check: each helper reports the page's top (the drawn
    helper too, saying "same" on page 4), the drawn helper a drawer note and a coverage it may not
    record; second reads agree. Counts the helpers running at once."""

    def __init__(self, delay: float = 0.0) -> None:
        self.lock = threading.Lock()
        self.delay = delay
        self.running = 0
        self.most = 0
        self.briefs: list[dict[str, Any]] = []
        self.finals: list[dict[str, Any]] = []

    def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        system = params["system"][0]["text"]
        asked = _first_text(params)
        if _helper_system(system) and asked == "Reply with OK.":
            return reply(text("OK"), model=SONNET, stop="max_tokens")
        if not _helper_system(system):
            if system == finding_critic.SYSTEM:
                return reply(text(json.dumps({"findings": []})), model=SONNET)
            third = params["model"] == SONNET
            answer = {"architect": '37"', "vendor": '46"', "sure": True}
            return reply(text(json.dumps(answer)), model=SONNET if third else HAIKU)
        return self.helper(params, asked)

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        drawn = brief.startswith("OBJECTIVE: the drawn things")
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        page = pages[0]
        step = _steps(params)
        final = params["tool_choice"] == {"type": "none"}
        if step == 0 and not final:
            with self.lock:
                self.briefs.append(dict(params))
                self.running += 1
                self.most = max(self.most, self.running)
            time.sleep(self.delay)
            top = {
                "page": page,
                "item": f"Area: top on page {page}",
                "category": "countertop",
                "architect": '37"',
                "vendor": '46"',
                "status": "same" if drawn and page == 4 else "different",
                "verified": False,
                "source": "architect",
                "arch_box": _box(page, 100),
                "vendor_box": _box(page, 300),
            }
            calls = [tool(f"t{page}{drawn}", "report_finding", top)]
            if drawn:
                note = {
                    **top,
                    "item": f"Area: pull-out drawers note on page {page}",
                    "architect": "pull-out drawers (note)",
                    "vendor": "2 doors drawn",
                    "status": "different",
                }
                calls.append(tool(f"n{page}", "report_finding", note))
            covered = {"pages": [{"page": p, "status": "different"} for p in pages]}
            calls.append(tool(f"c{page}{drawn}", "set_coverage", covered))
            return reply(*calls, stop="tool_use", model=SONNET)
        with self.lock:
            if step == 1:
                self.running -= 1
            self.finals.append(dict(params))
        lines = "\n".join(
            f"CHECKLIST p{p}: architect 1/1; strings 1/1; notes 1/1; counts 2/2; hardware 0/0; "
            "not compared: none"
            for p in pages
        )
        return reply(text(f"Pages from {page}: done.\n{lines}"), model=SONNET)


def _two_lens_check(
    tmp_path: Path, script: _LensScript, *, parallel: int = 8, areas: int = 5
) -> Harness:
    pages = 2 + areas * 2
    workspace_with_pdf(tmp_path, pages=pages)
    provider = ScriptedProvider(lead=[reply(text("Done."))], helper_answer=script)
    harness = Harness(tmp_path, provider, question=FULL_CHECK_INSTRUCTION, message_cap=15 * DOLLARS)
    engine = harness.engine
    folder = _pack(tmp_path, _set_map(areas))
    engine.tools.workspace = dataclasses.replace(harness.workspace, set_pack=folder)
    engine.tools.results = lambda page: RECORDS
    engine.tools.uploads = [{"name": FILE, "pages": pages, "kind": "the vendor's shop drawings"}]
    engine.book.page_count = pages
    engine.settings = dataclasses.replace(
        engine.settings,
        mode="full_check",
        products=("countertop",),
        max_parallel_agents=parallel,
        drawn_helpers=True,
    )
    return harness


def _blocks(outcome: Any, kind: str) -> list[dict[str, Any]]:
    return [block[kind] for block in outcome.blocks if block["type"] == kind]


def test_a_full_check_runs_two_helpers_per_group_and_folds_their_duplicates(tmp_path: Path) -> None:
    script = _LensScript()
    harness = _two_lens_check(tmp_path, script)

    outcome = harness.run()

    # Eight helpers for four jobs: four numbers briefs, four drawn-things briefs.
    assert len(script.briefs) == 8
    drawn = [b for b in script.briefs if _first_text(b).startswith("OBJECTIVE: the drawn things")]
    numbers = [b for b in script.briefs if "LENS: numbers" in _first_text(b)]
    assert len(drawn) == 4 and len(numbers) == 4
    for brief in drawn:
        assert brief["model"] == SONNET and brief["output_config"] == {"effort": "low"}
        assert "one other has the same pages for the numbers" in _first_text(brief)
    for brief in numbers:
        assert "one other has the same pages for the drawn things" in _first_text(brief)
    step = next(b["step"] for b in outcome.blocks if b["type"] == "step")
    assert len(step["agents"]) == 8
    assert sum("drawn things" in agent["description"] for agent in step["agents"]) == 4
    # The duplicates were folded: one "top" per page; page 4's disagreement kept as Wrong.
    findings = _blocks(outcome, "finding")
    tops = [f for f in findings if "top on page" in f["item"]]
    assert sorted(f["page"] for f in tops) == sorted({f["page"] for f in tops})
    assert all(f["status"] == "different" for f in tops)
    notes = [f for f in findings if "pull-out drawers note" in f["item"]]
    assert len(notes) == 4
    dropped = [b["reason"] for b in outcome.blocks if b["type"] == "finding_dropped"]
    assert any("both helpers of this page reported it" in reason for reason in dropped)
    assert any("disagree (different vs same)" in reason for reason in dropped)
    # The lead verifies the disagreement first.
    report = _lead_first(harness.provider)[-1]["text"]
    assert report.startswith("Helpers (planned by code, 8):")
    assert "The two helpers of a page disagree, verify these first (1):" in report
    assert "numbers helper different, drawn-things helper same" in report
    # Only the numbers helpers recorded coverage.
    coverage = _blocks(outcome, "coverage")[-1]
    helpers = {entry["by"] for entry in coverage["pages"] if entry["by"] != "code"}
    assert helpers <= {"helper:1", "helper:2", "helper:3", "helper:4"}


def test_helpers_queue_past_the_parallel_limit(tmp_path: Path) -> None:
    script = _LensScript(delay=0.15)
    harness = _two_lens_check(tmp_path, script, parallel=2)

    outcome = harness.run()

    # Three jobs (2 at once makes bigger jobs), two helpers each: six, never more than two at once.
    step = next(b["step"] for b in outcome.blocks if b["type"] == "step")
    assert len(step["agents"]) == 6 and all(agent["ok"] for agent in step["agents"])
    assert len(script.briefs) == 6 and script.most == 2


def test_a_drawn_things_helper_wraps_up_at_its_own_money_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(full_check_plan, "DRAWN_JOB_CAP_MICROS", 1)
    script = _LensScript()
    harness = _two_lens_check(tmp_path, script)

    outcome = harness.run()

    step = next(b["step"] for b in outcome.blocks if b["type"] == "step")
    by_lens = {agent["description"]: agent["stopped"] for agent in step["agents"]}
    assert all(stopped == "cost_cap" for name, stopped in by_lens.items() if "drawn things" in name)
    assert all(stopped is None for name, stopped in by_lens.items() if "numbers" in name)
    # They still reported (their final call, without tools).
    assert all(agent["report"] for agent in step["agents"])
