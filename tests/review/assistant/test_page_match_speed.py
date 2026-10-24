"""The page matcher beside the helpers (#1883): faster, in order, shown, timed.

A real two-file upload (99 content pages, a separate architect's PDF) spent 17 minutes in the AI
page matcher before any helper started, while the screen said "usually 4-5 min". Now:

- the matcher asks 8 at a time (a group's first call still writes the pictures' cache first),
  in the order the first helpers need, telling the full check as each answer lands;
- each helper waits only for its own pages: pages code (or a saved answer) already decided start
  at once, a page the matcher must decide starts as soon as its answer lands, with the SAME
  pairs as when the whole matcher had ended (the agreement rule is untouched);
- the live card shows a "Matching pages" step with its count, then the pages compared;
- the usual time depends on the set's pages and whether they are matched across files.

Synthetic facts and PDFs only, $0, no model call (fake askers and a scripted provider).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import check_time, page_match
from app.review.assistant.chat_contract import ChatLivePhaseOut, ChatLiveStateOut
from app.review.assistant.chat_engine import ChatEngine
from app.review.assistant.full_check_plan import CrossPairs, plan_jobs, with_pairs
from app.review.assistant.live_check import LiveCheck
from app.review.assistant.simple_check import pairing
from app.review.assistant.simple_check.pairing import UNPAIRED
from app.review.assistant.simple_check.worklist import build_worklist
from tests.review.assistant.test_full_check_fanout import FILE, _set_map
from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster
from tests.review.assistant.test_live_check import _live, _valid
from tests.review.assistant.test_page_match import _FakePictures, _pair, _two_files
from tests.review.assistant.test_set_pack_pages import _read

# ---- the matcher's calls: 8 at a time, in order, each one reported -------------------------------


class _Counting:
    """A fake matcher call that counts the calls running at once, and answers A1."""

    def __init__(self, hold: float = 0.05, wait_for: int = 0) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.most = 0
        self.order: list[str] = []
        self.hold = hold
        self.wait_for = wait_for

    def __call__(self, messages: list[dict[str, Any]], schema: Any) -> page_match.AskReply:
        where = next(
            b["text"] for b in messages[0]["content"] if b.get("text", "").startswith("The vendor")
        )
        with self.lock:
            self.now += 1
            self.most = max(self.most, self.now)
            self.order.append(where)
        ends = time.monotonic() + 2.0
        # Hold the call until `wait_for` run at once (or a while), so "at once" is really seen.
        while time.monotonic() < ends:
            with self.lock:
                if self.now >= self.wait_for:
                    break
            time.sleep(0.005)
        time.sleep(self.hold)
        with self.lock:
            self.now -= 1
        return page_match.AskReply(
            '{"matches": ["A1"], "confidence": "low", "reason": "same"}',
            "end_turn",
            {"input_tokens": 1000, "output_tokens": 40},
            "claude-sonnet-5-5",
            0.01,
        )


def _own_group(n: int) -> Any:
    """A vendor drawing shown its own two items (its own cache group)."""
    return _pair(f"p{n}.v0", (f"p{1000 + 2 * n}", f"p{1001 + 2 * n}"))


def test_the_matcher_asks_eight_at_a_time_and_never_more(tmp_path: Path) -> None:
    assert page_match.PARALLEL == 8
    ask = _Counting(wait_for=8)
    pairs = [_own_group(n) for n in range(1, 13)]

    report = page_match.match_pairs(pairs, tmp_path, ask, _FakePictures())

    assert report.answered == 12
    assert ask.most == 8


def test_a_groups_first_call_answers_before_the_rest_of_its_group(tmp_path: Path) -> None:
    """The pictures' cache entry: the group's first call alone, then the rest at once."""
    ask = _Counting(wait_for=4)
    same = [_pair(f"p{n}.v0") for n in range(1, 6)]  # five drawings shown the same two items

    page_match.match_pairs(same, tmp_path, ask, _FakePictures())

    assert ask.order[0].startswith("The vendor drawing (the vendor's file, page 1")
    assert ask.most == 4  # the other four together, only after the first answered


def test_the_matcher_asks_in_the_order_given_and_reports_every_drawing(tmp_path: Path) -> None:
    ask = _Counting()
    pairs = [_own_group(n) for n in range(1, 6)]
    told: list[str] = []
    wanted = [4, 2, 5, 1, 3]

    page_match.match_pairs(
        pairs,
        tmp_path,
        ask,
        _FakePictures(),
        parallel=1,
        first=lambda pair: wanted.index(pair.vendor.page),
        on_done=told.append,
    )

    assert [int(w.split("page ")[1].split(")")[0]) for w in ask.order] == wanted
    assert told == [f"p{n}.v0" for n in wanted]
    # Left to code (the money, a stop) is reported too: a helper never waits for it forever.
    money: list[str] = []
    page_match.match_pairs(
        [_own_group(9)],
        tmp_path / "m",
        ask,
        _FakePictures(),
        fits=lambda _t: False,
        on_done=money.append,
    )
    assert money == ["p9.v0"]
    stop = threading.Event()
    stop.set()
    stopped: list[str] = []
    page_match.match_pairs(
        [_own_group(8)], tmp_path / "s", ask, _FakePictures(), stop=stop, on_done=stopped.append
    )
    assert stopped == ["p8.v0"]


# ---- the run beside the helpers: same pairs, page by page ------------------------------------------


class _Engine:
    """What the matcher's run reads of the engine (no provider: the call is a fake asker)."""

    def __init__(self, folder: Path, model: str | None = "claude-sonnet-5-5") -> None:
        self.settings = type("S", (), {"page_match_model": model})()
        self.tools = type(
            "T", (), {"workspace": type("W", (), {"set_pack": folder, "uploads": folder})()}
        )()
        self.book = type("B", (), {"main_file": "shop.pdf"})()
        self.cancel = threading.Event()
        self._cross: CrossPairs | None = None
        self.live: Any = None

    def _fits(self, estimate: int) -> bool:
        return True

    def _price(self, model: Any, tokens: int, out: int) -> int:
        return 1


