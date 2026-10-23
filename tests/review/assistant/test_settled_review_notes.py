"""Five settled cases, each a plain note on its page or item while the rest of the check goes on
(owner, 2026-10-23). Made-up values only; $0.

1. The vendor's millimetres disagree with its inches (`660 [27"]`): inches count, millimetres are
   ignored (settled with GV); never red, never a question; a plain pre-flight note.
2. The architect's own plan and elevation print different sizes for one piece: the governing view
   is compared with, GV gets an "Ask the architect" item with a ready question, never red against
   the vendor because of it.
3. The same sheet twice in one upload: only the newest is checked; the older one says so.
4. A page whose numbers are only pen strokes code cannot read: said on the page, and the vendor
   letter asks for PDFs with real text.
5. (The architect shows the item with no sizes: `test_hard_uploads` and the work list's tests.)
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from app.review.assistant import plan_supervisor, preflight, rule_notes, sheet_revisions
from app.review.assistant.classification import classify
from app.review.assistant.preflight import Inputs, build
from app.review.assistant.set_pack import NUMBERS_UNREAD, settle_candidates
from app.review.assistant.simple_check import worklist
from app.review.assistant.simple_check.schema import (
    Drawing,
    Label,
    ViewsDisagree,
    WorkItem,
)
from app.review.assistant.vendor_consistency import UNREADABLE_WORDS, unreadable_pages
from tests.review.assistant import test_simple_verify as tsv

VENDOR = "vendor.pdf"
ARCH = "architect.pdf"


def _entry(page: int, sheets: list[str], role: str = "vendor", **more: Any) -> dict[str, Any]:
    return {
        "file": VENDOR,
        "page": page,
        "role": role,
        "halves": [{"box": [0, 0, 100, 100], "role": role, "round": None}],
        "rounds": [],
        "sheet_numbers": sheets,
        **more,
    }


def _map(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"files": [VENDOR], "pages": list(entries)}


TITLE = "BREAKFAST BAR ELEVATION SHEET MW-201 MILLWORK SHOP DRAWINGS HOTEL MADE UP"

# ---- 1. millimetres that disagree ----------------------------------------------------------------


def test_millimetres_that_disagree_are_a_plain_note_nobody_acts_on() -> None:
    dims = {
        "pages": [
            {
                "file": VENDOR,
                "page": 2,
                "labels": [
                    {"text": '660 [27"]', "inches": "27", "mm": "660", "mm_ignored": True},
                    {"text": '610 [24"]', "inches": "24", "mm": "610", "units": "agrees"},
                ],
            }
        ]
    }
    report = build(Inputs(page_map=_map(_entry(2, [])), main_file=VENDOR, dims=dims))
    line = next(line for line in report.lines if line.id == "mm_ignored")
    assert line.words == (
        'Page 2 prints millimetres that do not match the inches beside them (such as 660 [27"]): '
        "the inches are used and the millimetres are ignored."
    )
    assert line.level == "note" and line.goes_on and line.who == "nobody"


# ---- 2. the architect's own views disagree -------------------------------------------------------


def _label(ref: str, text: str, inches: str, x: float) -> Label:
    page, lid = ref[1:].split(".")
    return Label(
        ref=ref,
        page=int(page),
        id=lid,
        text=text,
        inches=inches,
        box=(x, 100.0, x + 20, 110.0),
        side="architect",
        orientation="h",
        route="text",
    )


def _work_item(disagree: ViewsDisagree) -> WorkItem:
    vendor = Drawing(
        ref="p1.v0",
        file=VENDOR,
        page=1,
        half=0,
        role="vendor",
        box=(0, 0, 9, 9),
        crop_box=(0, 0, 9, 9),
    )
    return WorkItem(
        id="p1.v0", file=VENDOR, page=1, vendor=vendor, architect=(), views_disagree=disagree
    )


DISAGREE = ViewsDisagree(
    governing="elevation",
    other="plan",
    governs="cabinet widths",
    governing_ref="p1002.a1",
    other_ref="p1001.a1",
    file=ARCH,
    governing_page=2,
    other_page=1,
    spans=(
        (_label("p1002.D1", '74"', "74", 300), _label("p1001.D1", '72"', "72", 300)),
        (_label("p1002.D3", '32"', "32", 320), _label("p1001.D3", '30"', "30", 320)),
    ),
    names=("overall width", "width of part 2 of 3"),
    title="VANITY ELEVATION",
)


def test_the_architects_own_views_disagreeing_is_an_ask_the_architect_item() -> None:
    found = worklist.architect_views_findings([_work_item(DISAGREE), _work_item(DISAGREE)])
    assert len(found) == 2  # one per span, never twice
    first = {**found[0], "checks": {}}
    classified = classify(first, proved=False)
    assert classified.outcome == "rule_note" and classified.reason == "architect_disagrees"
    assert classified.note is not None and classified.note.kind == rule_notes.VIEWS
    assert classified.settle == (
        'Architect: your plan (page 1) shows 72", your elevation (page 2) shows 74" for the '
        "vanity elevation: overall width. Please confirm which is right."
    )
    assert "never red against the vendor" in (classified.why or "")
    assert classified.note.standard == "GV compares cabinet widths with the elevation"


def test_a_ledger_disagreement_is_asked_only_when_code_read_both_values() -> None:
    ledger = {
        "page": 3,
        "vs_page": 5,
        "item": "Vanity overall width (same item, different views)",
        "architect": '74"',
        "vendor": '72"',
        "status": "check",
        "source": "cross_view",
        "reference_side": "architect",
        "dims": {"views": {"governing": "elevation", "other": "plan"}},
    }
    unread = classify({**ledger, "checks": {"printed_architect": "match"}}, proved=False)
    assert unread.outcome == "not_compared" and unread.reason == "architect_disagrees"
    both = {"printed_architect": "match", "printed_vendor": "match"}
    asked = classify({**ledger, "checks": both}, proved=False)
    assert asked.outcome == "rule_note"
    assert (asked.settle or "").startswith(
        'Architect: your plan (page 3) shows 72", your elevation'
    )


def test_a_vendor_value_on_a_span_the_architects_views_disagree_on_is_never_red() -> None:
    """The work item's guard: the pair code would otherwise make a red candidate (p2.D1 against
    the architect's p1.D1) is grey, "GV asks the architect"."""
    assert tsv._one(tsv._pair("p2.D1", "p1.D1")).outcome == "candidate"
    item = replace(tsv.ROUND_2, views_disagree=(("p1.D1", '42"', '40"', "elevation", "plan"),))
    found = tsv._one(tsv._pair("p2.D1", "p1.D1"), item)
    assert found.outcome == "not_compared" and found.reason_code == "architect_disagrees"
    assert "GV asks the architect" in found.reason
    assert found.finding is not None and found.finding["keep_reason"] == "architect_disagrees"
    assert classify(found.finding, proved=False).reason == "architect_disagrees"


