"""PDF portfolios at the front door (#1750, item 2), on made-up PDFs built here, $0.

A portfolio's attached PDFs come out byte for byte, each with its own file name; what is attached
and not read (a spreadsheet, a picture, a portfolio inside the portfolio, a file too big) is listed
with a plain reason; an ordinary PDF gets `None` (nothing to do).
"""

from __future__ import annotations

import io
import zlib

import pikepdf
import pytest

from extraction import portfolio
from extraction.portfolio import unpack


def _drawing(pages: int = 1, label: str = "24 1/2") -> bytes:
    """A small drawing: `pages` pages, each printing `label`."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont="/Helvetica"
        )
    )
    for number in range(1, pages + 1):
        page = pdf.add_blank_page(page_size=(700, 500))
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(
            f"BT /F1 12 Tf 300 200 Td ({label} p{number}) Tj ET".encode()
        )
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _with_files(
    files: dict[str, bytes], *, collection: bool = True, pages: int = 1, raw: bool = False
) -> bytes:
    """A PDF of `pages` pages with `files` attached; a `/Collection` (a portfolio) when asked.
    `raw`: each file kept unpacked (no filter) instead of packed by pikepdf on saving."""
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page()
    if collection:
        pdf.Root.Collection = pikepdf.Dictionary(Type=pikepdf.Name.Collection, View=pikepdf.Name.D)
    for name, data in files.items():
        pdf.attachments[name] = pikepdf.AttachedFileSpec(pdf, data, filename=name)
    out = io.BytesIO()
    pdf.save(out, compress_streams=not raw)
    return out.getvalue()


A, B = _drawing(2, "A-101"), _drawing(1, "A-102")
SHEET = b"PK\x03\x04 made-up spreadsheet bytes"


def test_a_portfolio_gives_each_attached_pdf_byte_for_byte_and_says_what_is_not_a_drawing() -> None:
    found = unpack(_with_files({"A-101.pdf": A, "A-102.pdf": B, "costs.xlsx": SHEET}))
    assert found is not None and found.portfolio and found.refused is None
    # A /Collection's cover is not read: its drawings are the attached files.
    assert not found.keep_own_pages
    assert [(p.name, p.pages, p.data) for p in found.parts] == [
        ("A-101.pdf", 2, A),
        ("A-102.pdf", 1, B),
    ]
    assert [(s.name, s.why) for s in found.skipped] == [
        ("costs.xlsx", "a spreadsheet, not a drawing PDF")
    ]


def test_an_ordinary_pdf_has_nothing_to_unpack() -> None:
    assert unpack(_drawing(3)) is None
    # Nor does a file that cannot be opened: the front door says why, as before.
    assert unpack(b"%PDF-1.7 damaged") is None


def test_a_drawing_set_with_a_file_attached_is_read_as_it_is_and_says_so() -> None:
    found = unpack(
        _with_files({"spec.pdf": A, "photo.jpg": b"\xff\xd8\xff"}, collection=False, pages=3)
    )
    assert found is not None and not found.portfolio and found.keep_own_pages
    assert found.parts == ()
    assert [s.name for s in found.skipped] == ["photo.jpg", "spec.pdf"]
    assert found.skipped[0].why == "a picture, not a drawing PDF"
    assert "attachments of an ordinary PDF are not read" in found.skipped[1].why


def test_one_page_with_a_pdf_attached_is_a_portfolio_that_keeps_its_page() -> None:
    found = unpack(_with_files({"A-101.pdf": A}, collection=False, pages=1))
    assert found is not None and found.portfolio and found.keep_own_pages
    assert [p.name for p in found.parts] == ["A-101.pdf"]


def test_a_portfolio_inside_the_portfolio_is_reported_never_opened() -> None:
    inner = _with_files({"deep.pdf": B, "deep.csv": b"a,b"})
    found = unpack(_with_files({"A-101.pdf": A, "more.pdf": inner}))
    assert found is not None
    assert [p.name for p in found.parts] == ["A-101.pdf"]
    why = {s.name: s.why for s in found.skipped}
    assert "a portfolio inside the portfolio" in why["more.pdf"]
    assert "only one level is unpacked" in why["more.pdf"]
    # Its own files are listed as not read too, never unpacked.
    assert (
        why["deep.pdf"] == why["deep.csv"] == "inside “more.pdf”, a portfolio inside the portfolio"
    )


def test_a_pdf_attached_to_an_attached_drawing_is_listed_not_read() -> None:
    drawing_with_file = _with_files({"note.pdf": B}, collection=False, pages=2)
    found = unpack(_with_files({"A-101.pdf": drawing_with_file}))
    assert found is not None and [p.name for p in found.parts] == ["A-101.pdf"]
    assert [(s.name, s.why) for s in found.skipped] == [
        ("note.pdf", "attached inside “A-101.pdf”: only one level is unpacked")
    ]


def test_too_many_attached_files_refuses_the_portfolio_plainly() -> None:
    files = {f"sheet-{n}.pdf": B for n in range(4)}
    found = unpack(_with_files(files), max_attachments=3)
    assert found is not None and found.parts == ()
    assert found.refused == (
        "this PDF portfolio holds 4 attached files, more than the 3 that are unpacked; "
        "upload its drawings as separate PDFs"
    )


def test_a_file_too_big_is_not_read_and_says_why() -> None:
    found = unpack(_with_files({"A-101.pdf": A, "A-102.pdf": B}), max_bytes=len(B) + 10)
    assert found is not None and [p.name for p in found.parts] == ["A-102.pdf"]
    assert found.skipped[0].name == "A-101.pdf"
    assert found.skipped[0].why.startswith("too big (the most one attached file may be is")


def test_the_files_together_have_a_limit() -> None:
    found = unpack(_with_files({"A-101.pdf": A, "A-102.pdf": B}), max_total=len(A) + 10)
    assert found is not None and [p.name for p in found.parts] == ["A-101.pdf"]
    assert "the attached files together may be at most" in found.skipped[0].why


def test_a_small_file_that_unpacks_huge_is_stopped_at_the_limit() -> None:
    # 50 MB of zeros packs into about 50 KB; nothing says its size. Unpacked only to the limit.
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.Root.Collection = pikepdf.Dictionary(Type=pikepdf.Name.Collection)
    packed = zlib.compress(b"%PDF-1.7\n" + bytes(50_000_000), 9)
    stream = pikepdf.Stream(pdf, packed)
    stream.Filter = pikepdf.Name.FlateDecode
    stream.Type = pikepdf.Name.EmbeddedFile
    spec = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.Filespec, F="big.pdf", UF="big.pdf", EF={"/F": stream})
    )
    pdf.Root.Names = pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(Names=["big.pdf", spec]))
    out = io.BytesIO()
    pdf.save(out)
    found = unpack(out.getvalue(), max_bytes=1_000_000)
    assert found is not None and found.parts == ()
    assert found.skipped[0].why.startswith("too big")
    # Nothing readable left in a /Collection: refused, plainly.
    assert found.refused is not None and "no drawing PDF that could be read" in found.refused


def test_only_pdfs_go_on_whatever_their_names_say() -> None:
    found = unpack(
        _with_files(
            {"fake.pdf": b"not a pdf at all", "notes.bin": b"\x00\x01", "A-101.pdf": A}, raw=True
        )
    )
    assert found is not None and [p.name for p in found.parts] == ["A-101.pdf"]
    why = {s.name: s.why for s in found.skipped}
    assert why["fake.pdf"] == "named as a PDF but it is not one"
    assert why["notes.bin"] == "not a PDF, so not a drawing that can be read"


def test_an_attachment_s_folder_is_left_off_its_name() -> None:
    found = unpack(_with_files({"sets/round 2/A-101.pdf": A}))
    assert found is not None and [p.name for p in found.parts] == ["A-101.pdf"]


@pytest.mark.parametrize("limit", ["MAX_ATTACHMENTS", "MAX_ATTACHMENT_BYTES", "MAX_TOTAL_BYTES"])
def test_the_limits_are_set(limit: str) -> None:
    assert getattr(portfolio, limit) > 0
