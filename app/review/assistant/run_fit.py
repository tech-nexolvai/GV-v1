"""GV's run fit: a vendor's run checked against the architect's by GV's site-fit rule, $0, no AI.

A run is a row of parts between two ends (a base cabinet run from wall to wall: filler, drawer
bank, door cabinets, filler) with its overall printed above them. The architect draws it as
designed; the vendor draws it fitted to the site, so the overall may differ. GV's rule for how it
is fitted is DATA, per product, in the product registry (`vocabulary/products.yaml`, `run_fit`):
each part kind is `fixed` (keeps its design size) or `flex` (absorbs the site difference), the
flex parts share the difference `equally`, and the height stack may keep its design sizes. Cabinets
carry the client lead's worked sheets: fillers and the drawer bank keep their size, the door
cabinets all change by the same amount. A product with no `run_fit` entry gets no check; a new
product's rule is an entry there, never code here.

**The checks** (code's arithmetic, exact to 1/16"):

- the architect's parts add up to the architect's overall (else the run is not judged);
- each fixed part = the architect's part ("the filler should stay 2"");
- the vendor's parts add up to the vendor's overall; when they do not, the run's string is Wrong
  (the vendor's own sum, with the drawing checks of `vendor_consistency`) and its flex parts are
  not judged (which number is wrong is not known);
- each flex part = the architect's part + (vendor overall - architect overall) / the number of
  flex parts ("the door cabinet should be 15" = (85" - 2" - 21" - 2") / 4"); a share that is not
  a whole 1/16" is not judged;
- the height stack (when the product's rule has one): each height = the architect's, and the
  stack adds up to its printed total.

Every Wrong carries the arithmetic as its proof; the green "should be" is the value the rule
gives. A part that is right is a Right item, so the architect comparison's red on a door the site
made smaller (18" designed, 15" drawn) is not raised: the rule, not the drawing, governs it.

**Reading the run, proved or nothing** (zero false reds):

- The parts are a row of the vendor's dimension labels (`dims.json`) that tile the overall end to
  end (`vendor_consistency`'s chain); a narrow part whose label code placed on another line (a 2"
  filler printed wider than its own span) is taken into the row only when the line work shows its
  span marked at both ends on the row's line and exactly one label stands over it, on the row's
  text line, or on the row's line beyond the run's end (a 1" top printed past its ticks).
- Each part's kind comes from its printed words (`2" FILLER`) or from what is drawn inside its
  column: `plain` (a strip), `stacked` (drawer fronts) or `crossed` (door swings), as the
  product's rule names them. A plain part is told by its look only at the run's ends. The vendor's
  and the architect's runs must name the same kinds in the same order (one side's kind fills the
  other's gap); any part neither tells, or a disagreement, and the run is not judged.
- The vendor's run is paired with the one architect run (of the work item's architect drawings,
  `simple_check.worklist`) with the same number of parts and the same kinds; none or several: not
  judged.

What code cannot prove is left to the AI harness (the compare call), never guessed.

Findings are in the chat finding shape (`created_by: "run_fit"`, filed under the product). A fixed
part's item is the architect's label against the vendor's (both printed, paired by code); a flex
part's item compares the vendor's label with the rule's value (`source: rules`, the rule's
arithmetic as its proof). Verification: `tests/review/assistant/test_run_fit.py` (made-up runs,
a made-up non-cabinet product too).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import vendor_consistency as consistency
from app.review.assistant.ledger import format_inches
from vocabulary.product_registry import Product, RunFit, RunPartKind, registry

__all__ = [
    "CREATED_BY",
    "TOLERANCE",
    "DrawingLabels",
    "Judged",
    "Part",
    "Run",
    "RunJudgement",
    "add_to_answer",
    "check_drawings",
    "check_pack",
    "covered_labels",
    "judge_run",
    "look",
    "own_item",
    "runs_on",
]

_log = logging.getLogger(__name__)

CREATED_BY: Final = "run_fit"
#: Two values closer than this are the same (a sixteenth: the drawings' own step).
TOLERANCE: Final = Fraction(1, 16)
#: The rule's method word in a Wrong flex part's `settled` (`proof.proof_line` says its `how`).
METHOD: Final = "run_fit"
#: A part's label prints on the row's own text line: its box top and bottom within this (points).
TEXT_ROW_PT: Final = 2.0
#: A label stands on the row's line beyond the run's end: its line this close to the row's (points).
ROW_PT: Final = 0.6
#: A span's end is marked when a slanted stroke this long crosses the line there (points): a tick
#: drawn larger than `vendor_consistency`'s, up to a drafter's 3/32" tick at a large scale.
MARK_MIN_PT: Final = 0.8
MARK_MAX_PT: Final = 12.0
MARK_REACH_PT: Final = 0.8
#: A label taken in from beyond the run's end is drawn within this share of its value (or 1"), at
#: the run's own scale: a sanity check of which span it is, never of its value (that is printed).
DRAWN_SHARE: Final = Fraction(35, 100)
#: Inside a part's column: diagonals both ways, each at least this share of the column's width and
#: reaching down `CROSSED_HEIGHT` of its height (together at least that much ink too), are
#: `crossed` (door swings, dashed or not); at
#: most `NO_DIAGONAL_SHARE` of its height in all is no diagonal at all (a tick or an arrowhead).
CROSSED_SHARE: Final = 0.5
CROSSED_HEIGHT: Final = 0.25
#: ... and each slant's pieces reach across this share of the column's width (a swing runs from one
#: side to the other); pieces shorter than `MIN_DIAGONAL_PT` (a glyph's stroke) are no swing.
CROSSED_SPAN: Final = 0.6
MIN_DIAGONAL_PT: Final = 1.0
NO_DIAGONAL_SHARE: Final = 0.1
#: A full-width line covers at least this share of the column's width.
FULL_WIDTH: Final = 0.9

type Segment = tuple[tuple[float, float], tuple[float, float]]
type Segments = Callable[[str, int], Sequence[Segment] | None]
type Outcome = Literal["right", "wrong"]
type Look = Literal["plain", "stacked", "crossed"]


# ---- the arithmetic (pure) ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Judged:
    """One vendor part judged by the rule."""

    index: int
    outcome: Outcome
    should_be: Fraction
    proof: str
    """The rule's arithmetic in one line ("door cabinet should be 15" = (85" - 2" - 21" - 2") / 4")."""


