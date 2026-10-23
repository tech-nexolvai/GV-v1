"""Pairing across a two-file upload: projects, reference sets, views, key plans, variants. Synthetic
sets only, $0, no model call (the set index's read and the page matcher are fake or saved).

A wrong pair can make false reds, so every rule here only ever removes a pair, or makes one where
the architect's own printed pointer and the AI page matcher agree:

- the set index's read is kept only where the page prints it (`set_index.verify`);
- a vendor drawing is paired only within its own project, never with a reference set (R1) nor a
  key plan; a project the architect's file lacks is "drawing needed" before any paid compare;
- an architect sheet of several titled views is narrowed to one view (look-alikes: none);
- a key plan's callout beside the vendor's item tag points to the view (agrees with the matcher);
- an accessible variant never pairs with a standard one; an opposite-hand unit is compared with its
  positions Not compared;
- a table page is skipped with its reason.
"""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.review.assistant import set_index
from app.review.assistant.simple_check import pairing, projects, views
from app.review.assistant.simple_check.pairing import (
    Facts,
    MatchAnswer,
    agree,
    clear_winner,
    decide,
    item_tags_in,
    markers_in,
    narrow,
    opposite_hand,
    rank,
    reuse,
)
from app.review.assistant.simple_check.worklist import (
    DRAWING_NEEDED,
    NO_DRAWING_NOTE,
    worklist_from,
)
from tests.review.assistant.test_simple_verify import ROUND_2, _one, _pair

SHOP, ARCH = "shop.pdf", "arch.pdf"
PAGE = [0.0, 0.0, 1200.0, 800.0]


# ---- a synthetic two-file set ------------------------------------------------------------------


def _label(n: int, text: str, inches: float, box: list[float], side: str, axis: str) -> dict:
    return {
        "id": f"D{n}",
        "text": text,
        "inches": str(inches),
        "box": box,
        "side": side,
        "orientation": axis,
        "route": "text",
    }


def _phrase(text: str, box: list[float]) -> dict:
    return {"text": text, "box": box}


def _entry(file: str, page: int, role: str | None) -> dict:
    halves = [] if role is None else [{"box": PAGE, "role": role, "from_upload": True}]
    return {"file": file, "page": page, "role": role or "general", "halves": halves}


