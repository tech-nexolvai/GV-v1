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
from fractions import Fraction
from uuid import UUID

import pytest

from extraction.stamp_text import StampText, drawing_ink, path_ink, read_stamp_text, stamps_only
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp
from units.normalise import normalise_to_inches
from units.notation import canonical_notation

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


# ---------------------------------------------------------------------------
# Stacks the words came apart from (#880)
# ---------------------------------------------------------------------------


def _drawing(stream: bytes) -> bytes:
    """A sheet whose pasted drawing is exactly `stream`, font text and paths, in the stamp's space."""
    return _pdf(
        annotations=[_stamp(appearance_object=6)],
        extra_objects=[_text_appearance(stream, 7), HELVETICA],
    )


#: The bar of `_split_stack`: a black stroke between its numerator and its denominator.
BAR = b"0.3 w 112.4 521.2 m 114.4 521.2 l S"


def _split_stack(
    top: bytes = b"1",
    bottom: bytes = b"2",
    *,
    whole: bytes = b"2",
    mark: bytes = b'"',
    bar: bytes = BAR,
    text: bytes = b"",
) -> bytes:
    """`whole`, then `top` over the bar over `bottom`, then `mark`, all at 4 points, set so that
    `extract_words` splits the label into two words: the whole number with the numerator (`21`),
    and the denominator with the mark (`2"`). `text` is more text in the same drawing."""
    start = 112.224 - 2.224 * len(whole)  # Helvetica digits are 556/1000 wide: touching the stack
    stream = b"BT /F1 4 Tf 1 0 0 1 %.3f 520 Tm (%s) Tj " % (start, whole)
    stream += b"1 0 0 1 112.3 522 Tm (" + top + b") Tj 1 0 0 1 112.3 518 Tm (" + bottom + b") Tj "
    if mark:
        stream += b"1 0 0 1 114.6 520 Tm (" + mark + b") Tj "
    return stream + text + b"ET " + bar


def _read(stream: bytes) -> StampText:
    return read_stamp_text(_drawing(stream), 0, document_version_id=DOCUMENT, dpi=DPI)


def _reasons(reading: StampText) -> list[str]:
    return [label.reason.value for label in reading.contents.set_aside]


def _exact(text: str) -> Fraction:
    return normalise_to_inches(canonical_notation(text)[0]).exact


def test_a_stack_split_across_two_words_is_put_back_together_and_marked() -> None:
    """**The gap this closes (#880).** Measured on `AI_Set_1`: stacks whose two lines fell into
    different words, such as `152` and `1"` for a sideways `15 1/2"`, each printed over by the
    other, were set aside, and 21 such labels reached a person blank. Outcome: one `2 1/2"`, worth
    exactly five halves, marked as stacked so it is only a reviewer's suggestion (#726); nothing
    left set aside."""
    reading = _read(_split_stack())

    assert [(item.text, item.stacked) for item in reading.contents.texts] == [('2 1/2"', True)]
    assert _exact(reading.contents.texts[0].text) == Fraction(5, 2)
    assert reading.contents.set_aside == ()


def test_a_sideways_split_stack_is_put_back_together_as_it_reads() -> None:
    """The client's split stacks are sideways. Outcome: read up the page, `2 1/2"`, marked stacked."""
    sideways = (
        b"BT /F1 4 Tf 0 1 -1 0 120 520 Tm (2) Tj 0 1 -1 0 118 522.3 Tm (1) Tj "
        b'0 1 -1 0 122 522.3 Tm (2) Tj 0 1 -1 0 120 524.6 Tm (") Tj ET '
        b"0.3 w 118.8 522.4 m 118.8 524.4 l S"
    )
    reading = _read(sideways)

    assert [(item.text, item.stacked) for item in reading.contents.texts] == [('2 1/2"', True)]
    assert reading.contents.texts[0].rotation_degrees == 90
    assert reading.contents.set_aside == ()


