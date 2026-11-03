"""What the other pages of a drawing set print, for the page reviewer (`set_context`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import set_context
from app.review.assistant.set_context import SetContext, scope_notes, tags_in


def _phrases(*texts: str) -> list[dict[str, Any]]:
    return [{"text": text, "box": [10.0, 10.0 + 12 * n, 200.0, 20.0 + 12 * n]} for n, text in enumerate(texts)]  # fmt: skip


PAGES = {
    1: _phrases("ST-02 Quartz top & side splash by GV", "WD-04 toe kick", "BIN BY GC/OWNER"),
    2: _phrases("ST-03 STONE TOP", "WD-04", "MT-001.C Metal bar"),
    3: _phrases("COUNTER TOP & SPLASH PROVIDED BY GV", "ST-02", "ML-018T.C"),
}


def _context(pages: dict[int, list[dict[str, Any]]] = PAGES, count: int = 3) -> SetContext:
    reads: list[int] = []

    def read(page: int) -> list[dict[str, Any]]:
        reads.append(page)
        if page not in pages:
            raise ValueError("no text")
        return pages[page]

    context = SetContext("Vendor set.pdf", read, count)
    context.reads = reads  # type: ignore[attr-defined]
    return context


def test_tags_and_who_supplies_it_notes_are_read_as_printed() -> None:
    assert tags_in("ST-02 Quartz, WD-04 toe kick, MT-001.C bar, ML-018T.C, GVQZ-19, SR433") == [
        "ST-02",
        "WD-04",
        "MT-001.C",
        "ML-018T.C",
        "GVQZ-19",
        "SR433",
    ]
    notes = scope_notes(_phrases("BIN BY GC/OWNER", "Top by others", "N.I.C.", "Plain note"))
    assert notes == ["BIN BY GC/OWNER", "Top by others", "N.I.C."]


def test_the_summary_lists_each_tag_and_note_with_its_pages_once_read() -> None:
    context = _context()
    text = context.summary(2)
    assert "has 3 pages; you are reviewing p2" in text
    assert "WD-04: p1, p2" in text and "ST-02: p1, p3" in text and "ST-03: p2" in text
    assert '"BIN BY GC/OWNER": p1' in text
    assert "Printed only on this page (p2): MT-001.C, ST-03" in text
    assert '"COUNTER TOP & SPLASH PROVIDED BY GV": p3' in text
    context.summary(1)
    assert context.reads == [1, 2, 3]  # type: ignore[attr-defined] - every page read once


def test_a_page_that_cannot_be_read_is_listed_never_fatal() -> None:
    context = _context({1: PAGES[1], 3: PAGES[3]})
    text = context.summary(1)
    assert "Pages not read: p2." in text
    with pytest.raises(ValueError, match="page 2 of Vendor set.pdf could not be read"):
        context.phrases(2)
    with pytest.raises(ValueError, match="no page 9"):
        context.phrases(9)
    assert context.phrases(3) == PAGES[3]


def test_a_large_set_stays_short_and_a_single_page_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(set_context, "MAX_TAGS", 2)
    many = {n: _phrases(f"WD-{n:02d}", "ST-02") for n in range(1, 21)}
    text = _context(many, 20).summary(1)
    assert "ST-02: p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12 (+8 more)" in text
    assert "(+19 more tags printed on fewer pages)" in text
    assert _context({1: PAGES[1]}, 1).summary(1).endswith("(this is the only page).")


def test_the_set_is_read_in_the_background_and_a_reviewer_waits_only_so_long() -> None:
    import threading

    gate = threading.Event()

    def slow(page: int) -> list[dict[str, Any]]:
        gate.wait(5)
        return PAGES[page]

    context = SetContext("Vendor set.pdf", slow, 3)
    assert context.ready(0.1) is False  # still reading: the reviewer goes on without it
    stop = threading.Event()
    stop.set()
    assert context.ready(30, stop) is False  # a cancel never leaves it waiting
    gate.set()
    assert context.ready(5) is True
    assert "WD-04: p1, p2" in context.summary(1)


# ---- families, pairs and code's cross-sheet please-checks (2026-10-29) -----------------------------


def _set_pages() -> dict[int, list[dict[str, Any]]]:
    """A set like the client's: cabinet sheets (p2-p4) and their countertop drawings (p12-p14);
    WD/ST/GVQZ used throughout; p3's countertop drawing tags the stone differently; p9 prints a
    code of the MT family no other sheet prints."""
    return {
        1: _phrases("INDEX"),
        2: _phrases("BOH-801.C", "WD-02 door", "ST-02 top", "GVQZ-19", "MT-02 bar"),
        3: _phrases("BOH-802.C", "WD-02 door", "ST-03 top", "GVQZ-16", "MT-02 bar", "ST-03 splash"),
        4: _phrases("BOH-803.C", "WD-02 door", "ST-03 top", "GVQZ-16", "MT-02 bar"),
        9: _phrases("GRT-806", "WD-02", "MT-05 bar", "ST-03", "GVQZ-16"),
        12: _phrases("BOH-801.CT", "ST-02", "GVQZ-19", '36"'),
        13: _phrases("BOH-802.CT", "ST-02", "GVQZ-19", '104 1/8"', '24"'),
        14: _phrases("BOH-803.CT", "ST-03", "GVQZ-16"),
    }


def test_finish_families_pairs_and_differences_are_read_from_the_set() -> None:
    context = _context(_set_pages(), 14)
    assert context.finish_families() == {"WD", "ST", "GVQZ", "MT"}  # BOH and GRT: item numbers
    assert context.item_tags(3) == ["BOH-802.C"]
    assert context.pair_pages(3) == [(13, "BOH-802.CT", "BOH-802.C")]
    assert context.pair_pages(9) == []
    diffs = context.finish_differences(3)
    assert [(d.family, d.here, d.there, d.other_page) for d in diffs] == [
        ("GVQZ", ["GVQZ-16"], ["GVQZ-19"], 13),
        ("ST", ["ST-03"], ["ST-02"], 13),
    ]
    assert context.finish_differences(2) == []
    assert [(o.tag, o.family, o.others) for o in context.outliers(9)] == [
        ("MT-05", "MT", {"MT-02": [2, 3, 4]})
    ]
    assert context.printed_sizes(13) == ['104 1/8"', '24"']
    assert context.occurrences(3, "ST-03") == [
        [10.0, 34.0, 200.0, 44.0],
        [10.0, 70.0, 200.0, 80.0],
    ]


def test_the_facts_for_a_page_name_its_other_drawing_and_the_differences() -> None:
    facts = _context(_set_pages(), 14).facts_for(3)
    assert "you are reviewing p3" in facts
    assert "ST-03: p4, p9, p14" in facts and "BOH-802.C: no other page" in facts
    assert (
        "This item's other drawing: p13 (BOH-802.CT, for BOH-802.C) prints tags BOH-802.CT, "
        'ST-02, GVQZ-19; sizes 104 1/8", 24".' in facts
    )
    assert (
        "Finish ST: this page tags ST-03; its other drawing p13 (BOH-802.CT) tags ST-02." in facts
    )
    assert "Only this page prints MT-05" in _context(_set_pages(), 14).facts_for(9)


def test_code_makes_a_please_check_per_difference_and_outlier_never_red() -> None:
    from app.review.assistant import likely_problems
    from app.review.assistant.set_context import check_page

    context = _context(_set_pages(), 14)
    found = check_page(context, 3)
    assert [f["item"] for f in found] == [
        "Finish GVQZ: this sheet and the item's other drawing differ",
        "Finish ST: this sheet and the item's other drawing differ",
    ]
    stone = found[1]
    assert stone["status"] == "check" and stone["vendor"] == "ST-03"
    assert stone["vendor_box"]["page"] == 3 and stone["vendor_box"]["y0"] == 34.0
    assert [o["vendor_box"]["y0"] for o in stone["also_on"]] == [70.0]  # the second spot
    mark = stone[likely_problems.FIELD]
    assert mark["by"] == "set_check" and mark["proof"] == "likely_only" and mark["kind"] == "finish"
    assert "It should be: ST-02 on both" in mark["words"]
    (outlier,) = check_page(context, 9)
    assert outlier["vendor"] == "MT-05" and "MT-02 (3 pages)" in outlier["why"]
    assert check_page(context, 2) == []


def test_the_finish_hook_adds_through_the_real_book_unless_the_reviewer_raised_the_tag(
    tmp_path: Path,
) -> None:
    import threading
    from types import SimpleNamespace

    from app.review.assistant import likely_problems, proof, set_context
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.page_reviewer import Reviews
    from app.review.assistant.sandbox import ChatWorkspace
    from tests.review.assistant.test_page_reviewer import FILE, _pdf

    workspace = ChatWorkspace.open(tmp_path / "chat")
    (workspace.uploads / FILE).write_bytes(_pdf())
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    book = FindingBook(workspace=workspace, listing=listing, page_count=14)
    shown: list[dict[str, Any]] = []
    reviews = Reviews()
    reviews.context[FILE] = _context(_set_pages(), 14)
    engine = SimpleNamespace(
        settings=SimpleNamespace(mode="full_check"),
        book=book,
        tools=SimpleNamespace(workspace=workspace),
        finding_lock=threading.RLock(),
        _show_finding=shown.append,
        _page_reviews=reviews,
        _plan=SimpleNamespace(jobs=[SimpleNamespace(pages=(3, 9))]),
    )
    import app.review.assistant.page_reviewer as reviewer_module

    original = reviewer_module.set_context_of
    reviewer_module.set_context_of = lambda eng, revs, file: revs.context[file]  # type: ignore[assignment]
    try:
        # The reviewer already raised the stone tag on p3: code adds the GVQZ one and p9's outlier.
        book.report(
            {
                "page": 3,
                "item": "Top: stone tag",
                "category": "other",
                "architect": "n/a",
                "vendor": "see the drawing",
                "status": "check",
                "check_reason": "pairing_not_certain",
                "verified": False,
                "source": "architect",
                "why": "ST-03 here, ST-02 on the countertop drawing",
                "vendor_box": {"page": 3, "x0": 10, "y0": 34, "x1": 200, "y1": 44, "file": FILE},
            },
            "page_reviewer",
        )
        mine = next(f for f in book.live() if f["created_by"] == "page_reviewer")
        book.replace(
            {
                **mine,
                likely_problems.FIELD: {
                    "by": "page_reviewer",
                    "proof": "likely_only",
                    "kind": "finish",
                    "words": "x",
                },
            }
        )
        assert set_context.add_to_answer(engine) == 2
    finally:
        reviewer_module.set_context_of = original  # type: ignore[assignment]
    added = [f for f in book.live() if f["created_by"] == "set_check"]
    assert sorted(f["vendor"] for f in added) == ["GVQZ-16", "MT-05"]
    for finding in added:
        assert proof.proof_group(finding) == "likely_problem"
        assert likely_problems.payload(finding)["by"] == "set_check"
        assert likely_problems.payload(finding)["kind"] == "finish"
    assert len(shown) == 2


def test_sheet_numbers_and_item_families_never_count_as_finish_families() -> None:
    """The review of 2026-10-29: a looser rule made every sheet's own number an outlier."""
    pages = {
        n: _phrases(f"MW-{100 + n}", "SEE 3/MW-501" if n in (1, 2, 3) else "", "ST-03")
        for n in range(1, 13)
    }
    pages[1].append({"text": "LA-007 toe kick", "box": [10.0, 90.0, 200.0, 100.0]})
    pages[7].append({"text": "BOH-801.C", "box": [10.0, 90.0, 200.0, 100.0]})
    pages[8].append({"text": "BOH-801.C", "box": [10.0, 90.0, 200.0, 100.0]})
    pages[9].append({"text": "BOH-801.CT", "box": [10.0, 90.0, 200.0, 100.0]})
    context = _context(pages, 12)
    assert context.finish_families() == {"ST"}  # MW: 13 codes; LA: one page; BOH: item numbers
    assert context.outliers(5) == [] and context.outliers(1) == []
    # A sheet number pairs with nothing; the item number pairs with its countertop drawing.
    assert context.pair_pages(5) == [] and context.pair_pages(7) == [(9, "BOH-801.CT", "BOH-801.C")]


