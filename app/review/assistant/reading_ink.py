"""The pen-stroke half of the reading coverage (`reading_coverage`): on each page the set pack's
drawn pass reads, the character-sized ink that looks like a printed number and became no label,
with its box and why code did not read it. $0, no OCR, a fraction of a second a page.

It only COUNTS: nothing here makes, changes or drops a label, a finding or a verdict.

**How a spot is found** (the method of the stroke research's funnel, in a cheap form): the
page's character-sized paths outside every label are grouped by closeness (as the drawn route's
clusters, with no page limit), each group split into rows of glyphs (`ink_labels`), and a row on
a drawing (`dim_labels._side`) is a candidate.

A candidate **looks like a number** only when its own strokes say so, and code is unsure of
everything else, so it is not counted (`looks_like_number`):

- it ends a row in an inch mark: two twin ticks standing apart (`_TICKS_APART`), in ink holding a
  glyph that is not one straight stroke (`_mark_turns`); a stroke drawn twice to look bold (an `A`'s bar, a
  `T`'s top) is no inch mark, nor is the typed text's own `"` beside it;
- it is millimetres over inches in brackets (`305` over `[12]`), the millimetres more than
  straight strokes, the brackets with corners, neither row one shape repeated (`_mm_turn`);
- two glyphs of it that are more than a straight stroke are centred on a dimension line, on a
  page code read almost nothing of (`Candidate.on_line`);
- on the vendor's drawings only, every one of its glyphs that is more than a straight stroke has
  the shape of a digit this set's vendor drew in a number code DID read (the set's own "alphabet", learned from its confirmed drawn labels: a shape match,
  never a reading), and none of its digit-sized straight strokes lies flat or slants (an upright
  one may be a `1`): a word or a tag (`TA-109`), whose letters are no digit, is not counted
  (`_digit_shapes`).

Never counted, whatever its strokes: ink with no glyph that is more than one straight stroke
(hatching, a wall's corner: `Candidate.shaped`), ink the page's own typed text sits on
(`Candidate.typed`: an AutoCAD sheet types its text and draws it again as strokes; the typed half
of the count already counted that text, as a label, as a number in a note, or not at all when it
is a tag, a word or a scale note), and ink inside a closed tag-sized shape (a bubble or a tag's
box, `dim_labels._Parsed.rings`). The count is "about": a number touching other ink, a lone `1"`,
or a number drawn only in straight strokes can be missed.

**Why it was not read**, the first that applies: GV's sticker over it, millimetres over
[inches], a stacked fraction, sideways, no inch mark of its own, lines drawn through it, too
small, crowded by other ink; else "other" (the two readers did not agree on one value, or it has
no dimension line of its own).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from time import process_time
from typing import Any, Final

import numpy as np

from app.review.assistant import dim_labels as dl
from app.review.assistant import ink_labels as il

__all__ = ["Candidate", "PageInk", "looks_like_number", "page_ink", "unread_spots"]

type Box = tuple[float, float, float, float]
type Shape = tuple[np.ndarray, float]
"""A glyph's shape in a reading direction: its strokes drawn on a small grid (unit length), and
its width over its height."""

#: The size (points) a glyph's neighbours are looked for within at least (1.1 times it): a
#: character's own size decides how far apart two read together (`ink_labels._rows_of`).
_CHARACTER_PT: Final = 8.0
#: A candidate row is a written number's size: its longer side and its shorter side, points.
_MIN_PT: Final = 3.0
_MAX_PT: Final = 60.0
_MAX_ACROSS_PT: Final = 30.0
#: A glyph bigger than this (points) is overlapping strokes chained along a line, no character.
_GLYPH_MAX_PT: Final = 16.0
#: A label of more glyphs than this is a drawing's detail (hatching, a dotted fill).
_LABEL_MAX_GLYPHS: Final = 40
#: More character-sized paths than this round a candidate: hatching or a dotted fill.
_BUSY_PATHS: Final = 150
#: A glyph is "too small" below this height (points): the research's tiny text.
_SMALL_PT: Final = 3.0
#: A pasted picture smaller than this (square points) over a number is GV's sticker.
_STICKER_AREA: Final = 40000.0
#: The small grid a glyph is drawn on (cells), and how alike two drawn glyphs must be (their
#: grids' correlation, less half their width difference) to be the same digit: 0.75 matched 43 of
#: 94 unread Set 3 numbers with no inch mark of their own and 15 of 1,286 other ink groups.
_GRID_W: Final = 8
_GRID_H: Final = 12
_SAME_SHAPE: Final = 0.75
#: An unmarked candidate with more digit-sized glyphs than this is a word or a note, not shaped.
_MAX_SHAPED: Final = 40
#: A label overlapping a candidate by this share (either way) means the candidate was read.
_READ_SHARE: Final = 0.3
#: An inch mark's two ticks stand apart (centre to centre, along the row) by at least this share
#: of their length. Measured 2026-11-05: a vendor's drawn marks 0.92 to 1.12, an architect's
#: lettering 0.50 to 0.68, the stroke-lettering tests 0.47; one stroke drawn twice to look bold
#: (an `A`'s bar, a foot mark) 0.11 to 0.30.
_TICKS_APART: Final = 0.35
#: Ink is the page's typed text drawn again when more than this share of its glyphs have their
#: centre inside one of the page's typed characters.
_TYPED_SHARE: Final = 0.5
#: A ring (`dim_labels._Parsed.rings`) holds a candidate when the candidate lies inside it with
#: this slack (points).
_RING_SLACK: Final = 0.5
#: A stacked fraction's digit is at least this tall (points) for the alphabet test: a screw's
#: countersink drawn small over a bar is no fraction.
_FRACTION_DIGIT_PT: Final = 1.5
#: A straight stroke stands upright (a `1`) when it is no wider than this share of its height,
#: in the reading direction.
_UPRIGHT: Final = 0.3
#: A bracket's straight back runs at least this share of its height.
_BRACKET_BACK: Final = 0.6
#: A page where code read fewer labels than this is looked at for numbers drawn on dimension
#: lines (`PageInk.stroke_numbers`): the set pack's own threshold for a whole-page drawing.
FEW_LABELS: Final = 3

REASON_ORDER: Final = (
    "gv_sticker",
    "mm_row",
    "stacked_fraction",
    "sideways",
    "no_inch_mark",
    "lines_through",
    "too_small",
    "crowded",
    "other",
)


@dataclass(slots=True)
class Candidate:
    """Ink on a drawing that may be a printed number code did not read."""

    box: Box
    side: str
    marked: bool
    """Its own strokes say "dimension": an inch mark (`_mark_turns`), or millimetres over
    [inches] (`_mm_turn`)."""
    reasons: tuple[str, ...]
    """Every reason that applies, in `REASON_ORDER`."""
    shapes: tuple[tuple[Shape, ...], ...] = ()
    """Groups of glyph shapes for the alphabet test, every shape of one group to be a digit's:
    its glyphs that are not one straight stroke, one group per direction it may read in which it
    has no letter's straight stroke (`_digit_shapes`); then each stacked fraction's numerator and denominator glyphs that are not one
    straight stroke, one group per fraction and direction, when each of them is at least
    `_FRACTION_DIGIT_PT` tall."""
    on_line: bool = False
    """Two glyphs or more centred on a dimension line (`dim_labels.dimension_line`): a number
    drawn as pen strokes in a dimension's place. Looked for only on a page where code read fewer
    than `FEW_LABELS` labels (`PageInk.stroke_numbers`)."""
    typed: bool = False
    """The page's own typed text sits on it (`_TYPED_SHARE`): that text drawn again as strokes,
    counted by the typed half of the count, never here."""
    tagged: bool = False
    """It lies inside a closed tag-sized shape (a bubble, a tag's box): a tag, never a
    dimension."""
    shaped: int = 0
    """How many of its glyphs are not one straight stroke (`_straight`): none is line work
    (hatching, a wall's corner), never a number."""


@dataclass(slots=True)
class PageInk:
    """One page's candidates and the digit shapes of the drawn labels code read on it."""

    candidates: list[Candidate] = field(default_factory=list)
    digits: list[tuple[Shape, str]] = field(default_factory=list)
    seconds: float = 0.0
    """Processor seconds this page's counting took."""
    stroke_numbers: int = 0
    """On a page where code read fewer than `FEW_LABELS` labels: its candidates sitting on a
    dimension line (`Candidate.on_line`), numbers drawn as lines that code could not read. The
    set pack keeps such a page as a drawing whose numbers cannot be read (`set_pack`), never a
    general page."""


# ---- shapes ----------------------------------------------------------------------------------


def _turned(points: np.ndarray, turn: il.Turn) -> np.ndarray:
    """`ink_labels._point` on a whole array of points."""
    if turn == 90:
        return np.stack([-points[:, 1], points[:, 0]], axis=1)
    if turn == 270:
        return np.stack([points[:, 1], -points[:, 0]], axis=1)
    return points


def _shape(glyph: il.Glyph, turn: il.Turn) -> Shape:
    """The glyph drawn small in the reading direction `turn`: its strokes on a `_GRID_W` x
    `_GRID_H` grid (its height fills the grid's), softened one cell and scaled to unit length,
    so two drawings of one digit match whatever order or pieces their strokes come in; and its
    width over its height."""
    frame = il._frame(glyph.box, turn)
    height = max(frame[3] - frame[1], 1e-6)
    scale = max(height, (frame[2] - frame[0]) * _GRID_H / _GRID_W) / _GRID_H
    starts: list[np.ndarray] = []
    ends: list[np.ndarray] = []
    for path in glyph.paths:
        for line in path.polylines:
            points = np.asarray(line, dtype=float).reshape(-1, 2)
            if len(points) == 1:
                points = np.vstack([points, points])
            starts.append(points[:-1])
            ends.append(points[1:])
    grid = np.zeros((_GRID_H + 2, _GRID_W + 2))
    if starts:
        a = _turned(np.concatenate(starts), turn)
        b = _turned(np.concatenate(ends), turn)
        steps = np.maximum(2, (2 * np.hypot(*(b - a).T) / scale).astype(int) + 1)
        segment = np.repeat(np.arange(len(a)), steps)
        at = np.arange(int(steps.sum())) - np.repeat(np.cumsum(steps) - steps, steps)
        share = (at / np.maximum(steps[segment] - 1, 1))[:, None]
        along = a[segment] + (b[segment] - a[segment]) * share
        cols = np.clip(((along[:, 0] - frame[0]) / scale).astype(int) + 1, 0, _GRID_W + 1)
        rows = np.clip(((along[:, 1] - frame[1]) / scale).astype(int) + 1, 0, _GRID_H + 1)
        grid[rows, cols] = 1.0
    soft = grid.copy()
    soft[1:] += 0.5 * grid[:-1]
    soft[:-1] += 0.5 * grid[1:]
    soft[:, 1:] += 0.5 * grid[:, :-1]
    soft[:, :-1] += 0.5 * grid[:, 1:]
    flat = soft.ravel() - soft.mean()
    norm = float(np.linalg.norm(flat))
    return (flat / norm if norm else flat), (frame[2] - frame[0]) / height


class _Alphabet:
    """The digit shapes a set's vendor drew in the numbers code read."""

    def __init__(self, digits: Sequence[tuple[Shape, str]]) -> None:
        self._grids = np.stack([shape[0] for shape, _ in digits]) if digits else None
        self._widths = np.array([shape[1] for shape, _ in digits])

    def __bool__(self) -> bool:
        return self._grids is not None

    def knows(self, shape: Shape) -> bool:
        """Whether the shape is close to one of the digits (`_SAME_SHAPE`: how alike the two
        drawings are, less half the difference of their widths)."""
        if self._grids is None:
            return False
        alike = self._grids @ shape[0] - 0.5 * np.abs(self._widths - shape[1])
        return bool(alike.max() >= _SAME_SHAPE)


def looks_like_number(candidate: Candidate, alphabet: _Alphabet | None) -> bool:
    """Whether a candidate counts as a printed number: never when typed text sits on it, when a
    tag's ring holds it or when no glyph of it is more than one straight stroke; else when its
    own strokes mark it as a dimension, when two of its glyphs that are more than a straight
    stroke are centred on a dimension line (on a page code read almost nothing of), or, on the
    vendor's drawings, when one of its groups of glyph shapes (`Candidate.shapes`: the whole of
    it read in one direction, or one of its stacked fractions) is all shaped as digits of the
    set's alphabet: the vendor's own hand, which says nothing of another drawing's lettering
    (a wall's corner on the architect's plan can match a vendor's 7). Anything else is ink
    code cannot tell, and is not counted."""
    if candidate.typed or candidate.tagged or not candidate.shaped:
        return False
    if candidate.marked or (candidate.on_line and candidate.shaped >= 2):
        return True
    if alphabet is None or not alphabet or candidate.side != "vendor":
        return False
    return any(shapes and all(alphabet.knows(s) for s in shapes) for shapes in candidate.shapes)


def alphabet_of(pages: Sequence[PageInk]) -> _Alphabet:
    return _Alphabet([pair for page in pages for pair in page.digits])


# ---- the page pass ---------------------------------------------------------------------------


def _digits_of(text: str) -> tuple[list[str], list[str], list[str]] | None:
    """`26 3/4"` -> (['2', '6'], ['3'], ['4']); feet, millimetres and words: `None`."""
    plain = text.replace('"', "").strip()
    if "'" in text or "[" in text:
        return None
    whole, _, fraction = plain.partition(" ") if " " in plain else (plain, "", "")
    if "/" in whole and not fraction:
        whole, fraction = "", whole
    top, _, bottom = fraction.partition("/")
    parts = (whole, top, bottom)
    if not any(parts) or not all(p.isdigit() or p == "" for p in parts):
        return None
    if bool(top) != bool(bottom):
        return None
    return list(whole), list(top), list(bottom)


def _learn(page: Any, near: _Near, labels: Sequence[Mapping[str, Any]]) -> list[tuple[Shape, str]]:
    """The digit shapes of the vendor's drawn labels code read on the page (one row, glyphs
    matching the value's digits one for one): the set's alphabet. A reference's lettering read
    on its drawings teaches nothing: its digits are also its room tags' and grid bubbles', so
    counting by them would count tags as unread numbers."""
    out: list[tuple[Shape, str]] = []
    for label in labels:
        if label.get("route") != "drawn" or label.get("origin") or label.get("mm"):
            continue
        if label.get("side") != "vendor":
            continue
        digits = _digits_of(str(label.get("text") or ""))
        box = label.get("box")
        if digits is None or not isinstance(box, Sequence) or len(box) != 4:
            continue
        spot: Box = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        found = [g for g in il.labels(near(spot), spot) if dl._overlap(g.box, spot) > 0.5]
        if not found:
            continue
        ink = found[0]
        turns = dl._marked_turns(ink) or il.turns(ink)
        turn: il.Turn = 0 if label.get("orientation") == "h" else (turns[0] if turns else 0)
        if label.get("orientation") == "v" and turn == 0:
            turn = 90
        rows = il.rows(ink, turn)
        if len(rows) != 1:
            continue
        whole: list[il.Glyph] = []
        top: list[il.Glyph] = []
        bottom: list[il.Glyph] = []
        for token in rows[0]:
            if token.kind == "glyph":
                whole.append(token.glyphs[0])
            elif token.kind == "fraction":
                top += token.glyphs
                bottom += token.below
        if (len(whole), len(top), len(bottom)) != tuple(len(part) for part in digits):
            continue
        for glyph, digit in zip(whole + top + bottom, [*digits[0], *digits[1], *digits[2]]):
            out.append((_shape(glyph, turn), digit))
    return out


class _Near:
    """The page's character-sized paths by box, for quick "what is near here" questions."""

    def __init__(self, paths: Sequence[Any]) -> None:
        self.paths = list(paths)
        self.boxes = (
            np.array([p.box for p in self.paths], dtype=float) if self.paths else np.zeros((0, 4))
        )

    def mask(self, box: Sequence[float], reach: float) -> np.ndarray:
        x0, y0, x1, y1 = box
        b = self.boxes
        return (
            (b[:, 0] >= x0 - reach)
            & (b[:, 2] <= x1 + reach)
            & (b[:, 1] >= y0 - reach)
            & (b[:, 3] <= y1 + reach)
        )

    def __call__(self, box: Sequence[float], reach: float = il.GATHER_PT) -> list[Any]:
        return [self.paths[i] for i in np.nonzero(self.mask(box, reach))[0]]


def _covered(boxes: np.ndarray, others: Sequence[Sequence[float]], share: float) -> np.ndarray:
    """For each row of `boxes`, whether more than `share` of it lies inside one of `others`."""
    if not len(boxes) or not others:
        return np.zeros(len(boxes), dtype=bool)
    o = np.array(others, dtype=float)
    across = np.clip(
        np.minimum(boxes[:, None, 2], o[None, :, 2]) - np.maximum(boxes[:, None, 0], o[None, :, 0]),
        0,
        None,
    )
    down = np.clip(
        np.minimum(boxes[:, None, 3], o[None, :, 3]) - np.maximum(boxes[:, None, 1], o[None, :, 1]),
        0,
        None,
    )
    area = np.maximum((boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1]), 1e-9)
    return ((across * down) / area[:, None] > share).any(axis=1)  # type: ignore[no-any-return]


def _pairs(boxes: np.ndarray, reach: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Every pair of boxes (`first`, `second` indexes) whose x ranges come within `reach` of the
    first's right edge, found by a sweep along x on whole arrays."""
    count = len(boxes)
    if count < 2:
        return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
    order = np.argsort(boxes[:, 0], kind="stable")
    x0 = boxes[order, 0]
    ends = np.searchsorted(x0, boxes[order, 2] + reach[order], side="right")
    span = np.maximum(ends - np.arange(count) - 1, 0)
    first = np.repeat(np.arange(count), span)
    second = first + 1 + (np.arange(int(span.sum())) - np.repeat(np.cumsum(span) - span, span))
    return order[first], order[second]


def _components(count: int, first: np.ndarray, second: np.ndarray) -> list[np.ndarray]:
    """The connected groups of `count` items joined by the pairs, each an index array."""
    label = np.arange(count)
    while len(first):
        smaller = np.minimum(label[first], label[second])
        before = label.copy()
        np.minimum.at(label, first, smaller)
        np.minimum.at(label, second, smaller)
        label = label[label]
        if np.array_equal(label, before):
            break
    order = np.argsort(label, kind="stable")
    cuts = np.nonzero(np.diff(label[order]))[0] + 1
    return list(np.split(order, cuts))


def _glyph_groups(boxes: np.ndarray) -> list[np.ndarray]:
    """Paths that are one glyph (`ink_labels._glyphs`' rule on whole arrays): one inside the
    other's box, or overlapping along both axes by half the smaller one's extent."""
    first, second = _pairs(boxes, np.full(len(boxes), 0.6))
    a, b = boxes[first], boxes[second]
    wa, ha, wb, hb = a[:, 2] - a[:, 0], a[:, 3] - a[:, 1], b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]
    size = np.maximum.reduce([wa, ha, wb, hb])
    slack = 0.05 * size

    def inside(inner: np.ndarray, outer: np.ndarray, wide: np.ndarray, tall: np.ndarray) -> Any:
        return (
            (wide > 2 * slack)
            & (tall > 2 * slack)
            & (inner[:, 0] >= outer[:, 0] - slack)
            & (inner[:, 2] <= outer[:, 2] + slack)
            & (inner[:, 1] >= outer[:, 1] - slack)
            & (inner[:, 3] <= outer[:, 3] + slack)
        )

    floor = 0.12 * size
    over_x = np.minimum(a[:, 2], b[:, 2]) - np.maximum(a[:, 0], b[:, 0])
    over_y = np.minimum(a[:, 3], b[:, 3]) - np.maximum(a[:, 1], b[:, 1])
    overlap = (
        (over_x >= -0.02 * size)
        & (over_y >= -0.02 * size)
        & (over_x >= 0.5 * np.maximum(floor, np.minimum(wa, wb)) - 0.02 * size)
        & (over_y >= 0.5 * np.maximum(floor, np.minimum(ha, hb)) - 0.02 * size)
    )
    join = inside(a, b, wb, hb) | inside(b, a, wa, ha) | overlap
    return _components(len(boxes), first[join], second[join])


def _label_groups(boxes: np.ndarray) -> list[np.ndarray]:
    """Glyphs that read together (`ink_labels._rows_of`' rule on whole arrays): side by side or
    stacked, a small gap apart for their size, or a word space apart when both are big and
    aligned. Rows stacked a character apart (millimetres over [inches]) stay one label."""
    wide, tall = boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]
    sizes = np.maximum(wide, tall)
    first, second = _pairs(boxes, 1.1 * np.maximum(sizes, _CHARACTER_PT))
    # Rows apart by more than the widest gap can never join: dropped before the full test.
    near = boxes[second, 1] <= boxes[first, 3] + 1.1 * np.maximum(sizes[first], sizes[second])
    near &= boxes[first, 1] <= boxes[second, 3] + 1.1 * np.maximum(sizes[first], sizes[second])
    first, second = first[near], second[near]
    a, b = boxes[first], boxes[second]
    sa, sb = sizes[first], sizes[second]
    size = np.maximum(sa, sb)
    gap_x = np.maximum(a[:, 0], b[:, 0]) - np.minimum(a[:, 2], b[:, 2])
    gap_y = np.maximum(a[:, 1], b[:, 1]) - np.minimum(a[:, 3], b[:, 3])
    big = np.minimum(sa, sb) >= 0.6 * size
    low_x = np.minimum(wide[first], wide[second])
    low_y = np.minimum(tall[first], tall[second])
    across = (-gap_y >= 0.25 * low_y) & (
        (gap_x <= 0.6 * size) | (big & (-gap_y >= 0.6 * low_y) & (gap_x <= 1.1 * size))
    )
    stacked = (-gap_x >= 0.25 * low_x) & (
        (gap_y <= 0.6 * size) | (big & (-gap_x >= 0.6 * low_x) & (gap_y <= 1.1 * size))
    )
    join = (np.minimum(sa, sb) >= 0.12 * size) & (across | stacked)
    return _components(len(boxes), first[join], second[join])