def test_without_its_bar_a_split_stack_stays_set_aside() -> None:
    """**The bar is what makes two lines a fraction.** Measured: on `AI_Set_2` page 17, two `1"`
    labels set side by side came back as one word, `1"1"`; on `AI_Set_1` page 3, fifteen pieces of
    notes set a line apart were set aside the same way. Outcome: with no bar drawn, nothing is read
    and both words stay with a reviewer."""
    reading = _read(_split_stack(bar=b""))

    assert reading.contents.texts == ()
    assert _reasons(reading) == ["stacked_fraction", "stacked_fraction"]


@pytest.mark.parametrize(
    ("top", "bottom"),
    [(b"1", b"3"), (b"5", b"4"), (b"4", b"4")],
    ids=["not-an-inch-fraction", "numerator-above", "numerator-equal"],
)
def test_a_split_stack_that_is_not_an_inch_fraction_gives_no_row(top: bytes, bottom: bytes) -> None:
    """Outcome: `1/3`, `5/4` and `4/4` are not composed — the rule `extraction/fraction_parts.py`
    holds a reading put together from its pieces to — and stay with a reviewer."""
    reading = _read(_split_stack(top, bottom))

    assert reading.contents.texts == ()
    assert _reasons(reading) == ["stacked_fraction", "stacked_fraction"]


def test_a_stack_without_its_inch_mark_gives_no_row() -> None:
    """A bare `3/4` says nothing of its unit. Outcome: a `24` and a `3` over a `4` with their bar,
    all in one word, and no mark: nothing read, the word left with a reviewer."""
    reading = _read(
        b"BT /F1 3 Tf 1 0 0 1 110 520 Tm (24) Tj /F1 2 Tf 1 0 0 1 113.6 521.2 Tm (3) Tj "
        b"1 0 0 1 113.6 519 Tm (4) Tj ET 0.3 w 113.65 520.7 m 114.65 520.7 l S"
    )

    assert reading.contents.texts == ()
    assert _reasons(reading) == ["stacked_fraction"]


def test_a_split_stack_with_a_leading_zero_gives_no_row() -> None:
    """Outcome: `02 1/2"` is not read; no label is written with a leading zero (#848's rule)."""
    reading = _read(_split_stack(whole=b"02"))

    assert not any(item.stacked for item in reading.contents.texts)
    assert "stacked_fraction" in _reasons(reading)


def test_a_stroke_under_the_denominator_is_not_its_bar() -> None:
    """Outcome: a short stroke drawn below the middle of the denominator, as a tick or the foot of a
    line is, does not make the two lines a fraction."""
    reading = _read(_split_stack(bar=b"0.3 w 112.4 518 m 114.4 518 l S"))

    assert reading.contents.texts == ()
    assert _reasons(reading) == ["stacked_fraction", "stacked_fraction"]


def test_a_label_printed_over_a_split_stack_stops_it_being_composed() -> None:
    """**Overprints are still refused.** Measured on `AI_Set_1`: a sideways `4 3/4"` drawn through a
    tilted `1 3/8"`. Outcome: a sideways `7` printed over the stack leaves it unread."""
    reading = _read(_split_stack(text=b"0 1 -1 0 112.5 517 Tm (7) Tj "))

    assert not any(item.stacked for item in reading.contents.texts)
    assert _reasons(reading) == ["stacked_fraction", "stacked_fraction"]


def test_a_label_touching_a_split_stack_stops_it_being_composed() -> None:
    """Measured on `AI_Set_1`: a sideways `1/8"` with a `4"` right after its mark, in one word.
    Outcome: a character touching the label that is no part of it may be a piece the label is
    missing, so the stack is not composed."""
    reading = _read(_split_stack(text=b'1 0 0 1 116.2 520 Tm (4") Tj '))

    assert not any(item.stacked for item in reading.contents.texts)
    assert "stacked_fraction" in _reasons(reading)


