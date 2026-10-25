"""What each dimension measures from and to: code reads where its ends land (fix 3, 2026-10-25).

AI is poor at locating a dimension's end points (2026 benchmarks: the best model 22%), and the
client-meeting job (a real upload) showed what that costs: a sink centre's offset compared with the sink base's
width, a field cut with an overhang, a top's depth with the cabinet and its door. So code works out,
for each printed dimension, **what each end lands on**, from the drawing's own geometry (its line
work, `dim_anchors`' spans read from the slash ticks), the words printed near it, and nothing the
AI said. Two dimensions are compared only when their ends are alike on both sides (`alike`).

**The end kinds** (an end may carry several: a top flush with a cabinet is both):

| Kind | What code needs |
|---|---|
| `cabinet_face` | a cabinet tag (VSB30, B24, VDB15-4) printed over the dimension whose width is the dimension's value; the same span drawn again (same value, same stations) or a dimension of the same stack ending at that station |
| `overhang_end` | an end beyond the outermost cabinet face of its stack by at most `OVERHANG_MAX_IN`, on a dimension whose other end is a cabinet face, its printed value the cabinets' printed sum plus that gap (the top's edge past the cabinets) |
| `field_cut_end` | the end of a vendor overall that includes GV's field cut (`field_cut.page_field_cuts`), where it passes the end of the top printed without it by one field cut |
| `wall` | that top's own end there (the field cut is per wall end); or an end on the near face of a band (two parallel lines `WALL_MIN_IN`-`WALL_MAX_IN` apart, both at least `WALL_RUN_IN` long) drawn beyond the dimension |
| `sink_centre` | the middle of a sink cut-out (a closed rectangle of a cut-out's size with a sink's words in it, or `sink_offset`'s proved sink plan), or a sink CL mark at the end |
| `cutout_edge` | a side of such a cut-out |
| `notch_edge` | the step of a notch in the top's outline (`notches`) |
| `top_edge` | an overhang end or a field cut's top: the top's own edge |
| `door_face` | not read yet: never claimed |

Anything code cannot prove is **unknown** and proves nothing: an unknown end never makes two
dimensions unlike, and never makes them alike. Never on a guess.

**Like for like** (`alike`): two dimensions are **unlike** when, whichever way round they are read
(a reversed unit is drawn mirrored), at least one pair of facing ends is of kinds that can never be
the same point (`_APART`: a sink's centre line is never a cabinet's side; a field cut's end is
never a wall's face or the overhang's edge). Unlike dimensions are never compared: the item goes to
the GV rule that relates them (the sink centre, `sink_centre`; the field cut, `field_cut`) or is Not
compared with the plain reason ("the vendor's 22" measures from the field-cut end of the top to
the sink's centre line; the architect's 20" from a cabinet's side to a cabinet's side").

The finding's record (`FIELD`) keeps the two label ids it was worked out for, so a later step that
pairs the item with other labels is never judged by a stale record (`unlike_reason`).

Pure code, $0, no AI. Verification: `tests/review/assistant/test_dim_ends.py` (made-up line work).
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from itertools import pairwise
from typing import Any, Final, Literal

__all__ = [
    "FIELD",
    "KINDS",
    "KIND_WORDS",
    "LIKE_FOR_LIKE",
    "NOT_WALL",
    "PROVING",
    "Chain",
    "End",
    "Ends",
    "Page",
    "Span",
    "alike",
    "chain_of",
    "countertop_drawing",
    "drawn_words",
    "extension",
    "field_cut_relief",
    "not_a_wall",
    "page_of",
    "right_ends",
    "same_kinds",
    "small_span",
    "touched",
    "unlike_reason",
    "wall_band",
    "wall_to_wall",
]

type Point = tuple[float, float]
type Segment = tuple[Point, Point]
type Box = tuple[float, float, float, float]
type Kind = Literal[
    "wall",
    "end_panel",
    "outline",
    "top_outline",
    "cabinet_face",
    "top_edge",
    "door_face",
    "sink_centre",
    "cutout_edge",
    "field_cut_end",
    "overhang_end",
    "notch_edge",
]
type Verdict = Literal["alike", "unlike", "unknown"]

#: The finding's record of the like-for-like check (`unlike_reason`).
FIELD: Final = "dim_ends"
KINDS: Final[tuple[Kind, ...]] = (
    "wall",
    "end_panel",
    "outline",
    "top_outline",
    "cabinet_face",
    "top_edge",
    "door_face",
    "sink_centre",
    "cutout_edge",
    "field_cut_end",
    "overhang_end",
    "notch_edge",
)
#: Each kind in the reasons' plain words.
KIND_WORDS: Final[Mapping[str, str]] = {
    "wall": "the wall's face",
    "end_panel": "the face of a thin band (a panel, a filler or a top's thickness)",
    "outline": "the outer edge of what is drawn",
    "top_outline": "an edge of the drawn piece of top",
    "cabinet_face": "a cabinet's side",
    "top_edge": "the top's edge",
    "door_face": "the door's face",
    "sink_centre": "the sink's centre line",
    "cutout_edge": "the sink cut-out's edge",
    "field_cut_end": "the field-cut end of the top",
    "overhang_end": "the top's edge past the cabinets (the overhang)",
    "notch_edge": "a notch's step",
}
#: Kinds that are never the same point (either way round).
_APART: Final[frozenset[frozenset[str]]] = frozenset(
    frozenset(pair)
    for pair in (
        *(("sink_centre", other) for other in KINDS if other != "sink_centre"),
        *(("cutout_edge", other) for other in KINDS if other not in ("cutout_edge", "sink_centre")),
        *(
            ("notch_edge", other)
            for other in KINDS
            if other not in ("notch_edge", "sink_centre", "cutout_edge")
        ),
        ("field_cut_end", "wall"),
        ("field_cut_end", "cabinet_face"),
        ("field_cut_end", "overhang_end"),
        ("overhang_end", "wall"),
        ("overhang_end", "cabinet_face"),
        ("door_face", "cabinet_face"),
    )
)

#: Two stations closer than this (points) are one place.
SAME_PT: Final = 1.0
#: The most an overhang can be (inches), and the least.
OVERHANG_MAX_IN: Final = 3.5
OVERHANG_MIN_IN: Final = 0.25
#: A wall drawn in plan: its two faces this far apart (inches), each at least this long.
WALL_MIN_IN: Final = 2.0
WALL_MAX_IN: Final = 8.0
WALL_RUN_IN: Final = 6.0
#: A notch's step: this short (inches), its two sides at least `NOTCH_SIDE_IN` long each way.
NOTCH_MIN_IN: Final = 0.25
NOTCH_MAX_IN: Final = 2.5
NOTCH_SIDE_IN: Final = 2.0
#: A cut-out of a sink: its size each way (inches, as `sink_offset`).
CUTOUT_MIN_IN: Final = 6.0
CUTOUT_MAX_IN: Final = 48.0
#: How far across from a dimension's line the thing its end lands on may be (inches).
REACH_IN: Final = 24.0
#: Rows of one stack of dimensions (points apart, across).
STACK_PT: Final = 60.0
#: A cabinet tag is printed this close across to its dimension's line (points).
TAG_PT: Final = 25.0
#: Small dimensions code looks for its own span for (inches): the 1" overhang, the 3/4" panel.
SMALL_MAX_IN: Final = Fraction(3)

#: A cabinet tag: letters, then its width in inches (VSB30, B24, VDB15-4, SB36, W3030).
_TAG: Final = re.compile(r"^(?P<letters>[A-Z]{1,5})(?P<width>\d{2})(?:\d{2})?(?:[-\s].*)?$")
#: Letters of a sink base (SB36, VSB30, SBA30).
_SINK_BASE: Final = re.compile(r"SB")
#: A sink's words inside a cut-out (a model or the word itself).
_SINK_WORDS: Final = re.compile(
    r"\b(?:sinks?|lav|lavatory|basin|undermount|kohler|elkay|blanco|franke|k-\d{3,5})\b",
    re.IGNORECASE,
)
#: A centre-line mark.
_CL: Final = re.compile(
    r"^\s*(?:sink\s+)?(?:c\s*/\s*l|cl|℄|cent(?:er|re)\s*-?\s*line)(?:\s+(?:of\s+)?sink)?\s*$",
    re.IGNORECASE,
)


# ---- spans -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Span:
    """One printed dimension as drawn: its line (`row`, across) and its two ends (`a` < `b`)."""

    id: str
    axis: str
    row: float
    a: float
    b: float
    inches: Fraction
    text: str = ""


@dataclass(frozen=True, slots=True)
class End:
    """What one end of a dimension lands on: its kinds (empty: unknown) and how code knows."""

    at: float
    kinds: frozenset[str] = frozenset()
    why: tuple[str, ...] = ()

    @property
    def words(self) -> str:
        if not self.kinds:
            return "a point code could not name"
        kind = next(k for k in _ORDER if k in self.kinds)
        return KIND_WORDS[kind]


#: The kind an end is named by when it carries several (the most telling first).
_ORDER: Final[tuple[str, ...]] = (
    "sink_centre",
    "cutout_edge",
    "notch_edge",
    "field_cut_end",
    "overhang_end",
    "door_face",
    "cabinet_face",
    "end_panel",
    "wall",
    "top_edge",
    "top_outline",
    "outline",
)


@dataclass(frozen=True, slots=True)
class Ends:
    """One dimension and what both its ends land on."""

    span: Span
    low: End
    high: End

    @property
    def known(self) -> bool:
        return bool(self.low.kinds and self.high.kinds)

    def words(self) -> str:
        return f"from {self.low.words} to {self.high.words}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.span.id,
            "low": sorted(self.low.kinds),
            "high": sorted(self.high.kinds),
            "how": [*self.low.why, *self.high.why],
        }


def _fraction(value: object) -> Fraction | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out > 0 else None


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _spans_from_labels(labels: Sequence[Mapping[str, Any]]) -> dict[str, Span]:
    """Spans from the set pack's own dimension lines (`dims.json`), only where the pack says the
    label is drawn to its value (`scale_ok`): a small label next to a long one can be given the
    long one's line."""
    out: dict[str, Span] = {}
    for label in labels:
        line = label.get("dim_line")
        value = _fraction(label.get("inches"))
        if label.get("scale_ok") is not True or value is None or not isinstance(line, Sequence):
            continue
        try:
            (x0, y0), (x1, y1) = ((float(p[0]), float(p[1])) for p in line)
        except (TypeError, ValueError, IndexError):
            continue
        if abs(y0 - y1) <= 0.5:
            out[str(label["id"])] = Span(
                str(label["id"]),
                "h",
                (y0 + y1) / 2,
                min(x0, x1),
                max(x0, x1),
                value,
                str(label.get("text") or ""),
            )
        elif abs(x0 - x1) <= 0.5:
            out[str(label["id"])] = Span(
                str(label["id"]),
                "v",
                (x0 + x1) / 2,
                min(y0, y1),
                max(y0, y1),
                value,
                str(label.get("text") or ""),
            )
    return out


