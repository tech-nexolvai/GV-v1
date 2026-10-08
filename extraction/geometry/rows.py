"""Dimension rows, their ticks, the slots between the ticks, the overall, and the label in each slot.

The plan "code finds the slots, AI reads them" (decided 2026-10-07) turns on one observation from
experiment E1: the structure of a width row — how many pieces, where each starts and ends, where the
overall sits — is in the drawing's own lines, and a few hundred lines of geometry recover it on every
page of both client sets in under a second. The AI is then asked to read a small crop per slot, not
to find the row. This module is that geometry, ported from E1's prototype into typed product code.

**What it does.** From a page's black and grey ink (`PageInk`: straight strokes, curves, characters,
and the boxes of the pasted drawings) it builds, for every horizontal run of line-work long enough to
be a dimension row:

* the **ticks** along it — the drawing's own slash marks first (tiny filled parallelograms or
  two-point diagonals, 1–3 pt on the client's sheets), keeping only the one slash size that row
  uses so a label's strokes touching the line never count; witness lines crossing the row as the
  fallback, with dashed centre lines and long strokes excluded; the breaks between the row's own
  pieces as the last resort, and such a row is returned but not ranked;
* the **slots** between consecutive ticks, merged scale-aware: two ticks are one only when closer
  than a fraction of the row's width, floored at a stroke width, so a 3/4" piece survives at any
  drawing scale;
* the **overall** — a two-tick row above or below whose end ticks coincide with the chain's ends;
  or one that is wider than the chain and encloses it, which is returned with the finding
  `pieces do not tile the overall` and the excess on each side rather than being dropped;
* the **label** in each slot — real text copied from the file (grouped by baseline first, so the
  mm digits and the `[inch]` digits that interleave along x on one client set come out as two
  labels instead of one jumble), or the box of a glyph-path run with no text, each flagged when it
  meets the edge of its drawing or the page, and a stacked fraction flagged as such.

**What it never does.** It reads no value: text is copied as printed, a glyph run is only located,
and nothing is parsed, summed or compared — a slot's width in points is a length on the page, not a
measurement. It never picks "the countertop": every row is returned, the ones passing the candidate
filter ranked (overall first, then most labelled, then widest) and the rest with the reason they were
set aside. It imports nothing from `verdict/`, and `verdict/` must never import it.

Everything is `Decimal` and `int`. A float's rounding is a verdict under exact match, so none is
allowed in anything the verdict path could one day consume — and the boxes here are what Phase 4
will crop and what the screen will draw.

Source: issue #980 · Plan: "Plan - Code finds the slots, AI reads them" (vault) ·
Verification: `tests/extraction/geometry/test_rows.py`
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from functools import partial
from itertools import pairwise

from evidence.coordinates import StoredPoint

__all__ = [
    "LABEL_IS_STACKED",
    "LABEL_TOUCHES_EDGE",
    "MEASURED_SETTINGS",
    "PIECES_DO_NOT_TILE",
    "Box",
    "CountertopRowCandidate",
    "GlyphCluster",
    "InkCharacter",
    "InkCurve",
    "InkLine",
    "LabelKind",
    "OverallRow",
    "PageInk",
    "PageRows",
    "Placement",
    "RowSettings",
    "Slot",
    "SlotLabel",
    "StoredBox",
    "TickSource",
    "Tiling",
    "build_rows",
]

#: Where a pdfplumber `(x, top)` in page points lands in stored coordinates. Supplied by the caller,
#: so this module never knows about dpi, rotation or crop boxes.
type Placement = Callable[[Decimal, Decimal], StoredPoint]

_ZERO = Decimal(0)
_TENTH = Decimal("0.1")
_ONE = Decimal(1)
_TWO = Decimal(2)

#: The findings' wording, so that a caller and a test name the same thing. A finding is about the
#: label a slot or the overall leads with; every other label found is still on the slot.
PIECES_DO_NOT_TILE = "pieces do not tile the overall"
LABEL_TOUCHES_EDGE = "a label touches the drawing or page edge"
LABEL_IS_STACKED = "a label is a stacked fraction"
FEET_AND_INCHES_ROW = "feet-and-inches: the architect's drawing, not the vendor's"
INSIDE_ARCHITECT_DRAWING = "inside the architect's drawing"
_FEET_AND_INCHES = re.compile(r"^\s*\d+\s*['’′]\s*-?\s*\d+(?:\s+\d+\s*/\s*\d+)?\s*[\"″]?\s*$")


# ---------------------------------------------------------------------------
# Input: a page's ink, in pdfplumber's frame (x to the right, `top` downward, page points)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    """An axis-aligned box in page points, pdfplumber's frame."""

    x0: Decimal
    top: Decimal
    x1: Decimal
    bottom: Decimal

    def __post_init__(self) -> None:
        for value in (self.x0, self.top, self.x1, self.bottom):
            if not isinstance(value, Decimal):
                raise TypeError("a Box takes Decimal coordinates only")
        if self.x1 < self.x0 or self.bottom < self.top:
            raise ValueError("a Box's x1 must not be left of x0, nor its bottom above its top")

    @property
    def width(self) -> Decimal:
        return self.x1 - self.x0

    @property
    def height(self) -> Decimal:
        return self.bottom - self.top

    @property
    def centre_x(self) -> Decimal:
        return (self.x0 + self.x1) / _TWO

    @property
    def centre_y(self) -> Decimal:
        return (self.top + self.bottom) / _TWO

    def overlaps_x(self, other: Box) -> bool:
        return self.x0 <= other.x1 and self.x1 >= other.x0

    def meets(self, other: Box) -> bool:
        return self.overlaps_x(other) and self.top <= other.bottom and self.bottom >= other.top

    def union(self, other: Box) -> Box:
        return Box(
            min(self.x0, other.x0),
            min(self.top, other.top),
            max(self.x1, other.x1),
            max(self.bottom, other.bottom),
        )


@dataclass(frozen=True, slots=True)
class StoredBox:
    """A box in stored coordinates: top-left, rotation applied, normalised to the visible page."""

    left: Decimal
    top: Decimal
    right: Decimal
    bottom: Decimal


