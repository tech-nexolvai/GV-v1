"""The vendor's room matrix against the architect's room schedule, by code (2026-11-04).

A real set's vendor matrix and the architect's room schedule listed the same rooms, but many rooms
carried another room type on the two lists, and the helper that paired them compared only sizes.
Code now reads both tables from their text and ruling lines and compares (a) the rooms, (b) the
accessible rooms and (c) each room's type. (a) and (b) are a Wrong only when both tables are read
whole (counts equal to the printed totals); (c) is always a please-check.

Made-up rooms and room types only: $0, no model call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import (
    chat_trust,
    check_record,
    likely_problems,
    proof,
    room_matrix,
)
from app.review.assistant.room_matrix import compare, read_table, type_key

Box = tuple[float, float, float, float]
VENDOR, ARCH = "shop.pdf", "arch.pdf"


def _grid(
    rows: list[tuple[str, list[str], int | None]],
    *,
    heading: str = "Room Type",
    groups: list[tuple[str, int]] | None = None,
    total: int | None = -1,
    codes: list[str] | None = None,
    stray: str | None = None,
) -> tuple[list[tuple[str, Box]], list[Any]]:
    """A made-up room table: one row per (name, rooms, printed total) under a room-type heading,
    its rooms in a column of their own (one cell each), a TOTAL column and a TOTAL row
    (`total`: -1 for the rooms' count, None for none). `groups`: an item column with (its words,
    how many rows it spans). `codes`: a code column left of the names (an architect's "QQ")."""
    phrases: list[tuple[str, Box]] = []
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    item_x, code_x, name_x, rooms_x, total_x, right = 40.0, 120.0, 200.0, 400.0, 700.0, 780.0
    left = item_x if groups else (code_x if codes else name_x)
    top = 100.0
    phrases.append((heading, (name_x + 20, 85, name_x + 120, 95)))
    phrases.append(("Total", (total_x + 20, 85, total_x + 50, 95)))
    if groups:
        phrases.append(("Item Code", (item_x + 5, 85, item_x + 60, 95)))
    if codes:
        phrases.append(("ROOM TYPE", (code_x + 5, 85, code_x + 70, 95)))
        heading_box = phrases[0][1]
        phrases[0] = ("ROOM DESCRIPTION", heading_box)
    y = top
    edges = [80.0, top]
    for n, (name, rooms, printed) in enumerate(rows):
        height = 12.0 * max(1, len(rooms))
        phrases.append((name, (name_x + 5, y + 2, name_x + 150, y + 10)))
        if codes:
            phrases.append((codes[n], (code_x + 5, y + 2, code_x + 40, y + 10)))
        for k, room in enumerate(rooms):
            phrases.append((room, (rooms_x + 10, y + 12 * k + 2, rooms_x + 30, y + 12 * k + 10)))
        if printed is not None:
            phrases.append((str(printed), (total_x + 20, y + 2, total_x + 30, y + 10)))
        y += height
        edges.append(y)
    if total is not None:
        count = sum(len(r) for _n, r, _t in rows) if total == -1 else total
        phrases.append(("Total", (name_x + 100, y + 2, name_x + 130, y + 10)))
        phrases.append((str(count), (total_x + 20, y + 2, total_x + 35, y + 10)))
        y += 14
        edges.append(y)
    for edge in edges:
        segments.append(((left, edge), (right, edge)))
    if groups:
        # The item column's own lines: only between groups.
        segments = [s for s in segments if s[0][1] in (80.0, top, edges[-1])]
        segments += [((name_x, e), (right, e)) for e in edges]
        start = 1
        for words, span in groups:
            y0, y1 = edges[start], edges[start + span]
            phrases.append((words, (item_x + 5, y0 + 2, item_x + 70, y0 + 10)))
            segments.append(((item_x, y1), (right, y1)))
            start += span
    for x in (left, item_x + 80 if groups else None, code_x if codes else None):
        if x is not None:
            segments.append(((x, 80.0), (x, y)))
    for x in (name_x, rooms_x, total_x, right):
        segments.append(((x, 80.0), (x, y)))
    if stray:
        phrases.append((stray, (rooms_x + 10, y + 30, rooms_x + 30, y + 38)))
    return phrases, segments


def _table(
    rows: list[tuple[str, list[str], int | None]], *, file: str = VENDOR, page: int = 6, **grid: Any
) -> Any:
    return read_table(*_grid(rows, **grid), file, page)


VENDOR_ROWS = [
    ("Double Queen", ["101", "102", "103"], 3),
    ("King", ["104", "105", "106", "107"], 4),
    ("King Studio", ["201", "202"], 2),
    ("ADA King", ["108"], 1),
]
ARCH_ROWS = [
    ("DOUBLE QUEEN", ["101", "102", "103"], 3),
    ("STANDARD KING", ["104", "105", "106", "107"], 4),
    ("KING STUDIO", ["201", "202"], 2),
    ("ADA KING", ["108"], 1),
]


def _pair(vendor_rows: Any = None, arch_rows: Any = None, **arch: Any) -> list[dict[str, Any]]:
    vendor = _table(vendor_rows or VENDOR_ROWS)
    architect = _table(
        arch_rows or ARCH_ROWS,
        file=ARCH,
        page=3,
        codes=arch.pop("codes", ["QQ", "K", "KS", "ADA K"]),
        **arch,
    )
    assert vendor is not None and architect is not None
    return compare(vendor, architect)


# ---- reading a table ----------------------------------------------------------------------------


def test_a_table_is_read_row_by_row_with_its_totals() -> None:
    table = _table(VENDOR_ROWS)
    assert table is not None and table.whole and table.total == 10
    assert [(r.name, list(r.rooms), r.total) for r in table.rows] == [
        (n, rooms, t) for n, rooms, t in VENDOR_ROWS
    ]
    assert [r.accessible for r in table.rows] == [False, False, False, True]


def test_the_description_column_is_read_not_the_code_column() -> None:
    table = _table(ARCH_ROWS, codes=["QQ", "K", "KS", "ADA K"], file=ARCH, page=3)
    assert table is not None
    assert [r.name for r in table.rows] == [n for n, _r, _t in ARCH_ROWS]


def test_an_item_group_marks_its_rows_accessible() -> None:
    """The vendor's accessible vanity spans two rows whose own names say nothing of ADA."""
    rows = [*VENDOR_ROWS[:3], ("King", ["108"], 1), ("Double Queen", ["203"], 1)]
    table = _table(rows, groups=[("XX-301 Standard Vanity", 3), ("XX-302 ADA Vanity", 2)])
    assert table is not None and table.whole
    assert [r.accessible for r in table.rows] == [False, False, False, True, True]


def test_a_table_not_read_whole_says_why() -> None:
    wrong = [*VENDOR_ROWS[:3], ("ADA King", ["108"], 2)]
    table = _table(wrong)
    assert table is not None and not table.whole
    assert any("total is 2" in d for d in table.doubts)
    table = _table(VENDOR_ROWS, total=12)
    assert table is not None and any("its total is 12" in d for d in table.doubts)
    table = _table(VENDOR_ROWS, total=None)
    assert table is not None and "the table prints no total" in table.doubts
    table = _table(VENDOR_ROWS, stray="109")
    assert table is not None and any("outside the table's rows" in d for d in table.doubts)


def test_no_lines_or_too_few_rooms_is_no_table() -> None:
    assert (
        read_table([("Room Type", (200, 85, 300, 95)), ("101", (410, 102, 430, 110))], [], "a", 1)
        is None
    )
    assert _table([("King", ["101", "102"], 2)]) is None  # fewer than ROOM_TABLE_MIN rooms


def test_room_type_names_are_normalised() -> None:
    assert type_key("STANDARD KING") == type_key("King") == type_key("Kings")
    assert type_key("Dbl Queen") == type_key("DOUBLE QUEEN") == type_key("Queen Double")
    assert type_key("King Conn.") == type_key("KING CONNECTING")
    assert type_key("Accessible King") == type_key("ADA KING") == type_key("HC King")
    assert type_key("King Suite") != type_key("King Studio")
    assert type_key("Type 1") != type_key("Type 2")


# ---- comparing ----------------------------------------------------------------------------------


def test_the_same_lists_give_no_finding() -> None:
    assert _pair() == []


def test_a_room_type_difference_is_one_please_check_per_pair_of_names() -> None:
    arch = [
        ("DOUBLE QUEEN", ["101", "102", "103", "105", "106"], 5),
        ("STANDARD KING", ["104", "107"], 2),
        ("KING SUITE", ["201", "202"], 2),
        ("ADA KING", ["108"], 1),
    ]
    found = _pair(arch_rows=arch)
    assert len(found) == 2
    first, second = found
    assert first["status"] == "check" and first["created_by"] == room_matrix.CREATED_BY
    assert (first["vendor"], first["architect"]) == ("King", "DOUBLE QUEEN")
    assert first["item"].startswith("Rooms 105, 106: room type")
    assert 'lists rooms 105, 106 as "King"' in first["why"]
    assert first[likely_problems.FIELD]["proof"] == "likely_only"
    assert first["vendor_box"]["file"] == VENDOR and first["arch_box"]["file"] == ARCH
    assert first["vendor_box"]["page"] == 6 and first["arch_box"]["page"] == 3
    assert [a["vendor"] for a in first["also_on"]] == ["106"]
    assert (second["vendor"], second["architect"]) == ("King Studio", "KING SUITE")
    assert "text_proof" not in first and "text_proof" not in second


def test_a_room_missing_from_one_list_is_a_wrong_only_when_both_are_read_whole() -> None:
    arch = [*ARCH_ROWS[:3], ("ADA KING", ["108", "109"], 2)]
    found = _pair(arch_rows=arch)
    assert len(found) == 1
    wrong = found[0]
    assert wrong["status"] == "different" and wrong["text_proof"]["kind"] == room_matrix.KIND
    assert wrong["text_proof"]["rooms"] == ["109"]
    assert wrong["item"] == ("Room 109: on the architect's schedule, not on the vendor's matrix")
    assert likely_problems.FIELD not in wrong
    # The vendor's table prints a total it does not add up to: a please-check, never Wrong.
    vendor = _table(VENDOR_ROWS, total=11)
    architect = _table(arch, file=ARCH, page=3, codes=["QQ", "K", "KS", "ADA K"])
    assert vendor is not None and architect is not None
    (held,) = compare(vendor, architect)
    assert held["status"] == "check" and "text_proof" not in held
    assert "Code could not prove it" in held["why"]


def test_accessible_rooms_on_one_list_only_are_compared() -> None:
    arch = [
        ("DOUBLE QUEEN", ["101", "102", "103"], 3),
        ("STANDARD KING", ["104", "105", "106", "107", "108"], 5),
        ("KING STUDIO", ["201"], 1),
        ("ADA KING", ["202"], 1),
    ]
    found = _pair(arch_rows=arch)
    kinds = sorted((f["item"], f["status"]) for f in found)
    assert kinds == [
        ("Room 108: accessible (ADA) on one list only", "different"),
        ("Room 202: accessible (ADA) on one list only", "different"),
    ]
    assert all(f["text_proof"]["kind"] == room_matrix.KIND for f in found)
    words = " ".join(f["why"] for f in found)
    assert "gives its accessible item to room 108" in words
    assert "does not give its accessible item to room 202" in words


# ---- through the finding book -------------------------------------------------------------------


def _vendor_pdf(path: Path, pages: int) -> None:
    from reportlab.pdfgen import canvas

    out = canvas.Canvas(str(path), pagesize=(842, 595))
    for n in range(pages):
        out.drawString(100, 100, f"sheet {n + 1}")
        out.showPage()
    out.save()


def _book(tmp_path: Path) -> Any:
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.chat_trust import apply_checks
    from app.review.assistant.sandbox import ChatWorkspace

    workspace = ChatWorkspace.open(tmp_path / "chat")
    _vendor_pdf(workspace.uploads / VENDOR, 6)
    _vendor_pdf(workspace.uploads / ARCH, 3)
    listing = [
        {"name": VENDOR, "pages": 6, "kind": "the vendor's shop drawings"},
        {"name": ARCH, "pages": 3, "kind": "the architect's drawings"},
    ]
    book = FindingBook(workspace=workspace, listing=listing, page_count=6)
    book.checker = lambda f: apply_checks(f, book.pdf_paths(), None)
    return book


def test_a_proved_missing_room_goes_through_the_book_as_a_text_witnessed_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    arch = [*ARCH_ROWS[:3], ("ADA KING", ["108", "109"], 2)]
    (finding,) = _pair(arch_rows=arch)
    assert chat_trust._text_made(finding) == "agrees"
    assert check_record.kind_of(finding) == "room_list"
    finding.pop("also_on", None)
    book = _book(tmp_path)
    assert book.add_code_finding(finding, created_by=room_matrix.CREATED_BY) is not None
    stored = book.live()[0]
    assert stored["witnesses"] == ["text"]
    assert proof.code_proved(stored) and proof.vendor_facing(stored)
    assert "both room tables whole" in proof.proof_line(stored)
    assert proof.proof_group(stored) == "clouded"  # the record switched off (the tests' default)
    monkeypatch.setenv(check_record.SWITCH_ENV, "on")
    assert proof.proof_group(stored) == "likely_problem"  # its kind has no keyed cases yet


def test_the_engine_hook_adds_each_finding_once_with_its_mark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import threading
    from types import SimpleNamespace

    arch = [
        ("DOUBLE QUEEN", ["101", "102", "103", "105", "106"], 5),
        ("STANDARD KING", ["104", "107"], 2),
        ("KING STUDIO", ["201", "202"], 2),
        ("ADA KING", ["108"], 1),
    ]
    made = _pair(arch_rows=arch)
    monkeypatch.setattr(room_matrix, "check_pack", lambda *_a, **_k: [dict(f) for f in made])
    book = _book(tmp_path)
    shown: list[dict[str, Any]] = []
    engine = SimpleNamespace(
        settings=SimpleNamespace(mode="full_check"),
        tools=SimpleNamespace(workspace=SimpleNamespace(set_pack=tmp_path, uploads=tmp_path)),
        book=book,
        finding_lock=threading.Lock(),
        _show_finding=shown.append,
    )
    assert room_matrix.add_to_answer(engine) == 1
    assert room_matrix.add_to_answer(engine) == 0  # already in the answer
    (stored,) = book.live()
    assert stored["created_by"] == room_matrix.CREATED_BY and stored["status"] == "check"
    assert likely_problems.likely_only(stored) and proof.vendor_facing(stored)
    assert proof.proof_group(stored) == "likely_problem"
    assert likely_problems.please_check(stored).startswith("Please check and confirm: rooms 105")
    assert [a["vendor"] for a in stored["also_on"]] == ["106"]
    assert all(a["finding_id"] == stored["id"] for a in stored["also_on"])
    assert shown and shown[0]["id"] == stored["id"]


def test_the_set_packs_two_tables_are_found_and_compared(tmp_path: Path) -> None:
    """From the set pack alone: the page map's upload roles, each page's printed phrases
    (`dims.json`) and its line work (kept in the pack): the vendor's matrix against the
    architect's schedule, the one that overlaps it."""
    import json

    import numpy as np

    from app.review.assistant import set_pack

    arch_rows = [
        ("DOUBLE QUEEN", ["101", "102", "103", "105", "106"], 5),
        ("STANDARD KING", ["104", "107"], 2),
        ("KING STUDIO", ["201", "202"], 2),
        ("ADA KING", ["108"], 1),
    ]
    pages = {
        (VENDOR, 6): _grid(VENDOR_ROWS),
        (ARCH, 3): _grid(arch_rows, codes=["QQ", "K", "KS", "ADA K"]),
        (ARCH, 4): _grid([("SUITE", ["301", "302", "303", "304", "305", "306", "307", "308"], 8)]),
    }
    (tmp_path / "line_work").mkdir()
    dims = {"pages": []}
    for (file, page), (phrases, segments) in pages.items():
        dims["pages"].append(
            {
                "file": file,
                "page": page,
                "labels": [],
                "phrases": [{"text": t, "box": list(b)} for t, b in phrases],
            }
        )
        rows = np.array([[a[0], a[1], b[0], b[1]] for a, b in segments], dtype=np.float64)
        np.savez(
            tmp_path / "line_work" / f"{set_pack.stem(file)}-p{page:04d}.npz",
            segments=rows,
            weight=np.float64(0.3),
        )
    (tmp_path / "dims.json").write_text(json.dumps(dims), encoding="utf-8")
    roles = {VENDOR: "vendor", ARCH: "architect"}
    (tmp_path / "page_map.json").write_text(
        json.dumps({"files": [VENDOR, ARCH], "upload_roles": roles, "pages": []}),
        encoding="utf-8",
    )
    found = room_matrix.check_pack(tmp_path, tmp_path / "uploads", VENDOR)
    assert [(f["vendor"], f["architect"], f["arch_box"]["page"]) for f in found] == [
        ("King", "DOUBLE QUEEN", 3)
    ]
    assert room_matrix.check_pack(tmp_path, tmp_path / "uploads", "another.pdf") == []
