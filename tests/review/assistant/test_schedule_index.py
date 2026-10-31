"""The set's material and finish tables (`schedule_index`), on made-up tables: unruled vendor tables,
ruled schedules drawn into a PDF, labelled countertop cells, one-digit tags, a disagreeing row, ties.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pikepdf
import pytest

from app.review.assistant import schedule_index, set_context
from app.review.assistant.page_reviewer import phrases
from app.review.assistant.schedule_index import agree, build_index, labelled_cells, table_rows
from app.review.assistant.set_context import SetContext
from app.review.assistant.under_markup import page_words


def at(text: str, x0: float, y0: float, x1: float | None = None, h: float = 8.0) -> dict[str, Any]:
    return {"text": text, "box": [x0, y0, x1 if x1 is not None else x0 + 6 * len(text), y0 + h]}


def vendor_table(stone: str = "ST-03", code: str = "QZ-16", colour: str = "Nova Grey") -> list[dict[str, Any]]:  # fmt: skip
    """A vendor sheet's unruled material table as the client's sheets print it: the tag column at
    x 952, each description in two lines to its right (the stone's product code on a line of its
    own, above its colour words), a strip of the same tags below, and callouts on the drawing."""
    return [
        at("MATERIAL SPEC : Core material- Industrial grade MDF", 953, 35, 1149),
        at("Laminate finish to match,", 986, 48, 1076),
        at("WD-02", 952, 54, 979),
        at("Acme - Smoked Walnut - AB1051", 986, 58, 1123),
        at("Laminate finish to match,", 986, 70, 1076),
        at("WD-04", 952, 77, 978),
        at("Acme AB426 - Slate Gray", 986, 80, 1086),
        at(code, 986, 93, 1021),
        at(stone, 953, 100, 974),
        at(f"Color: {colour},Quartz by GV", 986, 102, 1119),
        at("Metal finish to match,", 986, 116, 1063),
        at("MT-02", 952, 124, 977),
        at("Metalco #512 Dark Bronze", 986, 125, 1087),
        # The strip of tags under the table, and the drawing's callouts.
        at("WD-02", 958, 192, 984),
        at("WD-04", 1009, 192, 1035),
        at(stone, 1061, 192, 1083),
        at("MT-02", 1110, 192, 1135),
        at("WD-02", 834, 243, 851),
        at("WD-02", 834, 260, 851),
        at("WD-02", 834, 279, 851),
        at("ST-02 Quartz top &Side", 1049, 552, 1103),
    ]


def countertop(tag: str, words: str) -> list[dict[str, Any]]:
    """A countertop drawing's labelled stone cell (`COLOR:` then the tag and its words)."""
    return [
        at("COLOR：", 432, 527, 456),
        at(f"{tag} {words}", 490, 527, 623),
        at("QUANTITY:", 551, 507),
    ]


# ---- reading one page ------------------------------------------------------------------------------


def test_an_unruled_vendor_table_gives_each_tag_its_own_lines() -> None:
    tables = table_rows(3, vendor_table())
    assert len(tables) == 1
    rows = {r.tag: r for r in tables[0].rows}
    assert list(rows) == ["WD-02", "WD-04", "ST-03", "MT-02"]
    # The stone's product code on its own line above is the stone's, never a column of its own;
    # the metal row's "Metal finish to match," never leaks into the stone's row.
    assert rows["ST-03"].description == "QZ-16 Color: Nova Grey,Quartz by GV"
    assert rows["MT-02"].description == "Metal finish to match, Metalco #512 Dark Bronze"
    assert rows["WD-02"].description == "Laminate finish to match, Acme - Smoked Walnut - AB1051"
    assert rows["WD-02"].grain == "grain" and rows["ST-03"].grain == "plain"
    assert rows["ST-03"].box == (953.0, 100.0, 974.0, 108.0)
    assert rows["ST-03"].text_box == (986.0, 93.0, 1119.0, 110.0)


def test_one_digit_tags_are_rows_but_sheet_numbers_and_strips_are_not() -> None:
    found = [
        at("ST-3", 100, 100, 118),
        at("Quartz, Snow White", 140, 100, 240),
        at("PL-1", 100, 120, 118),
        at("Lamco 1573 Frosty White", 140, 120, 300),
        # A column of sheet numbers with words beside them is not a material table.
        at("A-1", 400, 100, 412),
        at("Floor plan", 430, 100, 480),
        at("A-2", 400, 120, 412),
        at("Elevations", 430, 120, 480),
        # A strip of tags on one line, and one tag repeated down a column, are not tables.
        at("WD-02", 100, 300, 127),
        at("WD-04", 150, 300, 177),
        at("ST-3", 200, 300, 218),
        at("WD-02", 600, 100, 627),
        at("door", 640, 100, 660),
        at("WD-02", 600, 120, 627),
        at("drawer", 640, 120, 670),
    ]
    tables = table_rows(1, found)
    assert [[(r.tag, r.description) for r in t.rows] for t in tables] == [
        [("ST-3", "Quartz, Snow White"), ("PL-1", "Lamco 1573 Frosty White")]
    ]


