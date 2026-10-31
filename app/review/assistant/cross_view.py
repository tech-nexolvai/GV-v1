"""The same span in two views (plan Phase 3, step 2, option 1, 2026-11-01), $0, no AI.

Sizes are 21 of the 50 issues GV's senior reviewer marked on the four tuned sheets, and many of
them are "the same object, two views, two numbers": a bay printed one width in the top view and
another in the front view drawn under it, the overall of the run printed differently in the two,
an overall height printed in two elevations and missing from the third. Models cannot ground such cross-view mismatches
(CrossProjection 2026, INDUSTRY-B M3): code must find which sizes measure the same thing. Only 17
of 177 sizes could be tied to a named part (`dim_parts`), because this vendor names parts by finish
tag, so this module ties sizes by **where their edges are**, the way orthographic views are drawn:

1. **The page's views** (`view_questions.views_of`) and, in each, its dimensions that prove their
   span (`page_views`): a `dims.json` label with its dimension line and both extension lines, drawn to
   its own value at its view's scale (`vendor_consistency`'s scale check: the scale at least
   `MIN_SCALE_SUPPORT` of the view's printed sizes share), or a slip of a few inches printed over
   the middle of its line (a typed-over value: exactly what this check is for). A label whose line
   is far from its value sits on another dimension's line and proves nothing. Each end of a usable
   dimension is an **edge station** of its view along the dimension's axis.
2. **Which views share an axis** (`_pairs`): a plan and an elevation share their widths (vertical
   edges, x); elevations and sections share their heights (horizontal edges, y). Two elevations
   never share widths (they are different walls), a plan never shares its depth with a section's
   height. Detail views are left out.
3. **Matching the edges** (`align`), in inches at each view's scale:
   - **Aligned** views (drawn one above the other, or side by side, at one scale, with at least two
     edges already lined up): an edge of one and an edge of the other are one when within
     `EDGE_IN` (1/16") on the sheet (projection, never another offset), at least `MIN_ALIGNED`
     edges in all. One piece may be drawn longer in one view than in the other (the bay above):
     the edges after it then match each other at a constant offset (by at most `STRETCH_IN`, at
     most `MAX_STRETCHES` times), each run of matched edges at least two long.
   - **Pasted** views (not lined up on the sheet, or at two scales): the same sequence of widths,
     edge after edge within `EDGE_IN`, at least `MIN_PASTED` edges, at one offset.
   - In both, **only when unique**: a second, different matching as good as the best (repeated
     equal cabinets) means no match at all. A wrong match is worse than none.
4. **The same span** (`spans`): a dimension of one view between two matched edges and a dimension
   of the other view between the two edges they match measure the same thing. Printed values that
   differ make a question ("same span: TOP VIEW 48", FRONT VIEW 49""); a span dimensioned in
   one view only says nothing (too noisy), unless it is a view's **overall** (its dimension over all
   its edges), the other view lines up with it by projection and prints no size of that value at
   all (an overall height in two elevations and not in the third).
5. **Across pages** (`page_pairs`): the item's other drawing (`SetContext.pair_pages`) is matched
   the pasted way only (no shared sheet), and only when one view of it matches.

**Red stays off** (`CROSS_VIEW_RED`). Every difference is a numbered please-check question for the
page reviewer (`question_blocks`, after `view_questions`' and `dim_parts`' own). A code-proved red
would need two code-read printed values (text layer, read cleanly) over the same span proved by
projection (aligned views, both ends in the run at offset 0, no stretch): `SpanPair.code_proved`,
`red_ready`. It is ready behind the constant and stays off until a measured run shows 0 false reds.

Pure code on the vendor layer's labels, lines and text (GV's markup is never read). Logs per page:
`spans_matched`, `span_disagreements`. Verification: `tests/review/assistant/test_cross_view.py`
(made-up geometry and values only).
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from importlib import import_module
from typing import Any, Final

from app.review.assistant.ledger import format_inches
from app.review.assistant.untrusted import fence

__all__ = [
    "CROSS_VIEW_RED",
    "EDGE_IN",
    "MAX_STRETCHES",
    "MIN_ALIGNED",
    "MIN_PASTED",
    "STRETCH_IN",
    "Dim",
    "Match",
    "PageViews",
    "SpanPair",
    "ViewDims",
    "align",
    "differences",
    "page_pairs",
    "page_views",
    "question_blocks",
    "red_ready",
    "spans",
]

_log = logging.getLogger(__name__)

type Box = tuple[float, float, float, float]

#: A difference between the same span in two views is NOT a red finding yet: a red needs a measured
#: run with 0 false reds first (the owner's zero-false-red rule). Flip only after that run, and wire
#: `red_ready` into the finding path in the same reviewed change.
CROSS_VIEW_RED: Final = False

#: Two edges of two views are one when within this, at the drawing's scale (inches).
EDGE_IN: Final = Fraction(1, 16)
#: Two views are drawn at one scale when their scales differ by at most this share.
SAME_SCALE_SHARE: Final = 0.01
#: Lined-up edges two views need when aligned on the sheet (projection), or when pasted (by the
#: sequence of their widths, at any offset).
MIN_ALIGNED: Final = 3
MIN_PASTED: Final = 3
#: In aligned views one piece may be drawn longer in one view (the edges after it line up again at
#: a constant offset): by at most this (inches) and this share of the piece, at most this often.
STRETCH_IN: Final = Fraction(2)
STRETCH_SHARE: Final = Fraction(1, 10)
MAX_STRETCHES: Final = 1
#: A label not drawn to its value may still be a typed-over value of its own line: printed over the
#: middle of its line, within max(3", 10 %) of what the line measures (`vendor_consistency`'s slip).
SLIP_IN: Final = Fraction(3)
SLIP_SHARE: Final = Fraction(1, 10)
#: A label off its view's scale by more than max(3/16", 1 %) is not drawn to its value.
OFF_IN: Final = Fraction(3, 16)
OFF_SHARE: Final = Fraction(1, 100)
#: A dimension belongs to the view whose box holds its line, else the nearest within this (points).
ASSIGN_PT: Final = 40.0
#: An untitled view printing an upright size of at least this is an elevation (a cabinet's height).
TALL_IN: Final = Fraction(30)
#: Questions this module adds to a page's list, at most; other drawings of the item read, at most.
MAX_QUESTIONS: Final = 8
MAX_OTHER_PAGES: Final = 3

#: What a dimension measures, by its view's kind and its axis on the page (h: across, v: up).
MEASURES: Final[Mapping[tuple[str, str], str]] = {
    ("plan", "h"): "width",
    ("plan", "v"): "depth",
    ("elevation", "h"): "width",
    ("elevation", "v"): "height",
    ("section", "h"): "depth",
    ("section", "v"): "height",
    ("view", "h"): "width",
}


# ---- the views and their dimensions ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dim:
    """A dimension that proves its span: printed value, its line's ends along its axis (page
    points), and its length drawn at its view's scale (inches)."""

    id: str
    page: int
    text: str
    inches: Fraction
    axis: str
    start: float
    end: float
    box: Box
    route: str
    clean: bool
    drawn: Fraction
    on_scale: bool

    @property
    def value(self) -> str:
        return format_inches(self.inches)

    @property
    def read_by_code(self) -> bool:
        """Read cleanly from the text layer (not OCR, not a fragment): what a red may rest on."""
        return self.route == "text" and self.clean