def test_a_countertop_sheet_of_several_tops_gives_no_difference() -> None:
    pages = _set_pages()
    pages[13] = _phrases("BOH-802.CT", "ST-02", "ST-03", "GVQZ-19", "GVQZ-16")  # two tops drawn
    context = _context(pages, 14)
    assert context.finish_differences(3) == []
    # One code on the other drawing, this page's odd code named: still found.
    pages[13] = _phrases("BOH-802.CT", "ST-02", "GVQZ-19")
    diffs = _context(pages, 14).finish_differences(3)
    assert [(d.family, d.here, d.there) for d in diffs] == [
        ("GVQZ", ["GVQZ-16"], ["GVQZ-19"]),
        ("ST", ["ST-03"], ["ST-02"]),
    ]


def test_every_same_page_spot_names_its_finding_for_the_api(tmp_path: Path) -> None:
    import threading
    from types import SimpleNamespace

    import app.review.assistant.page_reviewer as reviewer_module
    from app.review.assistant import set_context
    from app.review.assistant.chat_contract import ChatFindingAlsoOnOut
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.page_reviewer import Reviews
    from app.review.assistant.sandbox import ChatWorkspace
    from tests.review.assistant.test_page_reviewer import FILE, _pdf

    workspace = ChatWorkspace.open(tmp_path / "chat")
    (workspace.uploads / FILE).write_bytes(_pdf())
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    book = FindingBook(workspace=workspace, listing=listing, page_count=14)
    reviews = Reviews()
    reviews.context[FILE] = _context(_set_pages(), 14)
    engine = SimpleNamespace(
        settings=SimpleNamespace(mode="full_check"), book=book,
        tools=SimpleNamespace(workspace=workspace), finding_lock=threading.RLock(),
        _show_finding=lambda f: None, _page_reviews=reviews,
        _plan=SimpleNamespace(jobs=[SimpleNamespace(pages=(3,))]),
    )  # fmt: skip
    original = reviewer_module.set_context_of
    reviewer_module.set_context_of = lambda eng, revs, file: revs.context[file]  # type: ignore[assignment]
    try:
        assert set_context.add_to_answer(engine) == 2
        stone = next(f for f in book.live() if f["vendor"] == "ST-03")
        assert stone["also_on"] and all(
            ChatFindingAlsoOnOut.model_validate(entry).finding_id == stone["id"]
            for entry in stone["also_on"]
        )
        # The set unreadable at the finish: nothing added, the check goes on.
        reviewer_module.set_context_of = lambda eng, revs, file: (_ for _ in ()).throw(FileNotFoundError("gone"))  # type: ignore[assignment]
        assert set_context.add_to_answer(engine) == 0
    finally:
        reviewer_module.set_context_of = original  # type: ignore[assignment]


