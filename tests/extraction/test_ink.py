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


# --- #1198: GV's green tick beside a vendor number --------------------------------------------
#
# On second-round sheets GV puts a small green check glyph next to each vendor number it accepts.
# It is still GV's ink, so the number stays held (the markup is the answer key, never a reading
# source); code only names it, so the reason can say "GV ticked this" plainly. Any other GV ink
# at the same place keeps the general wording.

#: A green check glyph drawn as one filled outline of six corners (two strokes meeting at the
#: bottom, the right one longer), just to the right of the vendor's `36`; drawn from scratch.
GREEN_TICK_BESIDE_36 = (
    b"0 0.5 0.25 rg 125 524 m 126.2 525.3 l 128.9 522.5 l 134.6 528.2 l 135.8 527 l"
    b" 128.9 520.1 l h f\n"
)
#: The same outline in red: a red mark is not a tick, whatever its shape.
RED_TICK_BESIDE_36 = GREEN_TICK_BESIDE_36.replace(b"0 0.5 0.25 rg", b"1 0 0 rg")
#: The same outline, stroked in green rather than filled: a line drawing, not GV's glyph.
GREEN_STROKED_TICK = GREEN_TICK_BESIDE_36.replace(b"h f\n", b"h S\n").replace(b" rg", b" RG")
#: A green filled box of the same size: green, but not a tick.
GREEN_BOX_BESIDE_36 = b"0 0.5 0.25 rg 125 520 10 8 re f\n"
#: The tick mirrored (long stroke on the left): not the check glyph.
MIRRORED_TICK = (
    b"0 0.5 0.25 rg 135.8 524 m 134.6 525.3 l 131.9 522.5 l 126.2 528.2 l 125 527 l"
    b" 131.9 520.1 l h f\n"
)


def _crop(ink: PageInk) -> tuple[int, int, int, int]:
    box = _label(ink, "36").box
    return (box[0] - 20, box[1] - 10, box[2] + 40, box[3] + 10)


def test_a_green_tick_beside_the_number_is_still_gv_ink_and_is_named_a_tick() -> None:
    ink = read_page_ink(_sheet(BLACK_36 + GREEN_TICK_BESIDE_36), 0, dpi=DPI)
    assert ink.at(_crop(ink)) == InkAt(InkClass.GV, "", tick_only=True)
    # The label's own box does not reach the tick: nothing there is GV's.
    assert ink.at(_label(ink, "36").box) == InkAt(InkClass.VENDOR, "")


@pytest.mark.parametrize(
    "mark",
    [RED_TICK_BESIDE_36, GREEN_STROKED_TICK, GREEN_BOX_BESIDE_36, MIRRORED_TICK],
    ids=["red", "stroked", "box", "mirrored"],
)
def test_other_marks_beside_the_number_are_never_called_a_tick(mark: bytes) -> None:
    ink = read_page_ink(_sheet(BLACK_36 + mark), 0, dpi=DPI)
    found = ink.at(_crop(ink))
    assert found.ink is InkClass.GV
    assert found.tick_only is False


@pytest.mark.parametrize(
    "other",
    [RED_LINE_THROUGH_36, RED_38.replace(b"200 520", b"140 520"), YELLOW_OVER_36],
    ids=["red line", "red number", "yellow box"],
)
def test_a_tick_with_any_other_gv_ink_at_the_same_place_is_not_tick_only(other: bytes) -> None:
    ink = read_page_ink(_sheet(BLACK_36 + other + GREEN_TICK_BESIDE_36), 0, dpi=DPI)
    found = ink.at(_crop(ink))
    assert found.ink is not InkClass.VENDOR
    assert found.tick_only is False


def test_a_tick_elsewhere_on_the_page_changes_nothing_about_the_label() -> None:
    far = (
        b"0 0.5 0.25 rg 325 624 m 326.2 625.3 l 328.9 622.5 l 334.6 628.2 l 335.8 627 l"
        b" 328.9 620.1 l h f\n"
    )
    ink = read_page_ink(_sheet(BLACK_36 + far), 0, dpi=DPI)
    assert ink.at(_crop(ink)) == InkAt(InkClass.VENDOR, "")


# --- #1335: the two guards that keep other GV ink from being worded as a tick -----------------

#: The vendor's black `88` to the right of the tick, under a small yellow fill that covers its
#: middle only: a covered word whose fill lies outside the crop the test asks about.
BLACK_88_UNDER_A_SMALL_FILL = b"1 1 0 rg 155 522 4 4 re f\n0 g BT /F1 12 Tf 150 520 Td (88) Tj ET\n"


