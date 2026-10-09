"""Whether an architect's dimension runs between edges of the casework or to a centre line (D4, #1052).

An architect dimensions a fixture to its **centre line**: the sink's middle to the wall, one outlet
to the next. Such a number is never a cabinet's width, and pairing it with one could pass a wrong
cabinet on a number that measures something else (a `2' - 6"` between two outlets can have the
same value as a vendor's 30" cabinet). So each end tick of an architect's span is checked against
the line-work drawn at it, inside the architect's drawing.

**What an edge looks like, measured on the client's sets.** The architect's extension lines are
dashed and run the whole height of the drawing, through everything, so where an extension line
stops says nothing. What does: walking from the dimension line into the drawing at the tick's x,
the **first solid outline crossed** (the floor line under the cabinets, the ceiling line, the top
of a unit) is where a casework edge starts. A tick on an edge has a solid vertical line starting
there (or within a toe-kick's height of it). A tick on a centre line has nothing starting there:
any solid line at the same x is a coincidence further in — an upper cabinet's joint, a hook, a
door pull — and is not taken.

For each end tick:

* a **centre-line mark** (`CL`, `C L`, `C/L` or `℄`) printed on the tick's line → **not** on the
  outline;
* else, on either side of the dimension line, a solid vertical edge starts at the first solid
  outline crossed → **on** the outline;
* else a solid vertical line drawn at the tick only further in, or only dashed lines → **not**;
* else nothing drawn there → **unclear** (`None`).

A span is on the outline only when both ends are; not, when either end is not; unclear otherwise.
`None` and `False` are both safe: the span is never paired with a cabinet on them.

**Known limit.** A fixture drawn with a solid line on its own centre that starts at the first
outline crossed (a pendant's rod hanging from the ceiling line) reads as an edge. It is reported
with the reason, and the pairing (T2) has its own position check besides.

**What it never does.** It reads no number and decides no role. Coloured ink never reaches it (the
caller passes the drawing's black and grey only), so GV's markup is never an edge.

Pure. Source: issue #1052 · Verification: `tests/extraction/architect/test_outline.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final

from extraction.geometry.rows import Box, PageInk

__all__ = [
    "EndWitness",
    "LineWork",
    "OutlineSettings",
    "Piece",
    "end_witness",
    "line_work",
    "span_on_outline",
]

_TWO: Final = Decimal(2)
_ZERO: Final = Decimal(0)


@dataclass(frozen=True, slots=True)
class OutlineSettings:
    """Every length the witness uses, in page points unless named. Measured on the client's sets."""

    straightness_pt: Decimal
    """How far off the axis a stroke may drift and still be vertical or horizontal."""
    tolerance_pt: Decimal
    """How far from the tick's x a vertical line may be and still be drawn at the tick."""
    row_clearance_pt: Decimal
    """Lines this close to the dimension line belong to it (its ticks, its extension stubs)."""
    join_gap_pt: Decimal
    """A piece starting this close to where another stops continues it as one line."""
    solid_minimum_pt: Decimal
    """A line at least this long is solid line-work; shorter is a dash, a pull or a mark."""
    dash_maximum_pt: Decimal
    dash_minimum_pieces: int
    """A dashed line: at least this many collinear pieces, each no longer than the maximum."""
    mark_reach_pt: Decimal
    """A centre-line mark whose middle is this close to the tick's x sits on its line."""
    link_pt: Decimal
    """An edge starts at an outline when its near end is this close to it..."""
    toe_kick_in: Decimal
    """...or, where the drawing's scale is known, up to this many real inches past it (a cabinet's
    side starts at its toe kick, a little above the floor line)."""


@dataclass(frozen=True, slots=True)
class Piece:
    """One straight vertical or horizontal stroke of the drawing's ink."""

    at: Decimal
    """Its x for a vertical stroke, its y for a horizontal one."""
    start: Decimal
    end: Decimal
    """Its extent along its own axis, `start <= end`."""

    @property
    def length(self) -> Decimal:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class LineWork:
    """A page's straight strokes, split by direction."""

    verticals: tuple[Piece, ...]
    horizontals: tuple[Piece, ...]


@dataclass(frozen=True, slots=True)
class EndWitness:
    on_outline: bool | None
    reason: str


def _add(
    x0: Decimal,
    y0: Decimal,
    x1: Decimal,
    y1: Decimal,
    settings: OutlineSettings,
    verticals: list[Piece],
    horizontals: list[Piece],
) -> None:
    if abs(x0 - x1) <= settings.straightness_pt and y0 != y1:
        verticals.append(Piece((x0 + x1) / _TWO, min(y0, y1), max(y0, y1)))
    elif abs(y0 - y1) <= settings.straightness_pt and x0 != x1:
        horizontals.append(Piece((y0 + y1) / _TWO, min(x0, x1), max(x0, x1)))


def line_work(ink: PageInk, settings: OutlineSettings) -> LineWork:
    """Every vertical and horizontal stroke in the ink: lines, rectangles' sides, polylines' legs."""
    verticals: list[Piece] = []
    horizontals: list[Piece] = []
    for line in ink.lines:
        _add(line.x0, line.y0, line.x1, line.y1, settings, verticals, horizontals)
    for curve in ink.curves:
        points = curve.points + ((curve.points[0],) if curve.closed and curve.points else ())
        for (ax, ay), (bx, by) in pairwise(points):
            _add(ax, ay, bx, by, settings, verticals, horizontals)
    return LineWork(tuple(verticals), tuple(horizontals))