@dataclass(frozen=True, slots=True)
class InkLine:
    """A straight two-point stroke drawn in the drawing's ink."""

    x0: Decimal
    y0: Decimal
    x1: Decimal
    y1: Decimal


@dataclass(frozen=True, slots=True)
class InkCurve:
    """A path drawn in the drawing's ink that is not a plain line or rectangle: its box and points.

    A rectangle belongs here too, as its four corners in order with `closed` set — the builder takes
    a thin one as a stroke and a wide one as its four edges, exactly as the prototype did.
    """

    box: Box
    points: tuple[tuple[Decimal, Decimal], ...]
    closed: bool = False


@dataclass(frozen=True, slots=True)
class InkCharacter:
    """One character of real text set in the drawing's ink, with the box pdfplumber gives it."""

    text: str
    box: Box


@dataclass(frozen=True, slots=True)
class GlyphCluster:
    """A run of small strokes close together: text drawn as paths. Located, never read."""

    box: Box
    strokes: int


@dataclass(frozen=True, slots=True)
class PageInk:
    """Everything the builder looks at, already filtered to the drawing's black and grey ink.

    The adapter in `extraction/rows.py` fills this from the flattened stamp layer. Coloured ink —
    the reviewer's red, yellow and blue — is left out before it gets here, so a GV mark can never be
    a tick, a row or a label. `drawing_boxes` are the pasted drawings' rectangles, used only to flag
    a label that meets an edge.
    """

    width: Decimal
    height: Decimal
    lines: tuple[InkLine, ...]
    curves: tuple[InkCurve, ...]
    characters: tuple[InkCharacter, ...]
    drawing_boxes: tuple[Box, ...]
    architect_boxes: tuple[StoredBox, ...] = ()


# ---------------------------------------------------------------------------
# Settings: every threshold the builder uses, with no defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowSettings:
    """Every threshold the builder uses, in page points unless named otherwise.

    No field has a default. These are properties of a vendor's CAD output, not of drawings in
    general, and `MEASURED_SETTINGS` names the one set that has been measured. A caller that wants
    different ones writes them down.
    """

    straightness_pt: Decimal
    """How far off-axis a stroke may drift and still be horizontal or vertical."""
    stroke_minimum_pt: Decimal
    """A stroke shorter than this along its axis is a dot, not line-work."""
    thin_rect_pt: Decimal
    """A rectangle thinner than this is a stroke drawn as a filled bar."""
    same_row_pt: Decimal
    """Horizontal strokes within this of each other in y are one row."""
    touching_gap_pt: Decimal
    """Pieces of a row this close along x are one run."""
    inline_label_gap_pt: Decimal
    """A break in a row up to this wide may hold an in-line label and still be one run."""
    inline_label_reach_pt: Decimal
    """How far above or below the row an in-line label's centre may sit."""
    minimum_row_pt: Decimal
    """A run shorter than this is not a dimension row."""
    label_reach_pt: Decimal
    """How far above or below the row a slot's label is looked for."""
    label_slack_pt: Decimal
    """How far outside a slot's ticks a label's centre may sit."""
    glyph_maximum_pt: Decimal
    """A curve smaller than this in both axes may be a glyph stroke, never line-work."""
    glyph_gap_pt: Decimal
    """Glyph strokes this close are one run of text drawn as paths."""
    glyph_minimum_strokes: int
    """A glyph run has at least this many strokes; one stroke is a dot or a tick."""
    slash_minimum_pt: Decimal
    slash_maximum_pt: Decimal
    """A tick slash is a tiny mark: between these in both axes."""
    slash_maximum_points: int
    """A slash is a thin parallelogram with this many path points at most; a digit has dozens."""
    slash_reach_pt: Decimal
    """A slash's centre sits within this of the row's line."""
    slash_size_tolerance_pt: Decimal
    """A slash counts on a row only when within this of the row's modal slash size."""
    slash_merge_floor_pt: Decimal
    """Slashes are merged within at least this, whatever the tick merge works out to."""
    tick_merge_fraction: Decimal
    """Two ticks are one when closer than this fraction of the row's width..."""
    tick_merge_floor_pt: Decimal
    """...floored at a stroke's own thickness..."""
    tick_merge_cap_pt: Decimal
    """...and capped here."""
    witness_margin_pt: Decimal
    """A witness line must extend at least this past the row on both sides."""
    witness_maximum_pt: Decimal
    """A vertical longer than this is a cabinet edge or a wall, not a witness line."""
    dashed_collinear_pt: Decimal
    dashed_piece_maximum_pt: Decimal
    dashed_minimum_pieces: int
    dashed_span_pt: Decimal
    """A dashed line: at least `dashed_minimum_pieces` collinear verticals (within
    `dashed_collinear_pt`), each shorter than `dashed_piece_maximum_pt`, spanning more than
    `dashed_span_pt` together. Never a witness line."""
    gap_tick_maximum_pt: Decimal
    """In gap mode, a break in the row up to this wide is a tick slash drawn over the line."""
    overall_end_pt: Decimal
    """An overall's end ticks coincide with the chain's ends within this."""
    overall_reach_pt: Decimal
    """An overall sits within this of its chain vertically; a coincident line further away is
    the run's outline, not its dimension."""
    overall_excess_fraction: Decimal
    """A non-tiling overall exceeds the chain by at most this fraction of the chain's width a side."""
    candidate_minimum_slots: int
    candidate_maximum_slots: int
    candidate_slot_minimum_pt: Decimal
    candidate_labelled_fraction: Decimal
    """A row is a candidate when it has this many slots, none thinner than the minimum, and at
    least this fraction of them labelled."""
    baseline_pt: Decimal
    """Characters whose bottoms are within this are on one baseline."""
    word_gap_pt: Decimal
    """Characters on one baseline further apart than this are separate words."""
    edge_margin_pt: Decimal
    """A label within this of a drawing's or the page's edge touches it."""

    def __post_init__(self) -> None:
        for field in fields(self):
            name = field.name
            value = getattr(self, name)
            if name.endswith(("_pt", "_fraction")):
                if not isinstance(value, Decimal):
                    raise TypeError(f"{name} must be a Decimal")
            elif isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int")


