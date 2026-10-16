"""Revision diff between rounds (#1494): pairing from headings, the picture diff, round status.

Every PDF here is synthetic, drawn by the test: a vendor drawing pasted as a `/Stamp` (as on GV's
combined sheets), FreeText headings, and GV's coloured ink. $0, no network, no model."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pikepdf
import pytest

from app.review.assistant.revision_diff import (
    COMPARE_ROUNDS_TOOL,
    compare_rounds,
    diff_pages,
    pair_rounds,
    round_status,
)

PAGE_W, PAGE_H = 600.0, 420.0
DRAW_W, DRAW_H = 320.0, 220.0

#: Seven-segment strokes for a digit in a 10 x 16 box (form units, origin bottom-left).
_SEGMENTS = {
    "a": ((0, 16), (10, 16)),
    "b": ((10, 16), (10, 8)),
    "c": ((10, 8), (10, 0)),
    "d": ((0, 0), (10, 0)),
    "e": ((0, 0), (0, 8)),
    "f": ((0, 8), (0, 16)),
    "g": ((0, 8), (10, 8)),
}
_DIGITS = {
    "3": "abcdg",
    "4": "bcfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
}


def _digits(text: str, x: float, y: float) -> bytes:
    """A number drawn as pen strokes (as the vendor's numbers are: paths, not text)."""
    out = []
    for index, char in enumerate(text):
        left = x + index * 14
        for name in _DIGITS[char]:
            (x0, y0), (x1, y1) = _SEGMENTS[name]
            out.append(f"{left + x0} {y + y0} m {left + x1} {y + y1} l S")
    return ("\n".join(out) + "\n").encode()


def _drawing(number: str = "36", *, extra_box: bool = False, red_ink: bool = False) -> bytes:
    """A cabinet-like line drawing: frame, deterministic varied lines, one dimension number."""
    rng = random.Random(7)
    ops = [b"0 G 1.2 w", f"5 5 {DRAW_W - 10} {DRAW_H - 10} re S".encode()]
    for _ in range(28):
        if rng.random() < 0.5:
            x = rng.uniform(15, DRAW_W - 15)
            y0, y1 = sorted((rng.uniform(15, DRAW_H - 15), rng.uniform(15, DRAW_H - 15)))
            ops.append(f"{x:.1f} {y0:.1f} m {x:.1f} {y1:.1f} l S".encode())
        else:
            y = rng.uniform(15, DRAW_H - 15)
            x0, x1 = sorted((rng.uniform(15, DRAW_W - 15), rng.uniform(15, DRAW_W - 15)))
            ops.append(f"{x0:.1f} {y:.1f} m {x1:.1f} {y:.1f} l S".encode())
    for _ in range(6):
        x, y = rng.uniform(20, DRAW_W - 60), rng.uniform(20, DRAW_H - 50)
        ops.append(
            f"{x:.1f} {y:.1f} {rng.uniform(15, 40):.1f} {rng.uniform(10, 30):.1f} re S".encode()
        )
    ops.append(_digits(number, 150, 190))
    if extra_box:
        ops.append(b"250 30 40 25 re S 250 30 m 290 55 l S")
    if red_ink:
        ops.append(b"1 0 0 RG 2 w 30 150 m 120 150 l S 30 160 m 120 175 l S 0 G 1.2 w")
    return b"\n".join(ops)


def _different_drawing() -> bytes:
    rng = random.Random(99)
    ops = [b"0 G 1.2 w"]
    for _ in range(40):
        x0, y0 = rng.uniform(0, DRAW_W), rng.uniform(0, DRAW_H)
        x1, y1 = rng.uniform(0, DRAW_W), rng.uniform(0, DRAW_H)
        ops.append(f"{x0:.1f} {y0:.1f} m {x1:.1f} {y1:.1f} l S".encode())
    return b"\n".join(ops)


def _page(
    pdf: pikepdf.Pdf,
    stamps: list[tuple[bytes, tuple[float, float, float, float]]],
    notes: list[tuple[str, tuple[float, float, float, float]]],
) -> None:
    """A page with pasted drawings (stamps at PDF rects) and FreeText notes."""
    page = pdf.add_blank_page(page_size=(PAGE_W, PAGE_H))
    annotations = pikepdf.Array()
    for ops, rect in stamps:
        form = pikepdf.Stream(pdf, ops)
        form["/Type"] = pikepdf.Name.XObject
        form["/Subtype"] = pikepdf.Name.Form
        form["/BBox"] = [0, 0, DRAW_W, DRAW_H]
        annotations.append(
            pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.Stamp,
                    Rect=list(rect),
                    F=4,
                    AP=pikepdf.Dictionary(N=form),
                )
            )
        )
    for text, rect in notes:
        annotations.append(
            pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.FreeText,
                    Rect=list(rect),
                    Contents=pikepdf.String(text),
                    DA=pikepdf.String("/Helv 12 Tf 1 0 0 rg"),
                )
            )
        )
    page.obj["/Annots"] = annotations


