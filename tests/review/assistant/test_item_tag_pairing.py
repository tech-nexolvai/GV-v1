"""Pairing by the item tag both sides print, when the vendor draws one item on many sheets (owner,
2026-11-03: "fix it properly and dynamic as we have other drawings too").

On a real two-file set code paired 0 of 8 vanity pages although the architect prints the vendor's
own item tags. Four rules stood in the way, each now decided by evidence code already has:

1. **An item drawn on many sheets is still an item.** A tag the vendor's own title block names as
   its sheet's item (the ITEM cell, `title_block.cells`) and numbered as an item (100 or more)
   names its piece however many sheets draw it; a finish (numbered from 1) or a product's model
   number (never in the item cell) printed on many sheets is still no item.
2. **One job, two wordings.** An architect page whose title block prints part of the job's name
   ("PROJECT NORTH" of "PROJECT NORTH SPRINGFIELD") is the job's, unless another project of the
   file shares that part; a page naming another project is still never a candidate.
3. **The item's parts.** The architect's `XX-301.C`, `XX-301-ST`, `XX-301-ACC` are the item's
   own pieces; `XX-301A` (a variant), `XX-301-1`, `XX-301-ADA` are other items.
4. **The swap audit and a printed tag.** A pair the architect's printed tag made is doubted only
   on strong size evidence (both drawings fit the other page, or one by more than one size); one
   coincident size is weak.

Synthetic sets with made-up tags only: $0, no model call (the set index is a fake read).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from app.review.assistant.simple_check import pairing, projects
from app.review.assistant.simple_check.pairing import Facts, swap_is_strong
from app.review.assistant.simple_check.worklist import worklist_from
from tests.review.assistant.test_cross_file_projects import _entry, _label, _phrase, _read

F = Fraction
SHOP, ARCH = "shop.pdf", "arch.pdf"
NORTH = "PROJECT NORTH SPRINGFIELD"


def _page(
    file: str, page: int, side: str, sizes: tuple[int, ...], words: list[str]
) -> dict[str, Any]:
    """A page's labels (its sizes, the first across, the rest up and down) and its printed words,
    one per line down the right-hand side."""
    labels = [
        _label(
            n + 1, f'{v}"', v, [300 + 40 * n, 200, 320 + 40 * n, 210], side, "h" if n == 0 else "v"
        )
        for n, v in enumerate(sizes)
    ]
    phrases = [_phrase(w, [900, 600 + 12 * n, 980, 608 + 12 * n]) for n, w in enumerate(words)]
    return {"file": file, "page": page, "labels": labels, "phrases": phrases}


def _item_cell(tag: str) -> list[str]:
    """A title block's ITEM cell over its value (`title_block.cells`: the value below its anchor)."""
    return ["ITEM NO.", tag]


def _work(
    vendor: dict[int, tuple[tuple[int, ...], list[str], str]],
    architect: dict[int, tuple[tuple[int, ...], list[str], str, str]],
) -> Any:
    """A two-file set: each vendor page's sizes, printed words and item title; each architect
    page's sizes, printed words, item title and the project name its title block prints."""
    pages = [_entry(SHOP, p, "vendor") for p in vendor] + [
        _entry(ARCH, p, "architect") for p in architect
    ]
    dims = {
        "pages": [
            *(_page(SHOP, p, "vendor", sizes, words) for p, (sizes, words, _t) in vendor.items()),
            *(
                _page(ARCH, p, "architect", sizes, words)
                for p, (sizes, words, _t, _n) in architect.items()
            ),
        ]
    }
    index = {
        "version": 1,
        "pages": {
            **{
                f"{SHOP}#{p}": _read(SHOP, p, item=title, projects=[NORTH])
                for p, (_s, _w, title) in vendor.items()
            },
            **{
                f"{ARCH}#{p}": _read(ARCH, p, item=title, projects=[name])
                for p, (_s, _w, title, name) in architect.items()
            },
        },
    }
    return worklist_from(
        {
            "files": [SHOP, ARCH],
            "upload_roles": {SHOP: "vendor", ARCH: "architect"},
            "pages": pages,
        },
        dims=dims,
        set_index=index,
        main_file=SHOP,
    )


def _pages(work: Any, page: int) -> list[int]:
    pair = next(p for p in work.across if p.vendor.own_page == page)
    by = {c.id: c.page for c in pair.candidates}
    return sorted(by[c] for c in pair.chosen)


def _pair(work: Any, page: int) -> Any:
    return next(p for p in work.across if p.vendor.own_page == page)


#: Five vendor sheets of one vanity (its elevation, top, frame, countertop, sink), a desk sheet,
#: and the architect's three pages: the vanity's bath sheet (its parts tagged), the desk's sheet,
#: a sheet of another room. No size is shared: only the tags can pair.
VANITY_SHEETS = (1, 2, 3, 4, 5)


