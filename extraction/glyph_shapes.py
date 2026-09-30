"""The vendor's characters, read from the shapes the file draws them with (#756).

**Why a shape reader at all.** The client's labels are not text in the file. A print-to-PDF driver
turned the lettering into pen strokes, so no dictionary holds a `3/4"` and the vision readers read
about a quarter of the labels right on the human key (#641). But the file still holds the strokes,
and on the client's drawing **one path object is one character**. So a character can be read from
its shape: drawn once into a normalised raster, compared with shapes a person has confirmed, and
read exactly every time it recurs, with no model involved.

This module grows with the phases on #756. Phase A is `rasterise`: drawing a character's paths the
way the file draws them. Phase B adds what the inventory and the reader share: `describe` (a
character's shape and its size relative to its label), `overlap` and `chamfer` (how alike two shapes
are), and `cluster_shapes` (grouping the alike, so a person labels each shape once).

**Every comparison is exact.** Overlap is a ratio of whole pixel counts and chamfer distance a mean
of whole city-block distances, both returned as `Fraction`s and compared against the caller's stated
`Decimal` thresholds without a float in between. A match is a decision about which character a
shape is, and a float rounding either side of a threshold would make it one on one machine and not
on another.

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
from fractions import Fraction

import cv2
import numpy as np
from numpy.typing import NDArray

from extraction.annotations import SegmentKind, VectorPath

__all__ = [
    "GlyphShape",
    "ShapeSettings",
    "SubPath",
    "UndrawablePath",
    "chamfer",
    "cluster_shapes",
    "describe",
    "overlap",
    "rasterise",
    "size_ratio",
    "subpaths",
]


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


@dataclass(frozen=True, slots=True)
class ShapeSettings:
    """How a character is drawn for comparison. Stated by the caller, recorded with what it made.

    `dilate_px` thickens every drawn stroke before two shapes are compared. The client's file draws
    the same character with different point counts at different sizes — a `0` is an octagon at one
    size and a smooth loop at another (#756) — and a one-pixel line of each does not overlap the
    other, where the same line a few pixels wide does.
    """

    size_px: int
    bezier_steps: int
    stroke_px: int
    dilate_px: int

    def __post_init__(self) -> None:
        for name in ("size_px", "bezier_steps", "stroke_px"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.dilate_px, bool)
            or not isinstance(self.dilate_px, int)
            or self.dilate_px < 0
        ):
            raise ValueError("dilate_px must be a whole number of pixels")

    @property
    def config_hash(self) -> str:
        return (
            f"size_px={self.size_px};bezier_steps={self.bezier_steps};"
            f"stroke_px={self.stroke_px};dilate_px={self.dilate_px}"
        )


@dataclass(frozen=True, slots=True)
class GlyphShape:
    """One character as it is compared: its drawn shape and its size relative to its label.

    **The size is what the shape alone cannot say.** Normalised to its own box, a `1`, a `|`, an
    inch tick and a fraction bar turned on end are all one vertical line. Relative to the height of
    the run it sits in, a `1` is full height and a tick is a third of it.

    A `dot` is a path with no extent at all — a single point. It has no shape to normalise, so its
    raster is empty and it is compared only with other dots.
    """

    raster: NDArray[np.bool_]
    relative_height: Fraction
    relative_width: Fraction
    dot: bool = False
    straight: bool = False
    """One straight segment and nothing else. A `1`, a `-`, a `/`, an inch tick — or, far more often
    on the client's drawing, a piece of line-work. Its shape cannot say which; only where it sits
    can, so nothing is suggested for it (`scripts/glyph_inventory.py`)."""


def _dilated(raster: NDArray[np.uint8], dilate_px: int) -> NDArray[np.bool_]:
    if not dilate_px:
        return np.asarray(raster > 0, dtype=np.bool_)
    kernel = np.ones((2 * dilate_px + 1, 2 * dilate_px + 1), dtype=np.uint8)
    return np.asarray(cv2.dilate(raster, kernel) > 0, dtype=np.bool_)


def describe(path: VectorPath, *, run_height: Decimal, settings: ShapeSettings) -> GlyphShape:
    """One path as a `GlyphShape`, sized against `run_height` — the height of the label it is in.

    Raises `UndrawablePath` where `rasterise` would, except for a single point, which is a dot.
    """
    if run_height <= 0:
        raise ValueError("run_height must be positive; a character is sized against its label")
    xs = [x for x, _ in path.points]
    ys = [y for _, y in path.points]
    if not xs:
        raise UndrawablePath("the path has no points")
    height = Fraction(max(ys) - min(ys)) / Fraction(run_height)
    width = Fraction(max(xs) - min(xs)) / Fraction(run_height)
    if height == 0 and width == 0:
        return GlyphShape(
            raster=np.zeros((settings.size_px, settings.size_px), dtype=np.bool_),
            relative_height=height,
            relative_width=width,
            dot=True,
        )
    raster = rasterise(
        (path,),
        size_px=settings.size_px,
        bezier_steps=settings.bezier_steps,
        stroke_px=settings.stroke_px,
    )
    parts = subpaths(path, bezier_steps=settings.bezier_steps)
    return GlyphShape(
        raster=_dilated(raster, settings.dilate_px),
        relative_height=height,
        relative_width=width,
        straight=len(parts) == 1 and len(parts[0].points) == 2,
    )


def overlap(first: NDArray[np.bool_], second: NDArray[np.bool_]) -> Fraction:
    """Shared ink over combined ink, exactly. `1` is the same shape; `0` shares nothing."""
    union = int(np.logical_or(first, second).sum())
    if union == 0:
        return Fraction(0)
    return Fraction(int(np.logical_and(first, second).sum()), union)


def _distances(raster: NDArray[np.bool_]) -> NDArray[np.int64]:
    """City-block distance from every pixel to the nearest ink, as whole numbers.

    OpenCV returns the L1 transform as float32, but every value it holds is a small whole number,
    so the conversion to integers is exact rather than a rounding.
    """
    return cv2.distanceTransform(
        np.where(raster, 0, 255).astype(np.uint8), cv2.DIST_L1, cv2.DIST_MASK_3
    ).astype(np.int64)


def chamfer(first: NDArray[np.bool_], second: NDArray[np.bool_]) -> Fraction:
    """The symmetric mean distance, in pixels, from each shape's ink to the other's. `0` is equal.

    City-block distance, because it is a whole number for every pixel and the mean of whole
    numbers is an exact fraction; a Euclidean transform would round. Raises for an empty raster,
    which has no ink to measure from.
    """
    first_ink = int(first.sum())
    second_ink = int(second.sum())
    if not first_ink or not second_ink:
        raise ValueError("chamfer distance needs ink in both shapes")
    to_second = int(_distances(second)[first].sum())
    to_first = int(_distances(first)[second].sum())
    return (Fraction(to_second, first_ink) + Fraction(to_first, second_ink)) / 2


def size_ratio(first: GlyphShape, second: GlyphShape) -> Fraction:
    """How far apart two characters' sizes are, as the larger ratio of height and of width. `1` is equal.

    A zero dimension — a perfectly straight stroke has no width — is compared only with another
    zero: a flat line and a character with width are not the same size by any ratio.
    """
    worst = Fraction(1)
    for mine, theirs in (
        (first.relative_height, second.relative_height),
        (first.relative_width, second.relative_width),
    ):
        if mine == 0 or theirs == 0:
            if mine != theirs:
                return Fraction(10**9)
            continue
        worst = max(worst, max(mine, theirs) / min(mine, theirs))
    return worst


def cluster_shapes(
    shapes: Sequence[GlyphShape], *, minimum_overlap: Decimal, maximum_size_ratio: Decimal
) -> list[list[int]]:
    """Group alike shapes, in input order, each around the first member that founded it.

    A shape joins the cluster whose founder it overlaps most, provided the overlap reaches
    `minimum_overlap` and their sizes are within `maximum_size_ratio`; otherwise it founds a new
    cluster. Every dot is one cluster. Deterministic: the same shapes in the same order give the same
    clusters, which is what lets a label a person gave today apply to the same cluster tomorrow.

    Leader clustering rather than anything cleverer, because what it produces is shown to a person
    who checks every cluster's members — a stray member is visible on the contact sheet, and a
    cluster that mixes two characters is refused and split tighter (#756 phase B).
    """
    if not Decimal(0) < minimum_overlap <= Decimal(1):
        raise ValueError("minimum_overlap must be in (0, 1]")
    if maximum_size_ratio < 1:
        raise ValueError("maximum_size_ratio must be at least 1")
    threshold = Fraction(minimum_overlap)
    ratio_limit = Fraction(maximum_size_ratio)

    clusters: list[list[int]] = []
    founders: list[int] = []
    stacked: NDArray[np.bool_] | None = None
    dot_cluster: int | None = None
    for index, shape in enumerate(shapes):
        if shape.dot:
            if dot_cluster is None:
                dot_cluster = len(clusters)
                clusters.append([])
                founders.append(index)
                row = shape.raster[np.newaxis]
                stacked = row if stacked is None else np.concatenate((stacked, row))
            clusters[dot_cluster].append(index)
            continue
        best: int | None = None
        best_overlap = Fraction(0)
        if stacked is not None:
            shared = np.logical_and(stacked, shape.raster).sum(axis=(1, 2))
            combined = np.logical_or(stacked, shape.raster).sum(axis=(1, 2))
            for candidate in np.flatnonzero(shared):
                founder = shapes[founders[candidate]]
                if founder.dot or size_ratio(shape, founder) > ratio_limit:
                    continue
                score = Fraction(int(shared[candidate]), int(combined[candidate]))
                if score >= threshold and score > best_overlap:
                    best, best_overlap = int(candidate), score
        if best is None:
            clusters.append([index])
            founders.append(index)
            row = shape.raster[np.newaxis]
            stacked = row if stacked is None else np.concatenate((stacked, row))
        else:
            clusters[best].append(index)
    return clusters
