"""Two pairing gaps any drawing set can hit (Anant, 2026-11-04: "fix the system so new drawings
work"):

1. **A vendor's wrong item number.** A vendor's cabinet sheet prints another counter's item number
   (the architect's plan prints that number in another room), while the vendor's own countertop
   sheet of the same item prints the right one. Pairing followed the wrong number and ended
   "pairing unsure" between the wrong counter's plan and a page the AI matcher liked, so the whole
   check stayed incomplete. Now three printed things must agree before the number is doubted (the
   room the sheet names, the vendor's own other sheet of the item, the room the architect prints
   each number in); then pairing goes by the other number, and the wrong number is GV's
   please-check (never red).
2. **Sheets with a printed sheet number.** A vendor's copy of the architect's key plans (an older
   issue, no dimensions) was skipped as "a table, notes or pictures page", so the full check's
   helpers picked an architect page by themselves (the same page number) or found nothing to
   compare. Now a vendor page that prints the sheet number and title of exactly one architect
   page of its project is paired with it by code, compared whole, and never "nothing to compare".

Synthetic sets with made-up tags, rooms and sheet numbers: $0, no model call.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.review.assistant import full_check_roster, likely_problems, wrong_item_tag
from app.review.assistant.full_check_plan import (
    SAME_SHEET_LINE,
    CrossPairs,
    HelperJob,
    pair_lines,
    with_pairs,
)
from app.review.assistant.simple_check import pairing, worklist
from app.review.assistant.simple_check.worklist import NO_DRAWING_NOTE, worklist_from
from tests.review.assistant.test_cross_file_projects import PAGE, _entry, _label, _phrase, _read
from tests.review.assistant.test_full_check_roster import _Roster, _roster

SHOP, ARCH = "shop.pdf", "arch.pdf"
NORTH = "PROJECT NORTH"


def _divider(file: str) -> dict[str, Any]:
    return _read(file, 1, projects=[NORTH], set_kind="drawings", introduces=True)


def _work(
    vendor: dict[int, dict[str, Any]],
    architect: dict[int, dict[str, Any]],
    *,
    matches: dict[str, Any] | None = None,
) -> Any:
    """A two-file set of PROJECT NORTH (page 1 of each file is its divider). Each page:
    `labels`, `phrases`, `tags` (the architect's, read on the page), `read` (the set index's
    fields); a vendor page with no labels is typed text only."""
    pages = [_entry(SHOP, 1, None), _entry(ARCH, 1, None)]
    pages += [_entry(SHOP, p, "vendor") for p in vendor]
    pages += [_entry(ARCH, p, "architect") for p in architect]
    dims: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    index = {f"{SHOP}#1": _divider(SHOP), f"{ARCH}#1": _divider(ARCH)}
    for file, side in ((SHOP, vendor), (ARCH, architect)):
        for page, spec in side.items():
            dims.append(
                {
                    "file": file,
                    "page": page,
                    "labels": spec.get("labels", []),
                    "phrases": spec.get("phrases", []),
                }
            )
            if spec.get("tags"):
                role = "vendor" if file == SHOP else "architect"
                notes.append(
                    {
                        "file": file,
                        "page": page,
                        "drawings": [{"role": role, "box": PAGE, "tags": spec["tags"]}],
                    }
                )
            index[f"{file}#{page}"] = _read(file, page, **spec.get("read", {}))
    health = {
        "pages": [
            {"file": SHOP, "page": p, "facts": {"typed_text": 900, "drawn_letters": False}}
            for p in vendor
        ]
    }
    return worklist_from(
        {
            "files": [SHOP, ARCH],
            "upload_roles": {SHOP: "vendor", ARCH: "architect"},
            "pages": pages,
        },
        dims={"pages": dims},
        notes={"pages": notes},
        health={"pages": health["pages"]},
        set_index={"version": 1, "pages": index},
        matches=matches,
        main_file=SHOP,
    )


def _across(work: Any, page: int) -> Any:
    return next(p for p in work.across if p.vendor.own_page == page)


def _sure(ref: str, asked: list[str], picks: list[str]) -> dict[str, Any]:
    """The AI page matcher's saved answer for `ref`: sure of `picks`."""
    return {
        "version": 1,
        "answers": {
            ref: {
                "candidates": asked,
                "matches": picks,
                "confident": True,
                "reason": "the same counter",
                "prompt_version": 1,
            }
        },
    }


def _sizes(side: str, values: tuple[int, ...]) -> list[dict[str, Any]]:
    return [
        _label(n + 1, f'{v}"', v, [300 + 40 * n, 200, 330 + 40 * n, 210], side, "h" if n else "v")
        for n, v in enumerate(values)
    ]


