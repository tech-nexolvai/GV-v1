"""Each slot's whole label, and the crop a reader is shown of it (#987).

**Why this exists beside `extraction/geometry/rows.py`.** The row builder finds the row, its ticks
and its slots, and locates a label in each slot by its curves. That is enough to say *where* a label
is, not to show a reader *all* of it: a `1` or an `l` drawn as one straight stroke is a line, not a
curve, so a label like `17 5/8"` can be located without its first digit. A crop cut to that box
would show `7 5/8"`, and two readers would agree on it. So this module rebuilds each label as E2
did (Measurements 2026-10-07): from every small stroke and every black or grey character near the
row — curves, straight strokes and tiny rectangles alike — merged into runs, each run given to the
row line and the slot it belongs to, and fragments of one label on one baseline joined.

**The crop is code's, never a model's** (plan rule): the label run padded, down to its line, with
the slot's own ticks when the slot is narrow, and clipped short of every other run so that no
neighbouring label is in the picture. A reader that sees one label cannot copy another.

**What it never does.** It reads no value and decides nothing: text is copied as the file has it,
and a glyph run is only boxed. Every threshold is in `CropSettings`, which has no defaults;
`E2_CROP_SETTINGS` names the values E2 measured (27 agreed-right, 0 agreed-wrong on the vendor's
ink). Everything is `Decimal`; no float.

Source: issue #987 · Verification: `tests/extraction/slot_reader/test_runs.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Literal

from extraction.geometry.rows import (
    Box,
    CountertopRowCandidate,
    PageInk,
    PageRows,
    RowSettings,
    Tiling,
)

__all__ = [
    "E2_CROP_SETTINGS",
    "CropSettings",
    "Lane",
    "PlannedLabel",
    "PlannedOwner",
    "SlotPlan",
    "choose_row",
    "plan_slots",
]

_ZERO = Decimal(0)
_TWO = Decimal(2)


@dataclass(frozen=True, slots=True)
class CropSettings:
    """Every threshold the label runs and crops are built with, in page points unless named.

    No field has a default: they are E2's measurements on two vendors' drawings, written down in
    `E2_CROP_SETTINGS`, and a caller that wants others states them.
    """

    glyph_maximum_pt: Decimal
    """A stroke this large or larger in either axis is line-work, not part of a label."""
    diagonal_minimum_pt: Decimal
    """A straight stroke wider and taller than this is a diagonal — hatching or a leader."""
    tick_mark_maximum_pt: Decimal
    tick_reach_x_pt: Decimal
    tick_reach_y_pt: Decimal
    """A small stroke within these of one of the row's ticks is the tick slash, not a label."""
    dashed_collinear_pt: Decimal
    dashed_gap_pt: Decimal
    """A straight stroke continued on its own axis (within `dashed_collinear_pt`) after a gap of at
    most `dashed_gap_pt` is a dash of a dashed line."""
    beside_gap_pt: Decimal
    beside_overlap_fraction: Decimal
    """Two runs this close along x, overlapping this much in height, are one run."""
    tiny_height_pt: Decimal
    tiny_width_pt: Decimal
    tiny_gap_pt: Decimal
    """A mark no taller and wider than these (an inch mark, a dot) joins a run it is within
    `tiny_gap_pt` of vertically."""
    stacked_gap_pt: Decimal
    stacked_overlap_fraction: Decimal
    stacked_minimum_width_pt: Decimal
    """Two runs one above the other, this close and overlapping this much in width, are one run: a
    stacked fraction, or the millimetre line over its `[inch]` line."""
    run_maximum_height_pt: Decimal
    """A run never grows taller than this."""
    run_minimum_marks: int
    run_minimum_width_pt: Decimal
    """A run with fewer marks, or narrower, is a dot or a stray stroke."""
    overlap_floor_pt: Decimal
    """The smallest extent an overlap is measured against, so a zero-width stroke divides nothing by
    zero."""
    window_margin_pt: Decimal
    """How far left and right of the row's ends strokes are looked at."""
    above_line_pt: Decimal
    below_line_pt: Decimal
    """A run belongs to a row line when its bottom is at most `above_line_pt` above the line and its
    top at most `below_line_pt` below it."""
    row_reach_x_pt: Decimal
    """A run further than this past the row's ends belongs to no slot."""
    narrow_slot_pt: Decimal
    narrow_slack_pt: Decimal
    wide_slack_pt: Decimal
    """A label may hang this far past a slot's ticks: `narrow_slack_pt` for a slot narrower than
    `narrow_slot_pt`, else `wide_slack_pt`."""
    ambiguous_coverage_fraction: Decimal
    """A run covering a second slot at least this fraction as well as its best is ambiguous."""
    baseline_fraction: Decimal
    fragment_gap_pt: Decimal
    """Two runs of one slot whose centres are within this fraction of their height of each other,
    and at most `fragment_gap_pt` apart along x, are one label."""
    padding_pt: Decimal
    """How far the crop reaches past the label on every side."""
    ticks_in_crop_maximum_slot_pt: Decimal
    tick_margin_pt: Decimal
    """A slot no wider than `ticks_in_crop_maximum_slot_pt` is cropped with its ticks, plus
    `tick_margin_pt` either side."""
    clip_gap_x_pt: Decimal
    clip_gap_y_pt: Decimal
    """How far short of a neighbouring run the crop stops, along x and along y."""

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "run_minimum_marks":
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise TypeError("run_minimum_marks must be a positive int")
            elif not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise TypeError(f"{field.name} must be a finite Decimal, zero or more")

    @property
    def config_hash(self) -> str:
        """Every value, as text, for the identity of a run whose crops these settings change."""
        return ";".join(f"{field.name}={getattr(self, field.name)}" for field in fields(self))


