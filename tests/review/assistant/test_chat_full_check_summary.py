"""The full check always ends with a 5-6 line summary, its files even when stopped early, and a
live progress line (#1518).

Scripted replies and a real tiny PDF: no network, no paid call, no Docker.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_progress import FullCheckProgress, progress_numbers
from app.review.assistant.chat_provider import ProviderFailure
from app.review.assistant.chat_service import FULL_CHECK_INSTRUCTION
from app.review.assistant.chat_summary import (
    CODE_SUMMARY_NOTE,
    code_summary,
    needs_code_summary,
    top_issues,
)
from tests.review.assistant.chat_fakes import ScriptedProvider, events_named, reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS, Harness
from tests.review.assistant.test_chat_engine_findings import _full_check, _modules
from tests.review.assistant.test_chat_findings import finding_input, workspace_with_pdf

FILES = [
    "Vendor set - GV cloud drawing.pdf",
    "Vendor set - AI check report.pdf",
    "Vendor set - Not compared.pdf",
    "Vendor set - Not compared.xlsx",
    "Vendor set - GV review package.zip",
]


def _finding(
    item: str,
    status: str = "different",
    *,
    page: int = 1,
    difference: str | None = None,
    trust: str | None = "likely",
    vendor: str = '46"',
    architect: str = '37"',
) -> dict[str, Any]:
    return {
        "id": item,
        "page": page,
        "item": item,
        "status": status,
        "vendor": vendor,
        "architect": architect,
        "trust_level": trust,
        "inches": {"difference": difference} if difference is not None else None,
    }


def _code_proved(item: str, **more: Any) -> dict[str, Any]:
    """A Wrong code proved: both values printed at their spots, paired by code."""
    return {
        **_finding(item, **more),
        "trust_level": "confirmed",
        "witnesses": ["printed"],
        "paired_by": "code",
        "checks": {"printed_architect": "match", "printed_vendor": "match"},
        "inches": {"architect": "37", "vendor": "46", "difference": "9"},
    }


# ---- the instruction --------------------------------------------------------------------------


def test_the_full_check_instruction_asks_for_the_5_to_6_line_summary() -> None:
    words = FULL_CHECK_INSTRUCTION
    assert "2-4" not in words
    for rule in (
        "5-6 line plain-English summary",
        "no tables, no disclaimers",
        "pages, products, rounds",
        "overall verdict in one line",
        "top 3-5 issues by impact",
        "architect vs vendor",
        "could not be compared",
        "next step",
        "send the cloud drawing to the vendor",
        "stop early, still write this summary",
    ):
        assert rule in words, rule


# ---- code's summary ---------------------------------------------------------------------------


def test_top_issues_are_wrong_first_then_the_bigger_difference_then_the_more_trusted() -> None:
    findings = [
        _finding("right", "same"),
        _finding("check big", "check", difference="40"),
        _finding("wrong small", difference="1"),
        _finding("wrong big likely", difference="-17.5", trust="likely"),
        _finding("wrong big confirmed", difference="17.5", trust="confirmed"),
        _finding("to-check difference", difference="30", trust="to_check"),
        _finding("wrong no number", difference=None),
    ]

    order = [f["item"] for f in top_issues(findings, limit=10)]

    assert order == [
        "wrong big confirmed",
        "wrong big likely",
        "wrong small",
        "wrong no number",
        # Wrong (its status), but only an AI reading: after the checked Wrong ones (#1541).
        "to-check difference",
        "check big",
    ]
    assert len(top_issues(findings)) == 5


def test_code_writes_the_summary_only_when_the_ai_did_not() -> None:
    assert needs_code_summary("", None)
    assert needs_code_summary("  \n", None)
    assert needs_code_summary("Half an answer", "cancelled")
    assert not needs_code_summary("Checked 3 pages. All good.", None)


def _proved(finding: dict[str, Any]) -> dict[str, Any]:
    """A Wrong item code proved by itself (GV's rule check fails it): clouded."""
    return {
        **finding,
        "source": "rules",
        "rule_check_id": "C1",
        "witnesses": ["rule"],
        "checks": {"rule": "agrees"},
    }


def _summary(stopped: str | None, cap_hit: str | None = None) -> str:
    findings = [
        _proved(
            _finding(
                "Market area height",
                page=39,
                difference="17.5",
                vendor='107"',
                architect='89 1/2"',
            )
        ),
        _proved(
            _finding(
                "Market area height",
                page=40,
                difference="17.5",
                vendor='107"',
                architect='89 1/2"',
            )
        ),
        _proved(_finding("Buffet outlets", page=4, difference="9")),
        _finding("Bay sizes", "check", page=7),
        _finding("Backsplash", "same", page=2),
    ]
    return code_summary(
        findings,
        coverage={"total": 60, "not_opened": 0},
        page_count=60,
        products=("countertop", "cabinet"),
        stopped=stopped,
        cap_hit=cap_hit,
        cap_usd="7",
        budget_usd="40",
        turns=40,
        files=FILES,
    )


@pytest.mark.parametrize(
    ("stopped", "cap_hit", "words"),
    [
        ("cost_cap", "message", "Stopped early at the $7 limit"),
        ("cost_cap", "package", "used its $40 AI budget"),
        ("turn_limit", None, "after all 40 tool turns"),
        ("cancelled", None, "Stopped before the AI finished"),
        ("error", None, "Stopped by an error"),
        (None, None, None),
    ],
)
def test_the_code_summary_for_each_kind_of_stop(
    stopped: str | None, cap_hit: str | None, words: str | None
) -> None:
    summary = _summary(stopped, cap_hit)
    lines = summary.split("\n\n")[0].split("\n")

    assert 5 <= len(lines) <= 6
    assert lines[0] == "Checked 60 of 60 pages (countertops and cabinets, 1 round)."
    # The cloud drawing's own counts (#1619): Wrong = proved by code, Right, nothing to compare.
    assert lines[1] == "3 wrong (proved by code), 1 match, 1 with nothing to compare."
    # The same item and values on two pages is said once, with its pages.
    assert lines[2] == (
        'Biggest issues: Market area height: vendor 107", architect 89 1/2" (p39-40); '
        'Buffet outlets: vendor 46", architect 37" (p4).'
    )
    # Always one line on what could not be compared, by reason (#1619).
    assert lines[3] == (
        "1 item had nothing to compare (difference not proved by code: 1). See the Not-compared "
        "list (PDF / Excel)."
    )
    assert lines[-1] == "Next: send the cloud drawing to the vendor."
    for gone in ("your decision", "Your decision", "Vendor to confirm", "check before sending"):
        assert gone not in summary
    if words is None:
        assert len(lines) == 5
    else:
        assert words in lines[4]
    assert summary.endswith(CODE_SUMMARY_NOTE)
    assert "|" not in summary  # no tables


def test_a_summary_with_nothing_found_and_no_coverage_still_says_what_was_done() -> None:
    summary = code_summary(
        [], coverage=None, page_count=12, products=("countertop",), stopped="cancelled"
    )
    lines = summary.split("\n\n")[0].split("\n")
    assert lines[0].startswith("Checked items on 0 of 12 pages (countertops, 1 round)")
    assert lines[2] == "No wrong value was proved by code so far."
    assert lines[-1] == "Next: look over the report before signing off."


# ---- the engine: early stops still give the summary and the files ----------------------------


def _ends_with_code_summary(outcome: Any, harness: Harness) -> dict[str, Any]:
    summary = [b for b in outcome.blocks if b["type"] == "text" and b.get("by") == "code"]
    assert len(summary) == 1, outcome.blocks
    assert outcome.text.endswith(summary[0]["text"])
    assert summary[0]["text"].endswith(CODE_SUMMARY_NOTE)
    shown = [e for e in events_named(harness.events, "text_delta") if e.get("by") == "code"]
    assert shown and shown[-1]["text"] == summary[0]["text"]
    assert [f["name"] for f in harness.files] == FILES
    return summary[0]


def test_a_full_check_stopped_at_the_cap_writes_the_summary_and_makes_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(lead=[])
    harness = Harness(tmp_path, provider, message_cap=100_000)
    _full_check(harness)

    outcome = harness.run()

    assert provider.calls == [] and outcome.stopped == "cost_cap"
    summary = _ends_with_code_summary(outcome, harness)["text"]
    assert "Stopped early at the $0.10 limit" in summary
    assert "Vanity top" in summary  # the finding code made from GV Review's recorded result
    assert "I stopped before finishing" not in outcome.text  # the chat's stop note is replaced


def test_a_cancelled_full_check_with_findings_still_makes_its_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    cancel = threading.Event()
    cancel.set()
    provider = ScriptedProvider(lead=[])
    harness = Harness(tmp_path, provider, cancel=cancel)
    _full_check(harness)

    outcome = harness.run()

    assert outcome.stopped == "cancelled"
    assert "Stopped before the AI finished" in _ends_with_code_summary(outcome, harness)["text"]


def test_a_full_check_ended_by_an_error_still_writes_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    failure = ProviderFailure("The AI service is down (error 400).", status=400, retryable=False)
    harness = Harness(tmp_path, ScriptedProvider(lead=[failure, failure]))
    _full_check(harness)

    outcome = harness.run()

    assert outcome.stopped == "error"
    assert "Stopped by an error" in _ends_with_code_summary(outcome, harness)["text"]


def test_a_crash_in_a_full_check_still_ends_with_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    harness = Harness(tmp_path, ScriptedProvider(lead=[]))
    _full_check(harness)

    def crash() -> None:
        harness.engine._seed_recorded()
        raise RuntimeError("boom")

    monkeypatch.setattr(harness.engine, "_run", crash)

    outcome = harness.run()

    assert outcome.stopped == "error" and outcome.error
    assert "Stopped by an error" in _ends_with_code_summary(outcome, harness)["text"]


def test_a_full_check_that_ends_with_no_words_gets_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("d1", "drop_finding", {"finding_id": "F2", "reason": "centred"}),
                stop="tool_use",
            ),
            reply(text("")),
        ]
    )
    harness = Harness(tmp_path, provider)
    _full_check(harness)

    outcome = harness.run()

    assert outcome.stopped is None
    summary = _ends_with_code_summary(outcome, harness)["text"]
    assert "Stopped" not in summary and outcome.text == summary


