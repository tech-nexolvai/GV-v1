"""How a simple full check that did not finish says so (the pipeline audit, 2026-10-19): a check
with drawings not compared (a failed call, a Stop, a pilot, nothing in the products picked) never
says "Next: send the cloud drawing to the vendor", a pilot is never a review result for the vendor,
a Stop is said in this check's own words, and blind looks that could not be made are counted in the
summary. A real chat engine on a made-up set; the compare calls and the blind look replaced; no
model call, no money."""

from __future__ import annotations

import json
import re
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


@pytest.mark.usefixtures("read_twice")
def test_a_whole_check_still_says_send_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check = _Run(tmp_path)
    _fake_compare(monkeypatch, stop_after=None, cancel=check.cancel)
    monkeypatch.setattr(blind_module, "look", _agreeing)
    outcome = check.engine.run()
    assert SEND in outcome.text
    assert "Every drawing in the work list was compared (3 drawings)." in outcome.text
    assert not _state(outcome).get("incomplete")
    # Its own note (the screen's default said "the AI did not finish its own").
    assert outcome.text.rstrip().endswith("*Summary written by GV Review from code's own checks.*")


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
    # Counted like a failed compare call (orchestrator, 2026-10-19): not for sending (S0), in
    # plain words, never "send it".
    state = _state(outcome)
    assert state.get("incomplete") == (
        "1 possible difference could not get their second check (the service did not answer); "
        "check again to settle them"
    )
    assert SEND not in outcome.text and "Next: finish the check" in outcome.text
    # #1811: the page of the possible difference left open is never "same" in the page map.
    coverage = next(b for b in outcome.blocks if b.get("type") == "coverage")["coverage"]
    opened = [p for p in coverage["pages"] if str(p.get("note") or "").startswith("Open: ")]
    assert opened and all(p["status"] == "not_comparable" for p in opened)
    assert "possible difference not settled" in opened[0]["note"]
    assert coverage["counts"]["same"] < sum(
        1 for p in coverage["pages"] if p["status"] != "not_opened"
    )


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


def _uncombined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    check = _Run(tmp_path)
    page_map = json.loads((check.pack / "page_map.json").read_text())
    for entry in page_map["pages"]:
        entry["halves"] = []
    (check.pack / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")
    monkeypatch.setattr(compare_module, "compare_all", lambda items, **_: list(items))
    monkeypatch.setattr(compare_module, "PictureSource", _Pictures)
    return check


def test_nothing_compared_is_one_honest_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A $0 run on a client's plain CAD set (2026-10-19) printed "Checked 6 of 6 pages", "0 wrong,
    0 match" and "Every item could be compared." next to "Nothing was compared": the summary must
    say once, first, that nothing was compared, and nothing that reads as a finished check."""
    outcome = _uncombined(tmp_path, monkeypatch).engine.run()
    text = outcome.text
    first = text.strip().splitlines()[0]
    assert first.startswith("**Nothing was compared: no vendor drawing was found")
    assert text.count("Nothing was compared") == 1
    for misleading in ("Every item could be compared", "Checked ", "0 wrong", "so far"):
        assert misleading not in text, misleading
    assert SEND not in text and "Next: nothing can be sent to the vendor" in text


def test_code_findings_with_nothing_compared_keep_their_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code's own checks may still find something with no drawing compared (a GV rule on a
    vendor-only sheet): their counts stay, under the one sentence, never "every item compared"."""
    engine = _uncombined(tmp_path, monkeypatch).engine
    added: list[str] = []

    def with_code_finding(self: Any) -> None:
        made = self.book.add_code_finding(
            {
                "page": 1,
                "item": "Filler width",
                "category": "cabinet",
                "architect": '1" to 2"',
                "vendor": '4"',
                "status": "check",
                "why": "made up",
                "verified": False,
                "source": "rules",
            },
            created_by="consistency",
        )
        if made is not None:
            added.append(made["id"])

    monkeypatch.setattr(type(engine), "add_code_findings", with_code_finding)
    # A made-up item has no printed spot: the spot guard would take it out.
    from app.review.assistant import chat_engine

    monkeypatch.setattr(chat_engine, "spot_dropped", lambda *_a: [])
    text = engine.run().text
    assert added
    lines = text.strip().splitlines()
    assert lines[0].startswith("**Nothing was compared")
    assert "Every item could be compared" not in text and "Checked " not in text
    # Its counts stay (the made-up hook runs at each place code adds its findings).
    assert any(
        re.fullmatch(r"0 wrong \(proved by code\), 0 right, \d+ not compared\.", x) for x in lines
    )
    assert SEND not in text
