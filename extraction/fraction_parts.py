"""Reading a stacked fraction one piece at a time, and putting its value together in code (#848).

**The gap this closes.** The vendor's stacked fractions are found and laid out
(`extraction/glyph_bands.py`, #834), and then nothing read them: a crop that shows one is never
sent to a vision reader (#762), so on `AI_Set_2` page 2 the four stacked labels of one top reached
the form blank. Read whole, a stacked label is where readers go wrong: local OCR got all four of
those wrong, and two vision readers of different vendors once agreed on `3 3/4"` for a `3/4"`
(#726). Read one piece at a time — the whole number, the numerator, the denominator, each a plain
number on its own — local OCR read all twelve pieces of those four labels right (the plan on #756).

**Each piece is drawn from its own paths**, the black and grey ones the layout counted
(`StackedFraction.paths`), and from nothing else: no neighbouring line-work, no reviewer's markup,
no other piece. So what the reader is shown is one number, drawn as the file draws it
(`extraction.glyph_shapes.rasterise`), and it is asked nothing else.

**The readers read digits; code decides what they mean.** A piece is accepted only as one to three
digits, exactly as many as the layout counted for it. The value is then put together here, as an
exact `Fraction`, and refused unless it is an inch fraction as the trade writes one: the numerator
below the denominator, the denominator 2, 4, 8, 16, 32 or 64 (#786's rule,
`extraction.reader.INCH_DENOMINATORS`), and an inch mark drawn after it.

**Two readers, on every piece (#865).** One reader is one witness, so the admin's decision on the
plan for #756 is that a label is read only where two readers of different vendors agree on every
piece. The first is local OCR, a reader that is not a model and counts by its own name; the second
is handed in (`SecondReader`), and the two must be independent by `evidence.corroborate`'s
`independence_key`, the one place "different vendor" is decided. Both are shown the same picture of
each piece. The second is asked only once the first has read every piece and the value they make is
one the trade writes, so a label the first reader already rules out costs no second reading. Any
piece the two read differently, or the second does not read, is no reading of the label.

**It refuses rather than guesses**, and every refusal is a reason a caller can count. A piece that
does not read, reads as more than one thing, or reads with the wrong number of digits; a value the
trade would not write; a label with no inch mark; a character beside the label that the layout
counts as nothing (`FractionLayout.neighbours`); a label turned on the page, which the admin's
decision on the plan for #756 sends to a person; a piece the two readers do not agree on — each one
is no reading at all, never a reading with a doubt attached.

**What it never does is confirm.** A reading made here is a stacked fraction read, and the admin's
rule is that a stacked fraction always goes to a person (#726): its caller records it with
`STACKED_FRACTION_FLAG`, so no number of readers agrees it into evidence and automatic typing
refuses it. Two readers agreeing decides only that the form is pre-filled for a person to tick.

Source: issues #848 and #865, steps 2 and 3 of the plan on #756.
Verification: `tests/extraction/test_fraction_parts.py`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Protocol

import numpy as np

from evidence.corroborate import independence_key
from evidence.crop import encode_png
from extraction.annotations import StackedFraction, VectorPath
from extraction.glyph_bands import GlyphBox, GlyphCharacter
from extraction.glyph_shapes import UndrawablePath, rasterise, subpaths
from extraction.models.validation import MAXIMUM_PIECE_DIGITS, PIECE_DIGITS_RE
from extraction.ocr import OcrEngine
from extraction.reader import INCH_DENOMINATORS
from units.measurement import Measurement, Unit

__all__ = [
    "FRACTION_PARTS_EXTRACTOR",
    "FRACTION_PARTS_VERSION",
    "DrawnPiece",
    "FractionPartsReading",
    "FractionPartsRefusal",
    "PieceDrawing",
    "SecondReader",
    "read_fraction_parts",
]

#: The extractor a reading put together from its pieces is recorded under: its own, so every
#: consumer can tell it from a reading of the whole label, by OCR or by a model.
FRACTION_PARTS_EXTRACTOR: Final = "extraction.fraction_parts"

#: Ours, not the engine's: it names how pieces are cut, drawn, checked and put together, so a change
#: to any of that is a new version even when the engine is unchanged. `/2` since #865: a label is
#: read only where a second reader agrees on every piece.
FRACTION_PARTS_VERSION: Final = "extraction.fraction_parts/2"

#: The most digits a piece may read as, and the rule a piece's digits are held to: the same for both
#: readers, so one definition (`extraction.models.validation`).
MAXIMUM_DIGITS: Final = MAXIMUM_PIECE_DIGITS
_DIGITS_RE: Final = PIECE_DIGITS_RE

# The reasons a label is not read. Written here, never taken from what a reader returned, so a page
# result that counts them repeats nothing from the drawing. The second reader's own refusals are
# its caller's sentences, under the same rule (`SecondReader.read_digits`).
SET_IN_TEXT: Final = "the label is set in text, so it has no paths to draw"
TURNED: Final = "the label is turned on the page, and a sideways stacked fraction goes to a person"
NO_STACK: Final = "the fraction has no numerator or no denominator drawn in black or grey"
NO_INCH_MARK: Final = "no inch mark is drawn after the fraction"
NEIGHBOUR: Final = (
    "a character the layout counts as no part of the label lies within the character gap of it"
)
TOO_LONG: Final = f"a piece of the label has more than {MAXIMUM_DIGITS} characters"
PATHS_MISSING: Final = "a piece's paths were not kept with the label"
UNDRAWABLE: Final = "a piece could not be drawn as the file draws it"
NO_HEIGHT: Final = "a piece has no height to be drawn at"
NOT_A_NUMBER: Final = "a piece did not read as one number of one to three digits"
WRONG_COUNT: Final = "a piece read as a different number of digits than the drawing has"
LEADING_ZERO: Final = "a piece read with a leading zero, which no label is written with"
NOT_BELOW: Final = "the numerator is not below the denominator, or is zero"
NOT_AN_INCH_FRACTION: Final = "the denominator is not 2, 4, 8, 16, 32 or 64"
DISAGREE: Final = "the second reader read a piece differently, so a person types the value"


@dataclass(frozen=True, slots=True)
class PieceDrawing:
    """How each piece is drawn for the reader. Stated by a deployment, never defaulted.

    What a reader makes of a picture depends on how big the characters are in it, how thick their
    lines, and how much white is round them; a value fitted on one drawing would look like a reader
    everywhere else. So a deployment states them, with no default, as it states the stacked-fraction
    detector's (`FractionBarGeometry`), and the extraction run records them (`config_hash`).
    """

    height_px: int
    """How tall a piece is drawn, in pixels: its own height scaled to this, rounded up across the
    raster, with its width kept in proportion. The same for every piece, so a one-digit numerator
    and a three-digit whole number are drawn at one character height."""

    stroke_px: int
    """How thick a stroked path is drawn, in pixels."""

    bezier_steps: int
    """How many straight steps each Bézier curve is drawn as."""

    margin_px: int
    """How much white is put round the drawn piece, in pixels, on every side."""

    def __post_init__(self) -> None:
        for name in ("height_px", "stroke_px", "bezier_steps", "margin_px"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive whole number of pixels or steps")

    @property
    def config_hash(self) -> str:
        """Every number, as text for the identity of a run whose readings these settings change."""
        return (
            f"height_px={self.height_px};stroke_px={self.stroke_px};"
            f"bezier_steps={self.bezier_steps};margin_px={self.margin_px}"
        )


@dataclass(frozen=True, slots=True)
class FractionPartsReading:
    """One stacked label read piece by piece: what it says, its exact value, and where it is."""

    text: str
    """The label as one line, `37 1/2"` or `3/4"`, written from the digits read."""

    value: Measurement
    """The exact value in inches, put together in code from the pieces."""

    box: GlyphBox
    """The whole label in page space (PDF points): whole number, fraction and inch mark together."""


@dataclass(frozen=True, slots=True)
class FractionPartsRefusal:
    """Why one stacked label was not read: a sentence this module or its caller wrote, never a
    reader's text."""

    reason: str


