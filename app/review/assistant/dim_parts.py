"""Which part each dimension measures, from the drawing's own lines (plan Phase 3, 2026-11-01), $0,
no AI.

Sizes are 21 of the 50 issues GV's senior reviewer marked on the four tuned test sheets, and the
page reviewer finds 4-6 of them. GV's reviewer reads a size as "the toe kick's height" or "the
top's depth", then compares the same part in the other views and on the item's other drawing. Code
had the numbers (`dims.json`) but not what each one measures. This module ties a dimension to the
part it measures, the way a drafter reads it, and only when the drawing proves each step:

1. **Its two extension lines** (`dims.json`'s dimension line ends; a label whose line is not drawn
   to its value, `scale_ok` false, may sit on another dimension's line and is left out). Each is
   walked from the dimension line outward along its own line (its pieces through gaps up to
   `EDGE_GAP_PT`, then up to `EDGE_REACH_PT` further: some drafters stop it well short of the
   object, past a wall's hatching) to the first **corner**: a line square to it that ends there (an
   L or a T), or a collinear piece that begins there; a line merely crossing it, or another
   dimension's line, is not one. From the corner on, the collinear line is the **object edge** (at
   least `MIN_EDGE_PT`). Both lines are walked to the same side of the dimension line, and both
   edges must lie in one drawing (a view, `views_of`). Anything unclear: no edge, no part.
2. **The part callout.** The page's phrases with a part word (`PART_WORDS`: TOP, SPLASH, TOE KICK,
   END PANEL, FILLER, DOOR, DRAWER, SHELF, BIN, PANEL, CABINET, SINK, CUT-OUT...), stacked lines
   joined into one callout; view titles, notes and the title block are not callouts. Its
   **leader** is a line that starts at its left or right side, or straight under or over it
   (`LEADER_START_PT`), followed through single joints to its arrowhead's point (`Tip`).
3. **The proof.** The tip lies where the object edges run (`TIP_MARGIN_PT`: an arrow touching the
   part's face), and the nearest lines square to the dimension on each side of the tip, along it
   (the "cell" the tip is in; the leader's own pieces left out; a tip on a line counts on the side
   the leader runs into) are the two edges themselves (`CELL_PT`). So the callout points at the
   very thing the dimension spans, not at something inside it (a shelf in a cabinet's height) or
   beside it (the gap next to a leg). Words with no leader count only when written inside a closed
   piece (`_enclosed`), never floating in an open band. Two callouts naming different parts, or a
   callout naming two ("TOP & SIDE SPLASH"): no part. Never a guess.

Each dimension code accepts is kept as a `DimPart`: `{value, span, view, object_edges, part (or
null), how_sure}` (`how_sure`: `leader` when a leader proves it, `words` when the callout's words sit
on the part, `none` with the reason otherwise).

**The same part across views and sheets** (`disagreements`): the dimensions tied to a part are
grouped by the part and its direction (`DIRECTIONS`: an elevation's across-the-page size is a width
and up-the-page a height; a section's across is a depth; a plan's across is a width and up the page a
depth), over the page's views and the item's other drawing (`SetContext.pair_pages`). Only parts an
item has one of in a direction are compared (`ONE_PER_ITEM`: a page draws several doors, drawers,
shelves, fillers and end panels of different sizes, so "DOOR width 15 vs 18" proves nothing), and
only views that print one value for it. Two views printing different values make a line such as
"TOE KICK height: FRONT VIEW A 4", SECTION VIEW S1 3 1/2"".

**Never red here** (`DIM_PARTS_RED`, off). The links and the disagreements are handed to the page
reviewer as facts (`blocks`, one hook in `_Run.opening`), and each disagreement also as one of
code's numbered questions (`question_blocks`, after `view_questions`' own: the reviewer settles it
with report_issue or answer_question); what it reports from them is a please-check. A code-proved red would need two code-read printed values for the same named part
that disagree (`Disagreement.code_proved`: both from the text layer, both tied by a leader); that
path is ready behind the setting (`red_ready`) and stays off until a measured run shows 0 false reds
(the owner's zero-false-red rule).

Pure code on the vendor layer's text and lines (GV's markup is never read: the drawings-only copy
and `dim_labels`' colour rule). Industry note: no public tool links a dimension to the object it
measures from vector PDFs (INDUSTRY-B, 2026-10-31); `vendor_consistency`'s extension-line matching
is the closest analogue and this module follows its conventions. Verification:
`tests/review/assistant/test_dim_parts.py` (made-up lines, text and values only).
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from importlib import import_module
from typing import Any, Final

from app.review.assistant.ledger import format_inches
from app.review.assistant.untrusted import fence

__all__ = [
    "CELL_PT",
    "CORNER_PT",
    "DIM_PARTS_RED",
    "DIRECTIONS",
    "EDGE_GAP_PT",
    "LEADER_START_PT",
    "MIN_EDGE_PT",
    "ONE_PER_ITEM",
    "PART_WORDS",
    "Callout",
    "DimPart",
    "Disagreement",
    "Edge",
    "PageParts",
    "View",
    "blocks",
    "callouts",
    "disagreements",
    "facts",
    "link_page",
    "parts_in",
    "question_blocks",
    "red_ready",
    "views_of",
]

_log = logging.getLogger(__name__)

type Point = tuple[float, float]
type Segment = tuple[Point, Point]
type Box = tuple[float, float, float, float]

#: A disagreement between two code-read values for the same named part is NOT a red finding yet.
#: Red needs a measured run with 0 false reds first (the owner's zero-false-red rule): until then
#: every link and disagreement is a please-check lead for the page reviewer. Flip only after that
#: run, and wire `red_ready` into the finding path in the same reviewed change.
DIM_PARTS_RED: Final = False

#: A piece of line is on an extension line when within this of it (points, across).
EDGE_POS_PT: Final = 0.6
#: An extension line may stop this short of the object, or of the next piece of its line (points).
EDGE_GAP_PT: Final = 9.0
#: The object edge continues through gaps this small (a joint in the drawing's line work).
EDGE_JOIN_PT: Final = 1.0
#: A line square to the extension meets it (a corner of the object) within this (points).
CORNER_PT: Final = 0.8
#: That square line is at least this long (a tick or a glyph is shorter).
MIN_CORNER_LEN_PT: Final = 3.0
#: The object edge from its corner on is at least this long (points).
MIN_EDGE_PT: Final = 2.0
#: The walk goes on this far past where the extension line stops (points; some drafters stop it
#: well short of the object), and never farther than `MAX_WALK_PT` from the dimension line. Both
#: edges must then lie in one drawing (`_Views.same`).
EDGE_REACH_PT: Final = 48.0
#: The walk along an extension line stops this far from the dimension line (points).
MAX_WALK_PT: Final = 600.0
#: A dimension line is this close to a square line that is another dimension's line (points).
DIM_LINE_PT: Final = 0.6
#: The lines either side of a callout's tip are the dimension's edges within this (points).
CELL_PT: Final = 1.0
#: A line square to the dimension that counts for the tip's cell is at least this long (points).
MIN_CELL_LINE_PT: Final = 1.5
#: A leader starts within this of its callout's left or right side, or under or over it (points).
LEADER_START_PT: Final = 6.0
#: A leader's first piece is at least this long, and its far end at least this far from the words.
LEADER_MIN_PT: Final = 6.0
#: A leader's pieces meet within this (points); it is followed through at most this many joints.
LEADER_JOIN_PT: Final = 0.6
LEADER_HOPS: Final = 3
#: An arrowhead's point is within this of the leader's end (points).
ARROW_PT: Final = 4.0
#: Words with no leader name a part only inside a closed piece at most this many times the
#: dimension's span the other way (`_enclosed`).
WORDS_ACROSS: Final = 3.0
#: A callout's tip may sit this far outside the object's edges (an arrow touching its face).
TIP_MARGIN_PT: Final = 3.0
#: Stacked lines of one callout: at most this far apart (points) and aligned on one side.
STACK_GAP_PT: Final = 2.5
STACK_ALIGN_PT: Final = 4.0
#: The line work's pieces join into one drawing (a view) when they pass within this (points); a
#: drawing has at least `MIN_PIECES` pieces and is not the border (`PAGE_SHARE` of the page).
JOIN_PT: Final = 3.0
MIN_PIECES: Final = 12
PAGE_SHARE: Final = 0.5
#: A view title sits under its drawing at most this far (points); its bubble beside it.
TITLE_BELOW_PT: Final = 80.0
MARKER_PT: Final = 30.0
TITLE_JOIN_PT: Final = 60.0
#: A drawing thinner than this (points) is a dimension chain, a bubble or a fixture: it belongs to
#: the view it lies in or beside, within `THIN_GAP_PT`.
THIN_PT: Final = 40.0
THIN_GAP_PT: Final = 20.0
#: Lines in the reviewer's facts, at most.
MAX_LINK_LINES: Final = 30
MAX_DISAGREEMENTS: Final = 12

#: Part words, the more specific first (each match is taken out before the next is looked for).
PART_WORDS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(rf"(?<![A-Z]){pattern}(?![A-Z])"))
    for name, pattern in (
        ("TOE KICK", r"TOE\s*-?\s*KICKS?"),
        ("END PANEL", r"(?:END\s*PANEL|FINISHED\s*END)S?"),
        ("CUT-OUT", r"(?:SINK\s*)?CUT\s*-?\s*OUTS?"),
        ("SPLASH", r"(?:BACK\s*|SIDE\s*)?SPLASH(?:ES)?"),
        ("TOP", r"(?:COUNTER\s*-?\s*|STONE\s*|QUARTZ\s*)?TOPS?"),
        ("FILLER", r"(?:FILLERS?|SCRIBE)"),
        ("VALANCE", r"VALANCES?"),
        ("SHELF", r"(?:SHELF|SHELVES)"),
        ("DOOR", r"DOORS?"),
        ("DRAWER", r"DRAWERS?(?!\s*SLIDES?)"),
        ("PANEL", r"PANELS?"),
        ("FASCIA", r"FASCIAS?"),
        ("APRON", r"APRONS?"),
        ("BIN", r"BINS?"),
        ("CABINET", r"CABINETS?"),
        ("BLOCKING", r"(?:BLOCKING|BACKING)"),
        ("CLEAT", r"CLEATS?"),
        ("SINK", r"SINKS?"),
        ("LEG", r"LEGS?"),
    )
)

#: Parts an item has one of in a direction (its toe kick's height, its top's depth): compared
#: across views. Doors, drawers, shelves, fillers, end panels, panels, cabinets and legs come
#: several to an item, each its own size, so the same name is not the same object.
ONE_PER_ITEM: Final = frozenset({"TOE KICK", "TOP", "SPLASH", "VALANCE", "FASCIA", "APRON"})

#: What a dimension measures, by the kind of view and its direction on the page (h: across the
#: page, v: up the page). A detail names no direction, nor an untitled drawing's upright sizes (a
#: plan's depth or an elevation's height, `_name_untitled`): those are never compared.
DIRECTIONS: Final[Mapping[tuple[str, str], str]] = {
    ("elevation", "h"): "width",
    ("elevation", "v"): "height",
    ("section", "h"): "depth",
    ("section", "v"): "height",
    ("plan", "h"): "width",
    ("plan", "v"): "depth",
    ("untitled", "h"): "width",
}
#: An untitled drawing at most this big both ways is a detail (points); one printing an upright
#: size of at least `TALL_IN` is an elevation (a cabinet's height; a plan prints depths).
DETAIL_PT: Final = 220.0
TALL_IN: Final = Fraction(30)

_TITLE: Final = re.compile(r"\b(VIEW|ELEVATION|SECTION|PLAN|DETAIL)\b", re.IGNORECASE)
_NOT_TITLE: Final = re.compile(r"/|\bSEE\b|\bSCALE\b|\bTYP", re.IGNORECASE)
#: A general note is not a part callout ("NOTE-MOUNTING CLEATS AND ACCESSORIES ...").
_NOTE: Final = re.compile(r"^\s*(?:GENERAL\s+)?NOTES?\b", re.IGNORECASE)
_MARKER: Final = re.compile(r"^(?:[A-Z]|[A-Z]?\d{1,2})$")
_TITLE_BLOCK: Final = re.compile(
    r"\b(?:PROJECT|DESCRIPTION|ITEM\s*NO|OVERALL\s*SIZE|DRAWN|DATE|PAGE|SHEET|REV|SCALE|"
    r"COPYRIGHT|MATERIAL\s*SPEC|QTY)\b",
    re.IGNORECASE,
)


# ---- the parts a phrase names ---------------------------------------------------------------------


def parts_in(text: str) -> list[str]:
    """The parts `text` names, each once, the more specific first (`END PANEL`, not `PANEL`)."""
    rest = " ".join(text.upper().split())
    out: list[str] = []
    for name, pattern in PART_WORDS:
        if pattern.search(rest):
            if name not in out:
                out.append(name)
            rest = pattern.sub(" ", rest)
    return out


# ---- the line work --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Line:
    """A straight piece square to the page's axes, in a frame where it runs along `at` (constant
    across) from `lo` to `hi`."""

    at: float
    lo: float
    hi: float
    index: int


class _Frame:
    """The page's square pieces in one orientation's frame: `ups` run square to the dimension line
    (as its extension lines do), `levels` run along it. A vertical dimension is read in the frame
    turned a quarter (x and y swapped), so one code serves both."""

    def __init__(self, segments: Sequence[Segment], turned: bool) -> None:
        self.ups: dict[int, list[_Line]] = defaultdict(list)
        self.levels: dict[int, list[_Line]] = defaultdict(list)
        for index, (a, b) in enumerate(segments):
            (ax, ay), (bx, by) = ((a[1], a[0]), (b[1], b[0])) if turned else (a, b)
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length < 0.8:
                continue
            if abs(dx) <= 0.02 * length:
                line = _Line((ax + bx) / 2, min(ay, by), max(ay, by), index)
                self.ups[int(line.at)].append(line)
            elif abs(dy) <= 0.02 * length:
                line = _Line((ay + by) / 2, min(ax, bx), max(ax, bx), index)
                self.levels[int(line.at)].append(line)

    @staticmethod
    def _near(table: Mapping[int, list[_Line]], at: float, within: float) -> Iterable[_Line]:
        for key in range(math.floor(at - within), math.floor(at + within) + 1):
            for line in table.get(key, ()):
                if abs(line.at - at) <= within:
                    yield line

    def ups_at(self, x: float, within: float = EDGE_POS_PT) -> list[_Line]:
        return list(self._near(self.ups, x, within))

    def levels_at(self, y: float, within: float) -> list[_Line]:
        return list(self._near(self.levels, y, within))

    def ups_crossing(self, y: float, x_lo: float, x_hi: float) -> list[_Line]:
        """The `ups` between x_lo and x_hi that cross the level y (a ray along the dimension)."""
        out: list[_Line] = []
        for key in range(math.floor(x_lo), math.floor(x_hi) + 1):
            for line in self.ups.get(key, ()):
                if (
                    x_lo <= line.at <= x_hi
                    and line.hi - line.lo >= MIN_CELL_LINE_PT
                    and line.lo - 0.3 <= y <= line.hi + 0.3
                ):
                    out.append(line)
        return out


def _flip(point: Point) -> Point:
    return point[1], point[0]


def _dedupe(segments: Iterable[Any]) -> list[Segment]:
    """The page's pieces once each (a PDF often strokes a line twice, or both ways)."""
    seen: set[tuple[float, float, float, float]] = set()
    out: list[Segment] = []
    for seg in segments:
        try:
            (ax, ay), (bx, by) = seg
            a, b = (float(ax), float(ay)), (float(bx), float(by))
        except (TypeError, ValueError):
            continue
        if a == b:
            continue
        a, b = (a, b) if a <= b else (b, a)
        key = (round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2))
        if key not in seen:
            seen.add(key)
            out.append((a, b))
    return out


