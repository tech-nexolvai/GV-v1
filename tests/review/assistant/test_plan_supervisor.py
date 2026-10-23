"""The supervisor and the pre-flight gate (`plan_supervisor.py`), with a fake AI: never a paid call.

It can only warn or pause; a pause is allowed only for a swapped, broken or other-project file;
Run anyway goes on and records it; every stop and warning is in the answer's `preflight` block.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.review.assistant import plan_supervisor
from app.review.assistant.full_check_plan import CrossPairs
from app.review.assistant.plan_supervisor import decide, gate, look

VENDOR = "vendor.pdf"
ARCH = "architect.pdf"


def _reply(verdict: str, kind: str, why: str) -> str:
    return json.dumps({"verdict": verdict, "kind": kind, "why": why})


def test_a_stop_counts_only_for_the_three_allowed_kinds() -> None:
    assert (
        decide(_reply("stop", "files_swapped", "The architect file is a shop drawing.")).verdict
        == "stop"
    )
    assert decide(_reply("stop", "other_project", "Another job.")).verdict == "stop"
    assert decide(_reply("stop", "broken_file", "Empty file.")).verdict == "stop"
    # Any other stop is only a warning: it never stops the whole check.
    kept = decide(_reply("stop", "other", "Few pages have an architect drawing."))
    assert kept.verdict == "warn" and kept.why == "Few pages have an architect drawing."
    assert decide(_reply("ok", "other", "Fine.")).verdict == "ok"


def test_a_reply_that_is_not_the_form_never_stops() -> None:
    assert decide("I think it is fine").verdict == "not_run"
    assert decide(_reply("maybe", "other", "x")).verdict == "not_run"


def test_a_failed_call_never_stops() -> None:
    def broken(_system: str, _text: str, _schema: Any) -> tuple[str, int]:
        raise RuntimeError("provider down")

    assert look({"vendor_pages": 3}, broken).verdict == "not_run"


def test_the_supervisor_is_given_the_facts_as_json_and_its_cost() -> None:
    seen: dict[str, Any] = {}

    def fake(system: str, text: str, schema: Any) -> tuple[str, int]:
        seen.update(system=system, text=text, schema=schema)
        return _reply("warn", "other", "Most vendor pages have no architect drawing."), 800

    answer = look({"vendor_pages": 96, "pairing": {"paired": 7}}, fake)
    assert answer.verdict == "warn" and answer.cost_micros == 800
    assert '"vendor_pages": 96' in seen["text"] and seen["schema"]["required"] == [
        "verdict",
        "kind",
        "why",
    ]
    assert "never judge" in seen["system"]
    assert answer.out()["cost_usd"] == "0.0008"


# ---- the gate, on a fake engine --------------------------------------------------------------


class _Timings:
    @contextmanager
    def phase(self, _name: str):  # type: ignore[no-untyped-def]
        yield


class _Reply:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _Engine:
    """Just what the gate reads and writes; `_call` is a fake AI that answers `answer`."""

    def __init__(self, folder: Path, *, answer: str | None, money: bool = True) -> None:
        self.tools = SimpleNamespace(
            workspace=SimpleNamespace(set_pack=folder),
            uploads=[
                {"name": VENDOR, "pages": 2, "sha256": "aaa"},
                {"name": ARCH, "pages": 1, "sha256": "bbb"},
            ],
        )
        self.book = SimpleNamespace(main_file=VENDOR)
        self.settings = SimpleNamespace(products=("cabinet",))
        self.provider = SimpleNamespace(name="anthropic")
        self.timings = _Timings()
        self.blocks: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.calls: list[dict[str, Any]] = []
        self.spent = 0
        self.answer = answer
        self.money = money
        self._cross: CrossPairs | None = None

    def _add_block(self, block: dict[str, Any]) -> int:
        self.blocks.append(block)
        return len(self.blocks) - 1

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))

    def stored_blocks(self) -> list[dict[str, Any]]:
        return list(self.blocks)

    def _price(self, _model: Any, tokens: int, out: int) -> int:
        return tokens // 10 + out

    def _fits(self, _estimate: int | None) -> bool:
        return self.money

    def _call(self, **kwargs: Any) -> _Reply:
        self.calls.append(kwargs)
        self.spent += 900
        assert self.answer is not None
        return _Reply(self.answer)


def _pack(tmp_path: Path, architect_sha: str = "bbb") -> Path:
    folder = tmp_path / "pack"
    folder.mkdir(parents=True)
    page_map = {
        "files": [VENDOR, ARCH],
        "upload_roles": {VENDOR: "vendor", ARCH: "architect"},
        "pages": [
            {"file": VENDOR, "page": n, "halves": [{"role": "vendor", "box": [0, 0, 1, 1]}]}
            for n in (1, 2)
        ]
        + [{"file": ARCH, "page": 1, "halves": [{"role": "architect", "box": [0, 0, 1, 1]}]}],
    }
    (folder / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")
    (folder / "status.json").write_text(json.dumps({"state": "ready"}), encoding="utf-8")
    return folder


def _plan() -> Any:
    return SimpleNamespace(skipped=(), pages=2, jobs=())


def test_the_gate_records_its_report_and_a_warning_but_goes_on(tmp_path: Path) -> None:
    engine = _Engine(_pack(tmp_path), answer=_reply("warn", "other", "Only half the pages pair."))
    engine._cross = CrossPairs({1: ((ARCH, 1),)}, {2: "not paired with an architect drawing: x"})
    found = gate(
        engine,
        _plan(),
        None,
        model_name="claude-haiku-5-5",
        run_anyway=False,
        price_micros=5_000_000,
    )  # type: ignore[arg-type]
    assert found is not None and not found.paused
    assert found.supervisor.verdict == "warn" and found.supervisor.cost_micros == 900
    (call,) = engine.calls
    assert call["effort"] == "low" and call["tools"] == () and call["output_format"]
    assert '"price_usd": "5.00"' in call["messages"][0]["content"][0]["text"]
    (block,) = engine.blocks
    assert block["type"] == "preflight" and block["preflight"]["paused"] is False
    assert block["preflight"]["supervisor"]["why"] == "Only half the pages pair."
    assert engine.events[0][0] == "preflight"


def test_a_code_stop_pauses_with_no_ai_needed_and_run_anyway_goes_on(tmp_path: Path) -> None:
    folder = _pack(tmp_path)
    engine = _Engine(folder, answer=None)
    engine.tools.uploads[1]["sha256"] = "aaa"  # the same file as both sides
    found = gate(engine, _plan(), None, model_name=None, run_anyway=False, price_micros=0)  # type: ignore[arg-type]
    assert found is not None and found.paused and engine.calls == []
    assert found.supervisor.verdict == "not_run"
    outcome = plan_supervisor.paused_outcome(engine, found)  # type: ignore[arg-type]
    assert outcome.stopped == "cancelled" and outcome.cost_micros == 0
    assert outcome.text.startswith("Paused before comparing anything: The file uploaded as the")
    assert "Run anyway" in outcome.text
    again = _Engine(folder, answer=None)
    again.tools.uploads[1]["sha256"] = "aaa"
    kept = gate(again, _plan(), None, model_name=None, run_anyway=True, price_micros=0)  # type: ignore[arg-type]
    assert kept is not None and not kept.paused and kept.run_anyway
    assert again.blocks[0]["preflight"]["stops"] == ["files_swapped"]


def test_the_supervisor_may_pause_only_for_an_allowed_kind(tmp_path: Path) -> None:
    folder = _pack(tmp_path)
    engine = _Engine(folder, answer=_reply("stop", "other_project", "The files are two jobs."))
    found = gate(
        engine, _plan(), None, model_name="claude-haiku-5-5", run_anyway=False, price_micros=0
    )  # type: ignore[arg-type]
    assert found is not None and found.paused
    assert found.reasons() == ["The files are two jobs."]
    other = _Engine(folder, answer=_reply("stop", "other", "I do not like it."))
    kept = gate(
        other, _plan(), None, model_name="claude-haiku-5-5", run_anyway=False, price_micros=0
    )  # type: ignore[arg-type]
    assert kept is not None and not kept.paused and kept.supervisor.verdict == "warn"


def test_no_money_for_the_look_skips_it(tmp_path: Path) -> None:
    engine = _Engine(_pack(tmp_path), answer=_reply("ok", "other", "Fine."), money=False)
    found = gate(
        engine, _plan(), None, model_name="claude-haiku-5-5", run_anyway=False, price_micros=0
    )  # type: ignore[arg-type]
    assert found is not None and engine.calls == [] and found.supervisor.verdict == "not_run"


def test_pages_waiting_for_the_matcher_count_as_to_ask(tmp_path: Path) -> None:
    engine = _Engine(_pack(tmp_path), answer=None)
    engine._cross = CrossPairs({}, {1: "x", 2: "y"})
    matching = SimpleNamespace(to_answer=frozenset({1, 2}))
    found = gate(engine, _plan(), matching, model_name=None, run_anyway=False, price_micros=0)  # type: ignore[arg-type]
    assert found is not None and not found.paused
    nothing = _Engine(_pack(tmp_path / "b"), answer=None)
    nothing._cross = CrossPairs({}, {1: "x", 2: "y"})
    stopped = gate(nothing, _plan(), None, model_name=None, run_anyway=False, price_micros=0)  # type: ignore[arg-type]
    assert stopped is not None and stopped.paused
    assert [line.id for line in stopped.report.stops] == ["other_project"]


def test_duplicate_notes_only_when_both_pages_are_planned() -> None:
    report = SimpleNamespace(duplicates={3: 1, 5: 4})
    found = SimpleNamespace(report=report)
    assert plan_supervisor.duplicate_notes(found, [1, 2, 3, 5]) == [  # type: ignore[arg-type]
        (3, "the same as page 1: checked there (an exact copy)")
    ]


def test_run_anyway_goes_with_a_full_check_only() -> None:
    import pytest
    from pydantic import ValidationError

    from app.review.assistant.chat_contract import ChatMessageIn

    base = {"model": "claude-opus-5-5", "effort": "high", "thinking": True}
    full = ChatMessageIn.model_validate(
        {**base, "text": "", "mode": "full_check", "run_anyway": True}
    )
    assert full.run_anyway
    with pytest.raises(ValidationError):
        ChatMessageIn.model_validate({**base, "text": "Hello", "mode": "chat", "run_anyway": True})
