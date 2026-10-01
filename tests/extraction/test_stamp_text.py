"""Reading the font text inside a pasted drawing (formats phase 1).

Verification for: `extraction/stamp_text.py`.

The hard stops first: the reviewer's notes and the page's own content never come back as stamp
text, and the file passed in is never changed. Then that the text comes back exactly, joined where
a space split a dimension, and placed where the stamp puts it on the page.

No client drawing is read here.
"""

from __future__ import annotations

import zlib
from decimal import Decimal
from uuid import UUID

import pytest

from extraction.stamp_text import drawing_ink, read_stamp_text, stamps_only
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp

DOCUMENT = UUID("22222222-2222-4222-8222-222222222222")
DPI = 150
HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _text_appearance(stream: bytes, font_object: int) -> bytes:
    """A stamp appearance holding font text, in its own space as the client's stamps are."""
    compressed = zlib.compress(stream)
    return (
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [100 500 400 700] "
        b"/Matrix [1 0 0 1 -100 -500] /Resources << /Font << /F1 "
        + str(font_object).encode()
        + b" 0 R >> >> /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream"
    )


def _sheet(text: bytes = b"(36) Tj", *, with_markup: bool = False) -> bytes:
    """A sheet whose pasted drawing says `text`, optionally with a reviewer's note beside it."""
    annotations = [_stamp(appearance_object=6 if not with_markup else 7)]
    if with_markup:
        annotations.insert(0, _free_text('38"'))
    appearance_object = 6 if not with_markup else 7
    stream = b"BT /F1 12 Tf 110 520 Td " + text + b" ET"
    return _pdf(
        annotations=annotations,
        extra_objects=[_text_appearance(stream, appearance_object + 1), HELVETICA],
    )


def _texts(data: bytes) -> list[str]:
    return [
        item.text
        for item in read_stamp_text(data, 0, document_version_id=DOCUMENT, dpi=DPI).contents.texts
    ]


def test_the_text_in_a_pasted_drawing_is_read_exactly() -> None:
    assert _texts(_sheet()) == ["36"]


def test_the_reviewers_note_is_never_read_as_the_drawings_text() -> None:
    """**The hard stop.** Outcome: a reviewer's `38"` beside the drawing does not come back."""
    texts = _texts(_sheet(with_markup=True))

    assert texts == ["36"]
    assert not any("38" in text for text in texts)


def test_a_dimension_split_by_a_space_is_read_whole() -> None:
    """Outcome: `2' -5"` in a pasted drawing is one run, not `2'` (24 inches) and `-5"`."""
    texts = _texts(_sheet(b"(2' -5\") Tj"))

    assert len(texts) == 1
    assert texts[0].startswith("2") and texts[0].endswith('5"'), texts


def test_the_text_is_placed_where_the_stamp_puts_it() -> None:
    """Outcome: the reading's box sits inside the stamp's own rectangle on the page."""
    reading = read_stamp_text(_sheet(), 0, document_version_id=DOCUMENT, dpi=DPI)
    (item,) = reading.contents.texts
    xs = [point.x for point in item.extent.points]
    ys = [point.y for point in item.extent.points]

    # The stamp's `/Rect` is [50 50 350 250] on a 400 x 300 page: in stored space (0..1, y down)
    # that is x 0.125..0.875 and y 0.1667..0.8333.
    assert Decimal("0.125") <= min(xs) and max(xs) <= Decimal("0.875")
    assert Decimal("0.16") <= min(ys) and max(ys) <= Decimal("0.84")


def test_a_page_without_pasted_text_says_so() -> None:
    reading = read_stamp_text(
        _pdf(annotations=[_stamp(appearance_object=6)], extra_objects=[_appearance()]),
        0,
        document_version_id=DOCUMENT,
        dpi=DPI,
    )

    assert reading.contents.texts == ()
    assert reading.contents.unreadable_reason is not None
    assert reading.characters.readable == 0


