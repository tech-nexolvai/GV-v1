"""Dimension labels inside pasted pictures, read by two OCR engines: $0, no AI (#1830).

Some sheets carry a drawing pasted as a **picture**: a viewer's "paste image" leaves a raster
snapshot of the architect's or the vendor's drawing (a rectangle comment holding an image, #1825),
and a scanned or photographed page is one big image. The text route reads a PDF's characters and
the drawn route its vector pen strokes, so neither reads a number in a picture: an item whose
architect value is printed only there stayed Not compared ("architect gives no size") however
clearly the picture prints it. This module reads those numbers into the same records the other
routes make, so `dim_labels` lists them in `dims.json` and every later step uses them unchanged.

**Which pictures.** Every image the page shows with its pasted drawings (`verify_value`'s vendor
layer) plus its pasted pictures, at least `MIN_SIDE_PT` a side and `MIN_AREA_PT2` in area: a logo
or a product photo inside a stamp is not a drawing. GV's own marks are never drawn, and every
coloured pixel is whitened before reading (GV's marks are coloured; a drawing is black or grey).

**How a label is read.**

1. The picture is drawn at `SCALE` pixels a point (at most `MAX_SIDE_PX` a side) and RapidOCR's
   text detector finds the lines of text in it three ways round: as it is, and turned a quarter
   each way, so a sideways label is found lying flat. A line counts only in the turn that shows it
   wider than tall; that turn says which way it reads on the page.
2. Each line with a digit is cut out and read on its own, by each engine (RapidOCR's recogniser and
   Tesseract, the drawn route's engines: `dim_labels.drawn_readers`), at two sizes, **both ways
   along its line** (upright and upside down; for a sideways label, turned either way).
3. Each reading is parsed by the parsers every route uses (`verify_value._row_value`,
   `finding_checks.to_inches`): feet and inches, inches, fractions, millimetres over inches in
   brackets. A reading counts only when it is one dimension and nothing else: an inch or foot mark
   or a bracket, no word, no `+` or `=`, no leading zero. A bare number counts only as the
   millimetres right over a bracketed inch value (`dim_labels._paired`).

**Two independent reads must agree.** A label is confirmed only when both engines read it, each
reads exactly one value and it is the same one, and no reading of the other direction gives any
value (an upside-down `6` is a `9`). Anything less (one engine, two values, a value read the other
way round, a millimetre value its inches disagree with) is **unconfirmed**: never a label, listed
beside the page's labels with the reason `UNCONFIRMED` so an item at that spot stays Not compared
("number read from a picture: not confirmed"). A confirmed label is still an OCR reading, the trust
class of the drawn route (`route: "drawn"`, `origin: "picture"`): the drawn-ink witness, never
more, and a red only through the same code checks as any label (#756 D2).

**Where it is.** Each label's box is in PDF points of the page as shown (origin top-left), the box
of its ink inside the picture, with its direction (`h`/`v`). **No dimension line** is given: a
picture's line work is pixels, and measured on a real snapshot the lines code found there ran the
whole chain or stopped short (a wrong line is worse than none: it would feed the geometry checks).
So a picture label never passes a geometry or same-ends check on its line alone. The picture's
straight line work (long runs of dark pixels, the text masked out) is only whitened before a line
of text is read, so a dimension line under a label is not read as part of it.

Loaded by name from `dim_labels` (it parses PDFs and runs OCR). Verification:
`tests/review/assistant/test_picture_labels.py` (made-up pictures pasted into made-up PDFs).
"""

from __future__ import annotations

import io
import logging
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import Any, Final, Protocol

import cv2
import numpy as np
import pdfplumber
import pikepdf

from app.review.assistant import verify_value as vv
from extraction.gv_marks import without_gv_marks
from extraction.stamp_text import carries_drawing, pasted_picture
from extraction.visible_text import is_shx_comment, shx_comment_text

__all__ = [
    "MIN_AREA_PT2",
    "MIN_SIDE_PT",
    "ORIGIN",
    "UNCONFIRMED",
    "Detector",
    "PictureLabel",
    "PictureRead",
    "default_detector",
    "pages_with_pictures",
    "picture_boxes",
    "picture_page",
    "picture_sizes",
    "read_page",
    "read_picture",
]

