"""A roster full check on picked pages only (Anant, 2026-10-31: a fix is tested on the pages it
touches, one full run at the end).

Scripted models, a synthetic page map and a real tiny PDF: no network, no paid call, no Docker.

- The request: `pages` goes with a full check only, each page once.
- The plan: only the picked pages get helpers (the others are not even reference pages), and only
  their own skip notes stay; picked pages that hold no compared drawing stop before any paid step.
- The run: every finding, coverage line and recorded difference is on the picked pages; the price
  counts only them; the question shown names them; the scope is theirs, and the whole-set steps
  (resolver, escalation, spot guard) still run.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.review.assistant import full_check_roster
from app.review.assistant.chat_contract import ChatMessageIn
from app.review.assistant.chat_scope import question_scope
from app.review.assistant.chat_service import full_check_label
from app.review.assistant.full_check_plan import FanOutPlan, HelperJob
from app.review.assistant.product_scope import PriceRule
from tests.review.assistant.test_full_check_roster import _clean_ink, _findings, _Roster, _roster

FILE = "vendor.pdf"


@pytest.fixture(autouse=True)
def _reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")


def _message(**fields: object) -> ChatMessageIn:
    return ChatMessageIn.model_validate(
        {"model": "claude-opus-5-5", "effort": "high", "thinking": True, **fields}
    )


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
    engine.settings = dataclasses.replace(engine.settings, pages=(3, 6), price_rule=priced)  # type: ignore[arg-type]
    engine._asked_scope = question_scope("", mode="full_check", picked=(3, 6))
    resolved: list[bool] = []
    monkeypatch.setattr(
        type(engine), "_resolve_decisions", lambda self: resolved.append(self._whole_set_work())
    )

    outcome = harness.run()

    assert outcome.stopped is None
    assert engine._plan is not None
    assert {page for job in engine._plan.jobs for page in job.pages} == {3}
    findings = _findings(outcome)
    # (The scripted tag reader always answers with its page-2 table, whatever pages it was given.)
    assert {f["page"] for f in findings if not f["item"].endswith(": tag")} == {3}
    # GV's recorded difference on page 2 is not this check's.
    assert not any(f.get("record_id") for f in findings)
    coverage = [b["coverage"] for b in outcome.blocks if b["type"] == "coverage"][-1]
    assert {entry["page"] for entry in coverage["pages"] if entry["by"]} == {3, 6}
    assert {entry["page"] for entry in coverage["pages"]} <= {2, 3, 6}  # 2: the tag table's
    scope = [b["scope"] for b in outcome.blocks if b["type"] == "scope"][-1]
    assert scope["kind"] == "pages" and set(scope["pages"]) <= {2, 3, 6}
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
