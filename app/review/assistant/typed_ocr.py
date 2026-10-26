"""A second, independent reading of a typed number: its own spot drawn as a picture and read by two
OCR engines, $0, no AI (final plan change #2, option B, the owner 2026-10-26).

A number typed into a PDF is read from the file's text layer (`dim_labels`' text route). That is
exact when the file's characters say what is drawn, but it is one reading: a font whose characters
map to the wrong digits, or a slash a CAD program escaped, gives a wrong number with nothing to
contradict it. The pixels the reader sees are another route: code draws the number's own spot of
the set pack's drawings-only copy (`<name>_drawings_only.pdf`: GV's markup is not in it) and both
OCR engines read it (RapidOCR's recogniser and Tesseract, `dim_labels.drawn_readers`), the way the
picture route reads a pasted picture (`picture_labels._value`: one dimension and nothing else).

**The answer** (`TypedReader`, one label at a time, kept per label):

- `agrees`: both engines read exactly the label's value in one direction the label's text runs
  (level, or turned either way for an upright label). A second, different route: `reading_routes`
  counts it as `ocr`;
- `disagrees`, with the value: both engines read one and the same other value there, and neither
  reads the label's. A doubt: the would-be red waits as a Likely problem;
- `None`: anything else (an engine missing, nothing read, the engines differ, a stacked fraction
  or millimetres over inches the one-line engines cannot read). No route and no doubt.

Only asked for the numbers a would-be red rests on (`reading_routes.mark`), so a check reads a few
spots, never a whole set. Loaded by name: it renders PDF pages and runs OCR.

Verification: `tests/review/assistant/test_typed_ocr.py` (a made-up PDF with typed numbers).
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from importlib import import_module
from pathlib import Path
from typing import Any, Final

import cv2
import numpy as np

__all__ = ["AGREES", "DISAGREES", "TEXT_PX", "TypedReader", "reader_for_pack"]

_log = logging.getLogger(__name__)

AGREES: Final = "agrees"
DISAGREES: Final = "disagrees"
#: The letters' height the spot is drawn at (pixels), within the scale limits below.
TEXT_PX: Final = 48.0
_MIN_SCALE: Final = 2.0
_MAX_SCALE: Final = 16.0
#: A label in whole inches and nothing else (`36"`): the only kind whose other reading is a doubt.
_PLAIN_INCHES: Final = re.compile(r'\s*\d+\s*"\s*')
#: Room round the label's box (points), so no character is cut.
_PAD_PT: Final = 1.5
#: A straight run of ink longer than this many letter heights is line work (a dimension line under
#: the text, an extension line beside it), never part of a character. It is looked for in a wider
#: area (`_AROUND_LETTERS` letter heights round the box), so a line passing by is seen whole.
_LINE_LETTERS: Final = 1.2
_AROUND_LETTERS: Final = 1.5
#: A dimension line within this share of the text's height from its edge runs along the text, and
#: its width (points) when it is whitened.
_EDGE_SHARE: Final = 0.2
_LINE_PT: Final = 0.6
#: A piece of ink smaller than this share of a letter's height both ways is a speck.
_SPECK_SHARE: Final = 0.12
#: A closing bracket an engine reads at a word's end, perhaps as a `1`.
_EDGE_BRACKET: Final = re.compile(r"\s*\][1l|]?\s*$")

type Answer = tuple[str, str | None] | None
"""`(AGREES, None)`, `(DISAGREES, the value both engines read)` or `None`."""


def _turns(label: Mapping[str, Any]) -> tuple[int, ...]:
    orientation = label.get("orientation")
    if orientation == "h":
        return (0,)
    if orientation == "v":
        return (90, 270)
    return (0, 90, 270)


def _expected(label: Mapping[str, Any]) -> Decimal | None:
    try:
        value = Decimal(str(label.get("inches")))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value > 0 else None


class TypedReader:
    """Reads typed labels at their spots in the drawings-only copies (see the module). `pdfs`: each
    upload's name and its drawings-only copy; `readers`: the OCR engines (default: the drawn
    route's two)."""

    def __init__(self, pdfs: Mapping[str, Path], readers: Sequence[Any] | None = None) -> None:
        self._pdfs = dict(pdfs)
        self._readers = list(readers) if readers is not None else None
        self._sizes: dict[tuple[str, int], tuple[float, float]] = {}
        self._kept: dict[tuple[str, int, str], Answer] = {}
        self._lock = threading.Lock()

    def readers(self) -> list[Any]:
        if self._readers is None:
            self._readers = import_module("app.review.assistant.dim_labels").drawn_readers()
        return self._readers

    def __call__(self, label: Mapping[str, Any]) -> Answer:
        file = str(label.get("file") or "")
        try:
            page = int(label.get("file_page") or label.get("page") or 0)
        except (TypeError, ValueError):
            return None
        key = (file, page, str(label.get("id")))
        with self._lock:
            if key in self._kept:
                return self._kept[key]
        try:
            found = self._read(label, file, page)
        except Exception:  # noqa: BLE001 - an unread spot is no route and no doubt
            _log.exception("typed OCR: could not read %s", key)
            found = None
        with self._lock:
            self._kept[key] = found
        return found

    def _read(self, label: Mapping[str, Any], file: str, page: int) -> Answer:
        expected = _expected(label)
        pdf = self._pdfs.get(file)
        box = label.get("box")
        if expected is None or pdf is None or page < 1 or not isinstance(box, Sequence):
            return None
        readers = self.readers()
        names = sorted({str(reader.name) for reader in readers})
        if len(names) < 2:
            return None
        x0, y0, x1, y1 = (float(v) for v in box)
        letters = max(min(x1 - x0, y1 - y0), 1.0)
        scale = min(_MAX_SCALE, max(_MIN_SCALE, TEXT_PX / letters))
        around = max(_PAD_PT, _AROUND_LETTERS * letters)
        rendered = self._render(pdf, page, (x0, y0, x1, y1), scale, around)
        if rendered is None:
            return None
        wide, (left, top) = rendered
        wide = _without_lines(wide, letters * scale)
        _without_own_line(wide, label, (x0, y0, x1, y1), (left, top), scale)
        grey = wide[
            max(0, int((y0 - _PAD_PT - top) * scale)) : int((y1 + _PAD_PT - top) * scale) + 1,
            max(0, int((x0 - _PAD_PT - left) * scale)) : int((x1 + _PAD_PT - left) * scale) + 1,
        ]
        if grey.size == 0:
            return None
        grey = _inside_only(grey, letters * scale, _PAD_PT * scale)
        text = label.get("text") or ""
        pl = import_module("app.review.assistant.picture_labels")
        values: dict[tuple[str, int], set[Decimal]] = {}
        for turn in _turns(label):
            picture = pl._turned(grey, turn)
            parts = _stacked(picture)
            for reader in readers:
                found = values.setdefault((str(reader.name), turn), set())
                for height in pl.HEIGHTS_PX:
                    read = _read(reader, pl._sized(picture, letters * scale, height))
                    parsed = pl._value(read)
                    if parsed is None and read.startswith("[") and "[" not in str(text):
                        # Brackets an engine sees round a word at the picture's edge (`[1'-2"]`,
                        # its closing one read as a `1`): not the label's, which prints none.
                        parsed = pl._value(_EDGE_BRACKET.sub("", read[1:]))
                    if parsed is not None:
                        # A bare number counts for the digits (RapidOCR drops a drawn inch mark).
                        found.add(parsed[1])
                if parts is not None:
                    value = _stacked_value(reader, parts, pl)
                    if value is not None:
                        found.add(value)
        for turn in _turns(label):
            if all(expected in values.get((name, turn), set()) for name in names):
                return (AGREES, None)
        if not _PLAIN_INCHES.fullmatch(str(label.get("text") or "")):
            # A fraction, a foot mark or millimetres an engine drops read as another value
            # (`2'-4"` read `24"`): never a doubt.
            return None
        for turn in _turns(label):
            sets = [values.get((name, turn), set()) for name in names]
            shared = set.intersection(*sets) if sets else set()
            if len(shared) != 1 or any(expected in found for found in sets):
                continue
            other = next(iter(shared))
            # Only a misread of the same shape is a doubt (a font whose characters say 36 where
            # 38 is drawn): a whole number with as many digits.
            if other == other.to_integral_value() and len(str(int(other))) == len(
                str(int(expected))
            ):
                return (DISAGREES, _spelled(other))
        return None

    def _render(
        self,
        pdf: Path,
        page: int,
        box: tuple[float, float, float, float],
        scale: float,
        pad: float = _PAD_PT,
    ) -> tuple[np.ndarray, tuple[float, float]] | None:
        """The box (shown points, origin top-left) and `pad` round it, drawn grey at `scale`, with
        the drawn area's top-left corner (points)."""
        pdfium = import_module("extraction.pdfium_guard").pdfium
        lock = import_module("app.review.assistant.chat_drawing").PDFIUM_LOCK
        x0, y0, x1, y1 = box
        with lock:
            document = pdfium.PdfDocument(str(pdf))
            try:
                if page > len(document):
                    return None
                shown = document[page - 1]
                size = self._sizes.get((str(pdf), page))
                if size is None:
                    whole = shown.render(scale=1)
                    size = self._sizes[(str(pdf), page)] = (float(whole.width), float(whole.height))
                width, height = size
                left, top = max(0.0, x0 - pad), max(0.0, y0 - pad)
                right, bottom = min(float(width), x1 + pad), min(float(height), y1 + pad)
                if right - left < 1 or bottom - top < 1:
                    return None
                rendered = shown.render(
                    scale=scale, crop=(left, height - bottom, width - right, top)
                )
                rgb = np.array(rendered.to_pil().convert("RGB"))
            finally:
                document.close()
        grey: np.ndarray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        return grey, (left, top)


