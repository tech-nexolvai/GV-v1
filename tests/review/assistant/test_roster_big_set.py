"""A big set finishes within its fixed price, or says exactly what is left and continues (#1821).

Scripted models, a synthetic page map and a real tiny PDF: no network, no paid call.

- **The finishing steps' money first.** What the devil's advocate and the escalation need is
  worked out from the red candidates (a blind pick each, the picks that run at once held at their
  worst case, a look-again per page group) and kept before the extra closer pass after the lead
  review spends: that pass spends only what is left, so the late step is never the one short.
- **Many items in one call.** `record_items` records findings, strings and readings together,
  each checked by its own tool exactly as one call of it is; a refused entry never stops the rest.
- **An honest end.** A check that ran out of money says which pages and how many red candidates
  are left and why; what it did stays; its stored answer says how Continue finishes it.
- **Continue.** It keeps the findings and the pages already compared, plans helpers only on the
  pages left, and its price is those pages' plus what the finishing steps need.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster
from app.review.assistant.chat_request import BY_SHORT
from app.review.assistant.chat_service import continuable
from app.review.assistant.chat_timings import Timings
from app.review.assistant.full_check_plan import FanOutPlan, HelperJob
from tests.review.assistant.chat_fakes import reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS, Harness
from tests.review.assistant.test_full_check_fanout import FILE, _box, _first_text
from tests.review.assistant.test_full_check_roster import (
    SONNET,
    _clean_ink,
    _Lazy,
    _Roster,
    _roster,
    _run,
)


def _candidate(harness: Harness, page: int, *, backed: bool) -> str:
    """A red candidate on `page`: code read both values at their spots (`backed`, so the devil's
    advocate gives it a blind pick), or nothing read yet (so the escalation looks again)."""
    engine = harness.engine
    finding, _notes = engine.book.report(
        {
            "page": page,
            "item": f"Top on page {page}",
            "category": "countertop",
            "architect": '37"',
            "vendor": '46"',
            "status": "different",
            "verified": False,
            "source": "architect",
            "arch_box": _box(page, 100),
            "vendor_box": _box(page, 300),
        },
        "helper:1",
    )
    read = "match" if backed else "not_found"
    assert engine.book.replace(
        {
            **finding,
            "checks": {"printed_architect": read, "printed_vendor": read},
            "inches": {"architect": "37", "vendor": "46", "difference": "9"},
            "trust_level": "to_check",
            "witnesses": [],
        }
    )
    return str(finding["id"])


# ---- the finishing steps' money first ------------------------------------------------------------


def test_the_finishing_steps_need_is_worked_out_from_the_red_candidates(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster(), price=15 * DOLLARS)
    run = _run(harness)
    engine = harness.engine
    engine._plan = FanOutPlan(
        (HelperJob(number=1, pages=(2, 3), file=FILE, lens="numbers"),
         HelperJob(number=2, pages=(4, 5), file=FILE, lens="numbers")),
        (),
    )  # fmt: skip
    for page in (2, 3, 4, 5, 2, 3, 4):
        _candidate(harness, page, backed=True)
    _candidate(harness, 2, backed=False)
    _candidate(harness, 5, backed=False)

    needs = run.finish_needs()

    strong = engine._price(
        BY_SHORT["opus"],
        full_check_roster._ADVOCATE_PROMPT_TOKENS,
        full_check_roster._ADVOCATE_MAX_TOKENS,
    )
    assert strong
    # Seven backed candidates: six picks at once, each held at the strong pick's worst case.
    assert needs["advocate"] >= 6 * strong
    # Two candidates code cannot read, in two page groups: one look-again each.
    assert needs["escalation"] == 2 * full_check_roster.SHARPER_GROUP_MICROS
    assert sum(needs.values()) <= 15 * DOLLARS * full_check_roster.FINISH_MOST_SHARE


def test_the_extra_pass_after_the_lead_spends_only_what_the_finishing_steps_leave(
    tmp_path: Path,
) -> None:
    """The order (#1821): the advocate's and the escalation's needs are kept before the closer
    pass after the lead review spends, so when that pass has spent all it was given, six strong
    blind picks still fit at once. With the shares alone (the old rule) they did not."""
    harness = _roster(tmp_path, _Roster(), price=15 * DOLLARS)
    run = _run(harness)
    engine = harness.engine
    for page in (2, 3, 4, 5, 2, 3, 4):
        _candidate(harness, page, backed=True)
    strong = engine._price(
        BY_SHORT["opus"],
        full_check_roster._ADVOCATE_PROMPT_TOKENS,
        full_check_roster._ADVOCATE_MAX_TOKENS,
    )
    six = 6 * (strong or 0)

    def after_the_extra_pass() -> None:
        # The closer pass after the lead review spent every cent it was given.
        engine._spent += run._room("lead")

    # The old rule: only the later steps' shares kept.
    run.needs = {}
    shares_only = run._room("lead")
    after_the_extra_pass()
    assert not run._phase_room("advocate", six)  # the late step would be the one short

    engine._spent = 0
    run._refresh_needs("lead")
    assert run._room("lead") < shares_only  # the extra pass gets less ...
    after_the_extra_pass()
    assert run._phase_room("advocate", six)  # ... and the blind picks all fit
    # The escalation's own need is still kept behind the advocate's picks.
    assert run._left() - six >= run._kept_after("advocate")


def test_the_page_helpers_never_spend_what_the_seeded_candidates_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1's ceiling keeps the finishing steps' needs for the red candidates already there (GV's
    recorded differences, chat proofs) as well as the later steps' shares."""
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Roster(), price=5 * DOLLARS)
    captured: dict[str, Any] = {}
    real = full_check_roster._Run._launched

    def launched(self: Any, agents: Any, **given: Any) -> Any:
        if not captured:
            captured.update(
                ceiling=self.engine.helper_ceiling, plan=self.plan, spent=self.engine.spent
            )
        return real(self, agents, **given)

    monkeypatch.setattr(full_check_roster._Run, "_launched", launched)
    monkeypatch.setattr(
        full_check_roster._Run,
        "finish_needs",
        lambda self: {"advocate": 2 * DOLLARS, "escalation": 0},
    )
    harness.run()

    plan = captured["plan"]
    kept = sum(
        max(plan.budgets[name], 2 * DOLLARS if name == "advocate" else 0)
        for name in full_check_roster.PROTECTED
    )
    assert kept > plan.protected()  # the need is more than the advocate's share
    assert captured["ceiling"] == captured["spent"] + plan.price - kept