@dataclass(slots=True)
class ViewDims:
    """One view of a page: its name, kind, box, scale (points per inch) and usable dimensions; and
    every value printed in it (`printed`), for the overall check."""

    page: int
    name: str
    kind: str
    box: Box
    ppi: float | None = None
    dims: list[Dim] = field(default_factory=list)
    printed: set[Fraction] = field(default_factory=set)

    def along(self, axis: str) -> list[Dim]:
        return [d for d in self.dims if d.axis == axis]

    def measure(self, axis: str) -> str | None:
        return MEASURES.get((self.kind, axis))

    @property
    def where(self) -> str:
        return f"p{self.page} {self.name}"


@dataclass(slots=True)
class PageViews:
    """A page's views with their usable dimensions (`page_views`)."""

    page: int
    views: list[ViewDims] = field(default_factory=list)


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1


def _gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _reach(raw: Mapping[str, Any]) -> Box | None:
    """The box of a label's dimension line and its extension lines (the object it measures)."""
    points: list[tuple[float, float]] = []
    for line in [raw.get("dim_line")] + list(raw.get("extension") or []):
        if not isinstance(line, Sequence):
            continue
        for p in line:
            try:
                points.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError, IndexError):
                continue
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _owner(reach: Box, views: Sequence[Any]) -> int | None:
    """The view a dimension belongs to: the one nearest its line and extension lines (a gap of 0
    when they reach into it), within `ASSIGN_PT`; two equally near: none."""
    near = sorted((_gap(reach, v.box), n) for n, v in enumerate(views))
    near = [g for g in near if g[0] <= ASSIGN_PT]
    if not near or (len(near) > 1 and near[1][0] - near[0][0] < 1.0):
        return None
    return near[0][1]