def _set() -> dict[str, Any]:
    """Two projects in the vendor's file (NORTH, SOUTH), one project and a brand prototype in the
    architect's: a two-view elevation sheet of NORTH, a key plan of NORTH, the prototype printing
    the vendor's item code. Page pictures and texts are what the set pack would hold."""
    vendor_desk = [
        _label(1, '97"', 97, [300, 200, 320, 210], "vendor", "h"),
        _label(2, '41"', 41, [130, 300, 140, 320], "vendor", "v"),
        _label(3, '24"', 24, [500, 300, 510, 320], "vendor", "v"),
    ]
    table = [_label(1, "10", 10, [75, 450, 93, 466], "vendor", "v")]
    elevation = [
        _label(1, "8'-0\"", 96, [250, 200, 270, 210], "architect", "h"),
        _label(2, "3'-6\"", 42, [130, 280, 140, 300], "architect", "v"),
        _label(3, "5'-0\"", 60, [800, 200, 820, 210], "architect", "h"),
        _label(4, "1'-6\"", 18, [720, 280, 730, 300], "architect", "v"),
    ]
    elevation_words = [
        _phrase("1", [110, 380, 120, 395]),
        _phrase("RECEPTION DESK ELEVATION", [140, 380, 400, 395]),
        _phrase("A5.0", [105, 398, 125, 406]),
        _phrase('1/4" = 1\'-0"', [140, 398, 200, 406]),
        _phrase("2", [710, 380, 720, 395]),
        _phrase("LOBBY BENCH ELEVATION", [740, 380, 980, 395]),
        _phrase("A5.0", [705, 398, 725, 406]),
        _phrase("A5.0", [1100, 740, 1180, 790]),
    ]
    plan_tags = [
        {"text": "RD-101", "box": [300, 300, 340, 310]},
        {"text": "SB-202", "box": [1000, 100, 1040, 110]},
    ] + [{"text": f"PB-30{n}", "box": [600 + 30 * n, 500, 625 + 30 * n, 510]} for n in range(1, 9)]
    plan_words = [
        _phrase("FLOOR PLAN", [100, 700, 200, 712]),
        _phrase("1", [348, 278, 354, 288]),
        _phrase("A5.0", [342, 290, 362, 298]),
        *(_phrase(t["text"], t["box"]) for t in plan_tags),
    ]
    page_map = {
        "files": [SHOP, ARCH],
        "upload_roles": {SHOP: "vendor", ARCH: "architect"},
        "pages": [
            _entry(SHOP, 1, None),
            _entry(SHOP, 2, "vendor"),
            _entry(SHOP, 3, None),
            _entry(SHOP, 4, "vendor"),
            _entry(SHOP, 5, "vendor"),
            _entry(SHOP, 6, "vendor"),
            _entry(ARCH, 1, None),
            _entry(ARCH, 2, "architect"),
            _entry(ARCH, 3, None),
            _entry(ARCH, 4, "architect"),
            _entry(ARCH, 5, "architect"),
        ],
    }
    dims = {
        "pages": [
            {
                "file": SHOP,
                "page": 2,
                "labels": vendor_desk,
                "phrases": [
                    _phrase("RECEPTION DESK", [700, 700, 800, 710]),
                    _phrase("RD-101", [700, 720, 740, 730]),
                ],
            },
            {
                "file": SHOP,
                "page": 4,
                "labels": vendor_desk,
                "phrases": [_phrase("AB-201", [700, 720, 740, 730])],
            },
            {
                "file": SHOP,
                "page": 5,
                "labels": table,
                "phrases": [_phrase("ITEM CODE", [75, 400, 120, 410])],
            },
            {
                "file": SHOP,
                "page": 6,
                "labels": vendor_desk,
                "phrases": [_phrase("SB-202", [700, 720, 740, 730])],
            },
            {
                "file": ARCH,
                "page": 2,
                "labels": [dict(lb, side="architect") for lb in vendor_desk],
                "phrases": [
                    _phrase("RD-101", [600, 600, 640, 610]),
                    _phrase("FRONT DESK", [600, 620, 700, 630]),
                ],
            },
            {"file": ARCH, "page": 4, "labels": elevation, "phrases": elevation_words},
            {"file": ARCH, "page": 5, "labels": [], "phrases": plan_words},
        ]
    }
    notes = {
        "pages": [
            {
                "file": ARCH,
                "page": 5,
                "drawings": [{"role": "architect", "box": PAGE, "tags": plan_tags}],
            },
            {
                "file": SHOP,
                "page": 2,
                "drawings": [
                    {
                        "role": "vendor",
                        "box": PAGE,
                        "tags": [{"text": "RD-101", "box": [700, 720, 740, 730]}],
                    }
                ],
            },
        ]
    }
    health = {
        "pages": [
            {"file": SHOP, "page": p, "facts": {"typed_text": 900, "drawn_letters": False}}
            for p in (2, 4, 5, 6)
        ]
    }
    return {"page_map": page_map, "dims": dims, "notes": notes, "health": health}


def _read(file: str, page: int, **fields: Any) -> dict[str, Any]:
    """A set index entry as `set_index.read_set` saves it (already checked)."""
    base = {
        "file": file,
        "page": page,
        "projects": [],
        "set_kind": "unknown",
        "set_words": [],
        "introduces": False,
        "sheet": "",
        "item": "",
        "item_tags": [],
        "variant_words": [],
        "hand_words": [],
        "views": [],
        "dropped": [],
        "prompt_version": set_index.PROMPT_VERSION,
    }
    return {**base, **fields}


