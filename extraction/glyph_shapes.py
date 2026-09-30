"""The vendor's characters, read from the shapes the file draws them with (#756).

**Why a shape reader at all.** The client's labels are not text in the file. A print-to-PDF driver
turned the lettering into pen strokes, so no dictionary holds a `3/4"` and the vision readers read
about a quarter of the labels right on the human key (#641). But the file still holds the strokes,
and on the client's drawing **one path object is one character**. So a character can be read from
its shape: drawn once into a normalised raster, compared with shapes a person has confirmed, and
read exactly every time it recurs, with no model involved.

This module grows with the phases on #756. Phase A is `rasterise`: drawing a character's paths the
way the file draws them.

**Drawing it the way the file does is the whole difficulty.** Points alone join an inch mark's two
ticks into one stroke and put a corner at every Bézier control point, and two characters that
rasterise the same cannot be told apart by anything downstream. So a sub-path starts where the file
lifts the pen, a Bézier is flattened along its curve, a closed sub-path is closed, and a path this
cannot draw faithfully is refused rather than drawn approximately.

**Nothing here has a default.** The raster size, the Bézier resolution and the stroke width are
the caller's to state, as every threshold on this route is (#756 §Rules).

Source: issue #756 · Verification: `tests/extraction/test_glyph_shapes.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

import cv2
import numpy as np
from numpy.typing import NDArray

from extraction.annotations import SegmentKind, VectorPath

__all__ = ["SubPath", "UndrawablePath", "rasterise", "subpaths"]


class UndrawablePath(ValueError):
    """A path this module cannot draw faithfully, and why. Never drawn approximately instead."""


@dataclass(frozen=True, slots=True)
class SubPath:
    """One pen-down stretch of a path: its points along the drawn line, and whether it closes."""

    points: tuple[tuple[Decimal, Decimal], ...]
    closed: bool


def _bezier(
    start: tuple[Decimal, Decimal],
    first: tuple[Decimal, Decimal],
    second: tuple[Decimal, Decimal],
    end: tuple[Decimal, Decimal],
    steps: int,
) -> list[tuple[Decimal, Decimal]]:
    """Points along one cubic Bézier after `start`, at `steps` equal parameter intervals.

    Exact Decimal arithmetic in the Bernstein form. The control points shape the curve and are not
    on it, which is the point: the end point is the last sample, and neither control point is one.
    """
    samples: list[tuple[Decimal, Decimal]] = []
    for step in range(1, steps + 1):
        t = Decimal(step) / Decimal(steps)
        u = 1 - t
        weights = (u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t)
        controls = (start, first, second, end)
        samples.append(
            (
                sum((w * p[0] for w, p in zip(weights, controls, strict=True)), Decimal(0)),
                sum((w * p[1] for w, p in zip(weights, controls, strict=True)), Decimal(0)),
            )
        )
    return samples


def subpaths(path: VectorPath, *, bezier_steps: int) -> tuple[SubPath, ...]:
    """A path as the pen-down stretches the file draws, with every Bézier flattened.

    Raises `UndrawablePath` for a segment pdfium could not name and for a Bézier that is not a whole
    triple of points. Both mean the shape is not the file's shape, and a guess at it would be read
    as a character.
    """
    if isinstance(bezier_steps, bool) or not isinstance(bezier_steps, int) or bezier_steps < 1:
        raise ValueError("bezier_steps must be a positive integer")
    found: list[SubPath] = []
    current: list[tuple[Decimal, Decimal]] = []
    closed = False
    pending: list[tuple[Decimal, Decimal]] = []

    def finish() -> None:
        nonlocal current, closed
        if pending:
            raise UndrawablePath("a Bézier segment ends part-way through its three points")
        if current:
            found.append(SubPath(points=tuple(current), closed=closed))
        current, closed = [], False

    for segment in path.segments:
        if segment.kind is SegmentKind.UNKNOWN:
            raise UndrawablePath(
                "the path has a step the file does not name, so its shape is unknown"
            )
        if segment.kind is SegmentKind.MOVE:
            finish()
            current = [segment.point]
        elif segment.kind is SegmentKind.LINE:
            current.append(segment.point)
        else:
            if not current:
                raise UndrawablePath("a Bézier segment starts before the path has a point")
            pending.append(segment.point)
            if len(pending) == 3:
                current.extend(
                    _bezier(current[-1], pending[0], pending[1], pending[2], bezier_steps)
                )
                pending.clear()
        if segment.closes:
            if pending:
                raise UndrawablePath("a sub-path closes part-way through a Bézier segment")
            closed = True
    finish()
    return tuple(found)


def _pixel(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def rasterise(
    paths: Sequence[VectorPath], *, size_px: int, bezier_steps: int, stroke_px: int
) -> NDArray[np.uint8]:
    """Draw paths into a `size_px` square, scaled to their joint box with the aspect kept.

    Ink is 255 on 0. The longer side of the box spans the raster; the shorter is centred, so a `1`
    stays a thin upright stroke rather than being stretched into a block. Page `y` runs up and
    raster rows run down, so the drawing is flipped once, here.

    A stroked path is drawn as lines `stroke_px` wide along each sub-path; a filled one is filled.
    Refused with `UndrawablePath` where the draw mode is unknown, where the paths have no extent,
    and where nothing would be drawn at all.
    """
    for name, value in (("size_px", size_px), ("stroke_px", stroke_px)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not paths:
        raise UndrawablePath("there are no paths to draw")

    drawn: list[tuple[VectorPath, tuple[SubPath, ...]]] = []
    for path in paths:
        if path.stroked is None or path.filled is None:
            raise UndrawablePath("the file does not say whether a path is a line or a filled shape")
        drawn.append((path, subpaths(path, bezier_steps=bezier_steps)))

    points = [point for _, parts in drawn for part in parts for point in part.points]
    if not points:
        raise UndrawablePath("the paths have no points")
    left = min(x for x, _ in points)
    right = max(x for x, _ in points)
    bottom = min(y for _, y in points)
    top = max(y for _, y in points)
    span = max(right - left, top - bottom)
    if span == 0:
        raise UndrawablePath("the paths have no extent, so there is no shape to normalise")

    last = Decimal(size_px - 1)
    scale = last / span
    offset_x = (last - (right - left) * scale) / 2
    offset_y = (last - (top - bottom) * scale) / 2

    def to_pixels(part: SubPath) -> NDArray[np.int32]:
        return np.array(
            [
                [_pixel((x - left) * scale + offset_x), _pixel((top - y) * scale + offset_y)]
                for x, y in part.points
            ],
            dtype=np.int32,
        )

    raster = np.zeros((size_px, size_px), dtype=np.uint8)
    for path, parts in drawn:
        polygons = [to_pixels(part) for part in parts]
        if path.filled:
            cv2.fillPoly(raster, polygons, 255)
        if path.stroked:
            for part, polygon in zip(parts, polygons, strict=True):
                cv2.polylines(raster, [polygon], part.closed, 255, stroke_px)
    if not raster.any():
        raise UndrawablePath("the paths are neither stroked nor filled, so nothing is drawn")
    return raster
