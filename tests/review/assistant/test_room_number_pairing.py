"""Pairing a vendor page with the architect's page about the rooms both print (2026-11-04).

A real set: the vendor's kitchenette top has no item tag the architect prints, but its sheet lists
the three rooms it is for, and the architect's enlarged suite plan prints exactly those rooms in
its legend. Code now pairs them on that printed evidence, as strongly as an item tag, when:

- the vendor lists two rooms or more after a room or unit label (`rooms.vendor_rooms`), and prints
  no other room of the job outside its lists (never paired on a list read in part);
- exactly one architect page of its project prints every one of them, they are at least half of
  the room numbers it prints, and it lists at most half of the job's rooms
  (`rooms.room_page`): a room schedule, a matrix or a floor plan listing many rooms never wins.

A sheet the plan's elevation callouts point to goes with it only when code finds the vendor's
piece on a view they point to. Synthetic sets with made-up rooms only: $0, no model call.
"""

from __future__ import annotations

from typing import Any

from app.review.assistant.simple_check import pairing, rooms
from app.review.assistant.simple_check.worklist import worklist_from
from tests.review.assistant.test_cross_file_projects import _entry, _label, _phrase, _read

SHOP, ARCH = "shop.pdf", "arch.pdf"
NORTH = "PROJECT NORTH"
#: The architect's office address, printed in every page's title block: never a room.
ADDRESS = "4410 MAIN STREET"
#: Three floors of twelve rooms; the suite type is 101, 201, 301.
FLOORS = {f: [f * 100 + n for n in range(1, 13)] for f in (1, 2, 3)}
SUITE = "101, 201, 301"
KITCHENETTE = "KITCHENETTE TOP\nRoom Numbers :\n101 (01)\n201 (01)\n301 (01))\nROOM TYPES: S1"

#: The pages of the architect's file: a room schedule, three floor plans, the suite's plan (a
#: furniture plan tagging its pieces) and the suite's elevations sheet A6.1 (four views).
SCHEDULE, PLANS, SUITE_PLAN, ELEVATIONS = 1, (2, 3, 4), 5, 6


def _views_sheet(sizes: dict[int, list[tuple[str, float, str]]]) -> dict[str, Any]:
    """Sheet A6.1's four titled views in a row (bubble, title, sheet number under the bubble) and
    its title block's big sheet number; `sizes`: each view's printed sizes (text, inches, axis),
    above its title."""
    phrases: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    for n in range(1, 5):
        x = 100.0 + 300 * (n - 1)
        phrases += [
            _phrase(str(n), [x + 10, 380, x + 20, 395]),
            _phrase("GUESTROOM ELEVATIONS", [x + 40, 380, x + 250, 395]),
            _phrase("A6.1", [x + 5, 398, x + 25, 406]),
        ]
        for k, (text, inches, axis) in enumerate(sizes.get(n, [])):
            left = x + 60 + 40 * k
            labels.append(
                _label(
                    len(labels) + 1, text, inches, [left, 250, left + 30, 260], "architect", axis
                )
            )
    phrases.append(_phrase("A6.1", [1300, 740, 1380, 790]))
    return {"labels": labels, "phrases": phrases}