def _drawn(span: float, ppi: float) -> Fraction:
    return Fraction(span / ppi).limit_denominator(1024)


def _off(printed: Fraction, drawn: Fraction, absolute: Fraction, share: Fraction) -> bool:
    return abs(drawn - printed) > max(absolute, share * printed)


def page_views(
    page: int,
    labels: Sequence[Mapping[str, Any]],
    found: Sequence[Mapping[str, Any]],
    segments: Sequence[Any] | None,
    size: tuple[float, float] | None = None,
) -> PageViews:
    """The page's views (`view_questions.views_of`, details left out) with the dimensions that
    prove their span (see the module's step 1). Pure: labels (`dims.json`), phrases and line work
    in."""
    questions = import_module("app.review.assistant.view_questions")
    consistency = import_module("app.review.assistant.vendor_consistency")
    views, assigned = questions.views_of(found, segments, size)
    out = PageViews(page)
    every: list[list[Any]] = [[] for _ in views]
    for raw in labels:
        if not isinstance(raw, Mapping) or isinstance(raw.get("derived"), Mapping):
            continue
        label = consistency.read_label(raw, str(raw.get("file") or ""), page)
        if label is None:
            continue
        reach = _reach(raw) or label.box
        n = _owner(reach, views)
        if n is None or views[n].kind == "detail":
            continue
        every[n].append(label)
    for n, view in enumerate(views):
        if view.kind == "detail":
            continue
        mine = every[n]
        found_scale = consistency._scale(mine, mine) if mine else None
        ppi = float(found_scale[0]) if found_scale else None
        vd = ViewDims(page, str(view.name), str(view.kind), _box(view.box) or (0, 0, 0, 0), ppi)
        vd.printed = {label.inches for label in mine}
        for item, owner in assigned:
            if owner == n:
                vd.printed.update(
                    Fraction(v) for v in questions.inch_values(str(item.get("text") or ""))
                )
        if ppi is not None:
            for label in mine:
                if len(label.raw.get("extension") or []) != 2 or label.raw.get("scale_ok") is False:
                    continue
                drawn = _drawn(label.span, ppi)
                on_scale = not _off(label.inches, drawn, OFF_IN, OFF_SHARE)
                if not on_scale and (
                    _off(label.inches, drawn, SLIP_IN, SLIP_SHARE) or not label.centred()
                ):
                    continue
                vd.dims.append(
                    Dim(
                        id=label.id,
                        page=page,
                        text=label.text,
                        inches=label.inches,
                        axis=label.orientation,
                        start=label.start,
                        end=label.end,
                        box=label.box,
                        route=label.route,
                        clean=label.clean,
                        drawn=drawn,
                        on_scale=on_scale,
                    )
                )
        if vd.kind == "view" and any(d.axis == "v" and d.inches >= TALL_IN for d in vd.dims):
            vd.kind = "elevation"
        out.views.append(vd)
    return out