@dataclass(frozen=True, slots=True)
class DrawnPiece:
    """One piece as the second reader is shown it: the picture the first reader was shown."""

    png: bytes
    """The piece's RGB pixels, as the first reader had them, encoded as PNG."""

    digit_count: int
    """How many characters the layout counted in the piece, which a reading must match."""


class SecondReader(Protocol):
    """The second witness to every piece (#865): a reader independent of the first by vendor."""

    @property
    def extractor(self) -> str:
        """The name the reader's readings are recorded under."""

    @property
    def extractor_version(self) -> str:
        """For a model reader, its model id — which says who made it (`independence_key`)."""

    def read_digits(self, piece: DrawnPiece) -> str | FractionPartsRefusal:
        """The digits read in `piece`, held to its `digit_count`, or why there are none.

        A refusal's reason is the caller's own sentence — a spent budget, a call that failed, an
        answer the validator refused — never the reader's text.
        """


def _box(path: VectorPath) -> GlyphBox:
    """A path's bounding box, as `annotations.py` computes the boxes a layout is made of."""
    xs = [x for x, _ in path.points]
    ys = [y for _, y in path.points]
    return (min(xs), min(ys), max(xs), max(ys))


def _label_box(fraction: StackedFraction) -> GlyphBox:
    layout = fraction.layout
    assert layout is not None
    boxes = [layout.box, *(box for character in layout.whole for box in character)]
    boxes.extend(layout.inch_mark)
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _piece_paths(
    piece: Sequence[GlyphCharacter], paths: Sequence[VectorPath]
) -> list[VectorPath] | None:
    """The paths that draw one piece, or `None` where any of its boxes has no path."""
    wanted = {box for character in piece for box in character}
    found = [path for path in paths if _box(path) in wanted]
    if {_box(path) for path in found} != wanted:
        return None
    return found