@dataclass(frozen=True, slots=True)
class RunJudgement:
    parts: tuple[Judged | None, ...]
    """Per vendor part, in order: judged, or `None` (not judged: see `why_not`)."""
    adds_up: bool
    """The vendor's parts add up to its printed overall."""
    change: Fraction | None
    """What each flex part changes by, when the rule gives a size code can draw."""
    why_not: str = ""


def _inches(value: Fraction) -> str:
    return format_inches(value)


def _same(a: Fraction, b: Fraction) -> bool:
    return abs(a - b) <= TOLERANCE


def _whole_sixteenth(value: Fraction) -> bool:
    return (value * 16).denominator == 1


def judge_run(
    design: Sequence[Fraction],
    site: Sequence[Fraction],
    roles: Sequence[str],
    names: Sequence[str],
    design_total: Fraction,
    site_total: Fraction,
) -> RunJudgement | None:
    """The rule on one run: the architect's parts (`design`, adding up to `design_total`) and the
    vendor's (`site`, its overall `site_total`), each part's `role` (fixed or flex) and its name.
    `None` when the run cannot be judged at all (the architect's parts do not add up, the two runs
    differ in length, no flex part)."""
    count = len(design)
    if not count or len(site) != count or len(roles) != count or len(names) != count:
        return None
    if not _same(sum(design, Fraction(0)), design_total):
        return None
    flex = [i for i, role in enumerate(roles) if role == "flex"]
    if not flex:
        return None
    adds_up = _same(sum(site, Fraction(0)), site_total)
    change = (site_total - design_total) / len(flex)
    why_not = ""
    if not _whole_sixteenth(change):
        why_not = (
            f"the site difference {_inches(site_total - design_total)} over {len(flex)} flex parts "
            'is not a whole 1/16"'
        )
    elif not adds_up:
        why_not = "the vendor's parts do not add up to its overall"
    elif any(design[i] + change <= 0 for i in flex):
        why_not = "the site is too small for the flex parts"
    fixed_words = [_inches(design[i]) for i, role in enumerate(roles) if role == "fixed"]
    flex_equal = len({design[i] for i in flex}) == 1
    out: list[Judged | None] = []
    for i, (value, role) in enumerate(zip(site, roles, strict=True)):
        if role == "fixed":
            should = design[i]
            proof = f"{names[i]} should stay {_inches(should)}: it keeps its design size"
        elif why_not:
            out.append(None)
            continue
        else:
            should = design[i] + change
            if flex_equal:
                minus = "".join(f" - {w}" for w in fixed_words)
                proof = (
                    f"{names[i]} should be {_inches(should)} = ({_inches(site_total)}{minus}) / "
                    f"{len(flex)}"
                )
            else:
                proof = (
                    f"{names[i]} should be {_inches(should)} = {_inches(design[i])} + "
                    f"({_inches(site_total)} - {_inches(design_total)}) / {len(flex)}"
                )
        out.append(Judged(i, "right" if _same(value, should) else "wrong", should, proof))
    return RunJudgement(tuple(out), adds_up, None if why_not and adds_up else change, why_not)


def judge_stack(
    design: Sequence[Fraction],
    site: Sequence[Fraction],
    design_total: Fraction,
    site_total: Fraction,
    names: Sequence[str],
) -> RunJudgement | None:
    """A height stack: every height keeps its design size and the stack adds up to its total."""
    if len(design) != len(site) or not design or not _same(sum(design, Fraction(0)), design_total):
        return None
    adds_up = _same(sum(site, Fraction(0)), site_total)
    out = tuple(
        Judged(
            i,
            "right" if _same(value, design[i]) else "wrong",
            design[i],
            f"{names[i]} should stay {_inches(design[i])}: the heights keep their design sizes",
        )
        for i, value in enumerate(site)
    )
    return RunJudgement(out, adds_up, Fraction(0))