def test_one_letter_cabinet_codes_are_never_a_finish_family_and_spelling_is_one_code() -> None:
    pages = {
        n: _phrases("D22 base", "W70 wall", "ST-03" if n % 2 else "ST03") for n in range(1, 13)
    }
    context = _context(pages, 12)
    assert context.finish_families() == {"ST"}  # D and W: one letter; ST03 and ST-03 one code
    assert context.outliers(3) == []


def test_the_same_note_flagged_on_several_sheets_is_one_card_with_the_others_under_it(
    tmp_path: Path,
) -> None:
    """The $2/page run of 2026-10-29: "mounting cleats ... provided by Vendor" on 6 sheets."""
    import threading
    from types import SimpleNamespace

    from app.review.assistant import likely_problems, proof, set_context
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.sandbox import ChatWorkspace
    from tests.review.assistant.test_page_reviewer import FILE, _pdf

    note = "NOTE: MOUNTING CLEATS AND ACCESSORIES BY VENDOR"
    pages = {
        3: _phrases("BOH-802.C", note, "WD-02"),
        4: _phrases("BOH-803.C", note, "WD-02"),
        5: _phrases("REG-802.C", "A NOTE ONLY HERE ONCE", "WD-02"),
    }
    context = _context(pages, 5)
    context.build()
    workspace = ChatWorkspace.open(tmp_path / "chat")
    (workspace.uploads / FILE).write_bytes(_pdf())
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    book = FindingBook(workspace=workspace, listing=listing, page_count=5)
    shown: list[dict[str, Any]] = []
    dropped: list[str] = []
    engine = SimpleNamespace(
        book=book, finding_lock=threading.RLock(), _show_finding=shown.append,
        _show_dropped=lambda fid, why: dropped.append(fid),
    )  # fmt: skip

    def reviewer_item(
        page: int, y: float, kind: str = "scope", item: str = "Note: who supplies the cleats"
    ) -> str:
        book.report(
            {
                "page": page, "item": item, "category": "other",
                "architect": "n/a", "vendor": "see the drawing", "status": "check",
                "check_reason": "pairing_not_certain", "verified": False, "source": "architect",
                "why": "The note says by Vendor without naming the party.",
                "vendor_box": {"page": page, "x0": 12, "y0": y, "x1": 120, "y1": y + 8, "file": FILE},
            },
            "page_reviewer",
        )  # fmt: skip
        found = book.live()[-1]
        book.replace(
            {
                **found,
                likely_problems.FIELD: {
                    "by": "page_reviewer",
                    "proof": "likely_only",
                    "kind": kind,
                    "words": "x",
                },
            }
        )
        return str(found["id"])

    first = reviewer_item(3, 23)  # on the note (the second phrase: y 22-32)
    second = reviewer_item(4, 23)
    other_kind = reviewer_item(4, 23, kind="labels")  # the same note, another kind: its own card
    alone = reviewer_item(5, 23)  # a note printed on one sheet only
    different = reviewer_item(4, 23, item="Stone top: edge profile not stated")  # other words
    assert set_context.fold_repeats(engine, context) == 1
    live = {f["id"]: f for f in book.live()}
    assert second not in live and dropped == [second]
    kept = live[first]
    assert [(o["page"], o["finding_id"]) for o in kept["also_on"]] == [(4, second)]  # its own id
    assert other_kind in live and alone in live and different in live
    assert proof.proof_group(kept) == "likely_problem"