def _page_box(segments: Sequence[Segment]) -> Box:
    xs = [v for a, b in segments for v in (a[0], b[0]) if math.isfinite(v)]
    ys = [v for a, b in segments for v in (a[1], b[1]) if math.isfinite(v)]
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _spans_from_ticks(
    labels: Sequence[Mapping[str, Any]], segments: Sequence[Segment]
) -> tuple[dict[str, Span], float, Any]:
    """Spans read from each label's own slash ticks (`dim_anchors`), the page's points per inch,
    and its split line work."""
    dim_anchors = import_module("app.review.assistant.dim_anchors")

    box = _page_box(segments)
    for label in labels:
        found_box = _box(label.get("box"))
        if found_box is not None:  # a label printed outside the line work still counts
            box = (
                min(box[0], found_box[0]),
                min(box[1], found_box[1]),
                max(box[2], found_box[2]),
                max(box[3], found_box[3]),
            )
    lines = dim_anchors._lines(segments, box)
    found, ppi = dim_anchors._spans(lines, labels, box)
    out = {
        key: Span(key, s.axis, s.row, s.a, s.b, s.inches, str(s.label.get("text") or ""))
        for key, s in found.items()
    }
    return out, float(ppi), lines


def _small_spans(
    labels: Sequence[Mapping[str, Any]], lines: Any, ppi: float, known: Mapping[str, Span]
) -> dict[str, Span]:
    """Small dimensions (an overhang's 1", a panel's 3/4") whose text sits beside their ticks, not
    between them (`dim_anchors` needs a tick each side of the text): `small_span` for each label
    code has no span for yet."""
    out: dict[str, Span] = {}
    if ppi <= 0:
        return out
    for label in labels:
        key = str(label.get("id"))
        if key in known:
            continue
        found = small_span(label, lines, ppi, labels)
        if found is not None:
            out[key] = found
    return out


def small_span(
    label: Mapping[str, Any],
    lines: Any,
    ppi: float,
    others: Sequence[Mapping[str, Any]] = (),
) -> Span | None:
    """A small dimension's own span (an overhang's 1", a panel's 3/4", at most `SMALL_MAX_IN`),
    its text printed beside its two ticks, not between them: one pair of neighbouring ticks on a
    row by the label, drawn to its value at `ppi` (points per inch), the text within a text
    height of it (`_gap`). When no row by the label has one, a leader drawn from the label
    (`_leader_ends`) to the middle of such a pair names it (the drafter's `1"` taken out of a
    crowded chain). Two such pairs, or none: `None`; also when another of `others` (the page's
    labels) printing the same value sits nearer the pair (it is that label's: one span, one
    label). `lines` is `dim_anchors`' split line work of the page."""
    dim_anchors = import_module("app.review.assistant.dim_anchors")

    key = str(label.get("id"))
    value = _fraction(label.get("inches"))
    box = _box(label.get("box"))
    axis = label.get("orientation")
    if ppi <= 0 or value is None or value > SMALL_MAX_IN or box is None or axis not in ("h", "v"):
        return None
    near, far = (box[1], box[3]) if axis == "h" else (box[0], box[2])
    lo, hi = (box[0], box[2]) if axis == "h" else (box[1], box[3])
    drawn = float(value) * ppi
    slack = max(0.15 * drawn, 0.6)

    def ticks_on(row: float, start: float, end: float) -> set[tuple[float, float, float]]:
        pairs: set[tuple[float, float, float]] = set()
        for first, second in pairwise(dim_anchors._ticks(lines, axis, row, start, end)):
            if abs(second - first - drawn) <= slack:
                pairs.add((round(row, 1), round(first, 1), round(second, 1)))
        return pairs

    def tick_rows(low: float, high: float) -> set[float]:
        return {
            round((my if axis == "h" else mx) * 2) / 2
            for mx, my in lines.slanted
            if low <= (my if axis == "h" else mx) <= high
        }

    pairs: set[tuple[float, float, float]] = set()
    for row in tick_rows(near - 8, far + 8):
        pairs |= ticks_on(row, lo - drawn - 12, hi + drawn + 12)
    reach = max(far - near, SMALL_REACH_PT)
    beside = True
    if not pairs:
        beside = False
        # A leader from the label to the middle of its two ticks.
        for along, across in _leader_ends(lines, box, axis):
            for row in tick_rows(across - LEADER_ROW_PT, across + LEADER_ROW_PT):
                if abs(row - across) > LEADER_ROW_PT:
                    continue
                pairs |= {
                    pair
                    for pair in ticks_on(row, along - drawn - 2, along + drawn + 2)
                    if pair[1] - 0.6 <= along <= pair[2] + 0.6
                }
    if len(pairs) != 1:
        return None
    row, a, b = next(iter(pairs))
    mine = _gap(box, axis, row, a, b)
    if beside and mine > reach:
        return None  # the text is not by those ticks: they are another label's
    for other in others:
        other_box = _box(other.get("box"))
        if other is label or other_box is None or _fraction(other.get("inches")) != value:
            continue
        theirs = _gap(other_box, axis, row, a, b)
        if theirs < mine or (not beside and theirs <= reach):
            return None  # another label of the same value is the pair's
    return Span(key, str(axis), row, a, b, value, str(label.get("text") or ""))


#: A small dimension's text lies at most this far (points) from its ticks, or a text height.
SMALL_REACH_PT: Final = 2.0


def _gap(box: Box, axis: str, row: float, a: float, b: float) -> float:
    """How far a label's box lies from a span at `row` from `a` to `b` (points)."""
    near, far = (box[1], box[3]) if axis == "h" else (box[0], box[2])
    lo, hi = (box[0], box[2]) if axis == "h" else (box[1], box[3])
    along = max(0.0, a - hi, lo - b)
    across = max(0.0, near - row, row - far)
    return math.hypot(along, across)


#: A leader's end at its dimension line lies this close across to the line (points).
LEADER_ROW_PT: Final = 1.5
#: A leader starts this close to its label's box (points), and runs at least `LEADER_MIN_PT`.
LEADER_PAD_PT: Final = 1.5
LEADER_MIN_PT: Final = 3.0
LEADER_MAX_PT: Final = 60.0


def _leader_ends(lines: Any, box: Box, axis: str) -> list[tuple[float, float]]:
    """The far ends (`(along, across)` in the frame of `axis`) of the slanted strokes that start at
    the label's box and end away from it: the leaders a drafter draws from a small dimension's
    text to its line. A stroke along or across the line is never one (a dimension or extension
    line), nor a stroke with both ends at the box (the label's own ink)."""
    x0, y0, x1, y1 = box

    def at_box(point: Point) -> bool:
        return (
            x0 - LEADER_PAD_PT <= point[0] <= x1 + LEADER_PAD_PT
            and y0 - LEADER_PAD_PT <= point[1] <= y1 + LEADER_PAD_PT
        )

    out: list[tuple[float, float]] = []
    for a, b in lines.all:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if not LEADER_MIN_PT <= length <= LEADER_MAX_PT:
            continue
        if abs(dx) <= 0.05 * length or abs(dy) <= 0.05 * length:
            continue
        for start, end in ((a, b), (b, a)):
            if at_box(start) and not at_box(end):
                out.append((end[0], end[1]) if axis == "h" else (end[1], end[0]))
    return out