# ---- views ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class View:
    """One drawing on the page: its title as printed (with its bubble), or where it is when it has
    none, and its kind (elevation, section, plan, detail, or `view` when untitled)."""

    name: str
    kind: str
    box: Box | None = None


def _kind_of(title: str) -> str:
    upper = title.upper()
    if "SECTION" in upper:
        return "section"
    if "DETAIL" in upper:
        return "detail"
    if "PLAN" in upper or re.search(r"\bTOP\s+VIEW\b", upper):
        return "plan"
    return "elevation"


def _centre(box: Sequence[float]) -> Point:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _box_of(phrase: Mapping[str, Any]) -> Box | None:
    raw = phrase.get("box")
    if not isinstance(raw, Sequence) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1


def _titles(found: Sequence[Mapping[str, Any]]) -> list[tuple[str, Box]]:
    """The page's view titles, each with the bubble printed just before it ("A", "S1"); titles on
    one line next to each other are one title, named by the one with a bubble."""
    boxed = [(str(p.get("text") or "").strip(), b) for p in found if (b := _box_of(p)) is not None]
    titles: list[tuple[str, Box, bool]] = []
    for text, box in boxed:
        if (
            not _TITLE.search(text)
            or _NOT_TITLE.search(text)
            or _TITLE_BLOCK.search(text)
            or len(text.split()) > 5
            or parts_in(re.sub(r"\bTOP\b", " ", text, flags=re.IGNORECASE))
        ):
            continue
        marker = next(
            (
                other
                for other, ob in boxed
                if _MARKER.match(other)
                and abs(_centre(ob)[1] - _centre(box)[1]) <= 8
                and 0 <= box[0] - ob[2] <= MARKER_PT
            ),
            None,
        )
        titles.append((f"{text} {marker}" if marker else text, box, marker is not None))
    titles.sort(key=lambda t: (round(_centre(t[1])[1] / 8), t[1][0]))
    joined: list[tuple[str, Box, bool]] = []
    for name, box, marked in titles:
        if joined:
            last_name, last, last_marked = joined[-1]
            if abs(_centre(last)[1] - _centre(box)[1]) <= 8 and box[0] - last[2] <= TITLE_JOIN_PT:
                keep = (name, marked) if marked and not last_marked else (last_name, last_marked)
                joined[-1] = (
                    keep[0],
                    (min(last[0], box[0]), min(last[1], box[1]), max(last[2], box[2]), box[3]),
                    keep[1],
                )
                continue
        joined.append((name, box, marked))
    return [(name, box) for name, box, _ in joined]


