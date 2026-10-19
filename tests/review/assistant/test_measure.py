"""`measure_on_drawing` (#1545): code's ruler on a drawing, $0, no AI.

Synthetic sheets drawn here with reportlab at 1/2" = 1'-0" (3 points per real inch): a base unit
89" tall with a plain box above it, the architect's 8'-11" running from the floor to the top of the
box, and two printed widths to calibrate against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pikepdf
import pytest
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant.chat_tools import ChatTools, new_step
from app.review.assistant.measure import MeasureRefused, measure_on_drawing
from app.review.assistant.sandbox import ChatWorkspace
from tests.review.assistant.chat_fakes import FakeSandbox

W, H = 792.0, 612.0
FLOOR, UNIT_TOP, BOX_TOP = 500.0, 233.0, 179.0  # shown points: 89" unit, 18" box above it
LEFT, MIDDLE, RIGHT = 200.0, 308.0, 380.0  # a 36" unit and a 24" unit beside it


def _line(c: Any, x0: float, y0: float, x1: float, y1: float) -> None:
    """A line in shown points (origin top-left)."""
    c.line(x0, H - y0, x1, H - y1)


def sheet(path: Path, *, red_line_at: float | None = None) -> Path:
    c = canvas.Canvas(str(path), pagesize=(W, H))
    c.setStrokeColorRGB(0, 0, 0)
    c.rect(LEFT, H - FLOOR, MIDDLE - LEFT, FLOOR - UNIT_TOP)  # the unit's own outline
    c.rect(LEFT, H - UNIT_TOP, MIDDLE - LEFT, UNIT_TOP - BOX_TOP)  # a plain box above it
    c.rect(MIDDLE, H - FLOOR, RIGHT - MIDDLE, FLOOR - UNIT_TOP)  # the 24" unit beside it
    # The architect's height: extension lines at the floor and the top of the box, 8'-11".
    _line(c, 320, FLOOR, 350, FLOOR)
    _line(c, 320, BOX_TOP, 350, BOX_TOP)
    _line(c, 340, BOX_TOP, 340, FLOOR)
    # Printed widths: extension lines below the floor.
    for x in (LEFT, MIDDLE, RIGHT):
        _line(c, x, 510, x, 530)
    _line(c, LEFT, 520, RIGHT, 520)
    c.drawString(240, H - 518, "3'-0\"")
    c.drawString(330, H - 518, "2'-0\"")
    if red_line_at is not None:
        c.setStrokeColorRGB(0.9, 0.1, 0.1)
        _line(c, red_line_at, 100, red_line_at, 560)
    c.save()
    return path


CALIBRATE = [
    {"from": {"x": LEFT + 1, "y": 520}, "to": {"x": MIDDLE - 1.5, "y": 520}, "printed": "3'-0\""},
    {"from": {"x": MIDDLE + 1, "y": 521}, "to": {"x": RIGHT + 0.5, "y": 520}, "printed": '24"'},
]


def test_a_height_is_scaled_and_each_end_snaps_to_the_drawn_line(tmp_path: Path) -> None:
    pdf = sheet(tmp_path / "s.pdf")
    measured = measure_on_drawing(
        pdf, 1, {"x": 250, "y": FLOOR + 2}, {"x": 250, "y": UNIT_TOP - 1.5}, CALIBRATE, "v"
    )
    assert measured.value_in == 89
    assert 0 < measured.plus_minus_in <= 1
    assert round(measured.scale_ratio) == 24 and measured.spread_percent < 0.01
    assert all(end.startswith("snapped to") for end in measured.snapped)
    words = measured.words()
    assert words.startswith('about 89" (±') and "checked against 2 printed dims" in words
    assert "never as a Wrong by itself" in words
    # The same answer every time.
    again = measure_on_drawing(
        pdf, 1, {"x": 250, "y": FLOOR + 2}, {"x": 250, "y": UNIT_TOP - 1.5}, CALIBRATE, "v"
    )
    assert again.as_dict() == measured.as_dict()


def test_a_point_with_no_line_near_is_taken_as_given_and_is_less_sure(tmp_path: Path) -> None:
    pdf = sheet(tmp_path / "s.pdf")
    snapped = measure_on_drawing(
        pdf, 1, {"x": LEFT, "y": 450}, {"x": MIDDLE, "y": 450}, CALIBRATE, "h"
    )
    loose = measure_on_drawing(pdf, 1, {"x": 150, "y": 450}, {"x": 258, "y": 450}, CALIBRATE, "h")
    assert snapped.value_in == loose.value_in == 36
    assert loose.snapped == ("as given (no line within 4 pt)",) * 2
    assert loose.plus_minus_in > snapped.plus_minus_in


def _as_combined_sheet(pdf: Path) -> Path:
    """The same sheet as one of GV's combined review sheets: a drawing pasted on it as a stamp."""
    with pikepdf.open(pdf, allow_overwriting_input=True) as document:
        page = document.pages[0]
        appearance = document.make_stream(b"0 0 0 RG 1 w 2 2 36 16 re S")
        appearance["/Type"] = pikepdf.Name.XObject
        appearance["/Subtype"] = pikepdf.Name.Form
        appearance["/BBox"] = [0, 0, 40, 20]
        page.obj["/Annots"] = pikepdf.Array(
            [
                document.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Annot,
                        Subtype=pikepdf.Name.Stamp,
                        Rect=[500, 20, 540, 40],
                        AP=pikepdf.Dictionary(N=appearance),
                    )
                )
            ]
        )
        document.save(pdf)
    return pdf


