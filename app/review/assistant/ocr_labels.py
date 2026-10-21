"""Numbers the label readers missed, read at their spot by two OCR engines: $0, no AI (#1848).

The drawn route (`dim_labels`, `ink_labels`, `hand_labels`) reads a number drawn as pen strokes
only when its vectors prove what it is: a label that gathers to itself, with room round it and a
dimension line of its own, read the same by two engines. That keeps misreads out, and also leaves
some clearly printed numbers with no label (a `22"` on a dotted fill, whose vectors never stop
gathering the dots round it). An item resting on such a number stopped as "no code label for the
vendor value". Anant decided (2026-10-22): **when the label readers miss a printed number but both
OCR engines read the same single value at that spot, code makes a label of it**, in the trust class
of a number read in a pasted picture (`picture_labels`, #1832).

**Where it looks.** Every character-sized group of the page's vector ink (`dim_labels._clusters`,
split into labels by `ink_labels.labels`) on one of the page map's vendor drawings that no label
and no printed phrase covers, of a number's size, with no other ink in line with it a word space
away (`dim_labels._crowded`: what a part belongs to decides its value, so the `4` of a `47"` is
never read alone), and not a dimension line's own ticks. The architect's drawings are not read
here: their numbers are the reference (a misread one would set the wrong target), and they are
text or hand lettering, with routes of their own.

**How it is read.** The page's vendor layer (its own content and its pasted drawings; GV Review's
marks emptied, every coloured pixel whitened, so GV's notes are never read) is drawn at
`picture_labels.SCALE`, its straight line work outside the numbers whitened
(`picture_labels._line_work`). Each spot is cut out and read by each engine (RapidOCR's recogniser
and Tesseract, `dim_labels.drawn_readers`) at two sizes, in every direction its vectors allow
(`ink_labels.turns`) and the other way round, and each reading parsed by the picture route's rule
(`picture_labels._value`: one dimension, an inch or foot mark or brackets, no word, no `+` or
`=`). **Two independent reads must agree** (`picture_labels._decide`): both engines read exactly
one and the same value in one direction the vectors allow, and nothing is read the other way round
(a lone number read upside down counts only where the vectors leave the direction open), and the
row round it, read again, holds no word or sign (`_words_beside`: the `1"` of a scale note).
Anything less is never a label; it is listed on the page (`ocr.unconfirmed`, a suggestion only).

**What the label is.** `route: "drawn"`, `origin: "ocr"`: an OCR reading, the drawn-ink witness
and never more, exactly as a picture label (`label_origin.ocr_read`). Its box is its own ink in
PDF points (origin top-left), with its direction; it has **no dimension line**, so it never passes
a geometry, anchor or same-ends check on a line, and an item resting on it goes red only through
the routes that need code's checks and two agreeing picks, never on the OCR alone. It is added
after every other label of the page, so no older label's id moves.

Loaded by name from `dim_labels` (it parses PDFs and runs OCR). Verification:
`tests/review/assistant/test_ocr_labels.py` (made-up numbers drawn as vector strokes).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Any, Final

import cv2
import numpy as np

from app.review.assistant import ink_labels
from app.review.assistant import picture_labels as pl

__all__ = [
    "MAX_SPOTS",
    "ORIGIN",
    "UNCONFIRMED",
    "OcrRead",
    "Spot",
    "read_spots",
    "spot_of",
]

_log = logging.getLogger(__name__)

type Box = tuple[float, float, float, float]

#: A label's `origin` when it was read at its spot by two OCR engines on a vector page.
ORIGIN: Final = "ocr"
#: Why a number read there is not a label (a suggestion only).
UNCONFIRMED: Final = "number read by OCR at its spot: not confirmed"
#: The most spots read on one page (a page of hatching is not a page of dimensions).
MAX_SPOTS: Final = 120
#: A number's size: its letters at least this tall and its box at most this long (points).
MIN_LETTER_PT: Final = 2.0
MAX_LENGTH_PT: Final = 60.0


@dataclass(frozen=True, slots=True)
class Spot:
    """One group of vector ink no label covers: its box (shown points), the directions its
    vectors allow (`ink_labels.turns`) and the directions in which its vectors draw an inch mark
    at the end of its one row (two twin ticks), with how many digits they draw there."""

    box: Box
    turns: tuple[int, ...]
    marked: tuple[tuple[int, tuple[int, int, int]], ...] = ()
    """`(turn, layout)`: the row's whole number's, numerator's and denominator's character counts
    (`dim_labels._layout`) where its vectors end it in an inch mark."""
    layouts: tuple[tuple[int, tuple[int, int, int]], ...] = ()
    """`(turn, layout)` for each allowed direction in which the spot is one row the vectors lay
    out (`dim_labels._layout`): how many characters its whole number, numerator and denominator
    have. A reading counts only when it fits them (`Fits`)."""


#: A reading of a spot whose vectors draw the inch mark: `(spot, turn, text) -> inches or None`.
type Marked = Callable[[Spot, int, str], Decimal | None]
#: Whether a reading fits the spot's vectors: `(spot, turn, text, (kind, inches)) -> bool`.
type Fits = Callable[[Spot, int, str, tuple[str, Decimal]], bool]


@dataclass(slots=True)
class OcrRead:
    """What code read at a page's spots."""

    labels: list[pl.PictureLabel] = field(default_factory=list)
    status: str = "none"
    """`none` (no spot), `done`, `partial` (out of time) or `no_engine`."""
    spots: int = 0