def test_the_ai_s_own_summary_is_kept_and_code_writes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    own = "Checked 3 of 3 pages.\n1 wrong.\nVanity top p2.\nNothing to decide.\nSend it."
    harness = Harness(tmp_path, ScriptedProvider(lead=[reply(text(own))]))
    _full_check(harness)

    outcome = harness.run()

    # Its words are kept; the count it states is the cloud drawing's (#1554, #1619): GV Review
    # recorded two differences, code proved one; the other records no value and no spot, so it is
    # an internal note, never sent to the vendor (#1650). Code adds only the one line on what could
    # not be compared (#1619), after the AI's own summary.
    nothing = "Every item could be compared."
    assert outcome.text == f"{own}\n\n{nothing}"
    assert [b["text"] for b in outcome.blocks if b["type"] == "text"] == [own, nothing]
    assert [b["text"] for b in outcome.blocks if b.get("by") == "code"] == [nothing]
    assert [f["name"] for f in harness.files] == FILES


def test_a_chat_answer_never_gets_a_code_summary(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider(lead=[]), message_cap=100_000)

    outcome = harness.run()

    assert "this answer reached its $0.10 limit" in outcome.text
    assert not [b for b in outcome.blocks if b.get("by") == "code"]
    assert not events_named(harness.events, "progress")