# ---- matching the edges of two views --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Match:
    """Two views' matched edges: (station of one, station of the other, run), the stations in
    inches, and how they were matched (`aligned` on the sheet, or `pasted` by their widths)."""

    pairs: tuple[tuple[int, int, int], ...]
    a: tuple[float, ...]
    b: tuple[float, ...]
    how: str
    offsets: tuple[float, ...] = ()
    """Each run's offset (inches): 0 for the projection run of aligned views."""

    def span_how(self, run_a: int, run_b: int) -> str:
        """How a span between edges of these runs was matched: `aligned` (both ends in the run
        at offset 0 of aligned views: projection), `shifted` (both in the run after a stretch),
        `stretched` (a stretched piece lies between them) or `pasted`."""
        if run_a != run_b:
            return "stretched"
        if self.how == "pasted":
            return "pasted"
        return "aligned" if abs(self.offsets[run_a]) <= float(EDGE_IN) else "shifted"

    def partner(self, i: int) -> tuple[int, int] | None:
        """The other view's station matched with station `i`, and its run."""
        for pi, pj, run in self.pairs:
            if pi == i:
                return pj, run
        return None


def _stations(values: Sequence[float]) -> list[float]:
    """Sorted, one per place (within `EDGE_IN`)."""
    out: list[float] = []
    tol = float(EDGE_IN)
    for v in sorted(values):
        if out and v - out[-1] <= tol:
            continue
        out.append(v)
    return out


def _index(stations: Sequence[float], at: float) -> int | None:
    tol = float(EDGE_IN)
    best = min(range(len(stations)), key=lambda k: abs(stations[k] - at), default=None)
    if best is None or abs(stations[best] - at) > tol:
        return None
    return best


def align(
    a: Sequence[float],
    b: Sequence[float],
    *,
    aligned: bool,
) -> Match | None:
    """The one best matching of two views' edge stations (sorted inches): a chain of station pairs,
    increasing in both views, in runs at one offset each (`EDGE_IN`), each run at least two pairs.

    `aligned`: the views line up on the sheet at one scale, so the chain must hold a run at offset
    0 (projection) and may stretch once (`MAX_STRETCHES`, `STRETCH_IN`) to a run at another offset;
    at least `MIN_ALIGNED` pairs. Otherwise (pasted): one run at any offset (the same sequence of
    widths), at least `MIN_PASTED` pairs. None when nothing reaches that, or when another chain is
    as good (the matching is not unique)."""
    tol = float(EDGE_IN)
    need = MIN_ALIGNED if aligned else MIN_PASTED
    raw = [(i, j, b[j] - a[i]) for i in range(len(a)) for j in range(len(b))]
    shifts = sorted(p[2] for p in raw)
    # A pair is worth keeping when another pair has its offset (one lone edge proves nothing).
    pairs = [
        p for p in raw if bisect_right(shifts, p[2] + tol) - bisect_left(shifts, p[2] - tol) >= 2
    ]
    pairs.sort(key=lambda p: (p[0], p[1]))
    stretches = MAX_STRETCHES if aligned else 0
    # State: (pair, run length capped at 2, stretches used, a run at offset 0 of 2+ done).
    # Value: (pairs in the chain, ways to reach it, back pointer).
    State = tuple[int, int, int, bool]
    best: dict[State, tuple[int, int, State | None]] = {}
    for k, (i, j, s) in enumerate(pairs):
        candidates: list[tuple[int, int, State | None, State]] = []
        start: State = (k, 1, 0, False)
        candidates.append((1, 1, None, start))
        for prev, (count, ways, _back) in list(best.items()):
            q = prev[0]
            qi, qj, qs = pairs[q]
            if qi >= i or qj >= j:
                continue
            _qk, run, used, zero = prev
            if abs(s - qs) <= tol:
                state: State = (k, 2, used, zero or abs(s) <= tol)
                candidates.append((count + 1, ways, prev, state))
                continue
            if run < 2 or used >= stretches:
                continue
            piece_a, piece_b = a[i] - a[qi], b[j] - b[qj]
            limit = min(float(STRETCH_IN), float(STRETCH_SHARE) * min(piece_a, piece_b))
            if abs(piece_b - piece_a) > limit:
                continue
            candidates.append((count + 1, ways, prev, (k, 1, used + 1, zero)))
        for count, ways, back, state in candidates:
            old = best.get(state)
            if old is None or count > old[0]:
                best[state] = (count, ways, back)
            elif count == old[0]:
                best[state] = (count, old[1] + ways, old[2])
    ends = [
        (value[0], value[1], state)
        for state, value in best.items()
        if state[1] == 2 and (state[3] or not aligned)
    ]
    if not ends:
        return None
    top = max(count for count, _w, _s in ends)
    winners = [(ways, state) for count, ways, state in ends if count == top]
    if top < need or sum(w for w, _ in winners) != 1:
        return None
    chain: list[tuple[int, int, float]] = []
    state_or_none: State | None = winners[0][1]
    while state_or_none is not None:
        chain.append(pairs[state_or_none[0]])
        state_or_none = best[state_or_none][2]
    chain.reverse()
    out: list[tuple[int, int, int]] = []
    offsets: list[float] = []
    for n, (i, j, s) in enumerate(chain):
        if not n or abs(s - chain[n - 1][2]) > tol:
            offsets.append(s)
        out.append((i, j, len(offsets) - 1))
    how = "aligned" if aligned else "pasted"
    return Match(tuple(out), tuple(a), tuple(b), how, tuple(offsets))