#: E2's crop rules (2026-10-07, `research/E2/build_crops.py`), measured on both client sets: 38 keyed
#: slots, 27 agreed-right, 0 agreed-wrong on the vendor's ink. Two vendors only.
E2_CROP_SETTINGS = CropSettings(
    glyph_maximum_pt=Decimal(9),
    diagonal_minimum_pt=Decimal("0.5"),
    tick_mark_maximum_pt=Decimal("3.5"),
    tick_reach_x_pt=Decimal("2.5"),
    tick_reach_y_pt=Decimal(3),
    dashed_collinear_pt=Decimal("0.3"),
    dashed_gap_pt=Decimal(6),
    beside_gap_pt=Decimal("3.5"),
    beside_overlap_fraction=Decimal("0.5"),
    tiny_height_pt=Decimal(2),
    tiny_width_pt=Decimal("1.2"),
    tiny_gap_pt=Decimal(1),
    stacked_gap_pt=Decimal(2),
    stacked_overlap_fraction=Decimal("0.5"),
    stacked_minimum_width_pt=Decimal("1.5"),
    run_maximum_height_pt=Decimal(14),
    run_minimum_marks=2,
    run_minimum_width_pt=Decimal("2.5"),
    overlap_floor_pt=Decimal("0.1"),
    window_margin_pt=Decimal(25),
    above_line_pt=Decimal(8),
    below_line_pt=Decimal("1.5"),
    row_reach_x_pt=Decimal(8),
    narrow_slot_pt=Decimal(30),
    narrow_slack_pt=Decimal(6),
    wide_slack_pt=Decimal("1.5"),
    ambiguous_coverage_fraction=Decimal("0.9"),
    baseline_fraction=Decimal("0.4"),
    fragment_gap_pt=Decimal(12),
    padding_pt=Decimal(4),
    ticks_in_crop_maximum_slot_pt=Decimal(150),
    tick_margin_pt=Decimal("2.5"),
    clip_gap_x_pt=Decimal(1),
    clip_gap_y_pt=Decimal("0.2"),
)


