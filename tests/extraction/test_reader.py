"""Opening a PDF and reporting what is on its pages.

Verification for: `extraction/reader.py` (B2.1, #123).

**The PDFs here are written by hand, from a literal content stream.** Not reportlab, so the tests add
no dependency; not a captured drawing, so there is nothing to be wrong about. The file states exactly
what it contains — text `38 3/4` at `(20, 70)`, a rotated `984`, a line from `(20, 40)` to
`(120, 40)` — and the tests assert the reader reported *that*.

This is not the fixture `AGENTS.md` §9 forbids. Inventing a drawing in order to tune a threshold
encodes today's guess as ground truth; constructing a PDF with known content to check that a reader
repeats it back is a test of faithfulness, and there is no threshold anywhere in it.

The two tests worth reading are `test_rotated_text_is_not_reversed` and
`test_a_page_with_no_text_says_why`. Both guard failures that are invisible: a plausible wrong number,
and a page that reads as having no dimensions.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from extraction.reader import (
    PageContents,
    TextItem,
    UnreadablePdf,
    read_page_contents,
    read_pages,
)

DOCUMENT = UUID("11111111-1111-4111-8111-111111111111")
DPI = 150

#: A real PDF that happens to be in the repository. Not a drawing and not treated as one — it is here
#: so the reader meets a document it was not designed against.
REAL_PDF = (
    pathlib.Path(__file__).resolve().parents[2] / "docs" / "GV_Backend_Architecture_Proposal.pdf"
)


def _pdf(content: bytes, *, box: bytes = b"[0 0 200 100]", rotate: bytes = b"") -> bytes:
    """A one-page PDF containing exactly `content`.

    Assembled by hand because every byte then has a reason to be there. The cross-reference table is
    built from the real object offsets, so this is a valid PDF rather than something that happens to
    parse — a reader tested against a malformed file would be tested against the wrong thing.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox "
        + box
        + rotate
        + b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
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


#: Upright text, rotated text, and one horizontal line — the three things a dimension needs.
DRAWING = _pdf(
    b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (38 3/4) Tj ET\n"
    b"BT /F1 10 Tf 0 1 -1 0 150 30 Tm (984) Tj ET\n"
    b"1 w 20 40 m 120 40 l S\n"
)

#: Geometry but no text: what an outline-plotted or scanned sheet looks like to pdfplumber.
NO_TEXT = _pdf(b"1 w 20 40 m 120 40 l S\n")


def _contents(data: bytes = DRAWING, page: int = 0) -> PageContents:
    return read_page_contents(data, page, document_version_id=DOCUMENT, dpi=DPI)


def _by_text(contents: PageContents) -> dict[str, TextItem]:
    return {item.text: item for item in contents.texts}


# ---------------------------------------------------------------------------
# The two silent failures
# ---------------------------------------------------------------------------


def test_rotated_text_is_not_reversed() -> None:
    """**A dimension written bottom-to-top comes back backwards unless asked otherwise.**

    pdfplumber's default returns `489` where this drawing says `984`. That is the worst thing a reader
    can do: the result is a real number, correctly parsed, and wrong — no downstream check can catch
    it, because 489 is a perfectly plausible dimension and every arithmetic guard in the system would
    pass it through.

    Measured, not theorised: `char_dir_rotated="btt"` is what fixes it, and this asserts both halves —
    the right number present and the reversed one absent.
    """
    texts = _by_text(_contents())

    assert "984" in texts, f"rotated text was not read as written; got {sorted(texts)}"
    assert "489" not in texts, "the rotated dimension came back reversed"


def test_a_page_with_no_text_says_why() -> None:
    """**Empty and unread must not look alike.**

    Some CAD plot configurations convert text to outlines and scanned sheets never had text objects,
    so pdfplumber returns nothing for both. Reported as an empty page, that travels downstream as
    "this drawing shows no dimensions" — a false pass by omission, which is the failure the whole
    system exists to prevent.
    """
    page = read_pages(NO_TEXT)[0]

    assert page.vector_character_count == 0
    assert page.unreadable_reason is not None
    assert "outlines" in page.unreadable_reason
    assert "OCR" in page.unreadable_reason

    contents = _contents(NO_TEXT)
    assert contents.texts == ()
    assert contents.readable is False