# ---- progress ---------------------------------------------------------------------------------


def test_progress_numbers_count_pages_and_items_and_estimate_the_time_left() -> None:
    # Wrong so far counts only what code proved, as the files do (the pipeline audit: the live
    # line said 17 Wrong for a check whose files said 10): "e" is not proved, so Not compared.
    findings = [
        _code_proved("a"),
        _finding("b", "check"),
        _finding("c", "same"),
        _code_proved("d"),
        _finding("e"),
    ]
    numbers = progress_numbers(
        findings, {"total": 60, "not_opened": 48}, 60, "reading", elapsed=120.0
    )
    assert numbers == {
        "phase": "reading",
        "pages_done": 12,
        "pages_total": 60,
        "wrong_so_far": 2,
        "decision_so_far": 2,
        "right_so_far": 1,
        "eta_seconds": 480,
    }
    # Before any page is done there is no estimate; without coverage, pages with a finding count.
    assert progress_numbers([], None, 60, "reading", 5.0)["eta_seconds"] is None
    assert progress_numbers(findings, None, 60, "reading", 10.0)["pages_done"] == 1


def test_progress_is_sent_on_change_and_on_the_timer_only() -> None:
    now = [0.0]
    sent: list[dict[str, Any]] = []
    state: dict[str, Any] = {"findings": [], "coverage": None}
    progress = FullCheckProgress(
        lambda: (state["findings"], state["coverage"], 10),
        sent.append,
        clock=lambda: now[0],
        every=5.0,
    )
    progress.changed(force=True)
    progress.changed()  # nothing changed: nothing sent
    state["findings"] = [_code_proved("a", page=1)]
    now[0] = 10.0
    progress.changed()
    progress.phase("checking")

    assert [(s["phase"], s["wrong_so_far"], s["eta_seconds"]) for s in sent] == [
        ("reading", 0, None),
        ("reading", 1, 90),
        ("checking", 1, 90),
    ]


def test_a_full_check_streams_progress_and_stores_the_last_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    every_page = {"pages": [{"page": p, "status": "same"} for p in (1, 2, 3)]}
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input(item="Backsplash", page=1)),
                tool("c1", "set_coverage", every_page),
                stop="tool_use",
            ),
            reply(text("Done.\nOne.\nTwo.\nThree.\nFour.")),
        ]
    )
    harness = Harness(tmp_path, provider, message_cap=5 * DOLLARS)
    _full_check(harness)

    outcome = harness.run()

    events = events_named(harness.events, "progress")
    phases = [e["progress"]["phase"] for e in events]
    assert phases[0] == "reading" and phases[-1] == "writing" and "checking" in phases
    assert any(e["progress"]["pages_done"] == 3 for e in events)
    stored = [b for b in outcome.blocks if b["type"] == "progress"]
    assert len(stored) == 1 and stored[0]["progress"] == events[-1]["progress"]
    assert outcome.blocks.index(stored[0]) == events[-1]["index"]
    assert stored[0]["progress"]["pages_total"] == 3