_log = logging.getLogger(__name__)

type Box = tuple[float, float, float, float]
type Point = tuple[float, float]
type Quad = Sequence[Sequence[float]]

#: A label's `origin` when it was read from a pasted picture.
ORIGIN: Final = "picture"
#: Why a number read from a picture is not a label (and an item at its spot is Not compared).
UNCONFIRMED: Final = "number read from a picture: not confirmed"
#: The smallest picture read: points a side, and square points.
MIN_SIDE_PT: Final = 72.0
MIN_AREA_PT2: Final = 10_000.0
#: An embedded image narrower than this both ways (pixels) is never a pasted drawing.
_MIN_IMAGE_PX: Final = 200
#: The picture is drawn at this many pixels a point, at most this many pixels a side.
SCALE: Final = 4.0
MAX_SIDE_PX: Final = 4000
#: The text heights (pixels) each line is read at.
HEIGHTS_PX: Final = (40, 60)
#: The most lines read in one picture (a picture full of words is mostly not dimensions).
MAX_LINES: Final = 150
#: Line work: dark pixels in a straight run at least this long (points) are a line.
_LINE_PT: Final = 4.0
#: Line work: at most this thick (points).
_LINE_THICK_PT: Final = 2.0
#: A pixel is ink when darker than this (0 black, 255 white).
_INK: Final = 140

#: Turns: degrees the page's text is turned from upright, as read. 0 upright, 90 read bottom to
#: top, 270 top to bottom, 180 upside down. A line of each direction is also read the other way.
_OTHER_WAY: Final = {0: 180, 180: 0, 90: 270, 270: 90}
_ALONG: Final[dict[int, Point]] = {
    0: (1.0, 0.0),
    180: (-1.0, 0.0),
    90: (0.0, -1.0),
    270: (0.0, 1.0),
}

_PRIMES: Final = str.maketrans({"′": "'", "’": "'", "‘": "'", "″": '"', "“": '"', "”": '"'})
#: Not one dimension: a letter, `=` or `+`, a leading zero, or a decimal point (a drawing writes
#: inches in fractions; a `7.3"` is an engine's reading of `7'-3"` with its foot mark lost).
_NOT_A_DIMENSION: Final = re.compile(r"[A-Za-z=+]|(?<![\d/.])0\d|\d\.|\.\d")
_BARE: Final = re.compile(r"^\d{2,5}$")
_BRACKET_ONLY: Final = re.compile(r"^\[\s*[\d /]+\s*\"?\s*\]$")
_MM_OVER_INCHES: Final = re.compile(r"^(\d{2,5})\s*\[\s*[\d /]+\s*\"?\s*\]$")


class Detector(Protocol):
    """Finds the lines of text in a grey picture: `[(four corners in pixels, text read)]`."""

    def __call__(self, picture: np.ndarray) -> list[tuple[Quad, str]]: ...


class _RapidDetector:
    """RapidOCR's text detector and recogniser on a whole picture (Apache-2.0, local ONNX)."""

    def __init__(self) -> None:
        self._engine = import_module("rapidocr_onnxruntime").RapidOCR()
        self._lock = threading.Lock()

    def __call__(self, picture: np.ndarray) -> list[tuple[Quad, str]]:
        rgb = cv2.cvtColor(picture, cv2.COLOR_GRAY2RGB)
        with self._lock:
            result, _elapsed = self._engine(rgb, use_det=True, use_cls=False, use_rec=True)
        out: list[tuple[Quad, str]] = []
        for item in result or ():
            if len(item) >= 2 and isinstance(item[0], Sequence | np.ndarray):
                out.append(([list(map(float, p)) for p in item[0]], str(item[1])))
        return out


_DETECTOR: _RapidDetector | None = None
_DETECTOR_LOCK: Final = threading.Lock()


def default_detector() -> Detector | None:
    """RapidOCR's detector, loaded once; `None` when it is not installed."""
    global _DETECTOR
    with _DETECTOR_LOCK:
        if _DETECTOR is None:
            try:
                _DETECTOR = _RapidDetector()
            except Exception as missing:  # noqa: BLE001 - an engine not installed
                _log.warning("picture labels: RapidOCR is not available: %s", missing)
                return None
        return _DETECTOR


