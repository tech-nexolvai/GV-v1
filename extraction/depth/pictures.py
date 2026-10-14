"""The pictures the depth and sink reader shows its two AI readers (#1256, design §5).

Every picture is cut and marked by code; no model chooses a zoom. All of them start from one
`evidence.crop.RenderedPage` and take pixel boxes in that page's pixels. Render it with the
vendor's drawing only (`render_page(vendor_only=True)`): GV's separate notes are then left out,
**but marks GV made inside an earlier round's drawing are part of that pasted drawing and do reach
the picture** (red outlines and yellow tags on second-round sheets). The readers' questions must
keep telling them to ignore GV's yellow, red and green marks. They come out as PNG bytes
through `evidence.crop.encode_png`, the encoder every other crop in the product uses, so the same
inputs give the same bytes.

* **`view_picture`**: one view with its heading painted out. Every picture also paints out every
  heading on the panel it is given (`headings`), so no picture tells a reader the vendor's name for
  a view. The readers name the view's kind from
  the drawing alone, so their judgment stays independent of code's, which reads the heading.
* **`numbered_spans_picture`**: every span code found in the view, each drawn along its own axis in
  its own colour with its number beside it, on both axes. The width reader's numbered-rows
  picture (`workflow/slot_reader._numbered_rows_png`) draws boxes round horizontal rows only and is
  left as it is; this is a new function.
* **`upright_closeup`** / **`both_turns`**: a sideways label turned to read left to right with
  `extraction/vector_first.upright_png`, 90 or 270. Where the label is real text its matrix says
  which turn; where it is pen strokes both turns are sent and the reader says which reads.
* **`end_marks_picture`**: one span's two ends marked across its own axis: left and right for a
  horizontal span, top and bottom for a vertical one.
* **`lettered_outlines_picture`**: each sink outline outlined and lettered A, B, ..., so "which
  outline is the hole" is a choice of a letter, not a description.

The colours are none of the reviewer's (red, yellow, blue) and not the vendor's black.

Source: issue #1256 · Verification: `tests/extraction/depth/test_pictures.py`
"""

from __future__ import annotations

import functools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from evidence.crop import RenderedPage, encode_png
from extraction.geometry.dimension_lines import Axis
from extraction.vector_first import upright_png

__all__ = [
    "SPAN_COLOURS",
    "MarkedSpan",
    "PixelBox",
    "both_turns",
    "end_marks_picture",
    "lettered_outlines_picture",
    "numbered_spans_picture",
    "upright_closeup",
    "view_picture",
]

type PixelBox = tuple[int, int, int, int]
"""`(left, top, right, bottom)` in the rendered page's pixels, right and bottom exclusive."""

type _Colour = tuple[int, int, int]

#: The spans' colours, span 1 first, with the words a question can use for them. None is the
#: reviewer's red, yellow or blue, or the vendor's black, and each is dark enough for a white
#: number on it.
SPAN_COLOURS: Final[tuple[tuple[str, _Colour], ...]] = (
    ("magenta", (200, 0, 160)),
    ("brown", (140, 85, 20)),
    ("purple", (120, 50, 170)),
    ("teal", (0, 125, 125)),
    ("orange", (210, 95, 0)),
    ("olive", (105, 110, 0)),
)
_WHITE: Final[_Colour] = (255, 255, 255)
_MARK: Final[_Colour] = (200, 0, 160)


@dataclass(frozen=True, slots=True)
class MarkedSpan:
    """One span to draw, in page pixels."""

    number: int
    axis: Axis
    start: tuple[int, int]
    end: tuple[int, int]
    """The two ends on the dimension line, `start` the top or left one."""


def _clamped(rendered: RenderedPage, box: PixelBox) -> PixelBox:
    left, top, right, bottom = box
    left, top = max(0, left), max(0, top)
    right, bottom = min(rendered.width_px, right), min(rendered.height_px, bottom)
    if right <= left or bottom <= top:
        raise ValueError("the box gives no pixels on this page")
    return left, top, right, bottom


def _crop(rendered: RenderedPage, box: PixelBox) -> tuple[PixelBox, bytearray]:
    left, top, right, bottom = _clamped(rendered, box)
    stride = rendered.width_px * 3
    rows = b"".join(
        rendered.rgb_bytes[row * stride + left * 3 : row * stride + right * 3]
        for row in range(top, bottom)
    )
    return (left, top, right, bottom), bytearray(rows)