# ---- the same span in two views -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpanPair:
    """One span dimensioned in two views (`first`, `second`: the dimensions over it in each, each
    side printing one value), or a view's overall the other view does not print (`second` empty).
    """

    measure: str
    first: ViewDims
    second: ViewDims
    a: tuple[Dim, ...]
    b: tuple[Dim, ...]
    how: str
    """`aligned` (projection), `shifted` (edge for edge after a piece one view draws longer),
    `stretched` (that piece lies between its ends) or `pasted` (the same sequence of widths)."""

    @property
    def differs(self) -> bool:
        return bool(self.b) and self.a[0].inches != self.b[0].inches

    @property
    def missing(self) -> bool:
        return not self.b

    @property
    def code_proved(self) -> bool:
        """Two code-read printed values over one span proved by projection disagree: what a red
        would rest on (`red_ready`; off, `CROSS_VIEW_RED`)."""
        return (
            self.differs
            and self.how == "aligned"
            and any(d.read_by_code for d in self.a)
            and any(d.read_by_code for d in self.b)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "measure": self.measure,
            "how": self.how,
            "first": self.first.where,
            "first_value": self.a[0].value,
            "first_dims": [d.id for d in self.a],
            "second": self.second.where,
            "second_value": self.b[0].value if self.b else None,
            "second_dims": [d.id for d in self.b],
            "differs": self.differs,
            "missing": self.missing,
            "code_proved": self.code_proved,
        }


def _scales(first: ViewDims, second: ViewDims) -> tuple[float, float] | None:
    """The points per inch each view's stations are read at: one shared scale when they agree
    within `SAME_SCALE_SHARE` (so lined-up edges stay lined up), else each its own."""
    if first.ppi is None or second.ppi is None:
        return None
    if abs(first.ppi / second.ppi - 1) <= SAME_SCALE_SHARE:
        both = (first.ppi + second.ppi) / 2
        return both, both
    return first.ppi, second.ppi


def _lined_up(first: ViewDims, second: ViewDims, axis: str) -> bool:
    """The two views overlap across the axis they share (one above the other for widths, side by
    side for heights): their edges can project onto each other."""
    lo, hi = (0, 2) if axis == "h" else (1, 3)
    return min(first.box[hi], second.box[hi]) - max(first.box[lo], second.box[lo]) > 0