def test_the_facts_name_the_seams_the_items_countertop_drawing_marks() -> None:
    pages = _set_pages()
    pages[13] = _phrases("BOH-802.CT", "ST-02", "GVQZ-19", "JOINT", "JOINT")
    context = _context(pages, 14)
    assert context.seams(13) == 2 and context.seams(3) == 0
    facts = context.facts_for(3)
    assert (
        "p13 marks 2 seams (JOINT) in the top; this page marks none. Read p13 for where, and "
        "check a support or leg sits under each." in facts
    )
    assert "seam" not in context.facts_for(2)  # its countertop drawing marks none


def test_one_digit_codes_are_tags_only_in_a_finish_family() -> None:
    from app.review.assistant.set_context import family_of, item_key

    # A schedule that numbers from 1: ST-3, PL-1, WD-2 are tags (the whole tag pipeline sees them).
    assert tags_in("ST-3 top, PL-1 panel, WD2 front, MT-001.C bar, WD-04, ST-3A") == [
        "ST-3",
        "PL-1",
        "WD2",
        "MT-001.C",
        "WD-04",
        "ST-3A",
    ]
    assert family_of("ST-3") == "ST" and item_key("PL-1") == "PL-1"
    # Sheet, detail and section numbers keep out: one digit after any other prefix is no tag.
    assert tags_in("SEE A-1, MW-1 AND S-1; DETAIL D-2; SECTION S1") == []
    assert family_of("A-1") is None and item_key("MW-1") is None
    # Two or three digits read as before for every prefix.
    assert tags_in("MW-101 A-201 BOH-802.CT") == ["MW-101", "A-201", "BOH-802.CT"]