#: E1's thresholds (2026-10-07), measured on both client sets: 13 of 13 keyed pages found with the
#: right number of pieces and slot proportions within 0.2 % of the row. Two vendors' drawing styles
#: only — a third vendor is measured before these are assumed to hold for it.
MEASURED_SETTINGS = RowSettings(
    straightness_pt=Decimal("0.3"),
    stroke_minimum_pt=Decimal("1.0"),
    thin_rect_pt=Decimal("0.6"),
    same_row_pt=Decimal("0.6"),
    touching_gap_pt=Decimal("1.5"),
    inline_label_gap_pt=Decimal(45),
    inline_label_reach_pt=Decimal(7),
    minimum_row_pt=Decimal(15),
    label_reach_pt=Decimal(16),
    label_slack_pt=Decimal("1.0"),
    glyph_maximum_pt=Decimal(9),
    glyph_gap_pt=Decimal(3),
    glyph_minimum_strokes=2,
    slash_minimum_pt=Decimal("0.6"),
    slash_maximum_pt=Decimal(4),
    slash_maximum_points=8,
    slash_reach_pt=Decimal("1.6"),
    slash_size_tolerance_pt=Decimal("0.25"),
    slash_merge_floor_pt=Decimal("1.0"),
    tick_merge_fraction=Decimal("0.005"),
    tick_merge_floor_pt=Decimal("0.35"),
    tick_merge_cap_pt=Decimal("2.0"),
    witness_margin_pt=Decimal("0.8"),
    witness_maximum_pt=Decimal(40),
    dashed_collinear_pt=Decimal("0.5"),
    dashed_piece_maximum_pt=Decimal(15),
    dashed_minimum_pieces=3,
    dashed_span_pt=Decimal(60),
    gap_tick_maximum_pt=Decimal("3.5"),
    overall_end_pt=Decimal("2.5"),
    overall_reach_pt=Decimal(40),
    overall_excess_fraction=Decimal("0.15"),
    candidate_minimum_slots=2,
    candidate_maximum_slots=12,
    candidate_slot_minimum_pt=Decimal("1.0"),
    candidate_labelled_fraction=Decimal("0.6"),
    baseline_pt=Decimal("0.5"),
    word_gap_pt=Decimal("1.5"),
    edge_margin_pt=Decimal("0.5"),
)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class TickSource(StrEnum):
    """How a row's ticks were found."""

    SLASH = "slash"
    """The drawing's own tick slashes sitting on the line — the client's style."""
    WITNESS = "witness"
    """Perpendicular witness lines crossing the row."""
    GAP = "gap"
    """The breaks between the row's own pieces. Table rules and hatching look like this too, so a
    gap-mode row is returned but never ranked as a candidate."""


class LabelKind(StrEnum):
    TEXT = "text"
    """Real text in the file: `lines` holds it, copied as printed."""
    GLYPHS = "glyphs"
    """Text drawn as paths: only a box. Phase 4 crops it; nothing here reads it."""


class Tiling(StrEnum):
    COINCIDENT = "coincident"
    """The overall's end ticks sit on the chain's ends: the pieces tile the overall."""
    PIECES_DO_NOT_TILE = "pieces_do_not_tile"
    """The overall is wider than the chain of pieces. Reported, never dropped."""


@dataclass(frozen=True, slots=True)
class SlotLabel:
    """What sits in a slot: a run of real text, or a run of glyph paths."""

    kind: LabelKind
    box: Box
    stored: StoredBox
    lines: tuple[str, ...]
    """The text as printed, top to bottom: one line, or the overlapping lines of a stacked
    fraction; empty for glyphs."""
    strokes: int
    """How many path strokes a glyph run has; 0 for text."""
    touches_edge: bool
    """The box meets or crosses its drawing's edge or the page's: unreadable, for a person."""
    stacked: bool
    """Two of its text lines overlap each other in both x and y: a stacked fraction, whose
    numerator and denominator the file sets as separate runs at different heights. Their reading
    order is geometry, not text, so `text` is `None` and a person or a crop read decides (#762)."""
    distance_pt: Decimal
    """From the label's centre to the row's line."""

    @property
    def text(self) -> str | None:
        """The printed text, lines joined with one space; `None` for a glyph run or a stacked
        fraction."""
        if self.kind is not LabelKind.TEXT or self.stacked:
            return None
        return " ".join(self.lines)


@dataclass(frozen=True, slots=True)
class Slot:
    """The space between two consecutive ticks, and what was found labelling it."""

    index: int
    x0: Decimal
    x1: Decimal
    box: Box
    """The band the label was looked for in: the slot's width by the label reach above and below."""
    stored: StoredBox
    labels: tuple[SlotLabel, ...]
    """Nearest the slot's centre first. Empty is a slot nothing labels — reported, never filled."""

    @property
    def width_pt(self) -> Decimal:
        return self.x1 - self.x0

    @property
    def label(self) -> SlotLabel | None:
        return self.labels[0] if self.labels else None


@dataclass(frozen=True, slots=True)
class OverallRow:
    """The two-tick row that states the chain's whole width, and how it relates to the chain."""

    y: Decimal
    x0: Decimal
    x1: Decimal
    tick_source: TickSource
    tiling: Tiling
    left_excess_pt: Decimal
    right_excess_pt: Decimal
    """How far the overall reaches past the chain on each side; zero when coincident (within the
    end tolerance)."""
    box: Box
    stored: StoredBox
    labels: tuple[SlotLabel, ...]


@dataclass(frozen=True, slots=True)
class CountertopRowCandidate:
    """One dimension row: its ticks, slots, overall, labels, and where it stands among the page's.

    "Candidate" because this code does not know which row is the countertop. A model or the person
    says; this says what is there.
    """

    y: Decimal
    ticks: tuple[Decimal, ...]
    tick_source: TickSource
    slots: tuple[Slot, ...]
    overall: OverallRow | None
    findings: tuple[str, ...]
    labelled: int
    """How many slots have at least one label."""
    rank: int | None
    """1 for the best-placed candidate on the page; `None` when set aside."""
    rejected_because: str | None
    """Why the row is not a candidate, in plain words; `None` for a candidate."""

    @property
    def x0(self) -> Decimal:
        return self.ticks[0]

    @property
    def x1(self) -> Decimal:
        return self.ticks[-1]

    @property
    def width_pt(self) -> Decimal:
        return self.x1 - self.x0


