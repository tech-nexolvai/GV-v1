"""A step at its share uses what the other steps left unspent (Anant, 2026-10-28: "yes build the
budget fix").

A live full check (a real upload, 17 pages) had a $6.55 fixed price and used $4.61, yet six pages
were "not compared, out of money" and each step "stopped at its own share". Why: from the gap
closers on, $2.13 to $3.11 was kept for the later checks, $1.61 of it the devil's advocate's need
(a blind pick for each of its red candidates, at their worst case); the advocate then spent $0.01.
Every closer stopped with $0.10 or less of money nobody kept. The fix keeps every rule:

- The money a finished step left rolls on to the next (`_room`, as before).
- The later checks' money (their shares, or their needs when more, and the calls in flight) is
  never spent by an earlier step, and nothing passes the fixed price or the message/set caps.
- Once the advocate and the escalation are done, what they did not spend is the pool: the pages
  left "out of money" get one late look from it, before the finish, keeping the shares a second
  round of proof steps and the finish still need. Pages never compared come before polishing.
- The late look's items get their own proof steps (same rules: code decides red); the first
  round's items are never asked again.

Synthetic: made-up pages, values and prices; the only real numbers are the run's dollars.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster
from app.review.assistant.full_check_roster import (
    MIN_GAP_RUN_MICROS,
    OUT_OF_MONEY,
    PricePlan,
    gap_pages,
)
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import SONNET, Harness
from tests.review.assistant.test_full_check_roster import _clean_ink, _Lazy, _roster

_MONEY_NOTE = f"not compared: {OUT_OF_MONEY}"
_MONEY_LINE = f"not compared, {OUT_OF_MONEY}"


class _Engine:
    """Just the money a `_Run` reads: what is spent, the caps, and the calls in flight."""

    def __init__(self, price: int, *, message_cap: int | None = None, package_left: int = 10**9):
        self.spent = 0
        self.settings = SimpleNamespace(
            message_cap_micros=price if message_cap is None else message_cap,
            round_pick_cap_micros=0,
        )
        self.package_left = package_left
        self.helper_ceiling: int | None = None
        self.cancel = threading.Event()
        self._lock = threading.RLock()
        self._plan = SimpleNamespace(jobs=())
        self._progress = None
        self.waited = 0
        self.timings = SimpleNamespace(phase=lambda _name: contextlib.nullcontext())
        self.book = SimpleNamespace(live=list, drop_coverage_note=lambda *_a: None)

    def _fits(self, estimate: int | None) -> bool:
        return estimate is None or estimate <= self.package_left

    def _price(self, *_args: Any) -> int:
        return 0

    def _wait_checks(self) -> None:
        self.waited += 1

    def _show_coverage(self) -> None:
        return None


def _run(price: int, **given: Any) -> Any:
    engine = _Engine(price, **given)
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    run.plan = PricePlan.balanced(price, groups=5, pages=13)
    return run


def _kept_shares(plan: PricePlan) -> int:
    """The shares kept after the lead: the advocate's, the escalation's, the summary's, the reads'."""
    return sum(plan.budgets[name] for name in ("advocate", "escalation", "summary", "reads"))


# ---- the pool: what finished steps left, never the later checks' money ---------------------------


def test_a_step_over_its_share_draws_on_what_finished_steps_left() -> None:
    run = _run(5_000_000)
    plan = run.plan
    gaps = plan.budgets["gaps"]
    # The page helpers finished with $1 of their share unspent: the gap closers' room is their own
    # share plus that dollar, the later checks' shares kept.
    run.engine.spent = plan.budgets["numbers"] + plan.budgets["drawn"] - 1_000_000
    run.engine.spent += plan.budgets["tags"] + plan.budgets["whole_look"]
    run._charge("gaps", gaps)  # the closers already used their whole share
    assert run._room("gaps") == gaps + 1_000_000
    run.engine.spent += gaps
    assert run._phase_room("gaps", 900_000)  # past its share, from the helpers' leftover
    assert not run._phase_room("gaps", 1_000_001)  # never into the later checks' money


def test_the_later_checks_reserve_is_never_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(5_000_000)
    plan = run.plan
    run.needs = {"advocate": 1_500_000, "escalation": 10_000}  # the advocate's need is more
    kept = run._kept_after("lead")
    assert kept == _kept_shares(plan) - plan.budgets["advocate"] + 1_500_000
    run.engine.spent = 5_000_000 - kept - 100_000  # $0.10 above the reserve
    assert run._room("lead") == 100_000
    run._charge("lead", plan.budgets["lead"])  # the lead used its own share
    assert run._phase_room("lead", 100_000) and not run._phase_room("lead", 100_001)
    # The late look keeps the same reserve (its needs worked out again first).
    monkeypatch.setattr(full_check_roster._Run, "finish_needs", lambda self: dict(self.needs))
    assert run._late_room() == 100_000


def test_the_fixed_price_and_the_caps_are_never_passed() -> None:
    price = 5_000_000
    # The message's cap below the price: the cap is what is left.
    run = _run(price, message_cap=3_000_000)
    run.engine.spent = 2_000_000
    assert run._left() == 1_000_000
    assert run._late_room() == max(0, 1_000_000 - _kept_shares(run.plan))
    # Nothing left under the price: no room at all, however much a step's share says.
    run = _run(price)
    run.engine.spent = price
    assert run._left() == 0 and run._late_room() == 0
    assert not run._phase_room("advocate", 1)
    # The set's (package) cap refuses a call the price would allow.
    run = _run(price, package_left=10_000)
    assert not run._phase_room("advocate", 20_000) and run._phase_room("advocate", 10_000)


def test_calls_in_flight_are_charged_before_the_late_look_and_its_ceiling_holds_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(5_000_000)
    engine = run.engine
    engine.spent = 2_000_000
    run.money_pages = {4, 5}
    run.round_done = True
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        full_check_roster._Run,
        "_still_open",
        lambda self, band=None: {4: ["page not compared yet (not reached): compare"]},
    )

    def close(self: Any, pages: Any, budget: int, *_a: Any, **given: Any) -> list[int]:
        seen.update(budget=budget, ceiling=engine.helper_ceiling, waited=engine.waited, **given)
        return []

    monkeypatch.setattr(full_check_roster._Run, "_close", close)
    run._late_pages(None, None)
    room = 5_000_000 - 2_000_000 - _kept_shares(run.plan)
    assert seen["waited"] >= 1  # every read in flight charged before the pool is worked out
    assert seen["budget"] == room and seen["ceiling"] == 2_000_000 + room
    assert seen["late"] is True and engine.helper_ceiling is None


# ---- the replay of the live run's spend ($0: accounting only) ------------------------------------

#: The run's money in millionths of $: its fixed price and the advocate's need kept from the gaps on
#: (a blind pick at $0.045 for each of its red candidates). Pages are made up.
_PRICE = 6_550_000
_ADVOCATE_NEED = 1_605_000
_LEFT_PAGES = {page: ["page not compared yet (x)"] for page in range(2, 8)}


def test_the_replay_of_the_live_run_shows_which_stopped_steps_could_go_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(_PRICE)
    plan = run.plan
    run.needs = {"advocate": _ADVOCATE_NEED, "escalation": 160_000}
    # The log's "money kept before gaps: $3.11" and "before lead: $2.13".
    assert round(run._kept_after("gaps") / 1e6, 2) == 3.11
    assert round(run._kept_after("lead") / 1e6, 2) == 2.13
    # The first gap closers stopped at $3.37 spent: $0.07 nobody kept (they could not go on).
    run.engine.spent = 3_370_000
    assert run._room("gaps") < 80_000
    # The first pass after the lead: $0.33, four pages at the least a page gets; two left.
    run.engine.spent = _PRICE - run._kept_after("lead") - 330_000
    chosen, left = gap_pages(_LEFT_PAGES, run._room("lead"))
    assert len(chosen) == 4 and len(left) == 2
    # Its closer spent $0.23: $0.10 left, below one closer's least, so all six "out of money".
    run.engine.spent += 230_000
    assert run._room("lead") < MIN_GAP_RUN_MICROS
    # The advocate ($0.01), the escalation ($0.06) and the third reads ($0.13) are done: their
    # needs are gone. The pool is what is left less the shares a second round and the finish keep.
    run.engine.spent = 4_610_000  # the whole run's spend, the finish's too (the least pool)
    monkeypatch.setattr(
        full_check_roster._Run, "finish_needs", lambda self: {"advocate": 0, "escalation": 0}
    )
    pool = run._late_room()
    assert pool == _PRICE - 4_610_000 - _kept_shares(plan)
    assert round(pool / 1e6, 2) == 0.96
    chosen, left = gap_pages(_LEFT_PAGES, pool)
    assert len(chosen) == 6 and left == []  # every page left for money gets its late look
    assert pool // MIN_GAP_RUN_MICROS == 3  # three closers, about $0.32 each
    # The reserve is still whole for the late items' proof steps and the finish.
    assert _PRICE - 4_610_000 - pool == _kept_shares(plan)


# ---- which pages the late look takes --------------------------------------------------------------


def test_pages_never_compared_come_before_polishing_when_money_is_short() -> None:
    pages = {2: ["left open by its helper: x"], 3: ["left open by its helper: x"]}
    pages |= {6: ["page not compared yet (x)"], 7: ["page not compared yet (x)"]}
    chosen, left = gap_pages(pages, 2 * full_check_roster.MIN_GAP_PAGE_MICROS, first={6, 7})
    assert chosen == [6, 7] and left == [2, 3]
    # Without `first`, page order as before.
    assert gap_pages(pages, 2 * full_check_roster.MIN_GAP_PAGE_MICROS)[0] == [2, 3]


def test_the_late_look_takes_only_pages_left_for_money_and_never_polishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(5_000_000)
    run.money_pages = {4, 5, 6, 8}  # page 8: a later pass compared it after all
    run.not_done = [f"pages 4-6: {_MONEY_LINE}", "Close the gaps on page 2: used its share"]
    monkeypatch.setattr(
        full_check_roster._Run,
        "_still_open",
        lambda self, band=None: {
            3: ["page not compared yet (not reached): compare"],  # not left for money
            4: ["page not compared yet (x): compare"],
            5: ["left open by its helper: strings 0/2"],  # polishing
            6: ["page not compared yet (x): compare"],
        },
    )
    taken: list[list[int]] = []

    def close(self: Any, pages: Mapping[int, Any], *_a: Any, **_k: Any) -> list[int]:
        taken.append(sorted(pages))
        return []

    monkeypatch.setattr(full_check_roster._Run, "_close", close)
    run._late_pages(None, None)
    assert taken == [[4, 6]]
    # Page 5 keeps its "out of money" line; pages 4 and 6 lost theirs to the late look, and page 8
    # (compared) is no longer said to be out of money.
    assert run.not_done == ["Close the gaps on page 2: used its share", f"page 5: {_MONEY_LINE}"]
    assert run.money_pages == {5}


def test_no_late_look_when_the_pool_is_too_small_or_the_check_was_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        full_check_roster._Run,
        "_still_open",
        lambda self, band=None: {4: ["page not compared yet (x): compare"]},
    )
    taken: list[Any] = []

    def close(self: Any, pages: Mapping[int, Any], *_a: Any, **_k: Any) -> list[int]:
        taken.append(pages)
        return []

    monkeypatch.setattr(full_check_roster._Run, "_close", close)
    run = _run(5_000_000)
    run.money_pages = {4}
    run.not_done = [f"page 4: {_MONEY_LINE}"]
    run.engine.spent = 5_000_000 - _kept_shares(run.plan) - (MIN_GAP_RUN_MICROS - 1)
    run._late_pages(None, None)
    assert taken == [] and run.not_done == [f"page 4: {_MONEY_LINE}"]  # its reason stands
    run.engine.spent = 0
    run.engine.cancel.set()
    run._late_pages(None, None)
    assert taken == []


def test_the_late_items_alone_get_the_proof_steps() -> None:
    run = _run(5_000_000)
    old = {"id": "F1", "status": "different"}
    new = {"id": "F9", "status": "different"}
    assert run._in_scope(old) and run._in_scope(new)
    run.only_ids = frozenset({"F9"})
    assert not run._in_scope(old) and run._in_scope(new)
    run.engine.book = SimpleNamespace(live=lambda: [old, new])
    assert [f["id"] for f in run._unproved()] == ["F9"]
    run.only_ids = None
    assert [f["id"] for f in run._unproved()] == ["F1", "F9"]


def test_a_money_note_comes_off_a_page_and_its_other_notes_stay(tmp_path: Path) -> None:
    book = Harness(tmp_path, ScriptedProvider()).engine.book
    book.set_coverage(
        {"pages": [{"page": 2, "status": "not_comparable", "note": "pairing unsure; widths"}]},
        "helper:3",
    )
    book.note_coverage(2, _MONEY_NOTE)
    book.note_coverage(2, _MONEY_NOTE)
    book.drop_coverage_note(2, _MONEY_NOTE)
    entry = next(p for p in book.coverage_map()["pages"] if p["page"] == 2)
    assert entry["note"] == "pairing unsure; widths" and entry["status"] == "not_comparable"
    book.drop_coverage_note(9, _MONEY_NOTE)  # no page recorded: nothing to do


# ---- a whole roster check (scripted provider, no paid call) ---------------------------------------


class _ComparesOnSecondVisit(_Lazy):
    """The page helpers and the first gap closers leave their pages; a page's second closer
    compares it."""

    def __init__(self) -> None:
        super().__init__()
        self.closers: dict[int, int] = {}
        self.ids: list[str] = []

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if not brief.startswith(full_check_roster.GAP_SCHEMA_NOTE):
            return super().helper(params, brief)
        self._seen("gap", params)
        page = int(brief.split(" on page", 1)[1].lstrip("s ").split()[0].rstrip(",:"))
        with self.lock:
            self.closers[page] = self.closers.get(page, 0) + 1
            second = self.closers[page] > 1
        if not second or any(m["role"] == "assistant" for m in params["messages"]):
            return reply(text(f"Closed.\nCHECKLIST p{page}: architect 1/1; strings 0/0; not compared: none"), model=SONNET)  # fmt: skip
        return reply(
            tool("cov", "set_coverage", {"pages": [{"page": page, "status": "same"}]}),
            stop="tool_use",
            model=SONNET,
        )


def _coverage(outcome: Any) -> dict[int, Mapping[str, Any]]:
    blocks = [b for b in outcome.blocks if b.get("type") == "coverage"]
    return {int(p["page"]): p for p in blocks[-1]["coverage"]["pages"]}


@contextlib.contextmanager
def _short_after_the_lead(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The passes after the lead find too little money (the later checks' money kept); once the
    proof steps are done (`round_done`), the real room."""
    real_room = full_check_roster._Run._room

    def room(self: Any, phase: str) -> int:
        if phase == "lead" and self.lead_taken and not self.round_done:
            return MIN_GAP_RUN_MICROS - 1
        return int(real_room(self, phase))

    monkeypatch.setattr(full_check_roster._Run, "_room", room)
    yield