def _one_value(dims: Sequence[Dim]) -> bool:
    return len({d.inches for d in dims}) == 1


def _overall(dims: Sequence[Dim]) -> list[Dim]:
    """A view's overall along one axis: its dimensions over all its edges."""
    if len(dims) < 2:
        return []
    lo, hi = min(d.start for d in dims), max(d.end for d in dims)
    return [d for d in dims if d.start <= lo + 0.1 and d.end >= hi - 0.1]


def spans(
    first: ViewDims, second: ViewDims, axis: str, axis_b: str | None = None, *, same_page: bool
) -> tuple[Match | None, list[SpanPair]]:
    """The spans dimensioned in both views (and an overall printed in one only), by their matched
    edges (`align`)."""
    axis_b = axis_b or axis
    measure = first.measure(axis)
    if measure is None or measure != second.measure(axis_b):
        return None, []
    scales = _scales(first, second)
    dims_a, dims_b = first.along(axis), second.along(axis_b)
    if scales is None or not dims_a or not dims_b:
        return None, []
    st_a = _stations([e / scales[0] for d in dims_a for e in (d.start, d.end)])
    st_b = _stations([e / scales[1] for d in dims_b for e in (d.start, d.end)])
    aligned = (
        same_page
        and axis == axis_b
        and scales[0] == scales[1]
        and _lined_up(first, second, axis)
        # Two edges already lined up on the sheet: the views are projected, never matched at
        # another offset (equal cabinets repeat their widths at any of them).
        and sum(1 for x in st_a if _index(st_b, x) is not None) >= 2
    )
    match = align(st_a, st_b, aligned=aligned)
    if match is None:
        return None, []

    def ends(d: Dim, stations: Sequence[float], ppi: float) -> tuple[int, int] | None:
        i, j = _index(stations, d.start / ppi), _index(stations, d.end / ppi)
        return (i, j) if i is not None and j is not None else None

    over_b: dict[tuple[int, int], list[Dim]] = {}
    for d in dims_b:
        e = ends(d, st_b, scales[1])
        if e is not None:
            over_b.setdefault(e, []).append(d)
    over_a: dict[tuple[int, int], list[Dim]] = {}
    for d in dims_a:
        e = ends(d, st_a, scales[0])
        if e is not None:
            over_a.setdefault(e, []).append(d)
    out: list[SpanPair] = []
    for (i1, i2), here in over_a.items():
        p1, p2 = match.partner(i1), match.partner(i2)
        if p1 is None or p2 is None or not _one_value(here):
            continue
        there = over_b.get((p1[0], p2[0]), [])
        if there and not _one_value(there):
            continue
        how = match.span_how(p1[1], p2[1])
        if there:
            out.append(SpanPair(measure, first, second, tuple(here), tuple(there), how))
        elif (
            how == "aligned"
            and set(here) & set(_overall(dims_a))
            and here[0].inches not in second.printed
        ):
            out.append(SpanPair(measure, first, second, tuple(here), (), how))
    back = {pj: (pi, run) for pi, pj, run in match.pairs}
    for (j1, j2), there in over_b.items():
        if not set(there) & set(_overall(dims_b)) or not _one_value(there):
            continue
        q1, q2 = back.get(j1), back.get(j2)
        if q1 is None or q2 is None or (q1[0], q2[0]) in over_a:
            continue
        if there[0].inches in first.printed:
            continue
        how = match.span_how(q1[1], q2[1])
        if how != "aligned":
            continue
        out.append(SpanPair(measure, second, first, tuple(there), (), how))
    return match, out