@dataclass(frozen=True, slots=True)
class PageRows:
    """Every row found on a page: the ranked candidates, and the rest with their reasons."""

    candidates: tuple[CountertopRowCandidate, ...]
    """Ranked, rank 1 first. Advisory: a caller must not take the first silently."""
    rejected: tuple[CountertopRowCandidate, ...]
    """Rows that are not candidates — two-tick spans, gap-mode rows, unlabelled rows — kept so a
    page with thirty rows and three candidates can be told from a page with three rows."""

    @property
    def rows_found(self) -> int:
        return len(self.candidates) + len(self.rejected)


# ---------------------------------------------------------------------------
# Working records (not exported)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Horizontal:
    x0: Decimal
    x1: Decimal
    y: Decimal


@dataclass(frozen=True, slots=True)
class _Vertical:
    x: Decimal
    top: Decimal
    bottom: Decimal


@dataclass(frozen=True, slots=True)
class _Slash:
    x: Decimal
    y: Decimal
    width: Decimal
    height: Decimal


@dataclass(frozen=True, slots=True)
class _TextLine:
    text: str
    box: Box


@dataclass(frozen=True, slots=True)
class _Run:
    """A row of line-work: one y, an x-range, and the x of every piece end inside it."""

    y: Decimal
    x0: Decimal
    x1: Decimal
    ends: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class _Row:
    run: _Run
    source: TickSource
    ticks: tuple[Decimal, ...]


# ---------------------------------------------------------------------------
# Strokes
# ---------------------------------------------------------------------------


def _quantised(value: Decimal, step: Decimal) -> Decimal:
    return value.quantize(step, rounding=ROUND_HALF_UP)


def _add_stroke(
    x0: Decimal,
    y0: Decimal,
    x1: Decimal,
    y1: Decimal,
    *,
    settings: RowSettings,
    horizontals: list[_Horizontal],
    verticals: list[_Vertical],
) -> None:
    if abs(y0 - y1) <= settings.straightness_pt and abs(x0 - x1) >= settings.stroke_minimum_pt:
        horizontals.append(_Horizontal(min(x0, x1), max(x0, x1), (y0 + y1) / _TWO))
    elif abs(x0 - x1) <= settings.straightness_pt and abs(y0 - y1) >= settings.stroke_minimum_pt:
        verticals.append(_Vertical((x0 + x1) / _TWO, min(y0, y1), max(y0, y1)))


def _is_small(curve: InkCurve, settings: RowSettings) -> bool:
    return (
        curve.box.width < settings.glyph_maximum_pt and curve.box.height < settings.glyph_maximum_pt
    )


def _strokes(
    ink: PageInk, settings: RowSettings
) -> tuple[tuple[_Horizontal, ...], tuple[_Vertical, ...]]:
    """The page's horizontal and vertical line-work, from its lines and its larger curves.

    Small curves are left out on purpose: they are glyph strokes and tick slashes, and treating a
    tick's edge as a piece of the row would move the row's ends by a tick's width.
    """
    horizontals: list[_Horizontal] = []
    verticals: list[_Vertical] = []
    for line in ink.lines:
        _add_stroke(
            line.x0,
            line.y0,
            line.x1,
            line.y1,
            settings=settings,
            horizontals=horizontals,
            verticals=verticals,
        )
    for curve in ink.curves:
        box = curve.box
        if curve.closed and len(curve.points) == 4 and _is_rectangle(curve):
            if box.width <= settings.thin_rect_pt and box.height >= settings.stroke_minimum_pt:
                verticals.append(_Vertical(box.centre_x, box.top, box.bottom))
                continue
            if box.height <= settings.thin_rect_pt and box.width >= settings.stroke_minimum_pt:
                horizontals.append(_Horizontal(box.x0, box.x1, box.centre_y))
                continue
        if _is_small(curve, settings):
            continue
        points = curve.points + ((curve.points[0],) if curve.closed and curve.points else ())
        for (ax, ay), (bx, by) in pairwise(points):
            _add_stroke(
                ax, ay, bx, by, settings=settings, horizontals=horizontals, verticals=verticals
            )
    return tuple(horizontals), tuple(verticals)


def _is_rectangle(curve: InkCurve) -> bool:
    xs = {x for x, _ in curve.points}
    ys = {y for _, y in curve.points}
    return len(xs) == 2 and len(ys) == 2


def _slashes(ink: PageInk, settings: RowSettings) -> tuple[_Slash, ...]:
    """Tick marks drawn as tiny marks: two-point diagonals or tiny curves with few points.

    Never read as values; a slash is a position on a row and nothing else.
    """
    found: list[_Slash] = []
    low, high = settings.slash_minimum_pt, settings.slash_maximum_pt
    for line in ink.lines:
        dx, dy = abs(line.x0 - line.x1), abs(line.y0 - line.y1)
        if low <= dx <= high and low <= dy <= high:
            found.append(_Slash((line.x0 + line.x1) / _TWO, (line.y0 + line.y1) / _TWO, dx, dy))
    for curve in ink.curves:
        box = curve.box
        if (
            low <= box.width <= high
            and low <= box.height <= high
            and len(curve.points) <= settings.slash_maximum_points
        ):
            found.append(_Slash(box.centre_x, box.centre_y, box.width, box.height))
    found.sort(key=lambda slash: (slash.x, slash.y))
    return tuple(found)


def _glyph_clusters(ink: PageInk, settings: RowSettings) -> tuple[GlyphCluster, ...]:
    """Small curves grouped by proximity: where text drawn as paths sits. Not read."""
    small = [curve for curve in ink.curves if _is_small(curve, settings)]
    small.sort(key=lambda curve: (_quantised(curve.box.top / 4, _ONE), curve.box.x0))
    clusters: list[tuple[Box, int]] = []
    gap = settings.glyph_gap_pt
    for curve in small:
        box = curve.box
        for index, (cluster, count) in enumerate(clusters):
            if (
                box.x0 <= cluster.x1 + gap
                and box.x1 >= cluster.x0 - gap
                and box.top <= cluster.bottom + gap
                and box.bottom >= cluster.top - gap
            ):
                clusters[index] = (cluster.union(box), count + 1)
                break
        else:
            clusters.append((box, 1))
    return tuple(
        GlyphCluster(box=box, strokes=count)
        for box, count in clusters
        if count >= settings.glyph_minimum_strokes
    )


