"""How a simple full check that did not finish says so (the pipeline audit, 2026-10-19): a check
with drawings not compared (a failed call, a Stop, a pilot, nothing in the products picked) never
says "Next: send the cloud drawing to the vendor", a pilot is never a review result for the vendor,
a Stop is said in this check's own words, and blind looks that could not be made are counted in the
summary. A real chat engine on a made-up set; the compare calls and the blind look replaced; no
model call, no money."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import compare as compare_module
from app.review.assistant.simple_check.schema import CompareOutcome
from tests.review.assistant.test_simple_run_stop import (
    _agreeing,
    _fake_compare,
    _Pictures,
    _Run,
)

SEND = "send the cloud drawing to the vendor"


def _state(outcome: Any) -> dict[str, Any]:
    block = next(b for b in outcome.blocks if b.get("type") == "package")
    return dict(block["package"]["state"])


def test_a_whole_check_still_says_send_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    assert SEND in outcome.text
    assert "Every drawing in the work list was compared (3 drawings)." in outcome.text
    assert not _state(outcome).get("incomplete")


def test_a_pilot_is_not_a_check_for_the_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path, {"pilot": 1})
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    assert "Pilot run: 1 drawings compared; 2 more left out" in outcome.text
    assert "Every drawing in the work list was compared" not in outcome.text
    assert SEND not in outcome.text and "Next: finish the check" in outcome.text
    assert _state(outcome).get("incomplete")
    coverage = next(b for b in outcome.blocks if b.get("type") == "coverage")["coverage"]
    left = [p for p in coverage["pages"] if "pilot run" in str(p.get("note"))]
    assert left and all(p["status"] == "not_opened" for p in left)


def test_failed_drawings_and_a_stop_never_say_send_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=1, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    assert outcome.stopped == "cancelled"
    assert SEND not in outcome.text and "Next: finish the check" in outcome.text
    # The Stop is said in this check's words, not the roster's ("before the AI finished").
    assert "Stopped: 2 of 3 drawings were compared and verified" in outcome.text
    assert "before the AI finished" not in outcome.text


def test_blind_looks_that_could_not_be_made_are_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", lambda *_a: (None, "the call failed"))
    outcome = check.engine.run()
    assert "1 possible differences could not get their blind second look" in outcome.text


def test_nothing_in_the_products_picked_is_said_and_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _Run(tmp_path)
    # Every page prints only countertop words: a cabinet-only check has nothing to compare.
    texts = {
        "files": ["set.pdf"],
        "pages": [
            {"file": "set.pdf", "page": n, "text": "QUARTZ COUNTERTOP\nBACKSPLASH"}
            for n in (1, 2, 3, 4)
        ],
    }
    (check.pack / "page_texts.json").write_text(json.dumps(texts), encoding="utf-8")
    from app.review.assistant.product_scope import ProductScope

    scope = ProductScope.of(("cabinet",))
    check.engine.product_scope = scope
    check.engine.book.product_scope = scope

    def no_call(items: Any, **_: Any) -> list[CompareOutcome]:
        assert list(items) == []
        return []

    monkeypatch.setattr(compare_module, "compare_all", no_call)
    monkeypatch.setattr(compare_module, "PictureSource", _Pictures)
    outcome = check.engine.run()
    assert "Nothing was compared: no drawing of cabinets in this set" in outcome.text
    assert "Every drawing in the work list was compared" not in outcome.text
    assert SEND not in outcome.text
    assert _state(outcome).get("incomplete")


def test_pages_that_are_not_combined_sheets_are_never_a_review_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app test (2026-10-19): a vendor PDF and an architect PDF uploaded apart, as the start
    page asks, gave "Approved" with nothing compared. The work list reads GV's combined sheets
    only; with none, the check says so and is not for sending."""
    check = _Run(tmp_path)
    page_map = json.loads((check.pack / "page_map.json").read_text())
    for entry in page_map["pages"]:
        entry["halves"] = []
    (check.pack / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")
    monkeypatch.setattr(compare_module, "compare_all", lambda items, **_: list(items))
    monkeypatch.setattr(compare_module, "PictureSource", _Pictures)
    outcome = check.engine.run()
    assert "Nothing was compared: no vendor drawing was found on GV's combined sheets" in (
        outcome.text
    )
    assert SEND not in outcome.text and "Approved" not in outcome.text
    assert _state(outcome).get("incomplete")
