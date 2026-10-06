"""Whose ink a label is (#979).

Verification for: `extraction/ink.py`.

The failure modes first: the vendor's number under the reviewer's yellow box is "covered", the
reviewer's red number is the reviewer's, and a place touched by a red line or a pasted stamp is
the reviewer's too. Then that the plain case — black text on its own — is the vendor's, and that a
coloured mark elsewhere on the page changes nothing about it.

Every page here is built by hand, in the shape of the client's files (the drawing pasted in as a
`/Stamp`); no client drawing is read.
"""

from __future__ import annotations

import pytest

from extraction.ink import InkAt, InkClass, InkLabel, PageInk, read_page_ink
from extraction.reader import UnreadablePdf
from extraction.stamp_text import ColouredPath
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp
from tests.extraction.test_stamp_text import HELVETICA, _text_appearance

DPI = 150

#: The vendor's number, set in black at the default fill.
BLACK_36 = b"BT /F1 12 Tf 110 520 Td (36) Tj ET\n"
#: The reviewer's number, set in red, a little to the right.
RED_38 = b"1 0 0 rg BT /F1 12 Tf 200 520 Td (38) Tj ET\n"
#: The reviewer's yellow box, painted over the vendor's number.
YELLOW_OVER_36 = b"1 1 0 rg 105 515 30 14 re f\n"
#: The same yellow box, painted well away from it.
YELLOW_ELSEWHERE = b"1 1 0 rg 300 600 30 14 re f\n"
#: A red line running through the vendor's number; and the same line in black.
RED_LINE_THROUGH_36 = b"1 0 0 RG 1 w 100 525 m 300 525 l S\n"
BLACK_LINE_THROUGH_36 = b"0 G 1 w 100 525 m 300 525 l S\n"


#: A small stamp's picture: a blue square, filled; and a square outlined in black.
BLUE_FILLED_STAMP = b"0 0 1 rg 0 0 20 20 re f"
BLACK_OUTLINE_STAMP = b"0 G 1 w 1 1 18 18 re S"


def _sheet(
    stream: bytes, *, pasted: bytes | None = None, picture: bytes = BLUE_FILLED_STAMP
) -> bytes:
    """A page whose one pasted drawing draws `stream`, with font `/F1` to hand; `pasted` is the
    rectangle of a small stamp pasted onto the drawing, if one is, and `picture` what it draws."""
    if pasted is None:
        return _pdf(
            annotations=[_stamp(appearance_object=6)],
            extra_objects=[_text_appearance(stream, 7), HELVETICA],
        )
    return _pdf(
        annotations=[_stamp(appearance_object=7), _stamp(rect=pasted, appearance_object=9)],
        extra_objects=[
            _text_appearance(stream, 8),
            HELVETICA,
            _appearance(picture, bbox=b"[0 0 20 20]", matrix=b"[1 0 0 1 0 0]"),
        ],
    )


def _label(ink: PageInk, text: str) -> InkLabel:
    found = [label for label in ink.labels if label.text == text]
    assert len(found) == 1, [label.text for label in ink.labels]
    return found[0]


def test_black_text_on_its_own_is_the_vendors_ink() -> None:
    ink = read_page_ink(_sheet(BLACK_36), 0, dpi=DPI)
    assert [(label.text, label.ink) for label in ink.labels] == [("36", InkClass.VENDOR)]
    assert ink.at(_label(ink, "36").box) == InkAt(InkClass.VENDOR, "")
    assert ink.paths == () and ink.stamps == ()


def test_the_vendors_number_under_the_reviewers_yellow_box_is_covered() -> None:
    ink = read_page_ink(_sheet(YELLOW_OVER_36 + b"0 g " + BLACK_36), 0, dpi=DPI)
    label = _label(ink, "36")
    assert label.ink is InkClass.COVERED
    assert ink.at(label.box) == InkAt(InkClass.COVERED, "")


def test_the_reviewers_red_number_over_the_yellow_box_is_kept_as_what_the_reviewer_wrote() -> None:
    red_over_36 = b"1 0 0 rg BT /F1 12 Tf 110 520 Td (38) Tj ET\n"
    ink = read_page_ink(_sheet(YELLOW_OVER_36 + b"0 g " + BLACK_36 + red_over_36), 0, dpi=DPI)
    assert _label(ink, "36").ink is InkClass.COVERED
    assert _label(ink, "38").ink is InkClass.GV
    assert ink.at(_label(ink, "36").box) == InkAt(InkClass.COVERED, "38")


@pytest.mark.parametrize(
    "colour",
    [b"1 0 0 rg", b"0 0 1 rg", b"0 0.6 0 rg", b"0 1 1 0 k", b"1 0.5 0 rg"],
    ids=["red", "blue", "green", "cmyk-red", "orange"],
)
def test_text_set_in_any_colour_is_the_reviewers_ink(colour: bytes) -> None:
    coloured = colour + b" BT /F1 12 Tf 200 520 Td (38) Tj ET\n"
    ink = read_page_ink(_sheet(BLACK_36 + coloured), 0, dpi=DPI)
    assert _label(ink, "36").ink is InkClass.VENDOR
    assert _label(ink, "38").ink is InkClass.GV
    assert ink.at(_label(ink, "38").box) == InkAt(InkClass.GV, "38")
    assert ink.at(_label(ink, "36").box) == InkAt(InkClass.VENDOR, "")


