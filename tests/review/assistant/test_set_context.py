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