def test_pages_left_for_money_are_compared_from_what_the_finished_steps_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _ComparesOnSecondVisit()
    harness = _roster(tmp_path, script)
    with _short_after_the_lead(monkeypatch):
        outcome = harness.run()
    pages = _coverage(outcome)
    assert {pages[p]["status"] for p in (2, 3, 4, 5)} == {"same"}
    assert all(OUT_OF_MONEY not in str(pages[p].get("note")) for p in (2, 3, 4, 5))
    assert "out of money" not in outcome.text
    assert all(n >= 2 for n in script.closers.values()), script.closers  # the late look's visit
    assert harness.engine.spent <= harness.engine.settings.message_cap_micros


def test_with_the_money_really_short_the_pages_still_say_out_of_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    real_room = full_check_roster._Run._room

    def room(self: Any, phase: str) -> int:
        if phase == "lead" and self.lead_taken:
            return MIN_GAP_RUN_MICROS - 1  # after the lead and after the proof steps: short
        return int(real_room(self, phase))

    monkeypatch.setattr(full_check_roster._Run, "_room", room)
    script = _ComparesOnSecondVisit()
    outcome = _roster(tmp_path, script).run()
    pages = _coverage(outcome)
    for page in (2, 3, 4, 5):
        assert pages[page]["status"] not in ("same", "different")
        assert "out of money" in str(pages[page].get("note")), pages[page]
    assert "out of money" in outcome.text