def _index() -> dict[str, Any]:
    pages = {
        f"{SHOP}#1": _read(
            SHOP,
            1,
            projects=["PROJECT NORTH"],
            set_kind="shop drawing",
            set_words=["SHOP DRAWING"],
            introduces=True,
        ),
        f"{SHOP}#2": _read(SHOP, 2, item="RECEPTION DESK", item_tags=["RD-101"]),
        f"{SHOP}#3": _read(SHOP, 3, projects=["PROJECT SOUTH"], introduces=True),
        f"{SHOP}#4": _read(SHOP, 4),
        f"{SHOP}#5": _read(SHOP, 5),
        f"{SHOP}#6": _read(SHOP, 6, projects=["PROJECT NORTH"]),
        f"{ARCH}#1": _read(
            ARCH,
            1,
            projects=["BRANDCO"],
            set_kind="prototype",
            set_words=["BRAND PROTOTYPE"],
            introduces=True,
        ),
        f"{ARCH}#2": _read(ARCH, 2),
        f"{ARCH}#3": _read(
            ARCH,
            3,
            projects=["PROJECT NORTH"],
            set_kind="architectural",
            set_words=["ARCHITECTURAL SET"],
            introduces=True,
        ),
        f"{ARCH}#4": _read(
            ARCH,
            4,
            views=[
                {
                    "title": "RECEPTION DESK ELEVATION",
                    "number": "1",
                    "kind": "elevation",
                    "prints_sizes": True,
                    "variant_words": [],
                    "hand_words": [],
                },
                {
                    "title": "LOBBY BENCH ELEVATION",
                    "number": "2",
                    "kind": "elevation",
                    "prints_sizes": True,
                    "variant_words": [],
                    "hand_words": [],
                },
            ],
        ),
        f"{ARCH}#5": _read(
            ARCH,
            5,
            views=[
                {
                    "title": "FLOOR PLAN",
                    "number": "",
                    "kind": "plan",
                    "whole_floor": True,
                    "prints_sizes": False,
                }
            ],
        ),
    }
    return {"version": 1, "pages": pages}


def _answers(
    asked: list[str], matches: list[str], sure: bool, ref: str = "p2.v0"
) -> dict[str, Any]:
    return {
        "version": 1,
        "answers": {
            ref: {
                "candidates": asked,
                "matches": matches,
                "confident": sure,
                "reason": "the same reception desk",
                "prompt_version": 1,
            }
        },
    }


def _work(index: dict[str, Any] | None, matches: dict[str, Any] | None = None) -> Any:
    found = _set()
    return worklist_from(
        found["page_map"],
        dims=found["dims"],
        notes=found["notes"],
        health=found["health"],
        set_index=index,
        matches=matches,
        main_file=SHOP,
    )


def _across(work: Any, page: int) -> Any:
    return next(p for p in work.across if p.vendor.own_page == page)


# ---- the set index: only what the page prints ----------------------------------------------------