def _draw(paths: Sequence[VectorPath], drawing: PieceDrawing) -> tuple[bytes, int, int] | str:
    """One piece as black on white RGB bytes with its width and height, or why it cannot be.

    `rasterise` draws into a square and spans its longer side, so the square is sized to make the
    piece's height `height_px`: its side is the height scaled by the piece's own proportions,
    rounded up. Sized from the points the piece is drawn through, Bézier curves flattened, so the
    height scaled is the height drawn.
    """
    try:
        points = [
            point
            for path in paths
            for part in subpaths(path, bezier_steps=drawing.bezier_steps)
            for point in part.points
        ]
    except UndrawablePath:
        return UNDRAWABLE
    if not points:
        return UNDRAWABLE
    width = max(x for x, _ in points) - min(x for x, _ in points)
    height = max(y for _, y in points) - min(y for _, y in points)
    if height <= 0:
        return NO_HEIGHT
    side = 1 + math.ceil(
        Fraction(drawing.height_px) * Fraction(max(width, height)) / Fraction(height)
    )
    try:
        raster = rasterise(
            paths,
            size_px=side,
            bezier_steps=drawing.bezier_steps,
            stroke_px=drawing.stroke_px,
        )
    except UndrawablePath:
        return UNDRAWABLE
    page = np.pad(255 - raster, drawing.margin_px, constant_values=255)
    rgb = np.ascontiguousarray(np.repeat(page[:, :, None], 3, axis=2), dtype=np.uint8)
    return rgb.tobytes(), int(page.shape[1]), int(page.shape[0])


@dataclass(frozen=True, slots=True)
class _FirstReading:
    """One piece as the first reader read it, and the picture it read it from."""

    digits: str
    piece: DrawnPiece