def _without_lines(grey: np.ndarray, letter_px: float) -> np.ndarray:
    """The picture with its long straight runs of ink whitened (`_LINE_LETTERS`): the dimension
    line a number sits on and the extension lines beside it."""
    ink = (grey < 128).astype(np.uint8)
    length = max(3, round(_LINE_LETTERS * letter_px))
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, length), np.uint8))
    lines = cv2.bitwise_or(
        lines, cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((length, 1), np.uint8))
    )
    clean = grey.copy()
    clean[lines > 0] = 255
    return clean


#: A feet-and-inches reading whose dash an engine read as a dot (`3' . 6"` for `3'-6"`).
#: A dot an engine reads before the first digit (a faint speck): a drawing writes no `.8"`.
_LEADING_DOT: Final = re.compile(r"^[.,·]+(?=\d)")
_FEET_DOT: Final = re.compile(r"(\d)\s*'\s*[.·•]\s*(\d)")


def _without_own_line(
    wide: np.ndarray,
    label: Mapping[str, Any],
    box: tuple[float, float, float, float],
    origin: tuple[float, float],
    scale: float,
) -> None:
    """The label's own dimension line (`dim_line`) whitened in place, when the
    line runs along the text's edge or outside it (a short dimension's line is no longer than its
    text, so it is not found as line work); a line through the text's middle is left alone (it
    would cut the characters)."""
    line = label.get("dim_line")
    if not isinstance(line, Sequence) or len(line) != 2:
        return
    try:
        (ax, ay), (bx, by) = ((float(p[0]), float(p[1])) for p in line)
    except (TypeError, ValueError, IndexError):
        return
    x0, y0, x1, y1 = box
    if abs(ay - by) <= 0.5:
        edge = min(abs(ay - y0), abs(ay - y1)) <= _EDGE_SHARE * (y1 - y0) or not y0 < ay < y1
    elif abs(ax - bx) <= 0.5:
        edge = min(abs(ax - x0), abs(ax - x1)) <= _EDGE_SHARE * (x1 - x0) or not x0 < ax < x1
    else:
        return
    if not edge:
        return

    def px(x: float, y: float) -> tuple[int, int]:
        return round((x - origin[0]) * scale), round((y - origin[1]) * scale)

    width = max(1, round(_LINE_PT * scale))
    cv2.line(wide, px(ax, ay), px(bx, by), 255, width)