def test_gv_coloured_markup_on_a_combined_sheet_is_never_snapped_to(tmp_path: Path) -> None:
    pdf = _as_combined_sheet(sheet(tmp_path / "s.pdf", red_line_at=152.0))
    measured = measure_on_drawing(
        pdf, 1, {"x": 150, "y": 450}, {"x": 258, "y": 450}, CALIBRATE, "h"
    )
    assert measured.snapped[0] == "as given (no line within 4 pt)"


def test_a_red_line_on_an_ordinary_drawing_is_the_vendors_and_is_snapped_to(
    tmp_path: Path,
) -> None:
    """The colour rule: colour decides nothing on an ordinary upload (a vendor may draw in red)."""
    pdf = sheet(tmp_path / "s.pdf", red_line_at=152.0)
    measured = measure_on_drawing(
        pdf, 1, {"x": 150, "y": 450}, {"x": 258, "y": 450}, CALIBRATE, "h"
    )
    assert measured.snapped[0].startswith("snapped to")


def test_calibrations_that_disagree_are_refused(tmp_path: Path) -> None:
    pdf = sheet(tmp_path / "s.pdf")
    wrong = [CALIBRATE[0], {**CALIBRATE[1], "printed": '30"'}]
    with pytest.raises(MeasureRefused, match="do not agree on one scale"):
        measure_on_drawing(pdf, 1, {"x": 250, "y": FLOOR}, {"x": 250, "y": UNIT_TOP}, wrong, "v")


@pytest.mark.parametrize(
    ("calibrate", "why"),
    [
        (CALIBRATE[:1], "needs 2 to 8 printed dimensions"),
        ([CALIBRATE[0], {**CALIBRATE[1], "printed": "610 mm"}], "not one length"),
        ("3'-0\"", "must be a list"),
    ],
)
def test_too_few_or_unreadable_calibrations_are_refused(
    tmp_path: Path, calibrate: Any, why: str
) -> None:
    pdf = sheet(tmp_path / "s.pdf")
    with pytest.raises(MeasureRefused, match=why):
        measure_on_drawing(pdf, 1, {"x": 250, "y": FLOOR}, {"x": 250, "y": UNIT_TOP}, calibrate)


def test_the_extent_check_sees_a_dimension_that_runs_to_the_box_above_the_unit(
    tmp_path: Path,
) -> None:
    pdf = sheet(tmp_path / "s.pdf")
    measured = measure_on_drawing(
        pdf,
        1,
        {"x": 250, "y": FLOOR},
        {"x": 250, "y": UNIT_TOP},
        CALIBRATE,
        "v",
        {
            "from": {"x": 335, "y": FLOOR - 1},
            "to": {"x": 335, "y": BOX_TOP + 2},
            "printed": "8'-11\"",
        },
    )
    extent = measured.extent
    assert extent is not None
    assert extent["covers_more"] is True and extent["fits_scale"] is True
    assert extent["printed_in"] == 107 and extent["dimension_scaled_in"] == pytest.approx(107)
    assert extent["beyond_low_in"] == pytest.approx(18) and extent["beyond_high_in"] == 0
    assert 'about 18" above the unit' in extent["words"]
    assert "Your decision" in extent["words"] and "different_extent" in extent["words"]
    assert "box, soffit" in extent["measures"]
    assert measured.value_in == 89