class Lane(StrEnum):
    """Which two sources may seal a label's reading."""

    TEXT = "text"
    """Every mark is real text in the file: the file's text is one source, one reader the other."""
    GLYPHS = "glyphs"
    """Drawn as paths, or stacked text: two readers of different makers."""


_MarkKind = Literal["curve", "line", "rect", "char"]


@dataclass(frozen=True, slots=True)
class _Mark:
    box: Box
    kind: _MarkKind
    text: str | None


@dataclass(slots=True)
class _Run:
    box: Box
    marks: list[_Mark]

    @property
    def count(self) -> int:
        return len(self.marks)


@dataclass(frozen=True, slots=True)
class PlannedLabel:
    """One label run in a slot or on the overall, and the crop a reader is shown of it."""

    box: Box
    """The run's box in page points, pdfplumber's frame."""
    crop: Box
    """The crop in page points: the run padded, down to its line, clipped short of every
    neighbour, kept on the page."""
    lane: Lane
    text: str | None
    """The file's own text, lines top to bottom joined by one space; `None` for a glyph run and for
    stacked text, whose reading order is geometry."""
    text_stacked: bool
    """Two of its text lines overlap in both axes: a stacked fraction set as text."""
    has_digit: bool
    """Text with a digit, or any glyph run (its digits are unknown until read)."""
    touches_edge: bool
    ambiguous_slot: bool
    """A second slot is covered nearly as well: which piece it labels is not certain."""
    crowded: bool
    """Another run overlaps this one, so no crop can show this label alone."""
    ticks_in_crop: bool
    path_boxes: tuple[Box, ...]
    """Every small stroke at the label, for the fraction-bar detector."""


@dataclass(frozen=True, slots=True)
class PlannedOwner:
    """A slot of the chain (`index` from 0, left to right) or the overall (`index` is `None`)."""

    index: int | None
    x0: Decimal
    x1: Decimal
    line_y: Decimal
    band: Box
    """The slot's band from the row builder (or the overall's): what the screen draws."""
    labels: tuple[PlannedLabel, ...]
    """Left to right. Empty when nothing labels it."""


@dataclass(frozen=True, slots=True)
class SlotPlan:
    """The chosen row's slots and overall, each with its label runs, or why there is no row."""

    row: CountertopRowCandidate | None
    ambiguity: str | None
    """Why the row cannot be trusted as the countertop's, in plain words; `None` when it can."""
    slots: tuple[PlannedOwner, ...]
    overall: PlannedOwner | None


def _standing(candidate: CountertopRowCandidate) -> int:
    if candidate.overall is None:
        return 2
    return 0 if candidate.overall.tiling is Tiling.COINCIDENT else 1


def choose_row(rows: PageRows) -> tuple[CountertopRowCandidate | None, str | None]:
    """The rank-1 candidate and, where it cannot be trusted, why — never a silent pick.

    Ambiguous when it has no overall (nothing on the drawing says this chain is a run's whole
    width), or when the rank-2 candidate ties it: the same standing (overall coincident, overall
    not tiling, or none) and the same number of labelled slots. Width alone does not break a tie.
    """
    if not rows.candidates:
        return None, None
    first = rows.candidates[0]
    if first.overall is None:
        return first, "the row has no overall width above or below it"
    if len(rows.candidates) > 1:
        second = rows.candidates[1]
        if _standing(second) == _standing(first) and second.labelled == first.labelled:
            return first, "another row on the page fits as well"
    return first, None