def _set(
    vendor: dict[int, str],
    *,
    legend: str = SUITE,
    elevation_sizes: dict[int, list[tuple[str, float, str]]] | None = None,
    extra: dict[int, str] | None = None,
    matches: dict[str, Any] | None = None,
    vendor_marker: str = "",
    plan_sizes: bool = True,
    callout: bool = True,
    tables: dict[int, list[str]] | None = None,
) -> Any:
    """A two-file set of PROJECT NORTH: each vendor page's printed text (77" by 24" each), the
    architect's file above (`legend`: the suite plan's room list; `plan_sizes`: it prints three
    sizes, else none and the set index reads it so; `callout`: its four-way callout to A6.1), and
    `extra` architect pages (page: printed text) after it."""
    extra = extra or {}
    elevation_sizes = elevation_sizes or {
        n: [("8'-0\"", 96, "v"), ("9'-0\"", 108, "h")] for n in range(1, 5)
    }
    arch_text: dict[int, str] = {
        SCHEDULE: "ROOM SCHEDULE\n"
        + "\n".join(" ".join(str(r) for r in rs) for rs in FLOORS.values()),
        **{
            page: f"LEVEL {f} FLOOR PLAN\n" + "\n".join(str(r) for r in FLOORS[f])
            for page, f in zip(PLANS, FLOORS, strict=True)
        },
        SUITE_PLAN: f"FURNITURE PLAN\nGUESTROOM LEGEND\n{legend}\nKING SUITE",
        ELEVATIONS: "KING SUITE\nGUESTROOM ELEVATIONS",
        **extra,
    }
    texts = {(ARCH, p): f"{t}\n{ADDRESS}" for p, t in arch_text.items()}
    texts.update({(SHOP, p): t for p, t in vendor.items()})
    tags = [
        {"text": f"XX-{500 + n}", "box": [700 + 30 * n, 500, 725 + 30 * n, 510]} for n in range(9)
    ]
    four_way = [
        _phrase("A6.1", [600, 400, 620, 408]),
        _phrase("1", [607, 388, 613, 396]),
        _phrase("2", [626, 400, 632, 408]),
        _phrase("3", [607, 412, 613, 420]),
        _phrase("4", [588, 400, 594, 408]),
    ]
    dims_pages: list[dict[str, Any]] = [
        {
            "file": SHOP,
            "page": p,
            "labels": [
                _label(1, '77"', 77, [300, 200, 330, 210], "vendor", "h"),
                _label(2, '24"', 24, [130, 300, 140, 330], "vendor", "v"),
            ],
            "phrases": [_phrase(vendor_marker or "KITCHENETTE TOP", [700, 700, 800, 710])],
        }
        for p in vendor
    ]
    for entry in dims_pages:
        if (tables or {}).get(entry["page"]) is not None and entry["file"] == SHOP:
            # A vendor room matrix: its rooms in cells of their own, read as bare-number labels.
            entry["phrases"] = _cells(tables[entry["page"]])
            entry["labels"] = [
                _label(n + 1, room, int(room), list(box), "vendor", "h")
                for n, (room, box) in enumerate(_cell_boxes(tables[entry["page"]]))
            ]
    for page in arch_text:
        entry: dict[str, Any] = {"file": ARCH, "page": page, "labels": [], "phrases": []}
        if page == SCHEDULE and tables is not None:
            # The schedule as a room table: its rooms in cells, read as bare-number labels too.
            every = [str(r) for rs in FLOORS.values() for r in rs]
            entry["phrases"] = _cells(every, heading="ROOM DESCRIPTION")
            entry["labels"] = [
                _label(n + 1, room, int(room), list(box), "architect", "h")
                for n, (room, box) in enumerate(_cell_boxes(every))
            ]
        if page == SUITE_PLAN:
            entry["labels"] = (
                [
                    _label(1, "2'-6\"", 30, [200, 600, 230, 610], "architect", "h"),
                    _label(2, "5'-11\"", 71, [250, 600, 280, 610], "architect", "h"),
                    _label(3, "1'-6\"", 18, [300, 600, 330, 610], "architect", "h"),
                ]
                if plan_sizes
                else []
            )
            entry["phrases"] = [
                *(four_way if callout else []),
                *(_phrase(t["text"], t["box"]) for t in tags),
                _phrase("A6.0", [1300, 740, 1380, 790]),
            ]
        if page == ELEVATIONS:
            entry.update(_views_sheet(elevation_sizes))
        dims_pages.append(entry)
    notes = {
        "pages": [
            {
                "file": ARCH,
                "page": SUITE_PLAN,
                "drawings": [{"role": "architect", "box": [0, 0, 1400, 800], "tags": tags}],
            }
        ]
    }
    index = {
        "version": 1,
        "pages": {
            **{f"{SHOP}#{p}": _read(SHOP, p, projects=[NORTH]) for p in vendor},
            **{f"{ARCH}#{p}": _read(ARCH, p, projects=[NORTH]) for p in arch_text},
        },
    }
    if not plan_sizes:
        index["pages"][f"{ARCH}#{SUITE_PLAN}"]["views"] = [
            {"title": "FURNITURE PLAN", "number": "D", "kind": "plan", "prints_sizes": False}
        ]
    return worklist_from(
        {
            "files": [SHOP, ARCH],
            "upload_roles": {SHOP: "vendor", ARCH: "architect"},
            "pages": [_entry(SHOP, p, "vendor") for p in vendor]
            + [_entry(ARCH, p, "architect") for p in arch_text],
        },
        dims={"pages": dims_pages},
        notes=notes,
        set_index=index,
        matches=matches,
        main_file=SHOP,
        texts=texts,
    )