def test_the_index_keeps_only_what_the_page_prints() -> None:
    _shown, printed = set_index.printed_text(
        ARCH,
        4,
        dims=_set()["dims"],
        annotations=[{"file": ARCH, "page": 4, "kind": "FreeText", "text": "PROJECT NORTH"}],
    )
    entry = set_index.verify(
        {
            "projects": ["PROJECT NORTH", "PROJECT WEST"],
            "set_kind": "prototype",
            "set_words": ["BRAND PROTOTYPE"],
            "introduces_following_pages": False,
            "sheet": "A 5.0",
            "item": "",
            "item_tags": ["ZZ-999"],
            "variant_words": ["ADA"],
            "hand_words": [],
            "views": [
                {
                    "title": "RECEPTION DESK ELEVATION",
                    "number": "1",
                    "kind": "elevation",
                    "item_tags": [],
                    "prints_sizes": True,
                    "whole_floor": False,
                    "variant_words": [],
                    "hand_words": [],
                    "box": [0.05, 0.2, 0.4, 0.5],
                },
                {
                    "title": "KITCHEN ELEVATION",
                    "number": "3",
                    "kind": "elevation",
                    "item_tags": [],
                    "prints_sizes": True,
                    "whole_floor": False,
                    "variant_words": [],
                    "hand_words": [],
                    "box": [0.5, 0.2, 0.9, 0.5],
                },
            ],
        },
        printed,
        (1200.0, 800.0),
    )
    # GV's typed boxes count as printed; a name, a tag, a word or a view title not printed is out.
    assert entry["projects"] == ["PROJECT NORTH"]
    assert entry["item_tags"] == [] and entry["variant_words"] == []
    assert [v["title"] for v in entry["views"]] == ["RECEPTION DESK ELEVATION"]
    # A kind of set only with the words that say it printed; a sheet number with its space.
    assert entry["set_kind"] == "unknown" and entry["sheet"] == "A 5.0"
    assert len(entry["dropped"]) == 6
    assert set_index.reply_schema()["additionalProperties"] is False


def test_the_index_is_read_once_and_reused(tmp_path: Path) -> None:
    calls: list[int] = []

    def ask(messages: list[dict[str, Any]], schema: Any) -> set_index.AskReply:
        calls.append(1)
        answer = {
            "projects": ["PROJECT NORTH"],
            "set_kind": "architectural",
            "set_words": ["ARCHITECTURAL SET"],
            "introduces_following_pages": True,
            "sheet": "",
            "item": "",
            "item_tags": [],
            "variant_words": [],
            "hand_words": [],
            "views": [],
        }
        return set_index.AskReply(json.dumps(answer), "end_turn", {"input_tokens": 9}, "fake", 0.01)

    printed = set_index.Printed(
        frozenset({"PROJECT", "NORTH", "ARCHITECTURAL", "SET"}), "PROJECTNORTHARCHITECTURALSET"
    )
    pages = [set_index.PageToRead(ARCH, 3, "architect", "PROJECT NORTH", printed, None)]
    first = set_index.read_set(pages, tmp_path, ask, None)
    again = set_index.read_set(pages, tmp_path, ask, None)
    assert (first.answered, again.reused, len(calls)) == (1, 1, 1)
    saved = set_index.load(tmp_path)[f"{ARCH}#3"]
    assert saved["introduces"] and saved["set_kind"] == "architectural"
    # No money left: nothing is asked.
    stopped = set_index.read_set(
        [dataclasses.replace(pages[0], page=4)], tmp_path, ask, None, fits=lambda: False
    )
    assert stopped.skipped_for_money == 1 and len(calls) == 1


# ---- projects from the checked reads (no fixed word) -----------------------------------------------


def test_a_divider_introduces_its_pages_even_when_the_read_does_not_say_so() -> None:
    pages = _index()["pages"]
    for one in pages.values():
        one["introduces"] = False
    entries = _set()["page_map"]["pages"]
    printed = {(SHOP, 1): "1 PROJECT NORTH SHOP DRAWING", (SHOP, 3): "PROJECT SOUTH"}
    placed = projects.places(pages, entries, printed)
    assert placed[(SHOP, 2)].project == "project north"
    assert placed[(SHOP, 4)].project == "project south"
    # A page with no drawing that prints more than its project's name (notes, a title block)
    # introduces nothing; a name that is no project of the file (an office's stamp) is left out.
    noted = projects.places(pages, entries, {**printed, (SHOP, 3): "PROJECT SOUTH GENERAL NOTES"})
    assert noted[(SHOP, 4)].project == "project north"
    pages[f"{ARCH}#4"]["projects"] = ["PROJECT NORTH", "ACME STUDIO"]
    pages[f"{ARCH}#3"]["introduces"] = True
    assert projects.places(pages, entries)[(ARCH, 4)].project == "project north"


