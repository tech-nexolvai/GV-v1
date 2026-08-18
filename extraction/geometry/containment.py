"""Which item does a dimension span — one cabinet, or a run of three?

A dimension line says `36"`. This module decides *what* is 36 inches. That is a different question
from whether the number was read correctly, and the two failure modes look nothing alike: a misread
digit is caught by the dual-unit lane, while a mis-association produces a finding that is internally
consistent, fully traced and completely wrong. `docs/DESIGN_EXTRACTION.md` §6 names this as the
reason the story exists.

**Single versus run is a whole check, not a detail.** "The countertop is as wide as the cabinets
beneath it" needs to know it is summing three cabinets rather than measuring one. So the distinction
is carried in the type rather than left for a caller to infer from the length of a list.

**Ambiguity returns no association.** §6 fixes this regardless of what the drawings turn out to look
like: an ambiguous association returns *no* association, never the nearest guess. Two candidates
where neither contains the other, a line with no dominant axis, a run with a gap in it — each
refuses, and the refusal carries the candidates so a reviewer can see what the choice was between.

**The tolerance is the caller's, and there is no default.** What endpoint alignment separates "spans
this cabinet" from "spans the run" is empirical, `data/drawings/` is empty, and a default here would
be today's guess shipped as ground truth. It is a required keyword argument so that no call site can
acquire one by accident.

**What this deliberately does not do.** Dimension lines sit *offset* from what they measure, and how
far is a per-vendor empirical fact. Nothing here filters candidates by how near the line is to them
across the axis — so a dimension over one row of cabinets can match an identically-sized row
elsewhere on the sheet. That is B10.5's lane-4 problem. Inventing a proximity constant now would
hide the gap rather than close it.

**Why the input is not #179's `DimensionLine`.** That type belongs to B10.2, and its endpoints are
`PdfPoint` — exact, vector-sourced, deliberately not re-derived through pixels. Containment cannot
be answered there: items carry an `evidence.polygon.Polygon`, which is stored space, and comparing
across coordinate planes raises rather than returning a misleading answer. This module takes the
line's extent already in stored space; B8's `PageTransform` is the bridge.

Source: backend proposal Appendix B stage I, §10.1 · Design: `docs/DESIGN_EXTRACTION.md` §6 ·
Verification: `tests/extraction/geometry/test_containment.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from evidence.coordinates import StoredPoint
from evidence.polygon import PolygonSpaceMismatchError
from extraction.model.items import DrawingItem

type Axis = Literal["horizontal", "vertical"]
type Interval = tuple[Decimal, Decimal]
type SpanResolution = Span | CannotResolve


@dataclass(frozen=True, slots=True)
class DimensionExtent:
    """Where a dimension line starts and ends, in stored page coordinates.

    Carries the document version and page for the same reason `Polygon` does: a comparison against
    geometry from another sheet is not a wrong answer, it is a meaningless one, and it must raise
    rather than resolve.
    """

    start: StoredPoint
    end: StoredPoint
    document_version_id: UUID
    page: int

    def __post_init__(self) -> None:
        for name, point in (("start", self.start), ("end", self.end)):
            if not isinstance(point, StoredPoint):
                raise TypeError(f"{name} must be a StoredPoint")
            if not isinstance(point.x, Decimal) or not isinstance(point.y, Decimal):
                raise TypeError(f"{name} coordinates must be Decimal values")
            if not (Decimal(0) <= point.x <= Decimal(1) and Decimal(0) <= point.y <= Decimal(1)):
                raise ValueError(f"{name} must stay within stored page bounds 0..1")
        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        if isinstance(self.page, bool) or not isinstance(self.page, int):
            raise TypeError("page must be an integer")
        if self.page < 0:
            raise ValueError("page must be zero or greater")
        if self.start == self.end:
            raise ValueError(
                "a dimension line with identical endpoints spans nothing, and has no axis to "
                "resolve against"
            )

    @property
    def axis(self) -> Axis | None:
        """The direction the line runs, or `None` when it runs equally in both.

        `None` is a real answer, not a failure to compute one. A line at exactly 45° gives no reason
        to project onto one axis rather than the other, and picking either would make the result
        depend on which comparison happened to be written first.
        """
        horizontal = abs(self.end.x - self.start.x)
        vertical = abs(self.end.y - self.start.y)
        if horizontal == vertical:
            return None
        return "horizontal" if horizontal > vertical else "vertical"

    def interval(self, axis: Axis) -> Interval:
        """The line's extent along `axis`, low value first."""
        low, high = (
            (self.start.x, self.end.x) if axis == "horizontal" else (self.start.y, self.end.y)
        )
        return (low, high) if low <= high else (high, low)


