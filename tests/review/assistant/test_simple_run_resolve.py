"""The resolve loop inside the simple full check (#1735): a real chat engine on a made-up set, the
compare answer "unsure" about a pair code can verify. With the loop on, the AI's evidence (the ends,
in code's words) lets code verify it, the blind look agrees, and it is red, with the loop's rounds
on the finding, saved with the run and replayed at $0; with the loop off nothing changes; after a
stop it stays Not compared. The compare calls, the loop's call and the blind look are replaced; no
model call, no money."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import proof
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import compare as compare_module
from app.review.assistant.simple_check import run as run_module
from app.review.assistant.simple_check.schema import CompareOutcome, CompareResult, Pair
from tests.review.assistant.test_simple_run_stop import FILE, _agreeing, _Pictures, _Run

UNSURE = Pair(
    vendor_label="p2.D1",
    architect_label="p1.D1",
    measures="overall counter width",
    vendor_from="wall_face (left)",
    vendor_to="wall_face (right)",
    architect_from="wall_face (left)",
    architect_to="wall_face (right)",
    verdict="unsure",
    note="the right end is hard to see",
)
EVIDENCE = {
    "kind": "ends",
    "vendor_label": None,
    "architect_label": None,
    "labels": [],
    "vendor_from": "wall_face (left)",
    "vendor_to": "wall_face (right)",
    "architect_from": "wall_face (left)",
    "architect_to": "wall_face (right)",
    "conventions": [],
    "verdict": "different",
    "want": "none",
    "note": "both run wall to wall",
}


class _Reply:
    def __init__(self) -> None:
        self.stop_reason = "end_turn"
        self.usage = {"input_tokens": 8000, "output_tokens": 700}
        self.content: list[dict[str, Any]] = []

    def text(self) -> str:
        return json.dumps(EVIDENCE)


def _compare(monkeypatch: pytest.MonkeyPatch) -> None:
    def compare_all(items: Any, *, source: Any, on_outcome: Any, **_: Any) -> list[Any]:
        done = []
        for item in items:
            source(item)
            labels = [label.ref for label in item.vendor.labels]
            pairs = (UNSURE,) if "p2.D1" in labels else ()
            outcome = CompareOutcome(
                work_item=item.id,
                status="ok",
                result=CompareResult(
                    work_item=item.id,
                    pairs=pairs,
                    other_items=(),
                    unaccounted_labels=tuple(r for r in labels if r != "p2.D1"),
                ),
                asked_model="claude-sonnet-5-5",
                model="claude-sonnet-5-5",
                effort="high",
                attempts=1,
                usage={"input_tokens": 9000, "output_tokens": 3000},
                cost_usd=0.05,
                seconds=1.0,
            )
            on_outcome(outcome)
            done.append(outcome)
        return done

    monkeypatch.setattr(compare_module, "compare_all", compare_all)
    monkeypatch.setattr(compare_module, "PictureSource", _Pictures)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    # The loop's pictures are the engine's zoom crops; this made-up set has no drawings to cut.
    monkeypatch.setattr(run_module._Run, "_crops", lambda _self: None)


def _overall(check: _Run, outcome: Any) -> dict[str, Any]:
    return next(f for f in check.findings(outcome) if f["vendor"] == '45 1/4"')


def test_with_the_loop_on_an_unsure_pair_code_then_verifies_is_red_saved_and_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compare(monkeypatch)
    check = _Run(tmp_path, {"resolve": {"mode": "on"}})
    asked: list[dict[str, Any]] = []
    monkeypatch.setattr(
        check.engine, "_call", lambda **kw: asked.append(kw) or _Reply()  # type: ignore[func-returns-value]
    )
    outcome = check.engine.run()
    assert outcome.stopped is None
    red = _overall(check, outcome)
    assert red["status"] == "different" and proof.proof_group(red) == "clouded", red["why"]
    assert red["simple"]["resolve"][0]["kind"] == "ends"
    assert red["simple"]["resolve"][0]["failed"] == ["verdict:unsure"]
    assert len(asked) == 1 and asked[0]["output_format"]["properties"]["kind"]
    assert "The resolve step looked again at 1 difference" in outcome.text
    assert "1 verified for the blind second look" in outcome.text
    folder = check.run_dir()
    (saved,) = list((folder / "resolve").glob("*/round-1.json"))
    record = json.loads(saved.read_text())
    assert record["proposal"]["kind"] == "ends" and record["status"] == "candidate"
    assert json.loads((folder / "run.json").read_text())["resolve"]["mode"] == "on"

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a replay makes no model call")

    monkeypatch.setattr(blind_module, "look", no_call)
    monkeypatch.setattr(compare_module, "compare_all", no_call)
    replayed = run_module.replay(
        folder, check.pack, main_file=FILE, read=lambda *_a: None, ink=lambda _l: None
    )
    again = next(
        v
        for found in replayed.values()
        for v in found
        if v.vendor is not None and v.vendor.get("id") == "D1" and v.vendor.get("page") == 2
    )
    assert again.outcome == "red" and again.resolve
    off = run_module.replay(
        folder,
        check.pack,
        main_file=FILE,
        read=lambda *_a: None,
        ink=lambda _l: None,
        resolve=run_module.resolve_module.ResolveSettings(),
    )
    assert all(v.outcome != "red" for found in off.values() for v in found)


def test_with_the_loop_off_the_unsure_pair_stays_not_compared_and_nothing_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compare(monkeypatch)
    check = _Run(tmp_path)
    monkeypatch.setattr(check.engine, "_call", lambda **kw: pytest.fail("no loop call when off"))
    outcome = check.engine.run()
    assert _overall(check, outcome)["status"] == "check"
    assert "resolve step" not in outcome.text
    assert not list((check.run_dir() / "resolve").glob("*/*.json"))


def test_a_right_only_loop_never_makes_the_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compare(monkeypatch)
    check = _Run(tmp_path, {"resolve": {"mode": "right_only"}})
    monkeypatch.setattr(check.engine, "_call", lambda **_kw: _Reply())
    outcome = check.engine.run()
    found = _overall(check, outcome)
    assert found["status"] == "check" and "Right only" in found["why"]