def test_projects_come_from_the_pages_that_introduce_them() -> None:
    placed = projects.places(_index()["pages"], _set()["page_map"]["pages"])
    assert placed[(SHOP, 2)].project == "project north"
    assert placed[(SHOP, 4)].project == "project south"
    assert placed[(ARCH, 2)].reference and placed[(ARCH, 2)].project == "brandco"
    assert placed[(ARCH, 4)].project == "project north" and not placed[(ARCH, 4)].reference
    # A cover naming two projects places nothing; the same project by its own word.
    assert projects.match("grand hotel northfield", ["gh northfield", "gh southfield"]) == (
        "gh northfield"
    )
    assert projects.match("hotel", ["city hotel", "grand hotel"]) is None


# ---- the work list: projects, reference sets, views, key plans, tables ------------------------------


def test_a_vendor_drawing_is_paired_only_within_its_project_by_view() -> None:
    work = _work(_index())
    desk = _across(work, 2)
    # Never the brand prototype (R1), never the key plan: one candidate, the NORTH elevation sheet.
    assert desk.asked == ("p1004",)
    # The key plan prints RD-101 beside a callout to view 1 of A5.0: with the matcher naming the
    # sheet (even unsure), the pair is that view, not the whole sheet.
    work = _work(_index(), _answers(["p1004"], ["p1004"], sure=False))
    desk = _across(work, 2)
    assert desk.chosen == ("p1004",) and desk.views == ("p1004.a1.view1",)
    assert "key plan" in desk.why and "RECEPTION DESK ELEVATION" in desk.why
    item = next(i for i in work.items if i.vendor.own_page == 2)
    assert [d.titles for d in item.architect] == [("RECEPTION DESK ELEVATION",)]
    assert {lb.id for lb in item.architect[0].labels} == {"D1", "D2"}
    # A saved answer to more candidates (before code ruled the others out) still holds.
    wider = _work(_index(), _answers(["p1002", "p1004", "p1005"], ["p1004"], sure=False))
    assert _across(wider, 2).chosen == ("p1004",)


def test_a_project_the_architect_lacks_is_a_drawing_needed_up_front() -> None:
    south = _across(_work(_index()), 4)
    assert south.needed and south.asked == () and f"{DRAWING_NEEDED}:" in south.why
    assert "PROJECT SOUTH" in south.why


def test_an_item_shown_only_as_a_tag_on_a_plan_is_a_drawing_needed() -> None:
    """The matcher, shown every page of the project, named only the key plan; the plan prints
    the item's tag with no callout near it: a drawing is needed, before any paid compare. With a
    callout beside the tag, or a matcher that was not shown every page, it is left as not found."""
    bench = _across(
        _work(_index(), _answers(["p1002", "p1004", "p1005"], ["p1005"], True, "p6.v0")), 6
    )
    assert bench.needed and bench.chosen == () and "only as a tag on a plan" in bench.why
    assert "SB-202" in bench.why
    narrower = _work(_index(), _answers(["p1005"], ["p1005"], True, "p6.v0"))
    assert not _across(narrower, 6).needed
    desk = _across(_work(_index(), _answers(["p1004", "p1005"], ["p1005"], True)), 2)
    assert not desk.needed  # RD-101 has a callout beside it on the plan


def test_a_view_that_prints_no_sizes_is_a_drawing_needed() -> None:
    index = _index()
    for view in index["pages"][f"{ARCH}#4"]["views"]:
        view["prints_sizes"] = False
    found = _set()
    for entry in found["dims"]["pages"]:
        if entry["file"] == ARCH and entry["page"] == 4:
            entry["labels"] = []
    work = worklist_from(
        found["page_map"],
        dims=found["dims"],
        notes=found["notes"],
        health=found["health"],
        set_index=index,
        matches=_answers(["p1004"], ["p1004"], sure=True),
        main_file=SHOP,
    )
    desk = _across(work, 2)
    assert desk.needed and desk.chosen == () and "no sizes" in desk.why