def _pairs(page: PageViews) -> list[tuple[ViewDims, ViewDims, str]]:
    """The page's view pairs that share an axis: a plan and an elevation (widths, h), two views
    showing heights (elevations and sections, v)."""
    out: list[tuple[ViewDims, ViewDims, str]] = []
    views = page.views
    for n, first in enumerate(views):
        for second in views[n + 1 :]:
            kinds = {first.kind, second.kind}
            if "plan" in kinds and kinds & {"elevation", "view"} and len(kinds) == 2:
                out.append((first, second, "h"))
            if kinds <= {"elevation", "section"}:
                out.append((first, second, "v"))
    return out


def page_pairs(own: PageViews, others: Sequence[PageViews] = ()) -> tuple[list[SpanPair], int]:
    """Every span the page's views share (and, the pasted way, with the item's other drawings,
    `others`), and how many view pairs matched. Across pages a view must match one view only."""
    found: list[SpanPair] = []
    matched = 0
    for first, second, axis in _pairs(own):
        match, made = spans(first, second, axis, same_page=True)
        matched += match is not None
        found += made
    for other in others:
        for mine in own.views:
            for axis in ("h", "v"):
                hits: list[list[SpanPair]] = []
                for theirs in other.views:
                    measure = mine.measure(axis)
                    if measure is None or theirs.measure(axis) != measure:
                        continue
                    if measure == "width" and "plan" not in {mine.kind, theirs.kind}:
                        continue
                    match, made = spans(mine, theirs, axis, same_page=False)
                    if match is not None:
                        hits.append(made)
                if len(hits) == 1:
                    matched += 1
                    found += hits[0]
    return found, matched


_ORDER: Final[Mapping[str, int]] = {"aligned": 0, "stretched": 1, "shifted": 2, "pasted": 3}


def differences(found: Sequence[SpanPair]) -> list[SpanPair]:
    """The spans to ask about, each once: two printed values first (projection first), then
    overalls one view does not print (one question per view and value, however many views print
    it)."""
    out: list[SpanPair] = []
    seen: set[tuple[Any, ...]] = set()
    ranked = sorted(
        (p for p in found if p.differs or p.missing),
        key=lambda p: (p.missing, _ORDER.get(p.how, 9)),
    )
    for pair in ranked:
        if pair.missing:
            key: tuple[Any, ...] = (pair.second.where, pair.measure, pair.a[0].inches)
        else:
            key = tuple(sorted((d.page, d.id) for d in pair.a + pair.b))
        if key not in seen:
            seen.add(key)
            out.append(pair)
    return out


def red_ready(found: Sequence[SpanPair]) -> list[SpanPair]:
    """The differences a red could rest on (`SpanPair.code_proved`), only when `CROSS_VIEW_RED` is
    on. Off until a measured run shows 0 false reds; nothing calls it for a finding yet."""
    if not CROSS_VIEW_RED:
        return []
    return [p for p in found if p.code_proved]


# ---- the page reviewer's questions ----------------------------------------------------------------


def _b(box: Sequence[float]) -> str:
    return ",".join(f"{v:.0f}" for v in box)


_HOW: Final[Mapping[str, str]] = {
    "aligned": "their edges line up on the sheet",
    "shifted": "the views match edge for edge after a piece one view draws longer",
    "stretched": "their edges line up on the sheet, except that one view draws a piece between "
    "them longer",
    "pasted": "matched by the same sequence of widths",
}


def _sixteenths(value: Fraction) -> Fraction:
    return Fraction(round(value * 16), 16)


def question_text(pair: SpanPair) -> str:
    """The words of one question."""
    a = pair.a[0]
    first = f"{pair.first.where} prints {a.value} ({a.id} at {_b(a.box)})"
    if pair.missing:
        return (
            f"Overall {pair.measure}: {first} over all its edges; {pair.second.where}, drawn to "
            f"the same edges ({_HOW[pair.how]}), prints no size over that span and no "
            f"{a.value} at all. Is its overall {pair.measure} missing?"
        )
    b = pair.b[0]
    da, db = _sixteenths(a.drawn), _sixteenths(b.drawn)
    drawn = (
        f"drawn {format_inches(da)} and {format_inches(db)} long at the scale"
        if da != db
        else f"both drawn {format_inches(da)} long at the scale"
    )
    return (
        f"Same {pair.measure} span in two views: {first}; {pair.second.where} prints {b.value} "
        f"({b.id} at {_b(b.box)}); {_HOW[pair.how]}, {drawn}. Which is right?"
    )