def _rect(x: float, y: float, scale: float = 1.0) -> tuple[float, float, float, float]:
    return (x, y, x + DRAW_W * scale, y + DRAW_H * scale)


def _save(pdf: pikepdf.Pdf, path: Path) -> Path:
    pdf.save(path)
    return path


@pytest.fixture
def rounds_pdf(tmp_path: Path) -> Path:
    """Page 1: round 1 (dimension 36, GV's red ink, a GV note). Page 2: round 2, shifted on the
    page by (13, 7) points, the dimension now 47 and a new box added. Page 3: an identical copy of
    page 1's drawing. Page 4: an unrelated drawing."""
    pdf = pikepdf.new()
    _page(
        pdf,
        [(_drawing("36", red_ink=True), _rect(100, 120))],
        [
            ("1. Kitchen", (20, 390, 120, 410)),
            ("First set of comments", (180, 95, 360, 115)),
            ("Revise to 38", (330, 300, 400, 315)),
        ],
    )
    _page(
        pdf,
        [(_drawing("47", extra_box=True), _rect(113, 113))],
        [("Second set of comments", (190, 88, 370, 108))],
    )
    _page(pdf, [(_drawing("36"), _rect(100, 120))], [])
    _page(pdf, [(_different_drawing(), _rect(100, 120))], [])
    return _save(pdf, tmp_path / "rounds.pdf")