def _answering(
    monkeypatch: pytest.MonkeyPatch, text: str, gate: threading.Event | None = None
) -> list[int]:
    """The matcher's calls answered with `text` (after `gate`, when given); the calls counted."""
    calls: list[int] = []

    def asker(engine: Any, model_name: str, effort: str = "low") -> Any:
        def ask(messages: list[dict[str, Any]], schema: Any) -> page_match.AskReply:
            if gate is not None:
                assert gate.wait(5)
            calls.append(1)
            return page_match.AskReply(text, "end_turn", {"input_tokens": 10}, "fake", 0.01)

        return ask

    monkeypatch.setattr(page_match, "engine_asker", asker)
    monkeypatch.setattr(page_match, "Pictures", lambda folder, uploads: _FakePictures())
    return calls


SURE_A1 = '{"matches": ["A1"], "confidence": "high", "reason": "same three-cabinet run"}'
SURE_A2 = '{"matches": ["A2"], "confidence": "high", "reason": "looks the same"}'


def test_a_page_waits_only_for_its_own_answer_and_gets_the_same_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_files(tmp_path)
    page_map = _read(folder, "page_map.json")
    gate = threading.Event()
    calls = _answering(monkeypatch, SURE_A1, gate)
    engine = _Engine(folder)

    run = page_match.prepare_run(engine, page_map)  # type: ignore[arg-type]

    assert run is not None and run.total == 1 and run.waiting() == {1} == run.to_answer
    # Before the answer: code alone has no clear pick, so the page is not paired yet.
    assert 1 not in run.cross.pairs
    run.start([1])
    waited: list[CrossPairs] = []
    helper = threading.Thread(target=lambda: waited.append(run.wait_pages([1], engine.cancel)))
    helper.start()
    helper.join(0.3)
    assert helper.is_alive() and not waited  # the helper waits for its page's answer
    assert run.wait_pages([7], engine.cancel) is run.cross  # a page with no question: at once
    gate.set()
    helper.join(5)
    assert waited and waited[0].pairs == {1: (("arch.pdf", 2),)}
    assert run.finish().pairs == {1: (("arch.pdf", 2),)} and calls == [1]
    # The pages it was asked about stay fixed: a helper planned after the answer still waits for
    # (and is briefed from) the answers, never from the pairs before them.
    assert run.waiting() == frozenset() and run.to_answer == {1}
    # The blocking path (the other modes) gives exactly the same pairs from the saved answer.
    again = _Engine(folder, model=None)
    assert ChatEngine.cross_file_pairs(again, page_map) == run.finish()  # type: ignore[arg-type]


