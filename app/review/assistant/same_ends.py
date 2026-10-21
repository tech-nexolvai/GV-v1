"""Same end points proved from the drawn run, when vendor and architect draw the same run (#1808).

A red cloud between two drawings needs proof that both dimensions run between the same two points
(rule R8). The anchor fit (`full_check_advocate.same_extent`) lines the drawings up on values
printed once on each; on real sets it often cannot tell (a dimension line it took from the
wrong place, too few shared values, no line found). This proof uses each label's own span, read
from its slash ticks (`dim_anchors`), and works without one scale for both drawings:

**1. Stations.** On each drawing, the places along the pair's axis where dimensions end (a
height's levels, a width's stations).

**2. Ties: the same place on both drawings.** Each tie names one station on each drawing:
- the floor (heights) or the two walls (widths), as `dim_anchors` finds them, or the lowest level
  where two heights end on a long solid line (a counter's floor line);
- the same outlet on both drawings (`dim_anchors`: the same row, the same order), at its centre;
- a neighbouring dimension printed with the same value on both drawings, on one place only on
  each (its two ends are a tie each).
Ties must agree with each other: one place on one drawing ties to one place on the other, in the
same order. Ties that disagree are dropped, the weakest kind first; a run in the other order
(a mirrored drawing) proves nothing.

**3. The pair's ends.**
- Both ends on tied places that are each other's: the same ends (`same_extent`).
- An end on a tied place whose other place is not the other dimension's end: different ends
  (`different_extent`); this only blocks, it never makes red.
- One end tied, the other moved (the difference itself): the moved end is the same point only
  when both dimensions are drawn to their values from the tied end, the difference is at most
  `MAX_MOVE_IN`, and **neither drawing has any other drawn feature** (a long line across the
  dimension's reach, another dimension's end, an outlet) between where its own end lands and
  where the other drawing's value would land, plus `MARGIN_IN`. A counter's top 3/4" above its
  underside is such a feature: "34 1/4" to the underside against 35" to the top" is never proved.
- Anything else: `cannot_tell` (never a block: the existing checks decide as before), unless
  **a face lies where the other value would end** (`other_face`, #1837): on either drawing, the
  other drawing's longer value measured from either end of this dimension lands exactly on
  another drawn line across its reach (or another dimension's end). A vendor height to a top's
  underside, with the top's upper face drawn right where the architect's height to the counter
  would end, is then `different_extent`: never left to a blind pick.

Pure code, $0, no AI, never the AI's words about the ends. The caller passes each drawing's
labels (`dims.json`), its line work (`dim_anchors.page_segments`) and its box on the page.
Verification: `tests/review/assistant/test_same_ends.py` (hand-made line work).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final, Literal

from app.review.assistant import dim_anchors

__all__ = [
    "FACE_MIN_PPI",
    "MARGIN_IN",
    "MAX_MOVE_IN",
    "MIN_LINE_IN",
    "Line",
    "Side",
    "other_face",
    "prove",
    "prove_labels",
    "side",
]

type Extent = Literal["same_extent", "different_extent", "cannot_tell"]
type Box = tuple[float, float, float, float]
type Segment = tuple[tuple[float, float], tuple[float, float]]

#: Two ends closer than this (points) are one place.
_SAME_PT: Final = 1.0
#: Guards `dim_anchors._match_outlets`, which writes outlet ids onto shared drawings (`_ties`).
_OUTLETS_LOCK: Final = threading.Lock()
#: The least a moved end must move on both drawings (points) for `_features` to see a feature at
#: the other value apart from the end itself (#1808 review): twice `_SAME_PT`.
_SEEN_GAP_PT: Final = 2 * _SAME_PT
#: A moved end may be at most this far from the other drawing's value (inches).
MAX_MOVE_IN: Final = 3.0
#: The band beyond both candidate places that must hold no other feature (inches).
MARGIN_IN: Final = 0.5
#: A line shorter than this (inches) is a symbol's stroke, not a feature an end measures to.
MIN_LINE_IN: Final = 2.0
#: A dimension is drawn to its value within this (inches), or `_DRAWN_SHARE` of it.
_DRAWN_IN: Final = 0.5
_DRAWN_SHARE: Final = 0.02
#: A neighbour's tie may sit at most this much higher or further along on one drawing than on the
#: other, counted from the floor or the left wall (inches): a value printed again far away is
#: another place.
_SHIFT_IN: Final = 3.0
#: How far across from the dimension line the reach of an end's extension line is widened.
_REACH_PAD_IN: Final = 2.0
#: A gap an extension line may have (dashed) and still be one line (points).
_DASH_GAP_PT: Final = 10.0
#: Two values are the same within this (inches).
_SAME_IN: Final = Fraction(1, 32)
#: The least points per inch a dimension must be drawn at for `other_face` to look for a face
#: where the other value would end (#1837): about 3/8" to the foot.
FACE_MIN_PPI: Final = 2.5
#: Tie kinds, strongest first (the weakest is dropped first when ties disagree).
_KINDS: Final = ("base", "outlet", "neighbour")


@dataclass(frozen=True, slots=True)
class Line:
    """One label's dimension: its line's place across (`row`) and its two ends along (`a` < `b`)."""

    id: str
    axis: str
    row: float
    a: float
    b: float
    inches: Fraction
    text: str


@dataclass(slots=True)
class Side:
    """One drawing: its dimensions, its own points per inch, and `dim_anchors`' reading of it."""

    role: str
    ppi: float
    lines: dict[str, Line]
    drawing: dim_anchors.Drawing
    box: Box


