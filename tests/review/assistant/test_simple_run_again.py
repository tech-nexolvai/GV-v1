"""Compare them again (#1710, B12): only the drawings a run could not compare are compared now, and
merged with the run's saved answers into one whole result.

A stopped run keeps every paid answer (#1676). A full check that names it (`replay`) with
`compare_missing` uses each saved answer and blind look again at $0, compares only the drawings
it has no answer for (failed, stopped, never started), makes the blind looks it could not make,
and ends as one whole check: every drawing answered, the red proved, charged only for the new
calls. A run that is not in the chat is refused plainly, with no call.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import proof
from app.review.assistant.live_check import LiveCheck
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import compare as compare_module
from app.review.assistant.simple_check import run as run_module
from tests.review.assistant.test_live_check import _Screen
from tests.review.assistant.test_simple_run_stop import _agreeing, _fake_compare, _Run


def _copied(folder: Path, into: _Run) -> None:
    target = into.workspace.root / run_module.SIMPLE_DIR / folder.name
    shutil.copytree(folder, target)


def _statuses(outcome: Any) -> dict[str, str]:
    work = next(b for b in outcome.blocks if b.get("type") == "work_items")
    return {row["id"]: row["status"] for row in work["work_items"]}


def test_only_the_drawings_left_are_compared_and_the_result_is_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=1, cancel=first.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    stopped = first.engine.run()
    assert stopped.stopped == "cancelled"
    answered = {i for i, s in _statuses(stopped).items() if s == "answered"}
    left = {i for i, s in _statuses(stopped).items() if s != "answered"}
    assert answered and left
    folder = first.run_dir()

    again = _Run(tmp_path / "again", {"replay": folder.name, "compare_missing": True})
    _copied(folder, again)
    compared_now = _fake_compare(monkeypatch, stop_after=None, cancel=again.cancel)
    looks: list[str] = []
    monkeypatch.setattr(
        blind_module, "look", lambda *a: looks.append("x") or _agreeing(*a)  # type: ignore[func-returns-value]
    )
    screen = _Screen()
    live = LiveCheck(screen)
    again.engine.live = live
    live.start()
    whole = again.engine.run()
    live.finish(whole.stopped)

    assert whole.stopped is None
    # Only the drawings the stopped run had no answer for were compared (and charged).
    assert set(compared_now) == left
    assert len(again.ledger.rows) == len(left)
    assert set(_statuses(whole).values()) == {"answered"}
    # The look the stop left unmade is made now; the red is proved.
    assert looks == ["x"]
    reds = [f for f in again.findings(whole) if proof.proof_group(f) == "clouded"]
    assert [f["vendor"] for f in reds] == ['45 1/4"']
    assert "Every drawing in the work list was compared" in whole.text
    assert "Stopped" not in whole.text
    # The new run is whole by itself: every answer and the look saved again (replayable at $0).
    work = next(b for b in whole.blocks if b.get("type") == "work_items")
    saved = again.workspace.root / run_module.SIMPLE_DIR / work["run"]
    assert sorted(p.stem for p in (saved / "compare").glob("*.json")) == sorted(answered | left)
    assert len(list((saved / "blind").glob("*.json"))) == 1
    # On the live screen the re-used drawings are answered at once; only the rest are compared.
    comparing = {
        d["drawing"]["id"]
        for d in screen.named("check_drawing")
        if d["drawing"]["status"] == "comparing"
    }
    assert comparing == left
    assert live.snapshot()["tally"]["wrong"] == len(reds)


def test_a_run_that_is_not_in_the_chat_is_refused_with_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path, {"replay": "20261019-000000-missing", "compare_missing": True})

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no model call for a run that is not there")

    monkeypatch.setattr(compare_module, "compare_all", no_call)
    monkeypatch.setattr(compare_module, "PictureSource", lambda *_a: None)
    monkeypatch.setattr(blind_module, "look", no_call)
    outcome = check.engine.run()
    assert outcome.stopped == "error"
    assert "the run to compare again is not in this chat" in outcome.text
    assert check.ledger.rows == [] and outcome.cost_micros == 0


def test_compare_missing_needs_a_run_to_compare_again() -> None:
    from pydantic import ValidationError

    from app.review.assistant.chat_contract import ChatMessageIn

    base = {"model": "claude-opus-5-5", "effort": "high", "thinking": True, "mode": "full_check"}
    with pytest.raises(ValidationError, match="compare_missing goes with replay"):
        ChatMessageIn.model_validate({**base, "compare_missing": True})
    asked = ChatMessageIn.model_validate({**base, "replay": "run-1", "compare_missing": True})
    assert asked.compare_missing is True
    options = run_module.SimpleOptions.of({"replay": "run-1", "compare_missing": True})
    assert options.compare_missing is True
    assert run_module.SimpleOptions.of({"compare_missing": True}).compare_missing is False