def _median_ppi(spans: Mapping[str, Span]) -> float:
    ratios = sorted((s.b - s.a) / float(s.inches) for s in spans.values() if s.inches >= 6)
    return ratios[len(ratios) // 2] if ratios else 0.0


# ---- the things ends land on --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cabinet:
    """A dimension a cabinet tag names: the cabinet's width, face to face."""

    id: str
    tag: str
    sink_base: bool


@dataclass(frozen=True, slots=True)
class Notch:
    """A step in an outline: its short side (from `a` to `b` along `axis`, at `at` across)."""

    axis: str
    at: float
    a: float
    b: float

    @property
    def box(self) -> Box:
        if self.axis == "h":
            return (self.a, self.at, self.b, self.at)
        return (self.at, self.a, self.at, self.b)


def _phrase_box(phrase: Mapping[str, Any]) -> Box | None:
    return _box(phrase.get("box"))


def _cabinets(
    spans: Mapping[str, Span], phrases: Sequence[Mapping[str, Any]]
) -> dict[str, Cabinet]:
    """Dimensions a cabinet tag names: the tag printed over (or under) the dimension, centred within
    it, close across, its width number the dimension's value; then the same span drawn again (the
    same value and both stations, another view of the same cabinets)."""
    tags: list[tuple[str, Fraction, Box]] = []
    for phrase in phrases:
        text = " ".join(str(phrase.get("text") or "").split()).upper()
        match = _TAG.match(text)
        box = _phrase_box(phrase)
        if match is None or box is None:
            continue
        tags.append((text, Fraction(int(match.group("width"))), box))
    out: dict[str, Cabinet] = {}
    for span in spans.values():
        for text, width, box in tags:
            if width != span.inches:
                continue
            centre = (box[0] + box[2]) / 2 if span.axis == "h" else (box[1] + box[3]) / 2
            near, far = (box[1], box[3]) if span.axis == "h" else (box[0], box[2])
            gap = 0.0 if near <= span.row <= far else min(abs(span.row - near), abs(span.row - far))
            if span.a + 2 < centre < span.b - 2 and gap <= TAG_PT:
                letters = _TAG.match(text)
                sink = bool(letters and _SINK_BASE.search(letters.group("letters")))
                out[span.id] = Cabinet(span.id, text, sink)
                break
    for span in spans.values():
        if span.id in out:
            continue
        for key, cabinet in list(out.items()):
            other = spans[key]
            if (
                other.axis == span.axis
                and other.inches == span.inches
                and abs(other.a - span.a) <= SAME_PT
                and abs(other.b - span.b) <= SAME_PT
            ):
                out[span.id] = Cabinet(span.id, cabinet.tag, cabinet.sink_base)
                break
    return out


def _sink_named(phrases: Sequence[Mapping[str, Any]], box: Box) -> bool:
    for phrase in phrases:
        pbox = _phrase_box(phrase)
        if pbox is None or not _SINK_WORDS.search(str(phrase.get("text") or "")):
            continue
        cx, cy = (pbox[0] + pbox[2]) / 2, (pbox[1] + pbox[3]) / 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            return True
    return False


class _Rows:
    """Pieces of line at right angles to a pair of sides, kept by their place (rounded to half a
    point) and joined into runs once (gaps of 0.6 pt), so a cut-out's edge is looked up, never
    searched for (speed: a heavy page has 100k+ pieces, `rectangles`)."""

    def __init__(self, pieces: Iterable[tuple[float, float, float]]) -> None:
        rows: dict[float, list[tuple[float, float]]] = {}
        for p0, p1, at in pieces:
            rows.setdefault(round(at * 2) / 2, []).append((p0, p1))
        self.runs: dict[float, list[tuple[float, float]]] = {}
        self.starts: dict[float, list[float]] = {}
        for key, found in rows.items():
            runs: list[tuple[float, float]] = []
            for p0, p1 in sorted(found):
                if runs and p0 <= runs[-1][1] + 0.6:
                    runs[-1] = (runs[-1][0], max(runs[-1][1], p1))
                else:
                    runs.append((p0, p1))
            self.runs[key] = runs
            self.starts[key] = [r[0] for r in runs]
        self.keys = sorted(self.runs)

    def between(self, lo: float, hi: float) -> list[float]:
        return self.keys[bisect_left(self.keys, lo) : bisect_right(self.keys, hi)]

    def covered(self, at: float, lo: float, hi: float) -> bool:
        """One run of line work at `at` (within 0.6 pt) covers lo..hi."""
        for key in self.between(at - 0.6, at + 0.6):
            index = bisect_right(self.starts[key], lo + 0.6) - 1
            if index >= 0 and self.runs[key][index][1] >= hi - 0.6:
                return True
        return False


def rectangles(segments: Sequence[Segment], ppi: float) -> list[Box]:
    """Closed rectangles of a sink cut-out's size (`CUTOUT_MIN_IN`-`CUTOUT_MAX_IN` each way), their
    corners square, rounded or cut off: two sides of the same length facing each other, and a line
    across each end between them (less the corner, as far in as the side stops short)."""
    if ppi <= 0:
        return []
    low, high = CUTOUT_MIN_IN * ppi, CUTOUT_MAX_IN * ppi
    by_ends: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for a, b in segments:
        if abs(a[0] - b[0]) < 0.1 and abs(a[1] - b[1]) >= 0.5 * low:
            side = (min(a[1], b[1]), max(a[1], b[1]), (a[0] + b[0]) / 2)
            by_ends.setdefault((round(side[0]), round(side[1])), []).append(side)
    for found_sides in by_ends.values():
        found_sides.sort(key=lambda piece: piece[2])
    rows = _Rows(
        (min(a[0], b[0]), max(a[0], b[0]), (a[1] + b[1]) / 2)
        for a, b in segments
        if abs(a[1] - b[1]) < 0.1 and abs(a[0] - b[0]) >= 0.3
    )
    found: set[Box] = set()
    for (top_key, bottom_key), sides in by_ends.items():
        others = sorted(
            (
                s
                for t in (-1, 0, 1)
                for b in (-1, 0, 1)
                for s in by_ends.get((top_key + t, bottom_key + b), ())
            ),
            key=lambda piece: piece[2],
        )
        places = [piece[2] for piece in others]
        for a0, a1, left in sides:
            start = bisect_left(places, left + low)
            stop = bisect_right(places, left + high)
            for b0, b1, right in others[start:stop]:
                width = right - left
                if not low <= width <= high or abs(a0 - b0) > 0.9 or abs(a1 - b1) > 0.9:
                    continue
                top_end, bottom_end = (a0 + b0) / 2, (a1 + b1) / 2
                reach = 0.3 * min(width, bottom_end - top_end)
                edges: list[float | None] = []
                for end, toward in ((top_end, -1), (bottom_end, 1)):
                    lo, hi = (end - reach, end + 0.3) if toward < 0 else (end - 0.3, end + reach)
                    best = None
                    for at in rows.between(lo, hi):
                        corner = (at - end) * toward
                        inset = max(corner, 0.0) + 0.3
                        if rows.covered(at, left + inset, right - inset) and (
                            best is None or abs(at - end) < abs(best - end)
                        ):
                            best = at
                    edges.append(best)
                top, bottom = edges
                if top is None or bottom is None or not low <= bottom - top <= high:
                    continue
                found.add((round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)))
    return sorted(found)


def _names_a_sink(text: str) -> bool:
    """A sink's words, or a sink base's tag (VSB30, SB36)."""
    if _SINK_WORDS.search(text):
        return True
    tag = _TAG.match(" ".join(text.split()).upper())
    return bool(tag and _SINK_BASE.search(tag.group("letters")))


def _cutouts(
    segments: Sequence[Segment], phrases: Sequence[Mapping[str, Any]], ppi: float
) -> list[Box]:
    """Sink cut-outs: closed rectangles of a cut-out's size (`rectangles`) with a sink's words
    inside (its model, the word sink) and no such rectangle inside them (a top's outline holds its
    cut-out), or `sink_offset`'s proved sink plans (a faucet hole behind it, the run's lines round
    it)."""
    if ppi <= 0 or not segments:
        return []
    if not any(_names_a_sink(str(phrase.get("text") or "")) for phrase in phrases):
        return []  # no sink or sink base named on the page: no cut-out to find (nor time spent)
    named = [rect for rect in rectangles(segments, ppi) if _sink_named(phrases, rect)]

    def holds(outer: Box, inner: Box) -> bool:
        return (
            outer != inner
            and outer[0] <= inner[0]
            and outer[1] <= inner[1]
            and inner[2] <= outer[2]
            and inner[3] <= outer[3]
        )

    found = [rect for rect in named if not any(holds(rect, other) for other in named)]
    sink_offset = import_module("app.review.assistant.sink_offset")

    try:
        plans = sink_offset.plans_on([], segments, _page_box(segments), ppi)
    except Exception:  # noqa: BLE001 - no proved plan: the named rectangles only
        plans = []
    for plan in plans:
        box = tuple(float(v) for v in plan.box)
        if not any(
            all(abs(p - q) <= SAME_PT for p, q in zip(box, rect, strict=True)) for rect in found
        ):
            found.append(box)  # type: ignore[arg-type]
    return found


def notches(segments: Sequence[Segment], box: Box, ppi: float) -> list[Notch]:
    """Steps in an outline inside `box`: a short straight side (`NOTCH_MIN_IN`-`NOTCH_MAX_IN`) with a
    line at right angles leaving each of its ends, the two going opposite ways and each at least
    `NOTCH_SIDE_IN` long (the jog of a notched top's end)."""
    if ppi <= 0:
        return []
    x0, y0, x1, y1 = box
    inside = [
        (a, b)
        for a, b in segments
        if x0 - 2 <= min(a[0], b[0])
        and max(a[0], b[0]) <= x1 + 2
        and y0 - 2 <= min(a[1], b[1])
        and max(a[1], b[1]) <= y1 + 2
    ]
    horizontal = [
        (min(a[0], b[0]), max(a[0], b[0]), (a[1] + b[1]) / 2)
        for a, b in inside
        if abs(a[1] - b[1]) < 0.05 and abs(a[0] - b[0]) >= 0.3
    ]
    vertical = [
        (min(a[1], b[1]), max(a[1], b[1]), (a[0] + b[0]) / 2)
        for a, b in inside
        if abs(a[0] - b[0]) < 0.05 and abs(a[1] - b[1]) >= 0.3
    ]
    side = NOTCH_SIDE_IN * ppi
    out: list[Notch] = []
    # Long pieces only can be a step's sides; kept by their place (speed: a page has 100k+ pieces).
    by_place: dict[str, dict[float, list[tuple[float, float]]]] = {"h": {}, "v": {}}
    for name, pieces in (("h", horizontal), ("v", vertical)):
        for p0, p1, place in pieces:
            if p1 - p0 >= side:
                by_place[name].setdefault(round(place * 2) / 2, []).append((p0, p1))

    def leaves(name: str, at: float, end: float) -> int:
        """+1: a line at `at` leaves `end` toward larger values; -1 smaller; 0 none (or both)."""
        ways = set()
        key = round(at * 2) / 2
        for near in (key - 0.5, key, key + 0.5):
            for p0, p1 in by_place[name].get(near, ()):
                if abs(p0 - end) <= 0.6 and p1 - end >= side:
                    ways.add(1)
                if abs(p1 - end) <= 0.6 and end - p0 >= side:
                    ways.add(-1)
        return next(iter(ways)) if len(ways) == 1 else 0

    for a, b, at in horizontal:
        if not NOTCH_MIN_IN * ppi <= b - a <= NOTCH_MAX_IN * ppi:
            continue
        one, two = leaves("v", a, at), leaves("v", b, at)
        if one and two and one != two:
            out.append(Notch("h", at, a, b))
    for a, b, at in vertical:
        if not NOTCH_MIN_IN * ppi <= b - a <= NOTCH_MAX_IN * ppi:
            continue
        one, two = leaves("h", a, at), leaves("h", b, at)
        if one and two and one != two:
            out.append(Notch("v", at, a, b))
    return out