@dataclass(frozen=True, slots=True)
class _Tie:
    """One place on each drawing that is the same place, and how near an end must be to it
    (points, per drawing: an outlet's middle half, else `_SAME_PT`)."""

    vendor: float
    architect: float
    kind: str
    why: str
    v_near: float = _SAME_PT
    a_near: float = _SAME_PT


def _inches(label: Mapping[str, Any]) -> Fraction | None:
    try:
        value = Fraction(Decimal(str(label.get("inches"))))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value > 0 else None


def side(
    role: str,
    labels: Sequence[Mapping[str, Any]],
    segments: Iterable[Segment],
    box: Sequence[float],
) -> Side | None:
    """One drawing (`role` architect or vendor, `box` on its page), or `None` when code trusts no
    dimension on it. Each label's span is its own ticks' (`dim_anchors`); a label the tick reader
    missed keeps the dimension line `dims.json` found for it when that line is drawn to its value
    at the drawing's scale (`scale_ok`)."""
    area: Box = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    found = dim_anchors.drawing(role, labels, segments, area)
    if found is None or found.pt_per_in <= 0:
        return None
    lines = {
        key: Line(
            key, span.axis, span.row, span.a, span.b, span.inches, str(span.label.get("text"))
        )
        for key, span in found.spans.items()
    }
    for label in labels:
        key = str(label.get("id") or "")
        axis = label.get("orientation")
        value = _inches(label)
        line = label.get("dim_line")
        raw = label.get("box")
        if (
            not key
            or key in lines
            or label.get("side") != role
            or axis not in ("h", "v")
            or value is None
            or label.get("scale_ok") is not True
            or label.get("origin") == "picture"  # pixels, never a line (#1835)
            or not line
            or len(line) != 2
            or not raw
            or len(raw) != 4
        ):
            continue
        centre = ((float(raw[0]) + float(raw[2])) / 2, (float(raw[1]) + float(raw[3])) / 2)
        if not dim_anchors._inside(centre, area):
            continue
        along = 0 if axis == "h" else 1
        a, b = sorted(float(point[along]) for point in line)
        if abs((b - a) / found.pt_per_in / float(value) - 1) > dim_anchors.FIT:
            continue
        lines[key] = Line(
            key, str(axis), float(line[0][1 - along]), a, b, value, str(label.get("text"))
        )
    return Side(role, found.pt_per_in, lines, found, area)


