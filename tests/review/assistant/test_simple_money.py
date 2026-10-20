"""The simple check's money holds are real, Stop closes what is streaming, and Continue never pays
twice (#1769, the final review's weaknesses 2 and 3).

- Each compare attempt is held at its whole reply room; a retry or the repair turn holds again
  before it starts, and is refused when it would pass the cap.
- Each blind look is reserved before it starts, so looks running together never pass the cap.
- A stream that breaks midway has its usage recorded (its prompt is billed already).
- Stop closes the compare streams (they are kept where `ChatEngine.stop_streams` sees them) and
  ends the retry and warm-up waits at once.
- Continue of an interrupted simple check replays its saved answers ($0) and compares only the
  drawings left.

Made-up drawings and a scripted provider; no model call, no money."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant.chat_provider import ProviderFailure
from app.review.assistant.chat_service import SIMPLE_CONTINUE_WORDS, _simple_of, resume_of
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import run as run_module
from app.review.assistant.simple_check.compare import (
    CompareOutcome,
    CompareSettings,
    compare_all,
    compare_item,
)
from tests.review.assistant.test_simple_compare import (
    SYSTEM,
    _answer,
    _events,
    _item,
    _pictures,
    _Provider,
)
from tests.review.assistant.test_simple_run_stop import _agreeing, _fake_compare, _Run

DOLLARS = 1_000_000


class _Tracker:
    """`ChatEngine`'s stream registry as compare uses it."""

    def __init__(self) -> None:
        self.open: set[Any] = set()
        self.seen: list[Any] = []
        self.lock = threading.Lock()

    def track_stream(self, stream: Any) -> None:
        with self.lock:
            self.open.add(stream)
            self.seen.append(stream)

    def untrack_stream(self, stream: Any) -> None:
        with self.lock:
            self.open.discard(stream)

    def stop_streams(self) -> None:
        with self.lock:
            streams = list(self.open)
        for stream in streams:
            stream.close()


class _Breaks:
    """A stream that bills its prompt, then breaks (a dropped connection)."""

    def __iter__(self) -> Iterator[dict[str, Any]]:
        yield _events("{}")[0]  # message_start: 9,000 input tokens and 2,000 cached
        raise ConnectionError("connection reset")

    def close(self) -> None:
        pass