# ---- reading a run ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrawingLabels:
    """One drawing's labels as the set pack has them (its file's own page)."""

    file: str
    page: int
    """The file's own page."""
    labels: tuple[Mapping[str, Any], ...]
    phrases: tuple[Mapping[str, Any], ...] = ()
    ref: str = ""
    """The work list's drawing ref (`p3.v1`, `p1001.a1`)."""


@dataclass(frozen=True, slots=True)
class Part:
    label: consistency.Label
    start: float
    end: float
    taken_in: bool = False
    """Code placed its label on this span itself (see the module)."""


@dataclass(frozen=True, slots=True)
class Run:
    drawing: DrawingLabels
    overall: consistency.Label
    parts: tuple[Part, ...]
    orientation: str
    across: float
    """Where the parts' line is across its axis."""

    @property
    def taken_in(self) -> bool:
        return any(p.taken_in for p in self.parts)

    def as_string(self) -> list[consistency.Label]:
        """The parts as labels on their own spans (for `vendor_consistency`'s string check)."""
        out: list[consistency.Label] = []
        for part in self.parts:
            label = part.label
            if part.taken_in:
                label = replace(
                    label,
                    start=part.start,
                    end=part.end,
                    across=self.across,
                    extensions=(
                        abs(part.start - self.overall.start) <= consistency.END_PT,
                        abs(part.end - self.overall.end) <= consistency.END_PT,
                    ),
                )
            out.append(label)
        return out


def _marked(segments: Sequence[Segment], orientation: str, across: float, at: float) -> bool:
    """A slanted stroke (a tick or an arrowhead's side) crosses the line at `at`."""
    flip = orientation == "v"
    px, py = (across, at) if flip else (at, across)
    for (ax, ay), (bx, by) in segments:
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        if not MARK_MIN_PT <= length <= MARK_MAX_PT:
            continue
        along, square = (abs(dy), abs(dx)) if flip else (abs(dx), abs(dy))
        if square < 0.17 * length or along < 0.17 * length:
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (length * length)))
        cx, cy = ax + t * dx - px, ay + t * dy - py
        if (cx * cx + cy * cy) ** 0.5 <= MARK_REACH_PT:
            return True
    return False


def _middle(box: tuple[float, float, float, float], orientation: str) -> float:
    """A box's middle along the axis."""
    x0, y0, x1, y1 = box
    return (x0 + x1) / 2 if orientation == "h" else (y0 + y1) / 2


def _text_band(box: tuple[float, float, float, float], orientation: str) -> tuple[float, float]:
    x0, y0, x1, y1 = box
    return (y0, y1) if orientation == "h" else (x0, x1)


def _scale(parts: Sequence[consistency.Label]) -> float | None:
    """Points per inch of the run's own printed parts (their lines' total over their values)."""
    inches = sum((p.inches for p in parts), Fraction(0))
    span = sum(p.span for p in parts)
    return span / float(inches) if inches > 0 and span > 0 else None


def _take_in(
    gap: tuple[float, float],
    outer: float | None,
    row: Sequence[consistency.Label],
    labels: Sequence[consistency.Label],
    used: set[str],
    segments: Sequence[Segment],
) -> consistency.Label | None:
    """The one label of a gap in the row (see the module), or `None`."""
    g0, g1 = gap
    ref = row[0]
    if not (
        _marked(segments, ref.orientation, ref.across, g0)
        and _marked(segments, ref.orientation, ref.across, g1)
    ):
        return None
    band = _text_band(ref.box, ref.orientation)
    found: list[tuple[consistency.Label, bool]] = []
    for label in labels:
        if label.id in used or label.orientation != ref.orientation:
            continue
        middle = _middle(label.box, label.orientation)
        top, bottom = _text_band(label.box, label.orientation)
        over = (
            g0 <= middle <= g1
            and abs(top - band[0]) <= TEXT_ROW_PT
            and abs(bottom - band[1]) <= TEXT_ROW_PT
        )
        beyond = (
            outer is not None
            and abs(label.across - ref.across) <= ROW_PT
            and (
                (outer == g0 and abs(label.end - g0) <= consistency.GAP_PT)
                or (outer == g1 and abs(label.start - g1) <= consistency.GAP_PT)
            )
        )
        if over or beyond:
            found.append((label, beyond and not over))
    if len(found) != 1:
        return None
    label, away = found[0]
    if not label.clean:
        return None
    if away:
        # Printed away from its span: its drawn size must be near its value too (a sanity check of
        # which span it is, never its value: the value is the printed one).
        scale = _scale(row)
        if scale is None:
            return None
        drawn = Fraction((g1 - g0) / scale).limit_denominator(64)
        if abs(drawn - label.inches) > max(Fraction(1), DRAWN_SHARE * label.inches):
            return None
    return label


