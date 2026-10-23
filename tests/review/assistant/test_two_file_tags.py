"""A tag "the architect's" in a set of two files is read on the architect's paired pages only.

The tag reader was told only the vendor's file, yet each tag it filed as the architect's went to
the ARCHITECT's file at the page number it was read on: the vendor's page number there, another
drawing. #1887 settled the same mix-up for dimension labels; the same rule now holds for tags:

- the tag reader of a two-file set is told each of its pages' paired architect pages (and waits
  for the page matcher's answers on them, as the helpers do), and names the file of each tag;
- code takes an architect tag only from the architect's file, on a page paired with the vendor
  tag's page (`CrossPairs.pairs`): one read on the vendor's file, or on an architect page not
  paired with it, is never the architect's, and a page with nothing paired compares no tag;
- the item's architect box is on the architect's file at that paired page, `vs_page` always named.

Made-up pages and tags only. No network, no paid call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from app.review.assistant import full_check_roster
from app.review.assistant.dim_findings import typed_architect_spot
from app.review.assistant.full_check_roster import compare_tag_rows, tag_pair_lines
from app.review.assistant.sandbox import UPLOADS

VENDOR = "vendor.pdf"
ARCHITECT = "architect.pdf"
FILES = (VENDOR, ARCHITECT)
LISTING = [
    {"name": VENDOR, "kind": "the vendor's shop drawings"},
    {"name": ARCHITECT, "kind": "the architect's drawings"},
]


def _row(
    page: int, side: str, tag: str, *, file: str | None = None, item: str = "Bar: top"
) -> dict[str, Any]:
    return {
        "page": page,
        "file": file,
        "side": side,
        "item": item,
        "tag": tag,
        "box": {"page": page, "x0": 10, "y0": 10, "x1": 40, "y1": 20},
    }


def test_an_architect_tag_read_on_the_vendors_file_is_never_pinned_to_the_architects() -> None:
    """The bug: the vendor's page 30 holds a tag the reader filed as the architect's. It was put on
    the architect's file at page 30, another drawing; now it is not the architect's at all."""
    rows = [_row(30, "architect", "QZ-7"), _row(30, "vendor", "QZ-8")]
    assert compare_tag_rows(rows, files=FILES, pairs={30: ((ARCHITECT, 12),)}) == []
    # GV's combined sheets: one file, the architect's drawing on the vendor's page, as before.
    combined = compare_tag_rows(rows)
    assert len(combined) == 1 and "vs_page" not in combined[0]


def test_an_architect_tag_counts_only_on_a_page_paired_with_the_vendor_page() -> None:
    vendor = _row(30, "vendor", "QZ-8")
    paired = _row(12, "architect", "QZ-7", file=ARCHITECT)
    found = compare_tag_rows([vendor, paired], files=FILES, pairs={30: ((ARCHITECT, 12),)})
    assert len(found) == 1
    item = found[0]
    assert (item["page"], item["vs_page"], item["status"]) == (30, 12, "different")
    assert item["arch_box"]["file"] == ARCHITECT and item["arch_box"]["page"] == 12
    assert "file" not in item["vendor_box"]  # the vendor's own file, by default
    # The same tag on the architect's page 30 (the vendor's page number), not paired with it.
    elsewhere = _row(30, "architect", "QZ-7", file=ARCHITECT)
    assert compare_tag_rows([vendor, elsewhere], files=FILES, pairs={30: ((ARCHITECT, 12),)}) == []
    # A vendor page with no architect page paired compares no tag.
    assert compare_tag_rows([vendor, paired], files=FILES, pairs={}) == []


def test_the_architect_page_is_always_named_even_when_its_number_is_the_vendors() -> None:
    """The architect's own page 30 really paired with the vendor's page 30: `vs_page` and the file
    are named, so the item never falls back on "the vendor's page number" (#1887's refusal)."""
    rows = [_row(30, "vendor", "QZ-7"), _row(30, "architect", "QZ-7", file=ARCHITECT)]
    (item,) = compare_tag_rows(rows, files=FILES, pairs={30: ((ARCHITECT, 30),)})
    assert item["vs_page"] == 30 and item["arch_box"]["file"] == ARCHITECT
    assert item["status"] == "same"
    kept = typed_architect_spot(item, None, vendor_file=VENDOR, architect_file=ARCHITECT)
    assert kept["arch_box"] == item["arch_box"]


def test_the_uploads_folder_and_the_work_lists_page_numbers_are_read() -> None:
    """The file as the brief names it (in the uploads folder) or alone; `p1012` is page 12."""
    rows = [
        _row(30, "vendor", "QZ-7"),
        _row(1012, "architect", "QZ-7", file=f"{UPLOADS}/{ARCHITECT}"),
    ]
    (item,) = compare_tag_rows(rows, files=FILES, pairs={30: ((ARCHITECT, 12),)})
    assert item["vs_page"] == 12 and item["arch_box"]["page"] == 12


def test_the_vendor_tag_on_a_paired_page_is_the_one_compared() -> None:
    """A piece the vendor tags on two pages: the one whose paired architect page tags it."""
    rows = [
        _row(31, "vendor", "QZ-8"),
        _row(30, "vendor", "QZ-8"),
        _row(12, "architect", "QZ-7", file=ARCHITECT),
        _row(14, "architect", "QZ-9", file=ARCHITECT),
    ]
    (item,) = compare_tag_rows(rows, files=FILES, pairs={30: ((ARCHITECT, 12),)})
    assert (item["page"], item["vs_page"], item["architect"]) == (30, 12, "QZ-7")
    assert item["vendor_box"]["page"] == 30


def test_a_vendor_tag_named_on_the_architects_file_is_left_out() -> None:
    rows = [
        _row(30, "vendor", "QZ-8", file=ARCHITECT),
        _row(12, "architect", "QZ-7", file=ARCHITECT),
    ]
    assert compare_tag_rows(rows, files=FILES, pairs={30: ((ARCHITECT, 12),)}) == []


def _run(pairs: dict[int, tuple[tuple[str, int], ...]] | None) -> tuple[Any, list[dict[str, Any]]]:
    reported: list[dict[str, Any]] = []

    def report(given: dict[str, Any], by: str) -> tuple[dict[str, Any], list[str]]:
        # The book's own rule for an architect box (#1887): refused on the vendor's page number.
        typed_architect_spot(given, None, vendor_file=VENDOR, architect_file=ARCHITECT)
        reported.append(given)
        return {**given, "id": f"F{len(reported)}"}, []

    engine = SimpleNamespace(
        book=SimpleNamespace(report=report, listing=LISTING, main_file=VENDOR),
        _show_finding=lambda _f: None,
        _cross=None if pairs is None else SimpleNamespace(pairs=pairs),
    )
    return full_check_roster._Run(engine=engine), reported  # type: ignore[arg-type]


def test_the_roster_compares_tags_with_the_final_pairs() -> None:
    table = json.dumps(
        {
            "tags": [
                _row(30, "vendor", "QZ-8"),
                _row(30, "architect", "QZ-8"),  # the vendor's file: never the architect's
                _row(12, "architect", "QZ-7", file=ARCHITECT),
            ]
        }
    )
    run, reported = _run({30: ((ARCHITECT, 12),)})
    run._apply_tags(table)
    assert [(f["page"], f["vs_page"], f["architect"]) for f in reported] == [(30, 12, "QZ-7")]
    assert reported[0]["arch_box"]["file"] == ARCHITECT
    # No pairs known (the page matcher gave none): no architect tag is taken.
    run, reported = _run(None)
    run._apply_tags(table)
    assert reported == [] and run.failed_steps == []


def test_the_tag_reader_is_told_each_pages_paired_architect_pages() -> None:
    line = tag_pair_lines(
        [30, 31],
        {30: ((ARCHITECT, 12), (ARCHITECT, 14))},
        vendor_file=VENDOR,
        architect_file=ARCHITECT,
        uploads="uploads",
    )
    assert f"uploads/{ARCHITECT}, a separate PDF" in line
    assert f"your p.30 with uploads/{ARCHITECT} p.12, uploads/{ARCHITECT} p.14" in line
    assert "your p.31 with none" in line
    assert f"A tag on uploads/{VENDOR} is the vendor's, never the architect's" in line


def test_a_two_file_tag_reader_waits_for_its_pages_pairs() -> None:
    waited: list[tuple[int, ...]] = []

    def wait_pages(pages: Any, _cancel: Any) -> Any:
        waited.append(tuple(pages))
        return SimpleNamespace(pairs={30: ((ARCHITECT, 12),)})

    matching = SimpleNamespace(to_answer=frozenset({30}), wait_pages=wait_pages)
    run, _reported = _run({})
    run.engine.cancel = None
    run.plan = SimpleNamespace(caps={"tags": 1_000_000})
    agent = run._tag_agent([30, 31], None, matching=matching)
    assert "your p.30 with none" in agent.prompt  # before the matcher's answers
    assert agent.ready is not None
    brief = agent.ready()
    assert waited == [(30, 31)] and f"your p.30 with {UPLOADS}/{ARCHITECT} p.12" in brief
    # GV's combined sheets: the brief as before, no wait.
    run.engine.book.listing = LISTING[:1]
    combined = run._tag_agent([30, 31], None, matching=matching)
    assert combined.ready is None and "THE ARCHITECT'S OWN FILE" not in combined.prompt
