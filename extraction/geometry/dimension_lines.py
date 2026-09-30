"""Which of a sheet's strokes are dimension lines, and which are just lines.

`extraction/annotations.py` hands over every straight segment the vendor's line-work draws — 136 of
them on one real sheet. Most are the cabinets themselves. A few are the dimensions: the horizontal
run with a number over it that says how wide something is. Until they are told apart, a reading has
nothing to attach to, and `extraction/geometry/text_association.py` — written to make exactly that
attachment — says so in its own docstring: it "has never been given vector line-work on a sheet like
this".

**What makes a dimension line a dimension line is not its length or its position.** It is that both
of its ends are met by a perpendicular stroke. Those are the witness lines (extension lines), and
they are the physical link from the dimension to the geometry it measures — they run from the edges
of the thing being dimensioned out to the dimension line. That convention is the oldest thing in
technical drawing and it is why this can be geometry rather than inference: the drawing states what
each number measures, in strokes, and the file still contains them.

Measured on the real sheet before it was written: of the eighteen longest horizontal runs, twelve are
met at both ends by a perpendicular and six are not. That separation is the detector.

**Nothing here decides what a dimension measures.** It finds the line and the witness lines that
bound it. What the geometry between those witness lines *is* — a cabinet, a filler, a countertop — is
`containment.py`'s question and then a reviewer's. A module that answered it here would be assigning
meaning from geometry, and `docs/DESIGN_EXTRACTION.md` §6 names that as the failure this whole layer
exists to prevent: the number reads correctly, the arithmetic is exact, and the finding is about the
wrong thing.

**Refusing is the deliverable.** A stroke that cannot be resolved into a dimension is returned
unclassified, never forced into the nearest plausible structure. Every segment handed in comes back
out in one list or the other, and the result type refuses to be built if one goes missing — the same
contract `text_association.py` keeps, and for the same reason: a dimension quietly dropped is a check
that silently stops running.

**Both thresholds are the caller's and neither has a default.** How near a perpendicular must come to
an endpoint before it counts as a witness line, and how long a run must be before it is a candidate
at all, are properties of a vendor's CAD output rather than of drawings in general. A default here
would ship one sheet's measurements as though they were universal. They are required keyword
arguments so that no call site can acquire one by accident.

Source: issue #179 · Verification: `tests/extraction/geometry/test_dimension_lines.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from evidence.coordinates import StoredPoint
from extraction.geometry.containment import DimensionExtent

__all__ = [
    "Axis",
    "DetectedDimensions",
    "DimensionChain",
    "DimensionClosure",
    "DimensionLine",
    "detect",
]


class Axis(StrEnum):
    """Which way a dimension runs.

    Only the two orthogonal cases. A dimension drawn at an angle is real and this does not pretend
    to find it: an oblique run has no single perpendicular direction to look along, so classifying
    one would need the rotated frame that `deskew.py` owns. On the sheet this was measured against,
    one stroke in 136 was oblique — worth leaving unclassified rather than guessing at.
    """

    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


@dataclass(frozen=True, slots=True)
class DimensionLine:
    """One dimension line and the witness lines that bound it."""

    extent: DimensionExtent
    axis: Axis
    witness_lines: tuple[DimensionExtent, ...]
    """The perpendicular strokes meeting its ends. At least one per end, by construction — a run
    met at only one end is not classified. More than one is ordinary: a witness line is often drawn
    as several collinear pieces, and this reports what was found rather than merging them."""

    source: Literal["vector"] = "vector"
    """Where the endpoints came from. Only `vector` today. `#179` allows a raster fallback and
    requires that it record itself; the field exists so that a raster path cannot be added later
    without the distinction being visible to every consumer."""

    @property
    def span(self) -> Decimal:
        """How far it reaches along its own axis, in stored units.

        Exact: stored coordinates are `Decimal`, and the subtraction of two of them is exact. This
        is a length on the page and never a measurement — the number a dimension *states* is in its
        text, which this module does not read.
        """
        if self.axis is Axis.HORIZONTAL:
            return abs(self.extent.end.x - self.extent.start.x)
        return abs(self.extent.end.y - self.extent.start.y)


@dataclass(frozen=True, slots=True)
class DimensionChain:
    """Dimension lines drawn end to end along one axis.

    **This is what makes closure validation possible.** A cabinet run dimensioned as
    `3 + 15 + 36 + 18 + 2` and separately as an overall `74` is a statement the drawing makes about
    itself, and the parts summing to the whole is checkable without knowing what any of it means.
    `B3` is where that check lives; this only reports that the chain is there.

    Two or more lines, always. A single dimension is not a chain, and returning it as one would make
    every dimension on the sheet look like a closure waiting to be validated.
    """

    axis: Axis
    lines: tuple[DimensionLine, ...]

    @property
    def span(self) -> Decimal:
        """End to end across the whole chain."""
        starts = [self._along(line.extent.start) for line in self.lines]
        ends = [self._along(line.extent.end) for line in self.lines]
        points = starts + ends
        return max(points) - min(points)

    def _along(self, point: StoredPoint) -> Decimal:
        return point.x if self.axis is Axis.HORIZONTAL else point.y


@dataclass(frozen=True, slots=True)
class DimensionClosure:
    """An overall dimension and the smaller ones that tile the same extent.

    **The drawing checking itself.** A cabinet run dimensioned segment by segment *and* again
    overall is a statement that the parts add up to the whole. That the parts *tile* the whole is
    spatial — it is read off where the strokes are — and whether their stated numbers *sum* to the
    stated overall is arithmetic, which is `B3`'s question and can still come out wrong. Keeping
    those two apart is what stops this from being circular: nothing here consults a value, so
    finding a closure cannot make a closure check pass.

    **Not the same thing as a chain.** A `DimensionChain` is lines drawn end to end at the same
    offset — one row. A closure spans rows: a drawing puts the overall on one line and the
    breakdown on another above it, which is exactly the arrangement `CT-WIDTH-001` is written
    against. Requiring one offset would miss every real one.
    """

    axis: Axis
    overall: DimensionLine
    parts: tuple[DimensionLine, ...]
    """Two or more, in order along the axis. One "part" spanning the whole is the same dimension
    drawn twice, not a breakdown of it."""


@dataclass(frozen=True, slots=True)
class DetectedDimensions:
    """What the strokes resolved into, and what they did not.

    `unclassified` is not a leftovers bin to be ignored. It is most of the sheet — the cabinets, the
    borders, the hatching — and it is also where a dimension this detector could not see would end
    up. A caller counting only `lines` would have no way to tell a sheet with three dimensions from
    a sheet with thirty where twenty-seven were missed.
    """

    lines: tuple[DimensionLine, ...]
    chains: tuple[DimensionChain, ...]
    closures: tuple[DimensionClosure, ...]
    unclassified: tuple[DimensionExtent, ...]

    def __post_init__(self) -> None:
        classified = {id(line.extent) for line in self.lines}
        for line in self.lines:
            for witness in line.witness_lines:
                classified.add(id(witness))
        overlap = classified & {id(extent) for extent in self.unclassified}
        if overlap:
            raise ValueError(
                "a segment is both classified and unclassified; every stroke belongs to exactly "
                "one list so that a caller can account for all of them"
            )


def _axis_of(extent: DimensionExtent, *, straightness: Decimal) -> Axis | None:
    """Which axis a stroke runs along, or `None` if it runs along neither.

    `straightness` is how far a stroke may drift off-axis and still count as orthogonal. It is not a
    tolerance on the drawing's accuracy — a CAD file's coordinates are exact — but on the conversion
    that brought them here: stored coordinates are normalised through integer image pixels, so a
    truly horizontal line can arrive a fraction off. Compared on the exact `Decimal`s either way.
    """
    horizontal_drift = abs(extent.end.y - extent.start.y)
    vertical_drift = abs(extent.end.x - extent.start.x)
    if horizontal_drift <= straightness and vertical_drift > straightness:
        return Axis.HORIZONTAL
    if vertical_drift <= straightness and horizontal_drift > straightness:
        return Axis.VERTICAL
    # Both small is a dot, both large is oblique. Neither is classified.
    return None


def _crosses(
    *,
    witness: DimensionExtent,
    at: Decimal,
    along: Decimal,
    axis: Axis,
    tolerance: Decimal,
    crossing_margin: Decimal,
) -> bool:
    """Whether `witness` *crosses* the point (`at`, `along`) on a dimension running down `axis`.

    **Crosses, not merely touches, and that distinction is the whole detector.** A witness line runs
    from the edge of the thing being measured, past the dimension line, and a little beyond — so the
    dimension line's end sits strictly *inside* the witness line's span. That is a T-junction.

    A rectangle's corner is an L: the two strokes stop at each other. Requiring the crossing is what
    separates a dimension from the cabinet it measures, and on the sheet this was measured against
    the separation is not marginal — of 81 runs met at both ends, 21 are crossed at both ends and 53
    are plain box corners.

    Strictly inside, by `crossing_margin` — which is a different scale from `tolerance` and was
    briefly the same one, to the detector's cost. `tolerance` is how near the witness must sit
    *across* the axis, and is generous because a witness line and a dimension line meet by drawing
    convention rather than to the micron. `crossing_margin` is how far it must extend *past* along
    the axis, and is tight: a witness line overshoots by a few points, so demanding the wider
    tolerance there rejected every real dimension on the sheet and left six.
    """
    if axis is Axis.HORIZONTAL:
        across = witness.start.x
        low, high = min(witness.start.y, witness.end.y), max(witness.start.y, witness.end.y)
    else:
        across = witness.start.y
        low, high = min(witness.start.x, witness.end.x), max(witness.start.x, witness.end.x)
    if abs(across - at) > tolerance:
        return False
    return low + crossing_margin < along < high - crossing_margin


def detect(
    segments: Sequence[DimensionExtent],
    *,
    witness_tolerance: Decimal,
    minimum_span: Decimal,
    straightness: Decimal,
    crossing_margin: Decimal,
) -> DetectedDimensions:
    """Resolve a page's strokes into dimension lines, chains, and everything else.

    Args:
        segments: every straight stroke on the page, in stored coordinates. From
            `extraction/annotations.py:read_annotation_layers` on a markup-drawn sheet, or
            `extraction/reader.py` on one whose line-work is in the content stream.
        witness_tolerance: how near a perpendicular must come to an endpoint to bound it, in stored
            units. Too small and a dimension whose witness line stops a hair short is missed; too
            large and any passing stroke qualifies.
        minimum_span: how far a run must reach to be a candidate at all. Ticks, arrowheads and
            hatching are strokes too, and every one of them has ends that other strokes pass near.
        straightness: how far off-axis a stroke may drift and still count as orthogonal.
        crossing_margin: how far a witness line must extend *past* the dimension line before the
            junction counts as a T rather than an L. This is what separates a dimension from the
            box it measures, so it is the parameter to reach for when a vendor's sheet resolves into
            too many dimensions or none.

    Returns:
        Every *distinct* stroke, in exactly one of `lines` (with its witness lines), or
        `unclassified`. Coincident duplicates are collapsed first — see the note in the body.

    Raises:
        ValueError: if the segments come from more than one page or document version. Geometry from
            two sheets compared as one is not a wrong answer but a meaningless one, which is the
            reason `DimensionExtent` carries its provenance at all.
    """
    if not segments:
        return DetectedDimensions(lines=(), chains=(), closures=(), unclassified=())

    scopes = {(extent.document_version_id, extent.page) for extent in segments}
    if len(scopes) > 1:
        raise ValueError(
            "segments span more than one page or document version; a dimension line cannot be "
            "bounded by a witness line from another sheet"
        )

    # **The same stroke drawn twice is one stroke.** This sheet emits many of its lines in
    # duplicate — identical endpoints, identical page — which is ordinary in CAD output and doubled
    # the dimension count until it was handled. Deduplicated by value rather than by identity, and
    # order is preserved so the result does not depend on set iteration.
    #
    # Deduplicating is a claim about the drawing, so it is made here and visibly, rather than left
    # for every caller to discover: two coincident strokes cannot be two different dimensions, and
    # a caller that wanted the raw stroke count still has `segments`.
    seen: set[tuple[Decimal, Decimal, Decimal, Decimal]] = set()
    unique: list[DimensionExtent] = []
    for extent in segments:
        key = (extent.start.x, extent.start.y, extent.end.x, extent.end.y)
        reversed_key = (extent.end.x, extent.end.y, extent.start.x, extent.start.y)
        if key in seen or reversed_key in seen:
            continue
        seen.add(key)
        unique.append(extent)

    by_axis: dict[Axis, list[DimensionExtent]] = {Axis.HORIZONTAL: [], Axis.VERTICAL: []}
    oblique: list[DimensionExtent] = []
    for extent in unique:
        axis = _axis_of(extent, straightness=straightness)
        if axis is None:
            oblique.append(extent)
        else:
            by_axis[axis].append(extent)

    lines: list[DimensionLine] = []
    used: set[int] = set()

    for axis in (Axis.HORIZONTAL, Axis.VERTICAL):
        perpendicular = by_axis[Axis.VERTICAL if axis is Axis.HORIZONTAL else Axis.HORIZONTAL]
        for extent in by_axis[axis]:
            if axis is Axis.HORIZONTAL:
                low, high = min(extent.start.x, extent.end.x), max(extent.start.x, extent.end.x)
                along = extent.start.y
            else:
                low, high = min(extent.start.y, extent.end.y), max(extent.start.y, extent.end.y)
                along = extent.start.x
            if high - low < minimum_span:
                continue

            at_low = [
                witness
                for witness in perpendicular
                if _crosses(
                    witness=witness,
                    at=low,
                    along=along,
                    axis=axis,
                    tolerance=witness_tolerance,
                    crossing_margin=crossing_margin,
                )
            ]
            at_high = [
                witness
                for witness in perpendicular
                if _crosses(
                    witness=witness,
                    at=high,
                    along=along,
                    axis=axis,
                    tolerance=witness_tolerance,
                    crossing_margin=crossing_margin,
                )
            ]
            # **Both ends, or not a dimension.** One witness line is a stroke that happens to touch
            # something. Two, bounding a run, is the drawing saying "this distance, between these
            # two places" — which is the whole content of a dimension.
            if not (at_low and at_high):
                continue

            witnesses = tuple(at_low) + tuple(
                witness for witness in at_high if witness not in at_low
            )
            lines.append(DimensionLine(extent=extent, axis=axis, witness_lines=witnesses))
            used.add(id(extent))
            used.update(id(witness) for witness in witnesses)

    unclassified = tuple(extent for extent in unique if id(extent) not in used)
    return DetectedDimensions(
        lines=tuple(lines),
        chains=_chains(lines, tolerance=witness_tolerance),
        closures=_closures(lines, tolerance=witness_tolerance),
        unclassified=unclassified,
    )


def _interval(line: DimensionLine) -> tuple[Decimal, Decimal]:
    """Where a dimension starts and ends along its own axis, low first."""
    if line.axis is Axis.HORIZONTAL:
        return min(line.extent.start.x, line.extent.end.x), max(
            line.extent.start.x, line.extent.end.x
        )
    return min(line.extent.start.y, line.extent.end.y), max(line.extent.start.y, line.extent.end.y)


def _closures(
    lines: Sequence[DimensionLine], *, tolerance: Decimal
) -> tuple[DimensionClosure, ...]:
    """Find each dimension whose extent is tiled by smaller ones on the same axis.

    **Tiled, not merely contained.** Two sub-dimensions sitting somewhere inside a longer one say
    nothing; they might measure two unrelated features that happen to fall within it. A breakdown is
    parts that start where the overall starts, end where it ends, and meet each other in between
    with nothing left over. That is the drawing asserting these are *the* constituents, and it is
    the only arrangement a closure check can be run against.

    Across offsets on purpose — a drawing puts the overall on one line and the breakdown on another
    above it. Restricting to one offset, as `_chains` does, would find none of them.

    Nothing here reads a value. Whether the parts' stated numbers sum to the overall's stated number
    is arithmetic and `B3`'s to check; this only reports that the drawing laid them out as a
    breakdown, so there is something to check at all.
    """
    found: list[DimensionClosure] = []
    for axis in (Axis.HORIZONTAL, Axis.VERTICAL):
        candidates = [line for line in lines if line.axis is axis]
        for overall in candidates:
            low, high = _interval(overall)
            inside = sorted(
                (
                    other
                    for other in candidates
                    if other is not overall
                    and _interval(other)[0] >= low - tolerance
                    and _interval(other)[1] <= high + tolerance
                    # Strictly shorter, so the same dimension drawn twice is not its own breakdown.
                    and (_interval(other)[1] - _interval(other)[0]) < (high - low) - tolerance
                ),
                key=lambda line: _interval(line)[0],
            )
            parts = _tiling(inside, low=low, high=high, tolerance=tolerance)
            if parts is not None:
                found.append(DimensionClosure(axis=axis, overall=overall, parts=parts))
    return tuple(found)


def _tiling(
    inside: Sequence[DimensionLine], *, low: Decimal, high: Decimal, tolerance: Decimal
) -> tuple[DimensionLine, ...] | None:
    """The subset of `inside` that tiles `low..high` contiguously, or `None`.

    Greedy from the low end, taking the longest run that starts where the last one finished. Greedy
    rather than exhaustive because a sheet offers a handful of candidates and the longest-first rule
    is what a person reads off the drawing: the breakdown is the coarsest set of parts that covers
    the span, not the finest subdivision that happens to fit.

    **One rule does the refusing, not three.** This ended with
    `if abs(position - high) > tolerance or len(chosen) < 2: return None`, and mutation testing
    showed neither clause could be made to matter. The first is unreachable — the loop exits within
    `tolerance` of `high` and every candidate is bounded by it, so the distance can never exceed
    one. The second only ever fired for a single part spanning the whole, which `_closures` has
    already excluded by requiring each part to be strictly shorter.
    """
    chosen: list[DimensionLine] = []
    position = low
    while position < high - tolerance:
        following = [
            line
            for line in inside
            if abs(_interval(line)[0] - position) <= tolerance
            and _interval(line)[1] > position + tolerance
        ]
        if not following:
            return None
        step = max(following, key=lambda line: _interval(line)[1])
        chosen.append(step)
        position = _interval(step)[1]
    return tuple(chosen)


def _chains(lines: Sequence[DimensionLine], *, tolerance: Decimal) -> tuple[DimensionChain, ...]:
    """Group dimension lines drawn end to end along the same axis.

    Collinear and touching, both required. Collinear alone would chain two dimensions on opposite
    sides of a sheet that happen to share a `y`; touching alone would chain a dimension to a
    perpendicular it meets. Together they are the drawing's own statement that these measurements
    continue one another — which is exactly what a closure check needs and the only thing this
    reports about them.
    """
    grouped: list[DimensionChain] = []
    for axis in (Axis.HORIZONTAL, Axis.VERTICAL):
        candidates = [line for line in lines if line.axis is axis]

        def position(line: DimensionLine) -> tuple[Decimal, Decimal]:
            if line.axis is Axis.HORIZONTAL:
                return line.extent.start.y, min(line.extent.start.x, line.extent.end.x)
            return line.extent.start.x, min(line.extent.start.y, line.extent.end.y)

        for line in sorted(candidates, key=position):
            offset, start = position(line)
            placed = False
            for index, chain in enumerate(grouped):
                if chain.axis is not axis:
                    continue
                last = chain.lines[-1]
                last_offset, _ = position(last)
                if abs(last_offset - offset) > tolerance:
                    continue
                end = max(
                    last.extent.start.x if axis is Axis.HORIZONTAL else last.extent.start.y,
                    last.extent.end.x if axis is Axis.HORIZONTAL else last.extent.end.y,
                )
                if abs(end - start) <= tolerance:
                    grouped[index] = DimensionChain(axis=axis, lines=(*chain.lines, line))
                    placed = True
                    break
            if not placed:
                grouped.append(DimensionChain(axis=axis, lines=(line,)))

    # A chain of one is a dimension, not a chain. Reporting it as one would make every dimension on
    # the sheet look like a closure waiting to be validated.
    return tuple(chain for chain in grouped if len(chain.lines) > 1)
