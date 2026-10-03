"""What the parts of a vendor's drawing might be: suggested, for a person to confirm (#868).

Until this module nothing suggested a drawing's parts, so a person would have had to mark every
cabinet by hand. It suggests them from what the drawing already says about itself: the dimensions
it draws end to end along a run, and the cabinet codes it prints. **A suggestion is not a part**
(#852). The caller records each one as a `part_proposals` row and nothing more; only a person's
confirmation makes a drawing item, through `workflow/parts.py:confirm_part`, and a guard test fails
if anything else writes one.

## What is suggested, and from what

*A cabinet* is suggested for each horizontal dimension that belongs to a chain: two or more
dimensions drawn end to end at one offset, as `detect()` groups them (#591). That is how a run of
cabinets is dimensioned. A dimension drawn on its own is not suggested as a cabinet: it could be an
overall, or a box edge the detector took for a dimension (#748), and nothing here can tell which.
The part's left and right are the dimension's ends, and its height is how far the dimension's
extension lines reach.

**Every one is suggested as a cabinet, never as a filler.** A filler is drawn and dimensioned the
same way, and telling the two apart by how narrow the box is would be reading a width off the
geometry. The person confirming the part says which it is, and the reason says so.

**A partial chain suggests only what it saw.** A chain holding two of a run's five cabinets is two
suggestions. Nothing fills the gap or stretches the run to an overall dimension: a guessed part is
one the person would have to notice was guessed.

*A countertop* is suggested only from a horizontal dimension outside every chain that reaches from
the left end of one suggested cabinet to the right end of another, with suggested cabinets drawn end
to end in between. **Its extent is its own dimension's, never the union of the cabinets beneath
it.** The countertop check asks whether a run reaches both ends of its countertop, and a countertop
taken from the run would reach them by construction.

*A code* attaches to a cabinet only when exactly one cabinet code sits inside the cabinet's span
across the page, and that code sits inside no other suggested cabinet's span. Anything else (no code,
two codes, or one code over two cabinets) leaves the cabinet with no code and a reason that says
which. A code is kept as printed. It names a model rather than one cabinet, and nothing here reads a
width out of it (`vocabulary/cabinet_codes.py`).

## Where it looks

Only in the vendor's drawings, and only in those not nested inside another drawing on the page: a
small stamp pasted inside a panel is part of that panel, not a drawing of its own. Which drawing is
the vendor's is the caller's to say. A drawing's dimensions and codes are the ones lying wholly
inside the box around its region.

## The tolerance is the caller's, and there is no default

Whether two ends meet is a question about a tolerance, in stored units: the normalised `0..1` page
space, where one number is a different physical distance on different sheet sizes. It is required
and keyword-only so that no call site acquires one by accident. It is used only to decide whether a
countertop's ends meet its cabinets' ends and whether cabinets meet each other along the way.

Source: issue #868; #748 plan, step 3 · Verification: `tests/extraction/model/test_part_proposals.py`
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon, PolygonSpaceMismatchError
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.dimension_lines import Axis, DetectedDimensions, DimensionLine
from vocabulary.cabinet_codes import CABINET_CODES_VERSION, is_cabinet_code
from vocabulary.part_kinds import PartKind

__all__ = [
    "PROPOSER_SOURCE",
    "PROPOSER_VERSION",
    "PageParts",
    "PrintedText",
    "ProposedPart",
    "ViewOutline",
    "propose_parts",
]

#: What makes these suggestions, as `PartProposal.source` records it.
PROPOSER_SOURCE: Final = "extraction.model.part_proposals"

#: Which version of it, with the code-shape list it used: a different list is a different suggester.
PROPOSER_VERSION: Final = f"1;codes={CABINET_CODES_VERSION}"

#: How many codes a reason names before it only counts the rest, so it fits its 500-character column.
_CODES_NAMED: Final = 3

#: `(left, top, right, bottom)` in stored space, where `y` grows down the page.
type _Box = tuple[Decimal, Decimal, Decimal, Decimal]
type _Span = tuple[Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class ViewOutline:
    """One drawing on the page, as much of it as suggesting parts needs."""

    view_id: UUID
    region: Polygon
    vendor: bool
    """Whether the caller takes this drawing to be the vendor's. Only the vendor's are given
    suggestions; every drawing on the page counts when deciding which ones are nested."""


@dataclass(frozen=True, slots=True)
class PrintedText:
    """One reading of text: the candidate row that recorded it, what it says, and where it sits."""

    candidate_id: UUID
    text: str
    extent: Polygon


@dataclass(frozen=True, slots=True)
class ProposedPart:
    """One suggested part, with everything its `part_proposals` row records."""

    view_id: UUID
    kind: PartKind
    extent: Polygon
    """A box: the defining dimension's ends across the page, its extension lines' reach down it."""
    defining_line: DimensionExtent
    code: PrintedText | None
    reason: str


@dataclass(frozen=True, slots=True)
class PageParts:
    """What one page's drawings were given, and which drawings were left out as nested."""

    parts: tuple[ProposedPart, ...]
    nested: Mapping[UUID, str]
    """Each drawing that lies inside another, with why it was given nothing."""


