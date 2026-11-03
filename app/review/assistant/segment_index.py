"""Where a page's line work lies: the segments near a spot, without reading every segment of the page.

A big architect's sheet has 100,000 to 250,000 straight pieces of line work (`dim_labels._parse`).
The text route asked, for every printed dimension, about every one of them several times (its row,
its cuts, its ticks, its extension lines): about 95 million distance sums on one Portage sheet,
four minutes of plain Python (profiled 2026-11-03).

`near(segments, x0, y0, x1, y1)` gives the segments whose bounding box meets that box (widened by
`SLACK_PT`), **in their own order**. A caller still applies its own exact test to each segment it
gets, so its answer is the same as when it read them all, as long as the box it asks about holds
every segment its test can accept (each caller says why its box does). Asking about a smaller
sequence (under `MIN_INDEXED` segments), a list (it could change after it was indexed) or a box
with a missing bound (`nan`) gives the whole sequence back: the same answer, the old way.

The index (`SegmentIndex`): each segment is listed in the strips (`STRIP_PT` wide) its box
crosses, once along x and once along y; a question reads the strips of the axis that holds fewer
segments over its box, then keeps those whose box meets it. A segment with a coordinate that is
not finite is given back for every question (its exact test decides). Built once per sequence and
kept for the newest few (`KEPT`), keyed by the sequence itself (`flipped` gives the same segments
mirrored over the diagonal, x for y, with their index mirrored too: a vertical label is looked at
as a horizontal one).

Pure code, $0. Verification: `tests/review/assistant/test_segment_index.py` (made-up segments).
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Sequence
from itertools import chain
from typing import Any, Final

import numpy as np

__all__ = ["MIN_INDEXED", "SLACK_PT", "SegmentIndex", "flipped", "near"]

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

#: Sequences shorter than this are read whole (an index would cost more than it saves).
MIN_INDEXED: Final = 256
#: How far (points) every question's box is widened: a caller's box is worked out from its own
#: test, and this keeps a rounding difference from ever leaving a segment out.
SLACK_PT: Final = 1.0
#: The width of one strip (points).
STRIP_PT: Final = 16.0
#: The most strips a page is cut into along one axis (a sheet is under 300 inches).
_MAX_STRIPS: Final = 4096
#: Indexed sequences kept (a page's segments, mirrored, and the drawn route's).
KEPT: Final = 8


class _Axis:
    """One axis' strips: the segments listed by strip (`order`, strip by strip) and where each
    strip's list starts (`starts`)."""

    __slots__ = ("count", "low", "order", "starts")

    def __init__(self, lo: np.ndarray, hi: np.ndarray, finite: np.ndarray) -> None:
        if finite.any():
            low = float(lo[finite].min())
            high = float(hi[finite].max())
        else:
            low = high = 0.0
        count = int(min(_MAX_STRIPS, max(1, math.floor((high - low) / STRIP_PT) + 1)))
        self.low, self.count = low, count
        ids = np.flatnonzero(finite)
        first = self._strip(lo[ids])
        last = self._strip(hi[ids])
        spans = last - first + 1
        repeated = np.repeat(ids, spans)
        offsets = np.arange(int(spans.sum())) - np.repeat(np.cumsum(spans) - spans, spans)
        strips = np.repeat(first, spans) + offsets
        by_strip = np.argsort(strips, kind="stable")
        self.order = repeated[by_strip]
        self.starts = np.searchsorted(strips[by_strip], np.arange(count + 1), side="left")

    def _strip(self, values: np.ndarray) -> np.ndarray:
        found = np.floor((values - self.low) / STRIP_PT).astype(np.int64)
        return np.clip(found, 0, self.count - 1)

    def span(self, lo: float, hi: float) -> tuple[int, int]:
        """The strips `lo..hi` crosses (`hi` past the last strip: up to it), as a slice of `order`."""
        if hi < self.low or lo == math.inf:
            return 0, 0
        first = 0 if lo <= self.low else min(self.count - 1, math.floor((lo - self.low) / STRIP_PT))
        last = (
            self.count - 1
            if hi == math.inf
            else min(self.count - 1, math.floor((hi - self.low) / STRIP_PT))
        )
        if last < first:
            return 0, 0
        return int(self.starts[first]), int(self.starts[last + 1])


