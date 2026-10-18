"""The notes and tags each drawing prints (`drawing_notes.py`): pure code on `dims.json`'s phrases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.review.assistant import drawing_notes as dn

HALVES: list[dict[str, Any]] = [
    {"box": [0, 0, 400, 300], "role": "architect", "heading": "ID SET", "round": None},
    {"box": [0, 320, 400, 600], "role": "vendor", "heading": "VENDOR", "round": 1},
]


def _phrase(text: str, x: float, y: float) -> dict[str, Any]:
    return {"text": text, "box": [x, y, x + 60, y + 8], "side": "unknown"}


def test_tags_and_notes_are_sorted_per_drawing_and_one_sided_tags_listed() -> None:
    phrases = [
        _phrase("SOFT-CLOSE DRAWERS FOR", 20, 40),
        _phrase("(3) FIXED SHELVES", 20, 52),
        _phrase("FX-101-CUST", 200, 40),
        _phrase("FN-01", 200, 60),
        _phrase('36"', 120, 100),  # a dimension
        _phrase('SCALE: 1/2" = 1\'-0"', 20, 280),  # a scale note
        _phrase("FN-01", 200, 400),
        _phrase("TOEKICK- MX-02", 20, 420),  # a tag inside a note
        _phrase("Support by others", 20, 440),
    ]
    found = dn.page_notes(5, phrases, HALVES)
    architect, vendor = found["drawings"]
    assert [n["text"] for n in architect["notes"]] == [
        "SOFT-CLOSE DRAWERS FOR",
        "(3) FIXED SHELVES",
    ]
    assert sorted(t["text"] for t in architect["tags"]) == ["FN-01", "FX-101-CUST"]
    assert [n["text"] for n in vendor["notes"]] == ["TOEKICK- MX-02", "Support by others"]
    assert sorted(t["text"] for t in vendor["tags"]) == ["MX-02", "FN-01"]
    assert vendor["round"] == 1
    assert found["architect_only_tags"] == ["FX-101-CUST"]
    assert found["vendor_only_tags"] == ["MX-02"]


def test_a_page_with_one_side_lists_no_one_sided_tags() -> None:
    found = dn.page_notes(2, [_phrase("FX-101-CUST", 20, 40)], HALVES[:1])
    assert found["architect_only_tags"] == [] and found["vendor_only_tags"] == []


def test_the_set_pack_file_round_trips(tmp_path: Path) -> None:
    dims = {"pages": [{"file": "set.pdf", "page": 5, "phrases": [_phrase("FX-101-CUST", 20, 40)]}]}
    page_map = {"pages": [{"file": "set.pdf", "page": 5, "halves": HALVES}]}
    (tmp_path / dn.NOTES_FILE).write_text(json.dumps(dn.set_notes(dims, page_map)), "utf-8")
    entry = dn.notes_on(tmp_path, "set.pdf", 5)
    assert entry is not None and entry["architect_only_tags"] == ["FX-101-CUST"]
    assert dn.notes_on(tmp_path, "set.pdf", 6) is None and dn.notes_on(None, "set.pdf", 5) is None