# ---------------------------------------------------------------------------
# Text: characters to lines, by baseline first
# ---------------------------------------------------------------------------


def _text_lines(characters: Sequence[InkCharacter], settings: RowSettings) -> tuple[_TextLine, ...]:
    """Characters joined into lines of text, exactly as printed.

    **Baseline first, then x.** On one client set the millimetre label and the `[inch]` label of a
    dimension are set 1.8 pt apart vertically and interleave along x, so a word built by x alone
    comes out as `1[04106]`. Grouping by bottom edge before sorting by x gives `1016` and `[40]`
    as two lines, each copied as the file has it.
    """
    ordered = sorted(characters, key=lambda char: (char.box.bottom, char.box.x0))
    baselines: list[list[InkCharacter]] = []
    for char in ordered:
        if (
            baselines
            and abs(baselines[-1][-1].box.bottom - char.box.bottom) <= settings.baseline_pt
        ):
            baselines[-1].append(char)
        else:
            baselines.append([char])
    lines: list[_TextLine] = []
    for group in baselines:
        group.sort(key=lambda char: char.box.x0)
        words: list[list[InkCharacter]] = []
        for char in group:
            if words and char.box.x0 - words[-1][-1].box.x1 <= settings.word_gap_pt:
                words[-1].append(char)
            else:
                words.append([char])
        for word in words:
            text = "".join(char.text for char in word).strip()
            if not text:
                continue
            box = word[0].box
            for char in word[1:]:
                box = box.union(char.box)
            lines.append(_TextLine(text=text, box=box))
    return tuple(lines)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _label_sits_in(
    x0: Decimal,
    x1: Decimal,
    y: Decimal,
    lines: Sequence[_TextLine],
    clusters: Sequence[GlyphCluster],
    settings: RowSettings,
) -> bool:
    """Whether a label's centre sits inside the break `x0..x1` of a row at `y`."""
    reach = settings.inline_label_reach_pt
    for line in lines:
        if x0 <= line.box.centre_x <= x1 and abs(line.box.centre_y - y) <= reach:
            return True
    for cluster in clusters:
        if x0 <= cluster.box.centre_x <= x1 and abs(cluster.box.centre_y - y) <= reach:
            return True
    return False


def _runs(
    horizontals: Sequence[_Horizontal],
    lines: Sequence[_TextLine],
    clusters: Sequence[GlyphCluster],
    settings: RowSettings,
) -> tuple[_Run, ...]:
    """Horizontal strokes grouped into rows by y, then into runs along x.

    Two pieces are one run when they touch, or when the break between them is narrow enough to hold
    an in-line label and one does sit there — the client's rows are drawn broken around the text.
    """
    rows: list[tuple[Decimal, list[_Horizontal]]] = []
    for stroke in sorted(horizontals, key=lambda stroke: (stroke.y, stroke.x0)):
        if rows and abs(rows[-1][0] - stroke.y) <= settings.same_row_pt:
            rows[-1][1].append(stroke)
        else:
            rows.append((stroke.y, [stroke]))
    runs: list[_Run] = []
    for y, pieces in rows:
        pieces.sort(key=lambda stroke: (stroke.x0, stroke.x1))
        open_runs: list[tuple[Decimal, Decimal, set[Decimal]]] = []
        for piece in pieces:
            if open_runs:
                start, end, ends = open_runs[-1]
                touching = piece.x0 <= end + settings.touching_gap_pt
                labelled_break = piece.x0 <= end + settings.inline_label_gap_pt and _label_sits_in(
                    end, piece.x0, y, lines, clusters, settings
                )
                if touching or labelled_break:
                    ends.update((_quantised(piece.x0, _TENTH), _quantised(piece.x1, _TENTH)))
                    open_runs[-1] = (start, max(end, piece.x1), ends)
                    continue
            open_runs.append(
                (piece.x0, piece.x1, {_quantised(piece.x0, _TENTH), _quantised(piece.x1, _TENTH)})
            )
        for start, end, ends in open_runs:
            if end - start >= settings.minimum_row_pt:
                runs.append(_Run(y=y, x0=start, x1=end, ends=tuple(sorted(ends))))
    return tuple(runs)


def _tick_merge(run: _Run, settings: RowSettings) -> Decimal:
    """Scale-aware: how close two ticks must be to count as one.

    A fraction of the row's width, floored at a stroke's thickness and capped. A 3/4" piece in a
    101" row is 0.74 % of the row, so at 0.5 % it survives at any drawing scale — the fixed 2 pt
    that preceded this swallowed it on the smaller sheets.
    """
    scaled = settings.tick_merge_fraction * (run.x1 - run.x0)
    return max(settings.tick_merge_floor_pt, min(settings.tick_merge_cap_pt, scaled))


def _merged(xs: Iterable[Decimal], within: Decimal) -> tuple[Decimal, ...]:
    merged: list[Decimal] = []
    for x in sorted(xs):
        if merged and abs(merged[-1] - x) <= within:
            continue
        merged.append(x)
    return tuple(merged)


def _is_dashed(x: Decimal, verticals: Sequence[_Vertical], settings: RowSettings) -> bool:
    pieces = sorted(
        (stroke.top, stroke.bottom)
        for stroke in verticals
        if abs(stroke.x - x) <= settings.dashed_collinear_pt
    )
    if len(pieces) < settings.dashed_minimum_pieces:
        return False
    span = pieces[-1][1] - pieces[0][0]
    short = all(bottom - top < settings.dashed_piece_maximum_pt for top, bottom in pieces)
    return short and span > settings.dashed_span_pt