def _cell_boxes(rooms: list[str]) -> list[tuple[str, tuple[float, float, float, float]]]:
    return [
        (
            room,
            (
                400.0 + 60 * (n % 6),
                100.0 + 12 * (n // 6),
                420.0 + 60 * (n % 6),
                108.0 + 12 * (n // 6),
            ),
        )
        for n, room in enumerate(rooms)
    ]


def _cells(rooms: list[str], heading: str = "Room Type") -> list[dict[str, Any]]:
    """A room table's printed cells: its room-type heading, a type name, each room on its own."""
    return [
        _phrase(heading, [200, 85, 300, 95]),
        _phrase("King", [200, 102, 260, 110]),
        *(_phrase(room, list(box)) for room, box in _cell_boxes(rooms)),
    ]


def _pair(work: Any, page: int) -> Any:
    return next(p for p in work.across if p.vendor.own_page == page)


# ---- reading the rooms --------------------------------------------------------------------------


def test_the_vendors_rooms_are_read_after_a_room_or_unit_label() -> None:
    assert rooms.vendor_rooms([KITCHENETTE]) == {"101", "201", "301"}
    assert rooms.vendor_rooms(["ROOMS 101, 201"]) == {"101", "201"}
    assert rooms.vendor_rooms(["ROOM NO. 101 & 201 AND 301"]) == {"101", "201", "301"}
    assert rooms.vendor_rooms(["Room no:\n101, 105B, 106A,\n201 (03)\nFloor 1 = 1"]) == {
        "101",
        "105B",
        "106A",
        "201",
    }
    assert rooms.vendor_rooms(["Rooms : 101 102 103\nFL 1 = 3"]) == {"101", "102", "103"}
    assert rooms.vendor_rooms(["UNITS 2A/3A"]) == {"2A", "3A"}
    assert rooms.vendor_rooms(["ROOM #: 101/201"]) == {"101", "201"}
    # A range gives its two ends: its middle is not printed, so it is not guessed.
    assert rooms.vendor_rooms(["RM 101-401"]) == {"101", "401"}
    assert rooms.vendor_rooms(["RMS 101 THRU 105"]) == {"101", "105"}
    # A look-alike dash is the hyphen.
    assert rooms.vendor_rooms(["RM 101−401"]) == {"101", "401"}


def test_sizes_tags_sheets_dates_and_counts_are_never_rooms() -> None:
    for text in (
        'ROOM 25 1/2" [648mm]',
        "ROOM 610 MM",
        "ROOM [1830mm]",
        "ROOM TYPES: S1",
        "ROOM A-301",
        "ROOM XX-305",
        "ROOM 2025/01/02",
        'ROOM SCALE 3/8" = 1\'-0"',
        "32 UNITS",
        "UNIT 2 OF 4",
        "GRAND HOTEL INN AND SUITES 4410 MAIN STREET",
        "BATHROOM 101",
        "Room Numbers\n121\nQTY\n01\n2F 03 205, 211",
    ):
        found = rooms.vendor_rooms([text])
        assert len(found) < rooms.MIN_ROOMS, (text, found)
    assert rooms.vendor_rooms(["ROOM 101 [610mm]"]) == {"101"}
    printed = rooms.printed_rooms(
        ['A-301 XX-305 2025/01/02 24" 610 MM 1830mm 3/8" ID 9.9X 4410 MAIN 101, 201/301 X25']
    )
    assert printed == {"4410", "101", "201", "301"}


def test_the_page_about_the_rooms_must_be_specific() -> None:
    """Every vendor room printed, at least half of the page's rooms, and the page lists at most
    half of the job's rooms; exactly one page passes."""
    job = frozenset(str(r) for rs in FLOORS.values() for r in rs)
    pages = {
        "schedule": job,
        "floor 1": frozenset(str(r) for r in FLOORS[1]),
        "suite": frozenset({"101", "201", "301"}),
    }
    found, _why = rooms.room_page(frozenset({"101", "201", "301"}), pages, job)
    assert found is not None and found.page == "suite" and found.printed == 3
    # Most of the job's rooms: only the schedule prints them all, and it is a list.
    most = frozenset(sorted(job)[:30])
    found, why = rooms.room_page(most, pages, job)
    assert found is None and why is not None and "most of the job's rooms" in why
    # Two rooms of one floor: the floor plan prints ten more.
    found, why = rooms.room_page(frozenset({"101", "102"}), pages, job)
    assert found is None and why is not None and "many other rooms" in why
    # One room is never enough; two pages printing exactly the rooms tie.
    assert rooms.room_page(frozenset({"101"}), pages, job) == (None, None)
    tied = {**pages, "suite ceiling": frozenset({"101", "201", "301"})}
    found, why = rooms.room_page(frozenset({"101", "201", "301"}), tied, job)
    assert found is None and why is not None and "2 architect pages are about its rooms" in why


def test_a_number_on_most_pages_is_the_title_blocks() -> None:
    pages = {p: frozenset({"4410", str(100 + p)}) for p in range(1, 7)}
    pages[6] = frozenset({"106"})
    assert rooms.title_block(pages) == {"4410"}
    assert rooms.title_block(dict(list(pages.items())[:4])) == frozenset()


# ---- pairing ------------------------------------------------------------------------------------


def test_the_vendors_rooms_pair_it_with_the_suite_plan_by_code() -> None:
    """The kitchenette top lists rooms 101, 201, 301; the suite plan prints exactly them; the
    schedule and the floor plans print them too, among many. Code pairs it with the suite plan
    (a furniture plan, never a candidate of the matcher), as strongly as an item tag: the paid
    matcher is not asked, and the page is no longer "pairing unsure"."""
    work = _set({1: KITCHENETTE})
    pair = _pair(work, 1)
    assert pair.chosen == (f"p100{SUITE_PLAN}",), pair.why
    assert pair.code_paired and pair.unsure == () and not pair.needed
    assert "room numbers 101, 201, 301" in pair.why and "3 of the 3" in pair.why
    item = next(i for i in work.items if i.id == pair.vendor.ref)
    assert [d.own_page for d in item.architect] == [SUITE_PLAN]
    # Listed with the candidates (its page named), never one the matcher is asked about.
    assert f"p100{SUITE_PLAN}" in {c.id for c in pair.candidates}
    assert f"p100{SUITE_PLAN}" not in pair.asked


def test_a_matcher_answer_it_is_not_sure_of_changes_nothing() -> None:
    unsure = {
        "version": 1,
        "answers": {
            "p1.v0": {
                "candidates": ["p1001", "p1002", "p1003", "p1004", "p1006"],
                "matches": ["p1006"],
                "confident": False,
                "reason": "the suite's elevations",
                "prompt_version": 1,
            }
        },
    }
    pair = _pair(_set({1: KITCHENETTE}, matches=unsure), 1)
    assert pair.chosen == (f"p100{SUITE_PLAN}",) and pair.code_paired and pair.unsure == ()
    assert pairing.PAIRING_UNSURE not in pair.why


def test_a_schedule_or_floor_plan_never_wins_on_room_numbers() -> None:
    """A vendor page listing most of the job's rooms (only the schedule prints them all), or two
    rooms of one floor (its floor plan prints ten more): never paired by its rooms."""
    every = "Room Numbers :\n" + ", ".join(str(r) for rs in FLOORS.values() for r in rs[:10])
    floor = "ROOMS 102, 103"
    work = _set({1: every, 2: floor})
    for page in (1, 2):
        pair = _pair(work, page)
        assert pair.chosen == () and not pair.code_paired, (page, pair.why)
        assert "room numbers" not in pair.why


def test_two_pages_printing_exactly_its_rooms_pair_neither() -> None:
    """A second page of the project prints the suite's rooms alone too (a ceiling plan of the
    suite): the tie is not broken by a guess."""
    work = _set({1: KITCHENETTE}, extra={7: f"REFLECTED CEILING PLAN\n{SUITE}"})
    pair = _pair(work, 1)
    assert pair.chosen == () and not pair.code_paired


def test_a_room_of_the_job_printed_outside_its_list_refuses_the_rooms() -> None:
    """The vendor's page prints a job room (302) outside its room list: the list may have been
    read in part, so its rooms pair nothing."""
    pair = _pair(_set({1: f"{KITCHENETTE}\nSEE ALSO 302"}), 1)
    assert pair.chosen == () and not pair.code_paired


def test_the_title_blocks_address_is_never_a_room() -> None:
    """Every architect page prints the office's street number 4410; a vendor list naming it with
    the suite's rooms still finds no page (it is no room), and the suite's plan pairs without it."""
    pair = _pair(_set({1: "ROOMS 101, 201, 301, 4410"}), 1)
    assert pair.chosen == ()
    pair = _pair(_set({1: "ROOMS 101, 201, 301"}), 1)
    assert pair.chosen == (f"p100{SUITE_PLAN}",)


def test_an_accessible_vendor_unit_is_never_paired_with_a_standard_plan() -> None:
    pair = _pair(_set({1: KITCHENETTE}, vendor_marker="ADA KITCHENETTE TOP"), 1)
    assert pair.chosen != (f"p100{SUITE_PLAN}",)


def test_the_elevation_sheet_goes_with_the_plan_only_when_code_finds_the_piece_on_it() -> None:
    """The suite plan's four-way callout points to views 1-4 of sheet A6.1. View 3 prints the
    vendor's 77" and 24": that sheet and view go with the plan. Printing none of the vendor's
    sizes, the sheet is not compared (a callout says a view looks at a wall of the room, not that
    the piece is on it)."""
    sizes: dict[int, list[tuple[str, float, str]]] = {
        n: [("8'-0\"", 96, "v"), ("9'-0\"", 108, "h")] for n in (1, 2, 4)
    }
    sizes[3] = [("6'-5\"", 77, "h"), ("2'-0\"", 24, "v"), ("8'-0\"", 96, "v")]
    pair = _pair(_set({1: KITCHENETTE}, elevation_sizes=sizes), 1)
    assert pair.chosen == (f"p100{SUITE_PLAN}", f"p100{ELEVATIONS}"), pair.why
    assert pair.views == (f"p100{ELEVATIONS}.a1.view3",)
    assert pair.code_paired and pair.unsure == ()
    pair = _pair(_set({1: KITCHENETTE}), 1)
    assert pair.chosen == (f"p100{SUITE_PLAN}",) and pair.views == ()
    assert "callouts point to architect page 6, but code does not find" in pair.why


def test_a_rooms_plan_with_no_sizes_is_never_a_drawing_needed_while_its_callouts_point_on() -> None:
    """The suite plan prints no sizes (the set index reads it so) and code does not find the piece
    on the sheet its callouts point to: "pairing unsure" naming both, the paid matcher asked, never
    "drawing needed" (the drawing may be on that sheet). With no callout it is the plan alone, which
    prints no sizes of the piece: "drawing needed", as for any such page."""
    pair = _pair(_set({1: KITCHENETTE}, plan_sizes=False), 1)
    assert pair.chosen == () and not pair.needed and not pair.code_paired
    assert pair.unsure == (f"p100{SUITE_PLAN}", f"p100{ELEVATIONS}")
    assert pairing.PAIRING_UNSURE in pair.why and "prints no sizes of the piece" in pair.why
    pair = _pair(_set({1: KITCHENETTE}, plan_sizes=False, callout=False), 1)
    assert pair.chosen == () and pair.needed and pair.unsure == ()


# ---- a room matrix and the room schedule (2026-11-04) -----------------------------------------


EVERY_ROOM = [str(r) for rs in FLOORS.values() for r in rs]


def test_a_room_matrix_pairs_with_the_room_schedule_listing_the_same_rooms() -> None:
    """The vendor's room matrix and the architect's room schedule are both room tables listing
    every room of the job: the one schedule listing the same rooms is its page, by code (two
    lists of the whole job; the guard against lists holds for every other page)."""
    pair = _pair(_set({1: "VANITY MATRIX"}, tables={1: EVERY_ROOM}), 1)
    assert pair.chosen == (f"p100{SCHEDULE}",) and pair.code_paired, pair.why
    assert "room table listing the same rooms" in pair.why
    # A few rooms differ (the room check says which): still the same list.
    pair = _pair(_set({1: "VANITY MATRIX"}, tables={1: [*EVERY_ROOM[:-1], "999"]}), 1)
    assert pair.chosen == (f"p100{SCHEDULE}",)


def test_a_drawing_listing_most_rooms_in_a_note_never_pairs_with_the_schedule() -> None:
    """A vendor drawing (not a room table) noting most of the job's rooms: never the schedule."""
    note = "Room Numbers :\n" + ", ".join(EVERY_ROOM)
    pair = _pair(_set({1: note}, tables={}), 1)
    assert pair.chosen == () and not pair.code_paired


def test_a_room_tables_numbers_are_never_sizes_in_the_pairing() -> None:
    """Read as inches, a matrix's room numbers matched the schedule's as dozens of "sizes". On a
    room table's page a bare number is a room or a count: no size evidence."""
    work = _set({1: "VANITY MATRIX"}, tables={1: EVERY_ROOM})
    pair = _pair(work, 1)
    schedule = next(c for c in pair.candidates if c.id == f"p100{SCHEDULE}")
    assert not any("sizes" in e or '"' in e for e in schedule.evidence), schedule.evidence
