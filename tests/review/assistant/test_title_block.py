"""The title block's cells read by code and checked against the item's other drawing
(`title_block`): made-up templates, items and values only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import title_block
from app.review.assistant.set_context import SetContext


def _p(text: str, x0: float, y0: float, x1: float, y1: float | None = None) -> dict[str, Any]:
    return {"text": text, "box": [x0, y0, x1, y0 + 9 if y1 is None else y1], "n": len(text.split())}


def _cabinet_sheet(item: str, description: str, date: str, page: str) -> list[dict[str, Any]]:
    """A vendor's cabinet sheet: a row of anchors over a row of values (one template per vendor)."""
    return [
        _p('30"', 200, 100, 215),
        _p("PROJECT NAME", 380, 700, 450),
        _p("DESCRIPTION", 500, 700, 565),
        _p("ITEM NO.", 675, 700, 720),
        _p("DATE", 930, 700, 955),
        _p("REV.DATE", 980, 700, 1025),
        _p("PAGE", 1110, 700, 1135),
        _p("MADE-UP PLAZA", 370, 730, 460),
        _p("UPPER LEVEL", 380, 740, 445),
        _p(description, 495, 732, 575),
        _p(item, 675, 732, 718),
        _p(date, 920, 732, 965),
        _p(page, 1117, 732, 1126),
        _p("Copyright notice: these made-up drawings belong to nobody, " * 2, 38, 776, 1100),
    ]


def _stone_sheet(item: str, description: str, date: str) -> list[dict[str, Any]]:
    """A countertop drawing on another template: the item number over its name, a spec block."""
    return [
        _p("DRAWING # 3", 41, 23, 99),
        _p(item, 41, 35, 90),
        _p(description, 41, 46, 140),
        _p('36"', 300, 100, 315),
        _p("DATE: ____________", 379, 471, 486),
        _p("JOB SPECIFICATIONS", 497, 486, 599),
        _p("PROJECT:", 432, 547, 456),
        _p("Made-up Plaza - Upper Level", 460, 546, 600),
        _p("DATE:", 432, 566, 447),
        _p("2026/03/09", 470, 566, 526),
    ]


def _context(pages: dict[int, list[dict[str, Any]]]) -> SetContext:
    def read(page: int) -> list[dict[str, Any]]:
        if page not in pages:
            raise ValueError("no text")
        return pages[page]

    context = SetContext("Vendor set.pdf", read, max(pages))
    context.build()
    return context


def test_cells_are_read_below_their_anchor_in_its_column() -> None:
    found = _cabinet_sheet("ABC-101.C", "Cabinets @ break room", "02/14/2026", "7")
    cells = {c.name: (c.anchor, c.value) for c in title_block.cells(found)}
    assert cells["project"] == ("PROJECT NAME", "MADE-UP PLAZA UPPER LEVEL")
    assert cells["description"] == ("DESCRIPTION", "Cabinets @ break room")
    assert cells["item"] == ("ITEM NO.", "ABC-101.C")
    assert cells["date"] == ("DATE", "02/14/2026")
    assert cells["sheet"] == ("PAGE", "7")
    assert "rev_date" not in cells  # an empty cell is no cell
    assert title_block.sheet_date(found).isoformat() == "2026-02-14"  # type: ignore[union-attr]


def test_cells_right_of_a_colon_blank_cells_and_headings() -> None:
    found = _stone_sheet("ABC-101.CT", "Break room", "x")
    cells = {c.name: c.value for c in title_block.cells(found)}
    assert cells["sheet"] == "3"  # "DRAWING # 3": anchor and value in one phrase
    assert cells["project"] == "Made-up Plaza - Upper Level"  # not "SPECIFICATIONS"
    assert cells["date"] == "2026/03/09"  # the blank approval date is skipped
    assert title_block.description_of(found, "ABC-101.CT") == "Break room"
    assert {c.value for c in title_block.cells([_p("ITEM 101", 1100, 760, 1140)])} == {"101"}
    # Words that start like an anchor are no cell.
    assert title_block.cells([_p("DATED PLANS", 10, 10, 80), _p("PAGEANT", 10, 30, 80)]) == []


def test_the_item_cell_and_description_are_checked_against_the_item_s_other_drawing() -> None:
    cabinet = _cabinet_sheet("ABC-101.C", "Cabinets @ break room", "02/14/2026", "7")
    stone = _stone_sheet("ABC-101.CT", "Pantry wall", "x")
    context = _context({1: cabinet, 2: stone})
    lines = title_block.check(context, 1, cabinet)
    assert len(lines) == 2
    assert lines[0].startswith(
        "Item cell prints 'ABC-101.C'; the item's other drawing p2 is ABC-101.CT"
    )
    assert "(ABC-101.C / ABC-101.CT)" in lines[0] and "please check (kind F" in lines[0]
    assert "describes it as 'Pantry wall' (this cell lacks: pantry, wall)" in lines[1]
    # A cell naming both items, and a description holding every word of the other's: nothing.
    both = _cabinet_sheet("ABC-101.C / ABC-101.CT", "Cabinets @ break room", "02/14/2026", "7")
    same = _stone_sheet("ABC-101.CT", "Break room", "x")
    assert title_block.check(_context({1: both, 2: same}), 1, both) == []
    # The stone drawing has no DESCRIPTION cell: its description is not checked there.
    assert title_block.check(context, 2, stone) == []


def test_a_sheet_older_than_the_newest_of_its_template_is_said_never_against_the_stone() -> None:
    old = _cabinet_sheet("ABC-101.C", "Break room", "02/14/2026", "7")
    new = _cabinet_sheet("ABC-102.C", "Lobby", "03/02/2026", "8")
    stone = _stone_sheet("ABC-101.CT", "Break room", "x")  # dated 2026/03/09: another template
    lines = title_block.check(_context({1: old, 2: new, 3: stone}), 1, old)
    assert lines[-1].startswith("Date 02/14/2026 is older than the newest date on the set's sheets")
    assert "(03/02/2026, p2)" in lines[-1]
    assert title_block.check(_context({1: old, 2: new, 3: stone}), 2, new) == []


def test_the_hook_logs_the_cells_and_gives_them_without_the_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cabinet = _cabinet_sheet("ABC-101.C", "Cabinets @ break room", "02/14/2026", "7")
    stone = _stone_sheet("ABC-101.CT", "Pantry wall", "x")
    context = _context({1: cabinet, 2: stone})
    review = SimpleNamespace(page=1, found=cabinet, knowledge={})
    (block,) = title_block.blocks(review, lambda: context)
    text = block["text"]
    assert "Cells: PROJECT NAME: MADE-UP PLAZA UPPER LEVEL; DESCRIPTION: Cabinets" in text
    assert (
        "The item's other drawing p2: item ABC-101.CT, description 'Pantry wall', dated 03/09/2026."
        in text
    )
    assert text.count("Differs: ") == 2 and "never red" in text
    assert review.knowledge["title_cells"]["ITEM NO."] == "ABC-101.C"
    assert len(review.knowledge["title_mismatches"]) == 2

    def broken() -> SetContext:
        raise RuntimeError("no workspace")

    alone = SimpleNamespace(page=1, found=cabinet, knowledge={})
    (block,) = title_block.blocks(alone, broken)
    assert "Differs: nothing code could check." in block["text"]
    assert alone.knowledge["title_mismatches"] == []
    nothing = SimpleNamespace(page=1, found=[_p('30"', 10, 10, 30)], knowledge={})
    assert title_block.blocks(nothing, None) == []
    assert nothing.knowledge == {"title_cells": {}, "title_mismatches": []}