def test_a_page_with_text_is_not_reported_unreadable() -> None:
    """The control. Without it the test above passes against a reader that calls everything
    unreadable, which would be just as useless and much harder to notice."""
    page = read_pages(DRAWING)[0]

    assert page.vector_character_count > 0
    assert page.unreadable_reason is None
    assert _contents().readable is True


# ---------------------------------------------------------------------------
# What was on the page is what comes back
# ---------------------------------------------------------------------------


def test_the_page_reports_its_own_size_and_rotation() -> None:
    """From the page dictionary, as `Decimal`. The `pages` table requires both and constrains
    rotation to a quarter turn."""
    page = read_pages(DRAWING)[0]

    assert page.index == 0
    assert page.width_pt == Decimal(200)
    assert page.height_pt == Decimal(100)
    assert page.rotation == 0


@pytest.mark.parametrize(
    ("declared", "expected"),
    [(b" /Rotate 0", 0), (b" /Rotate 90", 90), (b" /Rotate 270", 270), (b" /Rotate -90", 270)],
)
def test_page_rotation_is_normalised_to_a_quarter_turn(declared: bytes, expected: int) -> None:
    """`/Rotate -90` is a real thing plotters emit, and it means 270.

    Normalised rather than refused: everything downstream accepts only the four values, and a sheet
    that reads perfectly well should not be rejected over how its rotation was spelled.
    """
    page = read_pages(_pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (12) Tj ET\n", rotate=declared))[0]

    assert page.rotation == expected


def test_upright_text_is_reported_as_unrotated() -> None:
    """The other half of the rotation test. A reader that returned 90 for everything would pass the
    rotated case and be wrong about every horizontal dimension on the sheet."""
    texts = _by_text(_contents())

    assert texts["38"].rotation_degrees == 0
    assert texts["38"].upright is True
    assert texts["984"].rotation_degrees == 90
    assert texts["984"].upright is False


def test_a_dimension_with_a_fraction_arrives_in_two_pieces() -> None:
    """**Documented because it is a real limitation, not because it is desired.**

    `38 3/4` comes back as `38` and `3/4`. This is the fragmentation the published work on this
    problem is mostly about — Scheibel et al. (2021) cluster fragments back together and reach 88%
    recall — and no tolerance setting fixes it, because the space between a whole number and its
    fraction is genuine.

    Merging them is a later step with a threshold in it, and thresholds need real drawings (#274). The
    test exists so the next person finds this stated rather than discovering it.
    """
    texts = _by_text(_contents())

    assert "38" in texts
    assert "3/4" in texts
    assert "38 3/4" not in texts


def test_a_drawn_line_becomes_a_segment_with_the_right_axis() -> None:
    """The line runs from (20,40) to (120,40) — horizontal, and `DimensionExtent` derives that."""
    contents = _contents()

    assert len(contents.segments) == 1
    assert contents.segments[0].axis == "horizontal"
    assert contents.segments[0].page == 0
    assert contents.segments[0].document_version_id == DOCUMENT


def test_a_fractional_page_size_is_exact_and_not_a_float_widened_to_decimal() -> None:
    """**A mutation-testing catch: nothing here exercised the float path.**

    pdfplumber returns floats, and `Decimal(200.1)` is
    `200.099999999999994315658113919198513031005859375` where `Decimal(str(200.1))` is `200.1`. The
    difference is invisible in stored coordinates, because those are normalised through *integer*
    image space and the rounding erases it — so replacing the conversion with `Decimal(float)` passed
    every other test in this module.

    A page size does not go through that rounding. It is persisted to the `pages` table as written,
    and a 45-digit tail there is a page size that will not compare equal to itself on a re-read.

    The earlier fixtures all declare whole-number boxes, which is why this needs its own PDF.
    """
    page = read_pages(
        _pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (12) Tj ET\n", box=b"[0 0 200.1 100.3]")
    )[0]

    assert page.width_pt == Decimal("200.1")
    assert page.height_pt == Decimal("100.3")
    # The claim, stated exactly: through `str`, not through the binary float.
    assert str(page.width_pt) == "200.1"
    assert str(page.height_pt) == "100.3"


def test_stored_coordinates_are_normalised_and_exact() -> None:
    """Stored space is `0..1` against the visible page, and every coordinate is a `Decimal`.

    A float here would put binary rounding into the polygon a reviewer is shown as the evidence
    behind a verdict — `ADR-0001`, one layer out from the arithmetic it protects.
    """
    for item in _contents().texts:
        for point in item.extent.points:
            assert isinstance(point.x, Decimal)
            assert isinstance(point.y, Decimal)
            assert Decimal(0) <= point.x <= Decimal(1)
            assert Decimal(0) <= point.y <= Decimal(1)


def test_geometry_is_read_from_lines_rectangles_and_paths() -> None:
    """A plotter may draw a dimension line as any of the three, and which it picks is a property of
    the software rather than of the drawing. Dropping two of the three would make a dimension
    invisible because of how the sheet was exported."""
    with_rect = _pdf(
        b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (12) Tj ET\n"
        b"1 w 20 40 m 120 40 l S\n"  # a line
        b"1 w 30 10 60 20 re S\n"  # a rectangle: four edges
    )

    contents = _contents(with_rect)

    # One line plus four rectangle edges. Asserted as a lower bound, because a path may also be
    # reported as a curve and counting exactly would pin pdfplumber's classification rather than the
    # reader's behaviour.
    assert len(contents.segments) >= 5


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"not a pdf at all", b"%PDF-1.4\nbroken"])
def test_a_file_that_is_not_a_readable_pdf_is_refused(data: bytes) -> None:
    """Refused, not returned as a document with no pages. `RawPage` requires a page size, and a
    reader that could not find one has not found an unreadable page — it has found a file it cannot
    parse, and the two need different handling."""
    with pytest.raises(UnreadablePdf):
        read_pages(data)