def test_the_stamps_line_work_is_not_read_twice() -> None:
    """Outcome: no segments — the stamp's paths are read by `annotations.py`, once."""
    reading = read_stamp_text(_sheet(), 0, document_version_id=DOCUMENT, dpi=DPI)

    assert reading.contents.segments == ()


def test_the_file_passed_in_is_never_changed() -> None:
    data = _sheet(with_markup=True)
    before = bytes(data)

    stamps_only(data, 0)

    assert data == before


def test_coloured_text_in_a_pasted_drawing_is_not_read() -> None:
    """**The vendor-layer rule, inside a snapshot.** Measured on `AI_Set_1`: it has no `/FreeText`;
    the reviewer's red corrections are text inside the pasted drawings, and were read as the
    vendor's. Outcome: the black `36"` is read, the red `38"` is counted and not read."""
    reading = read_stamp_text(
        _sheet(b'(36") Tj 1 0 0 rg 0 -20 Td (38") Tj'),
        0,
        document_version_id=DOCUMENT,
        dpi=DPI,
    )

    assert [item.text for item in reading.contents.texts] == ['36"']
    assert reading.characters.coloured == 3
    assert reading.characters.readable == 3


@pytest.mark.parametrize(
    ("colour", "ink"),
    [
        (None, True),
        ((0,), True),
        ((0.5,), True),
        ((0.0, 0.0, 0.0), True),
        ((0.3, 0.3, 0.3), True),
        ((0.0, 0.0, 0.0, 1.0), True),
        ((1.0, 0.0, 0.0), False),
        ((0.0, 0.0, 1.0), False),
        ((0.16, 0.192, 0.537), False),
        ((0.0, 1.0, 1.0, 0.0), False),
        (("P1",), False),
    ],
)
def test_drawing_ink_is_black_or_grey(colour: object, ink: bool) -> None:
    assert drawing_ink({"non_stroking_color": colour}) is ink


def test_a_stacked_fraction_in_a_pasted_drawing_is_set_aside() -> None:
    """Outcome: `24 3/4"` set as CAD text inside a snapshot is never `2434"`."""
    reading = read_stamp_text(
        _sheet(b'(24) Tj /F1 8 Tf 13.3 3 Td (3) Tj 0 -6 Td (4) Tj /F1 12 Tf 4.4 3 Td (") Tj'),
        0,
        document_version_id=DOCUMENT,
        dpi=DPI,
    )

    assert not any(item.text.startswith("24") for item in reading.contents.texts)
    assert [label.reason.value for label in reading.contents.set_aside] == ["stacked_fraction"]


#: `24 3/4"` at the client's sizes inside a pasted drawing: a 3-point `24`, a 2-point `3` over a `4`
#: touching it, and the mark. Small enough that `extract_words` keeps the stack in one word.
CLIENT_SIZE_STACK = (
    b'/F1 3 Tf (24) Tj /F1 2 Tf 3.6 1.2 Td (3) Tj 0 -2.2 Td (4) Tj /F1 3 Tf 1.2 1 Td (") Tj'
)


def test_a_stacked_fraction_in_a_pasted_drawing_is_read_whole_and_marked() -> None:
    """Outcome: `24 3/4"`, marked stacked — a reviewer's suggestion, never `2434"`."""
    reading = read_stamp_text(_sheet(CLIENT_SIZE_STACK), 0, document_version_id=DOCUMENT, dpi=DPI)

    assert [(item.text, item.stacked) for item in reading.contents.texts] == [('24 3/4"', True)]
    assert reading.contents.set_aside == ()


def test_millimetres_over_inches_in_a_pasted_drawing_are_one_dual_token() -> None:
    reading = read_stamp_text(
        _sheet(b"/F1 3 Tf (585) Tj 0.4 -3 Td ([23]) Tj"), 0, document_version_id=DOCUMENT, dpi=DPI
    )

    assert [(item.text, item.stacked) for item in reading.contents.texts] == [("585 [23]", False)]