class _Hangs:
    """A stream that bills its prompt, then waits until it is closed (a long answer)."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        yield _events("{}")[0]
        if not self.closed.wait(10):
            raise AssertionError("never closed")
        raise ConnectionError("closed")

    def close(self) -> None:
        self.closed.set()


class _OneStream:
    name = "anthropic"

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.opened = 0

    def open_stream(self, _params: Any) -> Any:
        self.opened += 1
        return self.stream


# ---- compare --------------------------------------------------------------------------------------


def test_a_stream_that_breaks_midway_is_recorded_and_charged() -> None:
    outcome = compare_item(
        _item(),
        provider=_OneStream(_Breaks()),  # type: ignore[arg-type]
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(retries=0),
        sleep=lambda _s: None,
    )
    assert outcome.status == "failed"
    assert outcome.usage["input_tokens"] == 9000
    assert outcome.usage["cache_read_input_tokens"] == 2000
    assert outcome.cost_usd > 0


class _BreaksAfterWords:
    """A stream that bills its prompt, streams 3,000 characters of thinking and words, then
    breaks: the service never reported the output, though it is paid for."""

    def __iter__(self) -> Iterator[dict[str, Any]]:
        yield _events("{}")[0]
        yield {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}
        yield {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "x" * 2000},
        }
        yield {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}
        yield {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "y" * 1000},
        }
        raise ConnectionError("connection reset")

    def close(self) -> None:
        pass


def test_a_broken_stream_counts_the_words_it_had_streamed() -> None:
    outcome = compare_item(
        _item(),
        provider=_OneStream(_BreaksAfterWords()),  # type: ignore[arg-type]
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(retries=0),
        sleep=lambda _s: None,
    )
    assert outcome.status == "failed"
    assert outcome.usage["input_tokens"] == 9000
    assert outcome.usage["output_tokens"] >= 3000 // 4  # never less than what was streamed


def test_stop_closes_the_stream_and_its_usage_is_kept() -> None:
    hangs = _Hangs()
    tracker = _Tracker()
    stop = threading.Event()
    done: list[Any] = []
    worker = threading.Thread(
        target=lambda: done.append(
            compare_item(
                _item(),
                provider=_OneStream(hangs),  # type: ignore[arg-type]
                source=_pictures,
                system=SYSTEM,
                settings=CompareSettings(retries=3),
                stop=stop,
                streams=tracker,
            )
        )
    )
    worker.start()
    for _ in range(100):
        if tracker.open:
            break
        time.sleep(0.02)
    assert tracker.open == {hangs}
    stop.set()
    tracker.stop_streams()  # what the reviewer's Stop does (`ChatEngine.stop_streams`)
    worker.join(5)
    assert not worker.is_alive()
    outcome = done[0]
    assert outcome.status == "failed" and "stopped" in (outcome.reason or "")
    assert outcome.attempts == 1  # not tried again after the Stop
    assert outcome.usage["input_tokens"] == 9000  # its prompt is billed: recorded
    assert tracker.open == set()


def test_a_stop_during_the_warm_up_starts_none_of_the_waiting_calls() -> None:
    """The first call streams while the others wait for its first event; the reviewer's Stop
    sets the engine's cancel and closes the stream, which wakes the others before the run's own
    flag follows (#1769 review): none of them opens a stream."""

    class Silent:
        """A call that sends nothing yet (its first event has not come) until it is closed."""

        def __init__(self) -> None:
            self.closed = threading.Event()

        def __iter__(self) -> Iterator[dict[str, Any]]:
            if not self.closed.wait(10):
                raise AssertionError("never closed")
            raise ConnectionError("closed")
            yield {}

        def close(self) -> None:
            self.closed.set()

    class Provider:
        name = "anthropic"

        def __init__(self) -> None:
            self.first = Silent()
            self.opened: list[float] = []

        def open_stream(self, _params: Any) -> Any:
            self.opened.append(time.monotonic())
            return self.first if len(self.opened) == 1 else _Hangs()

    provider = Provider()
    tracker = _Tracker()
    cancel = threading.Event()
    run_stop = threading.Event()  # the watcher would set it only half a second later
    stop = run_module._AnyStop(run_stop, cancel)
    items = [_item(item_id=f"p{n}.v1") for n in range(4)]
    worker = threading.Thread(
        target=lambda: compare_all(
            items,
            provider=provider,  # type: ignore[arg-type]
            source=_pictures,
            system=SYSTEM,
            settings=CompareSettings(retries=3, parallel=4),
            stop=stop,
            streams=tracker,
            warm_wait_s=30,
        )
    )
    worker.start()
    for _ in range(100):
        if tracker.open:
            break
        time.sleep(0.02)
    stopped = time.monotonic()
    cancel.set()
    tracker.stop_streams()
    worker.join(5)
    assert not worker.is_alive()
    assert len(provider.opened) == 1  # only the first call, opened before the Stop
    assert [at for at in provider.opened if at >= stopped] == []


def test_stop_ends_the_retry_wait_at_once() -> None:
    stop = threading.Event()
    provider = _Provider([ProviderFailure("overloaded", status=529, retryable=True)])
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()
    outcome = compare_item(
        _item(),
        provider=provider,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(retries=3),
        stop=stop,
    )
    assert time.monotonic() - started < 3  # the retry wait is at least 20 s
    assert outcome.status == "failed" and "stopped" in (outcome.reason or "")
    assert len(provider.calls) == 1


def test_stop_ends_the_warm_up_wait_at_once() -> None:
    """The first call starts alone; the others wait for its first event (up to 90 s). A Stop
    meanwhile ends the wait: they end stopped, never asked."""
    stop = threading.Event()
    opened: list[str] = []

    class _First:
        name = "anthropic"

        def open_stream(self, params: Any) -> Any:
            opened.append("x")
            return hangs

    class _Silent(_Hangs):
        def __iter__(self) -> Iterator[dict[str, Any]]:
            if not self.closed.wait(10):  # no first event: the others keep waiting
                raise AssertionError("never closed")
            raise ConnectionError("closed")

    hangs = _Silent()
    tracker = _Tracker()
    threading.Timer(0.3, lambda: (stop.set(), tracker.stop_streams())).start()
    started = time.monotonic()
    outcomes = compare_all(
        [_item(), _item(item_id="p7.v2")],
        provider=_First(),  # type: ignore[arg-type]
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(parallel=2),
        stop=stop,
        streams=tracker,
    )
    assert time.monotonic() - started < 5
    assert opened == ["x"]  # the second drawing was never asked
    assert [o.status for o in outcomes] == ["failed", "failed"]
    assert all("stopped" in (o.reason or "") for o in outcomes)


def test_a_retry_is_held_again_and_refused_when_it_would_pass_the_cap() -> None:
    asked: list[tuple[int, bool]] = []

    def no_room(_item: Any, attempts: int, repair: bool) -> str | None:
        asked.append((attempts, repair))
        return "the per-answer cap would be passed"

    provider = _Provider([ProviderFailure("overloaded", status=529, retryable=True), _events("{}")])
    outcome = compare_item(
        _item(),
        provider=provider,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(retries=3),
        sleep=lambda _s: None,
        before_attempt=no_room,
    )
    assert asked == [(1, False)]
    assert len(provider.calls) == 1
    assert outcome.status == "failed"
    assert outcome.reason == "not tried again: the per-answer cap would be passed"


def test_the_repair_turn_is_held_too() -> None:
    asked: list[tuple[int, bool]] = []

    def room(_item: Any, attempts: int, repair: bool) -> str | None:
        asked.append((attempts, repair))
        return None

    provider = _Provider([_events("not the form"), _events(json.dumps(_answer()))])
    outcome = compare_item(
        _item(),
        provider=provider,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(retries=1),
        sleep=lambda _s: None,
        before_attempt=room,
    )
    assert outcome.status == "ok" and outcome.repaired
    assert asked == [(1, True)]


# ---- the run's holds ------------------------------------------------------------------------------


def _engine(cap: int, prices: list[tuple[int, int]]) -> Any:
    def price(_model: Any, tokens: int, out: int) -> int:
        prices.append((tokens, out))
        return DOLLARS

    return SimpleNamespace(
        _price=price,
        _over=lambda estimate: "message" if estimate > cap else None,
    )


def test_a_compare_attempt_is_held_at_its_whole_reply_room_and_each_retry_again() -> None:
    prices: list[tuple[int, int]] = []
    run = run_module._Run(_engine(3 * DOLLARS, prices), run_module.SimpleOptions.of({}))  # type: ignore[arg-type]
    settings = CompareSettings(max_tokens=64000)
    draw = run._guarded(
        lambda _item: [SimpleNamespace(picture=SimpleNamespace(tokens=1000))], settings
    )
    item = _item()
    draw(item)
    assert prices[-1][1] == 64000  # the whole reply room, not a typical answer
    assert run.reserved == DOLLARS
    again = run._again(settings)
    assert again(item, 1, False) is None  # the retry holds once more
    assert run.reserved == 2 * DOLLARS and run.reservations[item.id] == 2 * DOLLARS
    assert again(item, 2, False) is None
    assert again(item, 3, False) == "the per-answer cap would be passed"
    assert run.stop.is_set() and run.cap_hit == "message"
    # The answer lets every hold of the drawing go (`_on_outcome`).
    with run.lock:
        run.reserved -= run.reservations.pop(item.id, 0)
    assert run.reserved == 0


def test_a_drawings_hold_goes_only_after_its_money_is_charged() -> None:
    """Between letting the hold go and charging the answer, a call starting elsewhere would see
    neither and could pass the cap with this one: the charge comes first."""
    run = run_module._Run(_engine(3 * DOLLARS, []), run_module.SimpleOptions.of({}))  # type: ignore[arg-type]
    item = _item()
    run.reserved = 2 * DOLLARS
    run.reservations[item.id] = 2 * DOLLARS
    held_at_charge: list[int] = []
    run._save_outcome = lambda _outcome: None  # type: ignore[method-assign]
    run._charge = lambda _model, _outcome: held_at_charge.append(run.reserved)  # type: ignore[method-assign]
    run._progress = lambda: None  # type: ignore[method-assign]
    run._tell = lambda *_args: None  # type: ignore[method-assign]
    run._on_outcome(
        CompareOutcome(
            work_item=item.id,
            status="failed",
            reason="the model call failed",
            asked_model="claude-sonnet-5-5",
            model="claude-sonnet-5-5",
            effort="high",
            attempts=1,
            usage={"input_tokens": 9000, "output_tokens": 3000},
            cost_usd=0.05,
            seconds=1.0,
        )
    )
    assert held_at_charge == [2 * DOLLARS]  # still held while the money is charged
    assert run.reserved == 0 and item.id not in run.reservations


def test_blind_looks_running_together_never_pass_the_cap_and_wait_for_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each look is reserved before it starts (both attempts at the whole reply room): with room
    for two, never more than two run together, and the others wait for one to end instead of
    being refused while most of the money is unspent (#1769 review)."""
    one = 2 * DOLLARS  # CALL_ATTEMPTS x the price below
    spent = [0]
    engine = SimpleNamespace(
        _price=lambda *_a: DOLLARS,
        _fits=lambda estimate: spent[0] + estimate <= 2 * one + DOLLARS // 2,
    )
    lock = threading.Lock()
    running = [0]
    most = [0]
    made: list[str] = []

    def look(_engine: Any, verified: Any, _item: Any, _settings: Any) -> tuple[Any, str]:
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
            made.append(verified)
        time.sleep(0.05)
        with lock:
            running[0] -= 1
            spent[0] += DOLLARS // 20  # a look really costs far less than its hold
        return "agreed", ""

    monkeypatch.setattr(blind_module, "look", look)
    monkeypatch.setattr(blind_module, "with_blind", lambda v, answer, why, **_kw: (v, answer, why))
    names = [f"c{n}" for n in range(6)]
    done = blind_module.look_all(
        engine,  # type: ignore[arg-type]
        [(name, None) for name in names],  # type: ignore[misc]
        blind_module.BlindSettings(),
    )
    assert sorted(made) == names and all(d[2] == "" for d in done)
    assert most[0] == 2
    assert blind_module.look_hold(engine, blind_module.BlindSettings()) == one  # type: ignore[arg-type]


def test_a_blind_look_that_would_pass_the_cap_alone_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(_price=lambda *_a: DOLLARS, _fits=lambda estimate: estimate <= 0)
    monkeypatch.setattr(blind_module, "look", lambda *_a: pytest.fail("no look is made"))
    monkeypatch.setattr(blind_module, "with_blind", lambda v, answer, why, **_kw: (v, answer, why))
    done = blind_module.look_all(
        engine,  # type: ignore[arg-type]
        [("a", None), ("b", None)],  # type: ignore[list-item]
        blind_module.BlindSettings(),
    )
    assert [d[2] for d in done] == [blind_module.GUARD_WORDS] * 2


def test_a_drawing_that_does_not_fit_waits_for_a_call_to_end_instead_of_stopping_the_check() -> (
    None
):
    """With room for two holds, a third drawing waits while two calls run, and starts once one of
    them is charged (its hold goes): the check is not stopped with most of the money unspent."""
    run = run_module._Run(_engine(2 * DOLLARS, []), run_module.SimpleOptions.of({}))  # type: ignore[arg-type]
    settings = CompareSettings(max_tokens=64000)
    draw = run._guarded(
        lambda _item: [SimpleNamespace(picture=SimpleNamespace(tokens=1000))], settings
    )
    run._tell = lambda *_args: None  # type: ignore[method-assign]
    first, second, third = (_item(item_id=f"p{n}.v1") for n in range(3))
    draw(first)
    draw(second)
    threading.Timer(0.3, run._release, args=(first.id,)).start()
    started = time.monotonic()
    draw(third)
    assert time.monotonic() - started >= 0.25
    assert not run.stop.is_set() and run.cap_hit is None and not run.refused
    assert run.reserved == 2 * DOLLARS


def test_a_drawing_that_would_pass_the_cap_with_nothing_running_is_refused() -> None:
    run = run_module._Run(_engine(DOLLARS // 2, []), run_module.SimpleOptions.of({}))  # type: ignore[arg-type]
    settings = CompareSettings(max_tokens=64000)
    draw = run._guarded(
        lambda _item: [SimpleNamespace(picture=SimpleNamespace(tokens=1000))], settings
    )
    item = _item()
    with pytest.raises(run_module._NotStarted, match="per-answer cap"):
        draw(item)
    assert run.stop.is_set() and run.cap_hit == "message" and item.id in run.refused


def test_the_reviewers_stop_starts_no_new_call_before_the_watcher_sees_it() -> None:
    """Stop sets the engine's cancel; the run's own flag follows twice a second. A drawing taken
    from the queue in between, or a retry, never starts a call (#1769 review)."""
    engine = _engine(10 * DOLLARS, [])
    engine.cancel = threading.Event()
    run = run_module._Run(engine, run_module.SimpleOptions.of({}))  # type: ignore[arg-type]
    settings = CompareSettings(max_tokens=64000)
    drawn: list[str] = []
    draw = run._guarded(lambda item: drawn.append(item.id) or [], settings)
    run._tell = lambda *_args: None  # type: ignore[method-assign]
    engine.cancel.set()
    with pytest.raises(run_module._NotStarted, match="stopped"):
        draw(_item())
    assert drawn == [] and run.stop.is_set()
    assert run._again(settings)(_item(), 1, False) == run_module.STOPPED_WORDS
    assert run.reserved == 0


# ---- Continue -------------------------------------------------------------------------------------


def test_continue_replays_the_saved_answers_and_compares_only_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _Run(tmp_path, {"message_id": "m-1"})
    _fake_compare(monkeypatch, stop_after=1, cancel=first.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    stopped = first.engine.run()
    folder = first.run_dir()
    assert json.loads((folder / "run.json").read_text())["message_id"] == "m-1"
    work = next(b for b in stopped.blocks if b.get("type") == "work_items")["work_items"]
    left = {row["id"] for row in work if row["status"] != "answered"}
    assert left

    # The server restarted; the reviewer pressed Continue on the interrupted answer m-1.
    pack = first.workspace.set_pack
    assert run_module.run_of_message(first.workspace.root, "m-1", pack) == folder.name
    assert run_module.run_of_message(first.workspace.root, "m-2", pack) is None
    job = SimpleNamespace(
        simple={"pilot": None},
        mode="full_check",
        full_check_mode="simple",
        continue_from="m-1",
    )
    options = _simple_of(job, SimpleNamespace(message_id="m-2"), first.workspace.root, pack)  # type: ignore[arg-type]
    assert options == {
        "pilot": None,
        "message_id": "m-2",
        "replay": folder.name,
        "compare_missing": True,
    }

    again = _Run(tmp_path / "again", options)
    shutil.copytree(folder, again.workspace.root / run_module.SIMPLE_DIR / folder.name)
    compared_now = _fake_compare(monkeypatch, stop_after=None, cancel=again.cancel)
    whole = again.engine.run()
    assert set(compared_now) == left  # the saved answers were not paid again
    assert len(again.ledger.rows) == len(left)
    assert whole.stopped is None


def test_continue_on_a_changed_set_pack_runs_the_whole_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saved answers name labels of the pack they were read against: when the drawings or the
    pack changed before Continue (its folder is named from both), they are never replayed."""
    first = _Run(tmp_path, {"message_id": "m-1"})
    _fake_compare(monkeypatch, stop_after=1, cancel=first.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    first.engine.run()
    root = first.workspace.root
    assert run_module.run_of_message(root, "m-1", first.workspace.set_pack) is not None
    other = Path(str(first.workspace.set_pack) + "-other")
    assert run_module.run_of_message(root, "m-1", other) is None
    assert run_module.run_of_message(root, "m-1", None) is None
    job = SimpleNamespace(
        simple={"pilot": None}, mode="full_check", full_check_mode="simple", continue_from="m-1"
    )
    options = _simple_of(job, SimpleNamespace(message_id="m-2"), root, other)  # type: ignore[arg-type]
    assert options == {"pilot": None, "message_id": "m-2"}


def test_a_roster_or_chat_message_keeps_its_options() -> None:
    job = SimpleNamespace(simple=None, mode="chat", full_check_mode="simple", continue_from=None)
    assert _simple_of(job, SimpleNamespace(message_id="m"), Path("/nowhere"), None) is None  # type: ignore[arg-type]


def test_the_resume_words_say_what_a_simple_continue_does() -> None:
    simple = resume_of([], mode="full_check", products=None, simple=True)
    assert simple["redo"] == SIMPLE_CONTINUE_WORDS
    roster = resume_of([], mode="full_check", products=None)
    assert "runs the full check again" in roster["redo"]


def test_the_engine_keeps_compare_streams_where_stop_closes_them(tmp_path: Path) -> None:
    check = _Run(tmp_path)
    engine = check.engine
    hangs = _Hangs()
    engine.track_stream(hangs)
    engine.stop_streams()
    assert hangs.closed.is_set()
    engine.untrack_stream(hangs)
    late = _Hangs()
    check.cancel.set()
    engine.track_stream(late)  # opened after the Stop: closed at once
    assert late.closed.is_set()