def test_the_extent_check_says_when_a_dimension_spans_the_unit_itself(tmp_path: Path) -> None:
    pdf = sheet(tmp_path / "s.pdf")
    measured = measure_on_drawing(
        pdf,
        1,
        {"x": 250, "y": FLOOR},
        {"x": 250, "y": UNIT_TOP},
        CALIBRATE,
        "v",
        {"from": {"x": 210, "y": FLOOR}, "to": {"x": 210, "y": UNIT_TOP}, "printed": "7'-5\""},
    )
    assert measured.extent is not None
    assert measured.extent["covers_more"] is False
    assert "spans the unit itself" in measured.extent["words"]


def test_the_tool_measures_an_upload_and_keeps_the_extent_for_the_findings(
    tmp_path: Path,
) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    sheet(workspace.uploads / "Vendor set.pdf")
    listing = [{"name": "Vendor set.pdf", "pages": 1, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    tools = ChatTools(workspace, FakeSandbox(workspace), uploads=listing)
    raw = {
        "page": 1,
        "from": {"x": 250, "y": FLOOR},
        "to": {"x": 250, "y": UNIT_TOP},
        "axis": "v",
        "calibrate": CALIBRATE,
        "dimension": {
            "from": {"x": 335, "y": FLOOR},
            "to": {"x": 335, "y": BOX_TOP},
            "printed": "8'-11\"",
        },
    }
    result = tools.measure_on_drawing(raw, new_step("m1", "measure_on_drawing", raw))
    assert not result.is_error
    text = result.content[0]["text"]
    assert text.startswith('about 89" (±') and "spans more than the unit" in text
    assert tools.extents[0]["page"] == 1 and tools.extents[0]["file"] == "Vendor set.pdf"
    assert tools.extents[0]["unit_in"] == 89 and tools.extents[0]["covers_more"] is True

    refused = tools.measure_on_drawing(
        {**raw, "calibrate": CALIBRATE[:1]}, new_step("m2", "measure_on_drawing", raw)
    )
    assert refused.is_error and "needs 2 to 8" in refused.content[0]["text"]
    missing = tools.measure_on_drawing(
        {**raw, "page": 9}, new_step("m3", "measure_on_drawing", raw)
    )
    assert missing.is_error


def test_with_no_calibration_the_drawings_measured_scale_is_used(tmp_path: Path) -> None:
    pdf = sheet(tmp_path / "s.pdf")
    scale = {"in_per_pt": 1 / 3, "from_labels": ["D1", "D2", "D4"], "spread_percent": 0.4}
    measured = measure_on_drawing(
        pdf, 1, {"x": 250, "y": FLOOR + 2}, {"x": 250, "y": UNIT_TOP - 1.5}, None, "v", scale=scale
    )
    assert measured.value_in == 89 and measured.calibrations == ()
    assert "scale measured by code from 3 printed dims of this drawing (D1, D2, D4)" in (
        measured.words()
    )
    with pytest.raises(MeasureRefused, match="no measured scale"):
        measure_on_drawing(pdf, 1, {"x": 250, "y": FLOOR}, {"x": 250, "y": UNIT_TOP}, None, "v")


def test_the_tool_takes_the_scale_from_the_set_pack_when_not_calibrated(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "scales.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pages": [
                    {
                        "file": "Vendor set.pdf",
                        "page": 1,
                        "scales": [
                            {
                                "page": 1,
                                "side": "vendor",
                                "in_per_pt": 1 / 3,
                                "from_labels": ["D1", "D2", "D3"],
                                "spread_percent": 0.2,
                                "box": [150, 150, 450, 560],
                            }
                        ],
                        "labels": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=pack)
    sheet(workspace.uploads / "Vendor set.pdf")
    listing = [{"name": "Vendor set.pdf", "pages": 1, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    tools = ChatTools(workspace, FakeSandbox(workspace), uploads=listing)
    raw = {"page": 1, "from": {"x": 250, "y": FLOOR}, "to": {"x": 250, "y": UNIT_TOP}, "axis": "v"}
    result = tools.measure_on_drawing(raw, new_step("m1", "measure_on_drawing", raw))
    assert not result.is_error
    text = result.content[0]["text"]
    assert text.startswith('about 89" (±') and "scale measured by code from 3" in text
    # Outside every measured scale: code says so and asks for a calibration.
    away = {**raw, "from": {"x": 700, "y": 50}, "to": {"x": 760, "y": 50}, "axis": "h"}
    refused = tools.measure_on_drawing(away, new_step("m2", "measure_on_drawing", away))
    assert refused.is_error and "give calibrate" in refused.content[0]["text"]