def propose_parts(
    views: Sequence[ViewOutline],
    detected: DetectedDimensions,
    texts: Sequence[PrintedText],
    *,
    edge_tolerance: Decimal,
) -> PageParts:
    """Suggest the cabinets and countertops of each vendor drawing on one page.

    `views` are every drawing on the page, `detected` is `detect()`'s result for the page's strokes,
    and `texts` are the page's readings a code may come from. Returns the suggestions, ordered by
    drawing as given and then down and across the page, cabinets before countertops.

    Raises `PolygonSpaceMismatchError` if anything comes from another page or document version, and
    `ValueError` if a drawing is listed twice. Both are caller mistakes rather than ambiguity: a part
    suggested from another sheet's strokes is not a wrong answer but a meaningless one.
    """
    _check_tolerance(edge_tolerance)
    _check_one_page(views, detected, texts)

    boxes = {view.view_id: _box(view.region.points) for view in views}
    nested: dict[UUID, str] = {}
    for view in views:
        outer = [
            other.view_id
            for other in views
            if other.view_id != view.view_id
            and _box_within(boxes[view.view_id], boxes[other.view_id])
        ]
        if outer:
            nested[view.view_id] = (
                f"it lies inside {_drawings(len(outer))}, so it belongs to the drawing around it "
                "rather than being one of its own, and whatever lies inside it lies inside that "
                "drawing too"
            )

    chained = frozenset(
        line for chain in detected.chains if chain.axis is Axis.HORIZONTAL for line in chain.lines
    )
    parts: list[ProposedPart] = []
    for view in views:
        if view.vendor and view.view_id not in nested:
            parts.extend(
                _view_parts(
                    view.view_id, boxes[view.view_id], detected, chained, texts, edge_tolerance
                )
            )
    return PageParts(parts=tuple(parts), nested=MappingProxyType(nested))


def _view_parts(
    view_id: UUID,
    box: _Box,
    detected: DetectedDimensions,
    chained: frozenset[DimensionLine],
    texts: Sequence[PrintedText],
    tolerance: Decimal,
) -> list[ProposedPart]:
    """One drawing's suggestions: a cabinet per chained dimension, then the countertops over them."""
    horizontal = sorted(
        (
            line
            for line in detected.lines
            if line.axis is Axis.HORIZONTAL
            and _points_within((line.extent.start, line.extent.end), box)
        ),
        key=_order,
    )
    segments = [line for line in horizontal if line in chained]
    spans = [_span(line) for line in segments]
    codes = [
        text
        for text in texts
        if is_cabinet_code(text.text) and _points_within(text.extent.points, box)
    ]
    over = [[code for code in codes if _inside(code, span)] for span in spans]

    parts = [
        _cabinet(view_id, segment, span, inside, spans)
        for segment, span, inside in zip(segments, spans, over, strict=True)
    ]
    for line in horizontal:
        if line not in chained and _spanned_end_to_end(_span(line), spans, tolerance):
            parts.append(
                ProposedPart(
                    view_id=view_id,
                    kind=PartKind.COUNTERTOP,
                    # Its own dimension and its own extension lines, never the cabinets' outline.
                    extent=_extent(line),
                    defining_line=line.extent,
                    code=None,
                    reason=(
                        "A horizontal dimension on the vendor's drawing that reaches from the left "
                        "end of one suggested cabinet to the right end of another, with suggested "
                        "cabinets drawn end to end in between, which is how a drawing gives the "
                        "overall of a run. Its left and right are this dimension's own ends and its "
                        "height is how far its own extension lines reach, not the cabinets' outline. "
                        "The person confirming it says whether it is the countertop."
                    ),
                )
            )
    return parts


def _cabinet(
    view_id: UUID,
    segment: DimensionLine,
    span: _Span,
    inside: list[PrintedText],
    spans: list[_Span],
) -> ProposedPart:
    """A cabinet suggestion for one chained dimension, with its code when exactly one is its own."""
    code: PrintedText | None = None
    if not inside:
        said = "No cabinet code sits wholly inside its span."
    elif len(inside) > 1:
        said = (
            f"{len(inside)} cabinet codes sit inside its span ({_named(inside)}), so which one is "
            "its own is for the person confirming it."
        )
    else:
        (only,) = inside
        # The span it was found in counts as one of these, so "no other" is exactly one.
        others = sum(1 for other in spans if _inside(only, other)) - 1
        if others:
            elsewhere = (
                "one other suggested cabinet's span"
                if others == 1
                else f"{others} other suggested cabinets' spans"
            )
            said = (
                f"The code {only.text!r} sits inside its span and inside {elsewhere}, so which one "
                "it names is for the person confirming it."
            )
        else:
            code = only
            said = f"The code {only.text!r} sits inside its span and no other suggested cabinet's."
    return ProposedPart(
        view_id=view_id,
        kind=PartKind.CABINET,
        extent=_extent(segment),
        defining_line=segment.extent,
        code=code,
        reason=(
            "A horizontal dimension on the vendor's drawing, one of a chain drawn end to end. Its "
            "ends give the part's left and right, and its extension lines its height. Suggested as "
            "a cabinet, but a filler is drawn the same way, so the person confirming it says which. "
            + said
        ),
    )