# ---- 2. sheets paired by the sheet number both print ------------------------------------------------


def _plan_view(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "number": "1",
        "kind": "plan",
        "whole_floor": True,
        "prints_sizes": False,
    }


def _key_plan(sheet: str, title: str, *, big: str | None = None) -> dict[str, Any]:
    """A key plan page: room tags and callouts in words, no dimension; its title block's sheet
    number printed large (`big`, code's own read) when given."""
    phrases = [
        _phrase(title, [100, 700, 300, 712]),
        _phrase("T-1", [400, 300, 420, 310]),
        _phrase("T-2", [500, 300, 525, 310]),
        *([_phrase(big, [1100, 740, 1180, 790])] if big else []),
    ]
    return {"phrases": phrases, "read": {"sheet": sheet, "views": [_plan_view(title)]}}


def _key_plans(**vendor_fields: Any) -> Any:
    """The vendor's copy of the architect's second floor key plan on its page 2; the architect's
    first and second floor key plans on its pages 2 and 3 (page numbers that do not line up)."""
    vendor = {2: {**_key_plan("A1.2", "SECOND FLOOR KEY PLAN"), **vendor_fields}}
    architect = {
        2: _key_plan("A1.1", "FIRST FLOOR KEY PLAN"),
        3: _key_plan("A1.2", "SECOND FLOOR KEY PLAN"),
    }
    return _work(vendor, architect)


def test_a_key_plan_pairs_with_the_architect_sheet_of_the_same_number_not_the_same_page() -> None:
    work = _key_plans()
    pair = _across(work, 2)
    assert pair.chosen == ("p1003",) and pair.sheet == "A1.2"
    assert pair.code_paired and not pair.needed and pair.unsure == ()
    assert "sheet number A1.2 under the title SECOND FLOOR KEY PLAN" in pair.why
    item = next(i for i in work.items if i.id == "p2.v0")
    assert [d.ref for d in item.architect] == ["p1003.a1"]
    assert all(s.page != 2 for s in work.skipped if s.file == SHOP)
    # The full check's helpers are told the pair and that the sheet is compared word by word.
    cross = CrossPairs.of(work)
    assert cross.pairs[2] == ((ARCH, 3),) and cross.sheets == {2: "A1.2"}
    job = with_pairs(HelperJob(1, (2,), SHOP), cross)
    assert job.sheets == ((2, "A1.2"),)
    lines = pair_lines(job.pairs, job.unpaired, "/uploads", dict(job.sheets))
    assert lines[-1] == SAME_SHEET_LINE.format(pages="p.2 (A1.2)")
    assert "never as not comparable" in lines[-1]
    assert pair_lines(job.pairs, job.unpaired, "/uploads") == lines[:-1]


def test_a_sheet_pair_needs_the_same_title_a_sheet_number_and_one_architect_page() -> None:
    """Each of these keeps the page skipped as before (no dimension: nothing for code to pair)."""
    other_title = _work(
        {2: _key_plan("A1.2", "SECOND FLOOR KEY PLAN")},
        {3: _key_plan("A1.2", "SECOND FLOOR FINISH PLAN")},
    )
    bare = _work({2: _key_plan("4", "KEY PLAN")}, {3: _key_plan("4", "KEY PLAN")})
    twice = _work(
        {2: _key_plan("A1.2", "SECOND FLOOR KEY PLAN")},
        {
            3: _key_plan("A1.2", "SECOND FLOOR KEY PLAN"),
            4: _key_plan("A1.2", "SECOND FLOOR KEY PLAN"),
        },
    )
    # The title block's largest sheet number says another sheet than the set index read.
    doubted = _key_plans(**_key_plan("A1.2", "SECOND FLOOR KEY PLAN", big="A1.3"))
    for work in (other_title, bare, twice, doubted):
        assert all(p.vendor.own_page != 2 for p in work.across)
        assert any(s.page == 2 and s.reason == NO_DRAWING_NOTE for s in work.skipped)
        assert all(i.id != "p2.v0" for i in work.items)


def test_code_and_the_index_agreeing_on_the_sheet_number_pair_it() -> None:
    work = _key_plans(**_key_plan("A1.2", "SECOND FLOOR KEY PLAN", big="A1.2"))
    assert _across(work, 2).chosen == ("p1003",)


