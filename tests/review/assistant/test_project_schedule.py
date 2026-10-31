"""GV's project schedule (`project_schedule`) on made-up spreadsheets and PDF tables: reading each
format, the should-be facts per page, where a page and the schedule differ (facts and code's
questions), the copy beside the drawings, the upload's media types, and the red path (off).
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.models.document import DocumentKind
from app.review.assistant import chat_trust, check_record, project_schedule, proof
from app.review.assistant.project_schedule import (
    ProjectSchedule,
    canon,
    check,
    facts,
    is_schedule,
    party,
    question_list,
    read_bytes,
    read_tables,
    red_findings,
    rows_of,
)
from app.review.assistant.set_context import SetContext
from app.schemas.packages import XLSX_MEDIA_TYPE, DocumentRegistration

SHA = "a" * 64


def at(text: str, x0: float, y0: float, x1: float | None = None, h: float = 8.0) -> dict[str, Any]:
    return {"text": text, "box": [x0, y0, x1 if x1 is not None else x0 + 6 * len(text), y0 + h]}


# ---- made-up schedules -----------------------------------------------------------------------------


def finish_csv() -> bytes:
    """A finish schedule as a CSV: a title row, a blank, the header, rows, a note, one more row."""
    return (
        b"PROJECT FINISH SCHEDULE,,\n,,\n"
        b"TAG,MATERIAL,MANUFACTURER / COLOR\n"
        b"ST-02,Quartz,Acme QZ-19 Snow White\n"
        b"ST-3,Quartz,Acme QZ-16 Nova Grey\n"
        b",,\nNote: see spec section 12,,\n"
        b"WD-04,Laminate,Acme AB426 Slate Gray\n"
    )


def item_workbook() -> bytes:
    """An item list and a finish legend as two sheets of one workbook."""
    openpyxl: Any = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    items = book.active
    assert items is not None
    items.title = "Items"
    items.append(["ITEM LIST"])
    items.append([])
    items.append(["ITEM NO.", "DESCRIPTION", "COUNTERTOP NO.", "ROOM", "SUPPLIED BY", "FINISH"])
    items.append(["ABC-102", "Vanity base cabinet", "ABC-102.CT", "Lobby", "By GV", "ST-02"])
    items.append(["XYZ-104", "Reception top", "N/A", "Lobby", "GV", ""])
    items.append(["ABC-105", "Bin cabinet", None, "Office", "By others", ""])
    items.append([101, "Loose shelf", None, "Office", "", ""])
    legend = book.create_sheet("Finishes")
    legend.append(["FINISH CODE", "DESCRIPTION"])
    legend.append(["MT-02", "Metalco #512 Dark Bronze"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def ruled_pdf(rows: list[list[str]]) -> bytes:
    """A schedule drawn as a ruled table in a PDF (a cell per column, a line round every cell)."""
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=(612, 792))
    xs, top = [50.0, 150.0, 400.0, 540.0], 700.0
    for i, row in enumerate(rows):
        for j, text in enumerate(row):
            page.drawString(xs[j] + 4, top - 20 * i - 14, text)
    for i in range(len(rows) + 1):
        page.line(xs[0], top - 20 * i, xs[-1], top - 20 * i)
    for x in xs:
        page.line(x, top, x, top - 20 * len(rows))
    page.save()
    return out.getvalue()


# ---- reading ---------------------------------------------------------------------------------------


def test_a_csv_finish_schedule_gives_each_tag_its_description() -> None:
    grids = read_tables(finish_csv(), "finishes.csv")
    finishes, items = rows_of(grids[0])
    assert items == []
    assert [(e.tag, e.description) for e in finishes] == [
        ("ST-02", "Quartz; Acme QZ-19 Snow White"),
        ("ST-3", "Quartz; Acme QZ-16 Nova Grey"),
        ("WD-04", "Laminate; Acme AB426 Slate Gray"),
    ]
    # The title row, the blank and the note are no rows; where each row is, for GV to find it.
    assert finishes[0].where.words() == "finishes.csv, row 4"


def test_a_workbook_item_list_reads_items_countertops_rooms_and_who_supplies_them() -> None:
    schedule = read_bytes([("schedule.xlsx", item_workbook())])
    assert schedule.unread == []
    top = schedule.items[canon("ABC-102")]
    assert (top.description, top.countertop, top.room, top.party) == (
        "Vanity base cabinet",
        "ABC-102.CT",
        "Lobby",
        "GV",
    )
    assert top.finishes == ("ST-02",)
    assert top.where.words() == "schedule.xlsx, sheet Items, row 4"
    assert schedule.items[canon("XYZ-104")].countertop is None  # "N/A" is no number
    assert schedule.items[canon("ABC-105")].party == "others"
    assert schedule.items["101"].description == "Loose shelf"  # a number cell, not "101.0"
    # The second sheet is a finish legend.
    assert schedule.finish("MT-02") is not None
    assert schedule.finish("MT-2") is schedule.finish("MT-02")


def test_a_ruled_pdf_table_is_read_and_a_file_is_told_by_its_bytes_not_its_name() -> None:
    pdf = ruled_pdf(
        [
            ["CODE", "DESCRIPTION", "SCOPE"],
            ["ST-02", "Quartz QZ-19 Snow White", "By GV"],
            ["MT-02", "Metalco #512 Dark Bronze", "By others"],
        ]
    )
    grids = read_tables(pdf, "legend.pdf")
    finishes, _items = rows_of(grids[0])
    assert [(e.tag, e.description, e.scope) for e in finishes] == [
        ("ST-02", "Quartz QZ-19 Snow White", "By GV"),
        ("MT-02", "Metalco #512 Dark Bronze", "By others"),
    ]
    assert finishes[0].where.place == "page 1"
    # The chat's copy of a workbook may be named .pdf: its bytes say what it is.
    assert read_bytes([("schedule.pdf", item_workbook())]).items


def test_an_unreadable_file_is_listed_and_the_others_still_count() -> None:
    schedule = read_bytes([("broken.pdf", b"%PDF-1.4 nothing"), ("f.csv", finish_csv())])
    assert schedule.unread == ["broken.pdf"]
    assert schedule.finish("ST-02") is not None


def test_a_tag_described_two_ways_in_the_schedule_is_told_never_decided() -> None:
    twice = finish_csv() + b"ST-02,Quartz,Acme QZ-16 Nova Grey\n"
    schedule = read_bytes([("f.csv", twice)])
    assert schedule.finish("ST-02").description == "Quartz; Acme QZ-19 Snow White"  # type: ignore[union-attr]
    assert len(schedule.conflicts) == 1 and "ST-02" in schedule.conflicts[0]


def test_spelling_and_party() -> None:
    assert canon("ST-02") == canon("ST-2") == canon("st02") == "ST-2"
    assert canon("ABC-102.CT") == "ABC-102.CT"
    assert party("By GV") == "GV"
    assert party("by others") == party("GC") == party("N.I.C.") == "others"
    assert party("GV / others") is None
    assert party("Quartz QZ-19") is None


# ---- one page against the schedule -----------------------------------------------------------------


def _schedule() -> ProjectSchedule:
    return read_bytes([("finishes.csv", finish_csv()), ("items.xlsx", item_workbook())])


def _pages(description: str = "VANITY BASE CABINET") -> dict[int, list[dict[str, Any]]]:
    """Two vendor sheets: p1 the cabinet sheet of ABC-102 (a material table whose stone row names
    another product, a stone callout the schedule does not list, the room's name), p2 ABC-105's
    sheet (by others in the schedule) with no scope note."""
    one = [
        at("ST-02", 900, 50, 925),
        at("QZ-16 Color: Harbor Mist, Quartz by GV", 940, 50, 1100),
        at("WD-04", 900, 70, 925),
        at("Acme AB426 Slate Gray", 940, 70, 1100),
        at("ST-02 QUARTZ TOP", 300, 300),
        at("ST-05 SPLASH", 300, 330),
        at("LOBBY", 200, 600),
        at("ITEM NO.:", 900, 760, 940),
        at("ABC-102.C", 950, 760, 1000),
        at("DESCRIPTION:", 900, 775, 960),
        at(description, 970, 775, 1100),
    ]
    two = [
        at("ITEM NO.:", 900, 760, 940),
        at("ABC-105", 950, 760, 1000),
        at("OFFICE", 200, 600),
        at("WD-04", 300, 300),
    ]
    return {1: one, 2: two}


def _context(pages: dict[int, list[dict[str, Any]]]) -> SetContext:
    context = SetContext("Vendor set.pdf", lambda page: pages[page], len(pages))
    context.project = _schedule()
    context.build()
    return context


def _check(context: SetContext, page: int) -> tuple[list[str], list[project_schedule.Mismatch]]:
    assert context.project is not None
    return check(context.project, context, page)


def test_the_page_gets_the_schedule_for_its_tags_and_its_item() -> None:
    context = _context(_pages())
    lines = facts(context.project, context, 1)
    text = "\n".join(lines)
    assert lines[0].startswith("GV's project schedule (finishes.csv, items.xlsx;")
    assert "  ST-02: 'Quartz; Acme QZ-19 Snow White' (finishes.csv, row 4)" in lines
    assert "  WD-04: 'Laminate; Acme AB426 Slate Gray' (finishes.csv, row 8)" in lines
    assert (
        "  Item ABC-102 (printed here as ABC-102.C): description 'Vanity base cabinet'; "
        "countertop item ABC-102.CT; room 'Lobby'; scope 'By GV'; finishes ST-02 "
        "(items.xlsx, sheet Items, row 4)" in lines
    )
    # The set-level list: the items no sheet prints (never one by others, never a bare number).
    assert "Schedule items no sheet of the set prints: ABC-102 'Vanity base cabinet'" not in text
    assert "XYZ-104 'Reception top' ('Lobby')" in text
    assert "ABC-105" not in text.split("Schedule items no sheet")[1]
    # And through the set's facts, beside the other set facts.
    assert "GV's project schedule" in context.facts_for(1)


def test_where_the_page_and_the_schedule_differ() -> None:
    context = _context(_pages())
    _lines, found = _check(context, 1)
    by = {m.what: m for m in found}
    assert set(by) == {"unscheduled_tag", "table_row", "countertop_unprinted", "unprinted_item"}
    row = by["table_row"]
    assert (row.kind, row.tag, row.vendor) == (
        "D",
        "ST-02",
        "QZ-16 Color: Harbor Mist, Quartz by GV",
    )
    assert row.should_be == "Quartz; Acme QZ-19 Snow White"
    assert row.boxes[0] == (900.0, 50.0, 925.0, 58.0)  # the table's tag cell, then its words
    assert row.proof is not None and row.proof["schedule"]["codes"] == ["QZ19"]
    assert by["unscheduled_tag"].tag == "ST-05" and by["unscheduled_tag"].kind == "D"
    assert "ST-02, ST-3" in by["unscheduled_tag"].text
    assert by["countertop_unprinted"].should_be == "ABC-102.CT"
    assert by["countertop_unprinted"].boxes == ((950.0, 760.0, 1000.0, 768.0),)  # the item cell
    assert by["unprinted_item"].tag == "XYZ-104" and by["unprinted_item"].kind == "F"
    assert by["unprinted_item"].boxes == ((200.0, 600.0, 230.0, 608.0),)  # where LOBBY is
    # A title-block description unlike the schedule's is a label question.
    context = _context(_pages("WALL SHELF UNIT"))
    _lines, found = _check(context, 1)
    title = next(m for m in found if m.what == "title_description")
    assert (title.kind, title.vendor, title.should_be) == (
        "F",
        "WALL SHELF UNIT",
        "Vanity base cabinet",
    )


def test_an_item_the_schedule_gives_to_others_is_a_scope_question() -> None:
    context = _context(_pages())
    _lines, found = _check(context, 2)
    scope = next(m for m in found if m.what == "scope_others")
    assert (scope.kind, scope.tag) == ("E", "ABC-105")
    assert scope.boxes == ((950.0, 760.0, 1000.0, 768.0),)
    # A page that already notes "by others" is not asked.
    pages = _pages()
    pages[2].append(at("BIN BY OTHERS", 300, 400))
    context = _context(pages)
    _lines, found = _check(context, 2)
    assert all(m.what != "scope_others" for m in found)


def test_a_top_number_printed_otherwise_than_the_schedule_numbers_it() -> None:
    pages = _pages()
    pages[1].append(at("ABC-102T TOP", 400, 400))
    context = _context(pages)
    pages_with_top = {**pages, 2: pages[2] + [at("ABC-102.CT", 100, 100)]}
    context = _context(pages_with_top)
    _lines, found = _check(context, 1)
    differs = next(m for m in found if m.what == "countertop_differs")
    assert (differs.tag, differs.should_be) == ("ABC-102T", "ABC-102.CT")
    assert all(m.what != "countertop_unprinted" for m in found)


def test_a_page_the_schedule_does_not_touch_gets_only_the_unprinted_items() -> None:
    pages = {1: [at("NOTHING HERE", 10, 10)], 2: [at("XYZ-104", 10, 10)]}
    context = _context(pages)
    lines = facts(context.project, context, 1)
    assert len(lines) == 2
    assert (
        lines[1]
        == "Schedule items no sheet of the set prints: ABC-102 'Vanity base cabinet' ('Lobby')."
    )
    pages[2].append(at("ABC-102.CT", 10, 30))  # its top's number: the item is printed
    context = _context(pages)
    assert facts(context.project, context, 1) == []
    assert facts(None, context, 1) == []


def test_the_differences_are_code_questions_numbered_on_from_the_page_s_own() -> None:
    context = _context(_pages())
    made = question_list(context.project, context, 1, start=4, room=25)
    assert [q.label for q in made] == ["Q4", "Q5", "Q6", "Q7"]
    assert [q.kind for q in made] == ["D", "D", "F", "F"]
    assert all(q.made_by == "schedule" and q.boxes for q in made)
    assert len(question_list(context.project, context, 1, start=24, room=2)) == 2
    assert question_list(context.project, context, 1, start=1, room=0) == []


def test_the_view_questions_hook_adds_the_schedule_s_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.review.assistant import view_questions

    context = _context(_pages())
    review = SimpleNamespace(page=1, found=_pages()[1], size=(1191.0, 842.0), questions=[])

    def segments(*_args: Any) -> Any:
        raise RuntimeError("no line work in a made-up page")

    monkeypatch.setattr(view_questions, "_segments", segments)
    run = SimpleNamespace(
        review=review, engine=SimpleNamespace(cancel=None), _context=lambda: context
    )
    blocks = view_questions.opening_blocks(run)
    assert blocks and "the project schedule" in blocks[0]["text"]
    assert any(q.made_by == "schedule" for q in review.questions)


# ---- the red path: off ---------------------------------------------------------------------------


def test_no_red_while_the_switch_is_off() -> None:
    assert project_schedule.SHOULD_BE_RED is False
    context = _context(_pages())
    assert red_findings(context.project, context, 1) == []

    class Engine:
        def __init__(self) -> None:
            raise AssertionError("never touched while the switch is off")

    assert project_schedule.add_reds(Engine, context, [1, 2]) == 0


def _vendor_pdf(path: Path, pages: int) -> None:
    out = canvas.Canvas(str(path), pagesize=(1191, 842))
    for n in range(pages):
        out.drawString(100, 100, f"sheet {n + 1}")
        out.showPage()
    out.save()


def test_with_the_switch_on_a_code_proved_row_goes_through_the_book_as_a_text_witnessed_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.review.assistant.chat_findings import FindingBook
    from app.review.assistant.chat_trust import apply_checks
    from app.review.assistant.sandbox import ChatWorkspace

    monkeypatch.setattr(project_schedule, "SHOULD_BE_RED", True)
    context = _context(_pages())
    made = red_findings(context.project, context, 1)
    assert len(made) == 1  # the stone row only: a missing tag or a label is never red
    finding = made[0]
    assert finding["status"] == "different" and finding["source"] == "project_schedule"
    assert finding["created_by"] == "project_schedule" and finding["paired_by"] == "code"
    assert (finding["vendor"], finding["architect"]) == (
        "QZ-16 Color: Harbor Mist, Quartz by GV",
        "Quartz; Acme QZ-19 Snow White",
    )
    assert finding["text_proof"]["vendor"]["codes"] == ["QZ16"]
    assert chat_trust._text_made(finding) == "agrees"
    assert check_record.kind_of(finding) == "schedule"
    # The path it takes: the finding book and its checks (the trust rule's text witness), GV's
    # schedule governs, its own kind of check, and a red only once that kind's record is clean.
    workspace = ChatWorkspace.open(tmp_path / "chat")
    _vendor_pdf(workspace.uploads / "Vendor set.pdf", 2)
    listing = [{"name": "Vendor set.pdf", "pages": 2, "kind": "the vendor's shop drawings"}]
    book = FindingBook(workspace=workspace, listing=listing, page_count=2)
    book.checker = lambda f: apply_checks(f, book.pdf_paths(), None)
    assert book.add_code_finding(finding, created_by=project_schedule.CREATED_BY) is not None
    stored = book.live()[0]
    assert stored["witnesses"] == ["text"] and stored["trust_level"] == "likely"
    assert stored["text_proof"]["kind"] == "schedule"
    assert proof.code_proved(stored)
    assert proof.governs(stored).startswith("GV's project schedule")
    assert "project schedule" in proof.proof_line(stored)
    assert proof.proof_group(stored) == "clouded"  # the record switched off (the tests' default)
    monkeypatch.setenv(check_record.SWITCH_ENV, "on")
    assert proof.proof_group(stored) == "likely_problem"  # its kind has no keyed cases yet
    assert "project schedule" in (proof.held_back(stored) or "")


def test_with_the_switch_on_words_alone_never_make_a_red(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_schedule, "SHOULD_BE_RED", True)
    pages = _pages()
    pages[1][1] = at("Harbor Mist marble polished", 940, 50, 1100)  # no product code
    context = _context(pages)
    _lines, found = _check(context, 1)
    assert any(m.what == "table_row" and m.proof is None for m in found)
    assert red_findings(context.project, context, 1) == []


# ---- the upload ------------------------------------------------------------------------------------


class _Source:
    def __init__(self, name: str, data: bytes, kind: str) -> None:
        self.name, self.kind, self.pages = name, kind, 1
        self.sha256 = hashlib.sha256(data).hexdigest()
        self._data = data

    def read(self) -> bytes:
        return self._data


def test_the_schedule_is_copied_beside_the_drawings_and_read_once(tmp_path: Path) -> None:
    data = item_workbook()
    sources = [_Source("../Items.xlsx", data, project_schedule.SCHEDULE_WORDS)]
    listing = project_schedule.sync(tmp_path, sources)
    assert listing == [
        {"name": "Items.xlsx", "sha256": sources[0].sha256, "kind": project_schedule.SCHEDULE_WORDS}
    ]
    assert (tmp_path / "schedules" / "Items.xlsx").read_bytes() == data
    loaded = project_schedule.load(tmp_path)
    assert loaded is not None and loaded.items
    assert project_schedule.load(tmp_path) is loaded  # read once
    # Gone from the set: the copy and its listing are removed.
    assert project_schedule.sync(tmp_path, []) == []
    assert project_schedule.load(tmp_path) is None
    assert not (tmp_path / "schedules").exists()
    bad = _Source("x.csv", b"TAG,DESCRIPTION\n", project_schedule.SCHEDULE_WORDS)
    bad.sha256 = SHA
    with pytest.raises(ValueError, match="hash"):
        project_schedule.sync(tmp_path, [bad])


def test_the_chat_keeps_the_schedule_out_of_the_drawings_listing(tmp_path: Path) -> None:
    from app.api.assistant_chats import _KIND_WORDS
    from app.review.assistant.chat_service import UploadSource, sync_uploads
    from app.review.assistant.sandbox import ChatWorkspace

    assert is_schedule(_KIND_WORDS["schedule"])
    assert not is_schedule(_KIND_WORDS["shop"]) and not is_schedule(_KIND_WORDS["architectural"])
    drawing = ruled_pdf([["A", "B", "C"], ["1", "2", "3"]])
    data = finish_csv()
    sources = [
        UploadSource(
            "vendor.pdf",
            hashlib.sha256(drawing).hexdigest(),
            _KIND_WORDS["shop"],
            1,
            lambda: drawing,
        ),
        UploadSource(
            "finishes.csv",
            hashlib.sha256(data).hexdigest(),
            _KIND_WORDS["schedule"],
            1,
            lambda: data,
        ),
    ]
    workspace = ChatWorkspace(tmp_path / "chat")
    listing, changed = sync_uploads(workspace, sources)
    assert changed and [item["name"] for item in listing] == ["vendor.pdf"]
    assert sorted(p.name for p in workspace.uploads.iterdir()) == ["vendor.pdf"]
    schedule = project_schedule.load(workspace.root)
    assert schedule is not None and schedule.finish("ST-02") is not None


def test_only_a_project_schedule_may_be_a_spreadsheet() -> None:
    plain = DocumentRegistration(kind=DocumentKind.SHOP, sha256=SHA)
    assert plain.media_type == "application/pdf"
    sheet = DocumentRegistration(kind=DocumentKind.SCHEDULE, sha256=SHA, media_type=XLSX_MEDIA_TYPE)
    assert sheet.media_type == XLSX_MEDIA_TYPE
    with pytest.raises(ValidationError, match="Only a project schedule"):
        DocumentRegistration(kind=DocumentKind.SHOP, sha256=SHA, media_type="text/csv")
    with pytest.raises(ValidationError):
        DocumentRegistration(kind=DocumentKind.SCHEDULE, sha256=SHA, media_type="image/png")  # type: ignore[arg-type]