def _ticks(
    run: _Run,
    verticals: Sequence[_Vertical],
    slashes: Sequence[_Slash],
    settings: RowSettings,
) -> tuple[TickSource, tuple[Decimal, ...]]:
    """Where the ticks are along a run, and how they were found.

    Slashes sitting on the line first. The ticks of one row are drawn with one stamp, so only the
    row's modal slash size counts — the stroke fragments of a label whose text touches the line
    would otherwise read as extra ticks. Then witness lines crossing the row, with long strokes and
    dashed centre lines excluded. Then the breaks between the row's own pieces.
    """
    y = run.y
    merge = _tick_merge(run, settings)
    gap = settings.touching_gap_pt
    on_row = [
        slash
        for slash in slashes
        if abs(slash.y - y) <= settings.slash_reach_pt and run.x0 - gap <= slash.x <= run.x1 + gap
    ]
    if on_row:
        sizes = Counter(
            (_quantised(slash.width, _TENTH), _quantised(slash.height, _TENTH)) for slash in on_row
        )
        mode_width, mode_height = sizes.most_common(1)[0][0]
        tolerance = settings.slash_size_tolerance_pt
        on_row = [
            slash
            for slash in on_row
            if abs(slash.width - mode_width) <= tolerance
            and abs(slash.height - mode_height) <= tolerance
        ]
    slash_xs = _merged((slash.x for slash in on_row), max(merge, settings.slash_merge_floor_pt))
    if len(slash_xs) >= 2:
        return TickSource.SLASH, slash_xs

    margin = settings.witness_margin_pt
    crossing = [
        stroke.x
        for stroke in verticals
        if run.x0 - gap <= stroke.x <= run.x1 + gap
        and stroke.top < y - margin
        and stroke.bottom > y + margin
        and stroke.bottom - stroke.top <= settings.witness_maximum_pt
        and not _is_dashed(stroke.x, verticals, settings)
    ]
    witness_xs = _merged(crossing, merge)
    if len(witness_xs) >= 2:
        return TickSource.WITNESS, witness_xs

    ends = run.ends
    ticks: list[Decimal] = [ends[0]]
    index = 1
    while index < len(ends) - 1:
        first, second = ends[index], ends[index + 1]
        if second - first <= settings.gap_tick_maximum_pt:
            ticks.append((first + second) / _TWO)
            index += 2
        else:
            ticks.append(first)
            index += 1
    ticks.append(ends[-1])
    kept = [
        x for position, x in enumerate(ticks) if position == 0 or x - ticks[position - 1] > merge
    ]
    return TickSource.GAP, tuple(kept)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _touches_edge(box: Box, ink: PageInk, settings: RowSettings) -> bool:
    """Whether a label's box comes within the margin of, or crosses, an edge it should be inside.

    The pasted drawings' rectangles when they are known, and always the page's own edges. A label
    the drawing's edge cuts through is one a person must read: the file holds only part of it.
    """
    margin = settings.edge_margin_pt
    frames = [frame for frame in ink.drawing_boxes if frame.meets(box)]
    frames.append(Box(_ZERO, _ZERO, ink.width, ink.height))
    for frame in frames:
        if (
            box.x0 <= frame.x0 + margin
            or box.x1 >= frame.x1 - margin
            or box.top <= frame.top + margin
            or box.bottom >= frame.bottom - margin
        ):
            return True
    return False


def _stored_box(box: Box, place: Placement) -> StoredBox:
    first = place(box.x0, box.top)
    second = place(box.x1, box.bottom)
    return StoredBox(
        left=min(first.x, second.x),
        top=min(first.y, second.y),
        right=max(first.x, second.x),
        bottom=max(first.y, second.y),
    )


def _labels(
    x0: Decimal,
    x1: Decimal,
    y: Decimal,
    *,
    lines: Sequence[_TextLine],
    clusters: Sequence[GlyphCluster],
    row_ys: Sequence[Decimal],
    ink: PageInk,
    settings: RowSettings,
    place: Placement,
) -> tuple[SlotLabel, ...]:
    """Every label whose centre sits over `x0..x1` within reach of the row at `y`.

    **Nearest the slot's centre first, then nearest the line.** A dimension's text is centred on
    its line by drawing convention, and on a wide span the strip within reach also holds whatever
    else sits there — a cabinet tag, a corner of hatching — which may be closer to the line but is
    never centred on it. Everything found is returned, so a caller sees the alternatives.

    **One text line, one label** — the millimetre line and the `[inch]` line under it come back as
    two labels, not one. Pairing them is notation, which `units/` owns: on one client page the pair
    sits 1.2 pt apart while the next dimension's line is 0.7 pt away, so no distance rule here could
    tell them apart without reading the brackets, and this module reads nothing. The exception is
    lines that overlap each other in both axes — a stacked fraction's numerator over its whole
    number — which are one label, flagged `stacked`, with no text (#762). A glyph run drawn twice,
    its fill and its outline, is one label, not two.
    """
    low, high = x0 - settings.label_slack_pt, x1 + settings.label_slack_pt
    reach = settings.label_reach_pt
    hits = [
        line
        for line in lines
        if low <= line.box.centre_x <= high and abs(line.box.centre_y - y) <= reach
    ]
    hits.sort(key=lambda line: (abs(line.box.centre_y - y), line.box.x0, line.box.top))
    groups: list[list[_TextLine]] = []
    for line in hits:
        for group in groups:
            if any(_overlap(line.box, member.box) for member in group):
                group.append(line)
                break
        else:
            groups.append([line])
    labels: list[SlotLabel] = []
    for group in groups:
        group.sort(key=lambda line: (line.box.top, line.box.x0))
        box = group[0].box
        for line in group[1:]:
            box = box.union(line.box)
        labels.append(
            SlotLabel(
                kind=LabelKind.TEXT,
                box=box,
                stored=_stored_box(box, place),
                lines=tuple(line.text for line in group),
                strokes=0,
                touches_edge=_touches_edge(box, ink, settings),
                stacked=len(group) > 1,
                distance_pt=abs(box.centre_y - y),
            )
        )
    for cluster in clusters:
        box = cluster.box
        if not (low <= box.centre_x <= high and abs(box.centre_y - y) <= reach):
            continue
        if _is_tick(box, row_ys, settings):
            continue
        labels.append(
            SlotLabel(
                kind=LabelKind.GLYPHS,
                box=box,
                stored=_stored_box(box, place),
                lines=(),
                strokes=cluster.strokes,
                touches_edge=_touches_edge(box, ink, settings),
                stacked=False,
                distance_pt=abs(box.centre_y - y),
            )
        )
    centre = (x0 + x1) / _TWO
    labels.sort(
        key=lambda label: (abs(label.box.centre_x - centre), label.distance_pt, label.box.top)
    )
    unique: list[SlotLabel] = []
    for label in labels:
        if not any(label.kind is seen.kind and label.box == seen.box for seen in unique):
            unique.append(label)
    return tuple(unique)


