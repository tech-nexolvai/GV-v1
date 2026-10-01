"""Surveying which kinds of text a page carries (formats phase 1).

Verification for: `extraction/text_sources.py`.

Each PDF is written by hand and carries one form a vendor's numbers can arrive in: text in the page's
own content, AutoCAD's SHX notes, a pasted drawing holding text, a pasted drawing holding only drawn
strokes, and a picture. The test that matters most is the last one — a kind no route reads is
reported as unread, never as a page with nothing on it.

No client drawing is read here.
"""

from __future__ import annotations

import zlib

import pytest

from extraction.text_sources import READ_BY, TextKind, TextSources, survey_page
from tests.extraction.test_annotations import _appearance, _cad_text, _pdf, _stamp

HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _single_page(content: bytes, resources: bytes) -> bytes:
    """A one-page PDF whose own content stream draws `content`, with `resources`."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Resources "
        + resources
        + b" /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        HELVETICA,
        (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
        ),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


#: A text PDF: the number is text in the page's own content.
TEXT_PAGE = _single_page(b"BT /F1 12 Tf 50 50 Td (36) Tj ET", b"<< /Font << /F1 5 0 R >> >>")

#: A scan: one image across the page, and nothing else.
SCANNED_PAGE = _single_page(b"q 400 0 0 300 0 0 cm /Im1 Do Q", b"<< /XObject << /Im1 6 0 R >> >>")

#: The client's form: a pasted drawing whose characters are drawn strokes.
DRAWN_PAGE = _pdf(annotations=[_stamp(appearance_object=6)], extra_objects=[_appearance()])

#: An AutoCAD SHX export: the drawn strokes, and one note per string.
CAD_PAGE = _pdf(
    annotations=[_cad_text('36"'), _cad_text("12", rect=b"[240 40 260 55]")],
    extra_objects=[],
)


def _stamp_with_text() -> bytes:
    """A pasted drawing whose appearance holds real font text: a snapshot of a text PDF."""
    stream = b"BT /F1 12 Tf 110 520 Td (36) Tj ET"
    compressed = zlib.compress(stream)
    appearance = (
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [100 500 400 700] "
        b"/Matrix [1 0 0 1 -100 -500] /Resources << /Font << /F1 7 0 R >> >> "
        b"/Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream"
    )
    return _pdf(annotations=[_stamp(appearance_object=6)], extra_objects=[appearance, HELVETICA])


def test_a_text_pdf_is_exact_text() -> None:
    sources = survey_page(TEXT_PAGE, 0)

    assert sources.content_characters == 2
    assert sources.kinds == (TextKind.EXACT_TEXT,)
    assert sources.unread == ()


def test_autocad_shx_notes_are_counted() -> None:
    sources = survey_page(CAD_PAGE, 0)

    assert sources.cad_text_notes == 2
    assert TextKind.CAD_TEXT_NOTES in sources.kinds
    assert sources.unread == ()


def test_a_printed_drawing_is_drawn_shapes() -> None:
    sources = survey_page(DRAWN_PAGE, 0)

    assert sources.drawn_paths > 0
    assert sources.kinds == (TextKind.DRAWN_SHAPES,)


def test_a_picture_with_nothing_else_is_scanned() -> None:
    sources = survey_page(SCANNED_PAGE, 0)

    assert sources.images == 1
    assert sources.kinds == (TextKind.SCANNED,)


def test_text_in_a_pasted_drawing_is_read_exactly() -> None:
    """**#738's mistake, corrected.** Font text inside a pasted drawing decodes; the survey counts
    its characters as readable and names the route that reads them."""
    sources = survey_page(_stamp_with_text(), 0)

    assert sources.stamp_text_decoded == 2
    assert sources.stamp_text_undecoded == 0
    assert TextKind.STAMP_TEXT in sources.kinds
    assert sources.unread == ()


def test_a_kind_nobody_reads_is_reported_unread() -> None:
    """**The test that matters.** Outcome: text that maps to no characters is named as unread —
    not lost inside a page that seems to have no numbers."""
    sources = TextSources(
        page_index=0,
        content_characters=0,
        cad_text_notes=0,
        stamp_text_decoded=0,
        stamp_text_undecoded=5,
        stamp_text_coloured=0,
        drawn_paths=40,
        images=0,
    )

    assert sources.unread == (TextKind.UNDECODED_TEXT,)
    assert sources.as_payload()["not_read_yet"] == ["undecoded_text"]


def test_an_empty_page_has_no_kinds() -> None:
    sources = survey_page(_pdf(annotations=[]), 0)

    assert sources.kinds == ()
    assert sources.as_payload()["kinds"] == []


def test_every_kind_says_what_reads_it() -> None:
    """Outcome: no kind can be added without stating which route reads it, or that none does."""
    assert set(READ_BY) == set(TextKind)


def test_a_page_beyond_the_document_is_refused() -> None:
    from extraction.reader import UnreadablePdf

    with pytest.raises(UnreadablePdf):
        survey_page(TEXT_PAGE, 3)


def test_coloured_text_in_a_pasted_drawing_is_reported_unread() -> None:
    """Outcome: coloured text inside a snapshot, which may be somebody's markup, is named as unread."""
    sources = TextSources(
        page_index=0,
        content_characters=0,
        cad_text_notes=0,
        stamp_text_decoded=12,
        stamp_text_undecoded=0,
        stamp_text_coloured=4,
        drawn_paths=0,
        images=0,
    )

    assert sources.kinds == (TextKind.STAMP_TEXT, TextKind.STAMP_COLOURED_TEXT)
    assert sources.as_payload()["not_read_yet"] == ["stamp_coloured_text"]