def _marks(
    ink: PageInk, window: Box, settings: CropSettings
) -> tuple[list[_Mark], list[_Mark], list[_Mark]]:
    """The small marks inside `window`: (every small path, every character, every space).

    Spaces are kept apart: they are not ink, so they never join or count towards a run, but a
    label's text needs them — `29 3/4` set in a small font has a space narrower than the gap that
    separates words, and without it the text would read `293/4`.

    Paths are lines, curves and rectangles under the glyph size in both axes; diagonal straight
    strokes are left out (hatching and leaders; a glyph's straight strokes are upright or flat).
    """
    limit = settings.glyph_maximum_pt
    diagonal = settings.diagonal_minimum_pt
    paths: list[_Mark] = []
    for line in ink.lines:
        box = Box(
            min(line.x0, line.x1),
            min(line.y0, line.y1),
            max(line.x0, line.x1),
            max(line.y0, line.y1),
        )
        if box.width >= limit or box.height >= limit or not box.meets(window):
            continue
        if box.width > diagonal and box.height > diagonal:
            continue
        paths.append(_Mark(box, "line", None))
    for curve in ink.curves:
        box = curve.box
        if box.width >= limit or box.height >= limit or not box.meets(window):
            continue
        kind: _MarkKind = "rect" if curve.closed and len(curve.points) == 4 else "curve"
        paths.append(_Mark(box, kind, None))
    characters = [
        _Mark(char.box, "char", char.text)
        for char in ink.characters
        if char.text.strip() and char.box.meets(window)
    ]
    spaces = [
        _Mark(char.box, "char", char.text)
        for char in ink.characters
        if char.text and not char.text.strip() and char.box.meets(window)
    ]
    return paths, characters, spaces


def _is_dash(mark: _Mark, lines: Sequence[_Mark], settings: CropSettings) -> bool:
    if mark.kind != "line":
        return False
    near, gap = settings.dashed_collinear_pt, settings.dashed_gap_pt
    box = mark.box
    if box.width <= settings.diagonal_minimum_pt:
        for other in lines:
            o = other.box
            if other is mark or o.width > settings.diagonal_minimum_pt:
                continue
            if abs(o.x0 - box.x0) <= near and (
                _ZERO < o.top - box.bottom <= gap or _ZERO < box.top - o.bottom <= gap
            ):
                return True
        return False
    for other in lines:
        o = other.box
        if other is mark or o.height > settings.diagonal_minimum_pt:
            continue
        if abs(o.top - box.top) <= near and (
            _ZERO < o.x0 - box.x1 <= gap or _ZERO < box.x0 - o.x1 <= gap
        ):
            return True
    return False


def _x_gap(a: Box, b: Box) -> Decimal:
    return max(a.x0, b.x0) - min(a.x1, b.x1)


def _y_gap(a: Box, b: Box) -> Decimal:
    return max(a.top, b.top) - min(a.bottom, b.bottom)


def _y_overlap_fraction(a: Box, b: Box, floor: Decimal) -> Decimal:
    overlap = min(a.bottom, b.bottom) - max(a.top, b.top)
    return overlap / max(floor, min(a.height, b.height))


def _x_overlap_fraction(a: Box, b: Box, floor: Decimal) -> Decimal:
    overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
    return overlap / max(floor, min(a.width, b.width))


def _merged_runs(marks: Sequence[_Mark], settings: CropSettings) -> list[_Run]:
    """E2's run merge: beside along a baseline, or stacked; repeated until nothing joins."""
    floor = settings.overlap_floor_pt

    def tiny(box: Box) -> bool:
        return box.height <= settings.tiny_height_pt and box.width <= settings.tiny_width_pt

    def beside(a: Box, b: Box) -> bool:
        if _x_gap(a, b) > settings.beside_gap_pt:
            return False
        if tiny(a) or tiny(b):
            return _y_gap(a, b) <= settings.tiny_gap_pt
        return _y_overlap_fraction(a, b, floor) >= settings.beside_overlap_fraction

    def stacked(a: Box, b: Box) -> bool:
        if min(a.width, b.width) < settings.stacked_minimum_width_pt:
            return False
        return (
            _x_overlap_fraction(a, b, floor) >= settings.stacked_overlap_fraction
            and _y_gap(a, b) <= settings.stacked_gap_pt
        )

    runs = [_Run(mark.box, [mark]) for mark in marks]
    changed = True
    while changed:
        changed = False
        runs.sort(key=lambda run: (run.box.x0, run.box.top, run.box.x1, run.box.bottom))
        out: list[_Run] = []
        for run in runs:
            for existing in out:
                joined = existing.box.union(run.box)
                if (
                    beside(existing.box, run.box) or stacked(existing.box, run.box)
                ) and joined.height <= settings.run_maximum_height_pt:
                    existing.box = joined
                    existing.marks.extend(run.marks)
                    changed = True
                    break
            else:
                out.append(run)
        runs = out
    return [
        run
        for run in runs
        if run.count >= settings.run_minimum_marks
        and run.box.width >= settings.run_minimum_width_pt
    ]


