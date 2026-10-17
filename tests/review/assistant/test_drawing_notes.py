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
        _phrase("PULL-OUT DRAWERS FOR", 20, 40),
        _phrase("(2) ADJ. SHELVES", 20, 52),
        _phrase("V-322-CUST", 200, 40),
        _phrase("WD-01", 200, 60),
        _phrase('36"', 120, 100),  # a dimension
        _phrase('SCALE: 1/2" = 1\'-0"', 20, 280),  # a scale note
        _phrase("WD-01", 200, 400),
        _phrase("TOEKICK- MT-02", 20, 420),  # a tag inside a note
        _phrase("Blocking by GC", 20, 440),
    ]
    found = dn.page_notes(5, phrases, HALVES)
    architect, vendor = found["drawings"]
    assert [n["text"] for n in architect["notes"]] == ["PULL-OUT DRAWERS FOR", "(2) ADJ. SHELVES"]
    assert sorted(t["text"] for t in architect["tags"]) == ["V-322-CUST", "WD-01"]
    assert [n["text"] for n in vendor["notes"]] == ["TOEKICK- MT-02", "Blocking by GC"]
    assert sorted(t["text"] for t in vendor["tags"]) == ["MT-02", "WD-01"]
    assert vendor["round"] == 1
    assert found["architect_only_tags"] == ["V-322-CUST"]
    assert found["vendor_only_tags"] == ["MT-02"]


def test_a_page_with_one_side_lists_no_one_sided_tags() -> None:
    found = dn.page_notes(2, [_phrase("V-322-CUST", 20, 40)], HALVES[:1])
    assert found["architect_only_tags"] == [] and found["vendor_only_tags"] == []


def test_the_set_pack_file_round_trips(tmp_path: Path) -> None:
    dims = {"pages": [{"file": "set.pdf", "page": 5, "phrases": [_phrase("V-322-CUST", 20, 40)]}]}
    page_map = {"pages": [{"file": "set.pdf", "page": 5, "halves": HALVES}]}
    (tmp_path / dn.NOTES_FILE).write_text(json.dumps(dn.set_notes(dims, page_map)), "utf-8")
    entry = dn.notes_on(tmp_path, "set.pdf", 5)
    assert entry is not None and entry["architect_only_tags"] == ["V-322-CUST"]
    assert dn.notes_on(tmp_path, "set.pdf", 6) is None and dn.notes_on(None, "set.pdf", 5) is None
