"""The simple full check tells the live screen what it does (#1710), in order, on a real engine.

A real chat engine runs the simple full check on the made-up set of `test_simple_run_stop` (the
compare calls and the blind look replaced; no model call, no money), with the live state attached
the way `chat_service` attaches it (its events, and the engine's events it follows):

- the work list at once, then each drawing's start and finish, then code verifying the answers,
  the blind second looks, the proved Wrong, the files: in that order;
- the live Wrong count never passes the final one, and equals it at the end; a candidate is counted
  as waiting for its second look until it is decided; no Wrong is taken back;
- a Stop: how many drawings are still finishing, then none, and the check ends "stopped";
- a replay at $0 sends the same drawings and findings.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import proof
from app.review.assistant.chat_contract import ChatLiveStateOut
from app.review.assistant.live_check import LiveCheck
from app.review.assistant.simple_check import blind as blind_module
from tests.review.assistant.test_live_check import _Screen, _valid
from tests.review.assistant.test_simple_run_stop import _agreeing, _fake_compare, _Run


def _attach(check: _Run) -> tuple[LiveCheck, _Screen]:
    screen = _Screen()
    live = LiveCheck(screen)
    check.engine.live = live
    check.engine._emit = (
        live.observe
    )  # the engine's events, as `chat_service.publish` hands them on
    register = check.engine.tools.register_file
    assert register is not None

    def registered(*args: Any) -> dict[str, Any]:
        shown = register(*args)
        live.observe("file", {"file": shown})  # `chat_service.register_file` sends `file`
        return shown

    object.__setattr__(check.engine.tools, "register_file", registered)
    live.start()
    return live, screen


class _StopButton:
    """Stop as `RunningMessage.stop` presses it: the live screen first, then the cancel flag."""

    def __init__(self, live: LiveCheck, cancel: threading.Event) -> None:
        self._live, self._cancel = live, cancel

    def set(self) -> None:
        self._live.stopping()
        self._cancel.set()


def _first(screen: _Screen, test: Any) -> int:
    return next(n for n, (event, data) in enumerate(screen.events) if test(event, data))


def _phase(name: str, step: str | None = None) -> Any:
    return lambda event, data: (
        event == "check_phase"
        and data["phase"]["phase"] == name
        and (step is None or data["phase"]["step"] == step)
    )


def test_drawings_phases_and_proved_findings_arrive_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    live, screen = _attach(check)
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    live.finish(outcome.stopped)
    assert outcome.stopped is None

    order = [
        _first(screen, _phase("getting_ready")),
        _first(screen, lambda e, _d: e == "check_drawings"),
        _first(screen, _phase("comparing")),
        _first(screen, lambda e, d: e == "check_drawing" and d["drawing"]["status"] == "comparing"),
        _first(screen, lambda e, d: e == "check_drawing" and d["drawing"]["status"] == "answered"),
        _first(screen, _phase("double_checking", "verifying")),
        _first(screen, _phase("double_checking", "second_looks")),
        _first(
            screen,
            lambda e, d: e == "check_finding"
            and d["finding"]["outcome"] == "wrong"
            and d["finding"]["proved_by"] == "checked_twice",
        ),
        _first(screen, _phase("making_files")),
        _first(screen, _phase("done")),
    ]
    assert order == sorted(order), order
    drawings = screen.named("check_drawings")[0]["drawings"]
    assert drawings and all(d["status"] == "waiting" for d in drawings)

    # Honest counts: the live Wrong count never passes the final list's, and ends equal to it.
    final_wrong = sum(
        1
        for block in outcome.blocks
        if block.get("type") == "finding" and proof.proof_group(block["finding"]) == "clouded"
    )
    wrongs = [d["tally"]["wrong"] for d in screen.named("check_tally")]
    assert final_wrong >= 1 and max(wrongs) <= final_wrong and wrongs[-1] == final_wrong
    waiting = [d["tally"]["waiting_second_look"] for d in screen.named("check_tally")]
    assert max(waiting) == 1 and waiting[-1] == 0
    assert live.withdrawn == 0
    # The look's Wrong is shown once code's checks on its recorded finding prove it (never on the
    # look's word alone): it was counted as waiting until then.
    red = next(
        r
        for r in live.snapshot()["findings"]
        if r["outcome"] == "wrong" and r["proved_by"] == "checked_twice"
    )
    assert red["id"] and red["final"]
    shown = _first(screen, lambda e, d: e == "check_finding" and d["finding"]["key"] == red["key"])
    before = [d["tally"] for e, d in screen.events[:shown] if e == "check_tally"]
    assert before[-1]["waiting_second_look"] == 1 and before[-1]["wrong"] == final_wrong - 1
    # The files counted as they were made.
    state = ChatLiveStateOut.model_validate(live.snapshot())
    assert state.counts.files_made == len(check.files) >= 3
    assert state.counts.drawings_done == state.counts.drawings_total == len(drawings)
    assert state.counts.second_looks_done == state.counts.second_looks_total == 1
    _valid(screen)


def test_a_stop_says_what_is_still_finishing_then_ends_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    live, screen = _attach(check)
    _fake_compare(monkeypatch, stop_after=1, cancel=_StopButton(live, check.cancel))  # type: ignore[arg-type]
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    live.finish(outcome.stopped)
    assert outcome.stopped == "cancelled"
    stops = [d["stopping"] for d in screen.named("check_stopping")]
    assert stops[0] == {"in_flight": 1, "not_started": 1}
    assert stops[-1]["in_flight"] == 0
    assert live.snapshot()["stopping"] == {"in_flight": 0, "not_started": 0}
    tiles = {d["id"]: d for d in live.snapshot()["drawings"]}
    never = [d for d in tiles.values() if d["status"] == "failed"]
    assert len(never) == 1 and "stopped" in (never[0]["reason"] or "")
    done = screen.last("check_phase")["phase"]
    assert (done["phase"], done["step"], done["stopping"]) == ("done", "stopped", True)
    # No blind look after a stop: the candidate is Not compared, never red, never left waiting.
    assert live.snapshot()["tally"]["waiting_second_look"] == 0
    assert live.snapshot()["tally"]["wrong"] == sum(
        1
        for block in outcome.blocks
        if block.get("type") == "finding" and proof.proof_group(block["finding"]) == "clouded"
    )
    assert live.withdrawn == 0
    _valid(screen)
