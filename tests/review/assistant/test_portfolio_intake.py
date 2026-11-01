"""A PDF portfolio at the chat's intake (#1750, item 2), on made-up PDFs, $0.

Each attached PDF is copied in as a document of its own (through the front door, named after its
file name), the pre-flight report says which attached files were not read and why, a portfolio
that cannot be unpacked stops the check with a plain reason, and an ordinary PDF is copied byte for
byte with exactly the listing it always had.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import preflight, set_pack
from app.review.assistant.chat_service import UploadSource, sync_uploads, uploaded_files_text
from app.review.assistant.sandbox import ChatWorkspace
from tests.extraction.test_portfolio import SHEET, A, B, _drawing, _with_files

SHOP = "the vendor's shop drawings (a sheet may carry the architect's drawing beside it)"
ARCHITECT = "the architect's drawings"


def _source(data: bytes, name: str, kind: str = ARCHITECT, reads: list[str] | None = None) -> Any:
    def read() -> bytes:
        if reads is not None:
            reads.append(name)
        return data

    return UploadSource(
        name=name, sha256=hashlib.sha256(data).hexdigest(), kind=kind, pages=1, read=read
    )


def _lines(listing: list[dict[str, Any]], kind: str) -> list[preflight.Line]:
    report = preflight.build(preflight.Inputs(listing=listing))
    return [line for line in report.lines if line.id == kind]


def test_a_portfolio_s_attached_pdfs_are_read_as_documents_of_their_own(tmp_path: Path) -> None:
    portfolio = _with_files({"A-101.pdf": A, "A-102.pdf": B, "costs.xlsx": SHEET})
    workspace = ChatWorkspace(tmp_path / "chat")
    reads: list[str] = []
    sources = [_source(portfolio, "Design set.pdf", reads=reads)]
    listing, changed = sync_uploads(workspace, sources)
    assert changed
    # The cover is not read; each drawing is its own document, its bytes as attached.
    assert [(item["name"], item["pages"]) for item in listing] == [
        ("A-101.pdf", 2),
        ("A-102.pdf", 1),
    ]
    assert (workspace.uploads / "A-101.pdf").read_bytes() == A
    assert (workspace.uploads / "A-102.pdf").read_bytes() == B
    assert not (workspace.uploads / "Design set.pdf").exists()
    first, second = listing
    came_from = {"name": "Design set.pdf", "sha256": sources[0].sha256}
    assert first["portfolio"] == second["portfolio"] == came_from
    assert first["sha256"] == hashlib.sha256(A).hexdigest() and first["kind"] == ARCHITECT
    # The spreadsheet is said to be not read, once, with why.
    assert first["attachments"] == [
        {
            "file": "Design set.pdf",
            "portfolio": True,
            "own_pages_read": False,
            "read": ["A-101.pdf", "A-102.pdf"],
            "not_read": [{"name": "costs.xlsx", "why": "a spreadsheet, not a drawing PDF"}],
        }
    ]
    assert "attachments" not in second
    (line,) = _lines(listing, "attachments")
    assert line.level == "note" and line.who == "you" and line.goes_on
    assert line.words == (
        "“Design set.pdf” is a PDF portfolio: 2 attached PDFs are read as files of their own "
        "(A-101.pdf; A-102.pdf); its cover page is not read. Not read, attached to "
        "“Design set.pdf”: costs.xlsx (a spreadsheet, not a drawing PDF)."
    )
    assert 'from_portfolio="Design set.pdf"' in uploaded_files_text(listing, changed=False)
    # A second sync knows the set is the same without reading the portfolio again.
    again, changed_again = sync_uploads(workspace, sources)
    assert not changed_again and again == listing and reads == ["Design set.pdf"]


def test_an_ordinary_pdf_is_copied_byte_for_byte_with_the_listing_it_always_had(
    tmp_path: Path,
) -> None:
    drawing = _drawing(2)
    workspace = ChatWorkspace(tmp_path / "chat")
    listing, _changed = sync_uploads(workspace, [_source(drawing, "shop.pdf", SHOP)])
    assert (workspace.uploads / "shop.pdf").read_bytes() == drawing
    (item,) = listing
    # Exactly the keys an upload had before portfolios: its set pack keeps its name.
    assert set(item) == {"name", "sha256", "pages", "kind", "intake", "intake_facts", "size"}
    old = [{"name": "shop.pdf", "sha256": item["sha256"], "kind": item["kind"]}]
    assert set_pack.pack_key(listing) == set_pack.pack_key(old)
    assert not _lines(listing, "attachments")


def test_a_shop_drawing_portfolio_is_joined_like_several_shop_files(tmp_path: Path) -> None:
    workspace = ChatWorkspace(tmp_path / "chat")
    reads: list[str] = []
    sources = [
        _source(_with_files({"C-1.pdf": A, "C-2.pdf": B}), "cabinets.pdf", SHOP, reads),
        _source(_drawing(1), "design.pdf", ARCHITECT, reads),
    ]
    listing, _changed = sync_uploads(workspace, sources)
    joined, design = listing
    assert joined["name"] == "Shop drawings (2 files joined).pdf" and joined["pages"] == 3
    assert [
        (p["name"], p["first_page"], p["portfolio"]["name"]) for p in joined["joined_from"]
    ] == [
        ("C-1.pdf", 1, "cabinets.pdf"),
        ("C-2.pdf", 3, "cabinets.pdf"),
    ]
    assert joined["attachments"][0]["read"] == ["C-1.pdf", "C-2.pdf"]
    assert design["name"] == "design.pdf" and "portfolio" not in design
    again, changed_again = sync_uploads(workspace, sources)
    assert not changed_again and again == listing and len(reads) == 2


def test_a_portfolio_inside_the_portfolio_is_reported(tmp_path: Path) -> None:
    inner = _with_files({"deep.pdf": B})
    workspace = ChatWorkspace(tmp_path / "chat")
    listing, _changed = sync_uploads(
        workspace, [_source(_with_files({"A-101.pdf": A, "more.pdf": inner}), "set.pdf")]
    )
    assert [item["name"] for item in listing] == ["A-101.pdf"]
    (line,) = _lines(listing, "attachments")
    assert "more.pdf (a portfolio inside the portfolio: not opened" in line.words
    assert "deep.pdf (inside “more.pdf”, a portfolio inside the portfolio)" in line.words


@pytest.mark.parametrize("kind", [ARCHITECT, SHOP])
def test_a_portfolio_too_big_to_unpack_stops_the_check_with_a_plain_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from extraction import portfolio

    monkeypatch.setattr(portfolio, "MAX_ATTACHMENTS", 2)
    workspace = ChatWorkspace(tmp_path / "chat")
    sources = [_source(_with_files({f"S-{n}.pdf": B for n in range(3)}), "big.pdf", kind)]
    if kind == SHOP:
        # Joined with another shop file: the refusal still names the portfolio.
        sources.append(_source(_drawing(1), "tops.pdf", SHOP))
    listing, _changed = sync_uploads(workspace, sources)
    report = preflight.build(preflight.Inputs(listing=listing))
    (stop,) = report.stops
    assert stop.id == "broken_file" and not stop.goes_on
    assert stop.words == (
        "“big.pdf” could not be opened: this PDF portfolio holds 3 attached files, more than "
        "the 2 that are unpacked; upload its drawings as separate PDFs."
    )
    assert stop.todo == "Upload the drawings as separate PDFs."


def test_a_portfolio_with_no_readable_drawing_stops_the_check(tmp_path: Path) -> None:
    workspace = ChatWorkspace(tmp_path / "chat")
    listing, _changed = sync_uploads(
        workspace, [_source(_with_files({"costs.xlsx": SHEET}), "set.pdf")]
    )
    assert [item["name"] for item in listing] == ["set.pdf"]
    report = preflight.build(preflight.Inputs(listing=listing))
    assert [line.id for line in report.stops] == ["broken_file"]
    assert "no drawing PDF that could be read" in report.stops[0].words
    (line,) = _lines(listing, "attachments")
    assert "costs.xlsx (a spreadsheet, not a drawing PDF)" in line.words


def test_a_drawing_set_with_a_file_attached_is_copied_as_it_is_and_says_so(
    tmp_path: Path,
) -> None:
    drawing = _with_files({"photo.jpg": b"\xff\xd8\xff"}, collection=False, pages=2)
    workspace = ChatWorkspace(tmp_path / "chat")
    listing, _changed = sync_uploads(workspace, [_source(drawing, "set.pdf")])
    assert [item["name"] for item in listing] == ["set.pdf"]
    assert (workspace.uploads / "set.pdf").read_bytes() == drawing
    (line,) = _lines(listing, "attachments")
    assert line.words == (
        "Not read, attached to “set.pdf”: photo.jpg (a picture, not a drawing PDF)."
    )


def test_attached_names_never_clash_with_an_upload(tmp_path: Path) -> None:
    workspace = ChatWorkspace(tmp_path / "chat")
    listing, _changed = sync_uploads(
        workspace,
        [_source(_with_files({"A-101.pdf": A}), "set.pdf"), _source(_drawing(1, "other"), "A-101.pdf")],
    )
    assert [item["name"] for item in listing] == ["A-101-1.pdf", "A-101.pdf"]
    assert (workspace.uploads / "A-101-1.pdf").read_bytes() == A