def _render(vendor_pdf: bytes, box: Box, size: tuple[float, float]) -> tuple[np.ndarray, float]:
    """The drawing's area of the vendor layer as a grey picture (`picture_labels._render`: its
    coloured pixels whitened), and its pixels a point."""
    return pl._render(vendor_pdf, box, size)


def _px(box: Box, origin: tuple[float, float], scale: float) -> tuple[float, float, float, float]:
    return (
        (box[0] - origin[0]) * scale,
        (box[1] - origin[1]) * scale,
        (box[2] - origin[0]) * scale,
        (box[3] - origin[1]) * scale,
    )


def _read_spot(
    clean: np.ndarray,
    box_px: tuple[float, float, float, float],
    spot: Spot,
    readers: Sequence[Any],
    marked: Marked | None = None,
    fits: Fits | None = None,
) -> tuple[dict[tuple[str, int], set[tuple[str, Decimal]]], list[str]]:
    """Each engine's readings of one spot in every direction its vectors allow and the other way
    round, at each size; the allowed directions are marked as found lying (`picture_labels._FOUND`),
    as the text detector marks a line on a picture. An engine often drops a drawn inch mark (`46`
    for `46"`): where the vectors draw the mark in that direction, `marked` reads the line's digits
    as the vectors lay them out (their count must be the vectors' count) as that many inches. In
    an allowed direction a reading counts only when it `fits` the vectors (`21 3/8"` with its
    fraction stacked, read `213"`, has a digit too few)."""
    values: dict[tuple[str, int], set[tuple[str, Decimal]]] = {}
    notes: list[str] = []
    allowed = {int(turn) for turn in spot.turns}
    for turn in allowed:
        values.setdefault((pl._FOUND, turn), set())
    ways = sorted(allowed | {pl._OTHER_WAY[turn] for turn in allowed})
    x0, y0, x1, y1 = box_px
    crops = {turn: pl._crop(clean, box_px, turn) for turn in ways}
    for number, reader in enumerate(readers):
        if number and not any(name != pl._FOUND and found for (name, _t), found in values.items()):
            break  # no engine read a dimension there: a second can never agree
        for turn in ways:
            crop = crops[turn]
            if crop is None:
                continue
            # The letters' height: across the reading direction.
            text_px = (y1 - y0) if turn in (0, 180) else (x1 - x0)
            for height in pl.HEIGHTS_PX:
                try:
                    read = reader(pl._sized(crop, text_px, height), single_line=True)
                except Exception as failed:  # noqa: BLE001 - an unread spot is left out
                    _log.warning("ocr labels: read failed: %s", type(failed).__name__)
                    continue
                text = " ".join(read).strip()
                if not text:
                    continue
                notes.append(f"{reader.name} {turn}: {text}")
                parsed = pl._value(text)
                if (parsed is None or parsed[0] == "bare") and marked is not None:
                    inches = marked(spot, turn, text)
                    if inches is not None:
                        parsed = ("dimension", inches)
                if (
                    parsed is not None
                    and fits is not None
                    and turn in allowed
                    and not fits(spot, turn, text, parsed)
                ):
                    notes[-1] += " (does not fit the vectors)"
                    parsed = None
                if parsed is not None:
                    values.setdefault((reader.name, turn), set()).add(parsed)
    # Where the vectors draw the inch mark at the end of the row, they say which way it reads: a
    # lone number read the other way round (`18"` upside down read `81`) is no second reading.
    # A whole dimension read the other way still refuses the spot.
    ends_marked = {turn for turn, _layout in spot.marked}
    if ends_marked:
        for (name, turn), found in values.items():
            if name != pl._FOUND and turn not in ends_marked:
                found.difference_update({item for item in found if item[0] == "bare"})
    return values, notes


#: Beside a number, along its row, this many letter heights are looked at for words.
BESIDE_HEIGHTS: Final = 2.0
#: What makes a row more than one dimension: a sign (`=`, `:`, `+`) or a word (two letters).
_WORDS: Final = re.compile(r"[=:+]|[A-Za-z]{2,}")