def _string(
    overall: consistency.Label,
    row: Sequence[consistency.Label],
    labels: Sequence[consistency.Label],
    segments: Sequence[Segment] | None,
) -> list[Part] | None:
    """The row as the overall's string: tiled end to end by its own labels, or with a narrow end
    part taken in (see the module)."""
    whole = consistency.proved_chain(overall, row)
    if whole is not None:
        return [Part(p, p.start, p.end) for p in whole]
    if segments is None:
        return None
    lo, hi = overall.start - consistency.GAP_PT, overall.end + consistency.GAP_PT
    inside = sorted((la for la in row if la.start >= lo and la.end <= hi), key=lambda la: la.start)
    if not inside or any(
        la not in inside
        and (
            (la.start < lo and la.end > overall.start + consistency.GAP_PT)
            or (la.start < overall.end - consistency.GAP_PT and la.end > hi)
        )
        for la in row
    ):
        return None
    for a, b in pairwise(inside):
        if abs(a.end - b.start) > consistency.GAP_PT:
            return None
    used = {overall.id, *(p.id for p in inside)}
    parts = [Part(p, p.start, p.end) for p in inside]
    if inside[0].start - overall.start > consistency.GAP_PT:
        gap = (overall.start, inside[0].start)
        label = _take_in(gap, overall.start, inside, labels, used, segments)
        if label is None:
            return None
        used.add(label.id)
        parts.insert(0, Part(label, gap[0], gap[1], taken_in=True))
    elif abs(inside[0].start - overall.start) > consistency.END_PT:
        return None
    if overall.end - inside[-1].end > consistency.GAP_PT:
        gap = (inside[-1].end, overall.end)
        label = _take_in(gap, overall.end, inside, labels, used, segments)
        if label is None:
            return None
        parts.append(Part(label, gap[0], gap[1], taken_in=True))
    elif abs(inside[-1].end - overall.end) > consistency.END_PT:
        return None
    return parts if len(parts) >= 2 else None


def _loose(raw: Mapping[str, Any], file: str, page: int) -> consistency.Label | None:
    """A label code found no dimension line for, read by its box alone: it can only be taken in
    over a marked span (`_take_in`), never be an overall or a row's part."""
    inches = _fraction(raw.get("inches"))
    box = raw.get("box")
    orientation = raw.get("orientation")
    if inches is None or inches <= 0 or orientation not in ("h", "v"):
        return None
    if not isinstance(box, Sequence) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    start, end = (x0, x1) if orientation == "h" else (y0, y1)
    return consistency.Label(
        id=str(raw.get("id") or ""),
        file=file,
        page=page,
        text=" ".join(str(raw.get("text") or "").split()),
        inches=inches,
        box=(x0, y0, x1, y1),
        side=str(raw.get("side") or "unknown"),
        orientation=str(orientation),
        route=str(raw.get("route") or "text"),
        units=raw.get("units") if isinstance(raw.get("units"), str) else None,
        start=start,
        end=end,
        across=float("nan"),
        extensions=(False, False),
        half=None,
        raw=raw,
    )


def runs_on(drawing: DrawingLabels, segments: Segments | None) -> list[Run]:
    """Every run (an overall and the row that tiles it) code can prove on one drawing."""
    labels: list[consistency.Label] = []
    loose: list[consistency.Label] = []
    for raw in drawing.labels:
        found = consistency.read_label(raw, drawing.file, drawing.page)
        if found is not None:
            labels.append(found)
        elif (lineless := _loose(raw, drawing.file, drawing.page)) is not None:
            loose.append(lineless)
    lines = segments(drawing.file, drawing.page) if segments is not None else None
    out: list[Run] = []
    for orientation in ("h", "v"):
        axis = [la for la in labels if la.orientation == orientation]
        rows = consistency.label_rows(axis)
        for overall in axis:
            if not overall.clean or not overall.centred():
                continue
            for row in rows:
                if abs(row[0].across - overall.across) < consistency.ROW_APART_PT:
                    continue
                others = axis + [la for la in loose if la.orientation == orientation]
                parts = _string(overall, row, others, lines)
                if parts is not None:
                    out.append(Run(drawing, overall, tuple(parts), orientation, row[0].across))
    return out


# ---- what a part is ------------------------------------------------------------------------------


