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

from extraction.stamp_text import read_stamp_text, stamps_only
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
    assert reading.readable_characters == 0


def test_the_stamps_line_work_is_not_read_twice() -> None:
    """Outcome: no segments — the stamp's paths are read by `annotations.py`, once."""
    reading = read_stamp_text(_sheet(), 0, document_version_id=DOCUMENT, dpi=DPI)

    assert reading.contents.segments == ()


def test_the_file_passed_in_is_never_changed() -> None:
    data = _sheet(with_markup=True)
    before = bytes(data)

    stamps_only(data, 0)

    assert data == before
