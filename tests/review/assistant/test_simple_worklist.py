"""The simple full check's work list (`simple_check/worklist.py`): every vendor drawing becomes an
item with its area's architect drawings, the governing one first, and every page is covered or
skipped with code's reason. Synthetic set packs with made-up values; one test reads a real pack
copy when `GV_SIMPLE_CHECK_PACK` names one (local only, $0)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.full_check_plan import DIVIDER_NOTE
from app.review.assistant.simple_check.worklist import build_worklist, worklist_from

FILE = "set.pdf"


def _half(role: str, box: list[float], round_: int | None = None) -> dict[str, Any]:
    return {"box": box, "role": role, "heading": None, "round": round_}


def _page(
    page: int,
    halves: list[dict[str, Any]],
    *,
    area: str | None = None,
    section: str | None = "1. WALL",
    area_from: int | None = None,
    **more: Any,
) -> dict[str, Any]:
    return {
        "file": FILE,
        "page": page,
        "role": "+".join(sorted({h["role"] for h in halves})) or "general",
        "halves": halves,
        "rounds": sorted({h["round"] for h in halves if h.get("round")}),
        "section": section,
        "area": area,
        "area_from_page": area_from,
        "sheet_numbers": [],
        "headings": [],
        **more,
    }


def _label(n: int, box: list[float], side: str, text: str = '24"') -> dict[str, Any]:
    return {"id": f"D{n}", "text": text, "inches": "24", "box": box, "side": side, "route": "text"}


TOP = [100.0, 50.0, 500.0, 300.0]
BOTTOM = [100.0, 350.0, 500.0, 700.0]


def _set() -> dict[str, Any]:
    """A made-up set: a divider; an area with the architect over round 1, round 2 on the next page
    (area carried), a detail of the area on a third page with a larger scale; an area whose vendor
    page has two drawings; an area the vendor did not submit; a vendor drawing with no architect
    anywhere in its area; a tiny mark; a drawing with no heading."""
    pages = [
        _page(1, [], section=None),
        _page(
            2,
            [_half("architect", TOP), _half("vendor", BOTTOM, 1)],
            area="1. Kitchen",
        ),
        _page(3, [_half("vendor", [50.0, 50.0, 700.0, 700.0], 2)], area_from=2),
        _page(4, [_half("architect", TOP), _half("brand_prototype", BOTTOM)], area_from=2),
        _page(
            5,
            [
                _half("architect", TOP),
                _half("vendor", [100.0, 350.0, 290.0, 700.0], 1),
                _half("vendor", [310.0, 350.0, 500.0, 700.0], 1),
                _half("vendor", [520.0, 600.0, 527.0, 612.0], 1),
            ],
            area="2. Vanity",
        ),
        _page(6, [_half("architect", TOP)], area="3. Bar"),
        _page(7, [_half("vendor", BOTTOM, 1)], area="4. Bench"),
        _page(8, [_half("architect", TOP), _half("general", BOTTOM)], area="5. Shelf"),
    ]
    return {"version": 5, "files": [FILE], "pages": pages}


def _dims() -> dict[str, Any]:
    return {
        "version": 1,
        "files": [FILE],
        "pages": [
            {
                "file": FILE,
                "page": 2,
                "labels": [
                    _label(1, [120.0, 100.0, 140.0, 108.0], "architect"),
                    _label(2, [120.0, 400.0, 140.0, 408.0], "vendor"),
                    # Just outside the vendor drawing's box, still nearest it: it is the vendor's.
                    _label(3, [120.0, 705.0, 140.0, 713.0], "vendor"),
                    # Far from every drawing: nobody's.
                    _label(4, [900.0, 900.0, 920.0, 908.0], "unknown"),
                ],
                "scale_notes": [{"text": '1/4" = 1\'-0"', "box": [120.0, 280.0, 180.0, 288.0]}],
                "phrases": [{"text": "QUARTZ COUNTERTOP", "box": [130.0, 420.0, 200.0, 428.0]}],
            },
            {
                "file": FILE,
                "page": 4,
                "labels": [_label(1, [120.0, 100.0, 140.0, 108.0], "architect")],
                "scale_notes": [{"text": '1 1/2" = 1\'-0"', "box": [120.0, 280.0, 180.0, 288.0]}],
                "phrases": [],
            },
            {
                "file": FILE,
                "page": 5,
                "labels": [
                    _label(1, [120.0, 100.0, 140.0, 108.0], "architect"),
                    _label(2, [150.0, 400.0, 170.0, 408.0], "vendor"),
                    _label(3, [350.0, 400.0, 370.0, 408.0], "vendor"),
                ],
                "scale_notes": [],
                "phrases": [],
            },
        ],
    }


def _notes() -> dict[str, Any]:
    return {
        "version": 1,
        "pages": [
            {
                "file": FILE,
                "page": 4,
                "drawings": [
                    {
                        "role": "architect",
                        "round": None,
                        "heading": None,
                        "box": TOP,
                        "tags": [{"text": "QZ-1"}],
                        "notes": [{"text": "DETAIL 3 - SINK SECTION"}],
                    }
                ],
                "architect_only_tags": [],
                "vendor_only_tags": [],
            },
            {
                "file": FILE,
                "page": 2,
                "drawings": [],
                "architect_only_tags": [{"text": "PL-9"}],
                "vendor_only_tags": [],
            },
        ],
    }


def _worklist() -> Any:
    return worklist_from(_set(), dims=_dims(), notes=_notes(), main_file=FILE)


def test_one_item_per_vendor_drawing_with_rounds() -> None:
    work = _worklist()
    assert [item.id for item in work.items] == ["p2.v1", "p3.v1", "p5.v1", "p5.v2"]
    first, second = work.items[0], work.items[1]
    assert (first.round, first.newest_round, first.later_rounds) == (1, False, ("p3.v1",))
    assert (second.round, second.newest_round, second.earlier_rounds) == (2, True, ("p2.v1",))
    assert second.area == "1. Kitchen" and second.section == "1. WALL"


def test_architect_drawings_of_the_area_the_detail_first() -> None:
    work = _worklist()
    for item in work.items[:2]:
        assert [d.ref for d in item.architect] == ["p4.a1", "p2.a1"]
        assert item.architect[0].detail
        assert item.architect[0].scale_note == '1 1/2" = 1\'-0"'
        assert item.architect[0].tags == ("QZ-1",)
        assert [d.ref for d in item.reference] == ["p4.b1"]
    assert [d.ref for d in work.items[2].architect] == ["p5.a1"]


def test_labels_go_to_the_nearest_drawing_of_their_side() -> None:
    work = _worklist()
    first = work.items[0]
    assert first.vendor_refs == ("p2.D2", "p2.D3")
    assert first.architect_refs == ("p4.D1", "p2.D1")
    assert work.items[2].vendor_refs == ("p5.D2",)
    assert work.items[3].vendor_refs == ("p5.D3",)
    # The crop keeps the label just outside the drawing's box.
    assert first.vendor.crop_box[3] >= 713.0


def test_products_tags_and_hard_items() -> None:
    work = _worklist()
    first = work.items[0]
    assert "countertop" in first.products
    assert first.architect_only_tags == ("PL-9",)
    assert first.hard == ()
    # Page 3's vendor drawing has no labels in dims.json.
    assert work.items[1].hard and "no numbered labels" in work.items[1].hard[0]


def test_skips_have_code_reasons_and_every_page_is_covered() -> None:
    work = _worklist()
    reasons = {(s.page, s.drawing): s.reason for s in work.skipped}
    assert reasons[(1, None)] == DIVIDER_NOTE
    assert "too small to be a drawing" in reasons[(5, "p5.h4")]
    assert "nothing to compare it with" in reasons[(7, "p7.v1")]
    assert "vendor did not submit it" in reasons[(6, "p6.a1")]
    assert "vendor did not submit it" in reasons[(8, "p8.a1")]
    assert "no heading saying whose it is" in reasons[(8, "p8.h2")]
    how = {cover.page: cover.how for cover in work.pages}
    assert how == {
        1: "skipped",
        2: "vendor",
        3: "vendor",
        4: "architect",
        5: "vendor",
        6: "skipped",
        7: "skipped",
        8: "skipped",
    }
    assert work.uncovered == ()


def test_a_page_paired_in_the_plan_check_brings_its_architect_drawing() -> None:
    pages = _set()
    pages["pages"][6]["pair_page"] = 6
    work = worklist_from(pages, dims=_dims(), notes=_notes(), main_file=FILE)
    bench = next(item for item in work.items if item.id == "p7.v1")
    assert [d.ref for d in bench.architect] == ["p6.a1"]


def test_a_page_skipped_in_the_plan_check_and_another_upload() -> None:
    pages = _set()
    pages["pages"][2]["skip"] = "the reviewer chose to skip it (plan check)"
    pages["files"].append("other.pdf")
    pages["pages"].append(
        {**_page(1, [_half("architect", TOP)], area="1. Kitchen"), "file": "other.pdf"}
    )
    work = worklist_from(pages, dims=_dims(), notes=_notes(), main_file=FILE)
    assert "p3.v1" not in [item.id for item in work.items]
    assert any(s.page == 3 and "plan check" in s.reason for s in work.skipped)
    assert any(s.file == "other.pdf" and "another upload" in s.reason for s in work.skipped)


def test_build_from_a_pack_folder(tmp_path: Path) -> None:
    (tmp_path / "page_map.json").write_text(json.dumps(_set()))
    (tmp_path / "dims.json").write_text(json.dumps(_dims()))
    (tmp_path / "notes.json").write_text(json.dumps(_notes()))
    work = build_worklist(tmp_path, FILE)
    # Read as a full check reads it: code's likely answers to the plan check laid over the map
    # (page 7 compared with the architect page before it; page 8's unheaded drawing the vendor's).
    by_id = {item.id: item for item in work.items}
    assert sorted(by_id) == ["p2.v1", "p3.v1", "p5.v1", "p5.v2", "p7.v1", "p8.v1"]
    assert [d.ref for d in by_id["p7.v1"].architect] == ["p6.a1"]
    assert [d.ref for d in by_id["p8.v1"].architect] == ["p8.a1"]
    assert work.uncovered == ()
    with pytest.raises(FileNotFoundError):
        build_worklist(tmp_path / "missing", FILE)


@pytest.mark.skipif(
    not os.environ.get("GV_SIMPLE_CHECK_PACK"), reason="a local copy of a real set pack ($0)"
)
def test_a_real_pack_covers_every_content_page() -> None:
    folder = Path(os.environ["GV_SIMPLE_CHECK_PACK"])
    page_map = json.loads((folder / "page_map.json").read_text(encoding="utf-8"))
    work = build_worklist(folder)
    content = {e["page"] for e in page_map["pages"] if e.get("halves")}
    covered = {c.page for c in work.pages if c.how != "skipped"}
    assert work.uncovered == ()
    assert content <= covered | {s.page for s in work.skipped}
    vendor = {
        (e["page"], n)
        for e in page_map["pages"]
        for n, h in enumerate(e.get("halves") or [])
        if h.get("role") == "vendor"
    }
    accounted = {(item.page, item.vendor.half) for item in work.items}
    for skipped in work.skipped:
        if skipped.drawing and ".h" in skipped.drawing:
            accounted.add((skipped.page, int(skipped.drawing.split(".h")[1]) - 1))
        elif skipped.drawing and "nothing to compare" in skipped.reason:
            accounted |= {v for v in vendor if v[0] == skipped.page}
    assert vendor <= accounted