def look(
    segments: Sequence[Segment], start: float, end: float, top: float, bottom: float
) -> Look | None:
    """What is drawn inside a horizontal run's part column `start..end` (across) from `top` to
    `bottom` (down): `crossed` (diagonals both ways), `stacked` (two or more full-width lines
    inside, no diagonals), `plain` (no line inside, no diagonals), else `None`."""
    width, height = end - start, bottom - top
    if width <= 0 or height <= 0:
        return None
    ink = 0.0
    #: Per slant (down-right, up-right): its pieces' ink and extent across and down.
    slants: dict[bool, list[float]] = {}
    rows: dict[float, list[tuple[float, float]]] = {}
    for (ax, ay), (bx, by) in segments:
        if min(ax, bx) < start - 0.6 or max(ax, bx) > end + 0.6:
            continue
        if min(ay, by) < top + 2.0 or max(ay, by) > bottom + 0.6:
            continue
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        if length < 0.2:
            continue
        if abs(dy) <= 0.05 * length:
            rows.setdefault(round((ay + by) / 4, 1) * 2, []).append((min(ax, bx), max(ax, bx)))
        elif abs(dx) > 0.05 * length:
            ink += length
            if length < MIN_DIAGONAL_PT:
                continue  # a glyph's stroke or a dot: no door swing
            got = slants.setdefault(dx * dy > 0, [0.0, ax, ax, ay, ay])
            got[0] += length
            got[1:] = [
                min(got[1], ax, bx),
                max(got[2], ax, bx),
                min(got[3], ay, by),
                max(got[4], ay, by),
            ]
    full: list[float] = []
    for y, pieces in rows.items():
        covered, reach = 0.0, None
        for lo, hi in sorted(pieces):
            if reach is None or lo > reach:
                covered += hi - lo
                reach = hi
            elif hi > reach:
                covered += hi - reach
                reach = hi
        if covered >= FULL_WIDTH * width:
            full.append(y)
    full.sort()
    inner = [y for y in full[1:-1]]
    if (
        len(slants) == 2
        and all(
            length >= CROSSED_SHARE * width
            and x1 - x0 >= CROSSED_SPAN * width
            and y1 - y0 >= CROSSED_HEIGHT * height
            for length, x0, x1, y0, y1 in slants.values()
        )
        and sum(v[0] for v in slants.values()) >= CROSSED_HEIGHT * height
    ):
        return "crossed"
    if ink > NO_DIAGONAL_SHARE * height:
        return None
    if len(inner) >= 2:
        return "stacked"
    if not inner and len(full) >= 2:
        return "plain"
    return None


_WORD: Final = re.compile(r"[a-z]+(?:\s+[a-z]+)*")


def _words(label: consistency.Label, phrases: Sequence[Mapping[str, Any]]) -> str:
    """The words printed with a part's dimension: its own text and the one phrase it sits in."""
    own = label.text.lower()
    x0, y0, x1, y1 = label.box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    holding = []
    for phrase in phrases:
        box = phrase.get("box")
        if isinstance(box, Sequence) and len(box) == 4:
            try:
                bx0, by0, bx1, by1 = (float(v) for v in box)
            except (TypeError, ValueError):
                continue
            if min(bx0, bx1) <= cx <= max(bx0, bx1) and min(by0, by1) <= cy <= max(by0, by1):
                holding.append(" ".join(str(phrase.get("text") or "").lower().split()))
    return " ".join([own, *holding[:1]]) if len(holding) <= 1 else own


def _named(words: str, kind: RunPartKind) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", words) for w in kind.words)


def _bottom(run: Run) -> float | None:
    """How far down the run's parts are drawn: the overall's extension lines' far ends."""
    ends = run.overall.raw.get("extension")
    found: list[float] = []
    if isinstance(ends, Sequence):
        for line in ends:
            try:
                (_fx, fy), (_nx, ny) = ((float(p[0]), float(p[1])) for p in line)
            except (TypeError, ValueError, IndexError):
                continue
            found.append(max(fy, ny))
    return max(found) if found else None


def kinds_of(run: Run, fit: RunFit, segments: Segments | None) -> list[str | None]:
    """Each part's kind under the product's rule, from its words or its look (see the module)."""
    lines = segments(run.drawing.file, run.drawing.page) if segments is not None else None
    bottom = _bottom(run) if run.orientation == "h" else None
    out: list[str | None] = []
    last = len(run.parts) - 1
    for index, part in enumerate(run.parts):
        words = _words(part.label, run.drawing.phrases)
        named = [kind for kind in fit.parts if kind.words and _named(words, kind)]
        if len(named) == 1:
            out.append(named[0].kind)
            continue
        if named or lines is None or bottom is None:
            out.append(None)
            continue
        drawn = look(lines, part.start, part.end, run.across, bottom)
        fits = [
            kind
            for kind in fit.parts
            if kind.drawn is not None
            and kind.drawn == drawn
            and (kind.place == "any" or index in (0, last))
        ]
        out.append(fits[0].kind if len(fits) == 1 else None)
    return out


def _merged(vendor: Sequence[str | None], architect: Sequence[str | None]) -> list[str] | None:
    """One kind per place from both drawings: the same, or one side's where the other cannot
    tell; `None` when they disagree or neither tells."""
    if len(vendor) != len(architect):
        return None
    out: list[str] = []
    for v, a in zip(vendor, architect, strict=True):
        if v is not None and a is not None and v != a:
            return None
        kind = v or a
        if kind is None:
            return None
        out.append(kind)
    return out


# ---- pairing and findings -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Paired:
    product: Product
    vendor: Run
    architect: Run
    kinds: tuple[str, ...]
    judged: RunJudgement
    stack: bool = False
    """A height stack (its parts are heights), not the run's widths."""


def _values(run: Run) -> list[Fraction]:
    return [p.label.inches for p in run.parts]


def _stack_of(run: Run, candidates: Sequence[Run]) -> Run | None:
    """The one vertical run beside a horizontal run's parts (left or right of them, within the
    height they are drawn), or `None`."""
    bottom = _bottom(run)
    if bottom is None:
        return None
    lo, hi = run.overall.start, run.overall.end
    found = [
        c
        for c in candidates
        if c.orientation == "v"
        and not lo <= c.across <= hi
        and c.overall.start >= run.across - 2.0
        and c.overall.end <= bottom + 2.0
    ]
    return found[0] if len(found) == 1 else None