def _vanity_set(
    *,
    cell: str | None = "XX-301",
    tag: str = "XX-301",
    architect_tag: str = "XX-301.C",
    architect_project: str = NORTH,
) -> Any:
    vendor: dict[int, tuple[tuple[int, ...], list[str], str]] = {}
    for p in VANITY_SHEETS:
        words = [tag, "VANITY TYPE 1"]
        if p == 1 and cell is not None:
            words = [*_item_cell(cell), "VANITY TYPE 1"]
        vendor[p] = ((50 + p, 21, 33), words, "VANITY TYPE 1")
    vendor[6] = ((90, 30, 29), ["XX-305", "RECEPTION DESK"], "RECEPTION DESK")
    architect = {
        11: (
            (96, 60, 18),
            [architect_tag, "XX-301-ST", "BATH ELEVATION"],
            "GUEST BATH",
            architect_project,
        ),
        12: ((120, 42, 36), ["XX-305", "DESK ELEVATION"], "LOBBY", NORTH),
        13: ((144, 80, 40), ["CORRIDOR ELEVATION"], "CORRIDOR", NORTH),
    }
    return _work(vendor, architect)


# ---- 1. an item drawn on many sheets ------------------------------------------------------------


def test_an_item_named_in_the_title_block_pairs_on_every_sheet_that_draws_it() -> None:
    """XX-301 is printed on all five of the vanity's sheets (more than the few sheets a tag was
    allowed before: it read as a finish); the vendor's title block names it as the item. Every
    sheet pairs with the architect's page printing the item's parts, by code (not asked)."""
    work = _vanity_set()
    for page in VANITY_SHEETS:
        pair = _pair(work, page)
        assert _pages(work, page) == [11], (page, pair.why)
        assert pair.code_paired and "XX-301" in pair.why
    # The desk's own tag still pairs its sheet.
    assert _pages(work, 6) == [12]


def test_a_tag_on_many_sheets_no_title_block_names_is_still_no_item() -> None:
    """The same five sheets, no ITEM cell: a tag on that many sheets may be a finish or a
    product's model number (a sink printed on five vanities' sheets), so it pairs nothing."""
    work = _vanity_set(cell=None)
    for page in VANITY_SHEETS:
        pair = _pair(work, page)
        assert pair.chosen == () and not pair.code_paired, (page, pair.why)


def test_a_finish_numbered_from_one_in_the_item_cell_stays_a_finish() -> None:
    """A code numbered under 100 (`XX-031`, the way a schedule numbers finishes) is never lifted
    by the title block: on five sheets it still pairs nothing."""
    work = _vanity_set(cell="XX-031", tag="XX-031", architect_tag="XX-031.C")
    for page in VANITY_SHEETS:
        assert _pair(work, page).chosen == ()


def test_title_block_items_are_read_by_code_from_the_item_cell() -> None:
    assert pairing.numbered_as_item("XX301") and pairing.numbered_as_item("XX1001")
    assert not pairing.numbered_as_item("XX31") and not pairing.numbered_as_item("LA2")


# ---- 2. one job, two wordings -------------------------------------------------------------------


def test_one_job_read_whole_and_in_part_is_one_project() -> None:
    north, brand = "project north springfield", "project north"
    assert projects.same_job(north, north)
    assert projects.same_job(brand, north, {north, brand})
    assert projects.same_job(north, brand, {north, brand})
    # Another project of the file shares the part: the brand alone names neither.
    assert not projects.same_job(brand, north, {north, brand, "project north shelbyville"})
    # A word of one the other lacks, on both sides: another job.
    assert not projects.same_job(
        "project north", "project south", {"project north", "project south"}
    )
    assert not projects.same_job("project west", north, {north, "project west"})


def test_an_architect_page_printing_part_of_the_jobs_name_is_a_candidate() -> None:
    """The vanity's bath sheet prints only "PROJECT NORTH" in its title block, the rest of the
    architect's file the full name: the same job, so it pairs (before, it was no candidate)."""
    work = _vanity_set(architect_project="PROJECT NORTH")
    assert _pages(work, 1) == [11]


def test_an_architect_page_naming_another_project_is_never_a_candidate() -> None:
    work = _vanity_set(architect_project="PROJECT WEST")
    pair = _pair(work, 1)
    assert pair.chosen == () and all(c.page != 11 for c in pair.candidates)


# ---- 3. the item's parts ------------------------------------------------------------------------