# ---- the answer ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PictureLabel:
    """One line of a picture read as a dimension (or a millimetre value, `kind`)."""

    text: str
    """As printed, in the spelling every route uses (`3'-0"`, `41 1/8"`, `457 [18]`)."""
    inches: Decimal
    box: Box
    """Its ink, in PDF points of the page as shown, origin top-left."""
    turn: int
    """How its text is turned on the page (`_ALONG`): 0, 90, 180 or 270."""
    font: float
    """Its letters' height (points)."""
    kind: str
    """`feet` (feet and inches), `dimension` (inches), `mm <number>` (millimetres over inches on one
    line), `bracket` (inches in brackets, under millimetres) or `bare` (millimetres)."""

    @property
    def mm(self) -> str | None:
        """The millimetres printed with it on one line (`1168` of `1168 [46]`)."""
        return self.kind[3:] if self.kind.startswith("mm ") else None

    confirmed: bool
    reads: tuple[str, ...]
    """Every reading, `engine turn: text`."""
    why: str = ""
    """Why it is not confirmed (empty when it is)."""

    @property
    def along(self) -> Point:
        return _ALONG[self.turn]

    @property
    def orientation(self) -> str:
        return "h" if self.turn in (0, 180) else "v"


@dataclass(slots=True)
class PictureRead:
    """One page's pictures and what code read in them."""

    boxes: list[Box] = field(default_factory=list)
    labels: list[PictureLabel] = field(default_factory=list)
    status: str = "none"
    """`none` (no picture), `done`, `partial` (out of time), `no_engine` or `failed`."""


# ---- the pictures of a page ----------------------------------------------------------------------


def _big_image(xobject: Any) -> bool:
    try:
        return (
            xobject.get("/Subtype") == pikepdf.Name("/Image")
            and max(int(xobject.get("/Width", 0)), int(xobject.get("/Height", 0))) >= _MIN_IMAGE_PX
        )
    except (TypeError, ValueError, pikepdf.PdfError):
        return False


def _holds_big_image(resources: Any, depth: int = 0) -> bool:
    """Whether resources hold an image of drawing size, directly or in a form inside (two deep)."""
    if not isinstance(resources, pikepdf.Dictionary) or depth > 2:
        return False
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, pikepdf.Dictionary):
        return False
    for xobject in xobjects.values():
        if _big_image(xobject):
            return True
        if xobject.get("/Subtype") == pikepdf.Name("/Form") and _holds_big_image(
            xobject.get("/Resources"), depth + 1
        ):
            return True
    return False


def _kept(annotation: Any) -> bool:
    """A pasted drawing (`carries_drawing`) or a pasted picture: what this route draws."""
    return carries_drawing(annotation) or pasted_picture(annotation)


def pages_with_pictures(pdf: Path) -> list[int]:
    """The pages (from 1) of `pdf` that may show a picture of drawing size: an image at least
    `_MIN_IMAGE_PX` wide in the page's content or in a pasted drawing or picture. Cheap: no page is
    drawn; `picture_page` and `read_page` look closer."""
    found: list[int] = []
    with pikepdf.open(str(pdf)) as source:
        for number, page in enumerate(source.pages, start=1):
            if _shows_big_image(page):
                found.append(number)
    return found


def _shows_big_image(page: Any) -> bool:
    if _holds_big_image(page.obj.get("/Resources")):
        return True
    for annotation in page.obj.get("/Annots") or []:
        if not _kept(annotation):
            continue
        appearance = (annotation.get("/AP") or pikepdf.Dictionary()).get("/N")
        if isinstance(appearance, pikepdf.Stream) and _holds_big_image(
            appearance.get("/Resources")
        ):
            return True
    return False