def _text_lines(chars: Sequence[_Mark], rows: RowSettings) -> list[tuple[str, Box]]:
    """Characters as printed, by baseline first and then along x (as `rows.py` reads text)."""
    ordered = sorted(chars, key=lambda mark: (mark.box.bottom, mark.box.x0))
    baselines: list[list[_Mark]] = []
    for mark in ordered:
        if baselines and abs(baselines[-1][-1].box.bottom - mark.box.bottom) <= rows.baseline_pt:
            baselines[-1].append(mark)
        else:
            baselines.append([mark])
    lines: list[tuple[str, Box]] = []
    for group in baselines:
        group.sort(key=lambda mark: mark.box.x0)
        words: list[list[_Mark]] = []
        for mark in group:
            if words and mark.box.x0 - words[-1][-1].box.x1 <= rows.word_gap_pt:
                words[-1].append(mark)
            else:
                words.append([mark])
        texts: list[str] = []
        box: Box | None = None
        for word in words:
            texts.append(" ".join("".join(mark.text or "" for mark in word).split()))
            for mark in word:
                box = mark.box if box is None else box.union(mark.box)
        text = " ".join(part for part in texts if part)
        if text and box is not None:
            lines.append((text, box))
    lines.sort(key=lambda item: (item[1].top, item[1].x0))
    return lines


def _strict_overlap(a: Box, b: Box) -> bool:
    return a.x0 < b.x1 and a.x1 > b.x0 and a.top < b.bottom and a.bottom > b.top


def _touches_edge(box: Box, ink: PageInk, rows: RowSettings) -> bool:
    """`rows.py`'s rule: within the margin of, or across, its drawing's edge or the page's."""
    margin = rows.edge_margin_pt
    frames = [frame for frame in ink.drawing_boxes if frame.meets(box)]
    frames.append(Box(_ZERO, _ZERO, ink.width, ink.height))
    return any(
        box.x0 <= frame.x0 + margin
        or box.x1 >= frame.x1 - margin
        or box.top <= frame.top + margin
        or box.bottom >= frame.bottom - margin
        for frame in frames
    )


@dataclass(slots=True)
class _Placed:
    run: _Run
    owner: int | None
    line_y: Decimal
    slot: tuple[Decimal, Decimal]
    ambiguous: bool