def _spanned_end_to_end(overall: _Span, spans: Sequence[_Span], tolerance: Decimal) -> bool:
    """Whether two or more of `spans` reach end to end from one end of `overall` to the other.

    A span that by itself reaches both ends is the same dimension drawn twice, not a breakdown of
    it, and is left out, so every way across uses at least two. Each step must start where the last
    one ended and move on by more than the tolerance, so the search always finishes.
    """
    low, high = overall
    pieces = [
        span
        for span in spans
        if not (abs(span[0] - low) <= tolerance and abs(span[1] - high) <= tolerance)
        and span[0] >= low - tolerance
        and span[1] <= high + tolerance
    ]
    reached = [low]
    seen: set[Decimal] = set()
    while reached:
        position = reached.pop()
        if position in seen:
            continue
        seen.add(position)
        for start, end in pieces:
            if abs(start - position) <= tolerance and end > position + tolerance:
                if abs(end - high) <= tolerance:
                    return True
                reached.append(end)
    return False


def _extent(line: DimensionLine) -> Polygon:
    """The dimension's ends across the page, and how far it and its extension lines reach down it."""
    left, right = _span(line)
    heights = [line.extent.start.y, line.extent.end.y] + [
        point.y for witness in line.witness_lines for point in (witness.start, witness.end)
    ]
    top, bottom = min(heights), max(heights)
    return Polygon(
        points=(
            StoredPoint(left, top),
            StoredPoint(right, top),
            StoredPoint(right, bottom),
            StoredPoint(left, bottom),
        ),
        space="stored",
        document_version_id=line.extent.document_version_id,
        page=line.extent.page,
    )


def _span(line: DimensionLine) -> _Span:
    """Where a horizontal dimension starts and ends across the page, left first."""
    return line.extent.interval("horizontal")


def _order(line: DimensionLine) -> tuple[Decimal, Decimal, Decimal]:
    """Down the page, then across it: the order a person reads the dimension rows in."""
    left, right = _span(line)
    return min(line.extent.start.y, line.extent.end.y), left, right


def _inside(text: PrintedText, span: _Span) -> bool:
    """Whether the text lies wholly inside the span across the page."""
    across = [point.x for point in text.extent.points]
    return span[0] <= min(across) and max(across) <= span[1]


def _box(points: Sequence[StoredPoint]) -> _Box:
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _box_within(inner: _Box, outer: _Box) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _points_within(points: Sequence[StoredPoint], box: _Box) -> bool:
    return all(box[0] <= point.x <= box[2] and box[1] <= point.y <= box[3] for point in points)


def _drawings(count: int) -> str:
    return "another drawing on the page" if count == 1 else f"{count} other drawings on the page"


def _named(codes: Sequence[PrintedText]) -> str:
    named = ", ".join(repr(code.text) for code in codes[:_CODES_NAMED])
    rest = len(codes) - _CODES_NAMED
    return named if rest <= 0 else f"{named} and {rest} more"


def _check_tolerance(tolerance: Decimal) -> None:
    if not isinstance(tolerance, Decimal):
        raise TypeError(
            "edge_tolerance must be a Decimal. A float would let binary rounding decide whether two "
            "dimensions meet."
        )
    if not tolerance.is_finite():
        # NaN fails every comparison, so no ends would ever meet; infinity makes every pair meet.
        # Neither loosens or tightens the question, both stop it being asked.
        raise ValueError("edge_tolerance must be a finite number")
    if tolerance < 0:
        raise ValueError("edge_tolerance cannot be negative")


def _check_one_page(
    views: Sequence[ViewOutline], detected: DetectedDimensions, texts: Sequence[PrintedText]
) -> None:
    """Everything from one page of one document version, and each drawing listed once."""
    ids = [view.view_id for view in views]
    if len(set(ids)) != len(ids):
        raise ValueError("a drawing is listed twice, and would be given every suggestion twice")
    scopes = (
        {(view.region.document_version_id, view.region.page) for view in views}
        | {(text.extent.document_version_id, text.extent.page) for text in texts}
        | {(line.extent.document_version_id, line.extent.page) for line in detected.lines}
    )
    if len(scopes) > 1:
        raise PolygonSpaceMismatchError(
            "the drawings, dimensions and readings come from more than one page or document "
            "version. A part suggested from another sheet's strokes would describe nothing on this "
            "one."
        )