def test_without_the_index_no_project_is_assumed() -> None:
    desk = _across(_work(None), 2)
    assert "p1002" in desk.asked  # the prototype is only known from what the index reads


def test_a_table_page_is_skipped_with_its_reason() -> None:
    work = _work(_index())
    assert any(s.page == 5 and s.reason == NO_DRAWING_NOTE for s in work.skipped)
    assert all(p.vendor.own_page != 5 for p in work.across)


# ---- views on a sheet ---------------------------------------------------------------------------


def test_views_keep_full_titles_and_own_their_labels() -> None:
    phrases = [
        ("7", (1600.0, 1400.0, 1607.0, 1412.0)),
        ("HALL ELEVATION @", (1625.0, 1395.0, 1890.0, 1413.0)),
        ("WAITING AREA", (1895.0, 1395.0, 2000.0, 1413.0)),
        ("IN 7.0", (1596.0, 1416.0, 1625.0, 1424.0)),
        ("3", (250.0, 1250.0, 256.0, 1262.0)),
        ("HALL ELEVATION", (275.0, 1245.0, 520.0, 1263.0)),
        ("IN7.0", (246.0, 1266.0, 260.0, 1275.0)),
        ("ELEVATIONS", (2300.0, 1450.0, 2460.0, 1475.0)),
        ("IN 7.0", (2310.0, 1600.0, 2450.0, 1650.0)),
    ]
    found = views.find_views(phrases)
    assert [(v.number, v.title, v.sheet) for v in found] == [
        ("3", "HALL ELEVATION", "IN7.0"),
        ("7", "HALL ELEVATION @ WAITING AREA", "IN7.0"),
    ]
    # A short view beside a tall one: the tall view's top dimensions above the short one's title
    # row still go to the tall view (its bubble is the last one left of them).
    assert views.owner((1610.0, 1170.0), found) == 1
    assert views.owner((600.0, 1130.0), found) == 0
    assert views.sheet_number(phrases, found) == "IN7.0"
    assert views.title_block_left(phrases) is not None
    # A callout on a plan: the sheet's letters over its number, view numbers round it.
    plan = [
        ("IN", (500.0, 400.0, 507.0, 407.0)),
        ("7.1", (498.0, 408.0, 508.0, 415.0)),
        ("5", (501.0, 384.0, 505.0, 391.0)),
    ]
    marks = views.callouts(plan, (), {"IN7.1"})
    assert [(m.sheet, m.numbers) for m in marks] == [("IN7.1", ("5",))]


def test_look_alike_views_stay_unpaired() -> None:
    vendor = Facts("p2.v0", words=frozenset({"LOBBY"}))
    twins = [
        Facts("p1004.a1.view1", words=frozenset({"LOBBY"}), view_count=3),
        Facts("p1004.a1.view2", words=frozenset({"LOBBY"}), view_count=3),
        Facts("p1004.a1.view3", view_count=3),
    ]
    best, why = narrow(vendor, twins)
    assert best is None and "two architect drawings could be this item" in why
    one, _ = narrow(vendor, [twins[0], Facts("p1004.a1.view3", view_count=2)])
    assert one is not None and one.ref == "p1004.a1.view1"
    # A key plan's callout points to one of them: the printed pointer decides over a word.
    pointed = dataclasses.replace(vendor, linked={"p1004.a1.view2": "the key plan points here"})
    best, _ = narrow(pointed, twins)
    assert best is not None and best.ref == "p1004.a1.view2"


# ---- evidence rules ------------------------------------------------------------------------------


def test_item_codes_and_materials() -> None:
    assert item_tags_in(["XX-001.C", "XX-801", "LM-4", "PL-07", "XX-318-CG"]) == {
        "XX1",
        "XX801",
        "XX318",
    }
    # A small item code only agrees with the matcher: code never pairs on it alone.
    vendor = Facts("p5.v0", tags=frozenset({"XX1"}), item_tags=frozenset({"XX1"}))
    items = [Facts("p1207", tags=frozenset({"XX1"}))] + [Facts(f"p100{n}") for n in range(1, 5)]
    ranked = rank(vendor, items)
    assert ranked[0].ref == "p1207" and ranked[0].strong and ranked[0].agree_only
    assert clear_winner(ranked) is None