def picture_page(data: bytes, page_index: int) -> bytes | None:
    """A one-page copy of page `page_index` with its own content, its pasted drawings and its pasted
    pictures merged into it (GV Review's own marks emptied, no other comment), or `None` when the
    page holds no image of drawing size (most pages: nothing is drawn)."""
    with pikepdf.open(io.BytesIO(data)) as source:
        page = source.pages[page_index]
        if not _shows_big_image(page):
            return None
        out = pikepdf.new()
        out.pages.append(page)
        if "/OCProperties" in source.Root:
            out.Root["/OCProperties"] = out.copy_foreign(
                source.make_indirect(source.Root["/OCProperties"])
            )
        copy = out.pages[0]
        every = copy.obj.get("/Annots") or pikepdf.Array()
        shx_comment_text(out, copy, [a for a in every if is_shx_comment(a)])
        copy.obj["/Annots"] = pikepdf.Array([a for a in every if _kept(a)])
        without_gv_marks(out, copy)
        out.flatten_annotations(mode="all")
        buffer = io.BytesIO()
        out.save(buffer)
        return buffer.getvalue()


def picture_sizes(page_pdf: bytes) -> tuple[list[Box], tuple[float, float]]:
    """The boxes (shown points) of the images of drawing size the one-page PDF shows, and the
    page's shown size."""
    boxes: list[Box] = []
    with pdfplumber.open(io.BytesIO(page_pdf)) as document:
        page = document.pages[0]
        shown = vv._placement(page)
        corner = shown(float(page.width), float(page.height))
        start = shown(0.0, 0.0)
        size = (abs(corner[0] - start[0]), abs(corner[1] - start[1]))
        for image in page.images:
            box = vv._shown_box(shown, *(float(image[k]) for k in ("x0", "top", "x1", "bottom")))
            x0, y0 = max(0.0, box[0]), max(0.0, box[1])
            x1, y1 = min(size[0], box[2]), min(size[1], box[3])
            width, height = x1 - x0, y1 - y0
            if min(width, height) < MIN_SIDE_PT or width * height < MIN_AREA_PT2:
                continue
            clipped = (x0, y0, x1, y1)
            if not any(_share(clipped, other) > 0.9 for other in boxes):
                boxes.append(clipped)
    return boxes, size


def _render(page_pdf: bytes, box: Box, size: tuple[float, float]) -> tuple[np.ndarray, float]:
    """The picture's area as a grey picture, its coloured pixels whitened, and its pixels a point."""
    pdfium = import_module("pypdfium2")
    lock = import_module("app.review.assistant.chat_drawing").PDFIUM_LOCK
    width, height = size
    x0, y0, x1, y1 = box
    scale = min(SCALE, MAX_SIDE_PX / max(x1 - x0, y1 - y0))
    with lock:
        document = pdfium.PdfDocument(page_pdf)
        try:
            rendered = document[0].render(
                scale=scale, crop=(x0, height - y1, width - x1, y0), may_draw_forms=True
            )
            rgb = np.array(rendered.to_pil().convert("RGB"))
        finally:
            document.close()
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    rgb[(hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 60)] = 255
    grey: np.ndarray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return grey, scale


def picture_boxes(page_pdf: bytes) -> list[Box]:
    """The boxes (shown points) of the pictures of drawing size a `picture_page` copy shows."""
    return picture_sizes(page_pdf)[0]


# ---- reading one picture -------------------------------------------------------------------------


def _share(a: Box, b: Box) -> float:
    """How much of the smaller box the two share (0 to 1)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return w * h / smaller


def _turned(picture: np.ndarray, turn: int) -> np.ndarray:
    """The picture turned so text turned `turn` on the page reads upright."""
    if turn == 90:
        return cv2.rotate(picture, cv2.ROTATE_90_CLOCKWISE)
    if turn == 270:
        return cv2.rotate(picture, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if turn == 180:
        return cv2.rotate(picture, cv2.ROTATE_180)
    return picture


def _unturned(quad: Quad, turn: int, shape: tuple[int, int]) -> tuple[float, float, float, float]:
    """A detected line's box (pixels of the turned picture) in pixels of the picture as drawn."""
    height, width = shape
    points: list[tuple[float, float]] = []
    for u, v in ((float(p[0]), float(p[1])) for p in quad):
        if turn == 90:  # turned clockwise: (x, y) went to (height - 1 - y, x)
            points.append((v, height - 1 - u))
        elif turn == 270:  # turned anticlockwise: (x, y) went to (y, width - 1 - x)
            points.append((width - 1 - v, u))
        else:
            points.append((u, v))
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(slots=True)
class _Line:
    box: tuple[float, float, float, float]
    """Pixels of the picture as drawn."""
    turn: int
    """The first turn that found it lying flat."""
    found: list[tuple[int, str]] = field(default_factory=list)
    """What the detector read in it, in each turn that found it."""