def test_a_countertop_item_written_without_its_dot_is_the_same_item() -> None:
    from app.review.assistant.set_context import family_of, item_key

    # 2026-11-02: "ABC-101CT" (no dot) was no tag, so its cabinet sheet had no other drawing.
    assert tags_in("ABC-101CT Countertop, ABC-101.C") == ["ABC-101CT", "ABC-101.C"]
    assert item_key("ABC-101CT") == item_key("ABC-101.C") == "ABC-101"
    assert family_of("ABC-101CT") == "ABC"
    # Only CT is read as a two-letter suffix: other letters after the number keep out as before.
    assert tags_in("HW150MM ABC-101XY") == []
    pages = {1: _phrases("ABC-101.C", "WD-04 toe kick"), 2: _phrases("ABC-101CT", "ST-03 top")}
    context = SetContext("Vendor set.pdf", lambda n: pages[n], 2)
    context.build()
    assert context.pair_pages(1) == [(2, "ABC-101CT", "ABC-101.C")]


def test_a_one_digit_finish_code_only_one_sheet_prints_is_an_outlier() -> None:
    pages = {n: _phrases("ST-2 Quartz top", f"ABC-{100 + n}.C") for n in range(1, 5)}
    pages[2] = _phrases("ST-3 Quartz top", "ABC-102.C")
    context = _context(pages, 4)
    (outlier,) = context.outliers(2)
    assert outlier.tag == "ST-3" and outlier.others == {"ST-2": [1, 3, 4]}