def test_a_copy_of_the_sheet_is_never_counted_as_nothing_to_compare(tmp_path: Path) -> None:
    """Its helper and the passes after the lead marked it not comparable (no sizes): any other
    page so marked is done (2026-11-03), a copy of the architect's sheet is not; the pass after
    the lead is told to compare its printed words."""
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    book = engine.book
    book.set_coverage({"pages": [{"page": p, "status": "same"} for p in range(1, 7)]}, "helper:1")
    for page in (2, 3):
        book.set_coverage(
            {
                "pages": [
                    {
                        "page": page,
                        "status": "not_comparable",
                        "note": "Key plan: room tags only, no millwork sizes",
                    }
                ]
            },
            "helper:7",
        )
    engine._plan = SimpleNamespace(skipped=(), jobs=(SimpleNamespace(pages=(1, 2, 3, 4, 5, 6)),))
    engine._cross = CrossPairs(pairs={2: ((ARCH, 3),)}, sheets={2: "A1.2"})
    run = full_check_roster._Run(engine=engine)
    run.vendor_pages = frozenset(range(1, 7))
    run.looks = {2: 2, 3: 2}
    assert run._nothing_to_compare() == {3: full_check_roster.NOTHING_NO_SIZES}
    assert run._pages_unfinished() == [2]
    assert "copy of the architect's sheet A1.2" in run._same_sheet(2)
    assert run._same_sheet(3) == ""


# ---- 1. a vendor's wrong item number ----------------------------------------------------------------

#: The architect's furniture plan (page 2): AB-501 in the STORE ROOM, CD-502 (and its top's CD-502T)
#: in the COPY ROOM, each room's name printed under its tags.
STORE = [300.0, 300.0, 340.0, 310.0]
COPY = [800.0, 300.0, 840.0, 310.0]


def _furniture_plan(*, cd: list[float] = COPY, extra: list[dict[str, Any]] = ()) -> dict:
    tags = [
        {"text": "AB-501", "box": STORE},
        {"text": "CD-502", "box": cd},
        {"text": "CD-502T", "box": [cd[0], cd[1] + 20, cd[2], cd[3] + 20]},
        *extra,
    ]
    phrases = [
        _phrase("FURNITURE PLAN", [100, 760, 220, 772]),
        _phrase("STORE ROOM", [290, 340, 360, 352]),
        _phrase("COPY ROOM", [790, 360, 850, 372]),
        _phrase("FILE ROOM", [1150, 340, 1210, 352]),
        *(_phrase(t["text"], t["box"]) for t in tags),
    ]
    return {
        "phrases": phrases,
        "tags": tags,
        "read": {"views": [_plan_view("FURNITURE PLAN")]},
    }


def _vendor_sheet(title: str, tag: str, sizes: tuple[int, ...]) -> dict[str, Any]:
    return {
        "labels": _sizes("vendor", sizes),
        "phrases": [_phrase(title, [700, 700, 900, 712]), _phrase(tag, [700, 720, 745, 730])],
        "read": {"item": title},
    }


def _copy_room(
    *,
    top_tag: str = "CD-502",
    title: str = "COPY ROOM - MILLWORK",
    plan: dict[str, Any] | None = None,
    sure: bool = True,
) -> Any:
    """The vendor's cabinet sheet of the copy room counter (page 2, the wrong number AB-501) and
    its countertop sheet (page 3, `top_tag`); the architect's furniture plan (page 2) and a tile
    detail sheet (page 3) the matcher is sure of for the cabinet sheet, on weak evidence."""
    vendor = {
        2: _vendor_sheet(title, "AB-501", (125, 24, 36)),
        3: _vendor_sheet(f"{top_tag} Copy Room", top_tag, (126, 25, 4)),
    }
    detail = {
        "labels": _sizes("architect", (66, 19, 48)),
        "phrases": [_phrase("COPY TILE DETAIL", [140, 380, 400, 395])],
        "read": {
            "views": [{"title": "COPY TILE DETAIL", "kind": "elevation", "prints_sizes": True}]
        },
    }
    architect = {2: plan or _furniture_plan(), 3: detail}
    matches = _sure("p2.v0", ["p1003"], ["p1003"]) if sure else None
    return _work(vendor, architect, matches=matches)


def test_a_wrong_item_number_pairs_by_the_item_number_the_room_and_the_vendors_top_give() -> None:
    work = _copy_room()
    cabinets = _across(work, 2)
    # The architect shows the copy room counter only as CD-502 on its plan: nothing to compare,
    # never "pairing unsure" between the wrong number's plan and the page the matcher liked.
    assert cabinets.needed and cabinets.chosen == () and cabinets.unsure == ()
    assert "only as a tag on a plan (CD-502 on its page 2" in cabinets.why
    assert "architect page 3 is not held for it" in cabinets.why
    wrong = cabinets.wrong_tag
    assert wrong is not None and wrong.confirmed
    assert (wrong.printed, wrong.other, wrong.room) == ("AB-501", "CD-502", "COPY ROOM")
    assert wrong.tag_rooms == ("STORE ROOM",) and wrong.other_pages == (3,)
    assert (wrong.arch_file, wrong.arch_page, wrong.arch_box) == (ARCH, 2, tuple(COPY))
    assert "the vendor's item number AB-501 does not fit" in cabinets.why
    # The countertop sheet's own number fits: it is as before, no wrong number.
    top = _across(work, 3)
    assert top.needed and top.wrong_tag is None
    assert "only as a tag on a plan (CD-502 on its page 2" in top.why