def test_a_split_stack_with_more_label_before_its_whole_number_gives_no_row() -> None:
    """Outcome: `3-2 1/2"` is not read as `2 1/2"`: the `-` touching the whole number may be the
    end of a feet part, so the label is left to a reviewer."""
    stream = (
        b"BT /F1 4 Tf 1 0 0 1 104.6 520 Tm (3) Tj 1 0 0 1 106.9 520 Tm (-) Tj "
        b"1 0 0 1 108.3 520 Tm (2) Tj 1 0 0 1 112.3 522 Tm (1) Tj 1 0 0 1 112.3 518 Tm (2) Tj "
        b'1 0 0 1 114.6 520 Tm (") Tj ET ' + BAR
    )
    reading = _read(stream)

    assert reading.contents.texts == ()
    assert _reasons(reading) == ["stacked_fraction", "stacked_fraction"]


def test_a_stack_with_a_piece_in_a_word_that_was_read_gives_no_row() -> None:
    """Outcome: a `3` over a `16"`, where `extract_words` left the `3` a word of its own and read
    it, is not composed from the `16"` alone; the `3` stays the bare number it was."""
    stream = (
        b"BT /F1 4 Tf 1 0 0 1 110 522 Tm (3) Tj 1 0 0 1 110 518 Tm (16) Tj "
        b'1 0 0 1 114.6 520 Tm (") Tj ET 0.3 w 110.1 521.2 m 114.3 521.2 l S'
    )
    reading = _read(stream)

    assert [(item.text, item.stacked) for item in reading.contents.texts] == [("3", False)]
    assert _reasons(reading) == ["stacked_fraction"]


def test_a_bar_drawn_twice_gives_one_reading() -> None:
    """A CAD program draws a stroke twice to embolden it (`extraction/glyph_bands.py`). Outcome: the
    second copy finds the same label, whose characters are already taken, and adds nothing."""
    reading = _read(_split_stack(bar=BAR + b" " + BAR))

    assert [(item.text, item.stacked) for item in reading.contents.texts] == [('2 1/2"', True)]


@pytest.mark.parametrize(
    "bar",
    [b"1 0 0 RG " + BAR, b"0.3 w 112.4 521.2 m 130 521.2 l S"],
    ids=["coloured", "runs-on-past-the-stack"],
)
def test_a_stroke_that_is_not_the_vendors_bar_composes_nothing(bar: bytes) -> None:
    """Outcome: a red stroke — a reviewer's, baked into the snapshot — and a line running on past the
    stack are not its bar, so the stack stays set aside."""
    reading = _read(_split_stack(bar=bar))

    assert reading.contents.texts == ()
    assert _reasons(reading) == ["stacked_fraction", "stacked_fraction"]


def test_what_is_left_of_a_word_once_its_label_is_composed_stays_set_aside() -> None:
    """Measured on `AI_Set_1`: a stack's word also held the mark of the `4"` beside it. Outcome: the
    label is read, and the character that is no part of it is not, nor is it thrown away: it stays
    set aside, so a reviewer is still sent to it."""
    reading = _read(_split_stack(text=b"1 0 0 1 105.6 520 Tm (x) Tj "))

    assert [(item.text, item.stacked) for item in reading.contents.texts] == [('2 1/2"', True)]
    assert _reasons(reading) == ["stacked_fraction"]
    (left,) = reading.contents.set_aside
    (label,) = reading.contents.texts
    assert max(point.x for point in left.extent.points) < min(
        point.x for point in label.extent.points
    )


@pytest.mark.parametrize(
    ("path", "ink"),
    [
        ({"stroke": True, "fill": False, "stroking_color": (0, 0, 0)}, True),
        ({"stroke": True, "fill": False, "stroking_color": None}, True),
        ({"stroke": False, "fill": True, "non_stroking_color": (0.4,)}, True),
        ({"stroke": True, "fill": False, "stroking_color": (1, 0, 0)}, False),
        (
            {"stroke": True, "fill": True, "stroking_color": (0,), "non_stroking_color": (1, 1, 0)},
            False,
        ),
        ({"stroke": False, "fill": False}, False),
    ],
)
def test_path_ink_counts_the_colours_a_path_shows(path: dict[str, object], ink: bool) -> None:
    assert path_ink(path) is ink