def wall_band(page: Page, span: Span, end: float, outward: int) -> bool:
    """A wall's near face at `end`: a solid line at right angles to the dimension there, and a
    parallel solid line `WALL_MIN_IN`-`WALL_MAX_IN` farther out (beyond the dimension), both at
    least `WALL_RUN_IN` long and overlapping that much, reaching the dimension's side of the drawing
    (within `REACH_IN` across); neither face another dimension's line, and no line between them
    (a band, drawn as walls are in plan)."""
    return bool(_wall_far_faces(page, span, end, outward))


def _wall_far_faces(page: Page, span: Span, end: float, outward: int) -> list[float]:
    """The far faces (stations) of the wall bands `wall_band` finds at `end`."""
    lines, ppi = page.lines, page.ppi
    if lines is None or ppi <= 0:
        return []
    pieces = lines.vertical if span.axis == "h" else lines.horizontal
    rows = [
        other.row for other in page.spans.values() if other.axis != span.axis
    ]  # dimension lines at right angles to this one
    reach = REACH_IN * ppi
    run = WALL_RUN_IN * ppi

    def faces(at: float) -> list[tuple[float, float]]:
        if any(abs(row - at) <= SAME_PT for row in rows):
            return []  # another dimension's line, never a wall
        out = []
        for p0, p1, place in pieces:
            reaches = min(abs(p0 - span.row), abs(p1 - span.row)) <= reach or p0 <= span.row <= p1
            if abs(place - at) <= 0.6 and p1 - p0 >= run and reaches:
                out.append((p0, p1))
        return out

    near = faces(end)
    if not near:
        return []
    found: list[float] = []
    for _p0, _p1, place in pieces:
        gap = (place - end) * outward
        if not WALL_MIN_IN * ppi <= gap <= WALL_MAX_IN * ppi:
            continue
        for f0, f1 in faces(place):
            for n0, n1 in near:
                lo, hi = max(f0, n0), min(f1, n1)
                if hi - lo < run:
                    continue
                between = [
                    q
                    for q0, q1, q in pieces
                    if 0.6 < (q - end) * outward < gap - 0.6
                    and min(q1, hi) - max(q0, lo) > 0.5 * run
                ]
                if not between and place not in found:
                    found.append(place)
    return found


# ---- one page ------------------------------------------------------------------------------------


@dataclass(slots=True)
class Page:
    """One page's dimensions and what their ends land on (`page_of`)."""

    file: str
    page: int
    spans: dict[str, Span] = field(default_factory=dict)
    ppi: float = 0.0
    cabinets: dict[str, Cabinet] = field(default_factory=dict)
    cutouts: list[Box] = field(default_factory=list)
    segments: Sequence[Segment] = ()
    lines: Any = None
    field_cuts: Any = None
    """A vendor page's field cuts (`field_cut.PageFieldCuts`), when known."""
    fit: Any = None
    """The page's scale fit (`scale_fit.PageFit`): its views' scales across and up, and each
    value checked against its drawn length (final plan #3)."""
    kinds: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    """(label id, "a" | "b") -> its end's kinds."""
    how: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    countertop: bool = False
    """A countertop's own shop drawing (`countertop_drawing`)."""
    toward: dict[tuple[str, str], int] = field(default_factory=dict)
    """(label id, "a" | "b") -> where the thing its end touches lies: +1 inside the dimension's
    span, -1 beyond it (a panel's face, a cut-out's edge), when code worked it out."""
    _notches: list[Notch] | None = None

    def _add(self, key: str, side: str, kind: str, why: str) -> None:
        self.kinds.setdefault((key, side), set()).add(kind)
        found = self.how.setdefault((key, side), [])
        if why not in found:
            found.append(why)

    def ends(self, label_id: object) -> Ends | None:
        span = self.spans.get(str(label_id))
        if span is None:
            return None
        return Ends(
            span,
            End(
                span.a,
                frozenset(self.kinds.get((span.id, "a"), ())),
                tuple(self.how.get((span.id, "a"), ())),
            ),
            End(
                span.b,
                frozenset(self.kinds.get((span.id, "b"), ())),
                tuple(self.how.get((span.id, "b"), ())),
            ),
        )

    def stack(self, span: Span) -> list[Span]:
        """The dimensions of `span`'s stack: the same axis, a row close across, overlapping it."""
        return [
            other
            for other in self.spans.values()
            if other.axis == span.axis
            and abs(other.row - span.row) <= STACK_PT
            and min(other.b, span.b) - max(other.a, span.a) > 0
        ]

    def notches_in(self, box: Box) -> list[Notch]:
        """The page's notch steps (`notches`) whose short side lies in `box`."""
        if self._notches is None:
            self._notches = notches(self.segments, _page_box(self.segments), self.ppi)
        x0, y0, x1, y1 = box
        return [
            step
            for step in self._notches
            if x0 <= step.box[0] and step.box[2] <= x1 and y0 <= step.box[1] and step.box[3] <= y1
        ]


def _station_ends(spans: Mapping[str, Span], axis: str, at: float) -> list[tuple[str, str]]:
    out = []
    for span in spans.values():
        if span.axis != axis:
            continue
        if abs(span.a - at) <= SAME_PT:
            out.append((span.id, "a"))
        if abs(span.b - at) <= SAME_PT:
            out.append((span.id, "b"))
    return out


def _fitted(found: Page, labels: Sequence[Mapping[str, Any]], segments: Sequence[Segment]) -> Any:
    """The page's scale fit (`scale_fit.page_fit`), and its lines for the labels code has no span
    for yet: only a line drawn to its value at its view's fit (`Check.agrees`), so a line the fit
    cannot vouch for never names an end."""
    scale_fit = import_module("app.review.assistant.scale_fit")
    try:
        fit = scale_fit.page_fit(labels, segments)
    except Exception:  # noqa: BLE001 - no fit: the spans read before stand alone
        return None
    for key, drawn in fit.spans.items():
        check = fit.check(key)
        if key in found.spans or check is None or not check.agrees:
            continue
        found.spans[key] = Span(
            key, drawn.axis, drawn.row, drawn.a, drawn.b, drawn.inches, _label_text(labels, key)
        )
    return fit


def _label_text(labels: Sequence[Mapping[str, Any]], key: str) -> str:
    return next(
        (str(label.get("text") or "") for label in labels if str(label.get("id")) == key), ""
    )


def _mark_cabinets(found: Page) -> None:
    for key, cabinet in found.cabinets.items():
        span = found.spans[key]
        why = f"a cabinet's side ({cabinet.tag})"
        found._add(key, "a", "cabinet_face", why)
        found._add(key, "b", "cabinet_face", why)
        # The same extension line: every dimension of its stack ending at that station.
        for station in (span.a, span.b):
            for other_id, side in _station_ends(found.spans, span.axis, station):
                other = found.spans[other_id]
                if abs(other.row - span.row) <= STACK_PT:
                    found._add(other_id, side, "cabinet_face", why)


def _mark_overhangs(found: Page) -> None:
    """A dimension of a cabinet stack that ends at a cabinet face and runs past the outermost one
    by a small gap, its printed value the cabinets' sum to that face plus the gap: the top's edge
    past the cabinets."""
    if found.ppi <= 0:
        return
    for span in found.spans.values():
        if span.id in found.cabinets:
            continue
        faces = sorted(
            {
                round(s, 1)
                for other in found.stack(span)
                if other.id in found.cabinets
                for s in (found.spans[other.id].a, found.spans[other.id].b)
            }
        )
        if len(faces) < 2:
            continue
        first, last = faces[0], faces[-1]
        cabinets = [found.spans[k] for k in found.cabinets if found.spans[k] in found.stack(span)]
        for side, end, inner, other_end in (
            ("b", span.b, last, span.a),
            ("a", span.a, first, span.b),
        ):
            gap = (end - inner) if side == "b" else (inner - end)
            if not OVERHANG_MIN_IN * found.ppi <= gap <= OVERHANG_MAX_IN * found.ppi:
                continue
            if not any(abs(other_end - f) <= SAME_PT for f in faces):
                continue
            lo, hi = sorted((other_end, inner))
            run = [c for c in cabinets if c.a >= lo - SAME_PT and c.b <= hi + SAME_PT]
            if not run or abs(sum(c.b - c.a for c in run) - (hi - lo)) > SAME_PT * len(run):
                continue  # the cabinets do not fill the run end to end
            printed_gap = span.inches - sum((c.inches for c in run), Fraction(0))
            drawn_gap = Fraction(round(gap / found.ppi * 16), 16)
            if printed_gap <= 0 or abs(printed_gap - drawn_gap) > Fraction(1, 4):
                continue
            why = "the top's edge past the last cabinet (its printed size less the cabinets')"
            found._add(span.id, side, "overhang_end", why)
            found._add(span.id, side, "top_edge", why)
            for other_id, other_side in _station_ends(found.spans, span.axis, end):
                if abs(found.spans[other_id].row - span.row) <= STACK_PT:
                    found._add(other_id, other_side, "overhang_end", why)
                    found._add(other_id, other_side, "top_edge", why)


