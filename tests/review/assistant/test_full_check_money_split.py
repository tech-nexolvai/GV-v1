"""The full check's money split (the live run of 2026-10-24): a $9 price, $4.36 used, yet ten
specialists stopped "at their share" and the summary said "the fixed price was reached".

- Calls running together are held at their worst case; a call that fits the money really spent
  waits for them to end instead of stopping its specialist (`ChatEngine._hold`).
- A specialist at its cap takes its pool's leftovers, then the price's money above what the later
  steps keep (`_Pool.spare`, `_Run._spare`); the later steps' money is never spent.
- A tag reader or a whole-drawing look the money stopped is "not done within the price", never an
  unreadable table or "no usable answer"; "the fixed price was reached" only when it was.
- 4" [101mm] is plain rounding, not millimetres that do not match.

Synthetic: scripted provider, no paid call, no client data."""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.review.assistant import full_check_roster, whole_look
from app.review.assistant.chat_engine import (
    FULL_CHECK_INSTRUCTION,
    KEPT_REACHED,
    PRICE_REACHED,
    _Agent,
    _final_stop_why,
    _Share,
)
from app.review.assistant.chat_request import BY_SHORT
from app.review.assistant.finding_checks import mm_off, units_check
from app.review.assistant.full_check_roster import PricePlan, _Pool
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS, Harness

# ---- the holds of the calls running together -------------------------------------------------


def test_a_call_that_fits_the_money_spent_waits_for_the_running_calls(tmp_path: Path) -> None:
    engine = Harness(tmp_path, ScriptedProvider()).engine
    share = _Share(limit=1_000_000)
    share.held = 900_000  # calls running now, at their worst case
    got: list[bool] = []
    waiting = threading.Thread(target=lambda: got.append(engine._hold(share, 500_000)))
    waiting.start()
    time.sleep(0.2)
    assert got == []  # it waits: the money spent ($0) leaves room
    engine._release(share, 900_000)  # the running calls ended (and cost little)
    waiting.join(5)
    assert got == [True] and share.held == 500_000


def test_a_call_that_cannot_fit_even_alone_stops_at_once(tmp_path: Path) -> None:
    engine = Harness(tmp_path, ScriptedProvider()).engine
    share = _Share(limit=1_000_000, held=100_000)
    engine._spent = 700_000
    started = time.monotonic()
    assert engine._hold(share, 400_000) is False  # 0.7 + 0.4 > 1.0: the share is really used
    assert engine._final_fits(share, 400_000) is False
    assert time.monotonic() - started < 1
    assert share.held == 100_000


def test_a_stop_while_waiting_ends_the_wait(tmp_path: Path) -> None:
    engine = Harness(tmp_path, ScriptedProvider()).engine
    share = _Share(limit=1_000_000, held=900_000)
    engine.cancel.set()
    assert engine._hold(share, 500_000) is False