# ---- ties --------------------------------------------------------------------------------------


def _long_perpendicular(found: Side, axis: str, at: float, length: float) -> bool:
    """A solid line at least `length` long lying across the axis within `_SAME_PT` of `at` (a
    floor line), its pieces joined over gaps up to 2 pt."""
    lines = found.drawing.lines
    pieces = sorted(
        (p0, p1)
        for p0, p1, place in (
            lines.horizontal if axis == "v" else [(y0, y1, x) for y0, y1, x in lines.vertical]
        )
        if abs(place - at) <= _SAME_PT
    )
    best, start, end = 0.0, None, None
    for p0, p1 in pieces:
        if start is not None and end is not None and p0 <= end + 2.0:
            end = max(end, p1)
        else:
            start, end = p0, p1
        best = max(best, end - start)
    return best >= length


def _base(found: Side, axis: str) -> float | None:
    """The floor (heights): `dim_anchors`' checked floor only. `None` for widths (the walls are
    ties of their own) or when not found. The lowest level where two heights end on a long line is
    not enough (#1808 review): on a drawing of the upper zone only it is the counter top, and
    tying it to the other drawing's floor proves a backsplash the same as a toe kick."""
    if axis != "v":
        return None
    drawing = found.drawing
    if drawing.floor is not None and drawing.floor_ok:
        return drawing.floor
    return None


def _intervals(
    found: Side, axis: str, skip: Iterable[tuple[float, float]]
) -> dict[Fraction, list[Line]]:
    """{value: one line per distinct place} of the axis' dimensions, leaving out lines on the
    places in `skip` (the pair itself, printed again)."""
    skipped = list(skip)
    out: dict[Fraction, list[Line]] = {}
    for line in found.lines.values():
        if line.axis != axis:
            continue
        if any(abs(line.a - a) <= _SAME_PT and abs(line.b - b) <= _SAME_PT for a, b in skipped):
            continue
        places = out.setdefault(line.inches, [])
        if not any(abs(p.a - line.a) <= _SAME_PT and abs(p.b - line.b) <= _SAME_PT for p in places):
            places.append(line)
    return out


def _value_key(value: Fraction, keys: Iterable[Fraction]) -> Fraction | None:
    near = [key for key in keys if abs(key - value) <= _SAME_IN]
    return near[0] if len(near) == 1 else None


def _ties(vendor: Side, architect: Side, axis: str, pair: tuple[Line, Line]) -> list[_Tie]:
    ties: list[_Tie] = []
    v_base, a_base = _base(vendor, axis), _base(architect, axis)
    if v_base is not None and a_base is not None:
        ties.append(_Tie(v_base, a_base, "base", "the floor"))
    if axis == "h":
        for wall, words in (("wall_L", "the left wall"), ("wall_R", "the right wall")):
            v_at, a_at = vendor.drawing.walls.get(wall), architect.drawing.walls.get(wall)
            if v_at is not None and a_at is not None:
                ties.append(_Tie(v_at, a_at, "base", words))
    notes: list[str] = []
    # `_match_outlets` writes the ids onto the drawings, which the roster's judges share across
    # threads (#1808 review): match and read the pairs under one lock, so another pair's match
    # never lends this one its ids.
    with _OUTLETS_LOCK:
        dim_anchors._match_outlets(architect.drawing, vendor.drawing, notes)
        a_outlets = {o.id: o for o in architect.drawing.outlets if o.id}
        matched = [
            (outlet, a_outlets[outlet.id], outlet.id)
            for outlet in vendor.drawing.outlets
            if outlet.id and outlet.id in a_outlets
        ]
    for outlet, other, outlet_id in matched:
        index = 1 if axis == "v" else 0
        v_low, v_high = outlet.box[index], outlet.box[index + 2]
        a_low, a_high = other.box[index], other.box[index + 2]
        ties.append(
            _Tie(
                (v_low + v_high) / 2,
                (a_low + a_high) / 2,
                "outlet",
                f"the same outlet ({outlet_id}) at its centre",
                # An end on its middle half is on its centre (as `dim_anchors` pairs them).
                max(_SAME_PT, 0.25 * (v_high - v_low)),
                max(_SAME_PT, 0.25 * (a_high - a_low)),
            )
        )
    v_line, a_line = pair
    v_places = _intervals(vendor, axis, [(v_line.a, v_line.b)])
    a_places = _intervals(architect, axis, [(a_line.a, a_line.b)])
    for value, places in v_places.items():
        key = _value_key(value, a_places)
        if key is None or len(places) != 1 or len(a_places[key]) != 1:
            continue
        v, a = places[0], a_places[key][0]
        words = f"{v.id} {v.text} and {a.id} {a.text}"
        ties.append(_Tie(v.a, a.a, "neighbour", words))
        ties.append(_Tie(v.b, a.b, "neighbour", words))
    return _consistent(_placed(ties, vendor, architect, axis))