class _Canvas:
    """A crop's RGB pixels, with the few drawing operations the pictures use."""

    def __init__(self, rendered: RenderedPage, box: PixelBox) -> None:
        (self.left, self.top, right, bottom), self.rgb = _crop(rendered, box)
        self.width, self.height = right - self.left, bottom - self.top

    def fill(self, x0: int, y0: int, x1: int, y1: int, colour: _Colour) -> None:
        """Paint the page-pixel rectangle `[x0, x1) x [y0, y1)`, clipped to the crop."""
        x0, x1 = max(0, x0 - self.left), min(self.width, x1 - self.left)
        y0, y1 = max(0, y0 - self.top), min(self.height, y1 - self.top)
        if x0 >= x1 or y0 >= y1:
            return
        run = bytes(colour) * (x1 - x0)
        stride = self.width * 3
        for row in range(y0, y1):
            self.rgb[row * stride + x0 * 3 : row * stride + x1 * 3] = run

    def frame(self, box: PixelBox, colour: _Colour, thickness: int) -> None:
        x0, y0, x1, y1 = box
        self.fill(x0, y0, x1, y0 + thickness, colour)
        self.fill(x0, y1 - thickness, x1, y1, colour)
        self.fill(x0, y0, x0 + thickness, y1, colour)
        self.fill(x1 - thickness, y0, x1, y1, colour)

    def badge(self, text: str, centre: tuple[int, int], colour: _Colour, size: int) -> None:
        """A filled square of `size` pixels round `centre`, with `text` in white on it."""
        x0, y0 = centre[0] - size // 2, centre[1] - size // 2
        self.fill(x0, y0, x0 + size, y0 + size, colour)
        inner = max(4, size - 2 * max(1, size // 8))
        mask = _glyph_mask(text, inner, inner)
        offset = (size - inner) // 2
        for glyph_y, row in enumerate(mask):
            for glyph_x, inked in enumerate(row):
                if inked:
                    x, y = x0 + offset + glyph_x, y0 + offset + glyph_y
                    self.fill(x, y, x + 1, y + 1, _WHITE)

    def png(self, max_side: int) -> bytes:
        """The crop as PNG, shrunk by a whole factor (box averaged) to `max_side` at most."""
        factor = -(-max(self.width, self.height) // max_side)
        if factor <= 1:
            return encode_png(self.width, self.height, bytes(self.rgb))
        from PIL import Image

        image = Image.frombytes("RGB", (self.width, self.height), bytes(self.rgb))
        small = image.reduce(factor)
        return encode_png(small.width, small.height, small.tobytes())


@functools.lru_cache(maxsize=128)
def _glyph_mask(text: str, width: int, height: int) -> tuple[tuple[bool, ...], ...]:
    """Which pixels of a `width` x `height` box the text's strokes cover, in Pillow's own
    scalable typeface sized to fill the box (the width reader found dotted digits misread)."""
    from PIL import Image, ImageDraw, ImageFont

    size = height
    font = ImageFont.load_default(size=size)
    while size > 4:
        font = ImageFont.load_default(size=size)
        left, top, right, bottom = font.getbbox(text)
        if right - left <= width and bottom - top <= height:
            break
        size -= 1
    image = Image.new("L", (width, height), 0)
    ImageDraw.Draw(image).text((width / 2, height / 2), text, fill=255, font=font, anchor="mm")
    data = image.tobytes()
    return tuple(tuple(data[y * width + x] >= 128 for x in range(width)) for y in range(height))


def view_picture(
    rendered: RenderedPage,
    view: PixelBox,
    headings: Sequence[PixelBox],
    *,
    pad: int,
    max_side: int,
) -> bytes:
    """The view with every heading box (grown by `pad` pixels) painted white."""
    canvas = _Canvas(rendered, view)
    _mask(canvas, [(x0 - pad, y0 - pad, x1 + pad, y1 + pad) for x0, y0, x1, y1 in headings])
    return canvas.png(max_side)


def _mask(canvas: _Canvas, headings: Sequence[PixelBox]) -> None:
    for x0, y0, x1, y1 in headings:
        canvas.fill(x0, y0, x1, y1, _WHITE)


def numbered_spans_picture(
    rendered: RenderedPage,
    view: PixelBox,
    spans: Sequence[MarkedSpan],
    *,
    headings: Sequence[PixelBox],
    thickness: int,
    badge: int,
    max_side: int,
) -> bytes:
    """Every span drawn along its own axis in its colour, its number on a badge beside it.

    The badge sits beside the line (above a horizontal span, left of a vertical one) a quarter of
    the way along from its top or left end, not at its middle: a vendor prints a dimension's number
    at the middle, and a badge there would cover the very number the reader is asked about.
    """
    canvas = _Canvas(rendered, view)
    _mask(canvas, headings)
    half = thickness // 2
    for index, span in enumerate(spans):
        colour = SPAN_COLOURS[index % len(SPAN_COLOURS)][1]
        (x0, y0), (x1, y1) = span.start, span.end
        if span.axis is Axis.HORIZONTAL:
            y = (y0 + y1) // 2
            canvas.fill(min(x0, x1), y - half, max(x0, x1) + 1, y - half + thickness, colour)
            centre = (min(x0, x1) + abs(x1 - x0) // 4, y - half - badge // 2 - 1)
        else:
            x = (x0 + x1) // 2
            canvas.fill(x - half, min(y0, y1), x - half + thickness, max(y0, y1) + 1, colour)
            centre = (x - half - badge // 2 - 1, min(y0, y1) + abs(y1 - y0) // 4)
        canvas.badge(str(span.number), centre, colour, badge)
    return canvas.png(max_side)


def upright_closeup(
    rendered: RenderedPage, label: PixelBox, *, rotation: Literal[90, 270]
) -> bytes:
    """The label's crop turned so a label reading up (90) or down (270) the page reads upright."""
    canvas = _Canvas(rendered, label)
    return upright_png(
        encode_png(canvas.width, canvas.height, bytes(canvas.rgb)),
        label_rotation_degrees=rotation,
    )


def both_turns(rendered: RenderedPage, label: PixelBox) -> tuple[bytes, bytes]:
    """The label turned both ways (90, then 270), for a pen-stroke label whose turn is unknown."""
    return (
        upright_closeup(rendered, label, rotation=90),
        upright_closeup(rendered, label, rotation=270),
    )


def end_marks_picture(
    rendered: RenderedPage,
    crop: PixelBox,
    span: MarkedSpan,
    *,
    thickness: int,
    reach: int,
    max_side: int,
) -> bytes:
    """The span's two ends marked across its own axis, each mark `reach` pixels either side of the
    line: vertical bars at a horizontal span's left and right ends, horizontal bars at a vertical
    span's top and bottom ends."""
    canvas = _Canvas(rendered, crop)
    half = thickness // 2
    for x, y in (span.start, span.end):
        if span.axis is Axis.HORIZONTAL:
            canvas.fill(x - half, y - reach, x - half + thickness, y + reach + 1, _MARK)
        else:
            canvas.fill(x - reach, y - half, x + reach + 1, y - half + thickness, _MARK)
    return canvas.png(max_side)


def lettered_outlines_picture(
    rendered: RenderedPage,
    crop: PixelBox,
    outlines: Sequence[tuple[str, PixelBox]],
    *,
    headings: Sequence[PixelBox],
    thickness: int,
    badge: int,
    max_side: int,
) -> bytes:
    """Each outline framed in its colour, its letter on a badge at its top-left corner, outside it
    for the first (outer) outline and inside for the others, so nested letters do not overlap."""
    canvas = _Canvas(rendered, crop)
    _mask(canvas, headings)
    for index, (letter, box) in enumerate(outlines):
        colour = SPAN_COLOURS[index % len(SPAN_COLOURS)][1]
        canvas.frame(box, colour, thickness)
        x0, y0, _x1, _y1 = box
        if index == 0:
            centre = (x0 - badge // 2 - 1, y0 - badge // 2 - 1)
        else:
            centre = (x0 + thickness + badge // 2 + 1, y0 + thickness + badge // 2 + 1)
        canvas.badge(letter, centre, colour, badge)
    return canvas.png(max_side)