# ---- 3. an older copy of a sheet ---------------------------------------------------------------


def _texts(*pairs: tuple[int, str]) -> dict[tuple[str, int], str]:
    return {(VENDOR, page): text for page, text in pairs}


def test_an_older_revision_is_left_out_with_its_reason() -> None:
    page_map = _map(_entry(4, ["MW-201"]), _entry(9, ["MW-201"]), _entry(12, ["MW-201"]))
    texts = _texts(
        (4, f"{TITLE} REV 1 10/02/2026"),
        (9, f"{TITLE} REV 0 09/12/2026"),
        (12, f"{TITLE} REVISIONS REV 0 REV 1 REV 2 10/20/2026"),
    )
    older = sheet_revisions.older_copies(page_map, texts, VENDOR)
    assert sorted(older) == [4, 9]
    assert older[4].newer == 12 and older[4].why == "REV 1; page 12 is REV 2"
    assert older[9].newer == 12 and older[9].why == "REV 0; page 12 is REV 2"
    assert older[4].words == (
        "Page 4 is an older copy of page 12 (REV 1; page 12 is REV 2): not checked."
    )


def test_letters_dates_and_the_later_page() -> None:
    letters = _texts((2, f"{TITLE} REV B"), (3, f"{TITLE} REV A"))
    found = sheet_revisions.older_copies(
        _map(_entry(2, ["MW-201"]), _entry(3, ["MW-201"])), letters, VENDOR
    )
    assert [(p, o.newer) for p, o in found.items()] == [(3, 2)]
    dated = _texts((2, f"{TITLE} 10/20/2026"), (3, f"{TITLE} 9/1/2026"))
    found = sheet_revisions.older_copies(
        _map(_entry(2, ["MW-201"]), _entry(3, ["MW-201"])), dated, VENDOR
    )
    assert [(p, o.newer) for p, o in found.items()] == [(3, 2)]
    plain = _texts((2, TITLE), (3, TITLE))
    found = sheet_revisions.older_copies(
        _map(_entry(2, ["MW-201"]), _entry(3, ["MW-201"])), plain, VENDOR
    )
    assert [(p, o.newer) for p, o in found.items()] == [(2, 3)]
    assert "the later page" in found[2].why