def test_a_labelled_cell_reads_the_tag_and_its_words() -> None:
    cells = labelled_cells(13, countertop("ST-02", "QZ-19 Snow White"))
    assert [(c.tag, c.description, c.source) for c in cells] == [
        ("ST-02", "QZ-19 Snow White", "cell")
    ]
    inline = labelled_cells(1, [at("FINISH: PL-1 Lamco 1573 Frosty White", 10, 10, 300)])
    assert [(c.tag, c.description) for c in inline] == [("PL-1", "Lamco 1573 Frosty White")]
    assert labelled_cells(1, [at("COLOR：", 10, 10, 40), at("TBD", 60, 10, 80)]) == []


def test_two_descriptions_agree_on_a_product_code_or_most_of_their_words() -> None:
    assert agree("QZ-16 Color: Nova Grey,Quartz by GV", "QZ16 Nova Grey")
    assert not agree("QZ-16 Nova Grey", "QZ-19 Snow White")
    assert agree("Laminate finish to match, Acme - Pale Elm - AB523", "MDF MELAMINE Pale Elm-AB523")  # fmt: skip
    assert agree("Nova Grey quartz", "QZ-16 Nova Grey")  # no code on one side: words
    assert not agree("Acme Smoked Walnut", "Acme Pale Elm")
    assert agree("Metalco #512 Dark Bronze", "Metalco # 512 bronze")


# ---- ruled tables drawn into a PDF -----------------------------------------------------------------


