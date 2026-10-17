"""The biggest issues first (#1569): code's impact score and order, the same everywhere.

Synthetic findings, no PDF needed except the report's (made by reportlab), no AI call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pypdf import PdfReader

from app.review.assistant.chat_drawing import IMPACT_NOTE, check_report_pdf
from app.review.assistant.chat_summary import top_issues
from app.review.assistant.impact import (
    biggest_line,
    blocks_with_impacts,
    by_impact,
    impact_of,
    with_impacts,
)


def _f(
    fid: str,
    item: str,
    difference: str | None = None,
    trust: str | None = "likely",
    status: str = "different",
    **more: Any,
) -> dict[str, Any]:
    inches = (
        None
        if difference is None
        else {"architect": "36", "vendor": None, "difference": difference}
    )
    return {
        "id": fid,
        "page": int(fid[1:]),
        "item": item,
        "status": status,
        "trust_level": trust,
        "inches": inches,
        "architect": "x",
        "vendor": "y",
        "source": "architect",
        **more,
    }


def test_a_fabrication_change_scores_above_a_cosmetic_one() -> None:
    overall = impact_of(_f("F1", "Buffet: overall width", "9"))
    tag = impact_of(_f("F2", "Buffet: material tag", "0.25"))
    missing = impact_of(_f("F3", "Pantry: outlet", None, vendor="not shown"))
    right = impact_of(_f("F4", "Buffet: depth", "0", status="same"))

    assert overall is not None and tag is not None and missing is not None
    assert overall["why"] == "changes fabrication (overall size); 9 in off; likely"
    assert tag["why"] == "cosmetic (a tag, label or note); 1/4 in off; likely"
    assert missing["why"].startswith("changes fabrication (a missing or extra part)")
    assert overall["score"] > missing["score"] > tag["score"]
    assert right is None
    # A quarter inch on a height is not cosmetic, and a bigger one counts more.
    small = impact_of(_f("F5", "Counter height", "0.25"))
    big = impact_of(_f("F6", "Counter height", "3"))
    assert small is not None and big is not None and big["score"] > small["score"]
    assert small["why"].startswith("changes fabrication (a height)")


def test_checked_items_come_before_ai_readings_only_then_the_biggest() -> None:
    findings = [
        _f("F1", "Label text", "0.25", trust="confirmed"),
        _f("F2", "Run overall length", "12", trust="to_check"),
        _f("F3", "Sink cut-out width", "2", trust="likely"),
        _f("F4", "Back splash height", "0.5", trust="confirmed"),
        _f("F5", "Top depth", "1", status="check"),
        _f("F6", "Top depth", "0", status="same"),
    ]

    ordered = [f["id"] for f in by_impact(findings)]

    # Wrong first: checked ones by score (sink cut-out, the height, the label), then the AI reading
    # only (however big); then Your decision; then Right.
    assert ordered == ["F3", "F4", "F1", "F2", "F5", "F6"]
    assert [f["id"] for f in top_issues(findings, limit=3)] == ["F3", "F4", "F1"]
    ranked = {f["id"]: f.get("impact") for f in with_impacts(findings)}
    assert [ranked[i]["rank"] for i in ("F3", "F4", "F1", "F2")] == [1, 2, 3, 4]
    assert ranked["F5"]["rank"] == 1 and ranked["F6"] is None
    # The findings keep their own order; only `impact` is added.
    assert [f["id"] for f in with_impacts(findings)] == [f["id"] for f in findings]


def test_blocks_get_their_impact_on_read_and_the_notes_name_the_biggest() -> None:
    blocks = [
        {"type": "text", "text": "x"},
        {"type": "finding", "finding": _f("F1", "Label", "0.25")},
        {"type": "finding", "finding": _f("F2", "Overall width", "4")},
    ]
    out = blocks_with_impacts(blocks)
    assert out[0] == blocks[0]
    assert out[2]["finding"]["impact"]["rank"] == 1 and out[1]["finding"]["impact"]["rank"] == 2
    assert "impact" not in blocks[1]["finding"]  # the stored blocks are not changed
    numbered = list(enumerate(by_impact([b["finding"] for b in blocks[1:]]), start=1))
    assert biggest_line(numbered) == "Biggest first: AI 1 p.2 Overall width · AI 2 p.1 Label"
    assert biggest_line([(1, _f("F1", "x", status="same"))]) is None


def test_the_check_report_lists_whats_wrong_biggest_first(tmp_path: Path) -> None:
    findings = by_impact(
        [
            _f("F1", "Label text", "0.25"),
            _f("F2", "Overall width", "6"),
            _f("F3", "Top depth", "0", status="same"),
        ]
    )
    out = tmp_path / "report.pdf"

    check_report_pdf(out, {"Set": "Synthetic"}, findings, [])

    words = " ".join(page.extract_text() for page in PdfReader(str(out)).pages)
    words = " ".join(words.split())
    assert "Biggest first: " + " ".join(IMPACT_NOTE.split()[:4]) in words
    assert words.index("Overall width") < words.index("Label text")