def _placed(ties: list[_Tie], vendor: Side, architect: Side, axis: str) -> list[_Tie]:
    """Neighbour ties in about the same place on both drawings, counted from a base tie. With no
    floor or wall, neighbour ties count only when at least two different values tie (one value
    printed once on each drawing may be a coincidence: #1808 review), or an outlet ties too."""
    bases = [tie for tie in ties if tie.kind == "base"]
    if not bases:
        values = {tie.why for tie in ties if tie.kind == "neighbour"}
        if len(values) >= 2 or any(tie.kind == "outlet" for tie in ties):
            return ties
        return [tie for tie in ties if tie.kind != "neighbour"]
    base = bases[0]
    kept = []
    for tie in ties:
        if tie.kind == "neighbour":
            v_in = (tie.vendor - base.vendor) / vendor.ppi
            a_in = (tie.architect - base.architect) / architect.ppi
            if abs(v_in - a_in) > _SHIFT_IN:
                continue
        kept.append(tie)
    return kept


def _clash(one: _Tie, two: _Tie) -> bool:
    same_v = abs(one.vendor - two.vendor) <= max(one.v_near, two.v_near)
    same_a = abs(one.architect - two.architect) <= max(one.a_near, two.a_near)
    if same_v != same_a:
        return True
    if same_v:
        return False
    return (one.vendor - two.vendor) * (one.architect - two.architect) < 0


def _consistent(ties: list[_Tie]) -> list[_Tie]:
    """The ties that agree: one place to one place, in the same order. Drops the tie with the
    most disagreements (the weakest kind first) until none is left."""
    kept = list(ties)
    while True:
        clashes = [
            sum(1 for other in kept if other is not tie and _clash(tie, other)) for tie in kept
        ]
        if not any(clashes):
            return kept
        worst = max(
            range(len(kept)), key=lambda i: (clashes[i] > 0, _KINDS.index(kept[i].kind), clashes[i])
        )
        del kept[worst]


# ---- the moved end -------------------------------------------------------------------------------


def _reach(found: Side, line: Line, at: float) -> tuple[float, float]:
    """The span across the axis that an end's extension line covers (from its dimension line to
    the far end of the line work on that place, dashes bridged), widened by `_REACH_PAD_IN`."""
    other = "h" if line.axis == "v" else "v"
    pieces = sorted(dim_anchors._pieces(found.drawing.lines, other, at, 0.6))
    low = high = line.row
    grew = True
    while grew:
        grew = False
        for p0, p1 in pieces:
            if p0 <= high + _DASH_GAP_PT and p1 > high:
                high, grew = p1, True
            if p1 >= low - _DASH_GAP_PT and p0 < low:
                low, grew = p0, True
    pad = _REACH_PAD_IN * found.ppi
    return low - pad, high + pad