def _inside_only(grey: np.ndarray, letter_px: float, pad_px: float = 0.0) -> np.ndarray:
    """The picture with every piece of ink that touches its edge whitened (the label's box holds its
    own characters with room round them, so ink reaching the edge is line work passing by: a tick,
    a dashed edge), every piece whose middle lies in the room round the box (`pad_px`: a tick at
    a line's end), and every speck far smaller than a character's stroke (a dot of hatching an
    engine reads as a decimal point)."""
    speck = _SPECK_SHARE * letter_px
    ink = (grey < 128).astype(np.uint8)
    count, labels, stats, _centres = cv2.connectedComponentsWithStats(ink, connectivity=8)
    rows, cols = grey.shape[:2]
    clean = grey.copy()
    for index in range(1, count):
        x, y, w, h = (int(v) for v in stats[index][:4])
        cx, cy = x + w / 2, y + h / 2
        outside = not (pad_px <= cx <= cols - pad_px and pad_px <= cy <= rows - pad_px)
        if x <= 0 or y <= 0 or x + w >= cols or y + h >= rows or max(w, h) < speck or outside:
            clean[labels == index] = 255
    return clean


def _read(reader: Any, picture: np.ndarray) -> str:
    try:
        text = " ".join(reader(picture, single_line=True)).strip()
        return _FEET_DOT.sub(r"\1'-\2", _LEADING_DOT.sub("", text))
    except Exception as failed:  # noqa: BLE001 - one failed read says nothing
        _log.warning("typed OCR: read failed: %s", type(failed).__name__)
        return ""


type _Part = np.ndarray
type Box = tuple[int, int, int, int]
#: What Tesseract reads a lone `1` as, and how narrow (width to height) such a part must be.
_ONE_LOOKALIKES: Final = frozenset({"]", "[", "|", "l", "I", "!"})
_ONE_SHARE: Final = 0.45


