"""A step that broke is never a sendable check (#1768, the final review's weakness 1): a crashed
code check or seeding, a labels step that failed, a blind look the runaway guard refused, and a
drawing the work list missed each make the review result S0 ("Check incomplete: not for sending")
with a named reason, never "send it to the vendor". A real chat engine on a made-up set; the
compare calls and the blind look replaced; no model call, no money."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_engine as engine_module
from app.review.assistant import set_pack
from app.review.assistant.chat_engine import CODE_CHECK_NAMES, ChatEngine
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import compare as compare_module
from app.review.assistant.simple_check import run as run_module
from app.review.assistant.simple_check import worklist as worklist_module
from tests.review.assistant.test_set_pack import LISTING, combined_set
from tests.review.assistant.test_simple_run_stop import _agreeing, _fake_compare, _Run

SEND = "send the cloud drawing to the vendor"

#: Each code check of `ChatEngine.add_code_findings`, by where a test breaks it.
_BREAK = {
    "consistency": "add_consistency",
    "architect_items": "add_architect_items",
    "code_pairs": "add_code_pairs",
    "run_fit": "add_run_fit",
    "filler_range": "add_filler_range",
}


def _state(outcome: Any) -> dict[str, Any]:
    block = next(b for b in outcome.blocks if b.get("type") == "package")
    return dict(block["package"]["state"])


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("made up")


def _not_for_sending(outcome: Any, words: str) -> None:
    state = _state(outcome)
    assert words in str(state.get("incomplete")), state
    assert SEND not in outcome.text and "Next: finish the check" in outcome.text


@pytest.mark.parametrize("check", sorted(CODE_CHECK_NAMES))
def test_a_code_check_that_crashes_makes_the_check_not_for_sending(
    check: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=run.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    if check == "ledger":
        monkeypatch.setattr(ChatEngine, "_ledger_findings", _boom)
    else:
        monkeypatch.setattr(engine_module, _BREAK[check], _boom)
    outcome = run.engine.run()
    name = CODE_CHECK_NAMES[check]
    _not_for_sending(outcome, name)
    assert outcome.text.count(f"could not run ({name})") == 1  # named once, not twice
    assert str(_state(outcome)["incomplete"]).count(name) == 1
    assert run.engine.code_checks_failed == [name]


def test_a_whole_check_has_no_broken_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=run.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = run.engine.run()
    assert not _state(outcome).get("incomplete")
    assert SEND in outcome.text and run.engine.code_checks_failed == []


def test_seeding_that_crashes_makes_the_check_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=run.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    monkeypatch.setattr(ChatEngine, "_seed_recorded", _boom)
    _not_for_sending(run.engine.run(), run_module.SEED_STEP)


def test_final_checks_that_crash_make_the_check_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=run.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    monkeypatch.setattr(ChatEngine, "_trust_phase", _boom)
    _not_for_sending(run.engine.run(), run_module.FINAL_STEP)


def test_two_broken_steps_are_both_named_after_the_other_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=run.cancel)
    monkeypatch.setattr(blind_module, "look", lambda *_a: (None, "the call failed"))
    monkeypatch.setattr(engine_module, "add_consistency", _boom)
    monkeypatch.setattr(ChatEngine, "_seed_recorded", _boom)
    note = str(_state(run.engine.run()).get("incomplete"))
    assert note.startswith("1 possible difference could not get their second check")
    assert CODE_CHECK_NAMES["consistency"] in note and run_module.SEED_STEP in note
    assert "2 steps of the check could not run" in note


def test_a_pack_whose_labels_broke_is_never_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "combined.pdf").write_bytes(combined_set())

    def timed_out(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("labels", 2400)

    monkeypatch.setattr(set_pack, "_dims", timed_out)
    folder = set_pack.pack_folder(tmp_path / "chats", LISTING)
    set_pack.build_pack(folder, uploads, LISTING)
    status = set_pack.read_status(folder)
    assert status["state"] == set_pack.READY_WITHOUT_LABELS == run_module.READY_WITHOUT_LABELS
    assert status["labels_error"] == "timed out"
    assert "page_map" in status["done"] and "dims" not in status["done"]


def test_a_pack_without_labels_compares_nothing_and_is_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path)
    status = json.loads((run.pack / "status.json").read_text())
    status["state"] = set_pack.READY_WITHOUT_LABELS
    (run.pack / "status.json").write_text(json.dumps(status), encoding="utf-8")

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("nothing is compared without the labels")

    monkeypatch.setattr(compare_module, "compare_all", no_call)
    outcome = run.engine.run()
    assert outcome.text.startswith(f"**The check could not run: {run_module.LABELS_NOT_READ}.")
    assert outcome.stopped == "error" and _state(outcome).get("incomplete")  # S0
    assert SEND not in outcome.text


def test_a_blind_look_the_guard_refused_is_a_failed_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=run.cancel)
    monkeypatch.setattr(
        blind_module, "look", lambda *_a: (None, "the answer's runaway guard was reached")
    )
    outcome = run.engine.run()
    _not_for_sending(outcome, "1 possible difference could not get their second check")
    assert "the money limit for one answer was reached" in str(_state(outcome)["incomplete"])
    assert "1 possible differences could not get their blind second look" in outcome.text
    overall = next(f for f in run.findings(outcome) if f["vendor"] == '45 1/4"')
    assert overall["status"] != "different"  # never red


def test_a_drawing_the_work_list_missed_is_reported_and_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=run.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    made = worklist_module.build_worklist

    def missing_one(*args: Any, **kwargs: Any) -> Any:
        return made(*args, **kwargs).model_copy(update={"uncovered": (4,)})

    monkeypatch.setattr(worklist_module, "build_worklist", missing_one)
    outcome = run.engine.run()
    _not_for_sending(outcome, "1 vendor page with a drawing was not compared (page 4")
    assert "**Not compared: 1 vendor page with a drawing was not compared" in outcome.text
    rows = run.engine.extra_gaps
    assert any(row["page"] == 4 and run_module.UNCOVERED_NOTE in row["what"] for row in rows)


def test_a_roster_check_without_labels_or_with_a_crash_is_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The older full checks (roster, lead) go on when the labels step broke, and their code
    checks may crash: each is named once, so the check is S0, never "send it"."""
    run = _Run(tmp_path)
    run.engine.settings = dataclasses.replace(run.engine.settings, full_check_mode="roster")
    status = json.loads((run.pack / "status.json").read_text())
    status["state"] = set_pack.READY_WITHOUT_LABELS
    (run.pack / "status.json").write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(engine_module, "add_run_fit", _boom)
    run.engine.incomplete_note = "an earlier reason"
    run.engine.add_code_findings()
    note = str(run.engine.incomplete_note)
    assert note.startswith("an earlier reason; 2 steps of the check could not run")
    assert note.count(engine_module.LABELS_STEP) == 1
    assert note.count(CODE_CHECK_NAMES["run_fit"]) == 1


def test_a_whole_roster_check_gets_no_note(tmp_path: Path) -> None:
    run = _Run(tmp_path)
    run.engine.settings = dataclasses.replace(run.engine.settings, full_check_mode="roster")
    run.engine.add_code_findings()
    assert run.engine.incomplete_note is None and run.engine.code_checks_failed == []