def _mark_field_cuts(found: Page, cuts: Any) -> None:
    """GV's field cut (`field_cut.page_field_cuts`): an overall that includes it passes the end of
    the top printed without it by one field cut; that end is the field-cut end, the top's own end
    there is a wall end (the field cut is per wall end)."""
    if cuts is None or not getattr(cuts, "overalls", None) or found.ppi <= 0:
        return
    per_end = Fraction(cuts.per_end)
    for overall_id, count in cuts.overalls.items():
        overall = found.spans.get(str(overall_id))
        if overall is None:
            continue
        part_value = overall.inches - count * per_end
        for part in found.spans.values():
            if part.axis != overall.axis or part.inches != part_value:
                continue
            low_gap, high_gap = part.a - overall.a, overall.b - part.b
            step = float(per_end) * found.ppi
            near = max(0.25 * step, SAME_PT)
            sides: list[str] = []
            if count == 1 and abs(high_gap) <= SAME_PT and abs(low_gap - step) <= near:
                sides = ["a"]
            elif count == 1 and abs(low_gap) <= SAME_PT and abs(high_gap - step) <= near:
                sides = ["b"]
            elif count == 2 and abs(low_gap - step) <= near and abs(high_gap - step) <= near:
                sides = ["a", "b"]
            if not sides:
                continue
            why_cut = "the field-cut end (FIELD CUT: the overall passes the top by one field cut)"
            why_wall = "the top's wall end (where the field cut is added)"
            for side in sides:
                cut_at = overall.a if side == "a" else overall.b
                wall_at = part.a if side == "a" else part.b
                for other_id, other_side in _station_ends(found.spans, overall.axis, cut_at):
                    found._add(other_id, other_side, "field_cut_end", why_cut)
                for other_id, other_side in _station_ends(found.spans, overall.axis, wall_at):
                    found._add(other_id, other_side, "wall", why_wall)
            for side in ("a", "b"):
                if side in sides:
                    continue
                at = overall.a if side == "a" else overall.b
                for other_id, other_side in _station_ends(found.spans, overall.axis, at):
                    found._add(
                        other_id, other_side, "top_edge", "the top's own end (its overall's)"
                    )
            break


def _mark_cutouts(found: Page, phrases: Sequence[Mapping[str, Any]]) -> None:
    reach = REACH_IN * found.ppi
    for span in found.spans.values():
        for side, end in (("a", span.a), ("b", span.b)):
            for box in found.cutouts:
                lo, hi = (box[0], box[2]) if span.axis == "h" else (box[1], box[3])
                near, far = (box[1], box[3]) if span.axis == "h" else (box[0], box[2])
                across = (
                    0.0
                    if near <= span.row <= far
                    else min(abs(span.row - near), abs(span.row - far))
                )
                if across > reach:
                    continue
                if abs(end - (lo + hi) / 2) <= SAME_PT:
                    found._add(
                        span.id, side, "sink_centre", "the middle of the sink cut-out drawn there"
                    )
                elif abs(end - lo) <= SAME_PT or abs(end - hi) <= SAME_PT:
                    found._add(
                        span.id, side, "cutout_edge", "a side of the sink cut-out drawn there"
                    )
            for phrase in phrases:
                text = str(phrase.get("text") or "")
                mark = _phrase_box(phrase)
                if mark is None or not _CL.match(text):
                    continue
                centre = (mark[0] + mark[2]) / 2 if span.axis == "h" else (mark[1] + mark[3]) / 2
                near, far = (mark[1], mark[3]) if span.axis == "h" else (mark[0], mark[2])
                across = (
                    0.0
                    if near <= span.row <= far
                    else min(abs(span.row - near), abs(span.row - far))
                )
                sink = re.search(r"sink", text, re.IGNORECASE) or found.cutouts
                if abs(end - centre) <= 4.0 and across <= 40.0 and sink:
                    found._add(span.id, side, "sink_centre", f"a centre-line mark ({text.strip()})")


def _mark_notches(found: Page) -> None:
    if not found.segments or found.ppi <= 0:
        return
    steps = found.notches_in(_page_box(found.segments))
    reach = REACH_IN * found.ppi
    for span in found.spans.values():
        for step in steps:
            if step.axis == span.axis:
                continue  # the step runs across the dimension: its ends land on its two levels
            # A step across the dimension's axis sits at one station (`at`).
            if min(abs(step.a - span.row), abs(step.b - span.row)) > reach:
                continue
            for side, end in (("a", span.a), ("b", span.b)):
                if abs(end - step.at) <= SAME_PT:
                    found._add(
                        span.id, side, "notch_edge", "the step of a notch in the top's outline"
                    )


# ---- what an end's extension line touches (final plan #4, 2026-10-25) ----------------------------

#: A countertop's own shop drawing names its edge profiles (GV's legend: eased, mitred, polished).
_TOP_WORDS: Final = re.compile(
    r"\b(?:eased|mit(?:er|re)d?|polished|bullnose|ogee|bevel(?:l?ed)?)\b", re.IGNORECASE
)
#: Gaps an extension line may have (dashes) and still be one line (points).
DASH_PT: Final = 3.0
#: An extension line ends this close (points) before the edge it touches, or this many inches.
TOUCH_GAP_PT: Final = 12.0
TOUCH_GAP_IN: Final = 1.0
#: An edge in line with the extension line counts up to `REACH_IN` past it when at least this long
#: (inches): a casework plan draws its dimensions outside the walls, with no extension line.
IN_LINE_MIN_IN: Final = 2.0
#: A panel (a cabinet's side, an end panel, a filler) drawn in plan: its two faces this far apart
#: (inches), each at least `PANEL_RUN_IN` long.
PANEL_MIN_IN: Final = 0.5
PANEL_MAX_IN: Final = 1.75
PANEL_RUN_IN: Final = 6.0
#: Kinds named from the touched geometry never override these (more telling) kinds.
_SPECIFIC: Final = frozenset({"sink_centre", "cutout_edge", "notch_edge", "field_cut_end"})


def countertop_drawing(found: Page, phrases: Sequence[Mapping[str, Any]]) -> bool:
    """A countertop's own shop drawing (GV's): it names its edge profiles (eased, mitred, polished)
    and prints no cabinet tag. Every outline it draws is a piece of top (or of splash)."""
    if found.cabinets:
        return False
    return any(_TOP_WORDS.search(str(phrase.get("text") or "")) for phrase in phrases)


def _across_pieces(found: Page, span: Span) -> Sequence[tuple[float, float, float]]:
    """Pieces at right angles to the span's axis: (from, to, place) with place along the axis."""
    lines = found.lines
    pieces: Sequence[tuple[float, float, float]] = (
        lines.vertical if span.axis == "h" else lines.horizontal
    )
    return pieces


def _along_pieces(found: Page, span: Span) -> Sequence[tuple[float, float, float]]:
    """Pieces along the span's axis: (from, to, place) with place across it."""
    lines = found.lines
    pieces: Sequence[tuple[float, float, float]] = (
        lines.horizontal if span.axis == "h" else lines.vertical
    )
    return pieces


def extension(found: Page, span: Span, end: float) -> tuple[float, int] | None:
    """The end's extension line: the line at right angles to the dimension at its end that crosses
    or meets its dimension line (dashes joined), as (its far end, which way it runs from the
    dimension line: +1 or -1 across). `None` when no such line is drawn."""
    if found.lines is None:
        return None
    pieces = sorted(
        (lo, hi) for lo, hi, place in _across_pieces(found, span) if abs(place - end) <= 0.8
    )
    runs: list[list[float]] = []
    for lo, hi in pieces:
        if runs and lo <= runs[-1][1] + DASH_PT:
            runs[-1][1] = max(runs[-1][1], hi)
        else:
            runs.append([lo, hi])
    for lo, hi in runs:
        if lo - 1.5 <= span.row <= hi + 1.5:
            before, after = span.row - lo, hi - span.row
            if max(before, after) < 2.0:
                return None  # a tick's stroke, no extension line
            return (hi, 1) if after >= before else (lo, -1)
    return None


def touched(found: Page, span: Span, end: float) -> tuple[float, float] | None:
    """The drawn edge the end's extension line meets, as its extent across the axis (from, to), or
    `None`. Either the edge goes on from the extension line, in line with it (a top's end edge,
    drawn where the extension line stops short of it by up to `TOUCH_GAP_PT` or `TOUCH_GAP_IN`), or
    a line along the dimension's axis passes through the end's station there (a corner). Another
    dimension's line is never that edge."""
    found_ext = extension(found, span, end)
    if found_ext is None:
        return None
    far, way = found_ext
    gap = max(TOUCH_GAP_PT, TOUCH_GAP_IN * found.ppi)
    least = max(2.0, 0.5 * found.ppi)
    in_line = [
        (lo, hi)
        for lo, hi, place in _across_pieces(found, span)
        if abs(place - end) <= 0.8
        and hi - lo >= max(least, IN_LINE_MIN_IN * found.ppi)
        and 0.0 <= ((lo - far) if way > 0 else (far - hi)) <= max(gap, REACH_IN * found.ppi)
    ]
    if in_line:
        nearest = min(in_line, key=lambda piece: abs((piece[0] if way > 0 else piece[1]) - far))
        return nearest
    rows = [
        other.row
        for other in found.spans.values()
        if other.axis == span.axis and other.a - 1 <= end <= other.b + 1
    ]
    best: float | None = None
    for lo, hi, place in _along_pieces(found, span):
        beyond = (place - far) * way
        if not -1.0 <= beyond <= gap or not lo - 1.0 <= end <= hi + 1.0:
            continue
        if hi - lo < least:
            continue  # a tick or a letter's stroke
        if any(abs(place - row) <= 0.6 for row in rows):
            continue  # a dimension line, not a drawn edge
        if best is None or abs(place - far) < abs(best - far):
            best = place
    return None if best is None else (best, best)


def _outline(found: Page, span: Span, end: float, outward: int, edge: tuple[float, float]) -> bool:
    """Nothing is drawn beyond the end (outward, within `REACH_IN`) across from the edge it touches,
    but other dimensions and their extension lines: the end is on the outer edge of the drawing."""
    reach = REACH_IN * found.ppi
    pad = 2.0 * found.ppi if edge[1] - edge[0] < 1.0 else 0.0  # a corner: round it
    low, high = edge[0] - pad, edge[1] + pad
    stations = {
        round(at, 1)
        for other in found.spans.values()
        if other.axis == span.axis
        for at in (other.a, other.b)
    }
    rows = [other.row for other in found.spans.values() if other.axis != span.axis]
    for lo, hi, place in _across_pieces(found, span):
        beyond = (place - end) * outward
        if not 1.0 < beyond <= reach or min(hi, high) - max(lo, low) <= 1.0:
            continue
        if any(abs(place - at) <= 0.8 for at in stations):
            continue  # another dimension's extension line
        if any(abs(place - row) <= 0.6 for row in rows):
            continue  # a dimension line
        return False
    return True


