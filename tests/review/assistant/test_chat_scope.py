"""A chat answer's scope (#1520): the whole set, or the pages / area the question names.

Asserted ($0: scripted replies, a tiny made-up PDF, no network, no paid call):
- the scope table: pages ("page 8", "p.8", "pages 27-28"), an area from the page map, whole-set
  wording, nothing named, a full check, and widening when the answer works on many more pages;
- a one-page question gets no whole-set send-back, no recorded differences from other pages, a
  coverage map of its own pages only, and trust counts of its own findings only;
- a whole-set question is unchanged (every page in the coverage map, the send-back runs);
- the text handed to the agent loses the team's ticket numbers.

All names and values here are made up.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_findings import COVERAGE_CHECK_PREFIX
from app.review.assistant.chat_scope import (
    WIDEN_PAGES,
    area_pages,
    in_scope,
    question_scope,
    scope_note,
    scoped_recorded,
    widened,
)
from app.review.assistant.chat_store import _scope_of
from app.review.assistant.chat_tools import LEAD_SYSTEM_PROMPT
from app.review.assistant.chat_trust import Recorded, recorded_from, rescored
from app.review.assistant.internal_refs import without_internal_refs, without_ticket_refs
from app.review.assistant.skills_export import SKILL_SOURCE
from tests.review.assistant.chat_fakes import ScriptedProvider, events_named, reply, text, tool
from tests.review.assistant.test_chat_engine import Harness
from tests.review.assistant.test_chat_engine_findings import RECORDS, _modules, _sent
from tests.review.assistant.test_chat_findings import finding_input, workspace_with_pdf

PAGE_MAP: dict[str, Any] = {
    "pages": [
        {"file": "Vendor set.pdf", "page": 1, "area": None, "section": "1"},
        {"file": "Vendor set.pdf", "page": 2, "area": "2. Pantry", "section": "1. WALL"},
        {"file": "Vendor set.pdf", "page": 3, "area": "2. Pantry", "section": "1. WALL"},
        {"file": "Vendor set.pdf", "page": 4, "area": "3.Snack counter", "section": "1. WALL"},
        {"file": "Vendor set.pdf", "page": 5, "area": "7. Pantry island", "section": "4. ISLAND"},
        {"file": "Architect.pdf", "page": 1, "area": "9. Lobby desk", "section": None},
    ]
}


# ---- the scope table ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "kind", "pages", "area", "why"),
    [
        ("Do the fillers on page 8 meet GV's filler rule?", "pages", [8], None, "pages_named"),
        ("What is the depth on p.8?", "pages", [8], None, "pages_named"),
        ("Compare pages 27-28 with the architect", "pages", [27, 28], None, "pages_named"),
        ("Check sheets 3 and 5", "pages", [3, 5], None, "pages_named"),
        ("Is everything on page 4 right?", "pages", [4], None, "pages_named"),
        (
            "Do the fillers on the snack counter (page 4) meet the rule?",
            "pages",
            [4],
            "Snack counter",
            "pages_named",
        ),
        ("Is the pantry countertop right?", "area", [2, 3], "Pantry", "area_named"),
        ("Check the Pantry Island fillers", "area", [5], "Pantry island", "area_named"),
        ("Which is wrong?", "whole_set", [], None, "nothing_named"),
        ("Are all the fillers within GV's limits?", "whole_set", [], None, "nothing_named"),
        ("Check the whole set", "whole_set", [], None, "whole_set_words"),
        ("Compare page 4 and every other page", "whole_set", [], None, "whole_set_words"),
        ("Check all pages for filler widths", "whole_set", [], None, "whole_set_words"),
        ("Go through the entire package", "whole_set", [], None, "whole_set_words"),
        ("What about the lobby desk?", "whole_set", [], None, "nothing_named"),
        ("Is page 99 right?", "whole_set", [], None, "nothing_named"),
    ],
)
def test_the_scope_of_a_question(
    question: str, kind: str, pages: list[int], area: str | None, why: str
) -> None:
    scope = question_scope(question, page_map=PAGE_MAP, page_count=60, main_file="Vendor set.pdf")
    assert (scope["kind"], scope["pages"], scope["area"], scope["why"]) == (
        kind,
        pages,
        area,
        why,
    )


def test_a_full_check_is_always_the_whole_set() -> None:
    scope = question_scope("page 8", mode="full_check", page_map=PAGE_MAP, page_count=60)
    assert scope == {
        "kind": "whole_set",
        "pages": [],
        "area": None,
        "why": "full_check",
        "label": "the whole set",
    }


def test_areas_come_from_the_vendors_file_with_their_numbering_taken_off() -> None:
    areas = area_pages(PAGE_MAP, "Vendor set.pdf")
    assert areas == {
        "pantry": ("Pantry", [2, 3]),
        "snack counter": ("Snack counter", [4]),
        "pantry island": ("Pantry island", [5]),
    }
    assert "lobby desk" in area_pages(PAGE_MAP)  # no main file known: every file's areas
    assert area_pages(None) == {}


def test_the_label_says_the_scope_in_plain_words() -> None:
    named = question_scope("fillers on the snack counter (page 4)?", page_map=PAGE_MAP)
    assert named["label"] == "page 4 (Snack counter)"
    assert question_scope("the pantry?", page_map=PAGE_MAP)["label"] == "Pantry (pages 2-3)"
    note = scope_note(named)
    assert note is not None and note.startswith("The app scoped this question to page 4")
    assert scope_note(question_scope("Which is wrong?")) is None


def test_the_pages_worked_on_join_the_scope_until_there_are_too_many() -> None:
    scope = question_scope("page 8?")
    assert widened(scope, [8, 9])["pages"] == [8, 9]
    assert widened(scope, [8, 9])["kind"] == "pages"
    near = list(range(9, 9 + WIDEN_PAGES))
    assert widened(scope, near)["kind"] == "pages"
    wide = widened(scope, list(range(9, 10 + WIDEN_PAGES)))
    assert (wide["kind"], wide["why"], wide["pages"]) == ("whole_set", "wide_work", [])
    whole = question_scope("Which is wrong?")
    assert widened(whole, range(1, 60)) == whole
    assert in_scope(scope, 8) and not in_scope(scope, 23) and in_scope(scope, None)
    assert in_scope(whole, 23)


def test_recorded_results_are_kept_to_the_scopes_pages() -> None:
    recorded = recorded_from(RECORDS)
    assert {item["page"] for item in recorded.differences} == {2, 3}
    narrow = scoped_recorded(recorded, frozenset({2}))
    assert [item["record_id"] for item in narrow.differences] == ["c1"]
    assert narrow.results == recorded.results  # a rules finding is still matched by its id
    assert scoped_recorded(recorded, None) is recorded
    unreadable = Recorded(readable=False)
    assert scoped_recorded(unreadable, frozenset({2})) is unreadable


# ---- the engine --------------------------------------------------------------------------------


def _narrow(tmp_path: Path, provider: ScriptedProvider, pages: int = 3) -> Harness:
    workspace_with_pdf(tmp_path, pages)
    harness = Harness(tmp_path, provider, question="Do the fillers on page 2 meet GV's rule?")
    harness.engine.tools.results = lambda page: RECORDS
    return harness


def test_a_one_page_question_gets_no_whole_set_send_back_or_other_pages_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                # A recorded difference of another page the lead brought in: not this question's.
                tool(
                    "r2",
                    "report_finding",
                    finding_input(page=3, vs_page=None, record_id="ai-review:p3:sink centre"),
                ),
                tool("c1", "set_coverage", {"pages": [{"page": 2, "status": "different"}]}),
                stop="tool_use",
            ),
            reply(text("One filler on page 2 is too wide.")),
        ]
    )
    harness = _narrow(tmp_path, provider)

    outcome = harness.run()

    # No send-back: no "pages not accounted for", no "recorded differences left out".
    assert len(provider.lead_calls) == 2
    first = _sent(provider.lead_calls[0])["messages"][-1]["content"]
    assert first[-1]["text"].startswith("The app scoped this question to page 2.")
    assert not any(
        COVERAGE_CHECK_PREFIX in block.get("text", "")
        for message in _sent(provider.lead_calls[1])["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    )
    # The scope: sent at once, stored as a block, and in the trust report.
    assert events_named(harness.events, "scope")[0]["scope"]["pages"] == [2]
    scope = next(b["scope"] for b in outcome.blocks if b["type"] == "scope")
    assert (scope["kind"], scope["pages"], scope["why"]) == ("pages", [2], "pages_named")
    # The coverage map lists the scope's page only: nothing "not opened" for the other pages.
    coverage = next(b["coverage"] for b in outcome.blocks if b["type"] == "coverage")
    assert [entry["page"] for entry in coverage["pages"]] == [2]
    assert coverage["counts"]["total"] == 1 and coverage["counts"]["not_opened"] == 0
    trust = next(b["trust"] for b in outcome.blocks if b["type"] == "trust")
    assert trust["scope"] == scope
    # Trust counts the in-scope finding only; GV's recorded results of page 2 only.
    assert trust["counts"]["total"] == 1
    assert trust["gv_checks"]["recorded"] == 1
    assert [item["record_id"] for item in trust["gv_checks"]["left_out"]] == ["c1"]
    assert trust["coverage"]["pages_not_opened"] == []
    assert trust["recorded_results"] is None
    unknowns = [u for b in outcome.blocks if b["type"] == "unknowns" for u in b["unknowns"]]
    assert all(unknown["page"] in (2, None) for unknown in unknowns)
    accuracy = next(b["accuracy"] for b in outcome.blocks if b["type"] == "accuracy")
    assert accuracy["coverage_factor"] is None
    # A reload reads the same scope, and the score is not re-scored with a page penalty.
    assert _scope_of(outcome.blocks) == scope
    again = rescored(accuracy, outcome.blocks)
    assert again is not None and again["score"] == accuracy["score"]


def test_a_full_check_is_not_seeded_on_a_one_page_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    provider = ScriptedProvider(lead=[reply(text("The fillers on page 2 are within the rule."))])
    harness = _narrow(tmp_path, provider)

    outcome = harness.run()

    assert len(provider.lead_calls) == 1
    assert not [b for b in outcome.blocks if b["type"] in ("finding", "coverage")]
    sent = json.dumps(_sent(provider.lead_calls[0])["messages"])
    assert "already recorded these differences" not in sent
    assert len(events_named(harness.events, "scope")) == 1  # unchanged: sent once


def test_working_on_many_more_pages_makes_it_whole_set_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    pages = 3 + WIDEN_PAGES + 1
    every_page = {"pages": [{"page": p, "status": "same"} for p in range(1, pages + 1)]}
    provider = ScriptedProvider(
        lead=[
            reply(tool("c1", "set_coverage", every_page), stop="tool_use"),
            reply(text("All pages are the same.")),
            reply(text("All the same.")),
        ]
    )
    harness = _narrow(tmp_path, provider, pages=pages)

    outcome = harness.run()

    # Whole-set work now: the recorded differences left out send the lead back once.
    assert len(provider.lead_calls) == 3
    note = _sent(provider.lead_calls[2])["messages"][-1]["content"][-1]["text"]
    assert "c1 (page 2: Vanity top)" in note
    scopes = events_named(harness.events, "scope")
    assert [s["scope"]["kind"] for s in scopes] == ["pages", "whole_set"]
    stored = next(b["scope"] for b in outcome.blocks if b["type"] == "scope")
    assert (stored["kind"], stored["why"]) == ("whole_set", "wide_work")
    coverage = next(b["coverage"] for b in outcome.blocks if b["type"] == "coverage")
    assert coverage["counts"]["total"] == pages


def test_a_whole_set_question_keeps_every_page_and_the_send_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("c1", "set_coverage", {"pages": [{"page": 1, "status": "same"}]}),
                stop="tool_use",
            ),
            reply(text("Page 1 is the same.")),
            reply(text("Page 1 is the same; the rest not opened.")),
        ]
    )
    harness = Harness(tmp_path, provider, question="Which items are wrong?")
    harness.engine.tools.results = lambda page: RECORDS

    outcome = harness.run()

    assert len(provider.lead_calls) == 3
    note = _sent(provider.lead_calls[2])["messages"][-1]["content"][-1]["text"]
    assert note.startswith(COVERAGE_CHECK_PREFIX) and "pages 2-3" in note
    assert "c1 (page 2: Vanity top)" in note
    scope = next(b["scope"] for b in outcome.blocks if b["type"] == "scope")
    assert (scope["kind"], scope["why"]) == ("whole_set", "nothing_named")
    coverage = next(b["coverage"] for b in outcome.blocks if b["type"] == "coverage")
    assert coverage["counts"] == {
        "total": 3,
        "same": 1,
        "different": 0,
        "not_comparable": 0,
        "not_opened": 2,
    }
    trust = next(b["trust"] for b in outcome.blocks if b["type"] == "trust")
    assert trust["gv_checks"]["recorded"] == 2


# ---- no internal references ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "careful", "everything"),
    [
        (
            'The examples use 3" - awaiting his confirmation (#674).',
            'The examples use 3" - the client has not yet confirmed which is right.',
            'The examples use 3" - the client has not yet confirmed which is right.',
        ),
        ("one wall end takes one (1.2.0, #1138).", "one wall end takes one (1.2.0).", None),
        ("Fixed in (PR #809) today; see issue #12.", "Fixed in today; see.", None),
        ("NOT_FOUND (#67, the rulebook), so", "NOT_FOUND (the rulebook), so", None),
        (
            "Unit #2 of the typical units",
            "Unit #2 of the typical units",
            "Unit of the typical units",
        ),
    ],
)
def test_ticket_numbers_are_taken_out(given: str, careful: str, everything: str | None) -> None:
    assert without_ticket_refs(given) == careful
    assert without_internal_refs(given) == (everything or careful)
    assert not re.search(r"#\d", without_internal_refs(given))


def test_the_agent_is_told_never_to_cite_ticket_numbers() -> None:
    assert "Never cite internal ticket numbers." in LEAD_SYSTEM_PROMPT
    # The skill text handed to the agent names no ticket either (a "#2" unit label is fine).
    assert not re.search(r"#\d{2,}", SKILL_SOURCE.read_text(encoding="utf-8"))