def check_drawings(
    vendor: DrawingLabels,
    architects: Sequence[DrawingLabels],
    segments: Segments | None,
    products: Sequence[Product],
    *,
    by_run: bool = False,
) -> list[Paired]:
    """The vendor drawing's runs judged against the architect drawings' (see the module).
    `by_run`: the work list paired neither drawing (`_left_over`), so the run itself must prove
    they are one: besides the same kinds in the same order, every fixed part prints the same."""
    vendor_runs = runs_on(vendor, segments)
    architect_runs = [run for drawing in architects for run in runs_on(drawing, segments)]
    out: list[Paired] = []
    for product in products:
        fit = product.run_fit
        if fit is None:
            continue
        roles = {kind.kind: kind.role for kind in fit.parts}
        for run in (r for r in vendor_runs if r.orientation == "h"):
            mine = kinds_of(run, fit, segments)
            matches: list[tuple[Run, list[str]]] = []
            for other in (r for r in architect_runs if r.orientation == "h"):
                if len(other.parts) != len(run.parts):
                    continue
                merged = _merged(mine, kinds_of(other, fit, segments))
                if merged is not None:
                    matches.append((other, merged))
            if len(matches) != 1:
                continue
            other, merged = matches[0]
            judged = judge_run(
                _values(other),
                _values(run),
                [roles[k] for k in merged],
                _names(merged, len(run.parts)),
                other.overall.inches,
                run.overall.inches,
            )
            if judged is None:
                continue
            if by_run and any(
                roles[kind] == "fixed" and not _same(mine_part, theirs_part)
                for kind, mine_part, theirs_part in zip(
                    merged, _values(run), _values(other), strict=True
                )
            ):
                continue
            out.append(Paired(product, run, other, tuple(merged), judged))
            if fit.height_stack:
                mine_stack = _stack_of(run, vendor_runs)
                theirs = _stack_of(other, architect_runs)
                if mine_stack is None or theirs is None:
                    continue
                if len(mine_stack.parts) != len(theirs.parts):
                    continue
                count = len(mine_stack.parts)
                # Up the page is up the cabinet: the floor's height first.
                names = [f"height {count - i} of {count} from the floor" for i in range(count)]
                stacked = judge_stack(
                    _values(theirs),
                    _values(mine_stack),
                    theirs.overall.inches,
                    mine_stack.overall.inches,
                    names,
                )
                if stacked is not None:
                    kinds = tuple("height" for _ in range(count))
                    out.append(Paired(product, mine_stack, theirs, kinds, stacked, stack=True))
    return out


def _names(kinds: Sequence[str], count: int) -> list[str]:
    """Each part's name: "filler (left)", "door cabinet 2" (numbered when its kind repeats)."""
    totals = {kind: kinds.count(kind) for kind in kinds}
    seen: dict[str, int] = {}
    out: list[str] = []
    for index, kind in enumerate(kinds):
        seen[kind] = seen.get(kind, 0) + 1
        if totals[kind] == 1:
            out.append(kind)
        elif totals[kind] == 2 and index in (0, count - 1):
            out.append(f"{kind} ({'left' if index == 0 else 'right'})")
        else:
            out.append(f"{kind} {seen[kind]}")
    return out