def plan_slots(
    rows: PageRows,
    ink: PageInk,
    *,
    settings: CropSettings,
    row_settings: RowSettings,
    selected_row: CountertopRowCandidate | None = None,
    row_choice_made: bool = False,
) -> SlotPlan:
    """The rank-1 row's slots and overall, each with its label runs and their crops.

    Returns a plan with no row when the page has no candidate. Never picks a row silently: the
    plan's `ambiguity` says why the row cannot be trusted, and the caller sends its readings to a
    person.
    """
    row: CountertopRowCandidate | None
    ambiguity: str | None
    if row_choice_made:
        if selected_row is None:
            return SlotPlan(
                row=None,
                ambiguity="the row reader selected no candidate; the reviewer must choose the row",
                slots=(),
                overall=None,
            )
        if selected_row not in rows.candidates[:6]:
            raise ValueError("the row reader may select only one of the first six code candidates")
        row = selected_row
        # A Claude row choice is a choice among code-generated candidates, not a request for the
        # row to also contain an overall. Piece spans remain useful without an overall; the
        # mapping/check boundary simply has no overall operand and therefore cannot produce a
        # countertop-width verdict.
        ambiguity = None
    else:
        row, ambiguity = choose_row(rows)
    if row is None:
        return SlotPlan(row=None, ambiguity=None, slots=(), overall=None)
    xs = row.ticks
    lines: list[tuple[Literal["chain", "overall"], Decimal]] = [("chain", row.y)]
    if row.overall is not None:
        lines.append(("overall", row.overall.y))
    top = min(y for _, y in lines) - settings.above_line_pt - settings.run_maximum_height_pt
    bottom = max(y for _, y in lines) + settings.run_maximum_height_pt
    window = Box(
        max(_ZERO, xs[0] - settings.window_margin_pt),
        max(_ZERO, top),
        min(ink.width, xs[-1] + settings.window_margin_pt),
        min(ink.height, bottom),
    )
    paths, characters, spaces = _marks(ink, window, settings)

    tick_points = [(x, row.y) for x in xs]
    if row.overall is not None:
        tick_points += [(row.overall.x0, row.overall.y), (row.overall.x1, row.overall.y)]
    straight = [mark for mark in paths if mark.kind == "line"]
    kept: list[_Mark] = []
    for mark in paths:
        box = mark.box
        if (
            box.width <= settings.tick_mark_maximum_pt
            and box.height <= settings.tick_mark_maximum_pt
            and any(
                abs(box.centre_x - tx) <= settings.tick_reach_x_pt
                and abs(box.centre_y - ty) <= settings.tick_reach_y_pt
                for tx, ty in tick_points
            )
        ):
            continue
        if _is_dash(mark, straight, settings):
            continue
        kept.append(mark)
    runs = _merged_runs([*kept, *characters], settings)

    slots = tuple(pairwise(xs))
    placed: list[_Placed] = []
    for run in runs:
        box = run.box
        if box.x1 < xs[0] - settings.row_reach_x_pt or box.x0 > xs[-1] + settings.row_reach_x_pt:
            continue
        options = sorted(
            (abs(line_y - box.bottom), position, owner, line_y)
            for position, (owner, line_y) in enumerate(lines)
            if box.bottom >= line_y - settings.above_line_pt
            and box.top <= line_y + settings.below_line_pt
        )
        if not options:
            continue
        _, _, which, line_y = options[0]
        if which == "overall":
            assert row.overall is not None
            placed.append(
                _Placed(run, None, line_y, (row.overall.x0, row.overall.x1), ambiguous=False)
            )
            continue
        coverage: list[tuple[Decimal, int]] = []
        for index, (a, b) in enumerate(slots):
            slack = (
                settings.narrow_slack_pt
                if (b - a) < settings.narrow_slot_pt
                else settings.wide_slack_pt
            )
            overlap = min(box.x1, b + slack) - max(box.x0, a - slack)
            if overlap > 0:
                width = max(settings.overlap_floor_pt, b - a)
                coverage.append((min(Decimal(1), overlap / width), index))
        if not coverage:
            continue
        coverage.sort(key=lambda item: (-item[0], item[1]))
        best, index = coverage[0]
        ambiguous = (
            len(coverage) > 1 and coverage[1][0] >= settings.ambiguous_coverage_fraction * best
        )
        placed.append(_Placed(run, index, line_y, slots[index], ambiguous))

    # Fragments of one label on one baseline, inside one slot, are one label.
    owners: dict[int | None, list[_Placed]] = {}
    for item in sorted(placed, key=lambda item: (item.run.box.x0, item.run.box.top)):
        group = owners.setdefault(item.owner, [])
        for existing in group:
            first, second = existing.run.box, item.run.box
            height = max(first.height, second.height)
            if (
                abs(first.centre_y - second.centre_y) <= settings.baseline_fraction * height
                and second.x0 - first.x1 <= settings.fragment_gap_pt
            ):
                existing.run.box = first.union(second)
                existing.run.marks.extend(item.run.marks)
                existing.ambiguous = existing.ambiguous or item.ambiguous
                break
        else:
            group.append(item)

    placed_ids = {id(item.run) for item in placed}
    every_run = [item.run.box for group in owners.values() for item in group]
    every_run += [run.box for run in runs if id(run) not in placed_ids]

    def label_for(item: _Placed) -> PlannedLabel:
        box = item.run.box
        a, b = item.slot
        pad = settings.padding_pt
        x0, x1 = box.x0 - pad, box.x1 + pad
        y0 = min(box.top, item.line_y) - pad
        y1 = max(box.bottom, item.line_y) + pad
        ticks_in = (b - a) <= settings.ticks_in_crop_maximum_slot_pt
        if ticks_in:
            x0 = min(x0, a - settings.tick_margin_pt)
            x1 = max(x1, b + settings.tick_margin_pt)
        others = [other for other in every_run if other is not box]
        crowded = any(_strict_overlap(other, box) for other in others)
        for other in others:
            if other.bottom > y0 and other.top < y1:
                if other.x1 <= box.x0 and other.x1 + settings.clip_gap_x_pt > x0:
                    x0 = other.x1 + settings.clip_gap_x_pt
                if other.x0 >= box.x1 and other.x0 - settings.clip_gap_x_pt < x1:
                    x1 = other.x0 - settings.clip_gap_x_pt
        for other in others:
            if other.x1 > x0 and other.x0 < x1:
                if other.bottom <= box.top and other.bottom + settings.clip_gap_y_pt > y0:
                    y0 = other.bottom + settings.clip_gap_y_pt
                if other.top >= box.bottom and other.top - settings.clip_gap_y_pt < y1:
                    y1 = other.top - settings.clip_gap_y_pt
        ticks_in = ticks_in and x0 <= a and x1 >= b
        crop = Box(
            max(_ZERO, min(x0, box.x0)),
            max(_ZERO, min(y0, box.top)),
            min(ink.width, max(x1, box.x1)),
            min(ink.height, max(y1, box.bottom)),
        )
        chars = [mark for mark in item.run.marks if mark.kind == "char"]
        all_text = len(chars) == item.run.count
        inside = [
            space
            for space in spaces
            if box.x0 <= space.box.centre_x <= box.x1
            and box.top <= space.box.centre_y <= box.bottom
        ]
        text_lines = _text_lines([*chars, *inside], row_settings) if chars else []
        stacked = any(
            _strict_overlap(first[1], second[1])
            for position, first in enumerate(text_lines)
            for second in text_lines[position + 1 :]
        )
        text = " ".join(line for line, _ in text_lines) if all_text and not stacked else None
        lane = Lane.TEXT if all_text and not stacked else Lane.GLYPHS
        reach = settings.padding_pt
        near = Box(box.x0 - reach, box.top - reach, box.x1 + reach, box.bottom + reach)
        return PlannedLabel(
            box=box,
            crop=crop,
            lane=lane,
            text=text,
            text_stacked=stacked,
            has_digit=(lane is Lane.GLYPHS)
            or any(char.isdigit() for line, _ in text_lines for char in line),
            touches_edge=_touches_edge(box, ink, row_settings),
            ambiguous_slot=item.ambiguous,
            crowded=crowded,
            ticks_in_crop=ticks_in,
            path_boxes=tuple(mark.box for mark in paths if mark.box.meets(near)),
        )

    planned_slots = tuple(
        PlannedOwner(
            index=slot.index,
            x0=slot.x0,
            x1=slot.x1,
            line_y=row.y,
            band=slot.box,
            labels=tuple(label_for(item) for item in owners.get(slot.index, [])),
        )
        for slot in row.slots
    )
    overall: PlannedOwner | None = None
    if row.overall is not None:
        overall = PlannedOwner(
            index=None,
            x0=row.overall.x0,
            x1=row.overall.x1,
            line_y=row.overall.y,
            band=row.overall.box,
            labels=tuple(label_for(item) for item in owners.get(None, [])),
        )
    return SlotPlan(row=row, ambiguity=ambiguity, slots=planned_slots, overall=overall)