def test_a_tick_with_a_pasted_stamp_at_the_crop_keeps_the_general_words() -> None:
    """A stamp pasted onto the label is GV's ink that is not a tick, even when it draws in black
    and adds no coloured path: the tick must not speak for it."""
    ink = read_page_ink(
        _sheet(
            BLACK_36 + GREEN_TICK_BESIDE_36,
            pasted=STAMP_ON_THE_LABEL,
            picture=BLACK_OUTLINE_STAMP,
        ),
        0,
        dpi=DPI,
    )
    crop = _crop(ink)
    # The tick is the only coloured path there; the stamp alone is what is not a tick.
    assert [path.tick for path in ink.paths if path.meets(crop)] == [True]
    assert any(path.tick for path in ink.paths)
    assert [stamp for stamp in ink.stamps if _meets(stamp, crop)] == list(ink.stamps)
    found = ink.at(crop, label=_label(ink, "36").box)
    assert found.ink is InkClass.GV
    assert found.tick_only is False and found.tick_beside is False


def test_a_tick_with_a_word_under_a_coloured_fill_keeps_the_general_words() -> None:
    """The crop reaches a vendor word GV covered with a fill, though the fill itself lies outside
    the crop: the covered word is GV's ink that is not a tick."""
    ink = read_page_ink(
        _sheet(BLACK_36 + GREEN_TICK_BESIDE_36 + BLACK_88_UNDER_A_SMALL_FILL), 0, dpi=DPI
    )
    covered = _label(ink, "88")
    assert covered.ink is InkClass.COVERED
    left, top, _, bottom = _crop(ink)
    crop = (left, top, covered.box[0], bottom)  # reaches the covered word's left edge only
    assert [path.tick for path in ink.paths if path.meets(crop)] == [True]
    assert ink.stamps == ()
    found = ink.at(crop, label=_label(ink, "36").box)
    assert found.ink is InkClass.COVERED
    assert found.tick_only is False and found.tick_beside is False


def _meets(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


# --- #1335: a tick in the crop's margin only is "beside" the number --------------------------

#: The same tick, moved left until it overlaps the right of the `6`: on the number's own box.
GREEN_TICK_ON_36 = (
    b"0 0.5 0.25 rg 121 524 m 122.2 525.3 l 124.9 522.5 l 130.6 528.2 l 131.8 527 l"
    b" 124.9 520.1 l h f\n"
)


def test_a_tick_that_touches_only_the_crops_margin_is_beside_the_number() -> None:
    ink = read_page_ink(_sheet(BLACK_36 + GREEN_TICK_BESIDE_36), 0, dpi=DPI)
    label = _label(ink, "36").box
    assert not any(path.meets(label) for path in ink.paths)
    assert ink.at(_crop(ink), label=label) == InkAt(
        InkClass.GV, "", tick_only=True, tick_beside=True
    )


def test_a_tick_on_the_numbers_own_box_is_not_beside_it() -> None:
    ink = read_page_ink(_sheet(BLACK_36 + GREEN_TICK_ON_36), 0, dpi=DPI)
    label = _label(ink, "36").box
    assert [path.tick for path in ink.paths if path.meets(label)] == [True]
    found = ink.at(_crop(ink), label=label)
    assert found.tick_only is True and found.tick_beside is False


def test_without_the_labels_own_box_the_place_asked_is_the_label() -> None:
    """A caller that asks about the label's own place (the depth reader) passes no label box: a
    tick there touches it, so it is never called beside."""
    ink = read_page_ink(_sheet(BLACK_36 + GREEN_TICK_BESIDE_36), 0, dpi=DPI)
    assert ink.at(_crop(ink)) == InkAt(InkClass.GV, "", tick_only=True, tick_beside=False)


def test_beside_is_only_ever_said_of_a_tick_and_nothing_else() -> None:
    ink = read_page_ink(_sheet(BLACK_36 + RED_LINE_THROUGH_36 + GREEN_TICK_BESIDE_36), 0, dpi=DPI)
    found = ink.at(_crop(ink), label=(0, 0, 1, 1))
    assert found.ink is InkClass.GV
    assert found.tick_only is False and found.tick_beside is False


def test_a_label_box_turned_inside_out_is_refused() -> None:
    ink = read_page_ink(_sheet(BLACK_36 + GREEN_TICK_BESIDE_36), 0, dpi=DPI)
    with pytest.raises(ValueError):
        ink.at(_crop(ink), label=(10, 10, 5, 20))