def _is_tick(box: Box, row_ys: Sequence[Decimal], settings: RowSettings) -> bool:
    """A slash-sized cluster sitting on any row's line is a tick, not a label.

    A tick slash is often two paths — the fill and its outline — and the overall's tick sits on
    the chain's, so the clusterer sees two to four tiny strokes exactly on a line. Without this
    every slot would be "labelled" by its own tick at distance zero, and by the overall's ticks a
    few points away.
    """
    if box.width > settings.slash_maximum_pt or box.height > settings.slash_maximum_pt:
        return False
    return any(abs(box.centre_y - y) <= settings.slash_reach_pt for y in row_ys)


def _overlap(first: Box, second: Box) -> bool:
    """Whether two boxes overlap in both axes — the parts of a stacked fraction do."""
    return (
        first.x0 < second.x1
        and first.x1 > second.x0
        and first.top < second.bottom
        and first.bottom > second.top
    )


# ---------------------------------------------------------------------------
# The overall
# ---------------------------------------------------------------------------


def _has_own_label(
    other: _Row,
    *,
    chain_labels: frozenset[Box],
    lines: Sequence[_TextLine],
    clusters: Sequence[GlyphCluster],
    row_ys: Sequence[Decimal],
    ink: PageInk,
    settings: RowSettings,
    place: Placement,
) -> bool:
    """A label over `other`'s span that is not one of the chain's own slot labels.

    A line close to the chain sees the chain's labels within reach too; those say nothing about
    whether `other` is a printed dimension.
    """
    return any(
        label.box not in chain_labels
        for label in _labels(
            other.ticks[0],
            other.ticks[-1],
            other.run.y,
            lines=lines,
            clusters=clusters,
            row_ys=row_ys,
            ink=ink,
            settings=settings,
            place=place,
        )
    )


def _overall_for(
    row: _Row,
    rows: Sequence[_Row],
    settings: RowSettings,
    *,
    labelled: Callable[[_Row], bool] = lambda _other: False,
) -> tuple[_Row, Tiling, Decimal, Decimal] | None:
    """The two-tick row that states this chain's whole width, if the drawing has one.

    Coincident ends first — the nearest row above or below whose two ticks sit on the chain's ends.
    Failing that, a two-tick row that is wider than the chain and encloses it, by at most the
    allowed fraction a side: the pieces do not tile it, and that is reported, not hidden.

    **Within either kind, a line carrying its own label beats a nearer one without** (`labelled`).
    A countertop's outline is ticked at the same two ends as its printed overall and can sit
    closer to the chain; only the printed line has a number on it. A client page (proof run
    2026-10-08) had the outline 15 px nearer, and the overall's label was never read. With no
    labelled candidate, the nearest still wins, as before.

    Within reach either way. A drawing puts the overall a few points from its chain; a two-tick
    line far away whose ends happen to coincide is the cabinet run's own outline or a wall-to-wall
    line, which E1 saw on two pages and named as the case needing a distance cap.
    """
    x0, x1, y = row.ticks[0], row.ticks[-1], row.run.y
    end = settings.overall_end_pt
    others = sorted(
        (
            other
            for other in rows
            if other is not row
            and len(other.ticks) == 2
            and settings.same_row_pt < abs(other.run.y - y) <= settings.overall_reach_pt
        ),
        key=lambda other: (not labelled(other), abs(other.run.y - y), other.run.y),
    )
    for other in others:
        if abs(other.ticks[0] - x0) <= end and abs(other.ticks[-1] - x1) <= end:
            return other, Tiling.COINCIDENT, _ZERO, _ZERO
    width = x1 - x0
    allowed = settings.overall_excess_fraction * width
    for other in others:
        left, right = other.ticks[0], other.ticks[-1]
        if (
            left <= x0 + end
            and right >= x1 - end
            and (right - left) > width + end
            and x0 - left <= allowed
            and right - x1 <= allowed
        ):
            return other, Tiling.PIECES_DO_NOT_TILE, max(_ZERO, x0 - left), max(_ZERO, right - x1)
    return None


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def build_rows(ink: PageInk, settings: RowSettings, *, place: Placement) -> PageRows:
    """Every dimension row on a page, with its ticks, slots, overall and labels.

    Args:
        ink: the page's black and grey ink — see `PageInk`.
        settings: every threshold, written down — see `RowSettings`.
        place: where a pdfplumber `(x, top)` lands in stored coordinates.

    Returns:
        The rows passing the candidate filter, ranked, and every other row with its reason. Nothing
        found is dropped; nothing is picked.
    """
    horizontals, verticals = _strokes(ink, settings)
    slashes = _slashes(ink, settings)
    clusters = _glyph_clusters(ink, settings)
    lines = _text_lines(ink.characters, settings)
    rows: list[_Row] = []
    for run in _runs(horizontals, lines, clusters, settings):
        source, ticks = _ticks(run, verticals, slashes, settings)
        if len(ticks) >= 2:
            rows.append(_Row(run=run, source=source, ticks=ticks))
    rows.sort(key=lambda row: (row.run.y, row.run.x0))
    row_ys = tuple(row.run.y for row in rows)

    reach = settings.label_reach_pt
    built: list[tuple[_Row, CountertopRowCandidate]] = []
    for row in rows:
        y = row.run.y
        slots: list[Slot] = []
        for index, (x0, x1) in enumerate(pairwise(row.ticks)):
            box = _band(x0, x1, y, ink, reach)
            slots.append(
                Slot(
                    index=index,
                    x0=x0,
                    x1=x1,
                    box=box,
                    stored=_stored_box(box, place),
                    labels=_labels(
                        x0,
                        x1,
                        y,
                        lines=lines,
                        clusters=clusters,
                        row_ys=row_ys,
                        ink=ink,
                        settings=settings,
                        place=place,
                    ),
                )
            )
        findings: list[str] = []
        overall: OverallRow | None = None
        found = _overall_for(
            row,
            rows,
            settings,
            labelled=partial(
                _has_own_label,
                chain_labels=frozenset(label.box for slot in slots for label in slot.labels),
                lines=lines,
                clusters=clusters,
                row_ys=row_ys,
                ink=ink,
                settings=settings,
                place=place,
            ),
        )
        if found is not None:
            other, tiling, left_excess, right_excess = found
            ox0, ox1, oy = other.ticks[0], other.ticks[-1], other.run.y
            obox = _band(ox0, ox1, oy, ink, reach)
            overall = OverallRow(
                y=oy,
                x0=ox0,
                x1=ox1,
                tick_source=other.source,
                tiling=tiling,
                left_excess_pt=left_excess,
                right_excess_pt=right_excess,
                box=obox,
                stored=_stored_box(obox, place),
                labels=_labels(
                    ox0,
                    ox1,
                    oy,
                    lines=lines,
                    clusters=clusters,
                    row_ys=row_ys,
                    ink=ink,
                    settings=settings,
                    place=place,
                ),
            )
            if tiling is Tiling.PIECES_DO_NOT_TILE:
                findings.append(PIECES_DO_NOT_TILE)
        labelled = sum(1 for slot in slots if slot.labels)
        first_labels = [slot.label for slot in slots if slot.label is not None]
        if overall is not None and overall.labels:
            first_labels.append(overall.labels[0])
        if any(label.touches_edge for label in first_labels):
            findings.append(LABEL_TOUCHES_EDGE)
        if any(label.stacked for label in first_labels):
            findings.append(LABEL_IS_STACKED)
        rejected = _rejection(
            row,
            slots,
            labelled,
            settings,
            overall=overall,
            architect_boxes=ink.architect_boxes,
            row_start=place(row.ticks[0], y),
            row_end=place(row.ticks[-1], y),
        )
        built.append(
            (
                row,
                CountertopRowCandidate(
                    y=y,
                    ticks=row.ticks,
                    tick_source=row.source,
                    slots=tuple(slots),
                    overall=overall,
                    findings=tuple(findings),
                    labelled=labelled,
                    rank=None,
                    rejected_because=rejected,
                ),
            )
        )

    def order(item: tuple[_Row, CountertopRowCandidate]) -> tuple[int, int, Decimal, Decimal]:
        candidate = item[1]
        if candidate.overall is None:
            standing = 2
        elif candidate.overall.tiling is Tiling.COINCIDENT:
            standing = 0
        else:
            standing = 1
        return standing, -candidate.labelled, -candidate.width_pt, candidate.y

    candidates: list[CountertopRowCandidate] = []
    rejected_rows: list[CountertopRowCandidate] = []
    for _, candidate in sorted(built, key=order):
        if candidate.rejected_because is None:
            candidates.append(
                CountertopRowCandidate(
                    y=candidate.y,
                    ticks=candidate.ticks,
                    tick_source=candidate.tick_source,
                    slots=candidate.slots,
                    overall=candidate.overall,
                    findings=candidate.findings,
                    labelled=candidate.labelled,
                    rank=len(candidates) + 1,
                    rejected_because=None,
                )
            )
        else:
            rejected_rows.append(candidate)
    return PageRows(candidates=tuple(candidates), rejected=tuple(rejected_rows))