def _drawings(
    segments: Sequence[Segment], size: tuple[float, float] | None
) -> tuple[list[int], dict[int, Box]]:
    """Each piece's drawing (its root) and each drawing's box: pieces that meet or pass within
    `JOIN_PT` are one drawing; a drawing of fewer than `MIN_PIECES` pieces or covering more than
    `PAGE_SHARE` of the page (the border) has no box."""
    parent = list(range(len(segments)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict[tuple[int, int], int] = {}
    for i, ((ax, ay), (bx, by)) in enumerate(segments):
        steps = max(1, int(math.hypot(bx - ax, by - ay) / JOIN_PT) + 1)
        for k in range(steps + 1):
            key = (
                int((ax + (bx - ax) * k / steps) // JOIN_PT),
                int((ay + (by - ay) * k / steps) // JOIN_PT),
            )
            other = cells.setdefault(key, i)
            ra, rb = root(i), root(other)
            if ra != rb:
                parent[ra] = rb
    roots = [root(i) for i in range(len(segments))]
    boxes: dict[int, list[float]] = {}
    counts: dict[int, int] = defaultdict(int)
    for i, ((ax, ay), (bx, by)) in enumerate(segments):
        r = roots[i]
        counts[r] += 1
        b = boxes.setdefault(r, [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)])
        b[0], b[1] = min(b[0], ax, bx), min(b[1], ay, by)
        b[2], b[3] = max(b[2], ax, bx), max(b[3], ay, by)
    every = list(boxes.values())
    if size is None or size[0] <= 0 or size[1] <= 0:
        size = (
            max((b[2] for b in every), default=1.0) - min((b[0] for b in every), default=0.0),
            max((b[3] for b in every), default=1.0) - min((b[1] for b in every), default=0.0),
        )
    page_area = max(1.0, size[0] * size[1])
    kept = {
        r: (b[0], b[1], b[2], b[3])
        for r, b in boxes.items()
        if counts[r] >= MIN_PIECES and (b[2] - b[0]) * (b[3] - b[1]) <= PAGE_SHARE * page_area
    }
    return roots, kept


@dataclass(slots=True)
class _Views:
    """The page's drawings and their titles: which view a piece of line belongs to."""

    roots: list[int]
    boxes: dict[int, Box]
    titled: dict[int, list[tuple[str, Box]]]
    alias: dict[int, int] = field(default_factory=dict)
    """A small or thin drawing (a dimension chain, a bubble, a fixture) -> the view it sits by."""

    def view_root(self, index: int) -> int | None:
        if not 0 <= index < len(self.roots):
            return None
        root = self.roots[index]
        return self.alias.get(root, root)

    def same(self, a: int, b: int) -> bool:
        """Two pieces of line are in one drawing (a view, with its small and thin drawings)."""
        ra, rb = self.view_root(a), self.view_root(b)
        return ra is not None and ra == rb

    def of(self, index: int | None, near: Point) -> View:
        root = self.roots[index] if index is not None and 0 <= index < len(self.roots) else None
        if root is not None:
            root = self.alias.get(root, root)
        named = self.titled.get(root, []) if root is not None else []
        if named:
            name, _box = min(named, key=lambda t: math.dist(_centre(t[1]), near))
            return View(name, _kind_of(name))
        box = self.boxes.get(root) if root is not None else None
        where = box if box is not None else (near[0], near[1], near[0], near[1])
        return View(f"the untitled drawing at {where[0]:.0f},{where[1]:.0f}", "view", box)


def _area(box: Box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def views_of(
    found: Sequence[Mapping[str, Any]],
    segments: Sequence[Segment],
    size: tuple[float, float] | None = None,
) -> _Views:
    """The page's views: its drawings (`_drawings`) at least `THIN_PT` both ways, each small or thin
    drawing counted with the one it lies in or beside (`THIN_GAP_PT`), and the title each view
    carries (a title inside a view's box, else the view just above it, `TITLE_BELOW_PT`)."""
    roots, boxes = _drawings(segments, size)
    thick = {r: b for r, b in boxes.items() if min(b[2] - b[0], b[3] - b[1]) >= THIN_PT}
    alias: dict[int, int] = {}
    for r, b in boxes.items():
        if r in thick:
            continue
        holding = [t for t, tb in thick.items() if _gap(b, tb) <= THIN_GAP_PT]
        if holding:
            alias[r] = min(holding, key=lambda t: (_gap(b, thick[t]), _area(thick[t])))
    # A view's box grown by its dimension chains and small pieces: a title sits under all of it.
    grown = {r: list(b) for r, b in thick.items()}
    for r, owner in alias.items():
        g, b = grown[owner], boxes[r]
        grown[owner] = [min(g[0], b[0]), min(g[1], b[1]), max(g[2], b[2]), max(g[3], b[3])]
    titled: dict[int, list[tuple[str, Box]]] = defaultdict(list)
    for name, tbox in _titles(found):
        cx, cy = _centre(tbox)
        inside = [r for r, b in thick.items() if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]]
        if inside:
            owner = min(inside, key=lambda r: _area(thick[r]))
        else:
            under = [
                (tbox[1] - box[3], r)
                for r in thick
                for box in (thick[r], (grown[r][0], grown[r][1], grown[r][2], grown[r][3]))
                if -10 <= tbox[1] - box[3] <= TITLE_BELOW_PT and box[0] - 20 <= cx <= box[2] + 20
            ]
            if not under:
                continue
            owner = min(under)[1]
        titled[owner].append((name, tbox))
    return _Views(roots, boxes, dict(titled), alias)


# ---- callouts -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tip:
    """Where a callout points: its leader's tip, the way the leader runs into it (`toward`, a unit
    vector; (0, 0) for words written on the part) and the leader's own pieces (by index), which
    are never taken for the part's lines."""

    point: Point
    toward: Point = (0.0, 0.0)
    pieces: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class Callout:
    """A part callout: its words (stacked lines joined), its box, the parts it names, and where it
    points (its leaders' tips, or its own middle when it has no leader)."""

    text: str
    box: Box
    parts: tuple[str, ...]
    tips: tuple[Tip, ...]
    leader: bool


def _stacked(found: Sequence[Mapping[str, Any]]) -> list[tuple[str, Box]]:
    """Phrases joined into callouts: a line just under another, aligned on its left or right."""
    boxed = sorted(
        ((str(p.get("text") or "").strip(), b) for p in found if (b := _box_of(p)) is not None),
        key=lambda t: (t[1][1], t[1][0]),
    )
    groups: list[list[Any]] = []
    for text, box in boxed:
        if not text:
            continue
        for group in groups:
            last = group[-1][1]
            if 0 <= box[1] - last[3] <= STACK_GAP_PT and (
                abs(box[0] - last[0]) <= STACK_ALIGN_PT or abs(box[2] - last[2]) <= STACK_ALIGN_PT
            ):
                group.append((text, box))
                break
        else:
            groups.append([(text, box)])
    out: list[tuple[str, Box]] = []
    for group in groups:
        text = " ".join(t for t, _ in group)
        boxes = [b for _, b in group]
        out.append(
            (
                text,
                (
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ),
            )
        )
    return out


def _to_box(point: Point, box: Box) -> float:
    dx = max(box[0] - point[0], 0.0, point[0] - box[2])
    dy = max(box[1] - point[1], 0.0, point[1] - box[3])
    return math.hypot(dx, dy)


def _ends_at(point: Point, ends: Mapping[tuple[int, int], list[tuple[int, Point]]]) -> list[int]:
    """The pieces with an end within `LEADER_JOIN_PT` of `point` (by index)."""
    key = (int(point[0] // 2), int(point[1] // 2))
    out: list[int] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for index, end in ends.get((key[0] + dx, key[1] + dy), ()):
                if math.dist(end, point) <= LEADER_JOIN_PT and index not in out:
                    out.append(index)
    return out


def _arrow_point(
    at: Point,
    box: Box,
    segments: Sequence[Segment],
    ends: Mapping[tuple[int, int], list[tuple[int, Point]]],
) -> Point:
    """Where an arrowhead drawn at a leader's end points: the point where two of its short sides
    meet (within `ARROW_PT` of the end, farther from the words than the end); else the end."""
    shared: dict[tuple[float, float], set[int]] = defaultdict(set)
    key = (int(at[0] // 2), int(at[1] // 2))
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            for index, end in ends.get((key[0] + dx, key[1] + dy), ()):
                if math.dist(*segments[index]) < LEADER_MIN_PT and math.dist(end, at) <= ARROW_PT:
                    shared[(round(end[0], 1), round(end[1], 1))].add(index)
    apexes = [
        point
        for point, pieces in shared.items()
        if len(pieces) >= 2 and _to_box(point, box) > _to_box(at, box)
    ]
    return max(apexes, key=lambda p: _to_box(p, box)) if apexes else at


def _leader_tips(
    box: Box,
    segments: Sequence[Segment],
    ends: Mapping[tuple[int, int], list[tuple[int, Point]]],
    near: Sequence[int],
) -> list[Tip]:
    """The tips of the leaders that start at the callout's left or right side (`near`: the pieces
    with an end near its box)."""
    tips: list[Tip] = []
    for index in near:
        a, b = segments[index]
        for start, far in ((a, b), (b, a)):
            if _to_box(start, box) > LEADER_START_PT or _to_box(far, box) < LEADER_MIN_PT:
                continue
            dx, dy = abs(far[0] - start[0]), abs(far[1] - start[1])
            beside = start[0] <= box[0] + 2 or start[0] >= box[2] - 2
            # Or straight down or up from under or over the words (some drafters' style).
            under = (
                dx <= 0.3 * dy
                and box[0] <= start[0] <= box[2]
                and (start[1] >= box[3] - 1 or start[1] <= box[1] + 1)
            )
            if not (beside or under):
                continue
            if math.dist(start, far) < LEADER_MIN_PT:
                continue
            # Followed through single joints (a landing, then the slanted line to the part).
            at, came, before = far, index, start
            pieces = {index}
            for _hop in range(LEADER_HOPS):
                nxt = [
                    i
                    for i in _ends_at(at, ends)
                    if i != came and math.dist(*segments[i]) >= LEADER_MIN_PT
                ]
                if len(nxt) != 1:
                    break
                p, q = segments[nxt[0]]
                following = q if math.dist(p, at) <= LEADER_JOIN_PT else p
                if _to_box(following, box) <= _to_box(at, box):
                    break
                before, at, came = at, following, nxt[0]
                pieces.add(came)
            length = math.dist(before, at)
            toward = ((at[0] - before[0]) / length, (at[1] - before[1]) / length)
            at = _arrow_point(at, box, segments, ends)
            if all(math.dist(at, t.point) > 1.0 for t in tips):
                tips.append(Tip(at, toward, frozenset(pieces)))
    return tips


def callouts(found: Sequence[Mapping[str, Any]], segments: Sequence[Segment]) -> list[Callout]:
    """The page's part callouts (`Callout`): phrases naming a part, not view titles or the title
    block, with their leaders' tips."""
    ends: dict[tuple[int, int], list[tuple[int, Point]]] = defaultdict(list)
    for index, (a, b) in enumerate(segments):
        for end in (a, b):
            ends[(int(end[0] // 2), int(end[1] // 2))].append((index, end))
    out: list[Callout] = []
    for text, box in _stacked(found):
        if (
            _NOTE.match(text)
            or _TITLE_BLOCK.search(text)
            or (_TITLE.search(text) and len(text.split()) <= 5 and not _NOT_TITLE.search(text))
        ):
            continue
        named = parts_in(text)
        if not named:
            continue
        reach = LEADER_START_PT + 1
        near: list[int] = []
        for kx in range(int((box[0] - reach) // 2), int((box[2] + reach) // 2) + 1):
            for ky in range(int((box[1] - reach) // 2), int((box[3] + reach) // 2) + 1):
                for index, end in ends.get((kx, ky), ()):
                    if _to_box(end, box) <= LEADER_START_PT and index not in near:
                        near.append(index)
        tips = _leader_tips(box, segments, ends, near)
        out.append(
            Callout(text, box, tuple(named), tuple(tips) or (Tip(_centre(box)),), leader=bool(tips))
        )
    return out


# ---- a dimension's object edges -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Edge:
    """The object edge one extension line of a dimension reaches: where along the dimension it is
    (`at`), where the object starts (its corner, `corner`) and ends (`end`), across the dimension,
    in page points; and a piece of it (for its view)."""

    at: float
    corner: float
    end: float
    index: int

    def as_list(self, orientation: str) -> list[list[float]]:
        a, b = (self.at, self.corner), (self.at, self.end)
        if orientation == "v":
            a, b = _flip(a), _flip(b)
        return [[round(a[0], 2), round(a[1], 2)], [round(b[0], 2), round(b[1], 2)]]


def _far_ends(raw: Mapping[str, Any], turned: bool) -> list[Point]:
    """The far ends of a label's two extension lines (in its frame)."""
    out: list[Point] = []
    for line in raw.get("extension") or []:
        try:
            far = (float(line[0][0]), float(line[0][1]))
        except (TypeError, ValueError, IndexError):
            continue
        out.append(_flip(far) if turned else far)
    return out


def _edge(
    frame: _Frame,
    at: float,
    dim_at: float,
    sign: float,
    dim_lines: Sequence[tuple[float, float, float]],
) -> Edge | None:
    """The object edge on the extension line at `at` (along the dimension), walked from the
    dimension line (`dim_at`, across) to the side `sign` (+1: down or right in the frame): along
    the extension line's own pieces (gaps up to `EDGE_GAP_PT`), then on up to `EDGE_REACH_PT` past
    where it stops (a wall's hatching between). The corner is the first place where a line square to
    it meets it and the object's edge starts: that square line ends there (an L or a T), or a
    collinear piece begins there. A square line merely crossing the extension line (another
    drawing's line, a leader) is not a corner, nor is another dimension's line. Returns the edge
    from that corner on (`Edge`), or None."""
    pieces = sorted(
        (
            min((line.lo - dim_at) * sign, (line.hi - dim_at) * sign),
            max((line.lo - dim_at) * sign, (line.hi - dim_at) * sign),
            line.index,
        )
        for line in frame.ups_at(at)
    )
    pieces = [p for p in pieces if p[1] > 0.5]
    reach = 0.0
    for lo, hi, _index in pieces:
        if lo > reach + EDGE_GAP_PT:
            break
        reach = max(reach, hi)
    limit = min(reach + EDGE_REACH_PT, MAX_WALK_PT)
    centre = dim_at + sign * limit / 2
    candidates: list[tuple[float, bool]] = []
    for line in frame.levels_at(centre, limit / 2 + CORNER_PT):
        if line.hi - line.lo < MIN_CORNER_LEN_PT or not (
            line.lo - CORNER_PT <= at <= line.hi + CORNER_PT
        ):
            continue
        distance = (line.at - dim_at) * sign
        if not 1.0 < distance <= limit:
            continue
        if any(
            abs(line.at - across) <= DIM_LINE_PT and lo - CORNER_PT <= at <= hi + CORNER_PT
            for across, lo, hi in dim_lines
        ):
            continue
        ends_here = abs(line.lo - at) <= CORNER_PT or abs(line.hi - at) <= CORNER_PT
        candidates.append((distance, ends_here))
    for corner, ends_here in sorted(candidates):
        starting = [
            p for p in pieces if abs(p[0] - corner) <= CORNER_PT and p[1] - corner >= MIN_EDGE_PT
        ]
        through = [p for p in pieces if p[0] <= corner + CORNER_PT and p[1] - corner >= MIN_EDGE_PT]
        if not starting and not (ends_here and through):
            continue
        index = (starting or through)[0][2]
        end = corner
        for lo, hi, _piece in pieces:
            if lo - EDGE_JOIN_PT <= end < hi:
                end = hi
        return Edge(at, dim_at + sign * corner, dim_at + sign * end, index)
    return None


def _both_edges(
    frame: _Frame, label: Any, turned: bool, dim_lines: Sequence[tuple[float, float, float]]
) -> list[Edge | None]:
    """The object edges at the label's two extension lines, both walked to the same side of its
    dimension line: the side where both reach an object. An extension line also runs a little past
    its dimension line, and `dim_labels`' far end can be that overshoot when the line stops short of
    the object, so both sides are tried; when both reach objects, the side of the far ends wins."""
    ends = _far_ends(label.raw, turned)
    lean = sum(far[1] - label.across for far in ends)
    tried: dict[float, list[Edge | None]] = {}
    for sign in (1.0, -1.0):
        tried[sign] = [
            _edge(frame, at, label.across, sign, dim_lines) for at in (label.start, label.end)
        ]
    full = [sign for sign, edges in tried.items() if all(e is not None for e in edges)]
    if len(full) == 1:
        return tried[full[0]]
    preferred = 1.0 if lean >= 0 else -1.0
    if full:
        return tried[preferred]
    partial = [sign for sign, edges in tried.items() if any(e is not None for e in edges)]
    return tried[partial[0]] if len(partial) == 1 else tried[preferred]


# ---- linking --------------------------------------------------------------------------------------


@dataclass(slots=True)
class DimPart:
    """One dimension code accepts, with the part it measures (or None and why)."""

    id: str
    page: int
    text: str
    inches: Fraction
    route: str
    clean: bool
    orientation: str
    span: tuple[float, float]
    """Where its two extension lines are, along the dimension (page points)."""
    view: View
    edges: tuple[Edge | None, Edge | None]
    part: str | None = None
    how_sure: str = "none"
    """`leader` (a leader's tip proves it), `words` (the callout's words sit on the part), or
    `none` (no part: `reason`)."""
    callout: str = ""
    reason: str = ""
    box: Box | None = None
    """Where its value is printed (page points)."""

    @property
    def direction(self) -> str | None:
        return DIRECTIONS.get((self.view.kind, self.orientation))

    @property
    def value(self) -> str:
        return format_inches(self.inches)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "value": self.value,
            "printed": self.text,
            "span": [round(v, 2) for v in self.span],
            "view": self.view.name,
            "direction": self.direction,
            "object_edges": [
                e.as_list(self.orientation) if e is not None else None for e in self.edges
            ],
            "part": self.part,
            "how_sure": self.how_sure,
            "callout": self.callout,
            "reason": self.reason,
        }


@dataclass(slots=True)
class PageParts:
    """A page's dimensions with their parts."""

    page: int
    dims: list[DimPart] = field(default_factory=list)

    @property
    def linked(self) -> list[DimPart]:
        return [d for d in self.dims if d.part is not None]

    @property
    def unlinked(self) -> list[DimPart]:
        return [d for d in self.dims if d.part is None]


def _in_cell(frame: _Frame, tip: Tip, turned: bool, span: tuple[float, float]) -> bool:
    """The nearest lines square to the dimension on either side of the tip (along it) are the
    dimension's two edges (`CELL_PT`); the leader's own pieces are not lines of the part. A tip on
    a line (an arrow touching a face) counts on the side the leader runs into; a callout's words,
    or a leader running along that line, on either side."""
    lo, hi = span
    x, y = _flip(tip.point) if turned else tip.point
    along = tip.toward[1] if turned else tip.toward[0]
    if not lo - CELL_PT <= x <= hi + CELL_PT:
        return False
    reach = (hi - lo) + 4 * CELL_PT
    lines = sorted(
        {
            round(line.at, 2)
            for line in frame.ups_crossing(y, x - reach, x + reach)
            if line.index not in tip.pieces
        }
    )
    on = [at for at in lines if abs(at - x) <= CORNER_PT]
    left = max((at for at in lines if at < x - CORNER_PT), default=None)
    right = min((at for at in lines if at > x + CORNER_PT), default=None)
    cells: list[tuple[float | None, float | None]] = []
    if not on:
        cells.append((left, right))
    elif along > 0.3:
        cells.append((on[-1], right))
    elif along < -0.3:
        cells.append((left, on[0]))
    else:
        cells += [(left, on[0]), (on[-1], right)]
    return any(
        a is not None and b is not None and abs(a - lo) <= CELL_PT and abs(b - hi) <= CELL_PT
        for a, b in cells
    )


def _enclosed(frames: Mapping[str, _Frame], tip: Tip, span: tuple[float, float]) -> bool:
    """Words with no leader name the part they are written on only when they sit in a closed
    piece: across the dimension too, the nearest lines either side of them are at most
    `WORDS_ACROSS` times the dimension's span apart (a door's or a panel's box). Words floating in
    a long open band (a note between a counter and the wall cabinets) name nothing."""
    x, y = tip.point
    width = span[1] - span[0]
    # Across a dimension whose cells run along x (frame "h"), the words' own cell runs along y:
    # the turned frame's cell, and the other way round. Both are tried.
    for frame, (a, b) in ((frames["v"], (y, x)), (frames["h"], (x, y))):
        reach = WORDS_ACROSS * width + 2 * CELL_PT
        lines = [line.at for line in frame.ups_crossing(b, a - reach, a + reach)]
        left = max((at for at in lines if at < a - CORNER_PT), default=None)
        right = min((at for at in lines if at > a + CORNER_PT), default=None)
        if left is None or right is None or right - left > WORDS_ACROSS * width:
            return False
    return True


def _between_edges(tip: Point, edges: Sequence[Edge]) -> bool:
    """The tip lies across the dimension where its object edges run (not between the edges'
    corners and the dimension line, where only the extension lines are)."""
    y = tip[1]
    return any(
        min(e.corner, e.end) - TIP_MARGIN_PT <= y <= max(e.corner, e.end) + TIP_MARGIN_PT
        for e in edges
    )


def link_page(
    page: int,
    labels: Sequence[Mapping[str, Any]],
    found: Sequence[Mapping[str, Any]],
    segments: Sequence[Any],
    size: tuple[float, float] | None = None,
) -> PageParts:
    """Each dimension of the page (`dims.json` labels with a dimension line and two extension
    lines) with its object edges, view and part (`DimPart`). Pure: labels, phrases (`phrases`) and
    line work (`dim_labels`' segments, page points as shown) in."""
    consistency = import_module("app.review.assistant.vendor_consistency")
    pieces = _dedupe(segments)
    frames = {"h": _Frame(pieces, turned=False), "v": _Frame(pieces, turned=True)}
    views = views_of(found, pieces, size)
    found_callouts = callouts(found, pieces)
    accepted: list[Any] = []
    for raw in labels:
        if not isinstance(raw, Mapping) or isinstance(raw.get("derived"), Mapping):
            continue
        label = consistency.read_label(raw, str(raw.get("file") or ""), page)
        # A line not drawn to its value (`scale_ok` false) may be another dimension's: its ends
        # prove nothing about what this value measures.
        if label is None or len(raw.get("extension") or []) != 2 or raw.get("scale_ok") is False:
            continue
        accepted.append(label)
    dim_lines: dict[str, list[tuple[float, float, float]]] = {"h": [], "v": []}
    for label in accepted:
        dim_lines[label.orientation].append((label.across, label.start, label.end))
    out = PageParts(page)
    for label in accepted:
        turned = label.orientation == "v"
        frame = frames[label.orientation]
        edges = _both_edges(frame, label, turned, dim_lines[label.orientation])
        first = next((e for e in edges if e is not None), None)
        near = _centre(label.box)
        view = views.of(first.index if first is not None else None, near)
        dim = DimPart(
            id=label.id,
            page=page,
            text=label.text,
            inches=label.inches,
            route=label.route,
            clean=label.clean,
            orientation=label.orientation,
            span=(label.start, label.end),
            view=view,
            edges=(edges[0], edges[1]),
            box=label.box,
        )
        out.dims.append(dim)
        if edges[0] is None or edges[1] is None:
            dim.reason = "no clear object edge at " + (
                "either end" if edges[0] is None and edges[1] is None else "one end"
            )
            continue
        both = [e for e in edges if e is not None]
        if not views.same(both[0].index, both[1].index):
            dim.reason = "its two edges are in different drawings"
            continue
        hits: list[tuple[Callout, bool]] = []
        for callout in found_callouts:
            for tip in callout.tips:
                local = _flip(tip.point) if turned else tip.point
                if (
                    _between_edges(local, both)
                    and _in_cell(frame, tip, turned, dim.span)
                    and (callout.leader or _enclosed(frames, tip, dim.span))
                ):
                    hits.append((callout, callout.leader))
                    break
        named = {part for callout, _ in hits for part in callout.parts}
        if not hits:
            dim.reason = "no callout points at what it spans"
        elif len(named) > 1:
            dim.reason = "callouts name different parts: " + ", ".join(sorted(named))
        else:
            callout = hits[0][0]
            dim.part = callout.parts[0]
            dim.how_sure = "leader" if any(lead for _, lead in hits) else "words"
            dim.callout = " ".join(callout.text.split())[:60]
    _name_untitled(out.dims)
    return out


def _name_untitled(dims: Sequence[DimPart]) -> None:
    """An untitled drawing's kind, for the direction of its sizes: one at most `DETAIL_PT` both
    ways is a detail (no direction); one printing an upright size of at least `TALL_IN` is an
    elevation (across: width, up: height); any other one's across sizes are widths (a plan's or an
    elevation's) and its upright sizes name no direction (a plan's depth or an elevation's height).
    """
    tall: dict[str, Fraction] = defaultdict(Fraction)
    for dim in dims:
        if dim.view.kind == "view" and dim.orientation == "v":
            tall[dim.view.name] = max(tall[dim.view.name], dim.inches)
    for dim in dims:
        view = dim.view
        if view.kind != "view":
            continue
        box = view.box
        if box is not None and box[2] - box[0] <= DETAIL_PT and box[3] - box[1] <= DETAIL_PT:
            dim.view = View(view.name, "detail", box)
        elif tall[view.name] >= TALL_IN:
            dim.view = View(view.name, "elevation", box)
        else:
            dim.view = View(view.name, "untitled", box)


# ---- the same part across views and sheets --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One part measured in one direction with different values in different views or sheets."""

    part: str
    direction: str
    sides: tuple[tuple[str, tuple[DimPart, ...]], ...]
    """(where: page and view, the dimensions there), each printing one value."""

    @property
    def code_proved(self) -> bool:
        """Two code-read printed values (the text layer, read cleanly) tied to the part by a leader
        disagree: what a red would rest on (`red_ready`; off, `DIM_PARTS_RED`)."""
        sure = [
            {d.inches for d in dims if d.route == "text" and d.clean and d.how_sure == "leader"}
            for _where, dims in self.sides
        ]
        values = [next(iter(s)) for s in sure if len(s) == 1]
        return len(set(values)) >= 2

    def line(self) -> str:
        said = "; ".join(
            f"{where} {dims[0].value} ({', '.join(f'p{d.page}.{d.id}' for d in dims)})"
            for where, dims in self.sides
        )
        return f"{self.part} {self.direction}: {said}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "direction": self.direction,
            "sides": [
                {
                    "where": where,
                    "value": dims[0].value,
                    "dims": [f"p{d.page}.{d.id}" for d in dims],
                    "page": dims[0].page,
                    "boxes": [list(d.box) for d in dims if d.box is not None],
                }
                for where, dims in self.sides
            ],
            "code_proved": self.code_proved,
        }


def disagreements(pages: Sequence[PageParts]) -> list[Disagreement]:
    """The parts (`ONE_PER_ITEM`) measured in one direction with different values in different
    views of the pages (the page first, then the item's other drawings). A view printing two values
    for one part in one direction is left out (code cannot tell which is the part's)."""
    grouped: dict[tuple[str, str], dict[str, list[DimPart]]] = defaultdict(dict)
    for page in pages:
        for dim in page.linked:
            direction = dim.direction
            if dim.part not in ONE_PER_ITEM or direction is None:
                continue
            where = f"p{dim.page} {dim.view.name}"
            grouped[(dim.part, direction)].setdefault(where, []).append(dim)
    out: list[Disagreement] = []
    for (part, direction), by_view in grouped.items():
        sides = [
            (where, tuple(dims))
            for where, dims in by_view.items()
            if len({d.inches for d in dims}) == 1
        ]
        if len({dims[0].inches for _, dims in sides}) >= 2:
            out.append(Disagreement(part, direction, tuple(sides)))
    return out


def red_ready(found: Sequence[Disagreement]) -> list[Disagreement]:
    """The disagreements a red could rest on (`Disagreement.code_proved`), only when
    `DIM_PARTS_RED` is on. Off until a measured run shows 0 false reds; nothing calls it for a
    finding yet."""
    if not DIM_PARTS_RED:
        return []
    return [d for d in found if d.code_proved]


# ---- the page reviewer's facts --------------------------------------------------------------------

_LEAD: Final = (
    "Parts code tied to dimensions (each dimension's extension lines walked to the object's "
    "edges, and a callout's leader or words pointing at exactly that object; a dimension is left "
    "out when any step is unclear). Use them to name what a size measures and to compare the "
    "same part across views; a difference is a please-check, never certain:"
)


def facts(pages: Sequence[PageParts]) -> list[str]:
    """The reviewer's lines: the page's links (`MAX_LINK_LINES`), then the disagreements over the
    page and the item's other drawings (`MAX_DISAGREEMENTS`)."""
    if not pages:
        return []
    own = pages[0]
    lines: list[str] = []
    linked = own.linked
    if linked:
        lines.append(
            f"p{own.page}: {len(linked)} of {len(own.dims)} dimensions tied to a part "
            "(id value, view: part direction [how]):"
        )
        for dim in linked[:MAX_LINK_LINES]:
            lines.append(
                f"  {dim.id} {dim.value}, {dim.view.name}: {dim.part} "
                f"{dim.direction or 'size'} [{dim.how_sure}: {dim.callout!r}]"
            )
    others = [
        f"p{p.page} ({len(p.linked)} tied: "
        + ", ".join(f"{d.id} {d.value} {d.part} {d.direction or 'size'}" for d in p.linked[:8])
        + ")"
        for p in pages[1:]
        if p.linked
    ]
    if others:
        lines.append("The item's other drawings: " + "; ".join(others))
    found = disagreements(pages)
    if found:
        lines.append("Same part, different sizes (please check which is right):")
        lines += [f"  {d.line()}" for d in found[:MAX_DISAGREEMENTS]]
    return lines


def _segments_of(run: Any, page: int) -> Sequence[Any] | None:
    """`page`'s line work, read by the reviewer's own reader (one per run)."""
    reader = getattr(run, "_dim_parts_lines", None)
    if reader is None:
        consistency = import_module("app.review.assistant.vendor_consistency")
        workspace = run.engine.tools.workspace
        reader = consistency.LineWork(workspace.uploads, workspace.set_pack)
        run._dim_parts_lines = reader
    found: Sequence[Any] | None = reader(run.review.file, page)
    return found


def _page_parts(
    run: Any, page: int, found: Sequence[Mapping[str, Any]], size: tuple[float, float] | None
) -> PageParts | None:
    labels = run._labels_on(page)
    segments = _segments_of(run, page)
    if not labels or not segments:
        return None
    return link_page(page, labels, found, segments, size)


def blocks(run: Any, context_of: Callable[[], Any] | None = None) -> list[dict[str, Any]]:
    """The page reviewer's hook (`_Run.opening`): the links and disagreements as one text block,
    logged as `dims_linked`, `dims_unlinked`, `part_disagreements` (and the links themselves,
    `dim_parts`). Nothing when the page's labels or line work cannot be had. Never raises."""
    review = run.review
    knowledge = getattr(review, "knowledge", None)
    try:
        own = _page_parts(run, review.page, review.found, review.size)
        if own is None:
            return []
        pages = [own]
        context = None
        try:
            context = (context_of or run._context)()
            if not context.built and not context.ready(0):
                context = None
        except Exception:  # noqa: BLE001 - the page's own links are still given
            context = None
        if context is not None:
            for other, _tag, _mine in context.pair_pages(review.page)[:3]:
                try:
                    parts = _page_parts(run, other, context.phrases(other), None)
                except Exception:  # noqa: BLE001 - that drawing is left out
                    _log.info("dim parts p%s: p%s not read", review.page, other)
                    continue
                if parts is not None:
                    pages.append(parts)
        lines = facts(pages)
        found = disagreements(pages)
    except Exception:  # noqa: BLE001 - the page is reviewed without them
        _log.exception("dim parts p%s: not worked out", getattr(review, "page", "?"))
        return []
    if isinstance(knowledge, dict):
        knowledge["dims_linked"] = len(own.linked)
        knowledge["dims_unlinked"] = len(own.unlinked)
        knowledge["part_disagreements"] = [d.as_dict() for d in found]
        knowledge["dim_parts"] = [d.as_dict() for d in own.linked]
    if not lines:
        return []
    return [{"type": "text", "text": _LEAD + "\n" + fence("\n".join(lines), "dim_parts")}]


_QUESTIONS_HEAD: Final = (
    "More of code's questions (numbered after the ones above): the same part printed with "
    "different sizes in two views or on the item's other drawing, each size tied to the part by its "
    "extension lines and a callout's arrow. Settle each the same way (report_issue with question "
    '"Qn", or answer_question with a short reason):'
)


def question_blocks(run: Any) -> list[dict[str, Any]]:
    """The disagreements `blocks` found, added to the page's numbered questions (`view_questions`,
    numbered after its own; the reviewer settles them like the others, the stop-hook and the log
    count them). Call after `view_questions.opening_blocks`. Never raises."""
    review = run.review
    try:
        knowledge = getattr(review, "knowledge", None) or {}
        found = knowledge.get("part_disagreements") or []
        asked = getattr(review, "questions", None)
        if not found or not isinstance(asked, list):
            return []
        questions = import_module("app.review.assistant.view_questions")
        made = []
        for d in found[:MAX_DISAGREEMENTS]:
            said = "; ".join(f"{side['where']} {side['value']}" for side in d["sides"])
            boxes = tuple(
                (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
                for side in d["sides"]
                if side.get("page") == review.page
                for b in side.get("boxes") or []
            )
            text = f"{d['part']} {d['direction']}: {said}. Which is right?"
            made.append(questions.Question(len(asked) + 1, "A", text, boxes, "dim_parts"))
            asked.append(made[-1])
        return [
            {
                "type": "text",
                "text": _QUESTIONS_HEAD
                + "\n"
                + fence(questions.listing(made), "dim_parts_questions"),
            }
        ]
    except Exception:  # noqa: BLE001 - the facts block still says it
        _log.exception("dim parts p%s: questions not added", getattr(review, "page", "?"))
        return []
