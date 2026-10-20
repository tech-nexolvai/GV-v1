"""A full check planned by code, fanned out at once, its checks pipelined and timed (#1512).

Scripted replies, a synthetic page map and a real tiny PDF: no network, no paid call, no Docker.

- Code splits the set from the page map: an area's pages stay together, jobs are 3-5 pages and
  balanced, divider pages are recorded as not comparable.
- Every job starts at once (wall time is about one helper's, not their sum); the lead's first call
  comes after them, with their reports, and lists only the findings worth verifying.
- A helper's findings get their second reads as soon as it reports; after the answer the second
  reads, third reads and critic run together.
- Each phase's seconds and cost are recorded in the trust block and the log.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import finding_critic
from app.review.assistant.chat_engine import (
    FULL_CHECK_HELPER_CALLS,
    FULL_CHECK_HELPER_MAX_TOKENS,
    FULL_CHECK_INSTRUCTION,
    FULL_CHECK_LEAD_INSTRUCTION,
    FULL_CHECK_LEAD_MAX_TOKENS,
)
from app.review.assistant.chat_timings import Timings
from app.review.assistant.chat_tools import HELPER_FOOTER_MARK, HELPER_SYSTEM_PROMPT
from app.review.assistant.full_check_plan import (
    DIVIDER_NOTE,
    MAX_JOB_PAGES,
    pack_ready,
    plan_jobs,
    rules_summary,
    wait_for_page_map,
)
from app.review.assistant.full_check_specialists import HELPER_CORE
from tests.dummy_products import DUMMY, use_dummy_registry
from tests.review.assistant.chat_fakes import SONNET, ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS, Harness
from tests.review.assistant.test_chat_findings import workspace_with_pdf


def _helper_system(text: str) -> bool:
    """A helper's system prompt: the shared one, or a helper kind's (`full_check_specialists`)."""
    return text == HELPER_SYSTEM_PROMPT or text.startswith(HELPER_CORE)


__all__ = ["use_dummy_registry"]  # the fixture, imported so pytest finds it

HAIKU = "claude-haiku-5-5"
FILE = "Vendor set.pdf"


@pytest.fixture(autouse=True)
def _reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")


# ---- a synthetic page map ---------------------------------------------------------------------------


def _half(role: str, round_: int | None = None) -> dict[str, Any]:
    return {"box": [0, 0, 300, 300], "role": role, "heading": None, "round": round_}


def _page(
    page: int, halves: list[dict[str, Any]], area: str | None, section: str | None = "1. Walls"
) -> dict[str, Any]:
    roles = sorted({h["role"] for h in halves} - {"general"})
    return {
        "file": FILE,
        "page": page,
        "role": "+".join(roles) if roles else "general",
        "halves": halves,
        "rounds": [],
        "section": section,
        "area": area,
        "area_from_page": None,
        "sheet_numbers": [],
        "headings": [],
    }


def _paired(page: int, area: str) -> dict[str, Any]:
    return _page(page, [_half("architect"), _half("vendor", 1)], area)


def _round_two(page: int, area: str) -> dict[str, Any]:
    return _page(page, [_half("vendor", 2)], area)


def _divider(page: int) -> dict[str, Any]:
    return _page(page, [], None, section=None)


def _set_map(areas: int, pages_per_area: int = 2) -> dict[str, Any]:
    """A divider, then `areas` areas (an architect + vendor page, then later rounds), then a
    divider at the end."""
    pages = [_divider(1)]
    number = 2
    for n in range(1, areas + 1):
        area = f"{n}. Area {n}"
        pages.append(_paired(number, area))
        number += 1
        for _ in range(pages_per_area - 1):
            pages.append(_round_two(number, area))
            number += 1
    pages.append(_divider(number))
    return {"version": 1, "files": [FILE], "pages": pages}


def _pack(root: Path, page_map: Mapping[str, Any], state: str = "ready") -> Path:
    folder = root / "pack"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")
    done = ["drawings_only", "page_map"] + (["pages", "contact_sheets"] if state == "ready" else [])
    (folder / "status.json").write_text(json.dumps({"state": state, "done": done}))
    return folder


# ---- the plan --------------------------------------------------------------------------------------


def test_the_plan_keeps_areas_together_balances_jobs_and_skips_dividers() -> None:
    # 42 pages: a divider, 20 areas of 2 pages (architect and vendor, then round 2), a divider.
    page_map = _set_map(areas=20, pages_per_area=2)
    plan = plan_jobs(page_map, FILE, parallel=8)

    assert plan is not None
    sizes = [len(job.pages) for job in plan.jobs]
    assert all(size <= MAX_JOB_PAGES for size in sizes)
    assert max(sizes) - min(sizes) <= 2
    assert len(plan.jobs) == 10  # 40 pages in areas of 2: 4 pages a job
    # Every content page once, in order; an area's two pages in one job.
    pages = [page for job in plan.jobs for page in job.pages]
    assert pages == list(range(2, 42))
    for job in plan.jobs:
        for page in job.pages:
            if page % 2 == 0:  # an area's first page: its second page is in the same job
                assert page + 1 in job.pages
    assert plan.skipped == ((1, DIVIDER_NOTE), (42, DIVIDER_NOTE))
    # Areas of 3 pages, 12 helpers at once: jobs of 3 pages.
    wide = plan_jobs(_set_map(areas=12, pages_per_area=3), FILE, parallel=12)
    assert wide is not None and [len(job.pages) for job in wide.jobs] == [3] * 12


def test_a_long_area_is_cut_and_its_later_parts_look_at_its_architect_page() -> None:
    page_map = _set_map(areas=1, pages_per_area=8)  # pages 2-9: one area
    plan = plan_jobs(page_map, FILE, parallel=8)

    assert plan is not None
    assert [job.pages for job in plan.jobs] == [(2, 3, 4, 5, 6), (7, 8, 9)]
    assert plan.jobs[1].reference == ((FILE, 2),)
    assert plan.jobs[0].reference == ()
    assert plan.jobs[0].description == "Pages 2-6 · 1. Area 1"


def test_no_plan_without_pasted_drawings_or_a_page_map() -> None:
    nothing = {"pages": [_divider(1), _divider(2)]}
    assert plan_jobs(nothing, FILE, parallel=8) is None
    assert plan_jobs(None, FILE, parallel=8) is None


def test_another_uploads_pages_of_the_same_area_are_given_to_look_at() -> None:
    page_map = _set_map(areas=3)
    page_map["pages"].append(
        {**_page(1, [_half("architect")], "2. Area 2"), "file": "Architect.pdf"}
    )
    plan = plan_jobs(page_map, FILE, parallel=8)

    assert plan is not None
    holder = next(job for job in plan.jobs if 4 in job.pages)
    assert ("Architect.pdf", 1) in holder.reference


def test_the_rules_summary_lists_the_picked_products_checks_only() -> None:
    rules = (
        "# GV's rules\n\n## The checks\n\n### CT-A-001: Widths match\n\n- Product: countertop.\n"
        "- What it checks: Every width matches the architect.\n\n### CT-B-001: Depth\n\n"
        "- Product: countertop.\n\n## Other products (not picked for this chat)\n\n"
        "### CAB-C-001: Cabinets\n\n- What it checks: Cabinet widths.\n"
    )
    summary = rules_summary(rules)

    assert summary.splitlines() == [
        "- CT-A-001: Widths match: Every width matches the architect.",
        "- CT-B-001: Depth",
    ]


def test_waiting_for_the_page_map(tmp_path: Path) -> None:
    stop = threading.Event()
    building = _pack(tmp_path / "a", _set_map(2), state="building")
    (building / "status.json").write_text(json.dumps({"state": "building", "done": []}))

    def finish() -> None:
        time.sleep(0.3)
        (building / "status.json").write_text(
            json.dumps({"state": "building", "done": ["drawings_only", "page_map"]})
        )

    threading.Thread(target=finish).start()
    started = time.monotonic()
    assert wait_for_page_map(building, stop, timeout=5) is not None
    assert time.monotonic() - started < 3
    assert not pack_ready(building)
    failed = _pack(tmp_path / "b", _set_map(2))
    (failed / "status.json").write_text(json.dumps({"state": "failed", "done": []}))
    assert wait_for_page_map(failed, stop, timeout=5) is None
    assert wait_for_page_map(None, stop) is None
    slow = _pack(tmp_path / "c", _set_map(2))
    (slow / "status.json").write_text(json.dumps({"state": "building", "done": []}))
    assert wait_for_page_map(slow, stop, timeout=0.3) is None


# ---- the fan-out, end to end with scripted replies -----------------------------------------------


class _Gate:
    """Opens when `parties` callers have arrived; a stream event (`__wait__`) waits on it and
    records whether it had to give up."""

    def __init__(self, parties: int, delay: float = 0.0) -> None:
        self.parties = parties
        self.delay = delay
        self.arrived = 0
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.timed_out = False

    def arrive(self) -> dict[str, Any]:
        with self.lock:
            self.arrived += 1
            if self.arrived >= self.parties:
                self.event.set()
        return {"type": "__wait__", "until": self}

    def wait(self, _timeout: float) -> bool:
        opened = self.event.wait(3)
        if not opened:
            self.timed_out = True
        time.sleep(self.delay)
        return opened


def _first_text(params: Mapping[str, Any]) -> str:
    return "\n".join(
        block["text"] for block in params["messages"][0]["content"] if block["type"] == "text"
    )


def _steps(params: Mapping[str, Any]) -> int:
    return sum(1 for message in params["messages"] if message["role"] == "assistant")


def _box(page: int, x0: float) -> dict[str, Any]:
    return {"page": page, "x0": x0, "y0": 100, "x1": x0 + 60, "y1": 116}


RECORDS = {
    "records": {
        "countertops": [
            {
                "id": "c1",
                "page": 2,
                "label": "Vanity top",
                "outcome": "Needs correction",
                "printed_overall": "3'-10\"",
                "needed_overall": '37"',
            }
        ],
    },
}


class _Script:
    """The non-lead calls of a full check: the warm-up, the helpers, the second and third reads
    and the critic, answered from their parameters, with what each saw recorded."""

    def __init__(self, jobs: int, helper_delay: float = 0.0, category: str = "countertop") -> None:
        self.category = category
        self.lock = threading.Lock()
        self.start_gate = _Gate(jobs, delay=helper_delay)
        self.calls: list[tuple[str, str, str]] = []
        self.briefs: list[dict[str, Any]] = []
        self.warmups: list[Mapping[str, Any]] = []
        self.gates: dict[str, _Gate] = {}

    def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        system = params["system"][0]["text"]
        asked = _first_text(params)
        if _helper_system(system) and asked == "Reply with OK.":
            self.warmups.append(params)
            return reply(text("OK"), model=SONNET, stop="max_tokens")
        if _helper_system(system):
            return self.helper(params, asked)
        if system == finding_critic.SYSTEM:
            return self.critic(params, asked)
        return self.read(params, asked)

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        page = pages[0]
        step = _steps(params)
        with self.lock:
            self.calls.append(("helper", str(page), str(step)))
            if step == 0:
                self.briefs.append(dict(params))
        if step == 0:
            found = {
                "page": page,
                "item": f"Top on page {page}",
                "category": self.category,
                "architect": '37"',
                "vendor": '46"',
                "status": "different",
                "verified": False,
                "source": "architect",
                "arch_box": _box(page, 100),
                "vendor_box": _box(page, 300),
            }
            if page == 2:
                found["record_id"] = "c1"
            covered = {"pages": [{"page": p, "status": "different"} for p in pages]}
            events = reply(
                tool(f"f{page}", "report_finding", found),
                tool(f"c{page}", "set_coverage", covered),
                stop="tool_use",
                model=SONNET,
            )
            return [self.start_gate.arrive(), *events]
        checklist = "\n".join(
            f"CHECKLIST p{p}: architect 1/1; notes 0/0; strings 1/1; not compared: none"
            for p in pages
        )
        events = reply(text(f"Pages from {page}: the top differs.\n{checklist}"), model=SONNET)
        gate = self.gates.get(f"helper-end:{page}")
        return [gate.arrive(), *events] if gate is not None else events

    def read(self, params: Mapping[str, Any], asked: str) -> list[dict[str, Any]]:
        # Both reads fence the item's name as data (#1775, C6); an older question quoted it.
        fenced = '<drawing_data source="item">\n'
        if fenced in asked:
            item = asked.split(fenced, 1)[1].split("\n</drawing_data>", 1)[0]
        else:
            item = asked.split('printed for "', 1)[1].split('"', 1)[0]
        third = params["model"] == SONNET
        with self.lock:
            self.calls.append(("third" if third else "second", item, ""))
        # The top on page 6 reads the other way (a disagreement); the others agree.
        vendor = '45"' if item == "Top on page 6" else '46"'
        answer = {"architect": '37"', "vendor": vendor, "sure": True}
        events = reply(text(json.dumps(answer)), model=SONNET if third else HAIKU)
        for key, gate in self.gates.items():
            if key == f"read:{item}":
                return [gate.arrive(), *events]
            if key.startswith("arrive-read:") and key.split(":", 1)[1] == item:
                gate.arrive()
        return events

    def critic(self, params: Mapping[str, Any], asked: str) -> list[dict[str, Any]]:
        ids = [
            block["text"].split(" ", 2)[1].rstrip(",")
            for block in params["messages"][0]["content"]
            if block["type"] == "text" and block["text"].startswith("Finding ")
        ]
        with self.lock:
            self.calls.append(("critic", ",".join(ids), ""))
        entries = [
            {
                "id": finding_id,
                "same_item": "yes",
                "right_dimension": "yes",
                "vendor_ink": "yes",
                "architect_side": "yes",
                "note": "",
            }
            for finding_id in ids
        ]
        events = reply(text(json.dumps({"findings": entries})), model=SONNET)
        gate = self.gates.get("critic")
        return [gate.arrive(), *events] if gate is not None else events


def _full_check(
    tmp_path: Path,
    script: _Script,
    lead: list[Any],
    *,
    areas: int = 5,
    parallel: int = 8,
    products: tuple[str, ...] = ("countertop",),
) -> Harness:
    pages = 2 + areas * 2
    workspace_with_pdf(tmp_path, pages=pages)
    provider = ScriptedProvider(lead=lead, helper_answer=script)
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
        products=products,
        max_parallel_agents=parallel,
    )
    return harness


def _lead_first(provider: ScriptedProvider) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = json.loads(provider.lead_calls[0])["messages"][0]["content"]
    return content


def _blocks(outcome: Any, kind: str) -> list[dict[str, Any]]:
    return [block[kind] for block in outcome.blocks if block["type"] == kind]


def test_a_full_check_starts_every_job_at_once_and_the_lead_only_verifies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 5 areas of 2 pages: content pages 2-11 in 4 jobs (2, 2, 2 and 4 pages... balanced).
    script = _Script(jobs=4, helper_delay=1.0)
    lead = [reply(text("Four tops differ; one needs a look."))]
    harness = _full_check(tmp_path, script, lead)
    caplog.set_level(logging.WARNING, logger="app.review.assistant.chat_engine")

    outcome = harness.run()

    provider = harness.provider
    # All four jobs were running at the same time (each waited for the others to start).
    assert script.start_gate.arrived == 4 and not script.start_gate.timed_out
    # The prompt cache was warmed once, with the helpers' own system prompt and tools.
    assert len(script.warmups) == 1
    warm = script.warmups[0]
    assert warm["max_tokens"] == 16 and warm["tools"] == script.briefs[0]["tools"]
    assert warm["system"] == script.briefs[0]["system"]
    # One lead call, made after every helper reported, with a verify-only first message.
    assert len(provider.lead_calls) == 1
    first = _lead_first(provider)
    texts = [block["text"] for block in first]
    assert FULL_CHECK_INSTRUCTION not in texts and FULL_CHECK_LEAD_INSTRUCTION in texts
    report = texts[-1]
    assert report.startswith("Helpers (planned by code, 4):")
    assert report.count("### Helper ") == 4 and "the top differs" in report
    assert len(_blocks(outcome, "finding")) == 4  # the recorded vanity top took page 2's reading
    lead_call = json.loads(provider.lead_calls[0])
    assert lead_call["max_tokens"] == FULL_CHECK_LEAD_MAX_TOKENS
    # The helpers: low effort, the smaller allowance, their pages and a 200-word report.
    for brief in script.briefs:
        assert brief["output_config"] == {"effort": "low"}
        assert brief["max_tokens"] == FULL_CHECK_HELPER_MAX_TOKENS
        words = brief["messages"][0]["content"][0]["text"]
        assert words.startswith("OBJECTIVE: a full GV review of the vendor's drawings on pages ")
        assert "WHAT TO COMPARE: " in words and "START: you are one of 4 helpers" in words
        # Each helper kind has its own system prompt: the shared core and its knowledge (#roster).
        system = brief["system"][0]["text"]
        assert system.startswith(HELPER_CORE) and "what-dimensions-measure.md" in system
        assert HELPER_FOOTER_MARK in words and "at most 200 words" in words
        assert "Your pages: " in words and "/mnt/user-data/set/pages/Vendor_set/p-0" in words
    # Divider pages: not comparable, by code.
    coverage = _blocks(outcome, "coverage")[-1]
    by_page = {entry["page"]: entry for entry in coverage["pages"]}
    assert by_page[1]["status"] == "not_comparable" and by_page[1]["by"] == "code"
    assert by_page[12]["note"] == DIVIDER_NOTE
    # Timings: in the trust block, per phase and per helper, and in the log.
    trust = _blocks(outcome, "trust")[-1]
    timings = trust["timings"]
    phases = {phase["phase"]: phase for phase in timings["phases"]}
    for name in ("seeding", "set_pack", "planning", "prewarm", "helpers", "lead_verification"):
        assert name in phases, name
    assert phases["helpers"]["calls"] == 8 and float(phases["helpers"]["cost_usd"]) > 0
    # Wall time is about one helper's (1 s and more each), not the sum of the four.
    helper_sum = sum(helper["seconds"] for helper in timings["helpers"])
    assert helper_sum >= 4.0 and phases["helpers"]["seconds"] < 0.6 * helper_sum
    assert [h["pages"] for h in timings["helpers"]] == [[2, 3], [4, 5], [6, 7], [8, 9, 10, 11]]
    assert all(h["calls"] == 2 and h["seconds"] >= 1.0 for h in timings["helpers"])
    assert float(timings["cost_usd"]) == pytest.approx(outcome.cost_micros / DOLLARS, abs=0.01)
    assert any("assistant chat timings (full_check)" in r.getMessage() for r in caplog.records)
    # The two files are still made.
    assert [f["name"] for f in harness.files] == [
        "Vendor set - GV cloud drawing.pdf",
        "Vendor set - AI check report.pdf",
        "Vendor set - Not compared.pdf",
        "Vendor set - Not compared.xlsx",
        "Vendor set - GV review package.zip",
    ]


def test_second_reads_start_as_each_helper_reports_and_the_lead_skips_backed_findings(
    tmp_path: Path,
) -> None:
    script = _Script(jobs=4)
    # The last job (pages 8-11) may only finish once another job's top was read again: the
    # second reads run while helpers are still working.
    script.gates["helper-end:8"] = _Gate(2)
    script.gates["arrive-read:Top on page 4"] = script.gates["helper-end:8"]
    lead = [reply(text("Done."))]
    harness = _full_check(tmp_path, script, lead)

    outcome = harness.run()

    assert not script.gates["helper-end:8"].timed_out
    report = _lead_first(harness.provider)[-1]["text"]
    findings = {f["item"]: f for f in _blocks(outcome, "finding")}
    # Page 2's helper read GV's recorded difference: folded into the finding the app made.
    vanity = next(f for f in _blocks(outcome, "finding") if f.get("record_id") == "c1")
    assert vanity["created_by"] == "gv_review" and vanity["vendor_box"]["page"] == 2
    assert vanity["vendor"] == '46"' and "Top on page 2" not in findings
    dropped = [b for b in outcome.blocks if b["type"] == "finding_dropped"]
    assert dropped and dropped[0]["reason"].startswith(f"Folded into {vanity['id']}")
    # Page 6's second read disagrees: the lead verifies it, with its boxes. The others are backed:
    # a second read agrees (pages 4 and 8), or code's rule check (GV's recorded vanity top).
    verify = report.split("To verify (", 1)[1]
    six = findings["Top on page 6"]
    assert verify.startswith("1):") and f"{six['id']} page 6" in verify
    assert 'second read disagrees (read 37" / 45")' in verify
    assert "vendor p.6 [300,100,360,116]" in verify
    backed = report.split("no re-check needed (", 1)[1].split("\n", 1)[0]
    assert backed.startswith("3): ")
    for finding in (vanity, findings["Top on page 4"], findings["Top on page 8"]):
        assert finding["id"] in backed and finding["id"] not in verify
    assert vanity["checks"]["rule"] == "agrees"
    for page in (4, 8):
        assert findings[f"Top on page {page}"]["checks"]["second_read"] == "agrees"


def test_after_the_answer_the_second_reads_and_the_critic_run_together(tmp_path: Path) -> None:
    script = _Script(jobs=4)
    # The lead reports one more finding; its second read and the critic must overlap.
    together = _Gate(2)
    script.gates["critic"] = together
    script.gates["read:Lead's own top"] = together
    lead_finding = {
        "page": 11,
        "item": "Lead's own top",
        "category": "countertop",
        "architect": '37"',
        "vendor": '46"',
        "status": "different",
        "verified": True,
        "source": "architect",
        "arch_box": _box(11, 100),
        "vendor_box": _box(11, 300),
    }
    lead = [
        reply(tool("l1", "report_finding", lead_finding), stop="tool_use"),
        reply(text("Five tops differ.")),
    ]
    harness = _full_check(tmp_path, script, lead)

    outcome = harness.run()

    assert together.arrived == 2 and not together.timed_out
    kinds = [kind for kind, _item, _step in script.calls]
    assert kinds.count("critic") == 1 and kinds.count("third") == 1  # page 6's disagreement
    # A helper's finding read before the lead's answer is not read again after it.
    seconds = [item for kind, item, _step in script.calls if kind == "second"]
    assert sorted(seconds) == sorted(set(seconds))
    trust = _blocks(outcome, "trust")[-1]
    phases = {phase["phase"]: phase for phase in trust["timings"]["phases"]}
    for name in ("second_reads", "third_reads", "critic", "finish_checks", "cloud_drawing"):
        assert name in phases, name
    assert phases["critic"]["calls"] == 1 and phases["third_reads"]["calls"] == 1


def test_a_full_check_helper_wraps_up_after_its_reading_calls(tmp_path: Path) -> None:
    calls: list[int] = []

    def answer(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not _helper_system(params["system"][0]["text"]):
            return reply(text('{"architect": null, "vendor": null, "sure": false}'), model=HAIKU)
        if _first_text(params) == "Reply with OK.":
            return reply(text("OK"), model=SONNET)
        calls.append(_steps(params))
        if params["tool_choice"] == {"type": "none"}:
            return reply(text("Wrapped up."), model=SONNET)
        views = [
            tool(
                f"v{_steps(params)}{n}", "view", {"path": "/mnt/user-data/set", "description": "x"}
            )
            for n in range(5)
        ]
        return reply(*views, stop="tool_use", model=SONNET)

    workspace_with_pdf(tmp_path, pages=5)
    provider = ScriptedProvider(
        lead=[reply(text("Done.")), reply(text("Done; pages 1-5 not compared."))],
        helper_answer=answer,
    )
    harness = Harness(tmp_path, provider, question=FULL_CHECK_INSTRUCTION)
    engine = harness.engine
    folder = _pack(tmp_path, {"files": [FILE], "pages": [_paired(p, "1. A") for p in (1, 2, 3)]})
    engine.tools.workspace = dataclasses.replace(harness.workspace, set_pack=folder)
    engine.settings = dataclasses.replace(engine.settings, mode="full_check")

    outcome = harness.run()

    # 5 views a turn: after 3 turns (15 >= 12 reading calls) it is told to wrap up.
    assert FULL_CHECK_HELPER_CALLS == 12
    assert calls == [0, 1, 2, 3]
    step = next(b["step"] for b in outcome.blocks if b["type"] == "step")
    assert step["agents"][0]["stopped"] == "turn_limit"


def _one_job(tmp_path: Path, answer: Any, lead: list[Any]) -> Harness:
    """A full check of one area on pages 1-3 (one helper), every non-lead call from `answer`."""
    workspace_with_pdf(tmp_path, pages=3)
    provider = ScriptedProvider(lead=lead, helper_answer=answer)
    harness = Harness(tmp_path, provider, question=FULL_CHECK_INSTRUCTION)
    engine = harness.engine
    folder = _pack(tmp_path, {"files": [FILE], "pages": [_paired(p, "1. A") for p in (1, 2, 3)]})
    engine.tools.workspace = dataclasses.replace(harness.workspace, set_pack=folder)
    engine.tools.uploads = [{"name": FILE, "pages": 3, "kind": "the vendor's shop drawings"}]
    engine.settings = dataclasses.replace(engine.settings, mode="full_check")
    return harness


def test_a_helper_is_sent_back_once_to_finish_its_checklist_then_the_lead_gets_the_gaps(
    tmp_path: Path,
) -> None:
    sent: list[str] = []

    def answer(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if _first_text(params) == "Reply with OK.":
            return reply(text("OK"), model=SONNET)
        if not _helper_system(params["system"][0]["text"]):
            return reply(text('{"architect": null, "vendor": null, "sure": false}'), model=HAIKU)
        last = params["messages"][-1]["content"][-1].get("text", "")
        sent.append(last)
        if _steps(params) == 0:
            covered = {"pages": [{"page": p, "status": "same"} for p in (1, 2, 3)]}
            return reply(tool("c1", "set_coverage", covered), stop="tool_use", model=SONNET)
        # Never a CHECKLIST line for page 3; page 2 short with "none" left.
        return reply(
            text(
                "All the same.\n"
                "CHECKLIST p1: architect 2/2; notes 1/1; strings 1/1; not compared: none\n"
                "CHECKLIST p2: architect 1/3; notes 0/0; strings 1/1; not compared: none"
            ),
            model=SONNET,
        )

    harness = _one_job(tmp_path, answer, [reply(text("Done."))])

    outcome = harness.run()

    backs = [words for words in sent if words.startswith("Your checklist is not finished")]
    assert len(backs) == 1  # once, never twice
    assert "- p2: architect not all done" in backs[0] and "- p3: no CHECKLIST line" in backs[0]
    report = _lead_first(harness.provider)[-1]["text"]
    gaps = report.split("Checklist gaps, verify these first (2):\n", 1)[1].split("\n\n", 1)[0]
    assert gaps.splitlines() == [
        "- p2 (helper 1): architect not all done, and 'not compared' says none",
        "- p3 (helper 1): no CHECKLIST line",
    ]
    assert report.index("Checklist gaps") < report.index("To verify (")
    coverage = {e["page"]: e for e in _blocks(outcome, "coverage")[-1]["pages"]}
    assert coverage[3]["note"] == "checklist: no CHECKLIST line" and coverage[1]["note"] is None


def test_a_stopped_full_check_still_gets_codes_finish_and_its_files(tmp_path: Path) -> None:
    calls: list[str] = []
    string = {
        "page": 2,
        "view": "elevation",
        "side": "vendor",
        "parts": ['10"', '20"'],
        "printed_total": '31"',
        "box": {"page": 2, "x0": 100, "y0": 200, "x1": 220, "y1": 215},
    }
    other = {**string, "page": 3, "printed_total": '41"', "parts": ['20"', '20"']}
    reported = {
        "page": 3,
        "item": "Overall run",
        "category": "countertop",
        "architect": '40"',
        "vendor": '41"',
        "status": "different",
        "verified": False,
        "source": "vendor_arithmetic",
        "printed_total": '41"',
        "vendor_box": {"page": 3, "x0": 100, "y0": 200, "x1": 220, "y1": 215},
    }

    def answer(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        calls.append(params["model"])
        if _first_text(params) == "Reply with OK.":
            return reply(text("OK"), model=SONNET)
        if params["model"] == HAIKU:  # the helper's difference, read again before the lead
            return reply(text('{"architect": null, "vendor": "41\\"", "sure": true}'), model=HAIKU)
        if not _helper_system(params["system"][0]["text"]):
            raise AssertionError("no third read or critic runs on a stopped full check")
        if _steps(params) == 0:
            return reply(
                tool("s1", "record_string", string),
                tool("s2", "record_string", other),
                tool("r1", "report_finding", reported),
                stop="tool_use",
                model=SONNET,
            )
        return reply(
            text(
                "CHECKLIST p1: architect 0/0; notes 0/0; strings 0/0; not compared: none\n"
                "CHECKLIST p2: architect 0/0; notes 0/0; strings 1/1; not compared: none\n"
                "CHECKLIST p3: architect 0/0; notes 0/0; strings 1/1; not compared: none"
            ),
            model=SONNET,
        )

    # The reviewer presses Stop while the lead is writing.
    lead = [reply(text("Checking the leads"), cancel_after=4)]
    harness = _one_job(tmp_path, answer, lead)
    harness.engine.tools.results = lambda page: RECORDS

    outcome = harness.run()

    assert outcome.stopped == "cancelled"
    # The warm-up, the helper's two calls and the second read made before the stop; no critic.
    assert calls.count(SONNET) == 3 and calls.count(HAIKU) <= 1
    findings = _blocks(outcome, "finding")
    sums = [f for f in findings if f["created_by"] == "ledger"]
    assert len(sums) == 1 and sums[0]["page"] == 2 and sums[0]["vendor"] == '31"'
    assert sums[0]["sum_text"] and "30" in sums[0]["sum_text"]
    # The helper's own finding of page 3's string carries code's sum (no second finding for it).
    run = next(f for f in findings if f["item"] == "Overall run")
    assert run["sum_text"] and "40" in run["sum_text"]
    trust = _blocks(outcome, "trust")[-1]
    assert trust["recorded_results"] is not None and trust["timings"]["phases"]
    assert [f["name"] for f in harness.files] == [
        "Vendor set - GV cloud drawing.pdf",
        "Vendor set - AI check report.pdf",
        "Vendor set - Not compared.pdf",
        "Vendor set - Not compared.xlsx",
        "Vendor set - GV review package.zip",
    ]


def test_without_a_page_map_the_lead_plans_as_before(tmp_path: Path) -> None:
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(lead=[reply(text("Done."))])
    harness = Harness(tmp_path, provider, question=FULL_CHECK_INSTRUCTION)
    engine = harness.engine
    engine.tools.results = lambda page: RECORDS
    engine.settings = dataclasses.replace(engine.settings, mode="full_check")

    outcome = harness.run()

    texts = [block["text"] for block in _lead_first(provider)]
    assert texts[0] == FULL_CHECK_INSTRUCTION
    assert texts[-1].startswith("GV Review already recorded these differences")
    trust = _blocks(outcome, "trust")[-1]
    phases = [phase["phase"] for phase in trust["timings"]["phases"]]
    assert "helpers" not in phases and "planning" in phases and "lead" in phases


def test_timings_union_overlapping_work_and_charge_the_calling_thread() -> None:
    now = [0.0]
    timings = Timings(clock=lambda: now[0])
    timings.helper_started(1, "Pages 2-4", [2, 3, 4])
    with timings.phase("helpers", helper=1):
        now[0] = 1.0
        timings.charge(200_000)
        other = threading.Thread(target=lambda: timings.charge(50_000))
        other.start()
        other.join()
        now[0] = 3.0
    timings.helper_ended(1, None)
    with timings.phase("lead"):
        now[0] = 4.0
    with timings.phase("lead"):
        now[0] = 4.0
        now[0] = 5.5

    result = timings.as_dict(250_000)

    phases = {phase["phase"]: phase for phase in result["phases"]}
    assert phases["helpers"] == {
        "phase": "helpers",
        "label": "Helpers",
        "seconds": 3.0,
        "cost_usd": "0.20",
        "calls": 1,
    }
    assert phases["other"]["cost_usd"] == "0.05"  # a thread outside any phase
    assert phases["lead"]["seconds"] == 2.5
    assert result["helpers"] == [
        {
            "number": 1,
            "description": "Pages 2-4",
            "pages": [2, 3, 4],
            "calls": 1,
            "seconds": 3.0,
            "cost_usd": "0.20",
            "stopped": None,
        }
    ]
    assert result["total_seconds"] == 5.5 and result["cost_usd"] == "0.25"
    assert "helpers 3.0 s $0.20 (1)" in Timings.log_line(result)


def test_a_full_check_of_a_product_added_only_to_the_registry(
    tmp_path: Path, use_dummy_registry: Path
) -> None:
    """#1547: a made-up product in a test registry is fully checked with no code change: every
    helper's brief carries its checklist (after the shared items) and the instruction to read its
    product file, its findings keep its category, and the cloud drawing and report are made."""
    script = _Script(jobs=4, category=DUMMY)
    lead = [reply(text("Four widgets differ."))]
    harness = _full_check(tmp_path, script, lead, products=(DUMMY,))

    outcome = harness.run()

    assert len(script.briefs) == 4
    for brief in script.briefs:
        words = brief["messages"][0]["content"][0]["text"]
        assert "products/<id>.md for each product in focus" in words
        assert "FOR EACH PRODUCT IN FOCUS (dummy widgets), also on every page:" in words
        assert "- widget size: Widget width and height against the architect's." in words
        assert "PRODUCTS: dummy widgets;" in words
        assert "Countertops (category" not in words
    findings = _blocks(outcome, "finding")
    assert len(findings) == 4
    # Page 2's is GV Review's recorded countertop (its category stays); the helpers' are widgets.
    helpers = [finding for finding in findings if finding["item"].startswith("Top on page")]
    assert len(helpers) == 3 and {finding["category"] for finding in helpers} == {DUMMY}
    assert [f["category"] for f in findings if f["item"] == "Vanity top"] == ["countertop"]
    assert [f["name"] for f in harness.files] == [
        "Vendor set - GV cloud drawing.pdf",
        "Vendor set - AI check report.pdf",
        "Vendor set - Not compared.pdf",
        "Vendor set - Not compared.xlsx",
        "Vendor set - GV review package.zip",
    ]