def _features(found: Side, line: Line, end: float, window: tuple[float, float]) -> list[str]:
    """What else is drawn inside `window` (points along the axis) within the end's reach: another
    dimension's end, a long line across the axis, an outlet. `end` itself is not counted."""
    low, high = window
    across = _reach(found, line, end)
    eps = _SAME_PT
    out: list[str] = []
    for other in found.lines.values():
        if other.axis != line.axis or other.id == line.id:
            continue
        for at in (other.a, other.b):
            if low <= at <= high and abs(at - end) > eps:
                out.append(f"{other.id}'s end")
    lines = found.drawing.lines
    pieces = (
        [(x0, x1, y) for x0, x1, y in lines.horizontal]
        if line.axis == "v"
        else [(y0, y1, x) for y0, y1, x in lines.vertical]
    )
    for p0, p1, at in pieces:
        if not low <= at <= high or abs(at - end) <= eps:
            continue
        if p1 - p0 < MIN_LINE_IN * found.ppi:
            continue
        if p1 < across[0] or p0 > across[1]:
            continue
        out.append("a drawn line")
    index = 1 if line.axis == "v" else 0
    for outlet in found.drawing.outlets:
        edges = (outlet.box[index], outlet.box[index + 2])
        centre = sum(edges) / 2
        if abs(centre - end) <= eps:
            continue  # the end is on this outlet's centre: its own feature
        if any(low <= p <= high for p in (*edges, centre)):
            out.append("an outlet")
    return out


def _moved(
    vendor: Side,
    architect: Side,
    pair: tuple[Line, Line],
    tied: tuple[float, float],
    moved: tuple[float, float],
) -> tuple[Extent, str]:
    v_line, a_line = pair
    (tv, ta), (mv, ma) = tied, moved
    if (mv - tv) * (ma - ta) <= 0:
        return "cannot_tell", "the two dimensions run the other way from the shared end"
    gap = abs(v_line.inches - a_line.inches)
    if min(vendor.ppi, architect.ppi) * float(gap) < _SEEN_GAP_PT:
        # The other edge would sit within a point or two of the end itself: `_features` could not
        # tell it from the end (#1808 review), so a thin top against its underside looks clear.
        return "cannot_tell", "the difference is too small to see at the drawings' scale"
    if gap > MAX_MOVE_IN:
        return (
            "cannot_tell",
            f'the difference is more than {MAX_MOVE_IN:.0f}" (too far to call it the same point)',
        )
    for found, line, length in ((vendor, v_line, abs(mv - tv)), (architect, a_line, abs(ma - ta))):
        drawn = length / found.ppi
        if abs(drawn - float(line.inches)) > max(_DRAWN_IN, _DRAWN_SHARE * float(line.inches)):
            return "cannot_tell", f"{line.id} is not drawn to its value from the shared end"
    margin_v, margin_a = MARGIN_IN * vendor.ppi, MARGIN_IN * architect.ppi
    v_other = tv + math.copysign(float(a_line.inches) * vendor.ppi, mv - tv)
    a_other = ta + math.copysign(float(v_line.inches) * architect.ppi, ma - ta)
    seen = {
        "vendor": _features(
            vendor, v_line, mv, (min(mv, v_other) - margin_v, max(mv, v_other) + margin_v)
        ),
        "architect": _features(
            architect, a_line, ma, (min(ma, a_other) - margin_a, max(ma, a_other) + margin_a)
        ),
    }
    busy = [f"{who}: {', '.join(sorted(set(what)))}" for who, what in seen.items() if what]
    if busy:
        said = "; ".join(busy)
        return "cannot_tell", f"another drawn feature is near the moved end ({said})"
    return "same_extent", ""


# ---- the proof -----------------------------------------------------------------------------------


