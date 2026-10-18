"""Clear words and re-scored badges in the chat's code-made outputs (#1513, $0).

- The trust level `to_check` reads "AI reading only" and the status `check` "Not compared"
  (#1619), so the two never share the words "To check": in the accuracy
  click-through, the CSV, the cloud drawing's notes and legend, and the check report.
- An answer stored before a recalibration is scored again from its stored findings with today's
  points table, nothing written, and marked `rescored` only when the number moved.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.review.assistant import chat_drawing
from app.review.assistant.chat_trust import (
    ACCURACY_CALIBRATION_LINE,
    ACCURACY_POINTS,
    CLOUD_WORDS,
    STATUS_WORDS,
    TRUST_WORDS,
    accuracy,
    findings_csv,
    rescored,
)


def _finding(finding_id: str = "F1", **changes: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": finding_id,
        "page": 2,
        "item": "Vanity top length",
        "architect": '37"',
        "vendor": '36"',
        "status": "different",
        "trust_level": "to_check",
        "evidence_kind": "inferred",
        "checks": {},
        "created_by": "lead",
    }
    finding.update(changes)
    return finding


def _blocks(*findings: dict[str, Any], score: int | None = 78, **stored: Any) -> list[Any]:
    blocks: list[Any] = [{"type": "text", "text": "Done."}]
    blocks += [{"type": "finding", "finding": f} for f in findings]
    blocks.append({"type": "accuracy", "accuracy": {"score": score, "level": "orange", **stored}})
    return blocks


def _stored(blocks: list[Any]) -> dict[str, Any]:
    return next(b["accuracy"] for b in blocks if b["type"] == "accuracy")


# ---- the words ----------------------------------------------------------------------------------


def test_trust_and_status_have_their_own_words() -> None:
    assert TRUST_WORDS == {
        "confirmed": "Confirmed",
        "likely": "Likely",
        "to_check": "AI reading only",
    }
    assert STATUS_WORDS == {
        "different": "Wrong",
        "check": "Not compared",
        "same": "Right",
    }
    assert CLOUD_WORDS == {"wrong": "Wrong", "check": "Not compared", None: "Right"}
    every = [*TRUST_WORDS.values(), *STATUS_WORDS.values(), *CLOUD_WORDS.values()]
    assert not any("to check" in words.casefold() for words in every)


def test_the_accuracy_click_through_says_ai_reading_only_once() -> None:
    score = accuracy([_finding()], [], None)
    how = score["breakdown"][0]["how"]
    assert how == "AI reading only"
    assert "to check" not in how.casefold()
    assert "AI reading only 43 of 45" in ACCURACY_CALIBRATION_LINE
    assert "to check" not in ACCURACY_CALIBRATION_LINE.casefold()
    likely = accuracy([_finding(trust_level="likely")], [], None)
    assert likely["breakdown"][0]["how"] == "Likely, AI reading only"


def test_the_csv_says_the_words_people_read() -> None:
    rows = list(
        csv.reader(
            io.StringIO(
                findings_csv(
                    [_finding(status="check"), _finding("F2", status="same", trust_level=None)]
                ).lstrip("﻿")
            )
        )
    )
    header, first, second = rows
    values = dict(zip(header, first, strict=True))
    # #1619: the Status is where the cloud drawing puts it: nothing to compare here.
    assert values["Status"] == "Not compared" and values["Trust"] == "AI reading only"
    values = dict(zip(header, second, strict=True))
    assert values["Status"] == "Right" and values["Trust"] == ""


def test_the_cloud_drawing_and_report_say_your_decision() -> None:
    note = chat_drawing.note_text(1, _finding(status="check"))
    assert note.startswith("Your decision: Vanity top length")
    for legend in (chat_drawing.LEGEND, chat_drawing.LEGEND_CLOUDED):
        assert "needs your decision" in legend
        assert "to check" not in legend.casefold() and "check:" not in legend.casefold()


# ---- re-scoring an old badge --------------------------------------------------------------------


def test_an_old_badge_is_scored_again_with_todays_points() -> None:
    blocks = _blocks(_finding())
    again = rescored(_stored(blocks), blocks)
    assert again is not None
    expected = round(ACCURACY_POINTS["to_check"]["inferred"] * 100)
    assert again["score"] == expected and again["rescored"] is True
    assert again["level"] == ("green" if expected >= 80 else "orange")
    assert again["breakdown"][0]["how"] == "AI reading only"
    # Nothing written: the stored block is as it was.
    assert _stored(blocks)["score"] == 78


def test_a_badge_already_on_todays_calibration_is_not_marked() -> None:
    blocks = _blocks(_finding(trust_level="confirmed", evidence_kind="observed"), score=100)
    again = rescored(_stored(blocks), blocks)
    assert again is not None and again["score"] == 100 and again["rescored"] is False


def test_dropped_findings_and_cited_values_count_as_when_stored() -> None:
    blocks = _blocks(_finding(), _finding("F2"), score=0)
    blocks[2] = {"type": "finding_dropped", "finding_id": "F2", "reason": "duplicate"}
    blocks.insert(1, {"type": "cite", "cite": {"id": "C1", "value": '36"', "printed": "match"}})
    again = rescored(_stored(blocks), blocks)
    assert again is not None and again["claims"] == 2
    points = sorted(item["points"] for item in again["breakdown"])
    assert points == [ACCURACY_POINTS["to_check"]["inferred"], 1.0]


def test_page_coverage_counts_from_its_block_else_the_stored_factor() -> None:
    coverage = {
        "pages": [
            {"page": 1, "pair_page": None, "status": "compared", "note": None, "by": "lead"},
            {"page": 2, "pair_page": None, "status": "not_opened", "note": None, "by": None},
        ],
        "counts": {"total": 2},
    }
    blocks = _blocks(_finding())
    blocks.insert(1, {"type": "coverage", "coverage": coverage})
    again = rescored(_stored(blocks), blocks)
    assert again is not None and again["coverage_factor"] == 0.5
    assert again["score"] == round(ACCURACY_POINTS["to_check"]["inferred"] * 50)

    # A whole-set answer with no coverage block keeps the factor it was stored with.
    blocks = _blocks(_finding(), coverage_factor=0.5, unknowns=3)
    again = rescored(_stored(blocks), blocks)
    assert again is not None and again["coverage_factor"] == 0.5 and again["unknowns"] == 3
    assert again["score"] == round(ACCURACY_POINTS["to_check"]["inferred"] * 50)


def test_no_badge_stays_none_and_an_unreadable_answer_keeps_its_score() -> None:
    assert rescored(None, _blocks(_finding())) is None
    broken: list[Any] = [{"type": "finding", "finding": {"id": "F1", "checks": "not a dict"}}]
    stored = {"score": 78, "level": "orange"}
    assert rescored(stored, broken) == stored