def _band(x0: Decimal, x1: Decimal, y: Decimal, ink: PageInk, reach: Decimal) -> Box:
    """The strip a label is looked for in, kept inside the page so it can be placed."""
    return Box(
        max(_ZERO, x0),
        max(_ZERO, y - reach),
        min(ink.width, x1),
        min(ink.height, y + reach),
    )


def _rejection(
    row: _Row,
    slots: Sequence[Slot],
    labelled: int,
    settings: RowSettings,
    *,
    overall: OverallRow | None,
    architect_boxes: Sequence[StoredBox],
    row_start: StoredPoint,
    row_end: StoredPoint,
) -> str | None:
    """Why a row is not a candidate, or `None`. Plain words, so a screen can show them as they are."""
    count = len(slots)
    if count < settings.candidate_minimum_slots:
        return "one slot: a span, not a chain of pieces"
    if count > settings.candidate_maximum_slots:
        return f"{count} slots: more than a countertop row has"
    if row.source is TickSource.GAP:
        return "ticks found only from breaks in the line: a table rule or hatching, not a row"
    thinnest = min(slot.width_pt for slot in slots)
    if thinnest < settings.candidate_slot_minimum_pt:
        return "a slot thinner than a stroke"
    if Decimal(labelled) < settings.candidate_labelled_fraction * Decimal(count):
        return f"only {labelled} of {count} slots have a label"
    first = [slot.label for slot in slots if slot.label is not None]
    if first and all(
        label.kind is LabelKind.TEXT
        and not any(character.isdigit() for line in label.lines for character in line)
        for label in first
    ):
        # A row of `EQ | EQ` centres a tap or a handle; it states no width. A reader once chose
        # one lying on the stone over the real piece row (proof run 2026-10-08). Labels drawn
        # as paths have no text to check and are never dropped by this.
        return "no label has a number: a centring or note line, not a row of widths"
    text_labels_by_region = {
        (label.box, label.text): label.text
        for slot in slots
        for label in slot.labels
        if label.kind is LabelKind.TEXT and label.text is not None
    }
    if overall is not None:
        text_labels_by_region.update(
            {
                (label.box, label.text): label.text
                for label in overall.labels
                if label.kind is LabelKind.TEXT and label.text is not None
            }
        )
    text_labels = tuple(text_labels_by_region.values())
    if text_labels and sum(
        _FEET_AND_INCHES.fullmatch(text) is not None for text in text_labels
    ) * 2 > len(text_labels):
        return FEET_AND_INCHES_ROW
    if any(
        box.left <= row_start.x <= box.right
        and box.left <= row_end.x <= box.right
        and box.top <= row_start.y <= box.bottom
        and box.top <= row_end.y <= box.bottom
        for box in architect_boxes
    ):
        return INSIDE_ARCHITECT_DRAWING
    return None