def _page_labels(near: _Near, free: np.ndarray) -> list[il.Label]:
    """The free character-sized paths as labels, the way the drawn route's `ink_labels.labels`
    makes them round each spot (glyphs, then rows), but for the whole page at once."""
    index = np.nonzero(free)[0]
    if not len(index):
        return []
    paths = [near.paths[int(i)] for i in index]
    boxes = near.boxes[index]
    glyphs: list[il.Glyph] = []
    for members in _glyph_groups(boxes):
        if len(members) == 1:
            path = paths[int(members[0])]
            glyphs.append(il.Glyph((path,), path.box))
            continue
        own = tuple(paths[int(i)] for i in members)
        box = il._union([p.box for p in own])
        if max(box[2] - box[0], box[3] - box[1]) > _GLYPH_MAX_PT:
            # Overlaps chained along a line or a fill (page-wide, nothing stops the chain): no
            # character; each path stays its own glyph.
            glyphs += [il.Glyph((path,), path.box) for path in own]
        else:
            glyphs.append(il.Glyph(own, box))
    if not glyphs:
        return []
    glyph_boxes = np.array([g.box for g in glyphs], dtype=float)
    out: list[il.Label] = []
    for members in _label_groups(glyph_boxes):
        if len(members) > _LABEL_MAX_GLYPHS:
            continue  # a drawing's detail (hatching, a dotted fill), never one number
        row = sorted((glyphs[int(i)] for i in members), key=lambda g: (g.box[1], g.box[0]))
        out.append(il.Label(tuple(row), il._union([g.box for g in row])))
    return out