def test_a_sure_pick_code_does_not_back_is_still_not_paired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agreement rule is untouched: the page is decided Not compared, with the reason."""
    folder = _two_files(tmp_path)
    page_map = _read(folder, "page_map.json")
    _answering(monkeypatch, SURE_A2)
    run = page_match.prepare_run(_Engine(folder), page_map)  # type: ignore[arg-type]
    assert run is not None
    run.start([1])

    found = run.wait_pages([1])

    assert found.pairs == {} and found.unpaired[1].startswith(UNPAIRED)
    assert "the AI page matcher is sure of architect page 3" in found.unpaired[1]
    # Never "no drawing": both pages named, as a person finds them (no private ids).
    assert "pairing unsure: architect page 3 or architect page 2?" in found.unpaired[1]
    assert "p1003" not in found.unpaired[1]


def test_a_failed_matcher_leaves_the_page_to_code_and_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_files(tmp_path)
    page_map = _read(folder, "page_map.json")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no provider")

    monkeypatch.setattr(page_match, "run_for_engine", broken)
    run = page_match.prepare_run(_Engine(folder), page_map)  # type: ignore[arg-type]
    assert run is not None
    run.start([1])

    found = run.wait_pages([1])

    assert found.pairs == {} and found.unpaired[1].startswith(UNPAIRED)
    assert run.waiting() == frozenset()


def test_the_work_list_ranks_once_and_runs_only_the_agreement_rule_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_files(tmp_path)
    counted: list[int] = []
    real = pairing.rank

    def rank(*args: Any, **kwargs: Any) -> Any:
        counted.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(pairing, "rank", rank)
    memo: dict[Any, Any] = {}
    first = build_worklist(folder, "shop.pdf", memo=memo)
    ranked = len(counted)
    second = build_worklist(folder, "shop.pdf", memo=memo)
    assert ranked >= 1 and len(counted) == ranked  # nothing ranked again
    assert second == first
    assert build_worklist(folder, "shop.pdf") == first  # the same as with no memo


def test_jobs_given_their_pairs_when_they_start_are_the_planned_jobs() -> None:
    page_map = _set_map(areas=6)
    across = CrossPairs(
        pairs={2: (("arch.pdf", 4),), 5: (("arch.pdf", 9), ("arch.pdf", 10))},
        unpaired={7: f"{UNPAIRED}: code found no clear architect drawing for it"},
    )
    planned = plan_jobs(page_map, FILE, 4, across=across)
    bare = plan_jobs(page_map, FILE, 4)
    assert planned is not None and bare is not None
    assert planned.jobs == tuple(with_pairs(job, across) for job in bare.jobs)
    assert any(job.pairs for job in planned.jobs) and any(job.unpaired for job in planned.jobs)


# ---- the roster: helpers start beside the matcher ---------------------------------------------------


class _SlowMatch:
    """A page matcher that has decided every page but 4 and 5 until a helper of pages 2-3 has
    started: it answers them then (page 4 paired, page 5 not)."""

    def __init__(self) -> None:
        self.answer = threading.Event()
        self.total = 2
        self.started: list[int] | None = None
        self.timed_out = False
        self.finished = False
        self.cross = CrossPairs()
        self.final = CrossPairs(
            pairs={4: (("arch.pdf", 7),)},
            unpaired={5: f"{UNPAIRED}: the AI page matcher found no architect drawing"},
        )

    to_answer = frozenset({4, 5})

    def start(self, first_pages: Any) -> None:
        self.started = list(first_pages)

    def wait_pages(self, pages: Any, cancel: Any = None) -> CrossPairs:
        if set(pages) & {4, 5}:
            self.timed_out = not self.answer.wait(10)
            return self.final
        return self.cross

    def finish(self) -> CrossPairs:
        self.finished = True
        return self.final


class _Watching(_Roster):
    """The roster's scripted answers, noting each helper's first call (its pages, its brief)."""

    def __init__(self, matcher: _SlowMatch) -> None:
        super().__init__()
        self.matcher = matcher
        self.first_calls: list[tuple[tuple[int, ...], bool, str]] = []

    def helper(self, params: Any, brief: str) -> list[dict[str, Any]]:
        if "on pages " in brief and not brief.startswith(("LEAD", "TAGS:", "PROVE:")):
            pages = tuple(
                int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")
            )
            text = json.dumps(params["messages"][0]["content"])
            with self.lock:
                if all(pages != seen[0] for seen in self.first_calls):
                    self.first_calls.append((pages, self.matcher.answer.is_set(), text))
            if pages == (2, 3):
                self.matcher.answer.set()  # the matcher answers pages 4-5 only now
        return super().helper(params, brief)


def test_helpers_start_beside_the_matcher_and_each_waits_only_for_its_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    matcher = _SlowMatch()
    script = _Watching(matcher)
    harness = _roster(tmp_path, script)
    engine = harness.engine

    def page_matching(page_map: Any) -> Any:
        engine._cross = matcher.cross
        return matcher

    monkeypatch.setattr(engine, "page_matching", page_matching)
    harness.run()

    assert matcher.started == [2, 3, 4, 5] and not matcher.timed_out and matcher.finished
    firsts = {pages: (answered, text) for pages, answered, text in script.first_calls}
    # Pages 2-3 needed no answer: their helpers ran before the matcher had answered pages 4-5.
    assert firsts[(2, 3)][0] is False
    # Pages 4-5 started once their answers were in, and were told the pairs.
    answered, text = firsts[(4, 5)]
    assert answered is True
    assert "PAIRED ACROSS THE UPLOADS" in text, text[-3000:]
    assert "your p.4 with /mnt/user-data/uploads/arch.pdf p.7" in text
    assert "NOT PAIRED" in text and "p.5:" in text
    assert "PAIRED ACROSS" not in firsts[(2, 3)][1]
    # The gap closers and the finishing steps read the final pairs.
    assert engine._cross is matcher.final


# ---- the live card: "Matching pages", then the pages compared --------------------------------------


def test_the_live_card_shows_matching_then_the_pages_compared() -> None:
    live, screen = _live(mode="roster")
    progress = {"phase": "reading", "pages_done": 0, "pages_total": 109}
    live.observe("progress", {"index": 0, "progress": progress})
    live.matching(0, 96)
    live.matching(74, 96)
    phase = screen.last("check_phase")["phase"]
    assert (phase["phase"], phase["step"], phase["done"], phase["total"], phase["unit"]) == (
        "comparing",
        "matching",
        74,
        96,
        "drawings",
    )
    # The pages' count, held while matching (it would hide the matcher's count).
    live.observe("progress", {"index": 0, "progress": {**progress, "pages_done": 12}})
    assert screen.last("check_phase")["phase"]["step"] == "matching"
    live.matched()
    phase = screen.last("check_phase")["phase"]
    assert (phase["step"], phase["done"], phase["total"], phase["unit"]) == (None, 12, 109, "pages")
    live.usual(35, 60, 99, 96)
    assert screen.last("check_phase")["phase"]["usual"] == {
        "low_minutes": 35,
        "high_minutes": 60,
        "pages": 99,
        "matched": 96,
    }
    ChatLiveStateOut.model_validate(live.snapshot())
    _valid(screen)


def test_the_simple_check_goes_back_to_getting_ready_after_matching() -> None:
    live, screen = _live()
    live.matching(0, 3)
    live.matching(3, 3)
    live.matched()
    phase = screen.last("check_phase")["phase"]
    assert (phase["phase"], phase["step"], phase["done"]) == ("getting_ready", None, None)
    _valid(screen)


def test_the_matcher_tells_the_live_card_each_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _two_files(tmp_path)
    _answering(monkeypatch, SURE_A1)
    engine = _Engine(folder)
    seen: list[tuple[str, dict[str, Any]]] = []
    engine.live = LiveCheck(lambda event, data: seen.append((event, data)), mode="roster")
    engine.live.start()

    page_match.run_for_engine(engine, folder, "shop.pdf")  # type: ignore[arg-type]

    steps = [
        (d["phase"]["step"], d["phase"]["done"], d["phase"]["total"])
        for e, d in seen
        if e == "check_phase"
    ]
    assert ("matching", 0, 1) in steps and ("matching", 1, 1) in steps
    assert steps[-1][0] is None  # back to what the check shows otherwise
    for event, data in seen:
        if event == "check_phase":
            ChatLivePhaseOut.model_validate(data["phase"])


# ---- the usual time ----------------------------------------------------------------------------------


def test_the_usual_time_follows_the_set() -> None:
    assert check_time.usual_minutes(0) is None
    low, high = check_time.usual_minutes(60)  # type: ignore[misc]
    assert low <= 25 <= high  # Set 3: about 60 content pages took about 25 minutes
    low, high = check_time.usual_minutes(5)  # type: ignore[misc]
    assert low <= 5 <= high  # Set 1: 5 pages, about 5 minutes
    # 99 pages whose 96 vendor drawings are matched across files: more, and a wider range.
    alone = check_time.usual_minutes(99)
    matched = check_time.usual_minutes(99, 96, page_match.PARALLEL)
    assert alone is not None and matched is not None
    assert matched[0] >= alone[0] and matched[1] > alone[1]
    # Matching 4 at a time (as before) would take longer than 8 at a time; the 99-page set's real
    # run (43 minutes, the matcher 4 at a time before any helper) is inside that range.
    slower = check_time.usual_minutes(99, 96, 4)
    assert slower is not None and slower[1] > matched[1]
    assert slower[0] <= 43 <= slower[1]
    assert all(value % 5 == 0 for value in (*alone, *matched))


def test_the_roster_puts_the_usual_time_on_the_live_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Roster())
    seen: list[tuple[str, dict[str, Any]]] = []
    harness.engine.live = LiveCheck(lambda event, data: seen.append((event, data)), mode="roster")
    harness.engine.live.start()

    harness.run()

    usual = [
        d["phase"].get("usual") for e, d in seen if e == "check_phase" and d["phase"].get("usual")
    ]
    assert usual and usual[0]["pages"] == 4 and usual[0]["matched"] == 0
    assert (usual[0]["low_minutes"], usual[0]["high_minutes"]) == check_time.usual_minutes(4)
