"""A roster full check on picked pages only (Anant, 2026-10-31: a fix is tested on the pages it
touches, one full run at the end).

Scripted models, a synthetic page map and a real tiny PDF: no network, no paid call, no Docker.

- The request: `pages` goes with a full check only, each page once.
- The plan: only the picked pages get helpers (the others are not even reference pages), and only
  their own skip notes stay; picked pages that hold no compared drawing stop before any paid step.
- The run: every finding, coverage line and recorded difference is on the picked pages; the price
  counts only them; the question shown names them; the scope is theirs, and the whole-set steps
  (resolver, escalation, spot guard) still run.
- Nothing outside the pick (the live run of 2026-10-31): code's whole-set checks (the vendor
  consistency check), a replayed chat proof or a stray helper never record an item, a coverage
  line or a set gap on another page, so no other page is "not compared", the check is not called
  incomplete for it and Continue never offers to compare it; the summary names the picked pages.
  A picked page that was really not compared is still said so, and a whole-set check is as before.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.review.assistant import full_check_roster, set_proofs, vendor_consistency
from app.review.assistant.chat_contract import ChatMessageIn
from app.review.assistant.chat_engine import ChatEngine
from app.review.assistant.chat_scope import question_scope
from app.review.assistant.chat_service import full_check_label
from app.review.assistant.full_check_plan import FanOutPlan, HelperJob
from app.review.assistant.product_scope import PriceRule
from app.review.assistant.sandbox import SandboxRefused
from tests.review.assistant.test_chat_engine import DOLLARS
from tests.review.assistant.test_full_check_roster import _clean_ink, _findings, _Roster, _roster

FILE = "vendor.pdf"


@pytest.fixture(autouse=True)
def _reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")


def _message(**fields: object) -> ChatMessageIn:
    return ChatMessageIn.model_validate(
        {"model": "claude-opus-5-5", "effort": "high", "thinking": True, **fields}
    )


def _pick(engine: Any, pages: tuple[int, ...]) -> None:
    """The harness's engine made a full check on `pages`, as `ChatEngine.__init__` makes one (the
    harness sets its settings after the engine is made): its scope, and the book's hard filter."""
    engine.settings = dataclasses.replace(engine.settings, pages=pages)
    engine._asked_scope = question_scope("", mode="full_check", picked=pages)
    engine.book.scope_pages = frozenset(pages)
    engine.book.picked_pages = frozenset(pages)


def _string(page: int) -> dict[str, Any]:
    """A vendor string whose parts do not add up to its overall, on `page` (made-up values)."""
    return {
        "page": page,
        "item": f"Area: dimension string (across) p{page}",
        "category": "countertop",
        "architect": '30"',
        "vendor": '30 1/8"',
        "status": "different",
        "why": "The vendor's parts don't add up.",
        "verified": False,
        "source": "vendor_arithmetic",
        "parts": ['10"', '10"', '10"'],
        "printed_total": '30 1/8"',
        "vendor_box": {"page": page, "x0": 300, "y0": 200, "x1": 360, "y1": 216, "file": FILE},
        "sum_text": '10" + 10" + 10" = 30" vs 30 1/8" printed (off 1/8")',
    }


def _consistency_on(monkeypatch: pytest.MonkeyPatch, pages: Sequence[int]) -> None:
    """The vendor consistency check (code, over the whole set) finds a string that does not add up
    and a sheet gap on each of `pages`."""

    def found(*_args: object, **_kwargs: object) -> vendor_consistency.ConsistencyResult:
        result = vendor_consistency.ConsistencyResult()
        result.findings = [_string(page) for page in pages]
        result.unknowns = [
            {"page": page, "what": f"Vendor page {page}: numbers drawn as pen strokes",
             "why": "unreadable", "needs": "a person reads them", "scope": "page"}
            for page in pages
        ]  # fmt: skip
        return result

    monkeypatch.setattr(vendor_consistency, "check_pack", found)


def _chat_string(engine: Any, page: int, side: str = "vendor") -> None:
    """A string a chat question recorded on `page`, kept for the set's next full check: its parts
    do not add up to its overall (made-up values)."""
    raw = {"page": page, "side": side, "parts": ["12", "15"], "printed_total": "28"}
    assert set_proofs.keep_ledger_entry(engine.tools.workspace.set_pack, "record_string", raw)


def _last(outcome: Any, kind: str) -> Any:
    return [block[kind] for block in outcome.blocks if block["type"] == kind][-1]


class _Priced:
    """The price rule, with the page counts it was asked for."""

    def __init__(self) -> None:
        self.rule = PriceRule()
        self.pages: list[int] = []

    def price(self, pages: int, bound: int | None = None) -> int:
        self.pages.append(pages)
        return self.rule.price(pages, bound)