def _read_already(box: Box, taken: np.ndarray) -> bool:
    """Whether a label covers the candidate: more than `_READ_SHARE` of either inside the other."""
    if not len(taken):
        return False
    across = np.clip(np.minimum(box[2], taken[:, 2]) - np.maximum(box[0], taken[:, 0]), 0, None)
    down = np.clip(np.minimum(box[3], taken[:, 3]) - np.maximum(box[1], taken[:, 1]), 0, None)
    shared = across * down
    own = max((box[2] - box[0]) * (box[3] - box[1]), 1e-9)
    theirs = np.maximum((taken[:, 2] - taken[:, 0]) * (taken[:, 3] - taken[:, 1]), 1e-9)
    return bool(((shared / own > _READ_SHARE) | (shared / theirs > _READ_SHARE)).any())


class _Lines:
    """The page's straight line work, for "does a long line pass through this box"."""

    def __init__(self, lines: np.ndarray) -> None:
        self.empty = not len(lines)
        if self.empty:
            return
        self.x0 = np.minimum(lines[:, 0], lines[:, 2])
        self.x1 = np.maximum(lines[:, 0], lines[:, 2])
        self.y0 = np.minimum(lines[:, 1], lines[:, 3])
        self.y1 = np.maximum(lines[:, 1], lines[:, 3])
        self.length = np.hypot(lines[:, 2] - lines[:, 0], lines[:, 3] - lines[:, 1])

    def through(self, box: Box) -> bool:
        """Whether a straight line 1.5 times the box's size or longer passes through the middle
        of the box (not just its edge)."""
        if self.empty:
            return False
        x0, y0, x1, y1 = box
        size = max(x1 - x0, y1 - y0)
        ix0, iy0 = x0 + 0.2 * (x1 - x0), y0 + 0.2 * (y1 - y0)
        ix1, iy1 = x1 - 0.2 * (x1 - x0), y1 - 0.2 * (y1 - y0)
        hit = (
            (self.x1 >= ix0)
            & (self.x0 <= ix1)
            & (self.y1 >= iy0)
            & (self.y0 <= iy1)
            & (self.length >= 1.5 * size)
        )
        return bool(hit.any())