def _fraction(value: object) -> Fraction | None:
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _box(label: consistency.Label) -> dict[str, Any]:
    x0, y0, x1, y1 = label.box
    return {"page": label.page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": label.file}


def _provenance(label: consistency.Label) -> dict[str, Any]:
    return {
        "id": label.id,
        "page": label.page,
        "file": label.file,
        "text": label.text,
        "inches": label.raw.get("inches"),
        "route": label.route,
        "side": label.side,
        "box": list(label.box),
    }


def _item(paired: Paired, name: str, area: str | None) -> str:
    place = f"{area}: " if area else ""
    what = "height" if paired.stack else "width"
    if paired.stack:
        return f"{place}{paired.product.label.lower()} run, {name}"
    return f"{place}{paired.product.label.lower()} run, {name} {what}"


def _rule_words(paired: Paired) -> str:
    return f"GV's site-fit rule for {paired.product.plural.lower()}"


def findings_of(
    paired: Paired, area: str | None = None, segments: Segments | None = None
) -> list[dict[str, Any]]:
    """One run's items: each judged part Right or Wrong, and the run's string Wrong when the
    vendor's parts do not add up (with `vendor_consistency`'s drawing checks)."""
    vendor, architect, judged = paired.vendor, paired.architect, paired.judged
    names = (
        [
            f"height {len(vendor.parts) - i} of {len(vendor.parts)} from the floor"
            for i in range(len(vendor.parts))
        ]
        if paired.stack
        else _names(paired.kinds, len(vendor.parts))
    )
    fit = paired.product.run_fit
    assert fit is not None
    out: list[dict[str, Any]] = []
    for index, verdict in enumerate(judged.parts):
        if verdict is None:
            continue
        mine = vendor.parts[index].label
        theirs = architect.parts[index].label
        kind = fit.kind(paired.kinds[index])
        fixed = paired.stack or (kind is not None and kind.role == "fixed")
        rule = f"{_rule_words(paired)}: {verdict.proof}"
        finding: dict[str, Any] = {
            "page": mine.page,
            "item": _item(paired, names[index], area)[:160],
            "category": paired.product.id,
            "vendor": mine.text,
            "status": "same" if verdict.outcome == "right" else "different",
            "verified": False,
            "vendor_box": _box(mine),
            "dims": {"vendor": _provenance(mine)},
            "created_by": CREATED_BY,
            "ledger_ids": [mine.ref, theirs.ref],
            "run_fit": {
                "product": paired.product.id,
                "kind": paired.kinds[index],
                "proof": verdict.proof,
                "should_be": _inches(verdict.should_be),
            },
        }
        if fixed or _same(verdict.should_be, theirs.inches):
            # The architect's own label is the value: both printed, paired by place in the run.
            finding.update(
                {
                    "architect": theirs.text,
                    "source": "architect",
                    "arch_box": _box(theirs),
                    "paired_by": "code",
                    "why": (
                        f"{rule}. The architect prints {theirs.text}; the vendor prints "
                        f"{mine.text}. Code paired the two by their place in the run "
                        f"({index + 1} of {len(vendor.parts)})."
                    ),
                }
            )
            finding["dims"]["architect"] = _provenance(theirs)
        else:
            finding.update(
                {
                    "architect": _inches(verdict.should_be),
                    "source": "rules",
                    "why": (
                        f"{rule} (the architect's {theirs.text}, the run's overall "
                        f"{architect.overall.text} designed and {vendor.overall.text} on the "
                        f"vendor's drawing). The vendor prints {mine.text}."
                    ),
                }
            )
            if verdict.outcome == "wrong":
                finding["settled"] = {"outcome": "wrong", "method": METHOD, "how": rule + "."}
                finding["checks"] = {"rule": "agrees"}
        finding["vs_page"] = architect.overall.page
        out.append(finding)
    if not judged.adds_up:
        drawing = [
            found
            for raw in vendor.drawing.labels
            if (found := consistency.read_label(raw, vendor.drawing.file, vendor.drawing.page))
        ]
        made = consistency.judge_string(vendor.overall, vendor.as_string(), drawing, segments, area)
        if made is not None:
            made["created_by"] = CREATED_BY
            made["category"] = paired.product.id
            made["string_geometry"] = "verified"
            out.append(made)
    return out


# ---- the set pack -------------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _drawing(
    drawing: Any, dims_pages: Mapping[tuple[str, int], Mapping[str, Any]]
) -> DrawingLabels | None:
    page = int(drawing.own_page)
    entry = dims_pages.get((str(drawing.file), page))
    if entry is None:
        return None
    wanted = {label.id for label in drawing.labels}
    labels = tuple(
        raw
        for raw in entry.get("labels") or []
        if isinstance(raw, Mapping) and str(raw.get("id")) in wanted
    )
    phrases = tuple(p for p in entry.get("phrases") or [] if isinstance(p, Mapping))
    return DrawingLabels(str(drawing.file), page, labels, phrases, str(drawing.ref))


def check_pack(
    folder: Path | None,
    uploads: Path | None,
    main_file: str | None = None,
    products: Sequence[Product] | None = None,
) -> list[dict[str, Any]]:
    """Every item of the set pack's runs (`dims.json`, the work list's pairing, the uploads' line
    work), for the products whose rule is in the registry (`products`: the check's own)."""
    if folder is None:
        return []
    chosen = [p for p in (products if products is not None else registry().products) if p.run_fit]
    if not chosen:
        return []
    worklist = import_module("app.review.assistant.simple_check.worklist")
    try:
        work = worklist.build_worklist(Path(folder), main_file)
    except (FileNotFoundError, ValueError):
        return []
    dims = _read(Path(folder) / "dims.json")
    if not isinstance(dims, Mapping):
        return []
    pages: dict[tuple[str, int], Mapping[str, Any]] = {}
    for entry in dims.get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            pages[(str(entry.get("file")), int(entry["page"]))] = entry
    segments = consistency.line_work(uploads, Path(folder)) if uploads is not None else None
    out: list[dict[str, Any]] = []
    for item in work.items:
        vendor = _drawing(item.vendor, pages)
        architects = [d for d in (_drawing(a, pages) for a in item.architect) if d is not None]
        if vendor is None or not architects:
            continue
        area = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", item.area or "").strip() or None
        try:
            for paired in check_drawings(vendor, architects, segments, chosen):
                out.extend(findings_of(paired, area, segments))
        except Exception:  # noqa: BLE001 - a drawing code cannot read gives no finding
            _log.exception("run fit: work item %s could not be checked", item.id)
    vendors, architects = _left_over(work, pages)
    if vendors and architects:
        for vendor in vendors:
            try:
                for paired in check_drawings(vendor, architects, segments, chosen, by_run=True):
                    out.extend(findings_of(paired, None, segments))
            except Exception:  # noqa: BLE001 - a drawing code cannot read gives no finding
                _log.exception("run fit: drawing %s could not be checked", vendor.ref)
    order = {"different": 0, "same": 1}
    return sorted(out, key=lambda f: (order.get(str(f["status"]), 2), f["page"]))


