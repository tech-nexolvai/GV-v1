"""What the other pages of a drawing set print, for the page reviewer (`set_context`)."""

from __future__ import annotations

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
