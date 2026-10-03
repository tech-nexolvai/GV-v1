"""Reading a stacked fraction piece by piece, and putting its value together in code (#848).

Each label here is a real stamp read by the real reader: the synthetic `28 3/4"` of
`tests/extraction/test_annotations.py`, whose digits are plotter-shaped strokes and not real
numerals. So the reader is a double that answers each piece it is shown in turn — what is tested is
what is drawn for it, what is accepted from it, and what is put together, never how well an engine
reads. How RapidOCR reads the client's own labels is measured on #848, locally.

Source: issue #848. Verification for: `extraction/fraction_parts.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest

from evidence.coordinates import ImagePoint
from extraction import fraction_parts
from extraction.annotations import StackedFraction
from extraction.fraction_parts import (
    FractionPartsReading,
    FractionPartsRefusal,
    PieceDrawing,
    read_fraction_parts,
)
from extraction.ocr import OcrItem
from tests.extraction.test_annotations import FRACTION_BAR, STACKED_APPEARANCE, _stacked_layers
from units.measurement import Measurement, Unit

DRAWING = PieceDrawing(height_px=40, stroke_px=4, bezier_steps=8, margin_px=32)

#: The synthetic `28 3/4"`'s strokes, one per line of `STACKED_APPEARANCE`, so a test can leave
#: some out or put something beside them.
TWO, EIGHT, THREE, BAR, FOUR_BODY, FOUR_STEM, TICK, OTHER_TICK = STACKED_APPEARANCE.splitlines(True)

#: The same `3/4"` with no whole number in front of it.
BARE = THREE + BAR + FOUR_BODY + FOUR_STEM + TICK + OTHER_TICK

#: A `+` just after the inch mark, centred on the bar: more label than the layout counts.
PLUS = b"129 525 m 131 525 l S\n130 523 m 130 527 l S\n"

#: Two more digits before the `28`, each within the 4 pt character gap of the next.
TWO_MORE_DIGITS = (
    b"101 520 m 104.6 525.5 l 101 525.5 l S\n" b"105.5 520 m 109.1 525.5 l 105.5 525.5 l S\n"
)

#: The `3` drawn in a reviewer's red: a fraction with no numerator in the vendor's ink.
RED_THREE = b"q 1 0 0 RG " + THREE.rstrip(b"\n") + b" Q\n"


def _fraction(appearance: bytes = STACKED_APPEARANCE) -> StackedFraction:
    (fraction,) = _stacked_layers(FRACTION_BAR, appearance=appearance).stacked_fractions
    assert fraction.layout is not None
    return fraction


@dataclass
class _Pieces:
    """Answers the pieces it is shown in turn, and keeps every picture it was shown."""

    answers: list[tuple[str, ...]]
    shown: list[np.ndarray] = field(default_factory=list)
    name: str = "pieces"
    version: str = "test/1"

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        self.shown.append(np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3))
        corners = (ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1), ImagePoint(0, 1))
        return tuple(
            OcrItem(text=text, confidence=Decimal("0.9"), image_extent=corners)
            for text in self.answers[len(self.shown) - 1]
        )


def _read(
    answers: list[tuple[str, ...]], fraction: StackedFraction | None = None
) -> tuple[FractionPartsReading | FractionPartsRefusal, _Pieces]:
    reader = _Pieces(answers)
    return (
        read_fraction_parts(fraction or _fraction(), engine=reader, drawing=DRAWING),
        reader,
    )


def test_a_label_is_read_piece_by_piece_and_its_value_put_together_exactly() -> None:
    """**The whole number, the numerator and the denominator, each alone, in that order.** The value
    is put together here, as a `Fraction` of an inch; the reader only ever said three numbers."""
    fraction = _fraction()
    result, reader = _read([("28",), ("3",), ("4",)], fraction)

    assert isinstance(result, FractionPartsReading)
    assert result.text == '28 3/4"'
    assert result.value == Measurement(exact=Fraction(115, 4), unit=Unit.INCH, raw_text='28 3/4"')
    assert len(reader.shown) == 3
    layout = fraction.layout
    assert layout is not None
    assert result.box[0] == layout.whole[0][0][0], "the label starts at its whole number"
    assert result.box[2] == max(box[2] for box in layout.inch_mark), "and ends at its inch mark"


def test_a_bare_fraction_has_no_whole_number_to_read() -> None:
    result, reader = _read([("3",), ("4",)], _fraction(BARE))

    assert isinstance(result, FractionPartsReading)
    assert result.text == '3/4"'
    assert result.value.exact == Fraction(3, 4)
    assert len(reader.shown) == 2


def test_each_piece_is_drawn_alone_black_on_white_at_the_stated_height() -> None:
    """**What the reader is shown is one number and nothing else.** Pure black on white, with the
    stated margin of white all round, and drawn so tall that every piece has one character height
    whatever its number of digits."""
    _result, reader = _read([("28",), ("3",), ("4",)])

    for picture in reader.shown:
        assert set(np.unique(picture)) <= {0, 255}, "nothing grey: drawn, not rendered"
        margin = DRAWING.margin_px
        assert (picture[:margin] == 255).all() and (picture[-margin:] == 255).all()
        assert (picture[:, :margin] == 255).all() and (picture[:, -margin:] == 255).all()
        rows = np.flatnonzero((picture == 0).any(axis=(1, 2)))
        ink_height = rows[-1] - rows[0] + 1
        # The line through the points spans `height_px` to just under one pixel more, each end
        # rounded to a pixel, and the pen adds its width across it.
        assert DRAWING.height_px <= ink_height <= DRAWING.height_px + 2 + DRAWING.stroke_px
    whole, numerator, _denominator = reader.shown
    assert whole.shape[1] > numerator.shape[1], "two digits drawn wider than one, not smaller"


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        # A piece that does not read.
        ([()], fraction_parts.NOT_A_NUMBER),
        # Read as two things, or as something other than digits.
        ([("2", "8")], fraction_parts.NOT_A_NUMBER),
        ([("Z8",)], fraction_parts.NOT_A_NUMBER),
        ([("２８",)], fraction_parts.NOT_A_NUMBER),
        ([("2 8",)], fraction_parts.NOT_A_NUMBER),
        ([("1028",)], fraction_parts.NOT_A_NUMBER),
        # Digits, but not as many as the drawing has.
        ([("8",)], fraction_parts.WRONG_COUNT),
        ([("28",), ("33",)], fraction_parts.WRONG_COUNT),
        # A leading zero no label is written with.
        ([("08",)], fraction_parts.LEADING_ZERO),
        # The numerator at or above the denominator, or zero.
        ([("28",), ("4",), ("4",)], fraction_parts.NOT_BELOW),
        ([("28",), ("5",), ("4",)], fraction_parts.NOT_BELOW),
        ([("28",), ("0",), ("4",)], fraction_parts.NOT_BELOW),
        # A denominator that is not a fraction of an inch.
        ([("28",), ("3",), ("5",)], fraction_parts.NOT_AN_INCH_FRACTION),
        ([("28",), ("3",), ("6",)], fraction_parts.NOT_AN_INCH_FRACTION),
    ],
)
def test_a_piece_the_drawing_does_not_bear_out_is_no_reading_at_all(
    answers: list[tuple[str, ...]], reason: str
) -> None:
    """**No reading, never a reading with a doubt attached.** The reason is this module's own
    sentence, so a page result that counts it repeats nothing a reader returned."""
    result, reader = _read(answers)

    assert result == FractionPartsRefusal(reason)
    assert len(reader.shown) == len(answers), "the first piece that fails ends it"


@pytest.mark.parametrize(
    ("appearance", "reason"),
    [
        (
            STACKED_APPEARANCE.replace(TICK, b"").replace(OTHER_TICK, b""),
            fraction_parts.NO_INCH_MARK,
        ),
        (STACKED_APPEARANCE + PLUS, fraction_parts.NEIGHBOUR),
        (TWO_MORE_DIGITS + STACKED_APPEARANCE, fraction_parts.TOO_LONG),
        (STACKED_APPEARANCE.replace(THREE, RED_THREE), fraction_parts.NO_STACK),
    ],
)
def test_what_the_drawing_rules_out_is_refused_before_anything_is_read(
    appearance: bytes, reason: str
) -> None:
    """A label with no inch mark, with a character beside it that it does not count, with a piece
    no reading could match, or with no numerator in the vendor's ink, costs no reading at all."""
    result, reader = _read([("28",), ("3",), ("4",)], _fraction(appearance))

    assert result == FractionPartsRefusal(reason)
    assert reader.shown == []


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_a_label_turned_on_the_page_goes_to_a_person_unread(degrees: int) -> None:
    """**A sideways stacked fraction always goes to a person** — the admin's decision on the plan for
    #756. Its pieces would be drawn as the page turns them, so it is not read at all."""
    fraction = _fraction()
    assert fraction.layout is not None
    turned = replace(fraction, layout=replace(fraction.layout, rotation_degrees=degrees))

    result, reader = _read([("28",), ("3",), ("4",)], turned)

    assert result == FractionPartsRefusal(fraction_parts.TURNED)
    assert reader.shown == []