def _on(ties: Sequence[_Tie], vendor_at: float | None, architect_at: float | None) -> list[_Tie]:
    return [
        tie
        for tie in ties
        if (vendor_at is None or abs(tie.vendor - vendor_at) <= tie.v_near)
        and (architect_at is None or abs(tie.architect - architect_at) <= tie.a_near)
    ]


# ---- a face where the other value lands -----------------------------------------------------------


def other_face(found: Side, line: Line, other: Fraction) -> str | None:
    """What is drawn exactly where the other drawing's longer value `other` would end on this
    drawing, beyond either end of `line`, along the line's own drawn scale (#1837), or None: a long
    line across the dimension's reach, or another dimension's end, within a sixteenth of an inch
    (at least `_SAME_PT`) of that place. Then the two dimensions may stop at different faces of one
    band: a vendor height to a top's underside, with the top's upper face drawn right where the
    architect's height to the counter would end. Outlets are not counted (an outlet's edge beside
    its centre is no face of a band). Only a difference up to `MAX_MOVE_IN`, seen apart from the
    line's own end (`_SEEN_GAP_PT`), on a line drawn at `FACE_MIN_PPI` or more, is looked at."""
    gap = other - line.inches
    if gap <= 0 or gap > MAX_MOVE_IN:
        # Only a value longer than the line is looked at, beyond the line's end (the other
        # drawing is looked at the other way round): inside a dimension's own extent a unit's
        # parts (a cabinet under a top, a panel) are drawn everywhere, so a line there is no sign.
        return None
    own = (line.b - line.a) / float(line.inches)
    if own < FACE_MIN_PPI or own * float(gap) < _SEEN_GAP_PT:
        # At a small scale a floor's drawn thickness or its hatching sits within a point or two
        # of the end: no face can be told apart there (a quarter-inch elevation, 1.5 pt an inch).
        return None
    near = max(_SAME_PT, own / 16)
    for shared, moved in ((line.a, line.b), (line.b, line.a)):
        landing = shared + math.copysign(float(other) * own, moved - shared)
        seen = [
            what
            for what in _features(found, line, moved, (landing - near, landing + near))
            if what != "an outlet"
        ]
        if seen:
            return (
                f"on the {found.role}'s drawing {seen[0]} lies exactly where the other value "
                f"would end ({_in_words(gap)} beyond {line.id}'s own end): the two may stop at "
                "different faces"
            )
    return None


def _in_words(value: Fraction) -> str:
    whole, part = divmod(value, 1)
    if not part:
        return f'{whole}"'
    return f'{whole} {part.numerator}/{part.denominator}"' if whole else f'{part}"'


def _faces(vendor: Side, architect: Side, v_line: Line, a_line: Line) -> str | None:
    """`other_face` on both drawings: the architect's value on the vendor's, and the vendor's on
    the architect's."""
    return other_face(vendor, v_line, a_line.inches) or other_face(architect, a_line, v_line.inches)


def prove(vendor: Side, architect: Side, vendor_id: str, architect_id: str) -> tuple[Extent, str]:
    """Whether the vendor's dimension `vendor_id` and the architect's `architect_id` run between
    the same two points (see the module), with why in plain words. Where the ends cannot tell, a
    drawn face exactly where the other value would end (`other_face`, #1837) says different
    points: such a pair is never left to a blind pick."""
    state, why = _prove(vendor, architect, vendor_id, architect_id)
    if state != "cannot_tell":
        return state, why
    v_line, a_line = vendor.lines.get(vendor_id), architect.lines.get(architect_id)
    if v_line is None or a_line is None or v_line.axis != a_line.axis:
        return state, why
    face = _faces(vendor, architect, v_line, a_line)
    if face is not None:
        return "different_extent", face
    return state, why