@dataclass(frozen=True, slots=True)
class _Reach:
    """A stroke's extent measured away from the dimension line, on one side of it."""

    near: Decimal
    far: Decimal


def _reach(start: Decimal, end: Decimal, row_y: Decimal, *, above: bool) -> _Reach | None:
    if above and start < row_y:
        return _Reach(max(_ZERO, row_y - end), row_y - start)
    if not above and end > row_y:
        return _Reach(max(_ZERO, start - row_y), end - row_y)
    return None


def _solid_lines(reaches: Sequence[_Reach], settings: OutlineSettings) -> list[_Reach]:
    """Solid lines: one piece, or pieces end to end, at least the solid minimum long.

    End to end only — the next piece starts where the last one stops. Two dashed lines drawn over
    each other a little out of step overlap dash by dash and would otherwise read as one solid line.
    Dashes are not passed in at all: a dash touching an edge's end is an extension line meeting it.
    """
    chains: list[_Reach] = []
    for reach in sorted(reaches, key=lambda reach: (reach.near, reach.far)):
        for index, chain in enumerate(chains):
            if abs(reach.near - chain.far) <= settings.join_gap_pt:
                chains[index] = _Reach(chain.near, reach.far)
                break
        else:
            chains.append(reach)
    return [chain for chain in chains if chain.far - chain.near >= settings.solid_minimum_pt]


def _one_side(
    x: Decimal,
    row_y: Decimal,
    *,
    above: bool,
    view: Box,
    work: LineWork,
    points_per_inch: Decimal | None,
    settings: OutlineSettings,
) -> tuple[EndWitness, bool]:
    """What one side of the dimension line says about an end tick, and whether any outline of the
    drawing is crossed on that side at all (the drawing is on that side)."""
    clearance = settings.row_clearance_pt
    at_x = [
        reach
        for piece in work.verticals
        if abs(piece.at - x) <= settings.tolerance_pt
        and view.x0 <= piece.at <= view.x1
        and piece.end >= view.top
        and piece.start <= view.bottom
        for reach in [_reach(piece.start, piece.end, row_y, above=above)]
        if reach is not None
    ]
    dashes = [reach for reach in at_x if reach.far - reach.near <= settings.dash_maximum_pt]
    solids = _solid_lines(
        [
            reach
            for reach in at_x
            if reach.near > clearance and reach.far - reach.near > settings.dash_maximum_pt
        ],
        settings,
    )
    crossings = sorted(
        abs(piece.at - row_y)
        for piece in work.horizontals
        if piece.start - settings.tolerance_pt <= x <= piece.end + settings.tolerance_pt
        and piece.length >= settings.solid_minimum_pt
        and view.top <= piece.at <= view.bottom
        and (piece.at < row_y - clearance if above else piece.at > row_y + clearance)
    )
    if crossings:
        first = crossings[0]
        allowed = settings.link_pt
        if points_per_inch is not None:
            allowed += settings.toe_kick_in * points_per_inch
        # Solid lines between the dimension line and the drawing's first outline belong to other
        # dimensions and notes, not to the drawing; they are not looked at.
        inside = [solid for solid in solids if solid.near >= first - settings.link_pt]
        if any(solid.near <= first + allowed for solid in inside):
            return (
                EndWitness(
                    True, "a solid edge starts at the first outline crossed from the dimension line"
                ),
                True,
            )
        if inside:
            return (
                EndWitness(
                    False,
                    "the solid line drawn at this end starts well inside the drawing, not at its "
                    "first outline: it shares the x of something the dimension locates",
                ),
                True,
            )
    elif solids:
        return (
            EndWitness(None, "a solid line is drawn at this end but no outline is crossed"),
            False,
        )
    if len(dashes) >= settings.dash_minimum_pieces:
        return (
            EndWitness(
                False,
                "only a dashed line is drawn at this end: a centre line, or a line to something "
                "not drawn at this place",
            ),
            bool(crossings),
        )
    return EndWitness(None, "nothing is drawn at this end"), bool(crossings)


def end_witness(
    x: Decimal,
    row_y: Decimal,
    *,
    view: Box,
    work: LineWork,
    centre_marks: Sequence[Box],
    points_per_inch: Decimal | None,
    settings: OutlineSettings,
) -> EndWitness:
    """What is drawn at one end tick of a span, inside the drawing `view`.

    Both sides of the dimension line are read. When the drawing's outline is crossed on one side
    only, that side decides. Otherwise a side saying the tick is not on an edge wins over a side
    saying it is: when unsure, never paired.
    """
    for mark in centre_marks:
        middle = (mark.x0 + mark.x1) / _TWO
        if view.meets(mark) and abs(middle - x) <= settings.mark_reach_pt:
            return EndWitness(False, "a centre-line mark is printed on this end's line")
    read = [
        _one_side(
            x,
            row_y,
            above=above,
            view=view,
            work=work,
            points_per_inch=points_per_inch,
            settings=settings,
        )
        for above in (True, False)
    ]
    drawing_sides = [witness for witness, crossed in read if crossed]
    sides = drawing_sides if len(drawing_sides) == 1 else [witness for witness, _ in read]
    for side in sides:
        if side.on_outline is False:
            return side
    for side in sides:
        if side.on_outline is True:
            return side
    return EndWitness(None, "no edge or centre line is drawn at this end")


def span_on_outline(left: EndWitness, right: EndWitness) -> bool | None:
    """Both ends on the outline → on it; either end not → not; otherwise unclear."""
    if left.on_outline is False or right.on_outline is False:
        return False
    if left.on_outline is True and right.on_outline is True:
        return True
    return None