def _crowded(box: Box, own: set[int], near: _Near, turn: il.Turn) -> bool:
    """Whether other character-sized ink sits in line with the candidate within 1.2 times its
    height (a word space), in its reading direction: a cheap form of the drawn route's own test."""
    frame = il._frame(box, turn)
    tall = frame[3] - frame[1]
    if tall <= 0:
        return False
    reach = 1.2 * max(box[2] - box[0], box[3] - box[1])
    for i in np.nonzero(near.mask(box, reach))[0]:
        if int(i) in own:
            continue
        other = il._frame(near.paths[i].box, turn)
        height = other[3] - other[1]
        if height < 0.5 * tall:
            continue
        across = min(frame[3], other[3]) - max(frame[1], other[1])
        gap = max(other[0] - frame[2], frame[0] - other[2])
        if across >= 0.5 * min(tall, height) and gap < 1.2 * tall:
            return True
    return False


def page_ink(
    parsed: Any,
    halves: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
) -> PageInk:
    """The page's candidates (ink on a drawing that may be a printed number, outside every label)
    and the digit shapes of its read drawn labels. `parsed`: `dim_labels._parse`'s page (already
    read by the drawn pass); `labels`: the page's labels after it."""
    started = process_time()
    page = parsed.page
    result = PageInk()
    if not page.paths:
        result.seconds = round(process_time() - started, 3)
        return result
    near = _Near(page.paths)
    result.digits = _learn(page, near, labels)
    read: list[Box] = [
        (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        for b in (label.get("box") for label in labels)
        if isinstance(b, Sequence) and len(b) == 4
    ]
    free = ~_covered(near.boxes, read, 0.5)
    lines = _Lines(dl._line_ends(parsed.segments))
    stickers = [
        box for box in page.pictures if (box[2] - box[0]) * (box[3] - box[1]) < _STICKER_AREA
    ]
    index_of = {id(path): i for i, path in enumerate(near.paths)}
    taken = np.array(read, dtype=float) if read else np.zeros((0, 4))
    typed = _Typed(page.chars)
    rings = np.array(getattr(parsed, "rings", ()) or np.zeros((0, 4)), dtype=float)
    few = sum(1 for label in labels if not label.get("derived")) < FEW_LABELS
    for ink in _page_labels(near, free):
        row = list(ink.glyphs)
        if max(g.size for g in row) < il._MIN_LABEL_PT or il._not_text(row):
            continue
        box = ink.box
        longer = max(box[2] - box[0], box[3] - box[1])
        shorter = min(box[2] - box[0], box[3] - box[1])
        if not (_MIN_PT <= longer <= _MAX_PT and shorter <= _MAX_ACROSS_PT):
            continue
        side = dl._side(box, halves)
        if side == "unknown" or _read_already(box, taken):
            continue
        if int(near.mask(box, il.GATHER_PT).sum()) > _BUSY_PATHS:
            continue  # hatching or a dotted fill
        found = _candidate(ink, side, near, lines, stickers, index_of, typed)
        found.typed = typed.over(row)
        found.tagged = _in_ring(box, rings)
        if (
            few
            and len(row) >= 2
            and _number_row(row, box)
            and _on_dimension_line(box, parsed.segments)
        ):
            found.on_line = True
            result.stroke_numbers += 1
        result.candidates.append(found)
    result.seconds = round(process_time() - started, 3)
    return result


#: A written number of two digits or more is at least this many times longer than high, and its
#: strokes cover at least this share of its height.
_ROW_LENGTH: Final = 1.2
_ROW_COVER: Final = 0.8
#: How far from its span's middle a dimension's number may be written (a share of the span).
_OFF_MIDDLE: Final = 0.1
#: A dimension line a number drawn as strokes sits on spans at least this many times the
#: number's own length (a tag's box, a room number's frame, is about as long as its text).
_SPAN_OVER_TEXT: Final = 2.0


def _on_dimension_line(box: Box, segments: Sequence[Any]) -> bool:
    """Whether ink in `box` sits on (or in a gap of) a dimension line with an extension line or a
    tick at an end, read as along its longer side (`dim_labels.dimension_line`), the line
    spanning at least `_SPAN_OVER_TEXT` times the ink's length, and the ink not framed by lines
    on both sides (a tag or a room number in its box)."""
    wide = box[2] - box[0] >= box[3] - box[1]
    font = min(box[2] - box[0], box[3] - box[1])
    length = max(box[2] - box[0], box[3] - box[1])
    if font <= 0 or _framed(box, segments, wide, font):
        return False
    x0, y0, x1, y1 = box[0] - 0.5, box[1] - 0.5, box[2] + 0.5, box[3] + 0.5
    # The ink's own strokes (wholly inside its box) are never its dimension line.
    around = [
        (a, b)
        for a, b in segments
        if not (x0 <= min(a[0], b[0]) and max(a[0], b[0]) <= x1)
        or not (y0 <= min(a[1], b[1]) and max(a[1], b[1]) <= y1)
    ]
    found = dl.dimension_line(box, "h" if wide else "v", around, font, drawn=True)
    if found is None:
        return False
    span = float(found.get("span_pt") or 0.0)
    if span < _SPAN_OVER_TEXT * length:
        return False
    # A dimension's number is written at the middle of its span.
    (ax, ay), (bx, by) = found["dim_line"]
    middle = (ax + bx) / 2 if wide else (ay + by) / 2
    centre = (box[0] + box[2]) / 2 if wide else (box[1] + box[3]) / 2
    return abs(middle - centre) <= max(font, _OFF_MIDDLE * span)


def _number_row(glyphs: Sequence[il.Glyph], box: Box) -> bool:
    """Whether ink is shaped like a written number of two digits or more: longer than
    `_ROW_LENGTH` times its height, at least `_SMALL_PT` high, and its strokes reach across most
    of that height (`_ROW_COVER`: a dashed line's dashes or a hatch do not)."""
    wide = box[2] - box[0] >= box[3] - box[1]
    length = max(box[2] - box[0], box[3] - box[1])
    height = min(box[2] - box[0], box[3] - box[1])
    if height < _SMALL_PT or length < _ROW_LENGTH * height:
        return False
    spans = sorted((g.box[1], g.box[3]) if wide else (g.box[0], g.box[2]) for g in glyphs)
    covered, reach = 0.0, None
    for lo, hi in spans:
        if reach is None or lo > reach:
            covered += hi - lo
            reach = hi
        elif hi > reach:
            covered += hi - reach
            reach = hi
    return covered >= _ROW_COVER * height


def _framed(box: Box, segments: Sequence[Any], wide: bool, font: float) -> bool:
    """Whether lines run along both long sides of the ink, close to it (within a character's
    size): the ink is framed (a tag, a room number in its box), never a dimension's number."""
    lo, hi = (box[0], box[2]) if wide else (box[1], box[3])
    near, far = (box[1], box[3]) if wide else (box[0], box[2])
    before = after = False
    for a, b in segments:
        if wide and abs(a[1] - b[1]) > 0.5 or not wide and abs(a[0] - b[0]) > 0.5:
            continue
        along = (min(a[0], b[0]), max(a[0], b[0])) if wide else (min(a[1], b[1]), max(a[1], b[1]))
        if along[0] > lo + 0.5 or along[1] < hi - 0.5:
            continue  # it does not run the ink's whole length
        across = a[1] if wide else a[0]
        if near - font <= across <= near:
            before = True
        elif far <= across <= far + font:
            after = True
        if before and after:
            return True
    return False


class _Typed:
    """The page's typed characters by box, for "does typed text sit on this ink"."""

    def __init__(self, chars: Sequence[Any]) -> None:
        boxes = [c.box for c in chars if str(getattr(c, "text", "")).strip()]
        self.boxes = np.array(boxes, dtype=float) if boxes else np.zeros((0, 4))

    def over(self, glyphs: Sequence[il.Glyph]) -> bool:
        """Whether more than `_TYPED_SHARE` of the glyphs have their centre inside a typed
        character's box."""
        if not len(self.boxes) or not glyphs:
            return False
        centres = np.array(
            [((g.box[0] + g.box[2]) / 2, (g.box[1] + g.box[3]) / 2) for g in glyphs], dtype=float
        )
        x0, y0 = centres.min(axis=0)
        x1, y1 = centres.max(axis=0)
        b = self.boxes
        near = b[(b[:, 2] >= x0) & (b[:, 0] <= x1) & (b[:, 3] >= y0) & (b[:, 1] <= y1)]
        if not len(near):
            return False
        inside = (
            (centres[:, None, 0] >= near[None, :, 0])
            & (centres[:, None, 0] <= near[None, :, 2])
            & (centres[:, None, 1] >= near[None, :, 1])
            & (centres[:, None, 1] <= near[None, :, 3])
        ).any(axis=1)
        return bool(inside.mean() > _TYPED_SHARE)


def _in_ring(box: Box, rings: np.ndarray) -> bool:
    """Whether the ink lies inside one of the page's closed tag-sized shapes."""
    if not len(rings):
        return False
    s = _RING_SLACK
    return bool(
        (
            (rings[:, 0] <= box[0] + s)
            & (rings[:, 1] <= box[1] + s)
            & (rings[:, 2] >= box[2] - s)
            & (rings[:, 3] >= box[3] - s)
        ).any()
    )


def _straight(glyph: il.Glyph) -> bool:
    """A speck, or one straight stroke (`ink_labels._ends`) maybe drawn twice side by side to
    look bold (every path of it straight and parallel): no digit but a `1` is one."""
    if glyph.size < 0.5:
        return True
    directions = []
    for path in glyph.paths:
        ends = il._ends(il.Glyph((path,), path.box))
        if ends is None:
            return False
        along = ends[1] - ends[0]
        directions.append(along / float(np.linalg.norm(along)))
    return all(abs(il._cross(directions[0], d)) < 0.08 for d in directions[1:])


def _tall(glyph: il.Glyph, turn: il.Turn) -> float:
    """The glyph's height read as `turn`."""
    frame = il._frame(glyph.box, turn)
    return frame[3] - frame[1]


def _bracket(glyph: il.Glyph, turn: il.Turn) -> bool:
    """Whether the glyph has a bracket's straight back: one straight piece of its strokes, along
    its height, at least `_BRACKET_BACK` of it (a `[` has one, a circle's arc has none)."""
    height = _tall(glyph, turn)
    if height <= 0:
        return False
    for path in glyph.paths:
        for line in path.polylines:
            points = [il._point(x, y, turn) for x, y in line]
            for (ax, ay), (bx, by) in pairwise(points):
                if abs(by - ay) >= _BRACKET_BACK * height and abs(bx - ax) <= 0.15 * abs(by - ay):
                    return True
    return False


def _alike(glyphs: Sequence[il.Glyph]) -> bool:
    """Whether the glyphs are one shape repeated (the same size, to a tenth of a point, and the
    same number of points): a pattern (chairs round a table, a fence), never a written value."""
    if len(glyphs) < 2:
        return False
    first = glyphs[0]
    points = [sum(len(line) for p in g.paths for line in p.polylines) for g in glyphs]
    return all(
        abs(g.wide - first.wide) <= 0.1 and abs(g.tall - first.tall) <= 0.1 and n == points[0]
        for g, n in zip(glyphs, points, strict=True)
    )


def _ticks_apart(row: Sequence[il.Token], count: int, turn: il.Turn) -> bool:
    """Whether the two ticks ending the row (its last `count` tokens) stand apart by at least
    `_TICKS_APART` of their length, along the row: an inch mark's two strokes, not one stroke
    drawn twice to look bold."""
    strokes = [
        [il._point(x, y, turn) for x, y in line]
        for token in row[len(row) - count :]
        for path in token.glyphs[0].paths
        for line in path.polylines
        if line
    ]
    if len(strokes) != 2:
        return False
    boxes = [
        (min(x for x, _ in s), min(y for _, y in s), max(x for x, _ in s), max(y for _, y in s))
        for s in strokes
    ]
    length = max(max(b[2] - b[0], b[3] - b[1]) for b in boxes)
    apart = abs((boxes[1][0] + boxes[1][2]) / 2 - (boxes[0][0] + boxes[0][2]) / 2)
    return length > 0 and apart >= _TICKS_APART * length


def _mark_turns(ink: il.Label, typed: _Typed | None = None) -> tuple[il.Turn, ...]:
    """The reading directions in which a row of the ink ends in an inch mark
    (`dim_labels._twin_ticks`) whose ticks stand apart (`_ticks_apart`), the ink holding a glyph
    besides them that is not one straight stroke (`_straight`): a written number's inch mark. A mark the
    page's typed text sits on (`typed`) is that text's, never this ink's. When the glyphs' own
    neighbours (`ink_labels.turns`) agree with some of those directions, only those."""
    found: list[il.Turn] = []
    for turn in (0, 90, 270):
        for row in il.rows(ink, turn):
            count = il.inch_mark_at_end(row, turn)
            if not count or not dl._twin_ticks(row, turn) or not _ticks_apart(row, count, turn):
                continue
            ticks = [g for token in row[len(row) - count :] for g in token.glyphs]
            if typed is not None and typed.over(ticks):
                continue
            if any(not _straight(g) for g in ink.glyphs if not any(g is t for t in ticks)):
                found.append(turn)
                break
    if len(found) > 1:
        agreed = [turn for turn in found if turn in il.turns(ink)]
        return tuple(agreed or found)
    return tuple(found)


def _mm_turn(ink: il.Label) -> il.Turn | None:
    """`dim_labels._mm_rows`' direction when its rows are written values: the millimetres hold a
    glyph that is not one straight stroke (`_straight`), the brackets ending the lower row are
    not straight strokes either (a bracket has corners) but each has a bracket's straight back
    (`_bracket`: a circle's arcs do not), and neither row is one shape repeated (`_alike`: a
    pattern); else `None`."""
    turn = dl._mm_rows(ink)
    if turn is None:
        return None
    rows = il.rows(ink, turn)
    if len(rows) != 2:
        return None
    upper = [g for token in rows[0] for g in token.glyphs]
    lower = [g for token in rows[1] if token.kind != "mark" for g in token.glyphs]
    if not lower or all(_straight(g) for g in upper):
        return None
    if _straight(lower[0]) or _straight(lower[-1]) or _alike(upper) or _alike(lower):
        return None
    if not (_bracket(lower[0], turn) and _bracket(lower[-1], turn)):
        return None
    return turn


def _candidate(
    ink: il.Label,
    side: str,
    near: _Near,
    lines: _Lines,
    stickers: Sequence[Sequence[float]],
    index_of: Mapping[int, int],
    typed: _Typed | None = None,
) -> Candidate:
    """A candidate's flags (`REASON_ORDER`) and, when its strokes do not mark it as a dimension,
    its digit-sized glyphs' shapes. An unmarked one's first reason is decided before "lines
    through", "too small" or "crowded" (it has no inch mark of its own), so those are not looked
    for: the count stays cheap."""
    box = ink.box
    marked_turns = _mark_turns(ink, typed)
    mm = None if marked_turns else _mm_turn(ink)
    marked = bool(marked_turns) or mm is not None
    turns = marked_turns or ((mm,) if mm is not None else il.turns(ink))
    rows = il.rows(ink, turns[0])
    tallest = max(g.size for g in ink.glyphs)
    digit_sized = [g for g in ink.glyphs if g.size >= 0.42 * tallest]
    flags = {
        "gv_sticker": any(_meets(box, s) for s in stickers),
        "mm_row": mm is not None,
        "stacked_fraction": any(token.kind == "fraction" for row in rows for token in row),
        "sideways": all(turn != 0 for turn in turns),
        "no_inch_mark": not marked,
    }
    shapes: tuple[tuple[Shape, ...], ...] = ()
    if marked:
        heights = sorted(
            il._frame(g.box, turns[0])[3] - il._frame(g.box, turns[0])[1] for g in digit_sized
        )
        height = heights[len(heights) // 2] if heights else 0.0
        own = {index_of[id(p)] for g in ink.glyphs for p in g.paths if id(p) in index_of}
        flags["lines_through"] = lines.through(box)
        flags["too_small"] = 0 < height < _SMALL_PT
        flags["crowded"] = _crowded(box, own, near, turns[0])
    elif len(digit_sized) <= _MAX_SHAPED:
        shapes = tuple(
            group for turn in turns[:2] if (group := _digit_shapes(ink, turn, tallest)) is not None
        )
        # Each stacked fraction on its own (a bar with glyphs above and below), whatever other
        # ink touches it: its digits alone.
        shapes += tuple(
            tuple(_shape(g, turn) for g in digits)
            for turn in turns[:2]
            for row in il.rows(ink, turn)
            for token in row
            if token.kind == "fraction"
            and (digits := [g for g in (*token.glyphs, *token.below) if not _straight(g)])
            and all(_tall(g, turn) >= _FRACTION_DIGIT_PT for g in digits)
        )
    reasons = tuple(r for r in REASON_ORDER if flags.get(r)) or ("other",)
    x0, y0, x1, y1 = (round(v, 2) for v in box)
    shaped = sum(1 for g in ink.glyphs if not _straight(g))
    return Candidate((x0, y0, x1, y1), side, marked, reasons, shapes, shaped=shaped)


def _digit_shapes(ink: il.Label, turn: il.Turn, tallest: float) -> tuple[Shape, ...] | None:
    """The shapes the alphabet test matches, read as `turn`: every glyph of its rows (a stacked
    fraction's numerator and denominator, never its bar; never a mark) that is not one straight
    stroke. A straight stroke standing upright (a `1`) or smaller than a digit tells nothing; one
    lying flat or slanting at a digit's size is a letter's stroke (a `T`'s top, an `A`'s leg) or a
    dash: `None`, no number."""
    out: list[Shape] = []
    for row in il.rows(ink, turn):
        for token in row:
            if token.kind == "mark":
                continue
            parts = (*token.glyphs, *token.below) if token.kind == "fraction" else token.glyphs
            for glyph in parts:
                if not _straight(glyph):
                    out.append(_shape(glyph, turn))
                    continue
                frame = il._frame(glyph.box, turn)
                wide, tall = frame[2] - frame[0], frame[3] - frame[1]
                if glyph.size >= 0.42 * tallest and wide > _UPRIGHT * tall:
                    return None
    return tuple(out)


def _meets(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def unread_spots(
    pages: Mapping[tuple[str, int], PageInk | None],
) -> dict[tuple[str, int], list[dict[str, Any]] | None]:
    """Each counted page's candidates that look like a printed number (`looks_like_number`, with
    the alphabet learned from every page's read drawn labels), as `{box, side, reasons}`; a page
    the drawn pass did not reach stays `None`."""
    alphabet = alphabet_of([page for page in pages.values() if page is not None])
    return {
        key: (
            None
            if page is None
            else [
                {"box": list(c.box), "side": c.side, "reasons": list(c.reasons)}
                for c in page.candidates
                if looks_like_number(c, alphabet)
            ]
        )
        for key, page in pages.items()
    }