def test_pages_go_with_a_full_check_only_each_page_once() -> None:
    assert _message(mode="full_check", pages=[4, 5, 12]).pages == [4, 5, 12]
    assert _message(mode="full_check").pages is None
    with pytest.raises(ValidationError, match="full check only"):
        _message(text="check page 4", pages=[4])
    with pytest.raises(ValidationError, match="at most once"):
        _message(mode="full_check", pages=[4, 4])
    with pytest.raises(ValidationError):
        _message(mode="full_check", pages=[])
    with pytest.raises(ValidationError):
        _message(mode="full_check", pages=[0])


def test_the_question_and_the_scope_name_the_picked_pages() -> None:
    assert full_check_label(["countertop"], "high") == "Full check · Countertops · High effort"
    assert full_check_label(["countertop"], "high", (12, 4, 5)).endswith(" · Pages 4-5, 12")
    assert full_check_label(["countertop"], "high", (7,)).endswith(" · Page 7")
    scope = question_scope("", mode="full_check", picked=(5, 4))
    assert (scope["kind"], scope["why"], scope["pages"]) == ("pages", "full_check", [4, 5])
    assert question_scope("", mode="full_check")["kind"] == "whole_set"


def test_the_plan_keeps_only_the_picked_pages() -> None:
    plan = FanOutPlan(
        (
            HelperJob(1, (2, 3), FILE, ((FILE, 9),), ("Area 1",)),
            HelperJob(2, (4, 5), FILE, (), ("Area 2",)),
        ),
        ((1, "divider"), (6, "divider")),
    )

    cut = full_check_roster.picked_plan(plan, {3, 6})

    assert [(job.number, job.pages) for job in cut.jobs] == [(1, (3,))]
    # A page left out is no part of the check: not a reference page either.
    assert cut.jobs[0].reference == ((FILE, 9),)
    assert cut.skipped == ((6, "divider"),)
    assert full_check_roster.picked_plan(plan, {1}).jobs == ()


def test_a_full_check_on_picked_pages_compares_and_prices_only_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script)
    engine = harness.engine
    priced = _Priced()
    _pick(engine, (3, 6))
    engine.settings = dataclasses.replace(engine.settings, price_rule=priced)  # type: ignore[arg-type]
    resolved: list[bool] = []
    monkeypatch.setattr(
        type(engine), "_resolve_decisions", lambda self: resolved.append(self._whole_set_work())
    )

    outcome = harness.run()

    assert outcome.stopped is None
    assert engine._plan is not None
    assert {page for job in engine._plan.jobs for page in job.pages} == {3}
    findings = _findings(outcome)
    # The scripted tag reader always answers with its page-2 table, whatever pages it was given:
    # its rows on page 2 are not this check's either.
    assert {f["page"] for f in findings} == {3}
    # GV's recorded difference on page 2 is not this check's.
    assert not any(f.get("record_id") for f in findings)
    coverage = _last(outcome, "coverage")
    assert [entry["page"] for entry in coverage["pages"]] == [3, 6]
    scope = _last(outcome, "scope")
    assert (scope["kind"], scope["pages"]) == ("pages", [3, 6])
    # The whole-set steps still run, on these pages.
    assert resolved == [True]
    # One content page priced: page 6 is a divider.
    assert set(priced.pages) == {1}


def test_picked_pages_with_no_compared_drawing_stop_before_any_paid_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script)
    engine = harness.engine
    engine.settings = dataclasses.replace(engine.settings, pages=(1, 6))

    outcome = harness.run()

    assert outcome.text.startswith("Nothing was compared: pages 1, 6 hold no vendor drawing")
    assert outcome.stopped == "cancelled"
    assert not any(script.calls.get(kind) for kind in ("numbers", "drawn", "tags", "advocate"))


def test_a_picked_checks_book_records_nothing_on_another_page(tmp_path: Path) -> None:
    """The hard filter, set when the engine is made (a Continue's carried items go through it
    before the check starts): code's item, a helper's item, a page move and a coverage line on a
    page not picked are never recorded; the picked pages' are."""
    harness = _roster(tmp_path, _Roster())
    made = harness.engine
    assert made.book.picked_pages is None  # a whole-set check: every page
    engine = ChatEngine(
        settings=dataclasses.replace(made.settings, pages=(3, 6)),
        provider=made.provider,
        tools=made.tools,
        ledger=made.ledger,
        emit=made._emit,
        history=(),
        user_content=made.user_content,
        package_spent=made.package_spent,
        read_picture=made.read_picture,
        cancel=made.cancel,
    )
    book = engine.book
    assert book.picked_pages == frozenset({3, 6})
    assert book.add_code_finding(_string(5), created_by="consistency") is None
    kept = book.add_code_finding(_string(3), created_by="consistency")
    assert kept is not None and kept["page"] == 3
    helper = {"page": 5, "item": "Top", "category": "countertop", "architect": '37"',
              "vendor": '46"', "status": "different", "verified": False, "source": "architect"}  # fmt: skip
    with pytest.raises(SandboxRefused, match="page 5 is not one of the pages this check"):
        book.report(helper, "helper:1")
    with pytest.raises(SandboxRefused, match="page 5 is not one of the pages this check"):
        book.update({"finding_id": kept["id"], "page": 5})
    book.set_coverage(
        {"pages": [{"page": 3, "status": "same"}, {"page": 5, "status": "same"}]}, "helper:1"
    )
    assert set(book.coverage) == {3}
    # The chat proofs replayed: the vendor's on the picked pages; the architect's all stay.
    _chat_string(engine, 3)
    _chat_string(engine, 5)
    _chat_string(engine, 5, side="architect")
    assert engine.replay_chat_proofs() == 2
    assert sorted((s.page, s.side) for s in engine.reading_ledger.strings) == [
        (3, "vendor"),
        (5, "architect"),
    ]