def _lines(grey: np.ndarray, detector: Detector) -> tuple[list[_Line], list[Box]]:
    """Every line of text with a digit, found in the three turns (merged when two turns find the
    same line the same way), and every line's box (pixels) for masking the line work."""
    lines: list[_Line] = []
    every: list[Box] = []
    for turn in (0, 90, 270):
        turned = _turned(grey, turn)
        for quad, text in detector(turned):
            xs, ys = [float(p[0]) for p in quad], [float(p[1]) for p in quad]
            wide = max(xs) - min(xs) >= max(ys) - min(ys)
            box = _unturned(quad, turn, grey.shape[:2])
            every.append(box)
            if not wide or not any(ch.isdigit() for ch in text):
                continue
            same = next(
                (
                    line
                    for line in lines
                    if _share(line.box, box) > 0.5 and line.turn in (turn, _OTHER_WAY[turn])
                ),
                None,
            )
            if same is None:
                lines.append(_Line(box, turn, [(turn, text)]))
            else:
                same.found.append((turn, text))
    return lines[:MAX_LINES], every


def _value(text: str) -> tuple[str, Decimal] | None:
    """`(kind, value)` when a reading is one dimension and nothing else: `feet` (feet and inches),
    `dimension` (inches, with their mark), `bracket` (inches in brackets alone), `mm <number>`
    (millimetres and their inches in brackets on one line) or `bare` (a millimetre number); else
    `None`. Two spellings of one value are two readings (`3'-0"` is never
    confirmed by `36"`)."""
    plain = " ".join(vv._plain(text.translate(_PRIMES)).split())
    if not plain or _NOT_A_DIMENSION.search(plain):
        return None
    if _BARE.fullmatch(plain):
        return "bare", Decimal(plain)
    value = vv._row_value(plain, inch_mark=True)
    if value is None or value <= 0:
        return None
    # Everything from the first digit to the last mark must be the value: no stray characters.
    core = plain.strip(" -.")
    if not core[:1].isdigit() and not core.startswith("["):
        return None
    if not core.endswith(("'", '"', "]")):
        return None
    if _BRACKET_ONLY.fullmatch(core):
        return "bracket", value
    millimetres = _MM_OVER_INCHES.fullmatch(core)
    if millimetres:  # one line: `1168 [46]` (the millimetres are part of the reading)
        return f"mm {millimetres.group(1)}", value
    return ("feet" if "'" in core else "dimension"), value


def _crop(grey: np.ndarray, box: tuple[float, float, float, float], turn: int) -> np.ndarray | None:
    x0, y0, x1, y1 = box
    pad = 0.25 * min(x1 - x0, y1 - y0) + 2
    h, w = grey.shape[:2]
    left, top = max(0, int(x0 - pad)), max(0, int(y0 - pad))
    right, bottom = min(w, int(x1 + pad + 1)), min(h, int(y1 + pad + 1))
    if right - left < 4 or bottom - top < 4:
        return None
    return _turned(grey[top:bottom, left:right], turn)


