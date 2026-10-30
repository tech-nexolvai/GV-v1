"""What code adds to the page reviewer's first message (`reviewer_context`), on made-up values.

- GV's reference pictures: read from the folder's manifest, in order, a job's own examples left
  out, at most 16 and a byte cap; a missing or broken folder is no picture, never an error.
- Which design sheet a tag sits on: a plan, or an elevation, section or detail.
- The sheet's description strip (DESCRIPTION, ITEM NO.) found by its labels, before the right and
  bottom strips; a stone shop's header (drawing number, tag, name) when there is no strip.
- The item number against the design's rooms (symbol-font names read, two-line names joined): the
  same number in another family in the room the sheet names, or the tag in another room of the
  same plan; a fact only when both sides are found.
- The same item on the other file of a joined upload: its sizes (code's reading of the drawn
  numbers when the text has none), models, seams and title; the same number in another family
  only when both sheets name one room.
- The sheet's date against the design's latest revision.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.review.assistant import reviewer_context
from app.review.assistant.reviewer_context import (
    date_fact,
    dates_in,
    model_tokens,
    other_file_facts,
    reference_pictures,
    room_facts,
    room_labels,
    same_item,
    sheet_description,
    sheet_fields,
    title_block,
    view_sheet,
)
from app.review.assistant.set_context import SetContext


def _at(text: str, x: float, y: float, w: float = 40) -> dict[str, Any]:
    return {"text": text, "box": [x, y, x + w, y + 9]}


def _png(path: Path, size: tuple[int, int] = (40, 30)) -> None:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, format="PNG")
    path.write_bytes(out.getvalue())


# ---- GV's reference pictures -------------------------------------------------------------------


def test_reference_pictures_follow_the_manifest_and_leave_this_jobs_examples_out(
    tmp_path: Path,
) -> None:
    for name in ("rule.png", "example.png", "same-job.png"):
        _png(tmp_path / name)
    _png(tmp_path / "big.png", (4000, 1000))
    manifest = [
        {"file": "rule.png", "caption": "Overall: wall to wall.", "group": "rule"},
        {"file": "missing.png", "caption": "Gone.", "group": "rule"},
        {"file": "../rule.png", "caption": "Outside the folder.", "group": "rule"},
        {"file": "rule.png", "caption": "Odd group.", "group": "other"},
        {
            "file": "same-job.png",
            "caption": "From this job.",
            "group": "example",
            "exclude_projects": ["Sample Tower"],
        },
        {"file": "example.png", "caption": "GV asks for the filler size.", "group": "example"},
        {"file": "big.png", "caption": "A wide sheet.", "group": "example"},
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    found = reference_pictures(tmp_path, ["sample tower - shop drawings.pdf"])
    assert [p.caption for p in found] == [
        "Overall: wall to wall.",
        "GV asks for the filler size.",
        "A wide sheet.",
    ]
    assert [p.group for p in found] == ["rule", "example", "example"]
    assert (found[-1].width, found[-1].height) == (1600, 400)  # made smaller
    assert found[0].png.startswith(b"\x89PNG")
    # Another job: its example is kept.
    assert len(reference_pictures(tmp_path, ["Other job.pdf"])) == 4


def test_reference_pictures_are_capped_by_count_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _png(tmp_path / "a.png")
    manifest = [{"file": "a.png", "caption": f"Rule {n}", "group": "rule"} for n in range(20)]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert len(reference_pictures(tmp_path)) == 16
    one = len(reference_pictures(tmp_path)[0].png)
    monkeypatch.setattr(reviewer_context, "MAX_REFERENCE_BYTES", one * 3)
    assert len(reference_pictures(tmp_path)) == 3


def test_a_missing_or_broken_folder_gives_no_picture_and_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert reference_pictures(None) == []
    with caplog.at_level(logging.WARNING):
        assert reference_pictures(tmp_path / "nowhere") == []
    assert "reference pictures could not be read" in caplog.text
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert reference_pictures(tmp_path) == []
    (tmp_path / "manifest.json").write_text('{"file": "a.png"}', encoding="utf-8")
    assert reference_pictures(tmp_path) == []
    (tmp_path / "bad.png").write_bytes(b"not a picture")
    (tmp_path / "manifest.json").write_text(
        json.dumps([{"file": "bad.png", "caption": "Broken.", "group": "rule"}]), encoding="utf-8"
    )
    assert reference_pictures(tmp_path) == []


# ---- the design's sheets ------------------------------------------------------------------------


def test_a_tag_under_an_elevation_title_is_a_view_and_one_in_a_room_is_a_plan() -> None:
    tag = [500.0, 300.0, 540.0, 309.0]
    plan = [_at("XYZ-301T", 500, 300), _at("LOUNGE", 500, 340), _at("GFI", 560, 310)]
    assert not view_sheet(plan, tag)
    elevation = [_at("XYZ-301T", 500, 300), _at("ELEVATION 3", 480, 420, 80)]
    assert view_sheet(elevation, tag)
    # A sheet titled as sections, the tag far from any title.
    titled = [_at("XYZ-301T", 500, 300), _at("CASEWORK SECTIONS", 1000, 750, 120)]
    assert view_sheet(titled, tag)
    both = [_at("XYZ-301T", 500, 300), _at("ENLARGED PLANS AND ELEVATIONS", 1000, 750, 160)]
    assert not view_sheet(both, tag)


def test_the_title_block_is_the_right_or_bottom_strip() -> None:
    found = [_at("PANTRY A", 1100, 400), _at("NOTE", 100, 700), _at("ELEVATION", 300, 300)]
    assert [p["text"] for p in title_block(found, (1224.0, 792.0))] == ["PANTRY A", "NOTE"]


# ---- the item number against the design's rooms --------------------------------------------------


def _design_plan() -> dict[int, list[dict[str, Any]]]:
    return {
        3: [
            _at("XYZ-301T", 200, 300),
            _at("LOUNGE", 200, 340),
            _at("XYZ-302T", 800, 300),
            _at("PANTRY 114", 800, 340),
            _at("SEE ELEVATION", 820, 380, 70),
            _at('36"', 860, 300),
        ]
    }


def test_room_labels_are_short_capitals_never_tags_sizes_or_notes() -> None:
    labels = [room for room, _box in room_labels(_design_plan()[3])]
    assert labels == ["LOUNGE", "PANTRY"]


def _symbol(text: str) -> str:
    """`text` in a symbol font that stores each letter as a private-use character."""
    return "".join(chr(0xF000 + ord(ch)) for ch in text)


def test_room_labels_read_a_symbol_font_join_two_lines_and_skip_turned_text() -> None:
    found = [
        _at(_symbol("STAFF"), 200, 330),
        _at(_symbol("LOUNGE"), 200, 341),
        {"text": "YRTNAP", "box": [600.0, 100.0, 612.0, 160.0]},  # written sideways
        _at("XYZ-301", 200, 300),
    ]
    assert room_labels(found) == [("STAFF LOUNGE", [200.0, 330.0, 240.0, 350.0])]


def _design_rooms() -> dict[int, list[dict[str, Any]]]:
    return {
        # XYZ-301 in the lounge; the design's pantry item with the same number is ABC-301.
        1: [
            _at("XYZ-301", 200, 300),
            _at("LOUNGE", 200, 340),
            _at("ABC-301", 900, 300),
            _at("ABC-301T", 900, 312),
            _at("PANTRY", 900, 340),
        ],
        # XYZ-302 in the staff lounge (its name in a symbol font, on two lines); the same plan
        # prints the breakfast room far off, with XYZ-305 and a cabinet code in it.
        2: [
            _at("XYZ-302", 200, 300),
            _at(_symbol("STAFF"), 200, 330),
            _at(_symbol("LOUNGE"), 200, 341),
            _at("XYZ-305", 900, 300),
            _at("W70", 960, 300),
            _at("BREAKFAST", 900, 340),
        ],
        # XYZ-303 beside a corridor's name; the meeting room is on another plan only.
        3: [_at("XYZ-303", 200, 300), _at("CORRIDOR", 200, 420)],
        4: [_at("MEETING ROOM", 200, 340, 90)],
    }


def test_another_familys_item_with_the_same_number_in_the_sheets_room_is_told() -> None:
    (line,) = room_facts(["XYZ-301"], "PANTRY B - CABINETS", _design_rooms())
    assert line == (
        "This sheet names 'PANTRY B - CABINETS'. The design's PANTRY item with this number is "
        "ABC-301 (design p1); this sheet uses XYZ-301, which the design prints in LOUNGE (design "
        "p1). The item number may be wrong: please check."
    )
    # A tag the design never prints, with the other family's item in the sheet's room.
    (line,) = room_facts(["QQ-301"], "PANTRY B", _design_rooms())
    assert "this sheet uses QQ-301, which the design does not print on a plan" in line


def test_a_tag_the_same_plan_puts_in_another_room_is_told() -> None:
    (line,) = room_facts(["XYZ-302"], "CABINETS @ BREAKFAST AREA", _design_rooms())
    assert line.startswith(
        "This sheet names 'CABINETS @ BREAKFAST AREA'. The design prints XYZ-302 in STAFF LOUNGE "
        "(design p2); the room the sheet names is elsewhere on that plan (its name 661 points "
        "from the tag)."
    )
    assert line.endswith(
        "The design's BREAKFAST holds XYZ-305. The room name or the item number may be wrong: "
        "please check."
    )


def test_no_room_fact_without_both_sides() -> None:
    design = _design_rooms()
    # The sheet names the tag's own room, or a room the design does not print.
    assert room_facts(["XYZ-302"], "STAFF LOUNGE CABINETS", design) == []
    assert room_facts(["XYZ-302"], "GARAGE", design) == []
    # The room the sheet names is only on another plan, and the tag's nearest name may be the
    # next room's: never said.
    assert room_facts(["XYZ-303"], "CABINETS @ MEETING ROOM", design) == []
    # The room the sheet names is printed near the tag on its plan.
    near = {1: [_at("XYZ-306", 200, 300), _at("LOUNGE", 200, 330), _at("BREAKFAST", 300, 330)]}
    near[2] = [_at("BREAKFAST", 900, 900)]
    assert room_facts(["XYZ-306"], "BREAKFAST", near) == []
    # No tag, or no description.
    assert room_facts([], "PANTRY", design) == []
    assert room_facts(["XYZ-301"], "", design) == []


# ---- the sheet's description strip ---------------------------------------------------------------


def _vendor_sheet() -> list[dict[str, Any]]:
    return [
        _at("MATERIAL", 1000, 60),
        _at("SPEC", 1060, 60),
        _at("DESCRIPTION", 460, 700, 70),
        _at("ITEM NO.", 640, 700, 50),
        _at("OVERALL SIZE", 730, 700, 70),
        _at("DATE", 920, 700, 30),
        _at("CABINETS @", 452, 738, 60),
        _at("STAFF LOUNGE", 452, 749, 70),
        _at("XYZ-301", 645, 748, 45),
        _at("03/01/2026", 910, 748, 60),
        _at("A notice printed across the whole strip", 30, 760, 1100),
    ]


def test_the_description_strip_is_found_by_its_labels_and_comes_first() -> None:
    texts = [p["text"] for p in title_block(_vendor_sheet(), (1224.0, 792.0))]
    assert texts == [
        "DESCRIPTION: CABINETS @ STAFF LOUNGE",
        "ITEM NO.: XYZ-301",
        "OVERALL SIZE",
        "DATE: 03/01/2026",
        "MATERIAL",
        "SPEC",
        "A notice printed across the whole strip",
    ]
    assert sheet_description(_vendor_sheet()) == (["XYZ-301"], "CABINETS @ STAFF LOUNGE")
    # A value after its label on its line, and one in the label's own phrase.
    inline = [
        _at("ITEM #: XYZ-302", 100, 700, 80),
        _at("ROOM NAME", 300, 700, 60),
        _at("STAFF LOUNGE", 365, 700, 70),
    ]
    assert [(f.label, f.value) for f in sheet_fields(inline)] == [
        ("ITEM #", "XYZ-302"),
        ("ROOM NAME", "STAFF LOUNGE"),
    ]


def test_a_revision_table_is_no_strip_and_a_stone_header_describes_the_sheet() -> None:
    revisions = [_at("DATE", 900, 600, 30), _at("DESCRIPTION", 950, 600, 70)]
    assert sheet_fields(revisions) == []
    assert sheet_description(revisions) == ([], "")
    header = [
        _at("DRAWING # 3", 40, 23, 60),
        _at("XYZ-301", 40, 35),
        _at("Staff Lounge B", 40, 47, 80),
        _at("PLAN VIEW", 700, 23, 50),
    ]
    assert sheet_description(header) == (["XYZ-301"], "Staff Lounge B")


# ---- the same item on the other file -------------------------------------------------------------


def test_same_item_takes_the_tops_t_and_nothing_else() -> None:
    assert same_item("XYZ-301", "XYZ-301T")
    assert same_item("XYZ-018T.C", "XYZ-018.C")
    assert same_item("XYZ-302.C", "XYZ-302.CT")
    assert not same_item("XYZ-301A", "XYZ-301B")
    assert not same_item("XYZ-301", "XYZ-302T")
    assert not same_item("XYZ-301", "QQ-301")


def test_models_are_read_and_tags_left_out() -> None:
    found = [_at("SINK ABC-UM-12 BY GV", 10, 10, 120), _at("ST-03", 10, 30), _at("K-3821", 10, 50)]
    assert model_tokens(found) == ["ABC-UM-12", "K-3821"]


def test_the_other_files_page_of_the_same_item_is_listed_with_its_facts() -> None:
    pages = {
        1: [_at("XYZ-301", 300, 300), _at('96"', 300, 330)],
        2: [_at("XYZ-302", 300, 300)],
        3: [
            _at("XYZ-301T", 300, 300),
            _at('97 1/2"', 300, 330),
            _at('25 1/2"', 340, 330),
            _at('4"', 380, 330),
            _at("SINK ABC-UM-12", 300, 360, 90),
            _at("JOINT", 400, 360),
            _at("STONE TOP PLAN", 1000, 740, 120),
        ],
        4: [_at("XYZ-301T", 300, 300)],
    }
    context = SetContext("Joined.pdf", lambda page: pages[page], 4)
    parts = [
        {"name": "Cabinets.pdf", "first_page": 1, "pages": 2},
        {"name": "Tops.pdf", "first_page": 3, "pages": 2},
    ]
    lines = other_file_facts(context, 1, parts, lambda _page: (1224.0, 792.0))
    assert len(lines) == 2
    assert lines[0] == (
        "p3 (from Tops.pdf) prints XYZ-301T (this page: XYZ-301). Its title block: 'STONE TOP "
        'PLAN\'. Largest sizes: 97 1/2", 25 1/2", 4". Sink or appliance models: ABC-UM-12. '
        "Seams (JOINT) marked: 1."
    )
    assert lines[1].startswith("p4 (from Tops.pdf)")
    # A page of the same file is never listed, and an upload of one file has no other file.
    assert other_file_facts(context, 2, parts) == []
    assert other_file_facts(context, 1, parts[:1]) == []


def test_the_same_number_in_another_family_links_only_when_both_sheets_name_one_room() -> None:
    pages = {
        1: [
            _at("DESCRIPTION", 460, 700, 70),
            _at("ITEM NO.", 640, 700, 50),
            _at("STAFF LOUNGE B", 452, 738, 70),
            _at("XYZ-301", 645, 738, 45),
        ],
        2: [
            _at("DRAWING # 2", 40, 23, 60),
            _at("ABC-301", 40, 35),
            _at("Staff Lounge B", 40, 47, 80),
        ],
        3: [_at("DRAWING # 3", 40, 23, 60), _at("ABC-301", 40, 35), _at("Pantry", 40, 47)],
    }
    parts = [
        {"name": "Cabinets.pdf", "first_page": 1, "pages": 1},
        {"name": "Tops.pdf", "first_page": 2, "pages": 2},
    ]
    labels = {2: [{"text": '96"'}, {"text": '30 1/2"'}, {"text": "170"}]}
    context = SetContext("Joined.pdf", lambda page: pages[page], 3)
    (line,) = other_file_facts(
        context, 1, parts, lambda _page: (1224.0, 792.0), lambda page: labels.get(page, [])
    )
    assert line.startswith(
        "p2 (from Tops.pdf) prints ABC-301: the same number as this page's XYZ-301 in another "
        "family, and both sheets name LOUNGE STAFF ('STAFF LOUNGE B'; 'Staff Lounge B'). One item "
        "under two numbers? Please check which number the design gives it."
    )
    assert 'Largest sizes: 96", 30 1/2" (code read the drawn numbers).' in line


# ---- the sheet's date ----------------------------------------------------------------------------


def test_dates_are_read_in_the_drawings_forms() -> None:
    assert [d.isoformat() for d, _t in dates_in("REV 2 04/02/2026 and 2026/04/14")] == [
        "2026-04-14",
        "2026-04-02",
    ]
    assert [d.isoformat() for d, _t in dates_in("25/03/2026")] == ["2026-03-25"]
    assert dates_in('3/4" 12-10-24 13/13/2026 1/2/1999') == []


def test_an_older_sheet_is_asked_to_follow_the_designs_latest_revision() -> None:
    assert date_fact(["DATE: 03/01/2026"], ["ISSUED 2026/02/01", "REV 3 2026/04/14"]) == (
        "This sheet is dated 03/01/2026; the design set's latest revision is dated 2026/04/14: "
        "check it follows that revision."
    )
    assert date_fact(["DATE: 05/01/2026"], ["2026/04/14"]) == ""
    assert date_fact(["NO DATE"], ["2026/04/14"]) == ""
    assert date_fact(["03/01/2026"], ["NONE"]) == ""