def _panel(found: Page, span: Span, end: float) -> int | None:
    """A panel's face at `end`: a solid line at right angles to the dimension there and a parallel
    one `PANEL_MIN_IN`-`PANEL_MAX_IN` away, both at least `PANEL_RUN_IN` long and overlapping that
    much, with no line between (a cabinet's side, an end panel or a filler, in plan). Returns which
    way the panel lies along the axis (+1 or -1), or `None` (none, or one each way)."""
    if found.lines is None or found.ppi <= 0:
        return None
    run = PANEL_RUN_IN * found.ppi
    pieces = [
        piece for piece in _across_pieces(found, span) if not _extension_line(found, span, piece)
    ]
    near = [(lo, hi) for lo, hi, place in pieces if abs(place - end) <= 0.6 and hi - lo >= run]
    if not near:
        return None
    ways: set[int] = set()
    for lo, hi, place in pieces:
        gap = place - end
        if not PANEL_MIN_IN * found.ppi <= abs(gap) <= PANEL_MAX_IN * found.ppi or hi - lo < run:
            continue
        for n0, n1 in near:
            low, high = max(lo, n0), min(hi, n1)
            if high - low < run:
                continue
            between = [
                q
                for q0, q1, q in pieces
                if 0.6 < (q - end) * (1 if gap > 0 else -1) < abs(gap) - 0.6
                and min(q1, high) - max(q0, low) > 0.5 * run
            ]
            if not between:
                ways.add(1 if gap > 0 else -1)
    return next(iter(ways)) if len(ways) == 1 else None


def _extension_line(found: Page, span: Span, piece: tuple[float, float, float]) -> bool:
    """A piece at right angles to `span`'s axis that is a dimension's extension line, never a drawn
    face: it stands at another dimension's end (of the same axis) and reaches that dimension's
    line."""
    lo, hi, place = piece
    return any(
        other.axis == span.axis
        and (abs(other.a - place) <= 0.8 or abs(other.b - place) <= 0.8)
        and lo - 3.0 <= other.row <= hi + 3.0
        for other in found.spans.values()
    )


def _mark_touched(found: Page, phrases: Sequence[Mapping[str, Any]]) -> None:
    """Each end named by what its extension line touches (final plan #4): a wall's face (a band
    2"-8" thick drawn beyond the end, its far face no other dimension's end), a panel's face (a
    strip 1/2"-1 3/4" thick at the end: a cabinet's side, an end panel or a filler), or, on a
    countertop's own drawing (`countertop_drawing`), the top's edge (the extension line touches the
    drawn outline). An end already named for a sink, a cut-out, a notch or a field cut keeps that
    name only. Anything code cannot see stays unknown."""
    if found.lines is None or found.ppi <= 0:
        return
    top = found.countertop = countertop_drawing(found, phrases)
    for span in list(found.spans.values()):
        stations = [
            at
            for other in found.spans.values()
            if other.axis == span.axis and other.id != span.id
            for at in (other.a, other.b)
        ]
        for side, end, outward in (("a", span.a, -1), ("b", span.b, 1)):
            if found.kinds.get((span.id, side), set()) & _SPECIFIC:
                continue
            if any(
                all(abs(face - station) > SAME_PT for station in stations)
                for face in _wall_far_faces(found, span, end, outward)
            ):
                found._add(
                    span.id, side, "wall", 'a wall or floor drawn beyond it (two lines 2"-8" apart)'
                )
            edge = touched(found, span, end)
            if top:
                # A countertop's own drawing draws pieces of top: every edge is the top's.
                if edge is not None:
                    found._add(
                        span.id,
                        side,
                        "top_outline",
                        "the top's outline, where its extension line meets it",
                    )
                continue
            way = _panel(found, span, end)
            if way is not None:
                found._add(
                    span.id,
                    side,
                    "end_panel",
                    'a thin band drawn at it (two lines 1/2"-1 3/4" apart: a panel, a filler '
                    "or a top's thickness)",
                )
                found.toward[(span.id, side)] = way * -outward
            elif edge is not None and _outline(found, span, end, outward, edge):
                found._add(
                    span.id,
                    side,
                    "outline",
                    "nothing is drawn beyond the edge its extension line meets",
                )