def _sized(picture: np.ndarray, text_px: float, height: int) -> np.ndarray:
    factor = height / max(text_px, 1.0)
    resized = cv2.resize(picture, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    framed: np.ndarray = cv2.copyMakeBorder(resized, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    return framed


def _ink_box(grey: np.ndarray, box: tuple[float, float, float, float], lines: np.ndarray) -> Box:
    """The box of the text's own ink inside a detected line (line work left out), in pixels."""
    x0, y0, x1, y1 = (round(v) for v in box)
    x0, y0 = max(0, x0), max(0, y0)
    area = (grey[y0:y1, x0:x1] < _INK) & (lines[y0:y1, x0:x1] == 0)
    ys, xs = np.nonzero(area)
    if len(xs) == 0:
        return float(x0), float(y0), float(x1), float(y1)
    return (
        float(x0 + xs.min()),
        float(y0 + ys.min()),
        float(x0 + xs.max() + 1),
        float(y0 + ys.max() + 1),
    )


def _read_line(
    grey: np.ndarray, line: _Line, readers: Sequence[Any]
) -> tuple[dict[tuple[str, int], set[tuple[str, Decimal]]], list[str]]:
    """Each engine's readings of one line, both ways along it, at each size."""
    values: dict[tuple[str, int], set[tuple[str, Decimal]]] = {}
    notes: list[str] = []
    detector_name = "RapidOCR"  # the detector is RapidOCR: its reading is that engine's
    for turn, text in line.found:
        notes.append(f"{detector_name} {turn} (found): {text}")
        values.setdefault((_FOUND, turn), set())
        parsed = _value(text)
        if parsed is not None:
            values.setdefault((detector_name, turn), set()).add(parsed)
    x0, y0, x1, y1 = line.box
    text_px = min(x1 - x0, y1 - y0)
    for turn in (line.turn, _OTHER_WAY[line.turn]):
        crop = _crop(grey, line.box, turn)
        if crop is None:
            continue
        for reader in readers:
            for height in HEIGHTS_PX:
                try:
                    read = reader(_sized(crop, text_px, height), single_line=True)
                except Exception as failed:  # noqa: BLE001 - an unread line is left out
                    _log.warning("picture labels: read failed: %s", type(failed).__name__)
                    continue
                text = " ".join(read).strip()
                if not text:
                    continue
                notes.append(f"{reader.name} {turn}: {text}")
                parsed = _value(text)
                if parsed is not None:
                    values.setdefault((reader.name, turn), set()).add(parsed)
    return values, notes


#: Marks each turn in which the text detector found a line lying flat (not an engine's vote).
_FOUND: Final = "found"


def _decide(
    values: dict[tuple[str, int], set[tuple[str, Decimal]]], names: Sequence[str]
) -> tuple[int | None, tuple[str, Decimal] | None, str]:
    """`(turn, (kind, value), why)`: confirmed when two engines each read exactly that one value in
    one direction, nothing was read the other way, and that direction is one the text detector
    found the line lying flat in; else `why` says what is missing."""
    turns = {turn for (name, turn), found in values.items() if found and name != _FOUND}
    found_lying = {turn for name, turn in values if name == _FOUND}
    if not turns:
        return None, None, "no engine read one dimension there"
    if len(turns) > 1:
        return min(turns), None, "it reads as a dimension both ways round"
    (turn,) = turns
    every = set().union(
        *(found for (name, t), found in values.items() if t == turn and name != _FOUND)
    )
    per_engine = [values.get((name, turn), set()) for name in names]
    if len(every) > 1:
        return turn, None, "the readings disagree"
    (only,) = every
    agreeing = sum(1 for found in per_engine if found == {only})
    if agreeing < 2:
        return turn, only, "only one engine read it"
    if turn not in found_lying:
        # Read only the way the text detector did not find it lying (upside down, or a sideways
        # label turned the wrong way): a drawing's text reads from the bottom or the right.
        return turn, only, "it reads only turned the other way round"
    return turn, only, ""


def read_picture(
    grey: np.ndarray,
    origin: Point,
    scale: float,
    readers: Sequence[Any],
    detector: Detector,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = monotonic,
) -> tuple[list[PictureLabel], bool]:
    """The labels read in one grey picture (dark ink on white) whose top-left corner is at `origin`
    (shown points) at `scale` pixels a point, and whether it was read to the end (`False`:
    `deadline` passed)."""
    found, every = _lines(grey, detector)
    mask = np.zeros(grey.shape[:2], np.uint8)
    for x0, y0, x1, y1 in every:
        cv2.rectangle(mask, (int(x0), int(y0)), (int(x1), int(y1)), 255, -1)
    line_px = _line_work(grey, mask, scale)
    # Lines are read without the line work (a dimension line through or under a label).
    clean = grey.copy()
    clean[line_px > 0] = 255
    names = sorted({str(reader.name) for reader in readers})
    out: list[PictureLabel] = []
    whole = True
    for line in found:
        if deadline is not None and clock() > deadline:
            whole = False
            break
        values, notes = _read_line(clean, line, readers)
        turn, decided, why = _decide(values, names)
        kinds = {kind for vs in values.values() for kind, _v in vs}
        if turn is None or (why and kinds == {"bare"}):
            continue  # nothing read as a dimension (a word, a tag, a note), or a lone number
        ink = _ink_box(grey, line.box, line_px)
        box = (
            origin[0] + ink[0] / scale,
            origin[1] + ink[1] / scale,
            origin[0] + ink[2] / scale,
            origin[1] + ink[3] / scale,
        )
        font = min(box[2] - box[0], box[3] - box[1])
        if decided is None:
            readings = sorted({v for vs in values.values() for v in vs}, key=lambda kv: kv[1])
            kind, value = readings[0]
            text = " / ".join(_spelled(k, v) for k, v in readings)
        else:
            kind, value = decided
            text = _spelled(kind, value)
        out.append(
            PictureLabel(
                text=text,
                inches=value,
                box=tuple(round(v, 2) for v in box),  # type: ignore[arg-type]
                turn=turn,
                font=round(font, 2),
                kind=kind,
                confirmed=not why,
                reads=tuple(notes),
                why=why,
            )
        )
    return out, whole


def _spelled(kind: str, value: Decimal) -> str:
    """A reading in the spelling every route uses: `3'-0"`, `41 1/8"`, `[18]`, `457`."""
    if kind == "bare":
        return format(value.normalize(), "f")
    if kind == "feet":
        feet = int(value // 12)
        rest = vv._inches_text(value - 12 * feet)
        return f"{feet}'-{rest}" if feet else rest
    text = vv._inches_text(value)
    if kind == "bracket":
        return f"[{text.rstrip(chr(34))}]"
    if kind.startswith("mm "):
        return f"{kind[3:]} [{text.rstrip(chr(34))}]"
    return text


def _line_work(grey: np.ndarray, text_mask: np.ndarray, scale: float) -> np.ndarray:
    """A mask of the picture's straight line work: runs of dark pixels at least `_LINE_PT` long
    and at most `_LINE_THICK_PT` thick, across or down. Text is masked out first, so a letter's
    stroke is never a line."""
    ink = ((grey < _INK) & (text_mask == 0)).astype(np.uint8) * 255
    length = max(3, round(_LINE_PT * scale))
    thick = max(2, round(_LINE_THICK_PT * scale))
    lines: np.ndarray = np.zeros(ink.shape, np.uint8)
    for horizontal in (True, False):
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (length, 1) if horizontal else (1, length)
        )
        runs = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
        _count, labels, stats, _centres = cv2.connectedComponentsWithStats(runs, connectivity=8)
        across = stats[:, cv2.CC_STAT_HEIGHT if horizontal else cv2.CC_STAT_WIDTH]
        keep = across <= thick
        keep[0] = False  # the background
        lines[keep[labels]] = 255
    return lines


# ---- one page ------------------------------------------------------------------------------------


def read_page(
    pdf: Path,
    page_index: int,
    readers: Sequence[Any],
    *,
    detector: Detector | None = None,
    budget_s: float = 120.0,
    clock: Callable[[], float] = monotonic,
) -> PictureRead:
    """The pictures of page `page_index` (from 0) of `pdf` and the labels read in them, within
    `budget_s` seconds. `readers`: the two OCR engines (`dim_labels.drawn_readers`); fewer than two
    distinct engines read nothing (`no_engine`): one engine can never confirm."""
    started = clock()
    result = PictureRead()
    page_pdf = picture_page(Path(pdf).read_bytes(), page_index)
    if page_pdf is None:
        return result
    boxes, size = picture_sizes(page_pdf)
    result.boxes = boxes
    if not boxes:
        return result
    detector = default_detector() if detector is None else detector
    if detector is None or len({str(reader.name) for reader in readers}) < 2:
        result.status = "no_engine"
        return result
    result.status = "done"
    for box in boxes:
        if clock() - started > budget_s:
            result.status = "partial"
            break
        grey, scale = _render(page_pdf, box, size)
        labels, whole = read_picture(
            grey,
            (box[0], box[1]),
            scale,
            readers,
            detector,
            deadline=started + budget_s,
            clock=clock,
        )
        result.labels += labels
        if not whole:
            result.status = "partial"
            break
    return result