# ---- a page's please-check the set's other sheets answer (2026-11-04) --------------------------


def _at(text: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {"text": text, "box": [x0, y0, x1, y1]}


def _item_cell(code: str) -> list[dict[str, Any]]:
    """A cabinet sheet's title block: the ITEM NO. anchor with its value under it."""
    return [_at("ITEM NO.", 600, 700, 650, 712), _at(code, 610, 730, 640, 740)]


def _stone_header(number: int, code: str) -> list[dict[str, Any]]:
    """A stone drawing's template: `DRAWING # n` with the item number on the line under it."""
    return [_at(f"DRAWING # {number}", 40, 20, 100, 30), _at(code, 40, 33, 72, 42)]


def _one_code_set() -> dict[int, list[dict[str, Any]]]:
    """Made-up: one vanity XY-310 (its cabinet sheets and its stone drawing all titled XY-310), a
    second vanity XY-320, and a one-off kitchenette top XY-905 on its own stone drawing."""
    return {
        1: [*_item_cell("XY-310"), _at("ST-01 BY GV", 380, 236, 430, 245), _at("WD-02", 50, 50, 80, 60),
            _at("QS-40-2", 380, 211, 418, 219), _at("Vanity by GV", 370, 220, 417, 229)],
        2: [*_item_cell("XY-310"), _at("WD-02", 50, 50, 80, 60)],
        3: [*_item_cell("XY-320"), _at("WD-02", 50, 50, 80, 60), _at("QS-40-2", 300, 200, 340, 210)],
        4: [*_item_cell("XY-320"), _at("WD-02", 50, 50, 80, 60)],
        5: [*_stone_header(1, "XY-905"), _at("HSK-UM-7", 300, 200, 340, 210)],
        6: [*_stone_header(2, "XY-310"), _at("QS-40-2", 288, 258, 337, 268), _at("WD-02", 50, 50, 80, 60)],
        7: [*_stone_header(3, "XY-320"), _at("QS-40-2", 288, 235, 337, 245)],
    }  # fmt: skip


def _built(pages: dict[int, list[dict[str, Any]]]) -> SetContext:
    context = _context(pages, max(pages))
    context.build()
    return context


def _reviewer_card(page: int, item: str, why: str, kind: str = "labels") -> dict[str, Any]:
    from app.review.assistant import likely_problems

    return {
        "id": f"F{page}",
        "page": page,
        "item": item,
        "why": why,
        "status": "check",
        likely_problems.FIELD: {"by": "page_reviewer", "proof": "likely_only", "kind": kind},
    }


def test_a_sheets_own_item_number_is_read_from_its_item_cell_or_its_drawing_header() -> None:
    context = _built(_one_code_set())
    assert context.own_items(1) == {"XY-310"}
    assert context.own_items(5) == {"XY-905"}
    assert context.own_items(6) == {"XY-310"}
    # A finish in a cell is never the sheet's item (numbered from 1).
    assert set_context.own_items(_item_cell("MT-002")) == frozenset()


def test_a_one_off_item_number_on_its_own_sheet_is_never_an_outlier() -> None:
    """Set A p16 of 2026-11-04: the kitchenette's own number, printed only on its own sheet, was
    "only this sheet prints it, a wrong code is likely"."""
    context = _built(_one_code_set())
    assert "XY" in context.finish_families()  # the vanity's number is on many sheets
    assert [o.tag for o in context.outliers(5)] == []
    assert not [f for f in set_context.check_page(context, 5) if "XY-905" in f["item"]]


def test_an_item_cell_claim_is_dropped_when_the_tops_drawing_has_the_same_number() -> None:
    context = _built(_one_code_set())
    card = _reviewer_card(
        1,
        "Title block item cell",
        "This sheet also draws the ST-01 top by GV, which has its own countertop drawing (p6). "
        "The item cell names only XY-310.",
    )
    why = set_context.item_cell_answered(context, card)
    assert why is not None and "p6" in why and "XY-310" in why
    # No page named: every other sheet of the item is titled XY-310 too.
    card["why"] = "The sheet also draws the stone top, which has its own drawing. Only XY-310."
    assert set_context.item_cell_answered(context, card) is not None


def test_an_item_cell_claim_stays_when_the_tops_drawing_has_its_own_number() -> None:
    """A cabinet sheet `XY-501.C` whose countertop drawing is `XY-501CT`: GV's reviewer writes the
    countertop number into the cell, so the claim is kept."""
    pages = {
        1: [*_item_cell("XY-501.C")],
        2: [*_stone_header(1, "XY-501CT")],
    }
    context = _built(pages)
    card = _reviewer_card(
        1,
        "Title block: item cell",
        "The sheet also draws the top, which has its own countertop drawing (p2), but the item "
        "cell names only XY-501.C.",
    )
    assert set_context.item_cell_answered(context, card) is None


def test_other_title_block_claims_are_never_answered_by_the_item_number() -> None:
    context = _built(_one_code_set())
    no_cells = _reviewer_card(
        6,
        "Vanity top XY-310: title block",
        "The title block has no item number, description or overall size. XY-310 appears only "
        "in the top-left note. The XY-310 sheets (p1/p2) give the description.",
    )
    assert set_context.item_cell_answered(context, no_cells) is None
    one_off = _reviewer_card(
        5,
        "Sheet header: item number XY-905",
        "XY-905 appears on this page only; p6 is XY-310. Please confirm the item number; if this "
        "top has its own countertop drawing number it should be added.",
    )
    assert set_context.item_cell_answered(context, one_off) is None


def test_a_supplier_claim_is_dropped_when_the_items_other_sheet_says_who_supplies_it() -> None:
    context = _built(_one_code_set())
    card = _reviewer_card(
        6,
        "Vanity top plan: sink QS-40-2 supply",
        "The QS-40-2 sink is drawn, but the sheet does not say who supplies the sink.",
        kind="scope",
    )
    why = set_context.supplier_answered(context, card)
    assert why is not None and "p1" in why and "QS-40-2 Vanity by GV" in why


def test_a_supplier_claim_stays_when_no_sheet_of_the_item_says_it() -> None:
    context = _built(_one_code_set())
    other_item = _reviewer_card(  # XY-320's sheets print the sink with no "by" note
        7, "Sink QS-40-2: who supplies it", "Nothing says who supplies the sink.", kind="scope"
    )
    assert set_context.supplier_answered(context, other_item) is None
    one_off = _reviewer_card(  # the only sheet of its item
        5,
        "Top plan: HSK-UM-7 sink supplier",
        "The sheet does not say who supplies it.",
        kind="scope",
    )
    assert set_context.supplier_answered(context, one_off) is None
    no_code_here = _reviewer_card(  # the page names no sink: nothing says what it is
        2, "Cut-out: sink QS-40-2 supplier", "The cut-out names no sink or supplier.", kind="scope"
    )
    assert set_context.supplier_answered(context, no_code_here) is None
    faucet = _reviewer_card(
        1,
        "Faucet: who supplies it",
        "A faucet is drawn; no note says who supplies it.",
        kind="scope",
    )
    assert set_context.supplier_answered(context, faucet) is None


def test_the_finish_hook_drops_the_answered_cards_with_their_reason(tmp_path: Path) -> None:
    import threading
    from types import SimpleNamespace

    from app.review.assistant import likely_problems
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.sandbox import ChatWorkspace
    from tests.review.assistant.test_page_reviewer import FILE, _pdf

    context = _built(_one_code_set())
    workspace = ChatWorkspace.open(tmp_path / "chat")
    (workspace.uploads / FILE).write_bytes(_pdf())
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    book = FindingBook(workspace=workspace, listing=listing, page_count=7)
    dropped: list[tuple[str, str]] = []
    engine = SimpleNamespace(
        book=book, finding_lock=threading.RLock(),
        _show_dropped=lambda fid, why: dropped.append((fid, why)),
    )  # fmt: skip

    def reviewer_item(page: int, item: str, why: str, kind: str) -> str:
        book.report(
            {
                "page": page, "item": item, "category": "other",
                "architect": "n/a", "vendor": "see the drawing", "status": "check",
                "check_reason": "pairing_not_certain", "verified": False, "source": "architect",
                "why": why,
                "vendor_box": {"page": page, "x0": 12, "y0": 20, "x1": 120, "y1": 28, "file": FILE},
            },
            "page_reviewer",
        )  # fmt: skip
        found = book.live()[-1]
        book.replace(
            {
                **found,
                likely_problems.FIELD: {
                    "by": "page_reviewer",
                    "proof": "likely_only",
                    "kind": kind,
                },
            }
        )
        return str(found["id"])

    cell = reviewer_item(
        1, "Title block item cell",
        "This sheet also draws the top, which has its own countertop drawing (p6). Only XY-310.",
        "labels",
    )  # fmt: skip
    sink = reviewer_item(
        6, "Sink QS-40-2 supply", "The sheet does not say who supplies the sink.", "scope"
    )
    faucet = reviewer_item(1, "Faucet: who supplies it", "No note says who supplies it.", "scope")
    assert set_context.drop_answered(engine, context) == 2
    live = {f["id"] for f in book.live()}
    assert cell not in live and sink not in live and faucet in live
    assert [fid for fid, _ in dropped] == [cell, sink]
    assert all(why.startswith("Answered by the set's other sheets: ") for _, why in dropped)