def _ruled_pdf(path: Path, rotate: int = 0) -> Path:
    """A one-page PDF with a ruled finish schedule (TAG | MATERIAL | COLOR), drawn so it reads
    upright as shown: on a `/Rotate 90` page the content is drawn turned, as on the client's
    countertop sheets."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )

    def place(sx: float, sy: float) -> tuple[float, float]:
        """A point as shown (origin top-left) in PDF space (origin bottom-left, 612 x 792)."""
        return (sy, sx) if rotate == 90 else (sx, 792 - sy)

    xs, ys = (100, 160, 300, 440), (100, 116, 132, 148)
    ops = ["0.5 w"]
    for y in ys:
        (ax, ay), (bx, by) = place(xs[0], y), place(xs[-1], y)
        ops.append(f"{ax} {ay} m {bx} {by} l S")
    for x in xs:
        (ax, ay), (bx, by) = place(x, ys[0]), place(x, ys[-1])
        ops.append(f"{ax} {ay} m {bx} {by} l S")
    cells = [
        ("TAG", "MATERIAL", "COLOR"),
        ("ST-3", "Quartz QZ-16", "Nova Grey"),
        ("PL-1", "Laminate WA-1573", "Frosty White"),
    ]
    turn = "0 1 -1 0" if rotate == 90 else "1 0 0 1"
    for row, texts in enumerate(cells):
        for col, text in enumerate(texts):
            tx, ty = place(xs[col] + 4, ys[row] + 11)
            ops.append(f"BT /F1 7 Tf {turn} {tx} {ty} Tm ({text}) Tj ET")
    page = pikepdf.Page(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=[0, 0, 612, 792],
            Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
            Contents=pdf.make_stream("\n".join(ops).encode()),
        )
    )
    if rotate:
        page.obj["/Rotate"] = rotate
    pdf.pages.append(page)
    out = path / f"ruled-{rotate}.pdf"
    pdf.save(out)
    return out


@pytest.mark.parametrize("rotate", [0, 90])
def test_a_ruled_schedule_is_read_where_its_words_are(tmp_path: Path, rotate: int) -> None:
    pdf = _ruled_pdf(tmp_path, rotate)
    found = phrases(page_words(pdf, 1))
    tables, searched = schedule_index.ruled_tables(pdf, {1: found})
    assert searched == 1
    assert len(tables) == 1
    table = tables[0]
    assert table.header == ("TAG", "MATERIAL", "COLOR")
    assert [(r.tag, r.description, r.source) for r in table.rows] == [
        ("ST-3", "Quartz QZ-16 Nova Grey", "ruled"),
        ("PL-1", "Laminate WA-1573 Frosty White", "ruled"),
    ]
    # The tag's cell holds the tag's words as the page shows them (the same frame, rotated or not).
    word = next(p for p in found if p["text"] == "ST-3")
    cell = table.rows[0].box
    middle = ((word["box"][0] + word["box"][2]) / 2, (word["box"][1] + word["box"][3]) / 2)
    assert cell[0] <= middle[0] <= cell[2] and cell[1] <= middle[1] <= cell[3]
    # The index prefers the ruled row and does not count the same rows twice.
    index = build_index({1: found}, pdf)
    assert [r.source for r in index.rows[1]] == ["ruled", "ruled"]


def test_a_grid_with_no_tag_column_or_no_header_is_not_a_schedule() -> None:
    box = (0.0, 0.0, 10.0, 10.0)
    title_block = [
        [("PROJECT NAME", box), ("DESCRIPTION", box), ("ITEM NO.", box)],
        [("PROJECT X", box), ('48" Standard Vanity', box), ("VN - 101", box)],
    ]
    assert schedule_index.read_grid(1, title_block, box) is None
    no_header = [[("ST-3", box), ("Quartz", box)], [("PL-1", box), ("Laminate white", box)]]
    assert schedule_index.read_grid(1, no_header, box) is None
    found = schedule_index.read_grid(
        1,
        [
            [("", box), ("MARK", box), ("MANUFACTURER", box), ("DESCRIPTION", box)],
            [("", box), ("WD-1", box), ("Acme", box), ("Smoked Walnut", box)],
            [("", None), ("", box), ("", box), ("", box)],
        ],
        box,
    )
    assert found is not None
    assert [(r.tag, r.description) for r in found.rows] == [("WD-1", "Acme Smoked Walnut")]


def test_header_rows_need_two_header_words_on_a_line() -> None:
    assert schedule_index.header_rows([at("MATERIAL SPEC", 10, 10, 80)]) == []
    assert schedule_index.header_rows([at("COLOR：", 10, 10, 40)]) == []
    assert schedule_index.header_rows([at("TAG", 10, 10, 30), at("FINISH", 80, 10, 120)]) == [
        (10.0, 10.0, 120.0, 18.0)
    ]
    assert schedule_index.header_rows([at("MARK MANUFACTURER COLOR", 10, 10, 200)]) == [
        (10.0, 10.0, 200.0, 18.0)
    ]


# ---- the set legend and its disagreements ----------------------------------------------------------


def _context(pages: dict[int, list[dict[str, Any]]], count: int) -> SetContext:
    return SetContext("Vendor set.pdf", lambda page: pages[page], count)


def _sheet(item: str, *rows: tuple[str, str]) -> list[dict[str, Any]]:
    found = [at(item, 100, 700, 160)]
    for n, (tag, words) in enumerate(rows):
        found += [at(tag, 900, 50 + 20 * n, 925), at(words, 940, 50 + 20 * n, 1100)]
    return found


def test_the_legend_takes_the_majority_and_a_disagreeing_row_is_a_fact() -> None:
    walnut, elm = ("WD-02", "Acme Smoked Walnut AB1051"), ("WD-02", "Acme Pale Elm AB523")
    stone = ("ST-3", "QZ-16 Nova Grey")
    pages = {
        1: _sheet("CAB-801.C", walnut, stone),
        2: _sheet("CAB-802.C", walnut, stone),
        3: _sheet("CAB-803.C", walnut, stone),
        4: _sheet("CAB-804.C", elm, stone),  # the odd row
    }
    context = _context(pages, 4)
    context.build()
    index = context.schedule
    assert index is not None
    legend = index.legend["WD-02"]
    assert legend.majority is not None and legend.majority.pages == [1, 2, 3]
    assert [g.pages for g in legend.groups] == [[1, 2, 3], [4]]
    lines = context.schedule_facts(4)
    expected = (
        "This page's table row WD-02 'Acme Pale Elm AB523' — set legend (p1, p2, p3): "
        "WD-02 'Acme Smoked Walnut AB1051'"
    )
    assert any(line.startswith(expected) for line in lines), lines
    assert "  ST-3: 'QZ-16 Nova Grey' (p1, p2, p3, p4)" in lines
    assert not any(line.startswith("This page's table row") for line in context.schedule_facts(1))
    # It reaches the reviewer with the set's other facts.
    assert "set legend (p1, p2, p3)" in context.facts_for(4)
    assert schedule_index.log_block(index, context)["row_disagreements"] == 1


def test_a_tie_has_no_majority_and_flags_no_row() -> None:
    plain = ("PL-1", "Lamco Frosty White")
    pages = {
        1: _sheet("CAB-801.C", ("WD-02", "Acme Smoked Walnut AB1051"), plain),
        2: _sheet("CAB-802.C", ("WD-02", "Acme Pale Elm AB523"), plain),
    }
    context = _context(pages, 2)
    context.build()
    assert context.schedule is not None
    assert context.schedule.legend["WD-02"].majority is None
    lines = context.schedule_facts(1)
    assert any(line.startswith("  WD-02 (split, no majority):") for line in lines), lines
    assert not any(line.startswith("This page's table row") for line in lines)


def _pair_pages() -> dict[int, list[dict[str, Any]]]:
    """The case of issues 9/10 of the 2026-10-31 failure map, made up: the cabinet sheet's table row
    says ST-03 Nova Grey, its countertop drawing (p13) and the set's other tables say ST-02 Snow
    White."""
    return {
        2: [at("CAB-801.C", 100, 700, 160), *vendor_table("ST-02", "QZ-19", "Snow White")],
        3: [at("CAB-802.C", 100, 700, 160), *vendor_table()],
        4: [at("CAB-803.C", 100, 700, 160), *vendor_table()],
        12: [at("CAB-801.CT", 100, 700, 170), *countertop("ST-02", "QZ-19 Snow White")],
        13: [at("CAB-802.CT", 100, 700, 170), *countertop("ST-02", "QZ-19 Snow White")],
        14: [at("CAB-803.CT", 100, 700, 170), *countertop("ST-03", "QZ-16 Nova Grey")],
    }


def test_a_row_that_disagrees_with_the_items_other_drawing_is_a_fact() -> None:
    context = _context(_pair_pages(), 14)
    lines = context.schedule_facts(3)
    assert (
        "This page's table row ST-03 'QZ-16 Color: Nova Grey,Quartz by GV' — the item's "
        "other drawing p13 (CAB-802.CT) lists ST-02 'QZ-19 Snow White' (set legend ST-02: p2, "
        "p12, p13). One of them is wrong (please check)."
    ) in lines
    assert (
        "Callouts here print ST-02, which this page's own table does not list; the set's tables say "
        "ST-02 is 'QZ-19 Snow White' (p2, p12, p13). The callout or the table is wrong (please "
        "check)."
    ) in lines
    # p4's table and its countertop drawing agree: only its ST-02 callout is a lead.
    assert not any("the item's other drawing" in line for line in context.schedule_facts(4))


def test_code_cards_only_while_schedule_red_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(_pair_pages(), 14)
    monkeypatch.setattr(schedule_index, "SCHEDULE_RED", False)
    off = set_context.check_page(context, 3)
    monkeypatch.setattr(schedule_index, "SCHEDULE_RED", True)
    on = set_context.check_page(context, 3)
    # The cross-sheet check already marks ST-03 (its first spot is the table row's tag cell); the
    # schedule adds the row's description cells to that card instead of a second card.
    assert [c["vendor"] for c in on] == [c["vendor"] for c in off]
    card = next(c for c in on if c["vendor"] == "ST-03")
    boxes = [(a["vendor_box"]["x0"], a["vendor_box"]["y0"]) for a in card["also_on"]]
    assert (986.0, 93.0) in boxes
    assert all(c["whole_look"]["proof"] == "likely_only" for c in on)  # never red


def test_a_schedule_card_of_its_own_marks_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    walnut, elm = ("WD-02", "Acme Smoked Walnut AB1051"), ("WD-02", "Acme Pale Elm AB523")
    stone = ("ST-3", "QZ-16 Nova Grey")
    pages = {n: _sheet(f"CAB-80{n}.C", walnut, stone) for n in (1, 2, 3)}
    pages[4] = _sheet("CAB-804.C", elm, stone)
    context = _context(pages, 4)
    monkeypatch.setattr(schedule_index, "SCHEDULE_RED", False)
    assert set_context.check_page(context, 4) == []
    monkeypatch.setattr(schedule_index, "SCHEDULE_RED", True)
    cards = set_context.check_page(context, 4)
    assert len(cards) == 1
    card = cards[0]
    assert card["item"] == "Material table row WD-02: disagrees with the set's other tables"
    assert card["vendor_box"]["x0"] == 900.0 and card["vendor_box"]["y0"] == 50.0
    assert [a["vendor_box"]["x0"] for a in card["also_on"]] == [940.0]
    assert card["whole_look"]["claim"] == "please_check"


def test_a_set_with_no_tables_says_nothing_and_a_broken_read_is_never_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context({1: [at("WD-02 door", 10, 10)], 2: [at("ST-02 top", 10, 10)]}, 2)
    assert context.schedule_facts(1) == []

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no")

    monkeypatch.setattr(schedule_index, "build_index", broken)
    other = _context({1: [at("WD-02 door", 10, 10)], 2: [at("ST-02 top", 10, 10)]}, 2)
    assert other.schedule_facts(1) == []
    assert "you are reviewing p1" in other.facts_for(1)