def test_a_dense_sheet_shares_sizes_with_anything() -> None:
    big = tuple(Fraction(n) for n in range(12, 180))
    vendor = Facts("p3.v0", values=(Fraction(30), Fraction(48), Fraction(60), Fraction(96)))
    ranked = rank(vendor, [Facts("p1207", values=big), Facts("p1208"), Facts("p1209")])
    assert not ranked[0].strong and "a dense sheet" in " ".join(ranked[0].evidence)
    assert decide(vendor, [Facts("p1207.a1", values=big), Facts("p1208.a1")]).chosen is None


def test_a_multi_view_sheet_is_never_ruled_out_by_its_kind_of_view() -> None:
    vendor = Facts("p51.v0", views=frozenset({"plan"}))
    single = rank(vendor, [Facts("p1203", views=frozenset({"elevation"}))])
    many = rank(vendor, [Facts("p1203", views=frozenset({"elevation"}), view_count=11)])
    assert single[0].contradicts is not None and many[0].contradicts is None


def test_an_accessible_variant_never_pairs_with_a_standard_one() -> None:
    assert markers_in(['36"W ADA VANITY', "HC-901"])[0] == {"ADA"}
    vendor = Facts("p45.v0", markers=frozenset({"ADA"}))
    ranked = rank(vendor, [Facts("p1205"), Facts("p1206", markers=frozenset({"ACCESSIBLE"}))])
    by = {s.ref: s for s in ranked}
    assert by["p1205"].contradicts is not None and "ADA" in by["p1205"].contradicts
    assert by["p1206"].contradicts is None
    # A sheet of several views: checked view by view, never on the whole sheet.
    sheet = rank(vendor, [Facts("p1205", whole_sheet=True, view_count=4)])
    assert sheet[0].contradicts is None
    assert agree(ranked, None, MatchAnswer(("p1205",), True))[0] == ()


def test_an_opposite_hand_unit_is_compared_without_its_positions() -> None:
    assert markers_in(["UNIT B (MIRROR)", "OPP. HAND", "REV.DATE"])[1] == {"MIRROR", "OPP. HAND"}
    flagged = opposite_hand(Facts("p9.v0", hand=frozenset({"MIRROR"})), Facts("p1009"))
    assert flagged is not None and "opposite-hand" in flagged
    assert opposite_hand(Facts("p9.v0"), Facts("p1009")) is None
    mirrored = dataclasses.replace(ROUND_2, opposite_hand=flagged)
    # From a left or right end: Not compared. A height (no side): compared as before.
    sided = _one(_pair("p2.D1", "p1.D1"), item=mirrored)
    assert sided.outcome == "not_compared" and "not compared" in sided.reason
    height = _one(
        _pair("p2.D1", "p1.D1", ends=("floor", "top of counter", "floor", "top of counter")),
        item=mirrored,
    )
    assert "opposite-hand" not in (height.reason or "")


def test_a_saved_answer_holds_when_code_only_rules_candidates_out() -> None:
    answer = MatchAnswer(("p1201", "p1203"), True, "plan and elevation")
    assert reuse(["p1201", "p1203", "p1204"], answer, ["p1203", "p1204"]) == MatchAnswer(
        ("p1203",), True, "plan and elevation"
    )
    assert reuse(["p1201", "p1203"], MatchAnswer(("p1201",), True), ["p1203"]) is None
    assert reuse(["p1201", "p1203"], MatchAnswer((), True), ["p1203"]) == MatchAnswer((), True)
    assert reuse(["p1203"], answer, ["p1203", "p1204"]) is None  # a new candidate: ask again
    assert pairing.MIN_WEAK_POOL == 4