def test_code_items_on_pages_not_picked_never_widen_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live run of 2026-10-31: the vendor consistency check (code, over the whole set) found a
    string that does not add up on a page not picked; that page widened the check's scope and was
    then "not opened", the check "incomplete, not for sending" and Continue offered to compare
    it, and the first line counted pages between the picked ones."""
    _clean_ink(monkeypatch)
    _consistency_on(monkeypatch, [3, 5])
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    _pick(engine, (2, 3))
    _chat_string(engine, 5)  # a chat question's string on a page not picked

    outcome = harness.run()

    assert outcome.stopped is None
    findings = _findings(outcome)
    assert {f["page"] for f in findings} == {2, 3}
    # The picked page's own string from the same check is kept: only the other page's is not.
    assert [f["page"] for f in findings if f["created_by"] == "consistency"] == [3]
    scope = _last(outcome, "scope")
    assert (scope["pages"], scope["label"]) == ([2, 3], "pages 2-3")
    assert [entry["page"] for entry in _last(outcome, "coverage")["pages"]] == [2, 3]
    assert {u["page"] for u in _last(outcome, "unknowns")} == {2, 3}
    trust = _last(outcome, "trust")
    assert trust["coverage"]["counts"]["total"] == 2
    assert not trust["coverage"].get("pages_not_opened")
    assert not any("not opened" in line.lower() for line in trust.get("not_checked") or [])
    # The chat question's string on page 5 never reached this check's ledger.
    assert not ((trust.get("ledger_entries") or {}).get("strings") or [])
    assert not any(block["type"] == "resume" for block in outcome.blocks)
    text = outcome.text
    assert text.startswith("Checked the 2 picked pages (pages 2-3; countertops, 1 round).")
    assert "Check incomplete" not in text and "Not for sending" not in text
    assert "Not finished" not in text and "page 5" not in text and "p5" not in text


def test_a_picked_page_not_compared_is_still_said_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real gap on a picked page (its price too small to reach it) is incomplete as before: said
    on the first line, not for sending, and Continue compares it; still nothing of another page."""
    _clean_ink(monkeypatch)
    _consistency_on(monkeypatch, [5])
    harness = _roster(tmp_path, _Roster(), price=DOLLARS // 50)  # 2 cents
    _pick(harness.engine, (2, 3))

    outcome = harness.run()

    assert outcome.stopped == "cost_cap"
    text = outcome.text
    assert text.startswith(
        "Checked 0 of the 2 picked pages (pages 2-3; countertops, 1 round); 2 pages were not "
        "compared (not finished: out of money)."
    )
    assert "**Check incomplete: " in text
    assert "Not finished (out of money): pages 2-3 not compared" in text
    assert "compares only pages 2-3," in _last(outcome, "resume")["redo"]
    assert [entry["page"] for entry in _last(outcome, "coverage")["pages"]] == [2, 3]
    assert "page 5" not in text and "p5" not in text


def test_a_whole_set_check_keeps_codes_items_on_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pages picked: the same items on every page, counted against the whole set, as before."""
    _clean_ink(monkeypatch)
    _consistency_on(monkeypatch, [3, 5])
    harness = _roster(tmp_path, _Roster())
    _chat_string(harness.engine, 5)

    outcome = harness.run()

    findings = _findings(outcome)
    assert sorted(f["page"] for f in findings if f["created_by"] == "consistency") == [3, 5]
    strings = _last(outcome, "trust")["ledger_entries"]["strings"]
    assert [(entry["page"], entry["by"]) for entry in strings] == [(5, "chat")]
    assert _last(outcome, "scope")["kind"] == "whole_set"
    assert [entry["page"] for entry in _last(outcome, "coverage")["pages"]] == [1, 2, 3, 4, 5, 6]
    assert 5 in {u["page"] for u in _last(outcome, "unknowns")}
    assert outcome.text.startswith("Checked 4 of 6 pages (countertops, 1 round)")
