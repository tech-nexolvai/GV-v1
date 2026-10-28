"""The full check's price scales with the set (owner, 2026-10-21, #1823).

$2 + $0.35 per content page in scope, at least $5, never above the drawing set's budget left; the
price is the full check's own cap (the chat's message cap never cuts it). Made-up sets, scripted
models: no network, no paid call.

Also the roster's numbers page helpers' two settings (`GV_ASSISTANT_NUMBERS_HELPER_EFFORT`,
`GV_ASSISTANT_NUMBERS_HELPER_PAGES`): the defaults give today's plan exactly, and the settings
change only the numbers helpers.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.review.assistant import full_check_roster
from app.review.assistant.full_check_plan import (
    DRAWN_JOB_CAP_MICROS,
    plan_jobs,
    scoped_plan,
    with_lenses,
)
from app.review.assistant.product_scope import PriceRule, ProductScope
from tests.review.assistant.test_chat_engine import DOLLARS
from tests.review.assistant.test_full_check_fanout import _first_text
from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster
from tests.review.assistant.test_product_scope import _map

RULE = PriceRule()  # the owner's numbers: $2 + $0.35 a page, at least $5


# ---- the rule --------------------------------------------------------------------------------------


def test_the_price_is_a_base_plus_a_price_per_content_page() -> None:
    assert RULE.price(40) == 16_000_000  # $2 + 40 x $0.35
    assert RULE.price(20) == 9_000_000
    assert RULE.price(31) == 12_850_000


def test_a_small_set_pays_the_minimum() -> None:
    assert RULE.price(6) == 5_000_000  # $4.10 by the rule, so the $5 minimum
    assert RULE.price(0) == 5_000_000
    assert RULE.price(9) == 5_150_000  # just above it


def test_the_price_never_passes_the_sets_budget_left() -> None:
    assert RULE.price(40, bound=12_000_000) == 12_000_000
    assert RULE.price(6, bound=3_000_000) == 3_000_000  # even below the minimum
    assert RULE.price(40, bound=-1) == 0
    assert RULE.price(40, bound=99_000_000) == 16_000_000


def test_the_settings_have_the_owners_numbers() -> None:
    settings = Settings(database_url="postgresql+psycopg://unused@localhost/unused")
    from app.api.assistant_chats import price_rule

    assert price_rule(settings) == RULE
    assert "assistant_full_check_cap_usd" not in Settings.model_fields
    cheaper = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        assistant_full_check_base_usd=Decimal(1),
        assistant_full_check_per_page_usd=Decimal("0.5"),
        assistant_full_check_min_usd=Decimal(2),
    )
    assert price_rule(cheaper).price(10) == 6_000_000
    assert settings.assistant_numbers_helper_effort == "low"
    assert settings.assistant_numbers_helper_pages == 5


# ---- the pages it counts -------------------------------------------------------------------------


def _texts(words: dict[int, str]) -> Any:
    return lambda files: {("V.pdf", page): text for page, text in words.items()}


def test_the_price_counts_the_content_pages_of_the_products_picked() -> None:
    """Pages with a drawing of the picked products; a title or divider page (no drawing pasted)
    is never counted, and an area of only other products is left out."""
    page_map = _map([("1. Market", 4), ("2. Buffet", 4)])
    page_map["pages"].insert(0, {"file": "V.pdf", "page": 0, "area": None, "halves": []})
    words = _texts({1: "BASE CABINET", 2: "FILLER", 3: "DRAWER", 4: "SHELF",
                    5: "QUARTZ TOP", 6: "BACKSPLASH", 7: "SOLID SURFACE TOP", 8: "QUARTZ TOP"})  # fmt: skip

    every, _all = scoped_plan(page_map, "V.pdf", 8, ProductScope.of(None), words)
    tops, _all = scoped_plan(page_map, "V.pdf", 8, ProductScope.of(["countertop"]), words)
    boxes, _all = scoped_plan(page_map, "V.pdf", 8, ProductScope.of(["cabinet"]), words)

    assert every is not None and tops is not None and boxes is not None
    assert every.pages == 8 and 0 in {page for page, _why in every.skipped}  # the divider
    assert tops.pages == 4 and boxes.pages == 4
    assert RULE.price(every.pages) == 5_000_000  # $4.80 by the rule: the minimum
    cheap = PriceRule(base=2_000_000, per_page=350_000, minimum=1_000_000)
    assert cheap.price(every.pages) == 4_800_000
    assert cheap.price(tops.pages) == 3_400_000
    # The plan check before the run counts the same pages as the full check's own plan.
    assert plan_jobs(page_map, "V.pdf", 8) is not None
    assert scoped_plan(page_map, "V.pdf", 8, ProductScope.of(None), None)[0] == every


# ---- the roster ------------------------------------------------------------------------------------


def _priced(harness: Any, rule: PriceRule) -> None:
    engine = harness.engine
    engine.settings = dataclasses.replace(engine.settings, price_rule=rule)


def _price_said(text: str) -> str:
    return text.split("Fixed price $", 1)[1].split(":", 1)[0]


def test_the_roster_price_is_its_whole_cap_not_the_message_cap(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full check starts under the set's budget ($50 here, never a chat's message cap), and
    once code has planned the pages its price (4 content pages: $1 + 4 x $0.50) is its cap."""
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Roster(), price=50 * DOLLARS)
    _priced(harness, PriceRule(base=1_000_000, per_page=500_000, minimum=1_000_000))

    outcome = harness.run()

    assert _price_said(outcome.text) == "3"
    assert harness.engine.settings.message_cap_micros == 3 * DOLLARS
    assert outcome.cost_micros <= 3 * DOLLARS


