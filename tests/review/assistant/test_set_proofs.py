"""The set's proof ledger (#1805): what a chat question proved reaches the set's next full check.

Synthetic values and a tiny test PDF: no network, no paid call, no Docker.

- Only answers code read are kept (a match, a reading); cannot-read and mismatches never are.
- A chat question's proving tools and its ledger entries are kept; a full check's are not.
- A full check replays the kept strings into its reading ledger (code adds them again), and its
  proving step takes a kept value at the finding's spot when its own tools cannot read it.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import set_proofs
from tests.review.assistant.test_full_check_roster import (
    _Roster,
    _roster,
    _run,
    _typed_wrong,
)

BOX = [300.0, 100.0, 360.0, 116.0]


def _verify(result: str, read: str = '46"') -> dict[str, Any]:
    return {"tool": "verify_value", "file": "a.pdf", "page": 3, "box": BOX, "claim": read,
            "side": "vendor", "label": None,
            "answer": {"result": result, "route": "drawn", "read": read, "value": read}}  # fmt: skip


def test_only_what_code_read_is_kept(tmp_path: Path) -> None:
    assert set_proofs.keep_value(tmp_path, _verify("match"), "chat-1")
    assert not set_proofs.keep_value(tmp_path, _verify("cannot_read"), "chat-1")
    assert not set_proofs.keep_value(tmp_path, _verify("mismatch", '45"'), "chat-1")
    unread = {"tool": "read_number", "file": "a.pdf", "page": 3, "box": BOX, "side": "vendor",
              "answer": {"outcome": "unread", "reason": "cut off"}}  # fmt: skip
    assert not set_proofs.keep_value(tmp_path, unread, "chat-1")
    read = {"tool": "read_number", "file": "a.pdf", "page": 4, "box": None, "side": "vendor",
            "answer": {"outcome": "read", "value": "57.125", "text": '57 1/8"', "label": "D7",
                       "box": [10, 10, 30, 20], "agreed": ["ocr", "shape"]}}  # fmt: skip
    assert set_proofs.keep_value(tmp_path, read, "chat-1")
    # The same proof again is kept once.
    assert set_proofs.keep_value(tmp_path, _verify("match"), "chat-2")
    kept = set_proofs.load(tmp_path)["values"]
    assert [(v["page"], v["inches"], v["chat"]) for v in kept] == [
        (4, "57.125", "chat-1"),
        (3, "46", "chat-2"),
    ]
    assert kept[0]["route"] == "ocr+shape" and kept[0]["label"] == "D7"
    assert set_proofs.load(None) == {"version": 1, "values": [], "ledger": []}


def test_a_kept_value_is_found_at_its_spot_only(tmp_path: Path) -> None:
    set_proofs.keep_value(tmp_path, _verify("match"), "chat-1")
    proofs = set_proofs.load(tmp_path)
    at = {"file": "a.pdf", "page": 3, "side": "vendor"}
    assert set_proofs.proved_at(proofs, **at, box=[290, 95, 370, 120], inches=Decimal(46))
    assert not set_proofs.proved_at(proofs, **at, box=[290, 95, 370, 120], inches=Decimal(45))
    assert not set_proofs.proved_at(proofs, **at, box=[500, 95, 570, 120], inches=Decimal(46))
    elsewhere = {**at, "side": "architect"}
    assert not set_proofs.proved_at(proofs, **elsewhere, box=BOX, inches=Decimal(46))


def test_a_chat_questions_proofs_are_kept_and_a_full_checks_are_not(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    folder = engine.tools.workspace.set_pack
    raw = {"page": 3, "side": "vendor", "parts": ["12", "15"], "printed_total": "28"}
    engine._ledger_tool("record_string", raw, "helper:1")  # a full check: not kept
    assert set_proofs.load(folder)["ledger"] == []
    engine.settings = dataclasses.replace(engine.settings, mode="chat")
    engine._ledger_tool("record_string", raw, "lead")
    engine._keep_chat_proof(_verify("match"))
    kept = set_proofs.load(folder)
    assert [e["input"] for e in kept["ledger"]] == [raw]
    assert len(kept["values"]) == 1


def test_a_full_check_replays_the_chat_strings_into_its_ledger(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    folder = engine.tools.workspace.set_pack
    raw = {"page": 3, "side": "vendor", "parts": ["12", "15"], "printed_total": "28"}
    assert set_proofs.keep_ledger_entry(folder, "record_string", raw, "chat-1")
    assert engine.replay_chat_proofs() == 1
    strings = engine.reading_ledger.strings
    assert len(strings) == 1 and strings[0].as_dict()["result"] == "does_not_add_up"


def test_the_proving_step_takes_a_chat_proof_its_own_tools_could_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant.chat_tools import ChatTools

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    finding_id = _typed_wrong(harness)
    finding = next(f for f in engine.book.live() if f["id"] == finding_id)
    box = finding["vendor_box"]
    name = str(engine.tools.uploads[0]["name"])
    proof = {"tool": "read_number", "file": name, "page": 3, "side": "vendor",
             "box": [box["x0"], box["y0"], box["x1"], box["y1"]],
             "answer": {"outcome": "read", "value": "46", "text": '46"',
                        "agreed": ["ocr", "shape"]}}  # fmt: skip
    assert set_proofs.keep_value(engine.tools.workspace.set_pack, proof, "chat-1")
    unread = {"result": "cannot_read", "route": "none", "read": None, "reads": [],
              "reason": "pen strokes", "value": None, "pending": False, "words": "unread"}  # fmt: skip
    monkeypatch.setattr(ChatTools, "verify_answer", lambda *a, **k: dict(unread))

    counts = _run(harness)._prove_values("verifier")

    assert counts.get("chat_proof") == 1
    found = next(f for f in engine.book.live() if f["id"] == finding_id)
    assert found["checks"]["verify_vendor"]["result"] == "match"
    assert found["checks"]["verify_vendor"]["route"] == "drawn"
    assert "chat question" in found["checks"]["verify_vendor"]["words"]
    assert (found["checks"].get("verify_architect") or {}).get("result") != "match"
    assert any(e["tool"] == "chat proof" for e in found["proof_tools"])
    json.dumps(found)  # stored as JSON


def test_a_chat_match_over_a_wide_box_never_proves_a_small_spot_inside_it(tmp_path: Path) -> None:
    """Review of #1812: a verify_value match says only that the value is somewhere in the box it
    was asked about; a finding's smaller spot inside that box is not proved by it."""
    wide = {**_verify("match", '24"'), "box": [100.0, 100.0, 500.0, 400.0]}
    set_proofs.keep_value(tmp_path, wide, "chat-1")
    proofs = set_proofs.load(tmp_path)
    at = {"file": "a.pdf", "page": 3, "side": "vendor", "inches": Decimal(24)}
    assert not set_proofs.proved_at(proofs, **at, box=[290, 240, 310, 260])
    assert set_proofs.proved_at(proofs, **at, box=[99, 99, 501, 401])