def test_a_fraction_set_in_text_has_nothing_to_draw() -> None:
    fraction = replace(_fraction(), layout=None, paths=())

    result, reader = _read([], fraction)

    assert result == FractionPartsRefusal(fraction_parts.SET_IN_TEXT)
    assert reader.shown == []


def test_a_piece_whose_paths_were_not_kept_is_not_drawn_from_anything_else() -> None:
    fraction = _fraction()
    without_the_eight = replace(fraction, paths=fraction.paths[:1] + fraction.paths[2:])

    result, reader = _read([("28",), ("3",), ("4",)], without_the_eight)

    assert result == FractionPartsRefusal(fraction_parts.PATHS_MISSING)
    assert reader.shown == []


def test_a_path_the_file_does_not_say_how_to_draw_is_not_drawn_approximately() -> None:
    fraction = _fraction()
    unknown = replace(fraction, paths=tuple(replace(path, stroked=None) for path in fraction.paths))

    result, reader = _read([("28",), ("3",), ("4",)], unknown)

    assert result == FractionPartsRefusal(fraction_parts.UNDRAWABLE)
    assert reader.shown == []


@pytest.mark.parametrize("name", ["height_px", "stroke_px", "bezier_steps", "margin_px"])
@pytest.mark.parametrize("value", [0, -1, True, Decimal(4), "4"])
def test_every_drawing_setting_is_a_positive_whole_number(name: str, value: object) -> None:
    """Stated by a deployment and never defaulted, so a typo is refused rather than drawn with."""
    stated: dict[str, object] = {
        "height_px": 40,
        "stroke_px": 4,
        "bezier_steps": 8,
        "margin_px": 32,
        name: value,
    }
    with pytest.raises(ValueError, match=name):
        PieceDrawing(**stated)  # type: ignore[arg-type]


def test_every_drawing_setting_is_in_the_run_identity() -> None:
    assert DRAWING.config_hash == "height_px=40;stroke_px=4;bezier_steps=8;margin_px=32"
