"""Each drawing's scale measured from its own printed dimensions (`view_scales.py`): pure code.

Labels here are made by hand in the shape `dims.json` holds them: a value in inches, a dimension
line, the drawn length (`span_pt`) and the drawing they are on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import view_scales as vs


def _label(
    number: int, inches: float, span: float, x: float, side: str = "vendor", y: float = 100
) -> dict[str, Any]:
    return {
        "id": f"D{number}",
        "inches": str(inches),
        "span_pt": span,
        "side": side,
        "orientation": "h",
        "box": [x, y, x + 10, y + 6],
        "dim_line": [[x - span / 2, y + 8], [x + span / 2, y + 8]],
    }


def test_labels_that_agree_give_one_scale_and_the_odd_one_is_not_drawn_to_its_value() -> None:
    # 1/4" = 1'-0" pasted at two thirds: 0.75 drawn points per inch, so 1.3333 in per point.
    labels = [
        _label(1, 36, 27.0, 100),
        _label(2, 24, 18.05, 140),
        _label(3, 48, 36.0, 180),
        _label(4, 30, 22.4, 220),
        _label(5, 12, 4.5, 170, y=110),  # printed 12", drawn 6": typed over
    ]
    notes = [{"text": '1/4" = 1\'-0"', "box": [120, 160, 180, 170]}]
    scales, checks = vs.page_scales(3, labels, notes)
    assert len(scales) == 1
    scale = scales[0]
    assert scale.side == "vendor" and scale.labels == ("D1", "D2", "D3", "D4")
    assert scale.in_per_pt == pytest.approx(4 / 3, rel=0.01) and scale.spread_percent < 1
    # The printed note says 2/3 in per point: GV resized the drawing.
    assert scale.note == '1/4" = 1\'-0"' and scale.note_agrees is False
    odd = {check["id"]: check for check in checks}["D5"]
    assert odd["drawn_to_value"] is False and odd["off_by"] == pytest.approx(-6, abs=0.1)
    assert {check["id"]: check for check in checks}["D1"]["drawn_to_value"] is True


def test_too_few_labels_or_labels_that_disagree_give_no_scale() -> None:
    two = [_label(1, 36, 27.0, 100), _label(2, 24, 18.0, 140)]
    assert vs.page_scales(1, two)[0] == []
    scattered = [_label(1, 36, 27.0, 100), _label(2, 24, 20.0, 140), _label(3, 48, 30.0, 180)]
    assert vs.page_scales(1, scattered)[0] == []


def test_two_drawings_at_two_scales_and_where_a_point_belongs() -> None:
    labels = [
        *(_label(n, 36, 27.0, 100 + 40 * n, side="architect", y=100) for n in range(1, 4)),
        *(_label(n, 36, 54.0, 100 + 70 * n, side="vendor", y=500) for n in range(4, 7)),
    ]
    scales, _ = vs.page_scales(5, labels)
    assert sorted((s.side, round(s.ratio)) for s in scales) == [("architect", 96), ("vendor", 48)]
    vendor = vs.scale_at(scales, (450, 500, 460, 506))
    assert vendor is not None and vendor.side == "vendor"
    assert vs.scale_at(scales, (900, 900, 910, 906)) is None


def test_the_set_pack_file_round_trips(tmp_path: Path) -> None:
    dims = {
        "pages": [
            {
                "file": "set.pdf",
                "page": 2,
                "labels": [_label(n, 36, 27.0, 100 + 40 * n) for n in range(1, 4)],
                "scale_notes": [],
            }
        ]
    }
    (tmp_path / vs.SCALES_FILE).write_text(json.dumps(vs.set_scales(dims)), encoding="utf-8")
    found = vs.scales_on(tmp_path, "set.pdf", 2)
    assert [(s.side, s.labels) for s in found] == [("vendor", ("D1", "D2", "D3"))]
    assert vs.scales_on(tmp_path, "set.pdf", 3) == [] and vs.scales_on(None, "set.pdf", 2) == []