def test_the_number_is_never_doubted_on_one_printed_thing_alone() -> None:
    # The vendor's sheets of the item agree on AB-501 (a vendor naming the room its own way):
    # nothing changes, and the page the matcher is sure of is still held.
    agreed = _across(_copy_room(top_tag="AB-501"), 2)
    assert agreed.wrong_tag is None
    assert agreed.unsure == ("p1003", "p1002") and pairing.PAIRING_UNSURE in agreed.why
    # The title names no room of the architect's plans.
    unnamed = _across(_copy_room(title="BASE UNIT - MILLWORK"), 2)
    assert unnamed.wrong_tag is None and unnamed.unsure == ("p1003", "p1002")
    # The architect prints the vendor's number in the room the sheet names: it fits.
    fits = _furniture_plan(extra=[{"text": "AB-501", "box": [800.0, 380.0, 840.0, 390.0]}])
    assert _across(_copy_room(plan=fits), 2).wrong_tag is None


def test_an_unconfirmed_other_number_leaves_the_pair_unsure_and_says_why() -> None:
    """The vendor's top prints CD-502, but the architect prints CD-502 in the FILE ROOM (another
    item of the copy room keeps the room's name on the plan): the two numbers stay in doubt;
    neither pairs the cabinet sheet, which stays unsure (a person confirms), never "drawing
    needed"."""
    plan = _furniture_plan(
        cd=[1160.0, 300.0, 1200.0, 310.0], extra=[{"text": "EF-503", "box": COPY}]
    )
    cabinets = _across(_copy_room(plan=plan), 2)
    wrong = cabinets.wrong_tag
    assert wrong is not None and not wrong.confirmed and wrong.other == "CD-502"
    assert not cabinets.needed and cabinets.chosen == ()
    assert cabinets.unsure and "code goes by neither number" in cabinets.why


def test_the_wrong_number_is_a_please_check_boxed_on_both_sides() -> None:
    work = _copy_room()
    dims = {
        (SHOP, 2): worklist.views_module.phrases_of(
            {"phrases": [_phrase("ITEM NO. AB-501", [700, 720, 790, 730])]}
        )
    }
    (finding,) = wrong_item_tag.findings_from(work.across, dims)
    assert finding["page"] == 2 and finding["status"] == "check"
    assert (finding["vendor"], finding["architect"]) == ("AB-501", "CD-502")
    assert finding["vendor_box"]["x0"] == 700 and finding["vendor_box"]["file"] == SHOP
    assert finding["arch_box"]["page"] == 2 and finding["arch_box"]["file"] == ARCH
    assert finding["created_by"] == wrong_item_tag.CREATED_BY
    assert finding[likely_problems.FIELD]["claim"] == "please_check"
    assert finding["why"].startswith("The item number AB-501 on this sheet looks wrong.")
    assert "STORE ROOM" in finding["why"] and "COPY ROOM" in finding["why"]
    # Said already (a page reviewer's card naming both numbers on the page): not added again.
    said = {"page": 2, "item": "Title block: item number", "why": "prints AB-501, plan has CD-502"}
    assert wrong_item_tag._already(finding, [said])
    assert not wrong_item_tag._already(finding, [{**said, "page": 3}])


def test_an_items_words_leave_out_its_tags_kind_and_job() -> None:
    assert worklist._item_words("COPY ROOM - MILLWORK", NORTH) == {"COPY", "ROOM"}
    assert worklist._item_words("CD-502 Copy Room", NORTH) == {"COPY", "ROOM"}
    assert worklist._item_words("Base cabinets (short) @ North lobby", NORTH) == {"SHORT", "LOBBY"}
    assert worklist._item_words("MILLWORK - CD-502", NORTH) == frozenset()
    # A unit type's number is part of the item's name: two types are two items.
    assert worklist._item_words("VANITY TYPE 12", NORTH) == {"VANITY", "TYPE", "12"}
    assert worklist._item_words("VANITY TYPE 12", NORTH) != worklist._item_words(
        "VANITY TYPE 13", NORTH
    )
