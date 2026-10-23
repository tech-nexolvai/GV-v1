"""The full check's summary line for a whole-drawing look whose answer was not usable (the client
meeting, 2026-10-23): said as what it is ("gave no usable answer; its sizes were still checked"),
never under "Not done within the fixed price", which read as a money problem. Scripted provider:
no paid call."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.full_check_roster import unusable_looks_line


def test_the_line_names_the_pages_plainly() -> None:
    assert unusable_looks_line(["pages 2"]) == (
        "The AI's whole-drawing look of page 2 gave no usable answer; its sizes were still checked."
    )
    assert unusable_looks_line(["pages 4, 5", "pages 9"]) == (
        "The AI's whole-drawing look of pages 4, 5 and 9 gave no usable answer; their sizes were "
        "still checked."
    )
    assert unusable_looks_line(["pages 4, 5", "pages 6"]).startswith(
        "The AI's whole-drawing look of pages 4-6 gave"
    )
    # Words with no page number are kept as they are.
    assert unusable_looks_line(["the first area"]) == (
        "The AI's whole-drawing look of the first area gave no usable answer; its sizes were "
        "still checked."
    )


def test_a_look_with_no_usable_answer_is_not_said_as_a_price_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.review.assistant.chat_fakes import reply, text
    from tests.review.assistant.test_full_check_roster import SONNET, _clean_ink, _Roster, _roster

    class _UnreadableLooks(_Roster):
        def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            if params["system"][0]["text"].startswith("You are an experienced millwork reviewer"):
                self._seen("whole_look", params)
                return reply(text("I could not decide."), model=SONNET)  # no claims to read
            return super().__call__(params)

    _clean_ink(monkeypatch)
    script = _UnreadableLooks()
    harness = _roster(tmp_path, script)
    engine = harness.engine
    engine.settings = dataclasses.replace(engine.settings, whole_look_model=SONNET)

    outcome = harness.run()

    assert script.calls["whole_look"] == 2  # areas 1 and 2: pages 2-3 and 4-5
    assert "gave no readable answer" not in outcome.text
    assert (
        "The AI's whole-drawing look of pages 2-5 gave no usable answer; their sizes were still "
        "checked." in outcome.text
    )
    not_done = [line for line in outcome.text.splitlines() if "within the fixed price" in line]
    assert all("whole-drawing look" not in line for line in not_done)
