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
from typing import Any
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


# ---------------------------------------------------------------------------
# Feet and inches, read whole (formats phase 1)
# ---------------------------------------------------------------------------

#: `2' -5"` written as one string with a space in it, as the client's stamps write their labels.
FEET_AND_INCHES = _pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (2' -5\") Tj ET\n1 w 20 40 m 120 40 l S\n")


def test_a_feet_and_inches_label_split_by_a_space_is_read_whole() -> None:
    """**The failure this prevents.** `extract_words` splits `2' -5"` into `2'` and `-5"`, and `2'`
    alone is 24 inches for a label that says 29. Outcome: one run, `2' -5"`, and no `2'`."""
    texts = [item.text for item in _contents(FEET_AND_INCHES).texts]

    # The font's standard encoding writes the straight apostrophe as `’`; `units.notation` reads it
    # as a foot mark, so the one run below values as 29 inches.
    assert texts == ['2’ -5"'], texts


def test_the_feet_and_inches_pattern_takes_only_whole_dimensions() -> None:
    from extraction.reader import FEET_INCH_TOKEN_RE

    for whole in ("2' -5\"", "6' -0\"", "5' -5 1/2\"", '3’ − 6"', "1'-0\"", "12' 3/4\""):
        assert FEET_INCH_TOKEN_RE.fullmatch(whole), whole
    for part in ("2'", '-5"', "984 [38 3/4]", '38 3/4"', "2' -5"):
        assert not FEET_INCH_TOKEN_RE.fullmatch(part), part


# ---------------------------------------------------------------------------
# Labels the words came apart from (#738). Each PDF is built to hold one shape measured on the
# client's sets; the measurement is in the test's docstring, not in the PDF.
# ---------------------------------------------------------------------------


def _set_aside(contents: PageContents) -> list[str]:
    return [label.reason.value for label in contents.set_aside]


def _valued(contents: PageContents) -> list[str]:
    from units.normalise import UnitNormalisationError, normalise_to_inches
    from units.notation import canonical_notation

    valued = []
    for item in contents.texts:
        try:
            normalise_to_inches(canonical_notation(item.text)[0])
        except UnitNormalisationError:
            continue
        valued.append(item.text)
    return valued


#: `24 3/4"` as CAD text sets it: a `24`, a smaller `3` over a `4`, an inch mark — beside a plain
#: `36"` that must still be read.
STACKED = _pdf(
    b"BT /F1 3 Tf 1 0 0 1 20 50 Tm (24) Tj ET\n"
    b"BT /F1 2 Tf 1 0 0 1 23.6 51.2 Tm (3) Tj ET\n"
    b"BT /F1 2 Tf 1 0 0 1 23.6 49 Tm (4) Tj ET\n"
    b'BT /F1 3 Tf 1 0 0 1 24.8 50 Tm (") Tj ET\n'
    b'BT /F1 10 Tf 1 0 0 1 100 50 Tm (36") Tj ET\n'
)


def test_a_stacked_fraction_is_read_whole_and_marked_stacked() -> None:
    """**The failure this prevents.** `extract_words` joins `24 3/4"` into `2434"` — 2,434 inches,
    exact and wrong; 69 labels on `AI_Set_1` came back that way. Outcome: `24 3/4"`, read from where
    each character sits, marked as stacked so it is only ever a reviewer's suggestion (#726) — and
    the plain label beside it read as before, unmarked."""
    contents = _contents(STACKED)
    items = {item.text: item for item in contents.texts}

    assert sorted(_valued(contents)) == ['24 3/4"', '36"']
    assert items['24 3/4"'].stacked is True
    assert items['36"'].stacked is False
    assert not any(text.startswith(("243", "244")) for text in items)
    assert contents.set_aside == ()


#: A stack `extract_words` split: the whole `2`, then a numerator `1"` set apart from it, over its
#: denominator `2`.
SPLIT_STACK = _pdf(
    b"BT /F1 10 Tf 1 0 0 1 20 50 Tm (2) Tj ET\n"
    b'BT /F1 6 Tf 1 0 0 1 29.6 56 Tm (1") Tj ET\n'
    b"BT /F1 6 Tf 1 0 0 1 29.6 49 Tm (2) Tj ET\n"
)


def test_a_numerator_split_from_its_stack_is_not_read_as_a_whole_number() -> None:
    """Outcome: the `1"` of a `2 1/2"` is not read as one inch."""
    contents = _contents(SPLIT_STACK)

    assert _valued(contents) == []
    assert "stacked_fraction" in _set_aside(contents)


#: Two lines of an appliance note, set one text height apart: no fraction in it.
TIGHT_NOTE = _pdf(
    b'BT /F1 10 Tf 1 0 0 1 20 60 Tm (29-7/8"W) Tj ET\n'
    b'BT /F1 10 Tf 1 0 0 1 20 50 Tm (16-1/2"H) Tj ET\n'
)


def test_lines_of_a_tightly_set_note_are_still_read() -> None:
    """**No false alarm.** Measured on `AI_Set_2`: tables and notes set a height apart look like a
    stack to a test of distance alone (335 words). Outcome: both lines are read."""
    contents = _contents(TIGHT_NOTE)

    assert sorted(item.text for item in contents.texts) == ['16-1/2"H', '29-7/8"W']
    assert contents.set_aside == ()


#: Millimetres written over their bracketed inches, close enough to come back as one word.
TWO_LINE_DUAL = _pdf(
    b"BT /F1 3 Tf 1 0 0 1 20 50 Tm (585) Tj ET\n" b"BT /F1 3 Tf 1 0 0 1 20.4 47 Tm ([23]) Tj ET\n"
)


def test_millimetres_over_inches_on_two_lines_are_read_as_one_dual_token() -> None:
    """`extract_words` made `[52835]` of `585` over `[23]` (84 on `AI_Set_1`). Outcome: `585 [23]`,
    the dual token it is — not a fraction, so not marked stacked."""
    contents = _contents(TWO_LINE_DUAL)
    (item,) = contents.texts

    assert item.text == "585 [23]"
    assert item.stacked is False
    assert contents.set_aside == ()


#: A sideways `2' - 0"`, as the client's elevations write their heights.
SIDEWAYS_FEET_AND_INCHES = _pdf(
    b"BT /F1 10 Tf 0 1 -1 0 150 10 Tm (2' - 0\") Tj ET\n", box=b"[0 0 200 100]"
)


def test_a_sideways_feet_and_inches_label_is_read_whole() -> None:
    """**The failure this prevents.** The join for `2' -5"` worked only on upright lines, so a
    sideways `2' - 0"` came back as `2'` (24 inches) and `0"` (none) — 94 such halves on `AI_Set_2`.
    Outcome: one run, read whole."""
    texts = [item.text for item in _contents(SIDEWAYS_FEET_AND_INCHES).texts]

    assert texts == ['2’ - 0"'], texts


#: A feet number with more digits right after it that no join can take: half a label.
HALF_A_LABEL = _pdf(b"BT /F1 10 Tf 1 0 0 1 20 50 Tm (7' +11\") Tj ET\n")


def test_a_feet_number_with_digits_right_after_it_is_not_read_alone() -> None:
    """Outcome: neither `7'` (84 inches) nor the `11"` after it is read as a label of its own."""
    contents = _contents(HALF_A_LABEL)

    assert _valued(contents) == []
    assert _set_aside(contents) == ["fragment", "fragment"]


#: `19.7"` whose `1` the words came apart from: set a little off the line, so `extract_words` puts it
#: on a line of its own, though it stands where the label's first digit belongs.
CUT_NUMBER = _pdf(
    b"BT /F1 10 Tf 1 0 0 1 24 53.5 Tm (1) Tj ET\n" b'BT /F1 10 Tf 1 0 0 1 29.6 50 Tm (9.7") Tj ET\n'
)


def test_a_number_cut_from_a_longer_one_is_not_read() -> None:
    """Measured on `AI_Set_1`: `9.7"` with the `1` of `19.7"` 0.26 heights before it."""
    contents = _contents(CUT_NUMBER)

    assert _valued(contents) == []
    assert _set_aside(contents) == ["fragment"]


#: Labels in a chain of dimensions: whole, and well apart.
CHAIN = _pdf(
    b'BT /F1 10 Tf 1 0 0 1 20 50 Tm (24") Tj ET\n'
    b'BT /F1 10 Tf 1 0 0 1 60 50 Tm (36") Tj ET\n'
    b"BT /F1 10 Tf 1 0 0 1 100 50 Tm (1' - 11\") Tj ET\n"
)


def test_labels_in_a_chain_of_dimensions_are_each_read() -> None:
    """**No false alarm.** Outcome: every label in the chain is read; none is taken for a piece."""
    contents = _contents(CHAIN)

    assert _valued(contents) == ['1’ - 11"', '24"', '36"']
    assert contents.set_aside == ()


#: One label drawn twice over itself, as a CAD program does for weight.
PRINTED_TWICE = _pdf(
    b"BT /F1 10 Tf 1 0 0 1 20 50 Tm (2' - 6\") Tj ET\n"
    b"BT /F1 10 Tf 1 0 0 1 20 50 Tm (2' - 6\") Tj ET\n"
)


def test_text_printed_twice_in_one_place_is_read_once() -> None:
    """Measured on `AI_Set_2`: a doubled `2' - 6"` came back as `22''`, 22 inches. Outcome: `2' - 6"`."""
    assert [item.text for item in _contents(PRINTED_TWICE).texts] == ['2’ - 6"']


def test_a_page_with_only_set_aside_text_says_so() -> None:
    contents = _contents(_pdf(b"BT /F1 10 Tf 1 0 0 1 20 50 Tm (7' +11\") Tj ET\n"))

    assert contents.texts == ()
    assert contents.unreadable_reason is not None
    assert "set aside" in contents.unreadable_reason


#: `24 3/4"` set large enough that its space is wider than a word break.
LARGE_MIXED = _pdf(b'BT /F1 24 Tf 1 0 0 1 10 40 Tm (24 3/4") Tj ET\n', box=b"[0 0 300 100]")


def test_inches_and_a_fraction_split_by_a_space_are_read_whole() -> None:
    """**The failure this prevents.** At 24 points the space is wider than `extract_words`' gap,
    and `3/4"` alone is three quarters of an inch. Outcome: one run, `24 3/4"`."""
    contents = _contents(LARGE_MIXED)

    assert [item.text for item in contents.texts] == ['24 3/4"']
    assert contents.set_aside == ()


# ---------------------------------------------------------------------------
# What a stacked label is never composed from
# ---------------------------------------------------------------------------


def _stack(whole: bytes, top: bytes, bottom: bytes, *, mark: bytes = b'"', x: float = 20) -> bytes:
    """`whole`, then `top` over `bottom`, then `mark`, at the client's sizes (3 and 2 points)."""
    width = 1.668 * len(whole)  # Helvetica digits are 556/1000 wide
    stream = b""
    if whole:
        stream += b"BT /F1 3 Tf 1 0 0 1 %.2f 50 Tm (%s) Tj ET\n" % (x, whole)
    stream += b"BT /F1 2 Tf 1 0 0 1 %.2f 51.2 Tm (%s) Tj ET\n" % (x + width + 0.26, top)
    stream += b"BT /F1 2 Tf 1 0 0 1 %.2f 49 Tm (%s) Tj ET\n" % (x + width + 0.26, bottom)
    if mark:
        stream += b"BT /F1 3 Tf 1 0 0 1 %.2f 50 Tm (%s) Tj ET\n" % (x + width + 1.46, mark)
    return stream


def test_a_fraction_standing_alone_reads_alone() -> None:
    contents = _contents(_pdf(_stack(b"", b"3", b"4")))

    assert [(item.text, item.stacked) for item in contents.texts] == [('3/4"', True)]


def test_a_stack_that_is_not_an_inch_fraction_is_left_unread() -> None:
    """Outcome: `5` over `3` — a numerator over a smaller number, or over anything that is not a
    fraction of an inch — is not composed, and stays with a reviewer."""
    for top, bottom in ((b"5", b"3"), (b"1", b"5")):
        contents = _contents(_pdf(_stack(b"24", top, bottom)))

        assert _valued(contents) == [], (top, bottom)
        assert _set_aside(contents) == ["stacked_fraction"]


def test_a_stack_without_its_unit_mark_is_not_suggested() -> None:
    """A bare `3/8` inside a note says nothing of its unit."""
    contents = _contents(_pdf(_stack(b"", b"3", b"8", mark=b"")))

    assert contents.texts == ()
    assert _set_aside(contents) == ["stacked_fraction"]


def test_a_label_printed_over_a_stack_stops_it_being_composed() -> None:
    """Measured on `AI_Set_1`: a sideways `4 3/4"` drawn through an upright `1 3/4"` composes
    perfectly from its own characters and is not what anybody wrote. Outcome: not suggested."""
    over = b"BT /F1 3 Tf 0 1 -1 0 22.5 48 Tm (19) Tj ET\n"
    contents = _contents(_pdf(_stack(b"24", b"3", b"4") + over))

    assert not any(item.stacked for item in contents.texts)
    assert "stacked_fraction" in _set_aside(contents)


#: `2 1/2"` at 4 points with its bar, split by `extract_words` into `21` and `2"` (#880).
SPLIT_STACK_WITH_BAR = _pdf(
    b"BT /F1 4 Tf 1 0 0 1 20 50 Tm (2) Tj 1 0 0 1 22.3 52 Tm (1) Tj "
    b'1 0 0 1 22.3 48 Tm (2) Tj 1 0 0 1 24.6 50 Tm (") Tj ET\n'
    b"0.3 w 22.4 51.2 m 24.4 51.2 l S\n"
)


def test_a_stack_the_words_came_apart_from_is_composed_round_its_bar() -> None:
    """Outcome: `2 1/2"`, marked stacked, on a page read as text as much as inside a pasted drawing."""
    contents = _contents(SPLIT_STACK_WITH_BAR)

    assert [(item.text, item.stacked) for item in contents.texts] == [('2 1/2"', True)]
    assert contents.set_aside == ()


def test_a_path_keep_path_refuses_is_no_bar_and_no_segment() -> None:
    """Outcome: with its only path refused, the same stack stays set aside and the page has no
    segments."""
    contents = read_page_contents(
        SPLIT_STACK_WITH_BAR, 0, document_version_id=DOCUMENT, dpi=DPI, keep_path=lambda _: False
    )

    assert not any(item.stacked for item in contents.texts)
    assert _set_aside(contents) == ["stacked_fraction", "stacked_fraction"]
    assert contents.segments == ()


# ---------------------------------------------------------------------------
# Every character is read into one label at most (#894)
# ---------------------------------------------------------------------------

#: A line of large text running past a label on both sides, as a note runs across the labels of a
#: drawing. `extract_text_lines` gives a line the box round all its characters, so this line's box
#: encloses the label although none of the label's characters is on it.
ENCLOSING_LINE = (
    b"BT /F1 20 Tf 1 0 0 1 2 40 Tm (A) Tj ET\n" b"BT /F1 20 Tf 1 0 0 1 180 40 Tm (B) Tj ET\n"
)

#: A taller line round the first. Measured on `AI_Set_1`, five labels sat inside two lines besides
#: their own and came back three times each.
TALLER_LINE = (
    b"BT /F1 40 Tf 1 0 0 1 0 30 Tm (I) Tj ET\n" b"BT /F1 40 Tf 1 0 0 1 188 30 Tm (I) Tj ET\n"
)

#: What the two lines read as themselves.
_ENCLOSING_TEXT = frozenset({"A", "B", "I"})

#: Every kind of label measured coming back more than once on `AI_Set_1` and `AI_Set_2`, and the one
#: other kind the join reads whole, each set small where the lines above enclose it, with what it
#: reads as on a page of its own: `(text, stacked)` for each text, and the reason for each label set
#: aside.
DOUBLED_SHAPES: dict[str, tuple[bytes, list[tuple[str, bool]], list[str]]] = {
    # Split at the space by `extract_words` and joined back (AI_Set_1 p5, AI_Set_2 p8).
    "feet and inches": (
        b"BT /F1 4 Tf 1 0 0 1 80 45 Tm (2' -5\") Tj ET\n",
        [('2’ -5"', False)],
        [],
    ),
    # Split at its spaces and joined back (AI_Set_2 p7 and p8).
    "dual token": (
        b"BT /F1 4 Tf 1 0 0 1 80 45 Tm (984 [38 3/4]) Tj ET\n",
        [("984 [38 3/4]", False)],
        [],
    ),
    # Set large enough for its space to split it: the shape `MIXED_INCH_TOKEN_RE` joins. Not
    # measured doubled; held to the same rule.
    "inches and a fraction": (
        b'BT /F1 4 Tf 1 0 0 1 80 45 Tm (24 3/4") Tj ET\n',
        [('24 3/4"', False)],
        [],
    ),
    # Millimetres over their bracketed inches, composed from one word (AI_Set_1 p3, seven labels).
    "millimetres over inches": (
        (
            b"BT /F1 3 Tf 1 0 0 1 80 50 Tm (585) Tj ET\n"
            b"BT /F1 3 Tf 1 0 0 1 80.4 47 Tm ([23]) Tj ET\n"
        ),
        [("585 [23]", False)],
        [],
    ),
    # A stacked fraction composed from its word (AI_Set_1 p1, three labels).
    "stacked fraction": (
        (
            b"BT /F1 3 Tf 1 0 0 1 80 50 Tm (24) Tj ET\n"
            b"BT /F1 2 Tf 1 0 0 1 83.6 51.2 Tm (3) Tj ET\n"
            b"BT /F1 2 Tf 1 0 0 1 83.6 49 Tm (4) Tj ET\n"
            b'BT /F1 3 Tf 1 0 0 1 84.8 50 Tm (") Tj ET\n'
        ),
        [('24 3/4"', True)],
        [],
    ),
    # The same, sideways (AI_Set_1 p1, three labels).
    "sideways stacked fraction": (
        (
            b"BT /F1 3 Tf 0 1 -1 0 80 40 Tm (24) Tj ET\n"
            b"BT /F1 2 Tf 0 1 -1 0 78.8 43.6 Tm (3) Tj ET\n"
            b"BT /F1 2 Tf 0 1 -1 0 81 43.6 Tm (4) Tj ET\n"
            b'BT /F1 3 Tf 0 1 -1 0 80 44.8 Tm (") Tj ET\n'
        ),
        [('24 3/4"', True)],
        [],
    ),
    # A stack the words came apart from, composed round its bar (#880; AI_Set_1 p1, two labels).
    "stack composed round its bar": (
        (
            b"BT /F1 4 Tf 1 0 0 1 80 50 Tm (2) Tj 1 0 0 1 82.3 52 Tm (1) Tj "
            b'1 0 0 1 82.3 48 Tm (2) Tj 1 0 0 1 84.6 50 Tm (") Tj ET\n'
            b"0.3 w 82.4 51.2 m 84.4 51.2 l S\n"
        ),
        [('2 1/2"', True)],
        [],
    ),
    # A joined label set aside as a piece of a longer one, a digit standing inside its span: the
    # label was set aside twice (AI_Set_1 p4).
    "piece of a longer label": (
        (
            b"BT /F1 4 Tf 1 0 0 1 80 45 Tm (1' -0\") Tj ET\n"
            b"BT /F1 4 Tf 1 0 0 1 85 48.2 Tm (3) Tj ET\n"
        ),
        [("3", False)],
        ["fragment"],
    ),
}


def _readings(contents: PageContents) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """`(texts, set_aside)`, every field of each, leaving out the enclosing lines' own letters."""
    texts = [
        (
            item.text,
            item.stacked,
            item.extent,
            item.image_extent,
            item.rotation_degrees,
            item.upright,
        )
        for item in contents.texts
        if item.text not in _ENCLOSING_TEXT
    ]
    labels = [(label.reason, label.extent, label.image_extent) for label in contents.set_aside]
    return texts, labels


@pytest.mark.parametrize("shape", sorted(DOUBLED_SHAPES))
def test_a_label_inside_another_lines_box_is_read_once(shape: str) -> None:
    """**The failure this prevents** (#894). The dual-token join put a word on every line whose box
    holds it, and a line's box can hold words that are not on it: measured on `AI_Set_1`, a note
    running across a drawing enclosed a stacked fraction none of whose characters it has. The label
    was joined on its own line and again on the note's, and read twice from the same characters,
    each copy with the same text, mark and place: 26 extra readings and two extra labels set aside
    on the two client sets. Outcome: inside two such lines, each shape reads exactly as it does on
    a page of its own — same text, same mark, same place — and once."""
    label, texts, reasons = DOUBLED_SHAPES[shape]
    alone = _contents(_pdf(label))
    enclosed = _contents(_pdf(label + ENCLOSING_LINE + TALLER_LINE))

    assert [(item.text, item.stacked) for item in alone.texts] == texts
    assert _set_aside(alone) == reasons
    assert _readings(enclosed) == _readings(alone)


def _characters_read_twice(data: bytes, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The characters of the page that more than one of its readings is made of, texts and labels
    set aside alike, each as its text.

    `TextItem` does not carry its characters, so they are taken from the runs the reader turns into
    readings: every run `_text_item` makes a reading of is a text or a set-aside label of the page.
    The runs are counted against the readings, so a reading made some other way fails here rather
    than going unchecked.
    """
    from extraction import reader

    runs: dict[int, dict[str, Any]] = {}  # each run held, so no other run can be given its `id`
    original = reader._text_item

    def recording(word: dict[str, Any], *args: Any) -> TextItem | None:
        item = original(word, *args)
        if item is not None:
            runs[id(word)] = word
        return item

    monkeypatch.setattr(reader, "_text_item", recording)
    contents = read_page_contents(data, 0, document_version_id=DOCUMENT, dpi=DPI)

    assert len(runs) == len(contents.texts) + len(contents.set_aside)
    readings: dict[int, int] = {}
    texts: dict[int, str] = {}
    for run in runs.values():
        for char in run.get("chars") or ():
            readings[id(char)] = readings.get(id(char), 0) + 1
            texts[id(char)] = str(char["text"])
    return sorted(texts[key] for key, count in readings.items() if count > 1)


#: Every page this module builds, and each doubled shape inside its enclosing lines.
_EVERY_PAGE = {
    "drawing": DRAWING,
    "feet and inches": FEET_AND_INCHES,
    "stacked": STACKED,
    "split stack": SPLIT_STACK,
    "tight note": TIGHT_NOTE,
    "two-line dual": TWO_LINE_DUAL,
    "sideways feet and inches": SIDEWAYS_FEET_AND_INCHES,
    "half a label": HALF_A_LABEL,
    "cut number": CUT_NUMBER,
    "chain": CHAIN,
    "printed twice": PRINTED_TWICE,
    "large mixed": LARGE_MIXED,
    "split stack with bar": SPLIT_STACK_WITH_BAR,
    **{
        f"{shape}, enclosed": _pdf(label + ENCLOSING_LINE + TALLER_LINE)
        for shape, (label, _, _) in DOUBLED_SHAPES.items()
    },
}


@pytest.mark.parametrize("page", sorted(_EVERY_PAGE))
def test_no_character_is_read_into_two_labels(page: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """**The invariant #894 restores.** A character drawn once is part of one reading at most: a
    text, or a label set aside. Two readings of one character are one label counted twice — a second
    row for a person to tick, and a second vote in every count readers are scored by."""
    assert _characters_read_twice(_EVERY_PAGE[page], monkeypatch) == []


def test_the_check_finds_a_character_read_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check above, run where the join hands every label over twice as #894 found it doing,
    names that label's characters: so it cannot pass by finding nothing."""
    from extraction import reader

    original = reader._dual_tokens

    def twice(*args: Any) -> tuple[tuple[TextItem, Any, dict[str, Any]], ...]:
        found = original(*args)
        return found + tuple((item, box, dict(run)) for item, box, run in found)

    monkeypatch.setattr(reader, "_dual_tokens", twice)

    assert _characters_read_twice(FEET_AND_INCHES, monkeypatch) == sorted('2’-5"')


def test_a_token_already_found_keeps_its_characters() -> None:
    """`_join_lines` joins no character a token already in `found` holds, whichever call found it:
    the sideways lines are joined after the upright ones, into the same list. Outcome: the same two
    words on a second line, in the same call or in a later one, add nothing."""
    from evidence.coordinates import PageTransform
    from extraction import reader

    def word(text: str, x0: float) -> dict[str, Any]:
        return {
            "text": text,
            "chars": [{"text": char} for char in text],
            "x0": x0,
            "top": 40,
            "x1": x0 + 5,
            "bottom": 45,
            "upright": True,
        }

    feet, inches = word("2'", 20), word('-5"', 27)
    sheet = (Decimal(0), Decimal(0), Decimal(200), Decimal(100))
    transform = PageTransform(dpi=DPI, rotation=0, media_box=sheet, crop_box=sheet)
    found: list[tuple[TextItem, Any, dict[str, Any]]] = []

    def join(lines: list[list[dict[str, Any]]]) -> None:
        reader._join_lines(lines, found, transform, Decimal(100), DOCUMENT, 0)

    join([[feet, inches], [feet, inches]])
    assert [run["text"] for _, _, run in found] == ["2' -5\""]

    join([[feet, inches]])
    assert len(found) == 1

    # Nor any run with one such character in it: `7' -5"` would share the inches.
    join([[word("7'", 20), inches]])
    assert len(found) == 1


#: The same label drawn twice, well apart on one line: two labels.
ALIKE = _pdf(
    b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (2' -5\") Tj ET\n"
    b"BT /F1 10 Tf 1 0 0 1 120 70 Tm (2' -5\") Tj ET\n"
)


def test_two_labels_alike_are_each_read() -> None:
    """**No false merge.** Read once means once for each label drawn, not once for each value: what
    keeps a character to one label is the character itself. Outcome: both labels are read."""
    contents = _contents(ALIKE)

    assert [item.text for item in contents.texts] == ['2’ -5"', '2’ -5"']
    assert len({item.image_extent for item in contents.texts}) == 2
