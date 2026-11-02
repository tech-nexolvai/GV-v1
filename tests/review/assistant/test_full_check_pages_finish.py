"""Pages finish while the fixed price has money (2026-11-03, Anant: "yes fix the money share so
pages finish").

A live full check (a real job, 18 vendor pages, the page reviewer at its rate a page) said "Fixed
price $28.60: this check used $10.76" and listed four pages "not compared ... each stopped at its
own share of the money while $3.91 of the fixed price was left; still not compared after 2 more
looks". Three things kept money from the pages, and the words blamed the money wrongly:

- the page reviewers' money was per page: a page that finished early kept its leftover, and once
  every reviewer was done what they had not spent ($14.15) could not reach any other step;
- the passes after the lead review were a fixed two, whatever they achieved and whatever money
  was left;
- the summary said "each stopped at its own share of the money" about pages no money had stopped.

The fix keeps every rule: the finishing steps' needs (the devil's advocate, the escalation) are
kept before any pass spends; the message's cap (the price plus the reviewers' money) is the hard
ceiling; a page that cannot be compared for a reason that is not money keeps that reason.

Made-up pages, values and prices; the only real numbers are the live run's dollars (the replay).
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster, page_reviewer
from app.review.assistant.chat_request import MODELS
from app.review.assistant.full_check_plan import UNPAIRED_NOTE, CrossPairs
from app.review.assistant.full_check_roster import (
    AGAIN_MOST_PASSES,
    MIN_GAP_RUN_MICROS,
    OUT_OF_MONEY,
    PricePlan,
    looks_reason,
)
from app.review.assistant.page_reviewer import Options, PageReview, ReviewPool, Reviews
from tests.review.assistant.chat_fakes import reply, text, tool
from tests.review.assistant.test_chat_engine import SONNET
from tests.review.assistant.test_full_check_roster import _clean_ink, _Lazy, _roster
from tests.review.assistant.test_page_reviewer import (
    FILE,
    _Engine,
    _first_final_turn,
    _reviewer_turn,
    _Script,
    _tight_last,
    _tiles,
)
from tests.review.assistant.test_unpaired_pages import WHY, _Matched

_MONEY_LINE = f"not compared, {OUT_OF_MONEY}"
_OPEN = "page not compared yet (x): compare the whole page"


# ---- a stand-in for the money a `_Run` reads --------------------------------------------------------


class _Money:
    """What `_Run` reads of the engine for money: the spend (the page reviewers' apart), the caps,
    and the calls in flight; a finding book with only `live`."""

    def __init__(self, cap: int, *, own: int = 0) -> None:
        self.spent = 0
        self.own_spent = 0
        self.own_share_micros = own
        self.settings = SimpleNamespace(message_cap_micros=cap, round_pick_cap_micros=0)
        self.helper_ceiling: int | None = None
        self.cancel = threading.Event()
        self._lock = threading.RLock()
        self._plan = SimpleNamespace(jobs=())
        self._progress = None
        self.found: list[dict[str, Any]] = []
        self.dropped: list[tuple[int, str]] = []
        self.timings = SimpleNamespace(phase=lambda _name: contextlib.nullcontext())
        self.book = SimpleNamespace(
            live=lambda: list(self.found),
            drop_coverage_note=lambda page, note: self.dropped.append((page, note)),
        )

    @property
    def counted_spent(self) -> int:
        return self.spent - self.own_spent

    @property
    def counted_cap(self) -> int:
        return max(0, int(self.settings.message_cap_micros) - self.own_share_micros)

    def _fits(self, estimate: int | None) -> bool:
        return estimate is None or self.spent + estimate <= self.settings.message_cap_micros

    def _price(self, *_args: Any) -> int:
        return 0

    def _wait_checks(self) -> None:
        return None

    def _show_coverage(self) -> None:
        return None


def _run(price: int, *, review: int = 0, pages: int = 13) -> Any:
    engine = _Money(price + review, own=review)
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    run.plan = PricePlan.balanced(price, groups=5, pages=pages)
    run.review_money = review
    return run


def _reviewers(run: Any, count: int, *, done: bool = True) -> list[Any]:
    agents = [
        SimpleNamespace(lens=page_reviewer.LENS, done=done, spent=0, cap_micros=0)
        for _ in range(count)
    ]
    run.agents = list(agents)
    return agents


class _Pages:
    """The pages still open (`_still_open`) and a pass after the lead (`_close`) as a script: each
    pass closes the pages `closes` names for it, spends `spends` and may add findings."""

    def __init__(
        self,
        run: Any,
        open_pages: set[int],
        closes: Sequence[set[int]] = (),
        spends: Sequence[int] = (),
        adds: Sequence[int] = (),
    ) -> None:
        self.run = run
        self.open = set(open_pages)
        self.closes = list(closes)
        self.spends = list(spends)
        self.adds = list(adds)
        self.budgets: list[int] = []
        self.taken: list[list[int]] = []

    def still_open(self, _run: Any, band: Any = None) -> dict[int, list[str]]:
        return {page: [_OPEN] for page in sorted(self.open)}

    def close(self, run: Any, pages: Mapping[int, Any], budget: int, *_a: Any, **_k: Any) -> Any:
        n = len(self.budgets)
        self.budgets.append(budget)
        assert run.engine.helper_ceiling == run.engine.counted_spent + budget  # never past it
        spend = min(self.spends[n] if n < len(self.spends) else 0, budget)
        run.engine.spent += spend
        self.open -= self.closes[n] if n < len(self.closes) else set()
        for k in range(self.adds[n] if n < len(self.adds) else 0):
            run.engine.found.append({"id": f"F{n}-{k}", "page": min(pages)})
        self.taken.append(sorted(pages))
        return sorted(pages)

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        script = self

        def still_open(run: Any, band: Any = None) -> dict[int, list[str]]:
            return script.still_open(run, band)

        def close(run: Any, pages: Mapping[int, Any], budget: int, *a: Any, **k: Any) -> Any:
            return script.close(run, pages, budget, *a, **k)

        monkeypatch.setattr(full_check_roster._Run, "_still_open", still_open)
        monkeypatch.setattr(full_check_roster._Run, "_close", close)


def _needs(monkeypatch: pytest.MonkeyPatch, **needs: int) -> None:
    monkeypatch.setattr(full_check_roster._Run, "finish_needs", lambda self: dict(needs))


# ---- (a) pages left open with money get more looks until closed or the money is gone ----------------


def test_four_open_pages_get_more_looks_while_each_pass_closes_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(5_000_000)
    pages = _Pages(run, {2, 3, 4, 5}, closes=[{2}, {3}, {4}, {5}], spends=[100_000] * 4)
    pages.patch(monkeypatch)
    run._after_lead(None)
    # The old fixed two passes left pages 4 and 5 "still not compared after 2 more looks".
    assert len(pages.budgets) == 4 and pages.open == set()
    assert pages.taken[-1] == [5] and run.not_done == []
    assert run.looks == {2: 1, 3: 2, 4: 3, 5: 4}


def test_the_passes_end_when_the_money_is_gone_and_the_pages_say_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(5_000_000)
    room = run._room("lead")
    # Each pass closes a page; the second leaves less than one closer's least.
    spends = [room // 2, room // 2 - MIN_GAP_RUN_MICROS // 2, 0]
    pages = _Pages(run, {2, 3, 4, 5}, closes=[{2}, {3}, {4}], spends=spends)
    pages.patch(monkeypatch)
    run._after_lead(None)
    assert len(pages.budgets) == 2
    assert run.money_pages == {4, 5}  # the late look may still take them
    assert run.not_done == [f"pages 4-5: {_MONEY_LINE}"]


def test_no_more_passes_than_the_most_even_when_every_pass_adds_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(5_000_000)
    pages = _Pages(run, {2, 3}, adds=[1] * 10, spends=[10_000] * 10)
    pages.patch(monkeypatch)
    run._after_lead(None)
    assert len(pages.budgets) == AGAIN_MOST_PASSES
    assert run.looks == {2: AGAIN_MOST_PASSES, 3: AGAIN_MOST_PASSES}
    assert run.not_done == [f"pages 2-3: not compared, {looks_reason(AGAIN_MOST_PASSES)}"]


# ---- (b) a pass that changed nothing stops the passes ---------------------------------------------


def test_a_pass_that_closed_nothing_and_added_nothing_stops_the_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(5_000_000)
    pages = _Pages(run, {2, 3, 4, 5}, spends=[40_000] * 4)
    pages.patch(monkeypatch)
    run._after_lead(None)
    assert len(pages.budgets) == 1  # the next pass would only repeat it
    assert run.not_done == ["pages 2-5: not compared, still not compared after 1 more look"]
    line = run._not_done_line(None)
    # No money stopped these pages: never said so (the live check blamed the money).
    assert "each stopped at its own share" not in line
    assert line.startswith("Not compared after more looks (listed, not dropped; not for lack")


def test_a_pass_that_only_added_findings_counts_as_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(5_000_000)
    pages = _Pages(run, {2, 3}, adds=[2, 0, 0], spends=[40_000] * 3)
    pages.patch(monkeypatch)
    run._after_lead(None)
    assert len(pages.budgets) == 2
    assert run.not_done == [f"pages 2-3: not compared, {looks_reason(2)}"]


def test_closers_that_never_compare_get_one_pass_in_a_whole_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Lazy()  # its closers never compare: the passes stop after one
    seen: list[list[int]] = []
    close = full_check_roster._Run._close

    def spy(self: Any, pages: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("again"):
            seen.append(sorted(pages))
        return close(self, pages, *args, **kwargs)

    monkeypatch.setattr(full_check_roster._Run, "_close", spy)
    outcome = _roster(tmp_path, script).run()
    assert seen == [[2, 3, 4, 5]]
    assert "still not compared after 1 more look" in outcome.text
    assert "each stopped at its own share" not in outcome.text


# ---- (c) the finishing steps' needs are kept before any pass spends -------------------------------


def test_the_devils_advocates_need_is_kept_whatever_the_passes_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(6_900_000, review=21_700_000)
    _reviewers(run, 14)
    run.engine.spent = 2_228_000 + 7_550_000
    run.engine.own_spent = 7_550_000
    _needs(monkeypatch, advocate=2_010_000, escalation=240_000)
    # Every pass spends all it is given and closes one page: the most money a pass can take.
    pages = _Pages(run, {3, 6, 7, 8}, closes=[{3}, {6}, {7}, {8}], spends=[10**9] * 4)
    pages.patch(monkeypatch)
    run._after_lead(None)
    kept = run._kept_after("lead")
    assert kept >= 2_010_000 + 240_000
    # The first pass spent all it was given: exactly the reserve is left, so no second pass.
    assert len(pages.budgets) == 1 and run._left() == kept
    assert run._room("advocate") >= 2_010_000
    assert run.money_pages == {6, 7, 8}  # said "out of money"; the late look may take them


# ---- (d) the fixed price (and the message's cap) is never passed ------------------------------------


def test_the_reviewers_unspent_money_joins_the_pool_only_once_all_are_done() -> None:
    run = _run(6_900_000, review=21_700_000)
    agents = _reviewers(run, 14, done=False)
    run.engine.spent = 2_228_000 + 7_550_000
    run.engine.own_spent = 7_550_000
    before = run._left()
    assert run._release_review_money() == 0  # reviewers still at work keep their money
    agents[0].done = True
    assert run._release_review_money() == 0
    for agent in agents:
        agent.done = True
    assert run._release_review_money() == 14_150_000
    assert run._release_review_money() == 0  # once
    assert run._left() == before + 14_150_000
    engine = run.engine
    assert engine.own_share_micros == 7_550_000  # the reviewers' share is what they spent
    assert engine.settings.message_cap_micros == 28_600_000  # the hard ceiling is unchanged
    # Spending all that is left lands exactly on the fixed price, never past it.
    engine.spent += run._left()
    assert engine.spent == 28_600_000 and run._left() == 0


def test_no_reviewer_money_no_release() -> None:
    run = _run(5_000_000)
    assert run._release_review_money() == 0 and run.released == 0


def test_the_passes_never_spend_past_the_fixed_price(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(5_000_000, review=4_000_000)
    _reviewers(run, 4)
    run.engine.spent = run.engine.own_spent = 1_000_000  # the reviewers spent $1 of $4
    pages = _Pages(run, {2, 3, 4, 5}, adds=[1] * 10, spends=[10**9] * 10)
    pages.patch(monkeypatch)
    run._after_lead(None)
    assert run.engine.spent <= run.engine.settings.message_cap_micros == 9_000_000
    assert run._left() >= run._kept_after("lead")


class _OnePageAPass(_Lazy):
    """Closers that compare page p only on its p-th visit: the gap closers' pass is every page's
    first, so the first pass after the lead compares page 2, the second page 3, the third page 4
    and the fourth page 5 (the old fixed two left pages 4 and 5)."""

    def __init__(self) -> None:
        super().__init__()
        self.visits: dict[int, int] = {}

    def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if params["system"][0]["text"].startswith("You are reviewing one page"):
            self._seen("page_review", params)
            return _reviewer_turn(params)
        return super().__call__(params)

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if not brief.startswith(full_check_roster.GAP_SCHEMA_NOTE):
            return super().helper(params, brief)
        self._seen("gap", params)
        page = int(brief.split(" on page", 1)[1].lstrip("s ").split()[0].rstrip(",:"))
        if any(m["role"] == "assistant" for m in params["messages"]):
            return reply(text("Closed."), model=SONNET)
        with self.lock:
            self.visits[page] = self.visits.get(page, 0) + 1
            visit = self.visits[page]
        if visit < page:
            return reply(text("Could not compare it yet."), model=SONNET)
        return reply(
            tool("cov", "set_coverage", {"pages": [{"page": page, "status": "same"}]}),
            stop="tool_use",
            model=SONNET,
        )


def _coverage(outcome: Any) -> dict[int, Mapping[str, Any]]:
    blocks = [b for b in outcome.blocks if b.get("type") == "coverage"]
    return {int(p["page"]): p for p in blocks[-1]["coverage"]["pages"]}


def test_a_whole_check_with_reviewers_finishes_its_pages_within_its_fixed_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.review.assistant.test_chat_engine import DOLLARS

    _clean_ink(monkeypatch)
    script = _OnePageAPass()
    harness = _roster(tmp_path, script, price=12 * DOLLARS)
    engine = harness.engine
    engine.settings = dataclasses.replace(
        engine.settings, page_reviewer=Options(per_page_micros=500_000)
    )
    outcome = harness.run()
    pages = _coverage(outcome)
    assert {pages[p]["status"] for p in (2, 3, 4, 5)} == {"same"}
    assert script.visits == {2: 2, 3: 3, 4: 4, 5: 5}
    assert "more look" not in outcome.text and "out of money" not in outcome.text
    # The reviewers' unspent money joined the pool once they were done; the cap held.
    assert script.calls.get("page_review", 0) > 0
    assert engine.own_share_micros == engine.spent - engine.counted_spent
    assert engine.spent <= engine.settings.message_cap_micros


# ---- (e) pages that cannot be compared keep their honest reason -----------------------------------


def test_an_unpaired_page_is_never_sent_on_however_many_passes_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _OnePageAPass()
    harness = _roster(tmp_path, script)
    engine = harness.engine
    matcher = _Matched(CrossPairs(pairs={2: (("arch.pdf", 9),)}, unpaired={5: WHY}))

    def page_matching(page_map: Any) -> Any:
        engine._cross = matcher.cross
        return matcher

    monkeypatch.setattr(engine, "page_matching", page_matching)
    seen: list[list[int]] = []
    close = full_check_roster._Run._close

    def spy(self: Any, pages: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(sorted(pages))
        return close(self, pages, *args, **kwargs)

    monkeypatch.setattr(full_check_roster._Run, "_close", spy)
    outcome = harness.run()
    assert len(seen) >= 3  # the gap closers, then a pass a page while each closed one
    assert all(5 not in pages for pages in seen), seen
    entry = _coverage(outcome)[5]
    assert entry["status"] == "not_comparable"
    assert entry["note"] == UNPAIRED_NOTE.format(
        why="no drawing of the same item was found in the architect's file"
    )
    assert {_coverage(outcome)[p]["status"] for p in (2, 3, 4)} == {"same"}


def test_pages_code_never_sends_on_are_never_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """No vendor drawing, no architect drawing paired, pairing unsure (a person confirms) and a
    vendor-only page already checked: never in a pass, whatever money is left."""
    run = _run(5_000_000)
    run.engine._plan = SimpleNamespace(jobs=[SimpleNamespace(pages=(2, 3, 4, 5, 6))])
    run.engine.book = SimpleNamespace(
        live=list, coverage_used=False, drop_coverage_note=lambda *_a: None
    )
    run.engine._checklist_gaps = {}
    run.no_vendor = frozenset({3})
    run.unpaired = {4: "no architect drawing", 5: "pairing unsure"}
    run.own_checked = {6}
    assert set(run._still_open()) == {2}


# ---- the replay of the live check's money ($0: accounting only) -----------------------------------

#: The live check: price $6.90 (14 content pages) + page reviewers $1.55 x 14 = $21.70 = $28.60.
#: Before the first pass after the lead, $4.67 was left under the price ($1.07 kept for the steps
#: after it: the advocate's need $0.52, the escalation's share, the summary's and second reads');
#: the reviewers had spent $7.55. The pass closers spent $0.27 and $0.17.
_PRICE, _REVIEW, _REVIEWED = 6_900_000, 21_700_000, 7_550_000
_SPENT_AT_LEAD = 6_900_000 - 4_672_000


def test_the_replay_of_the_live_check_says_why_its_four_pages_were_not_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(_PRICE, review=_REVIEW, pages=14)
    _reviewers(run, 14)
    run.engine.spent = _SPENT_AT_LEAD + _REVIEWED
    run.engine.own_spent = _REVIEWED
    _needs(monkeypatch, advocate=520_000, escalation=240_000)
    run.needs = {"advocate": 520_000, "escalation": 240_000}
    assert round(run._kept_after("lead") / 1e6, 2) == 1.07
    assert round(run._room("lead") / 1e6, 2) == 3.60  # the log's "$3.60 left"
    # Its pages 6 and 10 were compared by the first and second passes; 3 (a text-only checklist)
    # and 7-9 (key plans with no millwork sizes) were marked not comparable at every look.
    pages = _Pages(
        run,
        {3, 6, 7, 8, 9, 10},
        closes=[{6}, {10}, set()],
        spends=[270_000, 170_000, 140_000],
    )
    pages.patch(monkeypatch)
    run._after_lead(None)
    # The reviewers' $14.15 joined the pool: the first pass had $17.75, not $3.60.
    assert round(pages.budgets[0] / 1e6, 2) == 17.75
    assert len(pages.budgets) == 3  # the third pass closed nothing: no fourth
    assert run.looks == {3: 3, 6: 1, 7: 3, 8: 3, 9: 3, 10: 2}
    assert run.not_done == [
        "pages 3, 7, 8 and 9: not compared, still not compared after 3 more looks"
    ]
    line = run._not_done_line(None)
    assert "each stopped at its own share" not in line
    assert "not for lack of money, $18.24 of the fixed price was left" in line
    # Everything spent stays inside the $28.60 fixed price, the advocate's money kept.
    assert run.engine.spent == _SPENT_AT_LEAD + _REVIEWED + 580_000 <= 28_600_000
    assert run._room("advocate") >= 520_000


# ---- the page reviewers' one pool -------------------------------------------------------------------


def _agent(cap: int, spent: int = 0, *, done: bool = False) -> Any:
    return SimpleNamespace(cap_micros=cap, spent=spent, done=done, last_call=0)


def test_a_finished_pages_leftover_tops_up_a_page_at_its_cap() -> None:
    quiet, busy, third = _agent(1_000_000), _agent(1_000_000), _agent(1_000_000)
    pool = ReviewPool(3_000_000)
    for agent in (quiet, busy, third):
        pool.add(agent)
    busy.spent = 1_000_000
    assert pool.top_up(busy) == 0  # the others still at work hold their caps
    quiet.spent, quiet.done = 200_000, True
    assert pool.free() == 800_000
    assert pool.top_up(busy) == 800_000 and busy.cap_micros == 1_800_000
    assert pool.top_up(busy) == 0  # nothing more: the third still holds its cap
    third.spent, third.done = 100_000, True
    # At most its first cap again each time, and never past the pool.
    assert pool.top_up(busy) == 900_000 and busy.cap_micros == 2_700_000
    assert pool.committed() == 3_000_000 and pool.free() == 0
    assert not pool.all_done()


def _pooled_review(tmp_path: Path, turns: Sequence[Any], leftover: int) -> tuple[Any, ...]:
    """A reviewer with a $1 cap whose calls cost $0.05 and write 1,000 tokens, beside a finished
    page that left `leftover` of its $1."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = _Script(turns, 50_000, 1_000)
    engine = _Engine(tmp_path, script)
    review = PageReview(page=2, file=FILE, options=Options())
    reviews = Reviews()
    reviews.pool = ReviewPool(2_000_000)
    reviews.pool.add(_agent(1_000_000, 1_000_000 - leftover, done=True))
    agent = reviews.pool.add(
        SimpleNamespace(model=MODELS["claude-opus-5-5"], cap_micros=1_000_000, spent=0, last_call=0)
    )
    run = page_reviewer._Run(engine, agent, review, reviews, {"steps": []}, 0)  # type: ignore[arg-type]
    ended = run.run()
    return engine, review, ended, reviews


_BUSY = [[_tiles(1, 2, 3)], [_tiles(4, 5, 6)], [_tiles(7, 8, 9)]] + [[_tight_last()]] * 60


def test_a_busy_reviewer_goes_on_with_a_quiet_pages_leftover(tmp_path: Path) -> None:
    alone = _pooled_review(tmp_path / "alone", _BUSY, leftover=0)
    pooled = _pooled_review(tmp_path / "pooled", _BUSY, leftover=800_000)
    engine, review, _ended, reviews = pooled
    # Alone it stops its review at $0.80 (its share of $1, the final turn kept); with the quiet
    # page's $0.80 it reviews on past that, within its $1.80.
    assert _first_final_turn(alone[0].script.calls) * 50_000 == 800_000
    assert alone[1].topped_up == 0
    assert review.topped_up == 800_000
    assert _first_final_turn(engine.script.calls) * 50_000 > 800_000
    # Together the two pages never pass the reviewers' money ($2): this one stays within $1.80.
    assert engine.spent <= 1_800_000
    for n, call in enumerate(engine.script.calls):
        assert n * 50_000 + 10_000 + call.max_tokens * 20 <= 1_800_000
    (row,) = [r for r in _block(reviews, review)["pages"]]
    assert row["topped_up_usd"] == 0.8


def _block(reviews: Reviews, review: PageReview) -> dict[str, Any]:
    reviews.pages["p2"] = review
    return reviews.block()


def test_its_own_pass_never_takes_from_the_pool(tmp_path: Path) -> None:
    script = _Script([])
    engine = _Engine(tmp_path, script)
    review = PageReview(page=2, file=FILE, options=Options())
    reviews = Reviews()
    reviews.pool = ReviewPool(2_000_000)
    reviews.pool.add(_agent(1_000_000, 0, done=True))
    agent = reviews.pool.add(_agent(1_000_000))
    run = page_reviewer._Run(engine, agent, review, reviews, {"steps": []}, 0)  # type: ignore[arg-type]
    assert not run._topped_up()  # its own pass: the coverage check comes next
    review.pass_done = True
    assert run._topped_up() and agent.cap_micros == 2_000_000
    review.lens = "sizes"
    assert not run._topped_up()  # the sizes lens runs on what its page left