def test_a_page_beyond_the_document_is_refused_by_name() -> None:
    """A stage may be replaying an old message that names a page the document no longer has. The
    error says how many there are, which is what somebody debugging needs."""
    with pytest.raises(UnreadablePdf, match="beyond the 1 pages"):
        _contents(DRAWING, page=7)


@pytest.mark.parametrize("dpi", [0, -150, True])
def test_dpi_must_be_a_positive_integer(dpi: object) -> None:
    """No default, because stored coordinates are reached through integer image space and the
    resolution decides how much precision survives. A default here would pick that silently for
    every caller."""
    with pytest.raises(ValueError, match="dpi"):
        read_page_contents(DRAWING, 0, document_version_id=DOCUMENT, dpi=dpi)  # type: ignore[arg-type]


def test_the_bytes_must_be_bytes() -> None:
    with pytest.raises(TypeError):
        read_pages("a path, not the file")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A document it was not designed against
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not REAL_PDF.exists(), reason="the architecture PDF is not in this checkout")
def test_the_reader_survives_a_real_multi_page_document() -> None:
    """Not a drawing, and not treated as one — a smoke test that real-world input does not break it.

    Hand-written fixtures prove the reader repeats back what it was given; they cannot prove it copes
    with a document produced by software nobody here controls. This one has 26 pages, embedded fonts
    and real vector graphics.
    """
    pages = read_pages(REAL_PDF.read_bytes())

    assert len(pages) > 1
    assert all(page.width_pt > 0 and page.height_pt > 0 for page in pages)
    assert all(page.rotation in {0, 90, 180, 270} for page in pages)
    assert any(
        page.vector_character_count > 0 for page in pages
    ), "a text-bearing PDF reported no characters on any page"

    contents = read_page_contents(REAL_PDF.read_bytes(), 0, document_version_id=uuid4(), dpi=DPI)
    assert contents.texts
    assert contents.readable is True