def _stacked(picture: np.ndarray) -> tuple[_Part, _Part, _Part] | None:
    """A typed number with a stacked fraction (`97` then `1` over a bar over `2`), split into its
    whole number, numerator and denominator pictures, else `None`. The one-line engines cannot read
    a stacked fraction (they read `97 1/2"` as `97`), so each part is read alone. Ink touching the
    picture's edge is a line passing by, never a character."""
    ink = (picture < 128).astype(np.uint8)
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(ink, connectivity=8)
    rows, cols = picture.shape[:2]
    boxes: list[Box] = []
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        if area < 3 or x <= 0 or y <= 0 or x + w >= cols or y + h >= rows:
            continue
        boxes.append((x, y, w, h))
    if not boxes:
        return None
    tallest = max(h for _x, _y, _w, h in boxes)
    found: list[tuple[list[Box], list[Box], list[Box]]] = []
    for bar in boxes:
        bx, by, bw, bh = bar
        if bw < 2 * bh or bh > 0.25 * tallest:
            continue  # a fraction's bar: a short flat stroke

        def over(b: Box, bar: Box = bar) -> bool:
            return (
                b is not bar and min(b[0] + b[2], bar[0] + bar[2]) - max(b[0], bar[0]) >= 0.5 * b[2]
            )

        numerator = [b for b in boxes if over(b) and b[1] + b[3] <= bar[1] + 1]
        denominator = [b for b in boxes if over(b) and b[1] >= bar[1] + bar[3] - 1]
        # The whole number: left of the bar, across the bar's height.
        whole = [b for b in boxes if b[0] + b[2] <= bx + 1 and b[1] <= by <= b[1] + b[3]]
        if numerator and denominator and whole:
            found.append((whole, numerator, denominator))
    if len(found) != 1:
        return None
    whole, numerator, denominator = found[0]

    def cut(group: Sequence[Box]) -> np.ndarray:
        x0 = min(b[0] for b in group)
        y0 = min(b[1] for b in group)
        x1 = max(b[0] + b[2] for b in group)
        y1 = max(b[1] + b[3] for b in group)
        return picture[max(0, y0 - 1) : y1 + 1, max(0, x0 - 1) : x1 + 1]

    return cut(whole), cut(numerator), cut(denominator)


def _stacked_value(reader: Any, parts: tuple[_Part, _Part, _Part], pl: Any) -> Decimal | None:
    """The value an engine reads from a stacked fraction's three parts (`_stacked`): each must read
    as digits alone, the fraction proper (`2`, `4`, `8`, `16`, `32` or `64` below, less above)."""
    digits: list[int] = []
    for part in parts:
        text = "".join(
            _read(reader, pl._sized(part, float(part.shape[0]), height))
            for height in pl.HEIGHTS_PX[:1]
        ).strip(" .,")
        if text in _ONE_LOOKALIKES and part.shape[1] <= _ONE_SHARE * part.shape[0]:
            text = "1"  # one narrow upright stroke alone: Tesseract reads a lone 1 as a bracket
        if not text.isdigit():
            return None
        digits.append(int(text))
    whole, numerator, denominator = digits
    if denominator not in (2, 4, 8, 16, 32, 64) or not 0 < numerator < denominator:
        return None
    return Decimal(whole) + Decimal(numerator) / Decimal(denominator)


def _spelled(value: Decimal) -> str:
    whole = int(value)
    rest = value - whole
    if not rest:
        return f'{whole}"'
    sixteenths = int((rest * 64).to_integral_value())
    numerator, denominator = sixteenths, 64
    while numerator and numerator % 2 == 0:
        numerator //= 2
        denominator //= 2
    return f'{whole} {numerator}/{denominator}"' if whole else f'{numerator}/{denominator}"'


def reader_for_pack(
    pack: Path | None, files: Sequence[str] = ()
) -> Callable[[Mapping[str, Any]], Answer] | None:
    """A `TypedReader` over a set pack's drawings-only copies (`set_pack.drawings_only`), by each
    upload's name (`files`, else the names `dims.json` lists); `None` when the pack has none."""
    if pack is None:
        return None
    folder = Path(pack)
    names = list(files)
    if not names:
        import json

        try:
            dims = json.loads((folder / "dims.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        names = sorted({str(p.get("file")) for p in (dims or {}).get("pages") or [] if p})
    stem = import_module("app.review.assistant.set_pack").stem
    pdfs = {
        name: folder / f"{stem(name)}_drawings_only.pdf"
        for name in names
        if (folder / f"{stem(name)}_drawings_only.pdf").is_file()
    }
    return TypedReader(pdfs) if pdfs else None