def test_specialists_started_together_all_finish_when_their_worst_cases_fill_the_share(
    tmp_path: Path,
) -> None:
    """The live run's shape: many specialists at once, each call held at its worst case (about
    $0.17 a Sonnet call with its allowance), together far above the share, while what they
    really spend is a small part of it. Before, the late ones stopped at once."""

    class Slow:
        def wait(self, _timeout: float) -> None:
            time.sleep(0.05)

    def answer(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if params.get("tool_choice") == {"type": "none"} or len(params["messages"]) > 1:
            return [{"type": "__wait__", "until": Slow()}, *reply(text("Done."), model=SONNET_ID)]
        step = tool("t", "bash_tool", {"command": "true", "description": "Read"})
        return [
            {"type": "__wait__", "until": Slow()},
            *reply(step, stop="tool_use", model=SONNET_ID, usage={"output_tokens": 1_000}),
        ]

    harness = Harness(
        tmp_path,
        ScriptedProvider(helper_answer=answer),
        question=FULL_CHECK_INSTRUCTION,
        message_cap=9 * DOLLARS,
    )
    engine = harness.engine
    engine.settings = dataclasses.replace(
        engine.settings, mode="full_check", drawn_helpers=True, roster=True
    )
    engine.helper_share_percent = 100
    engine.helper_ceiling = DOLLARS  # $1 for twelve specialists: their worst cases add to ~$2
    agents = [
        _Agent(
            call_id=f"h{n}",
            description=f"Pages {n} · numbers",
            prompt=f"Helper {n}",
            model=BY_SHORT["sonnet"],
            pages=(n,),
            lens="numbers",
            soft_cap=True,
            cap_micros=200_000,
        )
        for n in range(1, 13)
    ]
    entries = engine._launch(agents)
    assert [entry["stopped"] for entry in entries] == [None] * 12
    assert all(entry["report"] == "Done." for entry in entries)
    assert engine.spent < DOLLARS


SONNET_ID = BY_SHORT["sonnet"].id


# ---- the price's spare money -------------------------------------------------------------------


def _specialist(cap: int, spent: int = 0, done: bool = False) -> Any:
    return SimpleNamespace(lens="numbers", cap_micros=cap, spent=spent, done=done, top_up=None)


def test_a_specialist_at_its_cap_takes_the_prices_spare_money_once_its_pool_is_used() -> None:
    pool = _Pool(1_000_000)
    busy, other = pool.add(_specialist(500_000, 450_000)), pool.add(_specialist(500_000))
    assert pool.top_up(busy) == 0  # its pool's money is promised to the other one
    pool.spare = lambda agent: 3_000_000
    assert pool.top_up(busy) == 500_000  # at most its first cap again, from the price
    pool.spare = lambda agent: 10_000
    assert pool.top_up(busy) == 0  # too little to be worth a call: it wraps up
    del other


def test_the_spare_money_keeps_what_the_later_steps_need(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider(), message_cap=9 * DOLLARS)
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(9 * DOLLARS, 7, 20)
    kept = sum(run.plan.budgets[name] for name in full_check_roster.PROTECTED)
    agent = _specialist(300_000, 250_000)
    assert run._spare(agent) == 0  # no first phase running: nothing to give
    other = _specialist(300_000, 100_000)
    other.last_call = 100_000
    run._p1_pools([agent, other], 9 * DOLLARS - kept)
    harness.engine._spent = 4_360_000  # the live run's spend
    # Less what is left of its own cap, and a record turn and report for each one running (#1654:
    # never refused for the share): at least WRAP_MICROS, else twice its last call.
    wraps = full_check_roster.WRAP_MICROS + 2 * 100_000
    assert run._spare(agent) == 9 * DOLLARS - kept - 4_360_000 - 50_000 - wraps
    other.done = True
    assert run._spare(agent) == 9 * DOLLARS - kept - 4_360_000 - 50_000 - 60_000
    harness.engine._spent = 9 * DOLLARS - kept  # the later steps' money is all that is left
    assert run._spare(agent) == 0


def test_the_first_phase_pools_share_the_spare_money(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider(), message_cap=9 * DOLLARS)
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(9 * DOLLARS, 7, 20)
    helper = SimpleNamespace(lens="numbers", cap_micros=100_000, spent=0, done=False, top_up=None)
    look = SimpleNamespace(
        lens=whole_look.LENS, cap_micros=150_000, spent=0, done=False, top_up=None
    )
    pools = run._p1_pools([helper, look], 6 * DOLLARS)
    assert len(pools) == 2 and all(pool.spare == run._spare for pool in pools)
    assert helper.top_up == pools[0].top_up and look.top_up == pools[1].top_up
    # No look: the looks' share goes to the helpers' pool, as before.
    alone = run._p1_pools([helper], 6 * DOLLARS)
    assert len(alone) == 1
    assert alone[0].limit == sum(
        run.plan.budgets[name] for name in ("numbers", "drawn", "tags", whole_look.LENS)
    )


def test_a_look_gets_at_least_one_looks_money() -> None:
    run = SimpleNamespace(plan=PricePlan.balanced(9 * DOLLARS, 7, 20))
    # Twenty pages share the looks' 14% of $9 ($0.063 each): a look costs about $0.12.
    assert whole_look._cap(run, 1, 20) == whole_look.ONE_LOOK_MICROS


# ---- honest words ---------------------------------------------------------------------------------


def test_the_fixed_price_is_said_reached_only_when_it_was() -> None:
    assert _final_stop_why(False) == PRICE_REACHED == "the fixed price was reached"
    assert _final_stop_why(True) == KEPT_REACHED
    assert "the fixed price was not reached" in KEPT_REACHED


def test_the_not_done_line_says_the_money_left(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider(), message_cap=9 * DOLLARS)
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(9 * DOLLARS, 7, 20)
    harness.engine._spent = 4_360_000
    run._capped("Pages 3-5 · numbers", "; the pages it left are listed")
    line = run._not_done_line(None)
    assert "Not done within the fixed price" not in line
    assert "$4.64 of the fixed price was left" in line and "Pages 3-5 · numbers" in line
    harness.engine._spent = 9 * DOLLARS
    assert run._not_done_line("cost_cap").startswith("Not done within the fixed price")


def _tag_reader(pages: tuple[int, ...]) -> Any:
    return SimpleNamespace(pages=pages, description="Tags and models")


def test_a_tag_reader_the_money_stopped_is_not_an_unreadable_table(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedProvider(), message_cap=9 * DOLLARS)
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(9 * DOLLARS, 7, 20)
    stopped = (
        "This helper stopped before it could write its report (the fixed price was reached).\n"
        "It had not used any tool yet."
    )
    run._apply_tags(
        [
            (_tag_reader(tuple(range(1, 15))), stopped, "cost_cap"),
            (_tag_reader(tuple(range(15, 21))), '{"tags": []}', "cost_cap"),
        ]
    )
    assert run.failed_steps == []
    assert run.unfinished_steps == ["the tags of pages 1-14 are not compared yet"]
    assert any("Tags and models, pages 1-14" in line for line in run.not_done)
    why = run._not_for_sending()
    assert why is not None and "could not be read" not in why
    assert why.startswith("a step was not finished within the price")
    # A table that is not readable for another reason is still a broken step (#1811).
    run._apply_tags(
        [
            (_tag_reader((1, 2)), "I could not decide.", None),
            (_tag_reader((3, 4)), '{"tags": []}', None),
        ]
    )
    assert run.failed_steps == ["the tag table of pages 1-2 could not be read"]


def test_a_look_the_money_stopped_is_not_a_look_with_no_usable_answer() -> None:
    state = whole_look.WholeLooks()
    drawings = (
        whole_look.Drawing("v.pdf", 9, (0, 0, 1, 1), "vendor"),
        whole_look.Drawing("a.pdf", 21, (0, 0, 1, 1), "architect"),
    )
    look = whole_look.Look(number=9, area="Kitchen", drawings=drawings)
    agent = SimpleNamespace(call_id="whole-look-p9", skip=False)
    state.looks[agent.call_id] = look
    engine = SimpleNamespace(tools=SimpleNamespace(workspace=None), book=None)
    run = SimpleNamespace(engine=engine)
    stopped = "This helper stopped before it could write its report (the fixed price was reached)."
    whole_look.apply_roster(run, [(agent, {"report": stopped, "stopped": "cost_cap"})], state)
    assert state.capped == ["pages 9"] and state.unread == []
    # The same words with no money stop: no usable answer, as before.
    whole_look.apply_roster(
        run, [(agent, {"report": "I could not decide.", "stopped": None})], state
    )
    assert state.unread == ["pages 9"]


# ---- millimetres within a drawing's rounding --------------------------------------------------


def test_millimetres_within_a_drawings_rounding_match_the_inches() -> None:
    for printed in ('4" [101mm]', '4" [102mm]', '3/4" [20mm]', '16 9/16" [420mm]', '3/32" [2mm]'):
        assert units_check(printed) == "agrees", printed
    for printed in ('4" [104mm]', '4" [100mm]', '27" [660]', '31 1/4" [800]'):
        assert units_check(printed) == "disagrees", printed
    # A pack made before this change marked 4" [101mm]: it is not said to mismatch any more.
    assert not mm_off({"text": '4" [101mm]', "mm_ignored": True})
    assert mm_off({"text": '31 1/4" [800]', "mm_ignored": True})
    assert not mm_off({"text": '31 1/4" [800]'})