def test_two_different_sheets_are_both_checked() -> None:
    texts = _texts((2, f"{TITLE} REV 0"), (3, "TALL PANTRY SECTION DETAIL NOTES HARDWARE REV 1"))
    page_map = _map(_entry(2, ["MW-201"]), _entry(3, ["MW-201"]))
    assert sheet_revisions.older_copies(page_map, texts, VENDOR) == {}
    # Other sheet numbers, the same revision mark, or GV's own rounds: never left out.
    other = _map(_entry(2, ["MW-201"]), _entry(3, ["MW-202"]))
    assert (
        sheet_revisions.older_copies(
            other, _texts((2, f"{TITLE} REV 0"), (3, f"{TITLE} REV 1")), VENDOR
        )
        == {}
    )
    same = _texts((2, f"{TITLE} REV 1"), (3, f"{TITLE} REV 1"))
    assert sheet_revisions.older_copies(page_map, same, VENDOR) == {}
    rounds = _map(_entry(2, ["MW-201"], rounds=[1]), _entry(3, ["MW-201"], rounds=[2]))
    revised = _texts((2, f"{TITLE} REV 0"), (3, f"{TITLE} REV 1"))
    assert sheet_revisions.older_copies(rounds, revised, VENDOR) == {}


def test_the_report_and_the_full_check_leave_the_older_copy_out() -> None:
    page_map = _map(_entry(4, ["MW-201"]), _entry(12, ["MW-201"]))
    texts = _texts((4, f"{TITLE} REV 0"), (12, f"{TITLE} REV 1"))
    report = build(Inputs(page_map=page_map, main_file=VENDOR, texts=texts))
    line = next(line for line in report.lines if line.id == "older_copies")
    assert line.words.startswith("Page 4 is an older copy of page 12 (REV 0; page 12 is REV 1)")
    assert line.pages == (4,) and line.goes_on
    gate = SimpleNamespace(report=report)
    assert plan_supervisor.duplicate_notes(gate, [4, 12]) == [  # type: ignore[arg-type]
        (4, "an older copy of page 12 (REV 0; page 12 is REV 1): not checked")
    ]


# ---- 4. numbers drawn as lines -------------------------------------------------------------------


def test_a_vendor_page_of_unread_pen_stroke_numbers_is_a_drawing_said_unread() -> None:
    candidate = {"box": [0, 0, 100, 100], "role": "vendor", "from_upload": True, "upload": "vendor"}
    page_map: dict[str, Any] = {
        "pages": [
            {"file": VENDOR, "page": 1, "role": "general", "halves": [], "candidate": candidate},
            {"file": VENDOR, "page": 2, "role": "general", "halves": [], "candidate": candidate},
            {
                "file": ARCH,
                "page": 1,
                "role": "general",
                "halves": [],
                "candidate": {**candidate, "role": "architect", "upload": "architect"},
            },
        ]
    }
    strokes = {(VENDOR, 1): 4, (VENDOR, 2): 2, (ARCH, 1): 5}
    _found, promoted = settle_candidates(page_map, {"pages": []}, strokes)
    assert promoted == [(VENDOR, 1)]  # two numbers are too few; the architect's file is not asked
    first = page_map["pages"][0]
    assert first["role"] == "vendor" and first[NUMBERS_UNREAD] == "strokes"
    unknowns = unreadable_pages(page_map)
    assert [u["what"] for u in unknowns] == [f"Vendor page 1: {UNREADABLE_WORDS}"]
    assert unknowns[0]["why"] == "unreadable" and "Print to PDF" in unknowns[0]["needs"]
    report = build(Inputs(page_map={**page_map, "files": [VENDOR]}, main_file=VENDOR))
    line = next(line for line in report.lines if line.id == "numbers_unread")
    assert line.words.startswith("Page 1: the numbers can't be read (drawn as lines, not text)")
    assert line.who == "the vendor" and line.goes_on and "Print to PDF" in (line.todo or "")