def test_a_price_above_a_small_message_cap_is_kept_whole(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A $7 price runs whole although a chat's message cap would be $5: the set's budget is
    the only upper bound."""
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Roster(), price=40 * DOLLARS)
    _priced(harness, PriceRule(base=3_000_000, per_page=1_000_000, minimum=5_000_000))

    outcome = harness.run()

    assert _price_said(outcome.text) == "7"
    assert harness.engine.settings.message_cap_micros == 7 * DOLLARS


def test_the_roster_price_never_passes_the_sets_budget_left(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Roster(), price=40 * DOLLARS)
    harness.engine.package_spent = lambda: 37 * DOLLARS  # the set's other answers spent $37

    outcome = harness.run()

    assert _price_said(outcome.text) == "3"  # the $5 minimum, cut to the $3 left
    assert harness.engine.settings.message_cap_micros == 3 * DOLLARS


def test_continue_prices_the_pages_left_plus_the_finishing_steps(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Continue (#1821): the rule on the pages left, plus what the finishing steps need for the
    red candidates carried over, never above the set's budget left."""
    _clean_ink(monkeypatch)
    needs = {"advocate": 600_000, "escalation": 400_000}
    monkeypatch.setattr(full_check_roster._Run, "finish_needs", lambda self: dict(needs))
    rule = PriceRule(base=1_000_000, per_page=500_000, minimum=1_000_000)

    def continued(folder: Any, spent: int) -> str:
        folder.mkdir()
        harness = _roster(folder, _Roster(), price=40 * DOLLARS)
        _priced(harness, rule)
        harness.engine.package_spent = lambda: spent
        harness.engine.carry_over(
            [],
            {
                "pages": [
                    {"page": 2, "status": "same", "note": None},
                    {"page": 3, "status": "same", "note": None},
                    {"page": 4, "status": "not_opened", "note": "not compared: out of money"},
                    {"page": 5, "status": "not_opened", "note": "not compared: out of money"},
                ]
            },
        )
        harness.engine.continuing = True
        return _price_said(harness.run().text)

    # Two pages left: $1 + 2 x $0.50 = $2, plus the $1 the finishing steps need.
    assert continued(tmp_path / "a", 0) == "3"
    # The set has $2.50 left: never above it.
    assert continued(tmp_path / "b", 37_500_000) == "2.50"


def test_continue_charges_what_is_left_of_the_earlier_price_plus_the_finishing_steps(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anant, 2026-10-28: a Continue costs what is left of the earlier answer's own price (its
    price minus what it spent) plus what the finishing steps need, never a fresh price; with no
    page left and no earlier price known, never the minimum ($5 on a real upload)."""
    _clean_ink(monkeypatch)
    needs = {"advocate": 600_000, "escalation": 400_000}
    monkeypatch.setattr(full_check_roster._Run, "finish_needs", lambda self: dict(needs))
    rule = PriceRule(base=1_000_000, per_page=500_000, minimum=5_000_000)

    def continued(folder: Any, left: int | None, open_pages: int) -> str:
        folder.mkdir()
        harness = _roster(folder, _Roster(), price=40 * DOLLARS)
        _priced(harness, rule)
        pages = [
            (
                {"page": p, "status": "same", "note": None}
                if p > 1 + open_pages
                else {"page": p, "status": "not_opened", "note": "not compared: out of money"}
            )
            for p in (2, 3, 4, 5)
        ]
        harness.engine.carry_over([], {"pages": pages})
        harness.engine.continuing = True
        harness.engine.continue_left = left
        return _price_said(harness.run().text)

    # The earlier answer's price $6.55, $3.92 spent: $2.63 left, plus the $1 the steps need.
    assert continued(tmp_path / "a", 2_630_000, 0) == "3.63"
    assert continued(tmp_path / "b", 2_630_000, 2) == "3.63"  # its pages were in that price
    # No earlier price known and no page left: only the finishing steps' need, no minimum.
    assert continued(tmp_path / "c", None, 0) == "1"


# ---- the numbers helpers' settings -----------------------------------------------------------------


def _plan(areas: list[tuple[str, int]]) -> tuple[Any, dict[str, Any]]:
    page_map = _map(areas)
    plan = plan_jobs(page_map, "V.pdf", 8)
    assert plan is not None
    return plan, page_map


def test_the_default_numbers_settings_give_todays_plan_exactly() -> None:
    plan, page_map = _plan([("1. Market", 5), ("2. Buffet", 3), ("3. Vanity", 4)])
    assert with_lenses(plan, 5, page_map) == with_lenses(plan)
    lensed = with_lenses(plan, 5, page_map)
    assert all(job.fold == () for job in lensed.jobs)
    count = len(plan.jobs)
    assert [j.partner for j in lensed.jobs] == [count + n for n in range(1, count + 1)] + list(
        range(1, count + 1)
    )


def test_smaller_numbers_groups_leave_the_drawn_things_helpers_as_they_are() -> None:
    plan, page_map = _plan([("1. Market", 5), ("2. Buffet", 2), ("3. Coffee", 1), ("4. Bar", 4)])
    today = with_lenses(plan)
    smaller = with_lenses(plan, 3, page_map)

    drawn_today = [j for j in today.jobs if j.lens == "drawn"]
    drawn = [j for j in smaller.jobs if j.lens == "drawn"]
    # The drawn-things helpers: the same pages, money cap and count.
    assert [(j.pages, j.cap_micros, j.areas) for j in drawn] == [
        (j.pages, j.cap_micros, j.areas) for j in drawn_today
    ]
    assert all(j.cap_micros is not None and j.cap_micros <= DRAWN_JOB_CAP_MICROS for j in drawn)
    numbers = [j for j in smaller.jobs if j.lens == "numbers"]
    assert all(len(j.pages) <= 3 for j in numbers)
    assert sorted(p for j in numbers for p in j.pages) == sorted(p for j in drawn for p in j.pages)
    # Each numbers job sits inside one drawn-things job, and folds with it once all reported.
    by_number = {j.number: j for j in smaller.jobs}
    for job in numbers:
        partner = by_number[job.partner]  # type: ignore[index]
        assert partner.lens == "drawn" and set(job.pages) <= set(partner.pages)
        if len(partner.pages) > 3:
            assert partner.number in job.fold and set(job.fold) <= set(by_number)
    # An area of three pages or fewer is never split; the five-page area is.
    for area in ("2. Buffet", "3. Coffee"):
        holding = [j for j in numbers if area in j.areas]
        assert len(holding) == 1
    assert len([j for j in numbers if "1. Market" in j.areas]) == 2
    assert [j.number for j in smaller.jobs] == list(range(1, len(smaller.jobs) + 1))


def test_the_numbers_settings_change_only_the_numbers_helpers(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)

    def run(folder: Any, **change: Any) -> _Roster:
        folder.mkdir()
        script = _Roster()
        harness = _roster(folder, script)
        engine = harness.engine
        engine.settings = dataclasses.replace(engine.settings, **change)
        harness.run()
        return script

    def pages(params: Any) -> list[int]:
        brief = _first_text(params)
        return [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]

    def effort(params: Any) -> Any:
        return (params.get("output_config") or {}).get("effort")

    today = run(tmp_path / "today")
    tuned = run(tmp_path / "tuned", numbers_effort="medium", numbers_pages=1)

    def page_helpers(script: _Roster, kind: str) -> list[Any]:
        """The first page helpers' calls (each turn), not the gap closers'."""
        return [p for p in script.params[kind] if _first_text(p).startswith("OBJECTIVE:")]

    # The drawn-things helpers: the same pages and effort.
    assert {tuple(pages(p)) for p in page_helpers(tuned, "drawn")} == {
        tuple(pages(p)) for p in page_helpers(today, "drawn")
    }
    assert {effort(p) for p in page_helpers(tuned, "drawn")} == {
        effort(p) for p in page_helpers(today, "drawn")
    }
    # The numbers helpers: one page each, at medium effort, covering the same pages.
    assert {tuple(pages(p)) for p in page_helpers(today, "numbers")} == {(2, 3), (4, 5)}
    assert {tuple(pages(p)) for p in page_helpers(tuned, "numbers")} == {(2,), (3,), (4,), (5,)}
    assert {effort(p) for p in page_helpers(today, "numbers")} == {"low"}
    assert {effort(p) for p in page_helpers(tuned, "numbers")} == {"medium"}
