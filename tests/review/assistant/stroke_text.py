"""Synthetic CAD stroke lettering for the drawn-number tests (#1561), $0.

`stroke_paths` writes text the way a CAD drawing exported to PDF does: every character is its own
stroked vector path (no text) in a single-line font (`FONT`, polylines on a 4 x 6 grid, like a
CAD stroke font), an inch mark is two separate ticks, and `{1/2}` is a stacked fraction
(numerator, bar and denominator, each its own path). `StrokeReader` is a tiny OCR engine for the
tests: it reads a picture by matching each blob of ink with the same font's digits, so what it
reads is what the route really drew.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

#: A single-line digit font: each digit's strokes as polylines on a 4 wide x 6 tall grid, y up.
FONT: dict[str, tuple[tuple[tuple[float, float], ...], ...]] = {
    "0": (((0, 0), (4, 0), (4, 6), (0, 6), (0, 0)),),
    "1": (((1, 5), (2, 6), (2, 0)),),
    "2": (((0, 6), (4, 6), (4, 3), (0, 3), (0, 0), (4, 0)),),
    "3": (((0, 6), (4, 6), (4, 0), (0, 0)), ((1, 3), (4, 3))),
    "4": (((0, 6), (0, 3), (4, 3)), ((3, 5), (3, 0))),
    "5": (((4, 6), (0, 6), (0, 3), (4, 3), (4, 0), (0, 0)),),
    "6": (((4, 6), (0, 6), (0, 0), (4, 0), (4, 3), (0, 3)),),
    "7": (((0, 6), (4, 6), (1, 0)),),
    "8": (((0, 0), (4, 0), (4, 6), (0, 6), (0, 0)), ((0, 3), (4, 3))),
    "9": (((4, 3), (0, 3), (0, 6), (4, 6), (4, 0), (0, 0)),),
}


@dataclass(frozen=True, slots=True)
class StrokePath:
    """One stroked path in PDF space (y up)."""

    lines: tuple[tuple[tuple[float, float], ...], ...]


_TOKEN = re.compile(r"\{(\d+)/(\d+)\}|.", re.DOTALL)


def stroke_paths(
    text: str, x: float, y: float, height: float = 6.0, turn: bool = False
) -> list[StrokePath]:
    """`text` as stroked paths with its baseline-left at PDF point `(x, y)`; `{n/d}` is a stacked
    fraction, `"` two ticks, `'` one tick. `turn`: reading up the page."""
    out: list[StrokePath] = []
    advance = 0.0
    scale = height / 6

    def place(along: float, up: float) -> tuple[float, float]:
        return (x - up, y + along) if turn else (x + along, y + up)

    def glyph(char: str, at: float, base: float, k: float) -> float:
        lines = tuple(
            tuple(place(at + gx * k, base + gy * k) for gx, gy in line) for line in FONT[char]
        )
        out.append(StrokePath(lines))
        return 4 * k

    def stroke(a0: float, u0: float, a1: float, u1: float) -> None:
        out.append(StrokePath(((place(a0, u0), place(a1, u1)),)))

    for match in _TOKEN.finditer(text):
        if match.group(1):
            small = scale * 0.45
            width = glyph(match.group(1), advance, height * 0.58, small)
            glyph(match.group(2), advance, -height * 0.02, small)
            stroke(advance - 0.3, height * 0.5, advance + width + 0.3, height * 0.5)
            advance += width + height * 0.3
        elif match.group(0) in "\"'":
            for tick in (0.0, 0.9)[: 2 if match.group(0) == '"' else 1]:
                stroke(advance + tick, height, advance + tick, height * 0.68)
            advance += height * 0.6
        elif match.group(0) == " ":
            advance += height * 0.55
        else:
            advance += glyph(match.group(0), advance, 0.0, scale) + height * 0.2
    return out


def stroke_ops(text: str, x: float, y: float, height: float = 6.0, turn: bool = False) -> bytes:
    """`stroke_paths` as PDF content: black stroked paths, one path per character or mark."""
    ops = []
    for path in stroke_paths(text, x, y, height, turn):
        parts = []
        for line in path.lines:
            for n, (px, py) in enumerate(line):
                parts.append(f"{px:.3f} {py:.3f} {'m' if n == 0 else 'l'}")
        ops.append("0 0 0 RG 0.3 w " + " ".join(parts) + " S")
    return (" ".join(ops) + " ").encode()


class StrokeReader:
    """Reads digits of the Hershey simplex font, a drawn `/` and inch / foot ticks in a picture
    (dark ink on white): each blob of ink is matched with the font's digits. A blob that is no
    digit of this font upright makes it read nothing rather than guess."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._digits: list[tuple[str, np.ndarray]] = []
        for digit, lines in FONT.items():
            picture = np.zeros((80, 60), np.uint8)
            for line in lines:
                points = np.array([[8 + 10 * x, 70 - 10 * y] for x, y in line], np.int32)
                cv2.polylines(picture, [points], False, 255, 4)
            self._digits.append((digit, self._norm(picture > 0)))

    @staticmethod
    def _norm(ink: np.ndarray) -> np.ndarray:
        ys, xs = np.nonzero(ink)
        cut = ink[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1].astype(np.uint8) * 255
        cut = cv2.dilate(cut, np.ones((3, 3), np.uint8))
        small = cv2.resize(cut, (20, 30), interpolation=cv2.INTER_AREA).astype(np.float32)
        return small / 255

    def one_shapes(self, picture: np.ndarray, *, single_line: bool) -> list[float]:
        """The width-to-height of each glyph read as `1` (as `TesseractReader.one_shapes`)."""
        return _ones(self, picture)

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        ink = (picture < 128).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        blobs = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= 6]
        if not blobs:
            return []
        tops = [stats[i, cv2.CC_STAT_TOP] for i in blobs]
        bottoms = [stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] for i in blobs]
        top, bottom = min(tops), max(bottoms)
        tall = bottom - top
        read = ""
        last_right: int | None = None
        for i in sorted(blobs, key=lambda i: stats[i, cv2.CC_STAT_LEFT]):
            left, width = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_WIDTH]
            height = stats[i, cv2.CC_STAT_HEIGHT]
            if last_right is not None and left - last_right > 0.3 * tall:
                read += " "
            last_right = left + width
            if height < 0.45 * tall:
                high = stats[i, cv2.CC_STAT_TOP] < top + 0.3 * tall
                if not high or height > 0.4 * tall:
                    return []  # a stacked fraction's part, not on the line: unreadable here
                read += "'"
                continue
            if height < 0.6 * tall:
                return []
            blob = (
                labels[
                    stats[i, cv2.CC_STAT_TOP] : stats[i, cv2.CC_STAT_TOP] + height,
                    left : left + width,
                ]
                == i
            )
            shape = self._norm(blob)
            score, digit = min((float(np.abs(shape - t).mean()), d) for d, t in self._digits)
            if score <= 0.12:
                read += digit
            elif width > 0.3 * height and _slanted(blob):
                read += "/"
            elif width < 0.25 * height:
                read += "1"
            else:
                return []
        text = read.replace("''", '"').strip()
        return [text] if text else []


def _ones(reader: StrokeReader, picture: np.ndarray) -> list[float]:
    """Width to height of every blob `reader` reads as `1` (what Tesseract's boxes tell)."""
    ink = (picture < 128).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    out = []
    for i in range(1, count):
        left, top, width, height = (int(v) for v in stats[i, :4])
        if stats[i, cv2.CC_STAT_AREA] < 6 or height < 8:
            continue
        blob = labels[top : top + height, left : left + width] == i
        score, digit = min(
            (float(np.abs(reader._norm(blob) - t).mean()), d) for d, t in reader._digits
        )
        if digit == "1" and score <= 0.12 or (score > 0.12 and width < 0.25 * height):
            out.append(width / height)
    return out


def _slanted(blob: np.ndarray) -> bool:
    """A thin stroke from top-right to bottom-left: a drawn `/`."""
    height, width = blob.shape
    rows = [np.nonzero(blob[r])[0] for r in (int(height * 0.15), int(height * 0.85))]
    if any(len(r) == 0 for r in rows):
        return False
    filled = blob.mean()
    return bool(rows[0].mean() > rows[1].mean() + 0.3 * width and filled < 0.35)