_QUESTIONS_HEAD: Final = (
    "More of code's questions (numbered after the ones above): one span measured in two views "
    "(the views' edges matched by where they are drawn) with two different sizes, or an overall "
    "one view prints and the other does not. Settle each the same way (report_issue with "
    'question "Qn", or answer_question with a short reason):'
)


def _segments(run: Any, page: int) -> Sequence[Any] | None:
    """`page`'s line work, by the reader the reviewer already has (one per run)."""
    reader = getattr(run, "_dim_parts_lines", None) or getattr(run, "_line_work", None)
    if reader is None:
        consistency = import_module("app.review.assistant.vendor_consistency")
        workspace = run.engine.tools.workspace
        reader = consistency.LineWork(workspace.uploads, workspace.set_pack)
        run._dim_parts_lines = reader
    found: Sequence[Any] | None = reader(run.review.file, page)
    return found


def _views_of(
    run: Any, page: int, found: Sequence[Mapping[str, Any]], size: tuple[float, float] | None
) -> PageViews | None:
    labels = run._labels_on(page)
    if not labels:
        return None
    return page_views(page, labels, found, _segments(run, page), size)


def question_blocks(run: Any) -> list[dict[str, Any]]:
    """The page reviewer's hook (`_Run.opening`, after `view_questions.opening_blocks` and
    `dim_parts.question_blocks`): each differing span (and missing overall) as one of code's
    numbered questions, logged as `spans_matched`, `span_disagreements` (and `cross_view`, the
    differences). Nothing when the page cannot be read or has no question list. Never raises."""
    review = run.review
    try:
        asked = getattr(review, "questions", None)
        own = _views_of(run, review.page, review.found, review.size)
        if own is None:
            return []
        others: list[PageViews] = []
        try:
            context = run._context()
            if not context.built and not context.ready(0):
                context = None
        except Exception:  # noqa: BLE001 - the page's own views are still compared
            context = None
        if context is not None:
            for other, _tag, _mine in context.pair_pages(review.page)[:MAX_OTHER_PAGES]:
                try:
                    views = _views_of(run, other, context.phrases(other), None)
                except Exception:  # noqa: BLE001 - that drawing is left out
                    _log.info("cross view p%s: p%s not read", review.page, other)
                    continue
                if views is not None:
                    others.append(views)
        found, _matched = page_pairs(own, others)
    except Exception:  # noqa: BLE001 - the page is reviewed without them
        _log.exception("cross view p%s: not worked out", getattr(review, "page", "?"))
        return []
    differing = differences(found)
    knowledge = getattr(review, "knowledge", None)
    if isinstance(knowledge, dict):
        knowledge["spans_matched"] = sum(1 for p in found if not p.missing)
        knowledge["span_disagreements"] = sum(1 for p in found if p.differs)
        knowledge["cross_view"] = [p.as_dict() for p in differing]
    if not differing or not isinstance(asked, list):
        return []
    try:
        questions = import_module("app.review.assistant.view_questions")
        made = []
        for pair in differing[:MAX_QUESTIONS]:
            boxes = tuple(d.box for d in pair.a[:1] + pair.b[:1] if d.page == review.page)
            if pair.missing:
                boxes += (pair.second.box,) if pair.second.page == review.page else ()
            made.append(
                questions.Question(len(asked) + 1, "A", question_text(pair), boxes, "cross_view")
            )
            asked.append(made[-1])
        return [
            {
                "type": "text",
                "text": _QUESTIONS_HEAD + "\n" + fence(questions.listing(made), "cross_view"),
            }
        ]
    except Exception:  # noqa: BLE001 - the review goes on without them
        _log.exception("cross view p%s: questions not added", getattr(review, "page", "?"))
        return []