def test_the_items_parts_after_a_separator_are_the_item() -> None:
    for text in ("XX-301.C", "XX-301-ST", "XX-301-ACC", "XX-301.ST", "XX-301_ACC", "XX-301.CT"):
        assert pairing.printed_item_tag([text], "XX301") == "XX-301", text
    # Unchanged: alone, a top's T, a sink's SK.
    for text in ("XX-301", "XX-301-T", "XX-301T", "XX-301-SK"):
        assert pairing.printed_item_tag([text], "XX301") == "XX-301", text
    # Another item: a letter glued to the number (a variant), a further numbered part, a variant
    # or hand word, a single letter after a hyphen, a long word, two parts.
    for text in (
        "XX-301A",
        "XX-301-1",
        "XX-301.1",
        "XX-301-ADA",
        "XX-301.ADA",
        "XX-301-HC",
        "XX-301.L",
        "XX-301.R",
        "XX-301-LH",
        "XX-301-RH",
        "XX-301-A",
        "XX-301-CUST",
        "XX-301-ST.C",
        "XX-301ST.C",
        "XX-3010",
    ):
        assert pairing.printed_item_tag([text], "XX301") is None, text


def test_a_variant_of_the_item_on_the_architects_page_pairs_nothing() -> None:
    """`XX-301-ADA` is the accessible unit, never the standard vanity's part: its tag pairs
    nothing, and a page printing it with the item's own part (`XX-301-ST`) is an accessible
    page, ruled out for a standard unit (code's variant rule, as before)."""
    work = _vanity_set(architect_tag="XX-301-ADA")
    assert _pair(work, 1).chosen == ()
    alone = _work(
        {1: ((51, 21, 33), [*_item_cell("XX-301"), "VANITY"], "VANITY")},
        {
            11: ((96, 60, 18), ["XX-301-ADA", "BATH ELEVATION"], "GUEST BATH", NORTH),
            12: ((120, 42, 36), ["DESK ELEVATION"], "LOBBY", NORTH),
        },
    )
    assert _pair(alone, 1).chosen == () and "item tag XX-301" not in _pair(alone, 1).why


# ---- 4. the swap audit and a printed tag ---------------------------------------------------------


def _drawn(ref: str, *sizes: int) -> Facts:
    return Facts(ref, values=tuple(F(v) for v in sizes), size_rows=())


def test_one_coincident_size_is_weak_two_witnesses_are_strong() -> None:
    wide, narrow = _drawn("v1", 54, 30), _drawn("v2", 42, 32)
    # The narrow vanity's 42" is a wall height on the wide one's sheet; nothing else fits.
    wall = _drawn("a1", 42, 96, 18)
    other = _drawn("a2", 96, 60, 18)
    assert pairing.swap_audit((wide, [wall]), (narrow, [other])) == (2, 0)
    assert not swap_is_strong((wide, [wall]), (narrow, [other]))
    # Both drawings fit the other page: a real crossing.
    both = _drawn("a2", 54, 96, 18)
    assert swap_is_strong((wide, [wall]), (narrow, [both]))
    # One drawing, both of its overall sizes on the other page: more than one shared size.
    two_sizes = _drawn("a1", 42, 32, 96)
    assert swap_is_strong((wide, [two_sizes]), (narrow, [other]))
    assert pairing.ONE_SIZE_POINTS == 2


def _swap_set(wide_sheet: tuple[int, ...], narrow_sheet: tuple[int, ...]) -> Any:
    """Two vanities, each tagged by the architect on its own bath sheet; both sheets carry one item
    title, so the swap audit compares them."""
    return _work(
        {
            1: ((54, 21, 30), [*_item_cell("XX-301"), "VANITY TYPE 1"], "VANITY TYPE 1"),
            2: ((42, 21, 32), [*_item_cell("XX-302"), "VANITY TYPE 2"], "VANITY TYPE 2"),
        },
        {
            11: (wide_sheet, ["XX-301.C", "BATH ELEVATION"], "GUEST BATH", NORTH),
            12: (narrow_sheet, ["XX-302.C", "BATH ELEVATION"], "GUEST BATH", NORTH),
        },
    )


def test_a_printed_tag_pair_survives_one_coincident_size() -> None:
    """The narrow vanity's 42" is printed on the wide one's sheet (a wall height), nothing else
    fits either way: weak, both tag pairs stay."""
    work = _swap_set((42, 96, 18), (96, 60, 18))
    assert _pages(work, 1) == [11] and _pages(work, 2) == [12]


def test_a_printed_tag_pair_is_unsure_on_strong_size_evidence() -> None:
    """Each vanity's width is printed on the other's sheet and not on its own: both pairs may be
    crossed, whatever the tags say ("pairing unsure", a person confirms)."""
    work = _swap_set((42, 96, 18), (54, 60, 18))
    for page in (1, 2):
        pair = _pair(work, page)
        assert pair.chosen == () and pairing.PAIRING_UNSURE in pair.why, pair.why
        assert "swap audit" in pair.why
