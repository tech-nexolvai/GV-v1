"""The `drawing_facts` tool: code's facts about a page, read from the set pack ($0, no AI)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.review.assistant import dim_pairs, drawing_notes, view_scales
from app.review.assistant.chat_tools import ChatTools, new_step
from app.review.assistant.sandbox import ChatWorkspace
from tests.review.assistant.chat_fakes import FakeSandbox

HALVES: list[dict[str, Any]] = [
    {"box": [0, 0, 600, 300], "role": "architect", "heading": "ID SET", "round": None},
    {"box": [0, 320, 600, 700], "role": "vendor", "heading": "VENDOR", "round": 1},
]


def _label(number: int, inches: float, start: float, y: float, side: str) -> dict[str, Any]:
    span = inches * (2.0 if side == "architect" else 3.0)
    return {
        "id": f"D{number}",
        "text": f'{inches:g}"',
        "inches": str(inches),
        "box": [start + span / 2 - 6, y - 8, start + span / 2 + 6, y - 2],
        "side": side,
        "orientation": "h",
        "route": "text",
        "dim_line": [[start, y], [start + span, y]],
        "extension": [[[start, y + 30], [start, y]], [[start + span, y + 30], [start + span, y]]],
        "span_pt": span,
    }


def _pack(folder: Path) -> None:
    labels = [
        _label(1, 36, 100, 100, "architect"),
        _label(2, 24, 172, 100, "architect"),
        _label(3, 30, 220, 100, "architect"),
        _label(4, 36, 100, 400, "vendor"),
        _label(5, 24, 208, 400, "vendor"),
        _label(6, 30, 280, 400, "vendor"),
    ]
    phrases = [
        {"text": "PULL-OUT DRAWERS FOR", "box": [20, 40, 120, 48], "side": "architect"},
        {"text": "V-322-CUST", "box": [200, 40, 260, 48], "side": "architect"},
        {"text": '1/4" = 1\'-0"', "box": [20, 280, 90, 288], "side": "architect"},
    ]
    dims = {
        "pages": [
            {
                "file": "set.pdf",
                "page": 4,
                "labels": labels,
                "phrases": phrases,
                "scale_notes": [{"text": '1/4" = 1\'-0"', "box": [20, 280, 90, 288]}],
            }
        ]
    }
    page_map = {"pages": [{"file": "set.pdf", "page": 4, "halves": HALVES}]}
    scales = view_scales.set_scales(dims)
    folder.mkdir(parents=True)
    for name, document in (
        (view_scales.SCALES_FILE, scales),
        (dim_pairs.PAIRS_FILE, dim_pairs.set_pairs(dims, scales)),
        (drawing_notes.NOTES_FILE, drawing_notes.set_notes(dims, page_map)),
    ):
        (folder / name).write_text(json.dumps(document), encoding="utf-8")


def _tools(tmp_path: Path, pack: Path | None) -> ChatTools:
    workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=pack)
    (workspace.uploads / "set.pdf").write_bytes(b"%PDF-1.7\n")
    listing = [{"name": "set.pdf", "pages": 9, "kind": "the drawing set"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    return ChatTools(workspace, FakeSandbox(workspace), uploads=listing, page_count=9)


def test_the_tool_lists_scales_pairs_notes_and_one_sided_tags(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _pack(pack)
    tools = _tools(tmp_path, pack)
    raw = {"page": 4}
    result = tools.drawing_facts(raw, new_step("f1", "drawing_facts", raw))
    assert not result.is_error
    text = result.content[0]["text"]
    assert text.startswith("Page 4: code's facts")
    # What the drawings print is fenced as data.
    assert '<drawing_data source="drawing_facts">' in text and text.endswith("</drawing_data>")
    assert "architect's drawing" in text and "about 1:36" in text
    assert 'its printed note 1/4" = 1\'-0" does NOT agree' in text
    assert 'SAME: architect D1 36" vs vendor D4 36"' in text
    assert '"PULL-OUT DRAWERS FOR"' in text
    assert "Architect's tags the vendor's drawing on this page does not print: V-322-CUST" in text
    only = tools.drawing_facts({"page": 4, "facts": ["notes"]}, new_step("f2", "drawing_facts", {}))
    assert "Scale:" not in only.content[0]["text"]


def test_the_tool_refuses_plainly(tmp_path: Path) -> None:
    tools = _tools(tmp_path, None)
    bad = tools.drawing_facts({"page": 4, "facts": ["rounds"]}, new_step("f1", "drawing_facts", {}))
    assert bad.is_error and "facts must be" in bad.content[0]["text"]
    missing = tools.drawing_facts({"page": 0}, new_step("f2", "drawing_facts", {}))
    assert missing.is_error
    none = tools.drawing_facts({"page": 4}, new_step("f3", "drawing_facts", {}))
    assert "no set pack" in none.content[0]["text"]
