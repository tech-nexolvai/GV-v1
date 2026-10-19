"""File-aware ids through the simple full check (#1722, separate PDFs step 3). Made-up pack facts,
$0.

A separate architect file is numbered after the vendor's file (its page 1 is set page 3 after a
2-page vendor file), so its drawings and labels get ids of their own; every PDF read and every
recorded spot uses the file's own page; a combined set is unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant.simple_check import worklist as worklist_module
from app.review.assistant.simple_check.compare import _caption
from app.review.assistant.simple_check.run import own_pages
from app.review.assistant.simple_check.schema import Picture
from app.review.assistant.simple_check.set_pages import set_pages
from app.review.assistant.simple_check.verify import Item, Pack, _architect_label

VENDOR, ARCH = "vendor.pdf", "arch.pdf"
WHOLE = [0.0, 0.0, 1224.0, 792.0]


def _page(file: str, page: int, role: str | None) -> dict[str, Any]:
    halves = (
        [{"box": WHOLE, "role": role, "heading": None, "round": None, "from_upload": True}]
        if role
        else []
    )
    return {
        "file": file,
        "page": page,
        "role": role or "general",
        "halves": halves,
        "rounds": [],
        "section": None,
        "area": None,
        "area_from_page": None,
        "sheet_numbers": [],
        "headings": [],
    }


def _label(n: int, inches: str, side: str, x: float = 300.0) -> dict[str, Any]:
    return {
        "id": f"D{n}",
        "text": f'{inches}"',
        "inches": inches,
        "box": [x, 300.0, x + 20, 310.0],
        "side": side,
        "orientation": "h",
        "route": "text",
    }


def separate_set() -> tuple[dict[str, Any], dict[str, Any]]:
    """A 2-page plain vendor file and a separate architect file (a cover, then a drawing)."""
    page_map = {
        "version": 8,
        "files": [VENDOR, ARCH],
        "upload_roles": {VENDOR: "vendor", ARCH: "architect"},
        "pages": [
            _page(VENDOR, 1, "vendor"),
            _page(VENDOR, 2, "vendor"),
            _page(ARCH, 1, None),
            _page(ARCH, 2, "architect"),
        ],
    }
    dims = {
        "files": [VENDOR, ARCH],
        "pages": [
            {"file": VENDOR, "page": 1, "labels": [_label(1, "24", "vendor")]},
            {"file": VENDOR, "page": 2, "labels": [_label(1, "36", "vendor")]},
            {"file": ARCH, "page": 1, "labels": []},
            {
                "file": ARCH,
                "page": 2,
                "labels": [_label(1, "24", "architect"), _label(2, "30", "architect", 400.0)],
            },
        ],
    }
    return page_map, dims


def test_separate_files_are_numbered_after_the_vendors_file() -> None:
    page_map, _dims = separate_set()
    pages = set_pages(page_map, VENDOR)
    assert pages.files == ((VENDOR, 0, 2), (ARCH, 2, 2))
    assert pages.separate
    assert pages.to_set(ARCH, 2) == 4 and pages.to_set(VENDOR, 2) == 2
    assert pages.own_page(ARCH, 4) == 2 and pages.own_page(VENDOR, 2) == 2
    assert pages.file_of(4) == (ARCH, 2) and pages.file_of(1) == (VENDOR, 1)


def test_a_combined_set_keeps_its_own_page_numbers() -> None:
    page_map = {
        "files": ["set.pdf"],
        "pages": [_page("set.pdf", 1, "vendor"), _page("set.pdf", 7, "architect")],
    }
    pages = set_pages(page_map, "set.pdf")
    assert not pages.separate
    assert pages.to_set("set.pdf", 7) == 7 and pages.own_page("set.pdf", 7) == 7
    # A file uploaded too but read by GV's headings (not by its upload) is not numbered.
    page_map["files"].append("copy.pdf")
    page_map["pages"].append({**_page("copy.pdf", 1, "vendor"), "file": "copy.pdf"})
    assert not set_pages(page_map, "set.pdf").joined("copy.pdf")


def test_the_architect_files_drawings_and_labels_get_set_page_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_map, dims = separate_set()

    def pair_first(vendor: Any, candidates: Any) -> tuple[list[Any], str]:
        return list(candidates[:1]), "paired for the test"

    monkeypatch.setattr(worklist_module, "_pair_across", pair_first)
    work = worklist_module.worklist_from(page_map, dims=dims, main_file=VENDOR)
    assert [item.id for item in work.items] == ["p1.v0", "p2.v0"]
    architect = work.items[0].architect[0]
    assert (architect.ref, architect.file, architect.page, architect.file_page) == (
        "p4.a1",
        ARCH,
        4,
        2,
    )
    assert architect.own_page == 2
    assert [label.ref for label in architect.labels] == ["p4.D1", "p4.D2"]
    assert work.items[0].paired_by == "paired for the test"
    assert work.items[1].architect[0].ref == "p4.a1"  # several vendor drawings, one architect
    # Only the vendor's pages are in the page cover; the architect's cover is skipped with its
    # file and its own page.
    assert [cover.page for cover in work.pages] == [1, 2]
    assert any(s.file == ARCH and s.page == 1 for s in work.skipped)
    assert work.uncovered == ()


def test_without_pairing_a_separate_vendor_drawing_is_skipped_with_the_reason() -> None:
    page_map, dims = separate_set()
    work = worklist_module.worklist_from(page_map, dims=dims, main_file=VENDOR)
    if work.items:  # paired by code (#1723): the ids are the set's
        assert all(d.ref == "p4.a1" for item in work.items for d in item.architect)
        return
    reasons = {s.drawing: s.reason for s in work.skipped if s.drawing}
    assert "p1.v0" in reasons and "(code)" in reasons["p1.v0"]


def test_verify_reads_the_architect_file_at_its_own_page() -> None:
    page_map, dims = separate_set()
    pack = Pack.from_documents(dims=dims, page_map=page_map, main_file=VENDOR)
    label = pack.label("p4.D2", ARCH, 4)
    assert label is not None
    assert (label["file"], label["page"], label["file_page"], label["inches"]) == (ARCH, 4, 2, "30")
    assert pack.label("p1.D1", VENDOR, 1)["inches"] == "24"  # type: ignore[index]
    assert "file_page" not in pack.label("p1.D1", VENDOR, 1)  # type: ignore[operator]
    assert pack.own_page(ARCH, 4) == 2 and pack.own_page(VENDOR, 2) == 2
    assert [h.role for h in pack.halves(ARCH, 4)] == ["architect"]
    item = Item(
        "p1.v0",
        vendor=_region(VENDOR, 1),
        architect=(_region(ARCH, 4, file_page=2),),
    )
    found = _architect_label("p4.D1", item, pack)
    assert found is not None and found["file"] == ARCH and found["file_page"] == 2


def _region(file: str, page: int, file_page: int | None = None) -> Any:
    from app.review.assistant.simple_check.verify import Region

    return Region(file, page, tuple(WHOLE), None, None, file_page)  # type: ignore[arg-type]


def test_a_recorded_finding_names_the_architect_file_and_its_own_page() -> None:
    page_map, dims = separate_set()
    pack = Pack.from_documents(dims=dims, page_map=page_map, main_file=VENDOR)
    finding = {
        "page": 1,
        "vs_page": 4,
        "vendor_box": {"page": 1, "x0": 1, "y0": 1, "x1": 2, "y1": 2, "file": VENDOR},
        "arch_box": {"page": 4, "x0": 1, "y0": 1, "x1": 2, "y1": 2, "file": ARCH},
        "dims": {
            "vendor": {"id": "D1", "page": 1, "file": VENDOR},
            "architect": {"id": "D1", "page": 4, "file": ARCH},
        },
    }
    out = own_pages(finding, pack)
    assert out["arch_box"]["page"] == 2 and out["arch_box"]["file"] == ARCH
    assert out["vendor_box"]["page"] == 1
    assert out["dims"]["architect"]["page"] == 2 and out["dims"]["vendor"]["page"] == 1
    assert out["vs_page"] == 2
    assert finding["arch_box"]["page"] == 4  # the verified item itself is not changed
    # A combined set's finding is unchanged.
    combined = Pack.from_documents(dims={"files": ["set.pdf"], "pages": []}, page_map=None)
    assert own_pages(finding, combined) == finding


def test_a_separate_files_picture_says_which_file_and_page_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picture = Picture(
        number=2,
        drawing="p4.a1",
        role="architect",
        file=ARCH,
        page=4,
        file_page=2,
        box=(0.0, 0.0, 10.0, 10.0),
        width=100,
        height=100,
        dpi=100,
        tokens=16,
        part="whole",
        labels=("p4.D1",),
    )
    page_map, dims = separate_set()
    monkeypatch.setattr(
        worklist_module, "_pair_across", lambda vendor, found: (list(found[:1]), "test")
    )
    item = worklist_module.worklist_from(page_map, dims=dims, main_file=VENDOR).items[0]
    words = _caption(picture, item)
    assert "page 4 (the separate file arch.pdf, its page 2)" in words
    assert "D5 = p4.D5" in words
    assert picture.own_page == 2