#: The work list's reasons for a vendor drawing of one file and an architect drawing of another
#: that it could not pair by their printed values (a run fitted to the site prints other sizes).
_UNPAIRED_VENDOR: Final = "not paired with an architect drawing"
_UNPAIRED_ARCHITECT: Final = "the architect's drawing no vendor drawing was paired with"


def _left_over(
    work: Any, pages: Mapping[tuple[str, int], Mapping[str, Any]]
) -> tuple[list[DrawingLabels], list[DrawingLabels]]:
    """The whole-page drawings of separate files the work list left unpaired: the vendor's and
    the architect's, each with its side's labels. Their runs pair only by the run (`by_run`)."""
    vendors: list[DrawingLabels] = []
    architects: list[DrawingLabels] = []
    for skip in work.skipped:
        if skip.reason.startswith(_UNPAIRED_VENDOR):
            side, into = "vendor", vendors
        elif skip.reason.startswith(_UNPAIRED_ARCHITECT):
            side, into = "architect", architects
        else:
            continue
        entry = pages.get((str(skip.file), int(skip.page)))
        if entry is None:
            continue
        labels = tuple(
            raw
            for raw in entry.get("labels") or []
            if isinstance(raw, Mapping) and raw.get("side") == side
        )
        phrases = tuple(p for p in entry.get("phrases") or [] if isinstance(p, Mapping))
        into.append(
            DrawingLabels(str(skip.file), int(skip.page), labels, phrases, str(skip.drawing))
        )
    return vendors, architects


# ---- the answer's own items ----------------------------------------------------------------------


def own_item(finding: Mapping[str, Any]) -> bool:
    return finding.get("created_by") == CREATED_BY


def _label_key(finding: Mapping[str, Any], main_file: str | None) -> tuple[str, int, str] | None:
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    vendor = (dims or {}).get("vendor")
    if not isinstance(vendor, Mapping) or not vendor.get("id"):
        return None
    try:
        page = int(vendor.get("page") or finding.get("page") or 0)
    except (TypeError, ValueError):
        return None
    return (str(vendor.get("file") or main_file or ""), page, str(vendor["id"]))


def covered_labels(
    live: Sequence[Mapping[str, Any]], main_file: str | None = None
) -> set[tuple[str, int, str]]:
    """The vendor labels the rule decided (Right or Wrong): `(file, own page, label id)`."""
    out: set[tuple[str, int, str]] = set()
    for finding in live:
        if own_item(finding):
            key = _label_key(finding, main_file)
            if key is not None:
                out.add(key)
    return out


def _decided(finding: Mapping[str, Any]) -> bool:
    decision = finding.get("decision")
    return isinstance(decision, Mapping) and bool(decision.get("decision"))


def add_to_answer(engine: Any) -> dict[str, int] | None:
    """The engine's hook (a full check, at its finish, $0): each run item added through the
    finding book (its checks, trust rule and product scope). An item the answer already has on the
    same vendor label (code's pair, a reading) is replaced: the rule governs that label. Returns
    how many were added and replaced."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    book = engine.book
    found = check_pack(
        workspace.set_pack, getattr(workspace, "uploads", None), getattr(book, "main_file", None)
    )
    if not found:
        return None
    trust = import_module("app.review.assistant.chat_trust")
    main_file = getattr(book, "main_file", None)
    counts = {"added": 0, "replaced": 0}
    with engine.finding_lock:
        live = book.live()
        consistency_overalls = {
            _label_key(f, main_file) for f in live if f.get("created_by") == consistency.CREATED_BY
        }
        for finding in found:
            key = _label_key(finding, main_file)
            if finding.get("source") == "vendor_arithmetic" and key in consistency_overalls:
                continue  # the vendor consistency check already has this string
            if key is not None:
                for other in book.live():
                    if own_item(other) or _decided(other) or _label_key(other, main_file) != key:
                        continue
                    if other.get("source") == "vendor_arithmetic":
                        continue  # a string is its own item
                    proof = (finding.get("run_fit") or {}).get("proof") or "GV's site-fit rule"
                    reason = f"Settled by GV Review: {proof}."
                    if book.absorb(str(other["id"])):
                        engine._show_dropped(str(other["id"]), reason)
                        counts["replaced"] += 1
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is None:
                continue
            extra: dict[str, Any] = {
                k: finding[k] for k in ("settled", "run_fit", "string_geometry") if k in finding
            }
            if finding.get("checks"):
                extra["checks"] = {**(added.get("checks") or {}), **finding["checks"]}
            updated = trust.judged({**added, **extra})
            if book.replace(updated):
                added = updated
            engine._show_finding(added)
            counts["added"] += 1
    return counts