def page_of(
    file: str,
    page: int,
    labels: Sequence[Mapping[str, Any]],
    phrases: Sequence[Mapping[str, Any]],
    segments: Sequence[Segment] | None = None,
    field_cuts: Any = None,
) -> Page:
    """One page read for its dimensions' ends: `labels` and `phrases` as `dims.json` holds them,
    `segments` its line work (None: the pack's own dimension lines only, and no shapes), and
    `field_cuts` a vendor page's `field_cut.PageFieldCuts`."""
    labels = [label for label in labels if isinstance(label, Mapping) and label.get("id")]
    found = Page(file, page, field_cuts=field_cuts)
    if segments:
        spans, ppi, lines = _spans_from_ticks(labels, segments)
        for key, span in _spans_from_labels(labels).items():
            spans.setdefault(key, span)
        found.spans = spans
        found.ppi = ppi or _median_ppi(spans)
        found.lines = lines
        found.segments = segments
        found.spans.update(_small_spans(labels, lines, found.ppi, found.spans))
        found.fit = _fitted(found, labels, segments)
        if found.ppi <= 0 and found.fit is not None and found.fit.fits:
            found.ppi = 1 / sorted(f.in_per_pt for f in found.fit.fits)[len(found.fit.fits) // 2]
        found.cutouts = _cutouts(segments, phrases, found.ppi)
    else:
        found.spans = _spans_from_labels(labels)
        found.ppi = _median_ppi(found.spans)
    found.cabinets = _cabinets(found.spans, phrases)
    _mark_cabinets(found)
    _mark_overhangs(found)
    _mark_field_cuts(found, field_cuts)
    _mark_cutouts(found, phrases)
    _mark_notches(found)
    _mark_touched(found, phrases)
    return found


# ---- like for like -------------------------------------------------------------------------------


def _apart(one: End, two: End) -> tuple[str, str] | None:
    for a in one.kinds:
        for b in two.kinds:
            if frozenset((a, b)) in _APART:
                return a, b
    return None


def alike(vendor: Ends, architect: Ends) -> tuple[Verdict, str]:
    """Whether two dimensions measure between the same kinds of points (see the module):
    `unlike` when, read either way round, a pair of facing ends can never be the same point;
    `alike` when every facing end shares a kind (one way round); else `unknown`."""
    ways = (
        (vendor.low, architect.low, vendor.high, architect.high),
        (vendor.low, architect.high, vendor.high, architect.low),
    )
    if all(_apart(v1, a1) or _apart(v2, a2) for v1, a1, v2, a2 in ways):
        why = (
            f"the vendor's {_value(vendor)} measures {vendor.words()}; the architect's "
            f"{_value(architect)} {architect.words()}"
        )
        return "unlike", why
    for v1, a1, v2, a2 in ways:
        if v1.kinds & a1.kinds and v2.kinds & a2.kinds:
            return "alike", f"both measure {vendor.words()}"
    return "unknown", "code could not name what both ends of each dimension land on"


# ---- the same two points, proved by what both ends touch (final plan #4) --------------------------

#: End kinds that name the same thing on both drawings: a wall's face, a panel's face, a tagged
#: cabinet's side, a sink's centre or its cut-out's edge, GV's field cut, a notch's step, the
#: overhang's edge. The outer edge of what is drawn (`outline`) never does (an architect's elevation
#: may draw a bulkhead above the unit), nor a top's edge (`top_edge`) unless both drawings are a
#: countertop's own.
PROVING: Final = frozenset(
    {
        "wall",
        "end_panel",
        "cabinet_face",
        "sink_centre",
        "cutout_edge",
        "field_cut_end",
        "notch_edge",
        "overhang_end",
    }
)
#: The most two dimensions proved alike by their ends may differ (inches): further apart, two
#: dimensions between the same kinds of points are more likely two different ones.
KINDS_MAX_DIFF_IN: Final = Fraction(3)
#: Facing kinds that say one dimension measures the countertop and the other the cabinets.
_TOP_KINDS: Final = frozenset({"top_edge", "top_outline"})
_CASEWORK_KINDS: Final = frozenset({"cabinet_face", "end_panel"})

type Extent = Literal["same_extent", "different_extent", "cannot_tell"]


def drawn_words(page: Page | None, label_id: object, whose: str) -> str | None:
    """`the vendor's 23" is drawn 24" long at its view's own scale` when the label's value does not
    agree with its drawn length at its view's fit (`scale_fit`, final plan #3), else `None`."""
    fit = page.fit if page is not None else None
    check = fit.disagrees(label_id) if fit is not None else None
    return None if check is None else check.words(whose)


def _agrees(page: Page, label_id: str) -> bool:
    fit = page.fit
    check = fit.check(label_id) if fit is not None else None
    return check is not None and check.agrees


def _toward(page: Page, span: Span, side: str, kind: str) -> int | None:
    """Where the thing an end touches lies: +1 inside the span, -1 beyond it, `None` unknown."""
    if kind == "wall":
        return -1
    if kind == "cutout_edge":
        end = span.a if side == "a" else span.b
        inside = span.b if side == "a" else span.a
        for box in page.cutouts:
            lo, hi = (box[0], box[2]) if span.axis == "h" else (box[1], box[3])
            if abs(end - lo) <= SAME_PT or abs(end - hi) <= SAME_PT:
                middle = (lo + hi) / 2
                return 1 if (middle - end) * (inside - end) > 0 else -1
        return None
    if kind == "end_panel":
        return page.toward.get((span.id, side))
    return 0  # a kind with no side (a centre line, a field cut's end)


def _proving(page: Page, other: Page, kinds: frozenset[str]) -> frozenset[str]:
    out = kinds & PROVING
    if page.countertop and other.countertop:
        out |= kinds & {"top_edge", "top_outline"}
    return out


def _shared(
    v_page: Page, v_ends: Ends, a_page: Page, a_ends: Ends, mirrored: bool
) -> list[tuple[str, str, frozenset[str]]] | None:
    """The proving kinds the facing ends share, one way round: [(vendor side, architect side,
    kinds)] for both ends, or `None` when an end shares none (or its sides disagree)."""
    pairs = (("a", "b"), ("b", "a")) if mirrored else (("a", "a"), ("b", "b"))
    out: list[tuple[str, str, frozenset[str]]] = []
    for v_side, a_side in pairs:
        v_end = v_ends.low if v_side == "a" else v_ends.high
        a_end = a_ends.low if a_side == "a" else a_ends.high
        shared = _proving(v_page, a_page, v_end.kinds) & _proving(a_page, v_page, a_end.kinds)
        kept = frozenset(
            kind
            for kind in shared
            if (t := _toward(v_page, v_ends.span, v_side, kind)) is not None
            and t == _toward(a_page, a_ends.span, a_side, kind)
        )
        if "cabinet_face" in kept and not _same_cabinet(v_page, v_ends.span, a_page, a_ends.span):
            kept = kept - {"cabinet_face"}
        if not kept:
            return None
        out.append((v_side, a_side, kept))
    return out


def _same_cabinet(v_page: Page, v_span: Span, a_page: Page, a_span: Span) -> bool:
    """Both dimensions are a tagged cabinet's width, the same tag on both drawings."""
    v_cab, a_cab = v_page.cabinets.get(v_span.id), a_page.cabinets.get(a_span.id)
    return v_cab is not None and a_cab is not None and v_cab.tag == a_cab.tag


def _rivals(page: Page, ends: Ends, sides: Sequence[tuple[str, frozenset[str]]]) -> list[str]:
    """Other dimensions of the same drawing and axis whose ends carry the same kinds (either way
    round): the proof would fit them as well, so it names nothing."""
    fit = page.fit
    mine = fit.sides.get(ends.span.id) if fit is not None else None
    out: list[str] = []
    for other in page.spans.values():
        if other.id == ends.span.id or other.axis != ends.span.axis:
            continue
        if abs(other.a - ends.span.a) <= SAME_PT and abs(other.b - ends.span.b) <= SAME_PT:
            continue  # the same dimension printed again
        if fit is not None and mine is not None and fit.sides.get(other.id, mine) != mine:
            continue  # the other drawing of a combined sheet
        found = page.ends(other.id)
        if found is None:
            continue
        for first, second in ((found.low, found.high), (found.high, found.low)):
            if sides[0][1] & first.kinds and sides[1][1] & second.kinds:
                out.append(other.id)
                break
    return out


def same_kinds(v_page: Page, vendor: Ends, a_page: Page, architect: Ends) -> tuple[Extent, str]:
    """Whether the vendor's and the architect's dimensions run between the same two points, proved
    by what their ends touch (final plan #4): both ends of both carry the same proving kinds
    (`PROVING`, facing the same way, one way round: a reversed unit is drawn mirrored), both values
    agree with their drawn lengths at their views' fits (`scale_fit`), they differ by at most
    `KINDS_MAX_DIFF_IN`, and no other dimension on either drawing runs between such points.
    `different_extent` when their ends are kinds that are never the same point (`alike`), or when
    one measures the countertop and the other the cabinets. Else `cannot_tell`: an unknown end
    proves nothing."""
    if vendor.span.axis != architect.span.axis:
        return "different_extent", "one is a width and the other a height"
    verdict, why = alike(vendor, architect)
    if verdict == "unlike":
        return "different_extent", why
    top = _top_against_casework(vendor, architect)
    if top is not None:
        return "different_extent", top
    for whose, page, ends in (
        ("the vendor's", v_page, vendor),
        ("the reference's", a_page, architect),
    ):
        said = drawn_words(page, ends.span.id, whose)
        if said is not None:
            return "cannot_tell", f"{said}: its number does not agree with its drawing"
        if not _agrees(page, ends.span.id):
            return "cannot_tell", f"code could not check {whose} value against its drawn length"
    ways = [
        found
        for mirrored in (False, True)
        if (found := _shared(v_page, vendor, a_page, architect, mirrored)) is not None
    ]
    if not ways:
        return "cannot_tell", "code could not name both ends as the same kinds of points on both"
    if abs(vendor.span.inches - architect.span.inches) > KINDS_MAX_DIFF_IN:
        return "cannot_tell", (
            f'they differ by more than {KINDS_MAX_DIFF_IN}" (two different dimensions between such '
            "points, more likely)"
        )
    shared = ways[0]
    for whose, page, ends, index in (
        ("vendor's", v_page, vendor, 0),
        ("reference's", a_page, architect, 1),
    ):
        sides = [(str(pair[index]), pair[2]) for pair in shared]
        rivals = _rivals(page, ends, sides)
        if rivals:
            return "cannot_tell", (
                f"another dimension on the {whose} drawing ({rivals[0]}) runs between the same "
                "kinds of points"
            )
    words = " and ".join(KIND_WORDS[next(k for k in _ORDER if k in pair[2])] for pair in shared)
    return "same_extent", (
        f"both run between the same kinds of points on both drawings ({words}), each drawn to its "
        "value at its own scale, and no other dimension on either drawing does"
    )


def _top_against_casework(vendor: Ends, architect: Ends) -> str | None:
    """`Not compared: one measures the countertop, the other the cabinets` when, read either way
    round, an end of one is the top's edge and the facing end of the other a cabinet's side or a
    panel's face (the brief of 2026-10-25: a countertop length against a cabinet run is compared
    only through GV's settled formula, which takes the job's overhang)."""
    ways = (
        ((vendor.low, architect.low), (vendor.high, architect.high)),
        ((vendor.low, architect.high), (vendor.high, architect.low)),
    )

    def apart(one: End, two: End) -> bool:
        return bool(
            (one.kinds & _TOP_KINDS and two.kinds & _CASEWORK_KINDS and not one.kinds & two.kinds)
            or (
                two.kinds & _TOP_KINDS and one.kinds & _CASEWORK_KINDS and not one.kinds & two.kinds
            )
        )

    if all(any(apart(v, a) for v, a in way) for way in ways):
        return (
            "one measures the countertop, the other the cabinets (the top's edge against a "
            f"cabinet's side or a panel): {vendor.words()} against {architect.words()}"
        )
    return None


def field_cut_relief(vendor: Ends, architect: Ends) -> int | None:
    """GV's rule relating a vendor top with its field cuts to the reference's (overall = the run
    plus one field cut per wall end): how many field cuts the vendor's dimension includes when
    reading its field-cut ends as the wall ends they are cut back to makes the two alike or
    unknown; `None` when that does not help."""
    count = sum(1 for end in (vendor.low, vendor.high) if "field_cut_end" in end.kinds)
    if not count:
        return None

    def back(end: End) -> End:
        if "field_cut_end" not in end.kinds:
            return end
        return End(end.at, frozenset((end.kinds - {"field_cut_end"}) | {"wall"}), end.why)

    cut_back = Ends(vendor.span, back(vendor.low), back(vendor.high))
    verdict, _why = alike(cut_back, architect)
    return None if verdict == "unlike" else count


#: End kinds a wall-to-wall run never stops at (Anant, 2026-10-25): a cabinet's side, the
#: overhang's edge, GV's field cut, the sink, a notch or a door.
NOT_WALL: Final = frozenset(
    {
        "cabinet_face",
        "overhang_end",
        "field_cut_end",
        "sink_centre",
        "cutout_edge",
        "notch_edge",
        "door_face",
    }
)


def not_a_wall(end: End) -> str | None:
    """What `end` is known to be other than a wall (`NOT_WALL`, or the top's own edge with no wall
    there: a free end), in words; `None` when nothing else is known."""
    other = sorted(end.kinds & NOT_WALL)
    if other:
        return KIND_WORDS[other[0]]
    if "top_edge" in end.kinds and "wall" not in end.kinds:
        return KIND_WORDS["top_edge"]
    return None


def wall_to_wall(page: Page, ends: Ends) -> tuple[bool, str]:
    """Whether the dimension runs from one wall to another, and why (Anant, 2026-10-25: a
    splash's length may be compared with the top's run only between the same two walls). Both
    ends must stand on a wall's near face drawn beyond them (`wall_band`), whose far face is no
    other dimension's end (two extension lines a filler apart are not a wall), and neither end may
    be known as anything else (`not_a_wall`: a cabinet's side, the overhang's edge, a field cut,
    the top's free edge).
    Anything code cannot see is no wall."""
    span = ends.span
    if page.lines is None or page.ppi <= 0:
        return False, "code has no line work for the architect's page"
    for name, end in (("one", ends.low), ("the other", ends.high)):
        why = not_a_wall(end)
        if why:
            return False, f"{name} end of it is {why}, not a wall"
    stations = [
        at
        for other in page.spans.values()
        if other.axis == span.axis and other.id != span.id
        for at in (other.a, other.b)
    ]
    for name, at, outward in (("one", span.a, -1), ("the other", span.b, 1)):
        faces = [
            face
            for face in _wall_far_faces(page, span, at, outward)
            if all(abs(face - station) > SAME_PT for station in stations)
        ]
        if not faces:
            return False, f"code sees no wall drawn at {name} end of it"
    return True, "code sees a wall drawn at both ends of it"


def _value(ends: Ends) -> str:
    from app.review.assistant.ledger import format_inches

    return format_inches(ends.span.inches)


# ---- the finding's record -----------------------------------------------------------------------


def record(
    finding: Mapping[str, Any], vendor: Ends, architect: Ends, verdict: Verdict, why: str
) -> dict[str, Any]:
    """The finding's record (`FIELD`) for two labels code worked out."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    return {
        "outcome": verdict,
        "why": why,
        "vendor": _label_key((dims or {}).get("vendor")),
        "architect": _label_key((dims or {}).get("architect")),
        "vendor_ends": vendor.as_dict(),
        "architect_ends": architect.as_dict(),
    }


def _label_key(side: object) -> str | None:
    if not isinstance(side, Mapping) or not side.get("id"):
        return None
    return f"{side.get('file') or ''}|{side.get('page')}|{side.get('id')}"


def unlike_reason(finding: Mapping[str, Any]) -> str | None:
    """The plain reason two labels are never compared (their ends are unlike), while the item still
    pairs those two labels; `None` otherwise."""
    held = finding.get(FIELD)
    if not isinstance(held, Mapping) or held.get("outcome") != "unlike":
        return None
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    if _label_key((dims or {}).get("vendor")) != held.get("vendor"):
        return None
    if _label_key((dims or {}).get("architect")) != held.get("architect"):
        return None
    return str(held.get("why") or "") or None


#: A Right's like-for-like record (final plan #4): {"proved": bool, "why": str, "vendor": key,
#: "architect": key}.
LIKE_FOR_LIKE: Final = "like_for_like"


def right_ends(
    finding: Mapping[str, Any], pages: Callable[[str, int], Page | None]
) -> dict[str, Any] | None:
    """Whether code proved a Right's two dimensions run between the same kinds of points: code
    paired them itself (`paired_by: code`), or every facing end of the two shares a kind code named
    from the drawing (`alike`), each value agreeing with its drawn length where code could check
    it. `None` when the finding pairs no two labels."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else None
    vendor_side = (dims or {}).get("vendor")
    reference_side = (dims or {}).get("architect")
    if not isinstance(vendor_side, Mapping) or not isinstance(reference_side, Mapping):
        return None
    keys = {"vendor": _label_key(vendor_side), "architect": _label_key(reference_side)}
    if finding.get("paired_by") == "code":
        return {"proved": True, "why": "code paired the two dimensions itself", **keys}
    pages_read = [
        pages(str(side.get("file") or ""), int(side.get("page") or 0))
        for side in (vendor_side, reference_side)
    ]
    found = [
        page.ends(side.get("id")) if page is not None else None
        for page, side in zip(pages_read, (vendor_side, reference_side), strict=True)
    ]
    vendor, reference = found
    if vendor is None or reference is None:
        return {"proved": False, "why": "code did not find both dimensions' lines", **keys}
    for page, side, whose in (
        (pages_read[0], vendor_side, "the vendor's"),
        (pages_read[1], reference_side, "the reference's"),
    ):
        said = drawn_words(page, side.get("id"), whose)
        if said is not None:
            return {
                "proved": False,
                "why": f"{said}: its number does not agree with its drawing",
                **keys,
            }
    verdict, why = alike(vendor, reference)
    return {"proved": verdict == "alike", "why": why, **keys}


def pages_reader(
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    phrases: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    segments: Callable[[str, int], Sequence[Segment] | None] | None,
    field_cuts: Callable[[str, int], Any] | None,
    main_file: str | None,
) -> Callable[[str, int], Page | None]:
    """Each page read once (`page_of`), on demand: the vendor's (main file's) pages with their field
    cuts."""
    kept: dict[tuple[str, int], Page | None] = {}

    def read(file: str, page: int) -> Page | None:
        key = (file, page)
        if key not in kept:
            found = labels.get(key)
            if not found:
                kept[key] = None
                return None
            lines = None
            if segments is not None:
                try:
                    lines = segments(file, page)
                except Exception:  # noqa: BLE001 - no line work: the pack's own lines only
                    lines = None
            cuts = None
            if field_cuts is not None and file == (main_file or file):
                try:
                    cuts = field_cuts(file, page)
                except Exception:  # noqa: BLE001 - no field cut known
                    cuts = None
            kept[key] = page_of(file, page, found, phrases.get(key, ()), lines, cuts)
        return kept[key]

    return read


def kept_line_work(pack: Any) -> Callable[[str, int], Sequence[Segment] | None] | None:
    """The set pack's kept line work (`vendor_consistency.LINE_WORK_DIR`), read only: `None` when
    the pack keeps none (an older pack: the pack's own dimension lines are used instead)."""
    from pathlib import Path

    if pack is None:
        return None
    folder = Path(pack) / "line_work"
    if not folder.is_dir():
        return None

    def read(file: str, page: int) -> Sequence[Segment] | None:
        import numpy as np

        set_pack = import_module("app.review.assistant.set_pack")

        path = folder / f"{set_pack.stem(file)}-p{page:04d}.npz"
        try:
            with np.load(path) as kept:
                rows = kept["segments"].tolist()
        except (OSError, ValueError, KeyError):
            return None
        return [((a, b), (c, d)) for a, b, c, d in rows]

    return read


def stations(spans: Iterable[Span]) -> list[float]:
    return sorted({round(v, 1) for s in spans for v in (s.a, s.b)})


# ---- a reference's chain of dimensions, summed --------------------------------------------------

#: A chain's parts: on one dimension line (across within this, points) and touching (along within
#: `CHAIN_GAP_PT`), as the vendor-string check proves strings (`vendor_consistency`).
CHAIN_ROW_PT: Final = 0.6
CHAIN_GAP_PT: Final = 0.6
#: Each part drawn to its value at the page's scale within this share (or `CHAIN_FIT_PT`).
CHAIN_FIT: Final = 0.05
CHAIN_FIT_PT: Final = 0.6
#: An extension line at a chain's station: this close along (points), reaching the line within
#: `CHAIN_REACH_PT`, at least `CHAIN_EXTENSION_PT` long.
CHAIN_STATION_PT: Final = 0.8
CHAIN_REACH_PT: Final = 1.5
CHAIN_EXTENSION_PT: Final = 1.5
#: A chain's line runs on past its outer end by at most this (points: a tick's overshoot); more
#: is a part code has no label for (an unread `3/4"`), so the run is not the whole chain.
CHAIN_RUN_ON_PT: Final = 1.5
#: The most parts one chain is read with.
CHAIN_MAX_PARTS: Final = 20


@dataclass(frozen=True, slots=True)
class Chain:
    """Printed dimensions end to end on one dimension line, and the run they make (`chain_of`)."""

    parts: tuple[Span, ...]
    run: Ends
    """The whole run: from the first part's low end to the last part's high end, its value the
    parts' sum."""

    @property
    def inches(self) -> Fraction:
        return self.run.span.inches


def _extension_at(page: Page, axis: str, row: float, station: float) -> bool:
    """An extension line crosses or meets the dimension line at `row` at `station`."""
    pieces = page.lines.vertical if axis == "h" else page.lines.horizontal
    return any(
        abs(at - station) <= CHAIN_STATION_PT
        and low - CHAIN_REACH_PT <= row <= high + CHAIN_REACH_PT
        and high - low >= CHAIN_EXTENSION_PT
        for low, high, at in pieces
    )


def chain_of(page: Page, label_id: object) -> Chain | None:
    """The chain the label's dimension is one part of, when the drawing proves it is one run with
    no printed overall (client meeting, 2026-10-24: the reference's `1"` beside its `35"` is one
    `36"` top): at least two parts on one dimension line, each touching the next (one part only at
    each joint: two readings there prove nothing), each drawn to its value at the page's scale,
    an extension line at every end and joint, no other dimension on the line overlapping the run,
    and no dimension printed over the whole run (a printed overall is compared itself, never the
    sum), and the line stops at the run's ends (a line running on holds a part code has no label
    for). The run's ends are what the outer parts' ends land on (`Page.ends`). `None` otherwise,
    and without the page's line work."""
    span = page.spans.get(str(label_id))
    if span is None or page.lines is None or page.ppi <= 0:
        return None
    row = [
        other
        for other in page.spans.values()
        if other.axis == span.axis and abs(other.row - span.row) <= CHAIN_ROW_PT
    ]
    parts = [span]
    for forward in (True, False):
        current = span
        while len(parts) <= CHAIN_MAX_PARTS:
            joined = [
                other
                for other in row
                if all(other is not part for part in parts)
                and abs((other.a - current.b) if forward else (current.a - other.b)) <= CHAIN_GAP_PT
            ]
            if not joined:
                break
            if len(joined) > 1:
                return None  # two dimensions start at one joint: not one chain
            current = joined[0]
            parts.append(current)
    if len(parts) < 2 or len(parts) > CHAIN_MAX_PARTS:
        return None
    ordered = sorted(parts, key=lambda part: part.a)
    start, end = ordered[0].a, ordered[-1].b
    for part in ordered:
        drawn = float(part.inches) * page.ppi
        if abs((part.b - part.a) - drawn) > max(CHAIN_FIT * drawn, CHAIN_FIT_PT):
            return None  # a part not drawn to its value: its ticks are not where it says
    across = sum(part.row for part in ordered) / len(ordered)
    for station in [start, *(part.b for part in ordered)]:
        if not _extension_at(page, span.axis, across, station):
            return None
    pieces = page.lines.horizontal if span.axis == "h" else page.lines.vertical
    for low, high, at in pieces:
        if abs(at - across) > CHAIN_ROW_PT + 0.2:
            continue
        if (low <= end + 0.5 and high - end > CHAIN_RUN_ON_PT) or (
            high >= start - 0.5 and start - low > CHAIN_RUN_ON_PT
        ):
            return None  # the line runs on past the chain: a part code has no label for
    for other in row:
        if any(other is part for part in ordered):
            continue
        if min(other.b, end) - max(other.a, start) > CHAIN_GAP_PT:
            return None  # another dimension on the line runs into the chain
    for other in page.spans.values():
        if any(other is part for part in ordered) or other.axis != span.axis:
            continue
        if abs(other.a - start) <= SAME_PT and abs(other.b - end) <= SAME_PT:
            return None  # the overall is printed: it is compared itself
    total = sum((part.inches for part in ordered), Fraction(0))
    first, last = page.ends(ordered[0].id), page.ends(ordered[-1].id)
    assert first is not None and last is not None
    run = Span(
        "+".join(part.id for part in ordered),
        span.axis,
        across,
        start,
        end,
        total,
        " + ".join(part.text for part in ordered),
    )
    return Chain(tuple(ordered), Ends(run, first.low, last.high))