def test_the_letter_asks_the_vendor_for_real_text() -> None:
    from app.review.assistant.send_package import UNREADABLE_ASK, Letter

    letter = Letter.__new__(Letter)
    object.__setattr__(letter, "unreadable", (3, 7))
    assert letter.unreadable_line == (
        f"The numbers on pages 3, 7 are drawn as lines, not text, so they could not be read. "
        f"{UNREADABLE_ASK}"
    )
    assert "Save as PDF or Export" in UNREADABLE_ASK and "not Print to PDF" in UNREADABLE_ASK


def test_preflight_module_keeps_its_exact_copy_finder() -> None:
    texts = {(VENDOR, 1): TITLE * 3, (VENDOR, 2): TITLE * 3}
    assert preflight.duplicate_pages(VENDOR, {"pages": []}, texts) == {2: 1}


# ---- 5. room and wall sizes only ----------------------------------------------------------------


def _plan(*sizes: tuple[str, str, str, float, float]) -> Drawing:
    """A plan view with labels (text, inches, orientation, x, y)."""
    labels = tuple(
        Label(
            ref=f"p1009.D{n}",
            page=1009,
            id=f"D{n}",
            text=text,
            inches=inches,
            box=(x, y, x + 20, y + 8) if way == "h" else (x, y, x + 8, y + 20),
            side="architect",
            orientation=way,
            route="text",
        )
        for n, (text, inches, way, x, y) in enumerate(sizes, start=1)
    )
    return Drawing(
        ref="p1009.a1.view2",
        file=ARCH,
        page=1009,
        half=0,
        role="architect",
        box=(0, 0, 900, 900),
        crop_box=(0, 0, 900, 900),
        labels=labels,
    )


ROOM = (
    ("26'-4\"", "316", "h", 400, 100),  # the unit's overall, wall to wall
    ("9'-0\"", "108", "h", 200, 120),
    ("8'-4\"", "100", "h", 400, 120),
    ("9'-0\"", "108", "h", 600, 120),
    ("27'-9\"", "333", "v", 50, 400),
    ('6"', "6", "v", 700, 800),  # a wall's thickness
    ("10'", "120", "h", 850, 50),  # a graphic scale
    ("8.1", "8.1", "h", 800, 300),  # a keynote, no size
)


def test_a_plan_of_room_and_wall_sizes_only_prints_no_piece_size() -> None:
    assert worklist._room_only(_plan(*ROOM))
    # A piece's own string on it (a 30" + 36" counter run): its sizes are a piece's.
    piece = (*ROOM, ('30"', "30", "h", 300, 500), ('36"', "36", "h", 340, 500))
    assert not worklist._room_only(_plan(*piece))
    # Too few sizes to tell: unsure, it keeps its sizes.
    assert not worklist._room_only(_plan(*ROOM[:2]))


def test_one_sheet_number_reused_in_two_hotels_is_two_sheets() -> None:
    """A vendor reuses a sheet number for the same piece in two hotels: only the hotel's name and
    the date change. With no revision mark it is never an older copy; with one, the set index's
    divider pages (or the printed projects) keep them apart."""
    page_map = _map(_entry(21, ["MW-201"]), _entry(37, ["MW-201"]))
    texts = _texts((21, f"{TITLE} ALPHA HOTEL 10/17/2026"), (37, f"{TITLE} BETA HOTEL 07/17/2026"))
    assert sheet_revisions.older_copies(page_map, texts, VENDOR) == {}
    marked = _texts((21, f"{TITLE} ALPHA HOTEL REV 1"), (37, f"{TITLE} BETA HOTEL REV 0"))
    assert sorted(sheet_revisions.older_copies(page_map, marked, VENDOR)) == [37]
    index = {
        "pages": {
            f"{VENDOR}#{n}": {"file": VENDOR, "page": n, "introduces": n == 30, "projects": []}
            for n in (21, 30, 37)
        }
    }
    assert sheet_revisions.older_copies(page_map, marked, VENDOR, index=index) == {}