class SegmentIndex:
    """The strips of one sequence of segments (`near`)."""

    __slots__ = ("always", "x", "x0", "x1", "y", "y0", "y1")

    def __init__(self, segments: Sequence[Segment], *, _from: SegmentIndex | None = None) -> None:
        if _from is not None:  # mirrored: x for y
            self.x0, self.x1, self.y0, self.y1 = _from.y0, _from.y1, _from.x0, _from.x1
            self.x, self.y, self.always = _from.y, _from.x, _from.always
            return
        flat = np.fromiter(
            chain.from_iterable(chain.from_iterable(segments)),
            dtype=np.float64,
            count=4 * len(segments),
        ).reshape(-1, 4)
        ax, ay, bx, by = flat[:, 0], flat[:, 1], flat[:, 2], flat[:, 3]
        self.x0, self.x1 = np.minimum(ax, bx), np.maximum(ax, bx)
        self.y0, self.y1 = np.minimum(ay, by), np.maximum(ay, by)
        finite = np.isfinite(flat).all(axis=1)
        self.always = np.flatnonzero(~finite)
        self.x = _Axis(self.x0, self.x1, finite)
        self.y = _Axis(self.y0, self.y1, finite)

    def mirrored(self) -> SegmentIndex:
        """The index of the same segments mirrored over the diagonal (each point `(y, x)`)."""
        return SegmentIndex((), _from=self)

    def ids(self, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
        """The positions (ascending) of the segments whose box meets `x0..x1` by `y0..y1`
        (bounds included, already widened), and of every segment with a coordinate that is not
        finite."""
        xs, xe = self.x.span(x0, x1)
        ys, ye = self.y.span(y0, y1)
        found = self.x.order[xs:xe] if xe - xs <= ye - ys else self.y.order[ys:ye]
        if found.size:
            found = np.unique(found)
            found = found[
                (self.x1[found] >= x0)
                & (self.x0[found] <= x1)
                & (self.y1[found] >= y0)
                & (self.y0[found] <= y1)
            ]
        if self.always.size:
            found = np.union1d(found, self.always)
        return found


_LOCK: Final = threading.Lock()
#: id(sequence) -> (the sequence, its index): the sequence is held so its id is never reused
#: while it is kept.
_KEPT: OrderedDict[int, tuple[Sequence[Segment], SegmentIndex]] = OrderedDict()
#: id(sequence) -> (the sequence, its mirror).
_MIRRORS: OrderedDict[int, tuple[Sequence[Segment], tuple[Segment, ...]]] = OrderedDict()


def _keep(store: OrderedDict[int, Any], key: int, value: Any) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > KEPT:
        store.popitem(last=False)


def _index(segments: Sequence[Segment]) -> SegmentIndex | None:
    """The index of `segments` (built on first use), or `None` when it is read whole."""
    if not isinstance(segments, tuple) or len(segments) < MIN_INDEXED:
        return None
    key = id(segments)
    with _LOCK:
        found = _KEPT.get(key)
        if found is not None and found[0] is segments:
            _KEPT.move_to_end(key)
            return found[1]
    made = SegmentIndex(segments)
    with _LOCK:
        _keep(_KEPT, key, (segments, made))
    return made


def near(
    segments: Sequence[Segment], x0: float, y0: float, x1: float, y1: float
) -> Sequence[Segment]:
    """The segments whose bounding box meets `x0..x1` by `y0..y1` (widened by `SLACK_PT`; a bound
    may be infinite), in their own order; the whole sequence when it is not indexed. The caller
    applies its own exact test to each."""
    if any(math.isnan(v) for v in (x0, y0, x1, y1)):
        return segments
    index = _index(segments)
    if index is None:
        return segments
    found = index.ids(x0 - SLACK_PT, y0 - SLACK_PT, x1 + SLACK_PT, y1 + SLACK_PT)
    return [segments[i] for i in found.tolist()]


def flipped(segments: Sequence[Segment]) -> Sequence[Segment]:
    """`segments` mirrored over the diagonal (each point `(y, x)`), in the same order: kept for
    the newest few sequences, with its index (the original's mirrored), so a page's vertical
    labels share one mirror. A list or a short sequence is mirrored afresh."""
    if not isinstance(segments, tuple) or len(segments) < MIN_INDEXED:
        return [((a[1], a[0]), (b[1], b[0])) for a, b in segments]
    key = id(segments)
    with _LOCK:
        found = _MIRRORS.get(key)
        if found is not None and found[0] is segments:
            _MIRRORS.move_to_end(key)
            return found[1]
    mirror = tuple(((a[1], a[0]), (b[1], b[0])) for a, b in segments)
    index = _index(segments)
    with _LOCK:
        _keep(_MIRRORS, key, (segments, mirror))
        if index is not None:
            _keep(_KEPT, id(mirror), (mirror, index.mirrored()))
    return mirror