def _words_beside(
    clean: np.ndarray, box_px: tuple[float, float, float, float], turn: int, reader: Any
) -> bool:
    """Whether the first engine reads a word or a sign in the row round a confirmed number
    (`BESIDE_HEIGHTS` letter heights each way along it): the `1"` of `SCALE: 1" = 1'-0"` is part of
    a note, not a dimension. The ink beside it was split from it by the vectors (`_crowded` lets a
    flat `=` pass as the dimension line's own piece), so the row is read again as one picture."""
    x0, y0, x1, y1 = box_px
    if turn in (0, 180):
        reach = BESIDE_HEIGHTS * (y1 - y0)
        wider = (x0 - reach, y0, x1 + reach, y1)
        text_px = y1 - y0
    else:
        reach = BESIDE_HEIGHTS * (x1 - x0)
        wider = (x0, y0 - reach, x1, y1 + reach)
        text_px = x1 - x0
    crop = pl._crop(clean, wider, turn)
    if crop is None:
        return False
    try:
        read = reader(pl._sized(crop, text_px, pl.HEIGHTS_PX[0]), single_line=True)
    except Exception as failed:  # noqa: BLE001 - an unread row says nothing
        _log.warning("ocr labels: read failed: %s", type(failed).__name__)
        return False
    return bool(_WORDS.search(" ".join(read)))


def read_spots(
    vendor_pdf: bytes,
    size: tuple[float, float],
    areas: Sequence[tuple[Box, Sequence[Spot]]],
    keep: Sequence[Box],
    readers: Sequence[Any],
    *,
    marked: Marked | None = None,
    fits: Fits | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = monotonic,
) -> OcrRead:
    """The numbers two engines read at `areas`' spots: each area is a drawing's box (shown points)
    and the spots inside it. `keep`: every printed label, phrase and spot box of the page, kept
    from the line work's whitening (a digit's stroke is never a line). Fewer than two distinct
    engines read nothing (`no_engine`)."""
    result = OcrRead()
    if not any(spots for _box, spots in areas):
        return result
    names = sorted({str(reader.name) for reader in readers})
    if len(names) < 2:
        result.status = "no_engine"
        return result
    result.status = "done"
    for area, spots in areas:
        if not spots:
            continue
        if deadline is not None and clock() > deadline:
            result.status = "partial"
            break
        grey, scale = _render(vendor_pdf, area, size)
        origin = (area[0], area[1])
        mask = np.zeros(grey.shape[:2], np.uint8)
        for box in keep:
            bx0, by0, bx1, by1 = _px(box, origin, scale)
            cv2.rectangle(mask, (int(bx0) - 1, int(by0) - 1), (int(bx1) + 1, int(by1) + 1), 255, -1)
        line_px = pl._line_work(grey, mask, scale)
        clean = grey.copy()
        clean[line_px > 0] = 255
        for spot in spots:
            if deadline is not None and clock() > deadline:
                result.status = "partial"
                break
            result.spots += 1
            box_px = _px(spot.box, origin, scale)
            values, notes = _read_spot(clean, box_px, spot, readers, marked, fits)
            turn, decided, why = pl._decide(values, names)
            kinds = {kind for vs in values.values() for kind, _v in vs}
            if turn is None or (why and kinds == {"bare"}):
                continue  # nothing read as a dimension there (a word, a tag), or a lone number
            if decided is not None and not why and _words_beside(clean, box_px, turn, readers[0]):
                why = "words or signs are written beside it"
            if decided is None:
                readings = sorted({v for vs in values.values() for v in vs}, key=lambda kv: kv[1])
                kind, value = readings[0]
                text = " / ".join(pl._spelled(k, v) for k, v in readings)
            else:
                kind, value = decided
                text = pl._spelled(kind, value)
            box = tuple(round(v, 2) for v in spot.box)
            result.labels.append(
                pl.PictureLabel(
                    text=text,
                    inches=value,
                    box=box,  # type: ignore[arg-type]
                    turn=turn,
                    font=round(min(box[2] - box[0], box[3] - box[1]), 2),
                    kind=kind,
                    confirmed=not why,
                    reads=tuple(notes),
                    why=why,
                )
            )
        if result.status == "partial":
            break
    return result


def spot_of(
    label: ink_labels.Label,
    marked: Sequence[tuple[int, tuple[int, int, int]]] = (),
    layout: Callable[[int], tuple[int, int, int] | None] | None = None,
) -> Spot | None:
    """A group of vector ink as a spot: of a number's size, its directions from its vectors (the
    directions its inch mark gives first, `marked`), and in each its row's `layout`."""
    x0, y0, x1, y1 = label.box
    if max(x1 - x0, y1 - y0) > MAX_LENGTH_PT or min(x1 - x0, y1 - y0) < MIN_LETTER_PT:
        return None
    if len(label.glyphs) < 2:  # a dimension has a mark (inch, foot, bracket): two glyphs at least
        return None
    turns = tuple(turn for turn, _layout in marked) or tuple(
        int(turn) for turn in ink_labels.turns(label)
    )
    layouts = tuple(
        (turn, found)
        for turn in turns
        if layout is not None and (found := layout(turn)) is not None
    )
    return Spot(label.box, turns, tuple(marked), layouts)
