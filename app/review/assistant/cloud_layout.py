"""Where the cloud drawing puts each cloud, marker and note (#1537): plain geometry, no PDF.

A reviewer clouds the VALUE, tightly, puts a numbered marker at the cloud and writes the note in a
clear part of the sheet with a leader line back to it. This module decides those places; the
drawing itself is `chat_drawing.py`.

- **Tight clouds.** When the drawing's text prints the value inside or near the AI's box
  (`verify_value.printed_spots`), the cloud goes round that text (`snapped`). Otherwise a box
  longer than `MAX_CLOUD_PT` (2.5 in) is cut down round its centre (`capped`), and the drawing
  outlines the AI's whole area with a thin dashed line.
- **Notes in clear space** (`place_notes`). Every place on the page is tried on a grid; a note may
  not touch another note, a cloud, a marker or anything in `blocked`, nor leave the page. Of the
  places left, the one with the least drawing ink under it (`InkMap`, from a low-resolution render)
  and nearest its cloud wins; on a tie, right of the cloud, then left, above, below. A note with no
  free place is not drawn on the sheet (its entry is still on the notes pages).
- **Words** wrap to at most `NOTE_LINES` lines of at most `NOTE_WIDTH_PT` (`wrap`).

All boxes are `(x0, y0, x1, y1)` in points of the page as shown, origin top-left.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Final

import numpy as np

__all__ = [
    "EDGE_PT",
    "MAX_CLOUD_PT",
    "NOTE_LINES",
    "NOTE_WIDTH_PT",
    "InkMap",
    "capped",
    "gap",
    "marker_spot",
    "overlaps",
    "place_notes",
    "snapped",
    "wrap",
]

type Box = tuple[float, float, float, float]

#: The longest side a cloud may have when code cannot find the value's text: 2.5 inches.
MAX_CLOUD_PT: Final = 180.0
#: Notes and markers stay this far inside the page edge.
EDGE_PT: Final = 10.0
#: The widest a note may be, and the most lines it may have.
NOTE_WIDTH_PT: Final = 200.0
NOTE_LINES: Final = 3
#: The free space kept round each note (from other notes, clouds and markers).
_NOTE_GAP_PT: Final = 4.0
#: The grid the notes are tried on.
_STEP_PT: Final = 6.0
#: How a place is scored: ink under the note (share of its area, 0 to 1) weighs this much, each
#: point away from the cloud this much, and a side other than the right a little more.
_INK_WEIGHT: Final = 3000.0
_DISTANCE_WEIGHT: Final = 0.35
_SIDE_PENALTY: Final = {"right": 0.0, "left": 12.0, "above": 24.0, "below": 36.0}


def overlaps(a: Box, b: Box, margin: float = 0.0) -> bool:
    """Whether two boxes overlap once `b` is grown by `margin` on every side."""
    return not (
        a[2] <= b[0] - margin
        or a[0] >= b[2] + margin
        or a[3] <= b[1] - margin
        or a[1] >= b[3] + margin
    )


def gap(a: Box, b: Box) -> float:
    """The shortest distance between two boxes (0 when they touch or overlap)."""
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy)


def capped(box: Box, at: tuple[float, float] | None = None) -> tuple[Box, bool]:
    """The box itself when neither side is longer than `MAX_CLOUD_PT`, else a box of at most that
    size per side round `at` (default the box's centre), kept inside the box; and whether it was
    cut down."""
    x0, y0, x1, y1 = (
        min(box[0], box[2]),
        min(box[1], box[3]),
        max(box[0], box[2]),
        max(box[1], box[3]),
    )
    width, height = x1 - x0, y1 - y0
    if max(width, height) <= MAX_CLOUD_PT:
        return (x0, y0, x1, y1), False
    cx, cy = at if at is not None else ((x0 + x1) / 2, (y0 + y1) / 2)
    w, h = min(width, MAX_CLOUD_PT), min(height, MAX_CLOUD_PT)
    left = min(max(cx - w / 2, x0), x1 - w)
    top = min(max(cy - h / 2, y0), y1 - h)
    return (left, top, left + w, top + h), True


def snapped(spots: Sequence[Box], pad: float = 3.0) -> Box | None:
    """The first spot (the one nearest the AI's box), padded; `None` when there is none."""
    if not spots:
        return None
    x0, y0, x1, y1 = spots[0]
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


class InkMap:
    """How much drawing ink lies under any box of a page: from a grey render of the page (`grey`,
    0 = black, 255 = white) drawn at `px_per_pt` pixels per point."""

    def __init__(self, grey: np.ndarray, px_per_pt: float, threshold: int = 235) -> None:
        ink = grey < threshold
        # A thin line can fall between pixels at low resolution: a pixel next to ink counts too.
        grown = ink.copy()
        grown[1:, :] |= ink[:-1, :]
        grown[:-1, :] |= ink[1:, :]
        grown[:, 1:] |= ink[:, :-1]
        grown[:, :-1] |= ink[:, 1:]
        total = np.zeros((grown.shape[0] + 1, grown.shape[1] + 1), dtype=np.int64)
        total[1:, 1:] = grown.astype(np.int64).cumsum(axis=0).cumsum(axis=1)
        self._total = total
        self._scale = px_per_pt

    def densities(self, xs: np.ndarray, ys: np.ndarray, width: float, height: float) -> np.ndarray:
        """The share of ink (0 to 1) under boxes of this size with their top-left corners at
        `xs`, `ys` (points)."""
        rows, cols = self._total.shape[0] - 1, self._total.shape[1] - 1
        c0 = np.clip(np.floor(xs * self._scale).astype(np.int64), 0, cols)
        r0 = np.clip(np.floor(ys * self._scale).astype(np.int64), 0, rows)
        c1 = np.clip(np.ceil((xs + width) * self._scale).astype(np.int64), 0, cols)
        r1 = np.clip(np.ceil((ys + height) * self._scale).astype(np.int64), 0, rows)
        t = self._total
        inked = t[r1, c1] - t[r0, c1] - t[r1, c0] + t[r0, c0]
        area = np.maximum((r1 - r0) * (c1 - c0), 1)
        return inked / area

    def density(self, box: Box) -> float:
        out = self.densities(
            np.array([box[0]]), np.array([box[1]]), box[2] - box[0], box[3] - box[1]
        )
        return float(out[0])


def wrap(
    text: str,
    width: float,
    measure: Callable[[str], float],
    lines: int = NOTE_LINES,
    first_width: float | None = None,
) -> list[str]:
    """`text` in at most `lines` lines no wider than `width` (`first_width` for the first line) as
    `measure` gives a string's width; a word too long for a line is cut; the last line ends with
    "…" when words were left out."""
    words = text.split()
    out: list[str] = []
    current = ""
    index = 0
    while index < len(words) and len(out) < lines:
        limit = first_width if (first_width is not None and not out) else width
        word = words[index]
        candidate = f"{current} {word}" if current else word
        if measure(candidate) <= limit:
            current = candidate
            index += 1
            continue
        if not current:
            # One word longer than the line: cut it.
            cut = len(word)
            while cut > 1 and measure(word[:cut]) > limit:
                cut -= 1
            current = word[:cut]
            words[index] = word[cut:]
        out.append(current)
        current = ""
    if current and len(out) < lines:
        out.append(current)
        current = ""
    if index < len(words) or current:
        last = out[-1] if out else ""
        limit = first_width if (first_width is not None and len(out) <= 1) else width
        while last and measure(last + "…") > limit:
            last = last[:-1].rstrip()
        if out:
            out[-1] = last + "…"
        else:
            out.append(last + "…")
    return out


def marker_spot(
    cloud: Box, radius: float, taken: Sequence[tuple[float, float]], page: tuple[float, float]
) -> tuple[float, float]:
    """Where a numbered marker goes at a cloud: on one of its corners (top left first), the first
    clear of the other markers and inside the page."""
    width, height = page
    x0, y0, x1, y1 = cloud
    low = EDGE_PT / 2 + radius

    def inside(x: float, y: float) -> tuple[float, float]:
        return min(max(x, low), width - low), min(max(y, low), height - low)

    def clear(point: tuple[float, float]) -> bool:
        return all(math.hypot(point[0] - x, point[1] - y) >= 2 * radius + 1 for x, y in taken)

    for corner in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        point = inside(*corner)
        if clear(point):
            return point
    # Every corner taken: step along the top edge to the right.
    for step in range(1, 41):
        point = inside(x0 + step * (2 * radius + 1), y0)
        if clear(point):
            return point
    return inside(x0, y0)


def _side(anchor: Box, xs: np.ndarray, ys: np.ndarray, w: float, h: float) -> np.ndarray:
    """A small penalty by which side of the cloud a note is on: right, left, above, below."""
    cx, cy = (anchor[0] + anchor[2]) / 2, (anchor[1] + anchor[3]) / 2
    nx, ny = xs + w / 2, ys + h / 2
    dx, dy = nx - cx, ny - cy
    horizontal = np.abs(dx) >= np.abs(dy)
    penalty = np.where(
        horizontal,
        np.where(dx >= 0, _SIDE_PENALTY["right"], _SIDE_PENALTY["left"]),
        np.where(dy < 0, _SIDE_PENALTY["above"], _SIDE_PENALTY["below"]),
    )
    return penalty.astype(float)


def place_notes(
    page: tuple[float, float],
    notes: Sequence[tuple[Box, float, float]],
    ink: InkMap | None,
    blocked: Sequence[Box] = (),
) -> list[Box | None]:
    """A box for each note `(cloud, width, height)`, in order: on the page, clear of every note
    placed before it and of `blocked` (clouds, markers, the stamp), with the least ink under it and
    near its cloud. `None` for a note with no free place left."""
    width, height = page
    taken: list[Box] = list(blocked)
    out: list[Box | None] = []
    for anchor, w, h in notes:
        if w > width - 2 * EDGE_PT or h > height - 2 * EDGE_PT:
            out.append(None)
            continue
        xs1 = np.arange(EDGE_PT, width - EDGE_PT - w + 0.01, _STEP_PT)
        ys1 = np.arange(EDGE_PT, height - EDGE_PT - h + 0.01, _STEP_PT)
        xs, ys = np.meshgrid(xs1, ys1)
        xs, ys = xs.ravel(), ys.ravel()
        free = np.ones(xs.shape, dtype=bool)
        for box in taken:
            m = _NOTE_GAP_PT
            free &= ~(
                (xs < box[2] + m)
                & (xs + w > box[0] - m)
                & (ys < box[3] + m)
                & (ys + h > box[1] - m)
            )
        if not free.any():
            out.append(None)
            continue
        xs, ys = xs[free], ys[free]
        dx = np.maximum(0.0, np.maximum(anchor[0] - (xs + w), xs - anchor[2]))
        dy = np.maximum(0.0, np.maximum(anchor[1] - (ys + h), ys - anchor[3]))
        cost = _DISTANCE_WEIGHT * np.hypot(dx, dy) + _side(anchor, xs, ys, w, h)
        if ink is not None:
            cost = cost + _INK_WEIGHT * ink.densities(xs, ys, w, h)
        best = int(np.argmin(cost))
        box = (float(xs[best]), float(ys[best]), float(xs[best]) + w, float(ys[best]) + h)
        out.append(box)
        taken.append(box)
    return out