def test_grey_text_is_the_vendors_ink() -> None:
    ink = read_page_ink(_sheet(b"0.5 g " + BLACK_36), 0, dpi=DPI)
    assert _label(ink, "36").ink is InkClass.VENDOR


def test_a_yellow_box_elsewhere_on_the_page_changes_nothing_about_the_label() -> None:
    ink = read_page_ink(_sheet(YELLOW_ELSEWHERE + b"0 g " + BLACK_36), 0, dpi=DPI)
    label = _label(ink, "36")
    assert label.ink is InkClass.VENDOR
    assert ink.at(label.box) == InkAt(InkClass.VENDOR, "")
    assert len(ink.paths) == 1


def test_a_red_line_through_the_label_marks_the_place_the_reviewers_but_covers_nothing() -> None:
    ink = read_page_ink(_sheet(RED_LINE_THROUGH_36 + BLACK_36), 0, dpi=DPI)
    label = _label(ink, "36")
    assert label.ink is InkClass.VENDOR  # a stroke hides no ink; it is only a mark on the place
    assert ink.at(label.box) == InkAt(InkClass.GV, "")


def test_a_black_line_through_the_label_is_the_drawings_own() -> None:
    ink = read_page_ink(_sheet(BLACK_LINE_THROUGH_36 + BLACK_36), 0, dpi=DPI)
    assert ink.at(_label(ink, "36").box) == InkAt(InkClass.VENDOR, "")
    assert ink.paths == ()


#: The drawing's rectangle is [50 50 350 250]; the label sits near its lower left corner, at about
#: (60, 70) in page points, so a 20-point stamp pasted there lies inside the drawing.
STAMP_ON_THE_LABEL = b"[55 60 75 80]"


def test_a_filled_stamp_pasted_over_the_label_covers_it() -> None:
    ink = read_page_ink(_sheet(BLACK_36, pasted=STAMP_ON_THE_LABEL), 0, dpi=DPI)
    assert len(ink.stamps) == 1
    assert ink.at(_label(ink, "36").box).ink is InkClass.COVERED


def test_a_stamp_pasted_onto_the_label_marks_the_place_the_reviewers_whatever_it_draws() -> None:
    ink = read_page_ink(
        _sheet(BLACK_36, pasted=STAMP_ON_THE_LABEL, picture=BLACK_OUTLINE_STAMP), 0, dpi=DPI
    )
    label = _label(ink, "36")
    assert label.ink is InkClass.VENDOR  # drawn in black, the stamp hides no ink by colour
    assert ink.paths == () and len(ink.stamps) == 1
    assert ink.at(label.box) == InkAt(InkClass.GV, "")


def test_a_box_well_away_from_every_mark_is_the_vendors() -> None:
    ink = read_page_ink(_sheet(RED_38 + YELLOW_ELSEWHERE + BLACK_36), 0, dpi=DPI)
    assert ink.at((0, 0, 5, 5)) == InkAt(InkClass.VENDOR, "")


def test_a_page_without_pasted_drawings_has_no_labels_and_no_marks() -> None:
    ink = read_page_ink(_pdf(annotations=[_free_text()]), 0, dpi=DPI)
    assert ink == PageInk(dpi=DPI, labels=(), paths=(), stamps=())
    assert ink.at((0, 0, 100, 100)) == InkAt(InkClass.VENDOR, "")


def test_a_page_beyond_the_document_is_refused() -> None:
    with pytest.raises(UnreadablePdf):
        read_page_ink(_sheet(BLACK_36), 3, dpi=DPI)


def test_a_box_turned_inside_out_is_refused() -> None:
    ink = read_page_ink(_sheet(BLACK_36), 0, dpi=DPI)
    with pytest.raises(ValueError):
        ink.at((10, 10, 5, 20))


def test_the_dpi_is_the_one_the_boxes_are_in() -> None:
    at_150 = _label(read_page_ink(_sheet(BLACK_36), 0, dpi=150), "36").box
    at_300 = _label(read_page_ink(_sheet(BLACK_36), 0, dpi=300), "36").box
    assert all(abs(2 * a - b) <= 2 for a, b in zip(at_150, at_300, strict=True))


def test_covers_is_the_fills_own_rule_and_a_stroke_covers_nothing() -> None:
    square = ((0, 0), (10, 0), (10, 10), (0, 10))
    filled = ColouredPath(lines=(), areas=(), rings=(square,), even_odd=False)
    assert filled.covers((5, 5))
    assert not filled.covers((15, 5))
    stroked = ColouredPath(lines=((0, 0, 10, 0),), areas=(), rings=(), even_odd=False)
    assert not stroked.covers((5, 0))
    hole = ((3, 3), (7, 3), (7, 7), (3, 7))
    even_odd = ColouredPath(lines=(), areas=(), rings=(square, hole), even_odd=True)
    assert not even_odd.covers((5, 5))
    assert even_odd.covers((1, 1))