@dataclass(frozen=True, slots=True)
class Span:
    """What a dimension line measures: one item, or an ordered run of them."""

    extent: DimensionExtent
    items: tuple[DrawingItem, ...]
    kind: Literal["single", "run"]
    rationale: str
    """Why it resolved this way, in plain English — which item was chosen over which, and on what
    grounds. §2.4: a reviewer looking at a finding has to be able to see the reasoning, and "the
    geometry matched" is not reasoning."""

    def __post_init__(self) -> None:
        if self.kind not in ("single", "run"):
            raise ValueError("kind must be 'single' or 'run'")
        if self.kind == "single" and len(self.items) != 1:
            raise ValueError("a single span covers exactly one item")
        if self.kind == "run" and len(self.items) < 2:
            raise ValueError(
                "a run covers two or more items. One item is a single, and calling it a run would "
                "make a rule that sums a run silently sum one thing."
            )
        if not self.rationale.strip():
            raise ValueError("a span must record why it resolved as it did")


@dataclass(frozen=True, slots=True)
class CannotResolve:
    """No association, and the reason. **This is the mark for human confirmation.**

    Returned rather than raised, because being unable to tell which item a dimension belongs to is
    an ordinary outcome on a real drawing, not an error in the code. The candidates come with it so
    a reviewer sees what the choice was between rather than being told only that there was one.
    """

    reason: str
    candidates: tuple[DrawingItem, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a refusal must say what could not be decided")


def resolve_span(
    extent: DimensionExtent,
    items: tuple[DrawingItem, ...],
    *,
    endpoint_tolerance: Decimal,
) -> SpanResolution:
    """Decide what `extent` measures, or refuse.

    `endpoint_tolerance` is in stored units — the same normalised 0..1 space as the coordinates — and
    is required. See the module docstring for why it has no default.

    Raises `PolygonSpaceMismatchError` if any item is on another page or from another document
    version. That is not ambiguity to be marked for review; it is a caller mistake, and answering it
    would mean comparing geometry from two different sheets.
    """
    _check_tolerance(endpoint_tolerance)
    _require_same_page(extent, items)

    axis = extent.axis
    if axis is None:
        return CannotResolve(
            "the line runs equally in both directions, so there is no axis to measure along. "
            "Choosing one would make the answer depend on which comparison ran first.",
            items,
        )

    line = extent.interval(axis)
    projected = [(item, _project(item, axis)) for item in items]

    aligned = [item for item, span in projected if _aligns(span, line, endpoint_tolerance)]
    if aligned:
        return _resolve_single(extent, aligned)

    return _resolve_run(extent, axis, projected, line, endpoint_tolerance)


def _check_tolerance(tolerance: Decimal) -> None:
    if not isinstance(tolerance, Decimal):
        raise TypeError(
            "endpoint_tolerance must be a Decimal. A float tolerance would make the boundary "
            "between 'spans this cabinet' and 'spans the run' depend on binary rounding."
        )
    if not tolerance.is_finite():
        # NaN slipped through every check below it: `Decimal("NaN") < 0` is False, and so is every
        # `abs(...) <= NaN` afterwards, so a line sitting exactly on an item refused with the reason
        # "no item starts and ends where the dimension line does". A refusal naming the wrong cause
        # is worse than a crash — a reviewer goes looking for a missing cabinet. Infinity is the
        # mirror image: every item aligns, and the first one found wins.
        raise ValueError(
            "endpoint_tolerance must be a finite number. A NaN or infinite tolerance does not "
            "widen or narrow the match, it removes it — every comparison silently answers the same "
            "way and the refusal names a cause that is not the real one."
        )
    if tolerance < 0:
        raise ValueError("endpoint_tolerance cannot be negative")


def _require_same_page(extent: DimensionExtent, items: tuple[DrawingItem, ...]) -> None:
    for item in items:
        if (
            item.extent.document_version_id != extent.document_version_id
            or item.extent.page != extent.page
        ):
            raise PolygonSpaceMismatchError(
                "the dimension line and the items must share document version and page"
            )


def _project(item: DrawingItem, axis: Axis) -> Interval:
    """The item's extent along `axis`, low value first."""
    values = [point.x if axis == "horizontal" else point.y for point in item.extent.points]
    return (min(values), max(values))


def _describe(item: DrawingItem, span: Interval) -> str:
    """An item a reviewer can pick out of a drawing.

    The type alone is not enough: a run of three base cabinets is three `CT001`s, and "a gap between
    the CT001 and the CT001" tells nobody which pair to look at. Where it sits along the dimension
    does.
    """
    return f"the {item.item_type.value} from {span[0]} to {span[1]}"


def _aligns(span: Interval, line: Interval, tolerance: Decimal) -> bool:
    """Whether both ends of `span` sit on both ends of `line`, within tolerance."""
    return abs(span[0] - line[0]) <= tolerance and abs(span[1] - line[1]) <= tolerance


def _resolve_single(extent: DimensionExtent, aligned: list[DrawingItem]) -> SpanResolution:
    """One aligned item is the answer. Several means nesting — or ambiguity."""
    if len(aligned) == 1:
        item = aligned[0]
        return Span(
            extent=extent,
            items=(item,),
            kind="single",
            rationale=(
                f"one item ({item.item_type.value}) starts and ends where the dimension line does, "
                "within the stated endpoint tolerance"
            ),
        )

    innermost = _most_specific(aligned)
    if innermost is None:
        return CannotResolve(
            f"{len(aligned)} items line up with the dimension equally well and none is inside "
            "another, so there is no ground for preferring one. The nearest guess is exactly what "
            "must not be returned here.",
            tuple(aligned),
        )

    others = [item for item in aligned if item is not innermost]
    return Span(
        extent=extent,
        items=(innermost,),
        kind="single",
        rationale=(
            f"{len(aligned)} items line up with the dimension. The {innermost.item_type.value} was "
            f"chosen because it lies inside the {'other' if len(others) == 1 else 'others'} "
            f"({', '.join(sorted(item.item_type.value for item in others))}), making it the most "
            "specific item the dimension can be about"
        ),
    )


def _most_specific(candidates: list[DrawingItem]) -> DrawingItem | None:
    """The one candidate contained by every other, or `None` if there isn't exactly one.

    Decided by actual polygon containment rather than by area. Two items can have equal area and no
    containment relationship at all, and picking the smaller would be answering a question nobody
    asked — the drawing says which is inside which.
    """
    inside_all = [
        candidate
        for candidate in candidates
        if all(
            other.extent.contains(candidate.extent)
            for other in candidates
            if other is not candidate
        )
    ]
    return inside_all[0] if len(inside_all) == 1 else None


def _resolve_run(
    extent: DimensionExtent,
    axis: Axis,
    projected: list[tuple[DrawingItem, Interval]],
    line: Interval,
    tolerance: Decimal,
) -> SpanResolution:
    """A contiguous group of items whose combined extent is what the line measures."""
    within = sorted(
        (
            (item, span)
            for item, span in projected
            if span[0] >= line[0] - tolerance and span[1] <= line[1] + tolerance
        ),
        key=lambda pair: (pair[1][0], pair[1][1]),
    )
    if len(within) < 2:
        return CannotResolve(
            "no item and no group of adjacent items starts and ends where the dimension line does",
            tuple(item for item, _ in projected),
        )

    members = [item for item, _ in within]
    spans = [span for _, span in within]

    if not _aligns((spans[0][0], spans[-1][1]), line, tolerance):
        return CannotResolve(
            "the items under the dimension line do not reach both of its ends, so what it measures "
            "is not any of them and not all of them together",
            tuple(members),
        )

    # Signed, not absolute. The first version compared `abs(...)` and so reported an overlap as a
    # gap, leaving the overlap branch below unreachable — a reviewer would have been sent looking
    # for a missing cabinet that was never missing.
    overlapping = [
        index
        for index in range(len(within) - 1)
        if spans[index][1] - spans[index + 1][0] > tolerance
    ]
    if overlapping:
        index = overlapping[0]
        return CannotResolve(
            f"{_describe(members[index], spans[index])} and "
            f"{_describe(members[index + 1], spans[index + 1])} overlap along the dimension rather "
            "than sitting side by side, so summing them would count the shared part twice",
            tuple(members),
        )

    gaps = [
        index
        for index in range(len(within) - 1)
        if spans[index + 1][0] - spans[index][1] > tolerance
    ]
    if gaps:
        index = gaps[0]
        return CannotResolve(
            f"the items under the dimension line are not adjacent — there is a gap between "
            f"{_describe(members[index], spans[index])} and "
            f"{_describe(members[index + 1], spans[index + 1])} wider than the stated tolerance, so "
            "they are not one run and something between them is missing or unread",
            tuple(members),
        )

    return Span(
        extent=extent,
        items=tuple(members),
        kind="run",
        rationale=(
            f"{len(members)} adjacent items ({', '.join(item.item_type.value for item in members)}) "
            f"together start and end where the dimension line does, and no single item does. "
            f"The dimension measures the run, not any one of them"
        ),
    )