def test_a_proof_by_older_readers_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import read_number

    set_proofs.keep_value(tmp_path, _verify("match"), "chat-1")
    proofs = set_proofs.load(tmp_path)
    at = {"file": "a.pdf", "page": 3, "side": "vendor", "box": [290, 95, 370, 120]}
    assert set_proofs.proved_at(proofs, **at, inches=Decimal(46))
    monkeypatch.setattr(read_number, "READER_VERSION", read_number.READER_VERSION + 1)
    assert not set_proofs.proved_at(proofs, **at, inches=Decimal(46))


def test_a_string_the_chat_re_read_replaces_its_misread_in_the_kept_ledger(
    tmp_path: Path,
) -> None:
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    folder = engine.tools.workspace.set_pack
    engine.settings = dataclasses.replace(engine.settings, mode="chat")
    misread = {"page": 3, "side": "vendor", "parts": ['12"', '24"', '12"'], "printed_total": '50"'}
    engine._ledger_tool("record_string", misread, "lead")
    reread = {**misread, "parts": ['13"', '24"', '13"'], "replaces": "S1"}
    engine._ledger_tool("record_string", reread, "lead")
    kept = [e["input"] for e in set_proofs.load(folder)["ledger"]]
    assert kept == [{k: v for k, v in reread.items() if k != "replaces"}]


def test_a_spot_the_readers_read_against_is_never_overridden_by_a_chat_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant.chat_tools import ChatTools

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    finding_id = _typed_wrong(harness)
    finding = next(f for f in engine.book.live() if f["id"] == finding_id)
    box = finding["vendor_box"]
    name = str(engine.tools.uploads[0]["name"])
    proof = {"tool": "read_number", "file": name, "page": 3, "side": "vendor",
             "box": [box["x0"], box["y0"], box["x1"], box["y1"]],
             "answer": {"outcome": "read", "value": "46", "text": '46"',
                        "agreed": ["ocr", "shape"]}}  # fmt: skip
    assert set_proofs.keep_value(engine.tools.workspace.set_pack, proof, "chat-1")
    unread = {"result": "cannot_read", "route": "none", "read": None, "reads": [],
              "reason": "", "value": None, "pending": False, "words": "unread",
              "read_again": True, "read_number_code": "readers_disagree"}  # fmt: skip
    monkeypatch.setattr(ChatTools, "verify_answer", lambda *a, **k: dict(unread))

    counts = _run(harness)._prove_values("verifier")

    assert not counts.get("chat_proof")
    found = next(f for f in engine.book.live() if f["id"] == finding_id)
    assert (found["checks"].get("verify_vendor") or {}).get("result") != "match"