def _prove(vendor: Side, architect: Side, vendor_id: str, architect_id: str) -> tuple[Extent, str]:
    v_line, a_line = vendor.lines.get(vendor_id), architect.lines.get(architect_id)
    if v_line is None or a_line is None:
        return "cannot_tell", "code did not find both dimensions' ends from their ticks"
    if v_line.axis != a_line.axis:
        return "different_extent", "one is a width and the other a height"
    ties = _ties(vendor, architect, v_line.axis, (v_line, a_line))
    if not ties:
        return "cannot_tell", "no place is tied on both drawings"
    ends = ((v_line.a, a_line.a), (v_line.b, a_line.b))
    tied: list[tuple[float, float, _Tie]] = []
    loose: list[tuple[float, float]] = []
    for v_end, a_end in ends:
        both = _on(ties, v_end, a_end)
        if both:
            tied.append((v_end, a_end, both[0]))
            continue
        elsewhere = _on(ties, v_end, None) or _on(ties, None, a_end)
        if elsewhere:
            return (
                "different_extent",
                f"one end lands where the other drawing has another place ({elsewhere[0].why})",
            )
        loose.append((v_end, a_end))
    if len(tied) == 2:
        words = " and ".join(sorted({t.why for _v, _a, t in tied}))
        return "same_extent", f"both ends are tied on both drawings ({words})"
    if len(tied) == 1 and len(loose) == 1:
        v_end, a_end, tie = tied[0]
        state, why = _moved(vendor, architect, (v_line, a_line), (v_end, a_end), loose[0])
        if state == "same_extent":
            return state, (
                f"one end is tied on both drawings ({tie.why}); at the other, no other drawn "
                "feature lies near either value on either drawing"
            )
        return state, why
    return "cannot_tell", "neither end is tied on both drawings"


# ---- from two labels -----------------------------------------------------------------------------


def _half(page_map: Mapping[str, Any] | None, label: Mapping[str, Any], role: str) -> Box | None:
    """The box of the label's drawing: the page map's half of that role holding its centre."""
    raw = label.get("box")
    if not raw or len(raw) != 4:
        return None
    cx, cy = (float(raw[0]) + float(raw[2])) / 2, (float(raw[1]) + float(raw[3])) / 2
    page, file = label.get("page"), label.get("file")
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or entry.get("page") != page:
            continue
        if file is not None and entry.get("file") not in (None, file):
            continue
        for half in entry.get("halves") or []:
            box = half.get("box") if isinstance(half, Mapping) else None
            if (
                half.get("role") == role
                and box
                and len(box) == 4
                and box[0] <= cx <= box[2]
                and box[1] <= cy <= box[3]
            ):
                return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    return None


type LabelsOf = Callable[[str, int], Sequence[Mapping[str, Any]]]
type SegmentsOf = Callable[[str, int], Sequence[Segment]]


def prove_labels(
    vendor: Mapping[str, Any],
    architect: Mapping[str, Any],
    *,
    labels_of: LabelsOf,
    segments_of: SegmentsOf,
    page_map: Mapping[str, Any] | None,
    cache: dict[Any, Side | None] | None = None,
) -> tuple[Extent, str]:
    """`prove` for a vendor and an architect label (`dims.json`'s, with file and page): each
    drawing is the page map's half holding the label. `cache` keeps the drawings read."""
    memo = cache if cache is not None else {}
    found: list[Side | None] = []
    for label, role in ((vendor, "vendor"), (architect, "architect")):
        if str(label.get("side") or "") != role or not label.get("id"):
            return "cannot_tell", f"the {role}'s value is not a {role} label"
        box = _half(page_map, label, role)
        if box is None:
            return "cannot_tell", f"the page map has no {role} drawing around the {role}'s label"
        file, page = str(label.get("file")), int(label.get("page") or 0)
        key = (file, page, role, box)
        if key not in memo:
            memo[key] = side(role, labels_of(file, page), segments_of(file, page), box)
        found.append(memo[key])
    vendor_side, architect_side = found
    if vendor_side is None or architect_side is None:
        return "cannot_tell", "code trusts no dimension's ticks on one of the drawings"
    return prove(vendor_side, architect_side, str(vendor["id"]), str(architect["id"]))