# ---- many items in one call ----------------------------------------------------------------------


def _top(page: int, **change: Any) -> dict[str, Any]:
    return {
        "page": page,
        "item": f"Area 1: top on page {page}",
        "category": "countertop",
        "architect": '37"',
        "vendor": '46"',
        "status": "different",
        "verified": False,
        "source": "architect",
        "arch_box": _box(page, 100),
        "vendor_box": _box(page, 300),
        **change,
    }


def test_record_items_records_each_entry_as_its_own_tool_would(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    string = {"page": 3, "side": "vendor", "parts": ["12", "18"], "printed_total": "31"}

    result = engine._record_items(
        {
            "findings": [_top(2), {"page": 3, "item": "no status"}, _top(4)],
            "strings": [string],
            "readings": "not a list",
        },
        "helper:1",
    )

    words = result.content[0]["text"].splitlines()
    assert not result.is_error
    assert words[0].startswith("findings 1: Finding F")
    assert words[1].startswith("findings 2: Refused:")  # one entry refused, the rest recorded
    assert words[2].startswith("findings 3: Finding F")
    assert words[3].startswith("strings 1:") and "31" in words[3]  # code's sum came back
    assert words[4] == "readings: Refused: a list is needed."
    recorded = [f for f in engine.book.live() if f["item"].startswith("Area 1: top")]
    assert sorted(f["page"] for f in recorded) == [2, 4]
    assert all(f["created_by"] == "helper:1" for f in recorded)
    assert len(engine.reading_ledger.strings) == 1
    # Each entry counts under its own tool in the timings (what was recorded, not how).
    assert engine.timings.as_dict(0)["phases"][0]["tools"] == {
        "record_string": 1,
        "report_finding": 3,
    }
    # Nothing at all to record is refused plainly.
    assert engine._record_items({}, "helper:1").is_error
    assert engine._record_items({"findings": [{"page": 2}]}, "helper:1").is_error


class _Batching(_Roster):
    """Numbers helpers that record their pages' tops and their coverage in one record_items."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        drawn = brief.startswith("OBJECTIVE: the drawn things")
        if drawn or brief.startswith(("TAGS:", "PROVE:", "LEAD REVIEW")):
            return super().helper(params, brief)
        if brief.startswith(full_check_roster.GAP_SCHEMA_NOTE):
            return reply(text("Closed."), model=SONNET)
        self._seen("numbers", params)
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        if not any(m["role"] == "assistant" for m in params["messages"]):
            found = [_top(p, item=f"Top on page {p}", vendor_dim="D1", architect_dim="D2")
                     for p in pages]  # fmt: skip
            for entry in found:
                del entry["arch_box"], entry["vendor_box"], entry["architect"], entry["vendor"]
            covered = {"pages": [{"page": p, "status": "different"} for p in pages]}
            return reply(
                tool("b1", "record_items", {"findings": found}),
                tool("c1", "set_coverage", covered),
                stop="tool_use",
                model=SONNET,
            )
        lines = "\n".join(
            f"CHECKLIST p{p}: architect 1/1; notes 0/0; strings 0/0; not compared: none"
            for p in pages
        )
        return reply(text(f"Done.\n{lines}"), model=SONNET)


def test_a_helper_records_its_pages_in_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Batching()
    harness = _roster(tmp_path, script)
    numbers = script.params  # filled while it runs

    outcome = harness.run()

    tops = [
        b["finding"]
        for b in outcome.blocks
        if b["type"] == "finding" and b["finding"]["item"].startswith("Top on page")
    ]
    assert sorted(f["page"] for f in tops) == [2, 3, 4, 5]
    assert all(f["vendor"] == '46"' and f["architect"] == '37"' for f in tops)  # code's labels
    offered = {t["name"] for t in numbers["numbers"][0]["tools"]}
    assert {"record_items", "report_finding", "record_string"} <= offered  # none removed


def test_the_timings_add_up_each_phases_tokens_for_the_log_only() -> None:
    timings = Timings(clock=lambda: 0.0)
    with timings.phase("helpers"):
        timings.charge(1000)
        timings.tokens({"input_tokens": 1200, "output_tokens": 800,
                        "cache_read_input_tokens": 30_000, "cache_creation_input_tokens": 0})  # fmt: skip
        timings.tokens({"input_tokens": 300, "output_tokens": "x"})
    shown = timings.as_dict(1000)
    line = Timings.log_line(shown)
    assert "{tokens in 2k out 800 read 30k write 0}" in line
    # Never stored: the published timings stay as they were.
    assert "tokens" not in Timings.stored(shown)["phases"][0]


# ---- an honest end, and Continue -----------------------------------------------------------------


def _short_after_the_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    real_room = full_check_roster._Run._room

    def room(self: Any, phase: str) -> int:
        if phase == "lead" and self.lead_taken:
            return full_check_roster.MIN_GAP_RUN_MICROS - 1  # after the lead: really short
        return int(real_room(self, phase))

    monkeypatch.setattr(full_check_roster._Run, "_room", room)


def _resume(outcome: Any) -> Mapping[str, Any] | None:
    return next((b["resume"] for b in outcome.blocks if b.get("type") == "resume"), None)


def test_a_check_out_of_money_says_what_is_left_and_how_continue_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    _short_after_the_lead(monkeypatch)
    outcome = _roster(tmp_path, _Lazy()).run()

    assert "Not finished (out of money): pages 2-5 not compared" in outcome.text
    assert "Continue finishes the rest from this check's saved work" in outcome.text
    resume = _resume(outcome)
    assert resume is not None and resume["mode"] == "full_check"
    assert resume["products"] == ["countertop"]
    assert "compares only pages 2-5" in resume["redo"]
    # Its stored answer can be continued (the API and the service ask `continuable`).
    stored = SimpleNamespace(
        stopped=outcome.stopped, role="assistant", mode="full_check", blocks=outcome.blocks
    )
    assert continuable(stored)  # type: ignore[arg-type]


def test_a_finished_check_offers_no_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    outcome = _roster(tmp_path, _Roster()).run()
    assert _resume(outcome) is None
    assert "Not finished" not in outcome.text
    plain = SimpleNamespace(stopped="cancelled", role="assistant", mode="chat", blocks=[])
    assert not continuable(plain)  # type: ignore[arg-type]
    assert continuable(SimpleNamespace(stopped="interrupted", role="assistant", mode="chat",
                                       blocks=[]))  # type: ignore[arg-type]  # fmt: skip


def test_continue_keeps_a_cards_folded_copies_so_a_conflict_never_turns_red_again(
    tmp_path: Path,
) -> None:
    """One item, one card (review of #1916): a card Not compared because its copies disagree on
    the architect's size keeps that on Continue (its copies were folded away)."""
    from app.review.assistant.proof import proof_group

    harness = _roster(tmp_path, _Roster(), price=5 * DOLLARS)
    folded = {"copies": [{"finding_id": "F9", "group": "right"}], "conflict": ['36"', '40"']}
    earlier = {**_top(2, item="Area 1: top"), "id": "F1", "created_by": "helper:1"}
    harness.engine.carry_over([{**earlier, "same_item": folded}], None)
    kept = harness.engine.book.live()
    assert len(kept) == 1 and kept[0]["same_item"] == folded
    assert proof_group(kept[0]) == "not_compared"


def test_continue_compares_only_the_pages_left_and_keeps_what_was_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    # The earlier answer: pages 2 and 3 compared (one Wrong kept), pages 4 and 5 out of money.
    script = _Roster()
    harness = _roster(tmp_path, script, price=5 * DOLLARS)
    engine = harness.engine
    earlier = {**_top(2, item="Area 1: top kept from before"), "id": "F1", "created_by": "helper:1"}
    engine.carry_over(
        [earlier],
        {
            "pages": [
                {"page": 2, "status": "different", "note": None},
                {"page": 3, "status": "same", "note": None},
                {"page": 4, "status": "not_opened", "note": "not compared: out of money"},
                {"page": 5, "status": "same", "note": "not reached within the fixed price"},
            ]
        },
    )
    engine.continuing = True

    outcome = harness.run()

    briefs = [_first_text(p) for p in script.params["numbers"] + script.params["drawn"]]
    asked = {
        int(n)
        for brief in briefs
        for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")
    }
    assert asked == {4, 5}  # the pages left, never the ones already compared
    findings = [b["finding"] for b in outcome.blocks if b["type"] == "finding"]
    assert any(f["item"] == "Area 1: top kept from before" for f in findings)
    pages = {
        int(p["page"]): p
        for p in [b for b in outcome.blocks if b["type"] == "coverage"][-1]["coverage"]["pages"]
    }
    assert {pages[p]["status"] for p in (2, 3, 4, 5)} <= {"same", "different"}
    # Its price: the two pages left of four, plus the finishing steps' needs, under the cap.
    assert "Fixed price $" in outcome.text
    price = float(outcome.text.split("Fixed price $", 1)[1].split(":", 1)[0])
    assert 2.5 <= price <= 5.0
    assert _resume(outcome) is None


def test_carried_pages_are_done_only_when_compared_and_not_said_unreached() -> None:
    coverage = {
        "pages": [
            {"page": 2, "status": "different", "by": "earlier"},
            {"page": 3, "status": "same", "by": "earlier", "note": "not reached"},
            {"page": 4, "status": "not_opened", "by": "earlier", "note": "out of money"},
            {"page": 5, "status": "same", "by": "helper:1"},
            {"page": 6, "status": "not_comparable", "by": "earlier", "note": "brand prototype"},
            {"page": 7, "status": "same", "by": "earlier", "note": "not compared: out of money"},
        ]
    }
    assert full_check_roster.carried_done(coverage) == {2, 6}
    plan = FanOutPlan(
        (HelperJob(number=1, pages=(2, 3), file=FILE), HelperJob(number=2, pages=(6,), file=FILE)),
        ((1, "divider"),),
    )
    left = full_check_roster.remaining_plan(plan, {2, 6})
    assert [(job.number, job.pages) for job in left.jobs] == [(1, (3,))]
    assert left.skipped == ((1, "divider"),)
    assert json.dumps(left.pages) == "1"