def _read_piece(
    piece: Sequence[GlyphCharacter],
    paths: Sequence[VectorPath],
    *,
    engine: OcrEngine,
    drawing: PieceDrawing,
) -> _FirstReading | FractionPartsRefusal:
    """One piece's digits, or why it has none: exactly one reading, of exactly as many digits as the
    piece has characters, and with no leading zero where there is more than one."""
    found = _piece_paths(piece, paths)
    if found is None:
        return FractionPartsRefusal(PATHS_MISSING)
    drawn = _draw(found, drawing)
    if isinstance(drawn, str):
        return FractionPartsRefusal(drawn)
    rgb, width, height = drawn
    items = engine.read(rgb, width=width, height=height)
    if len(items) != 1:
        return FractionPartsRefusal(NOT_A_NUMBER)
    text = items[0].text.strip()
    if not _DIGITS_RE.fullmatch(text):
        return FractionPartsRefusal(NOT_A_NUMBER)
    if len(text) != len(piece):
        return FractionPartsRefusal(WRONG_COUNT)
    if len(text) > 1 and text.startswith("0"):
        return FractionPartsRefusal(LEADING_ZERO)
    return _FirstReading(
        digits=text,
        piece=DrawnPiece(png=encode_png(width, height, rgb), digit_count=len(piece)),
    )


def read_fraction_parts(
    fraction: StackedFraction,
    *,
    engine: OcrEngine,
    drawing: PieceDrawing,
    second: SecondReader,
) -> FractionPartsReading | FractionPartsRefusal:
    """Read one stacked label piece by piece with two readers, or say why not.

    Everything the drawing alone can rule out is ruled out before anything is read, so a label that
    could never be accepted costs no reading. Then the whole number (where there is one), the
    numerator and the denominator are each drawn and read by `engine` in that order, and the first
    piece that fails ends it; so does a value the trade would not write. Only then is `second` asked
    about each piece, in the same order, and the first piece it does not read, or reads differently,
    ends it.

    Raises `ValueError` when `second` is not independent of `engine` by `independence_key`: two
    readings by one reader are one witness, and agreement between them would be no agreement.
    """
    if independence_key(engine.name, engine.version) == independence_key(
        second.extractor, second.extractor_version
    ):
        raise ValueError(
            f"the second reader {second.extractor!r} is not independent of the first, "
            f"{engine.name!r}: a label is read only where two readers of different vendors agree"
        )
    layout = fraction.layout
    if layout is None:
        return FractionPartsRefusal(SET_IN_TEXT)
    # **Upright labels only**: the admin's decision on the plan for #756 is that a sideways stacked
    # fraction always goes to a person, and a piece drawn as the page turns it is not upright.
    if layout.rotation_degrees != 0:
        return FractionPartsRefusal(TURNED)
    if not layout.numerator or not layout.denominator:
        return FractionPartsRefusal(NO_STACK)
    if not layout.inch_mark:
        return FractionPartsRefusal(NO_INCH_MARK)
    if layout.neighbours:
        return FractionPartsRefusal(NEIGHBOUR)
    pieces = (layout.whole, layout.numerator, layout.denominator)
    if any(len(piece) > MAXIMUM_DIGITS for piece in pieces):
        return FractionPartsRefusal(TOO_LONG)

    firsts: list[_FirstReading | None] = []
    for piece in pieces:
        if not piece:
            firsts.append(None)  # a bare fraction, `3/4"`: there is no whole number to read
            continue
        read = _read_piece(piece, fraction.paths, engine=engine, drawing=drawing)
        if isinstance(read, FractionPartsRefusal):
            return read
        firsts.append(read)
    whole, numerator, denominator = (None if read is None else int(read.digits) for read in firsts)
    assert numerator is not None and denominator is not None

    if denominator not in INCH_DENOMINATORS:
        return FractionPartsRefusal(NOT_AN_INCH_FRACTION)
    if not 0 < numerator < denominator:
        return FractionPartsRefusal(NOT_BELOW)

    # **The second witness, piece by piece (#865).** Asked last, so it is never paid to read a label
    # the first reader has already ruled out; and every piece, so no piece rests on one reader.
    for first in firsts:
        if first is None:
            continue
        seconded = second.read_digits(first.piece)
        if isinstance(seconded, FractionPartsRefusal):
            return seconded
        if seconded != first.digits:
            return FractionPartsRefusal(DISAGREE)

    exact = Fraction(numerator, denominator) + (0 if whole is None else whole)
    text = ("" if whole is None else f"{whole} ") + f'{numerator}/{denominator}"'
    return FractionPartsReading(
        text=text,
        value=Measurement(exact=exact, unit=Unit.INCH, raw_text=text),
        box=_label_box(fraction),
    )