def _shown(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """A PDF rect (origin bottom-left) as the page shows it (origin top-left)."""
    x0, y0, x1, y1 = rect
    return (x0, PAGE_H - y1, x1, PAGE_H - y0)


def _form_point(rect: tuple[float, float, float, float], x: float, y: float) -> tuple[float, float]:
    """A point in the drawing's own units, as a top-left page point for a stamp at `rect`."""
    scale = (rect[2] - rect[0]) / DRAW_W
    return rect[0] + x * scale, PAGE_H - (rect[1] + y * scale)


def _hits(zone: dict[str, Any], x: float, y: float, side: str = "box_a", slack: float = 6) -> bool:
    box: dict[str, float] = zone[side]
    return bool(
        box["x0"] - slack <= x <= box["x1"] + slack and box["y0"] - slack <= y <= box["y1"] + slack
    )


# ---- pairing -------------------------------------------------------------------------------------


def test_rounds_are_paired_from_their_headings(rounds_pdf: Path) -> None:
    pairs = pair_rounds(rounds_pdf)
    assert len(pairs) == 1
    pair = pairs[0]
    assert (pair["round1_page"], pair["round2_page"]) == (1, 2)
    assert (pair["round_from"], pair["round_to"]) == (1, 2)
    assert pair["title"] == "1. Kitchen"
    assert pair["confidence"] == 0.9
    assert "next page" in pair["reason"]
    # The region is the drawing the heading labels, in top-left points.
    assert pair["round1_region"] == dict(
        zip(("x0", "y0", "x1", "y1"), _shown(_rect(100, 120)), strict=True)
    )


def test_two_rounds_side_by_side_on_one_page_pair_on_that_page(tmp_path: Path) -> None:
    pdf = pikepdf.new()
    _page(pdf, [], [("2. Bar", (20, 390, 120, 410))])
    _page(
        pdf,
        [
            (_drawing("36"), (10, 100, 10 + DRAW_W * 0.85, 100 + DRAW_H * 0.85)),
            (_drawing("47"), (310, 100, 310 + DRAW_W * 0.85, 100 + DRAW_H * 0.85)),
        ],
        [
            ("3. Vanity", (20, 390, 120, 410)),
            ("First set of comments", (40, 75, 220, 95)),
            ("Second set of comments", (340, 75, 520, 95)),
        ],
    )
    pairs = pair_rounds(_save(pdf, tmp_path / "side.pdf"))
    assert [(p["round1_page"], p["round2_page"], p["confidence"]) for p in pairs] == [(2, 2, 0.95)]
    assert pairs[0]["title"] == "3. Vanity"  # the section page (no drawing) never names an item
    assert pairs[0]["round1_region"]["x0"] == 10
    assert pairs[0]["round2_region"]["x0"] == 310


def test_a_later_round_under_the_same_title_pairs_and_another_item_does_not(tmp_path: Path) -> None:
    pdf = pikepdf.new()
    _page(
        pdf,
        [(_drawing(), _rect(100, 120))],
        [("4. Island", (20, 390, 120, 410)), ("First set of comments", (180, 95, 360, 115))],
    )
    _page(
        pdf,
        [(_drawing(), _rect(100, 120))],
        [("5. Desk", (20, 390, 120, 410)), ("First set of comments", (180, 95, 360, 115))],
    )
    _page(
        pdf,
        [(_drawing(), _rect(100, 120))],
        [("4. Island", (20, 390, 120, 410)), ("Round 2", (180, 95, 360, 115))],
    )
    pairs = pair_rounds(_save(pdf, tmp_path / "later.pdf"))
    assert [(p["round1_page"], p["round2_page"], p["confidence"]) for p in pairs] == [(1, 3, 0.75)]


def test_a_set_without_round_headings_has_no_pairs(tmp_path: Path) -> None:
    pdf = pikepdf.new()
    _page(pdf, [(_drawing(), _rect(100, 120))], [("1. Kitchen", (20, 390, 120, 410))])
    assert pair_rounds(_save(pdf, tmp_path / "single.pdf")) == []


# ---- the picture diff ----------------------------------------------------------------------------


def test_a_shifted_round_lines_up_and_only_the_real_changes_are_zones(
    rounds_pdf: Path, tmp_path: Path
) -> None:
    result = diff_pages(rounds_pdf, 1, 2, pictures_dir=tmp_path / "pictures")
    assert result["aligned"] is True
    assert result["match"] >= 0.8
    assert result["scale"] == pytest.approx(1.0, abs=0.01)
    zones = result["zones"]
    assert zones, "the changed number and the new box must be found"
    round1 = _rect(100, 120)
    # The dimension 36 -> 47 (drawn at form 150..174, 190..206).
    number = _form_point(round1, 162, 198)
    assert any(_hits(zone, *number) for zone in zones)
    # The new box (form 250..290, 30..55) is an added zone.
    new_box = _form_point(round1, 270, 42)
    added = [zone for zone in zones if _hits(zone, *new_box)]
    assert added and added[0]["kind"] == "added"
    # GV's red ink on round 1 (form 30..120, 150..175) is not a change.
    for x, y in ((40, 150), (75, 150), (110, 150), (75, 167)):
        assert not any(_hits(zone, *_form_point(round1, x, y), slack=0) for zone in zones)
    # Nothing else: two zones (the number, the box).
    assert len(zones) == 2
    # Round 2's boxes are round 1's moved by the drawing's shift on the page (13 right, 7 down).
    for zone in zones:
        assert zone["box_b"]["page"] == 2 and zone["box_a"]["page"] == 1
        assert zone["box_b"]["x0"] - zone["box_a"]["x0"] == pytest.approx(13, abs=2)
        assert zone["box_b"]["y0"] - zone["box_a"]["y0"] == pytest.approx(7, abs=2)
        assert zone["area"] > 0
    picture = result["picture"]
    assert (tmp_path / "pictures" / f"{picture['picture_id']}.png").is_file()
    assert picture["width"] > picture["height"] > 0


def test_a_round_pasted_bigger_is_scaled_to_line_up(tmp_path: Path) -> None:
    pdf = pikepdf.new()
    _page(pdf, [(_drawing("36"), _rect(40, 120))], [])
    _page(pdf, [(_drawing("47"), (30, 40, 30 + DRAW_W * 1.25, 40 + DRAW_H * 1.25))], [])
    result = diff_pages(_save(pdf, tmp_path / "scaled.pdf"), 1, 2)
    assert result["aligned"] is True
    assert result["scale"] == pytest.approx(1.25, abs=0.02)
    assert len(result["zones"]) == 1
    zone = result["zones"][0]
    assert _hits(zone, *_form_point(_rect(40, 120), 162, 198))
    bigger = (30.0, 40.0, 30 + DRAW_W * 1.25, 40 + DRAW_H * 1.25)
    assert _hits(zone, *_form_point(bigger, 162, 198), side="box_b", slack=8)


def test_the_same_drawing_has_no_zones(rounds_pdf: Path) -> None:
    result = diff_pages(rounds_pdf, 3, 2)
    assert result["aligned"] is True
    assert len(result["zones"]) == 2  # 3 is page 1's drawing without GV's ink: same two changes
    same = diff_pages(rounds_pdf, 1, 3)
    assert same["aligned"] is True
    assert same["zones"] == []  # only GV's red ink differs, and it is not the drawing


def test_drawings_that_do_not_line_up_are_refused(rounds_pdf: Path) -> None:
    result = diff_pages(rounds_pdf, 1, 4)
    assert result["aligned"] is False
    assert result["zones"] == []
    assert "do not line up" in result["reason"]


# ---- round status --------------------------------------------------------------------------------


def _zone(zone_id: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {
        "id": zone_id,
        "kind": "changed",
        "box_a": {"page": 1, "x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "box_b": {"page": 2, "x0": x0 + 5, "y0": y0, "x1": x1 + 5, "y1": y1},
        "area": (x1 - x0) * (y1 - y0),
    }


def _finding(
    fid: str, box: tuple[float, float, float, float] | None, **fields: Any
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": fid,
        "status": "different",
        "architect": '37"',
        "vendor": '46"',
    }
    if box is not None:
        finding["vendor_box"] = dict(zip(("x0", "y0", "x1", "y1"), box, strict=True), page=1)
    finding.update(fields)
    return finding


def test_round_status_per_finding_and_changes_nobody_asked_for() -> None:
    zones = [
        _zone("Z1", 100, 100, 130, 115),
        _zone("Z2", 300, 50, 340, 70),
        _zone("Z3", 500, 300, 520, 320),
    ]
    findings = [
        _finding("F1", (105, 98, 125, 112)),  # spot changed, has values -> re-read
        _finding("F2", (200, 200, 230, 215)),  # nothing changed there
        _finding("F3", (290, 45, 310, 60), architect="", vendor=""),  # changed, nothing to re-read
        _finding("F4", (400, 400, 420, 410), status="same"),  # not a difference, untouched
        _finding("F5", None),  # no vendor box
        _finding("F6", (105, 98, 125, 112), vendor_box=None),
    ]
    findings[5]["vendor_box"] = {"page": 7, "x0": 1, "y0": 1, "x1": 2, "y1": 2}
    result = round_status(findings, zones, page=1, markup=[(495, 330, 505, 340)])
    statuses = {f["finding_id"]: f["round_status"] for f in result["findings"]}
    assert statuses == {
        "F1": "changed_check",
        "F2": "still_wrong",
        "F3": "fixed_in_later_round",
        "F4": "untouched",
        "F5": "not_on_page",
        "F6": "not_on_page",
    }
    f1 = result["findings"][0]
    assert f1["zones"] == ["Z1"]
    assert f1["recheck_box"] == [zones[0]["box_b"]]
    # Z2 has F3, Z3 has GV's markup within 18 points: no unrequested change ...
    assert result["unrequested_changes"] == []
    # ... without the markup, Z3 is a change nobody asked for.
    alone = round_status(findings, zones, page=1)
    assert [z["id"] for z in alone["unrequested_changes"]] == ["Z3"]
    assert result["counts"]["not_on_page"] == 2


# ---- the tool ------------------------------------------------------------------------------------


def test_compare_rounds_tool_schema_is_plain() -> None:
    assert COMPARE_ROUNDS_TOOL["name"] == "compare_rounds"
    schema = COMPARE_ROUNDS_TOOL["input_schema"]
    assert set(schema["properties"]) == {"round1_page", "round2_page", "round1_file", "round2_file"}
    assert schema["additionalProperties"] is False


def test_compare_rounds_finds_the_partner_and_reports_findings(
    rounds_pdf: Path, tmp_path: Path
) -> None:
    round1 = _rect(100, 120)
    nx, ny = _form_point(round1, 162, 198)
    findings = [
        _finding("F1", (nx - 15, ny - 10, nx + 15, ny + 10)),
        _finding("F2", (130, 330, 150, 345)),
    ]
    result = compare_rounds(
        {"round1_page": 1}, {"rounds.pdf": rounds_pdf}, tmp_path / "pics", findings=findings
    )
    assert result["pairing"]["round2_page"] == 2
    statuses = {f["finding_id"]: f["round_status"] for f in result["round_status"]["findings"]}
    assert statuses == {"F1": "changed_check", "F2": "still_wrong"}
    text = result["text"]
    assert text.startswith("Round 1 p.1 vs round 2 p.2: lined up")
    assert "2 change zones" in text
    assert "F1 changed_check" in text
    # The new box has no finding and no GV markup near it: nobody asked for it.
    assert "nobody asked for these" in text
    assert (tmp_path / "pics" / f"{result['picture']['picture_id']}.png").is_file()


def test_compare_rounds_lists_pairs_and_refuses_bad_arguments(
    rounds_pdf: Path, tmp_path: Path
) -> None:
    listed = compare_rounds({}, {"rounds.pdf": rounds_pdf}, tmp_path)
    assert listed["text"].startswith("1 round pairs:")
    assert listed["pairs"][0]["round2_page"] == 2
    with pytest.raises(ValueError, match="no upload named"):
        compare_rounds(
            {"round1_page": 1, "round1_file": "x.pdf"}, {"rounds.pdf": rounds_pdf}, tmp_path
        )
    with pytest.raises(ValueError, match="outside"):
        compare_rounds({"round1_page": 1, "round2_page": 9}, {"rounds.pdf": rounds_pdf}, tmp_path)
    with pytest.raises(ValueError, match="no later round"):
        compare_rounds({"round1_page": 3}, {"rounds.pdf": rounds_pdf}, tmp_path)
    refused = compare_rounds(
        {"round1_page": 1, "round2_page": 4}, {"rounds.pdf": rounds_pdf}, tmp_path
    )
    assert "do not line up" in refused["text"]
    assert refused["diff"]["zones"] == []
