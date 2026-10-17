"""The vendor's drawn numbers, split into labels and re-typeset onto one line (#1561).

A CAD drawing exported to PDF often turns its lettering into lines: every character is its own
vector path (an inch mark is two ticks, a stacked fraction is a numerator, a bar and a
denominator). OCR reads such a number badly when it sees the whole box at once: a stacked fraction
sits above and below the line, a neighbouring number or a dimension tick gets read with it, and a
number written up the page needs turning. This module uses the vectors instead of a picture:

1. **Glyphs.** Paths that overlap along both axes are one glyph (the hole of an outlined `0`,
   the two strokes of a hand-lettered `4`).
2. **Labels.** Glyphs side by side on one row (or one above the other, for a number written up the
   page) are one label. A dashed line's pieces (straight strokes in a row) and a label made only of
   one or two straight strokes (dimension ticks) are not text.
3. **Reading direction.** A label whose characters are taller than wide reads left to right; one
   whose characters are wider than tall reads up or down the page, and the way that puts its inch
   marks high and its fraction after the whole number wins (both are kept when nothing tells).
4. **Re-typeset.** Each row of a label is redrawn upright on its own picture, at its own geometry,
   with every stacked fraction moved onto the line: numerator, a drawn `/`, denominator, each as
   tall as the row. `63` with `1` over `2` becomes `63 1/2"` on one line, which OCR reads.

Nothing here reads a value: `verify_value` hands these pictures to its OCR engines.

Source: `RESEARCH-pen-strokes.md` (local study, 2026-10-17) · Verification:
`tests/review/assistant/test_ink_labels.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import cv2
import numpy as np

__all__ = [
    "GATHER_PT",
    "HEIGHTS_PX",
    "MAX_LABELS",
    "Glyph",
    "InkPath",
    "Label",
    "Token",
    "inch_mark_at_end",
    "labels",
    "row_picture",
    "rows",
    "turns",
]

type Box = tuple[float, float, float, float]
type Turn = Literal[0, 90, 270]


class InkPath(Protocol):
    """One vector path of the vendor's ink, in shown points (origin top-left)."""

    @property
    def box(self) -> Box: ...

    @property
    def polylines(self) -> tuple[tuple[tuple[float, float], ...], ...]: ...

    @property
    def filled(self) -> bool: ...

    @property
    def width(self) -> float: ...


#: Glyphs at most this far from the box (points) are gathered, so a label the box clips is whole.
GATHER_PT: Final = 12.0
#: A label counts as at the spot when it comes this close to the box (points).
_TOUCH_PT: Final = 3.0
#: A label smaller than this (points) is a speck, not text.
_MIN_LABEL_PT: Final = 0.9
#: The most labels one box may hold: more is a whole view, not one dimension.
MAX_LABELS: Final = 30
#: The re-typeset row's height in pixels, and the white margin round it.
_FRAME_PX: Final = 24


@dataclass(frozen=True, slots=True)
class Glyph:
    """One character's paths (or one mark: a tick, a dash, a fraction bar)."""

    paths: tuple[InkPath, ...]
    box: Box

    @property
    def wide(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def tall(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def size(self) -> float:
        return max(self.wide, self.tall)


@dataclass(frozen=True, slots=True)
class Label:
    """Glyphs that read together: one written number (or word), possibly on two rows."""

    glyphs: tuple[Glyph, ...]
    box: Box


type TokenKind = Literal["glyph", "fraction", "mark"]


@dataclass(frozen=True, slots=True)
class Token:
    """One thing on a row, in reading order: a glyph, a stacked fraction, or a small mark (a tick
    of an inch mark, a dash)."""

    kind: TokenKind
    glyphs: tuple[Glyph, ...]
    """The glyph or mark; for a fraction, the numerator's glyphs."""
    below: tuple[Glyph, ...] = ()
    """A fraction's denominator."""
    along: float = 0.0
    middle: float = 0.0


def _union(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


class _Sets:
    """Union-find over indexes."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def join(self, a: int, b: int) -> None:
        self._parent[self.find(a)] = self.find(b)

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = {}
        for item in range(len(self._parent)):
            out.setdefault(self.find(item), []).append(item)
        return list(out.values())


def _glyphs(paths: Sequence[InkPath]) -> list[Glyph]:
    """Paths that overlap along both axes by half the smaller one's extent, or one lying inside
    the other's box, are one glyph (the hole of an outlined `0`, the bar of a `3`)."""
    sets = _Sets(len(paths))
    for i, a in enumerate(paths):
        for j in range(i + 1, len(paths)):
            b = paths[j].box
            size = max(a.box[2] - a.box[0], a.box[3] - a.box[1], b[2] - b[0], b[3] - b[1])
            slack = 0.05 * size
            if _inside(a.box, b, slack) or _inside(b, a.box, slack):
                sets.join(i, j)
                continue
            floor = 0.12 * size
            over_x = min(a.box[2], b[2]) - max(a.box[0], b[0])
            over_y = min(a.box[3], b[3]) - max(a.box[1], b[1])
            if over_x < -0.02 * size or over_y < -0.02 * size:
                continue
            span_x = max(floor, min(a.box[2] - a.box[0], b[2] - b[0]))
            span_y = max(floor, min(a.box[3] - a.box[1], b[3] - b[1]))
            if over_x >= 0.5 * span_x - 0.02 * size and over_y >= 0.5 * span_y - 0.02 * size:
                sets.join(i, j)
    return [
        Glyph(tuple(paths[i] for i in group), _union([paths[i].box for i in group]))
        for group in sets.groups()
    ]


def _inside(inner: Box, outer: Box, slack: float) -> bool:
    """Whether `inner` lies inside `outer` (with `slack`), and `outer` is a real glyph's box."""
    big = outer[2] - outer[0] > 2 * slack and outer[3] - outer[1] > 2 * slack
    return (
        big
        and inner[0] >= outer[0] - slack
        and inner[2] <= outer[2] + slack
        and inner[1] >= outer[1] - slack
        and inner[3] <= outer[3] + slack
    )


def _ends(glyph: Glyph) -> tuple[np.ndarray, np.ndarray] | None:
    """A glyph's two ends when it is one straight stroke, else `None`."""
    if len(glyph.paths) != 1:
        return None
    points = [point for line in glyph.paths[0].polylines for point in line]
    if len(points) < 2:
        return None
    start, end = np.array(points[0], float), np.array(points[-1], float)
    length = float(np.linalg.norm(end - start))
    if length < 1e-6:
        return None
    direction = (end - start) / length
    off = max(abs(_cross(direction, np.array(p, float) - start)) for p in points)
    return (start, end) if off <= 0.06 * length else None


def _cross(a: np.ndarray, b: np.ndarray) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _dashes(glyphs: Sequence[Glyph]) -> set[int]:
    """Indexes of the pieces of a dashed line (an extension line drawn dashed), never text: at
    least three straight strokes of a similar length, parallel and in one line, each near the
    next. Two in a row are not enough (a `3`'s middle bar beside a fraction bar)."""
    strokes = [(i, ends) for i, g in enumerate(glyphs) if (ends := _ends(g)) is not None]
    partners: dict[int, set[int]] = {}
    for i, (a0, a1) in strokes:
        for j, (b0, b1) in strokes:
            if i >= j:
                continue
            la, lb = float(np.linalg.norm(a1 - a0)), float(np.linalg.norm(b1 - b0))
            if not 0.67 <= la / lb <= 1.5:
                continue
            ua, ub = (a1 - a0) / la, (b1 - b0) / lb
            gap = float(np.linalg.norm((b0 + b1) / 2 - (a0 + a1) / 2))
            longest = max(la, lb)
            if longest * 1.05 < gap <= 4 * longest:
                parallel = abs(_cross(ua, ub)) < 0.08
                in_line = abs(_cross(ua, (b0 + b1) / 2 - (a0 + a1) / 2)) < 0.15 * longest
                if parallel and in_line:
                    partners.setdefault(i, set()).add(j)
                    partners.setdefault(j, set()).add(i)
    out: set[int] = set()
    for i, near in partners.items():
        chain = {i} | near | {k for j in near for k in partners.get(j, set())}
        if len(chain) >= 3:
            out |= chain
    return out


def _rows_of(glyphs: Sequence[Glyph]) -> list[list[Glyph]]:
    """Glyphs side by side on a row (overlapping across, a small gap along), in either direction.
    Two big glyphs on exactly one row may be a word space apart."""
    sets = _Sets(len(glyphs))
    for i, a in enumerate(glyphs):
        for j in range(i + 1, len(glyphs)):
            b = glyphs[j]
            size = max(a.size, b.size)
            if min(a.size, b.size) < 0.12 * size:
                continue
            gap_x = max(a.box[0], b.box[0]) - min(a.box[2], b.box[2])
            gap_y = max(a.box[1], b.box[1]) - min(a.box[3], b.box[3])
            big = min(a.size, b.size) >= 0.6 * size
            low_x, low_y = min(a.wide, b.wide), min(a.tall, b.tall)
            across_rows = -gap_y >= 0.25 * low_y and (
                gap_x <= 0.6 * size or (big and -gap_y >= 0.6 * low_y and gap_x <= 1.1 * size)
            )
            up_the_page = -gap_x >= 0.25 * low_x and (
                gap_y <= 0.6 * size or (big and -gap_x >= 0.6 * low_x and gap_y <= 1.1 * size)
            )
            if across_rows or up_the_page:
                sets.join(i, j)
    return [[glyphs[i] for i in group] for group in sets.groups()]


def _arrowhead(glyph: Glyph) -> bool:
    """A filled triangle or quadrangle (a dimension's arrowhead or a dot): no character is one."""
    return all(
        path.filled and sum(len(line) for line in path.polylines) <= 6 for path in glyph.paths
    )


def _not_text(glyphs: Sequence[Glyph]) -> bool:
    """One or two straight strokes, specks or arrowheads: a dimension tick, a leader's end."""
    return len(glyphs) <= 2 and all(
        _ends(g) is not None or g.size < 0.5 or _arrowhead(g) for g in glyphs
    )


def labels(paths: Sequence[InkPath], spot: Box) -> list[Label]:
    """The labels of the vendor's ink at `spot` (shown points): every label that comes within
    `_TOUCH_PT` of the box, whole even when the box clips it."""
    x0, y0, x1, y1 = spot
    near = [
        p
        for p in paths
        if p.box[0] >= x0 - GATHER_PT
        and p.box[2] <= x1 + GATHER_PT
        and p.box[1] >= y0 - GATHER_PT
        and p.box[3] <= y1 + GATHER_PT
    ]
    glyphs = _glyphs(near)
    dashed = _dashes(glyphs)
    kept = [g for i, g in enumerate(glyphs) if i not in dashed]
    out: list[Label] = []
    for group in _rows_of(kept):
        if max(g.size for g in group) < _MIN_LABEL_PT or _not_text(group):
            continue
        box = _union([g.box for g in group])
        if (
            box[2] < x0 - _TOUCH_PT
            or box[0] > x1 + _TOUCH_PT
            or box[3] < y0 - _TOUCH_PT
            or box[1] > y1 + _TOUCH_PT
        ):
            continue
        group.sort(key=lambda g: (g.box[1], g.box[0]))
        out.append(Label(tuple(group), box))
    out.sort(key=lambda label: (label.box[1], label.box[0]))
    return out


# ---- the reading direction -----------------------------------------------------------------------


def _frame(box: Box, turn: Turn) -> Box:
    """A shown box in the reading frame of `turn` (the picture turned clockwise by `turn`):
    `(along start, top, along end, bottom)`. 90 reads up the page, 270 down it."""
    x0, y0, x1, y1 = box
    if turn == 90:
        return (-y1, x0, -y0, x1)
    if turn == 270:
        return (y0, -x1, y1, -x0)
    return box


def _point(x: float, y: float, turn: Turn) -> tuple[float, float]:
    if turn == 90:
        return -y, x
    if turn == 270:
        return y, -x
    return x, y


def rows(label: Label, turn: Turn) -> list[list[Token]]:
    """The label's rows in reading order, each its tokens in reading order, read as `turn`."""
    placed = [(g, _frame(g.box, turn)) for g in label.glyphs]
    height = max(f[3] - f[1] for _, f in placed)
    used: set[int] = set()
    tokens: list[Token] = []
    for bar, f in placed:
        flat = f[3] - f[1] <= 0.14 * height and 0.18 * height <= f[2] - f[0] <= 1.3 * height
        if not flat:
            continue
        reach = 0.3 * height

        def beside(
            upper: bool, bar: Glyph = bar, f: Box = f, reach: float = reach, taken: set[int] = used
        ) -> list[Glyph]:
            out = []
            for g, e in placed:
                if g is bar or id(g) in taken or e[3] - e[1] < 0.2 * height:
                    continue
                if not (e[0] >= f[0] - reach and e[2] <= f[2] + reach):
                    continue
                # at least half of it over the bar: an inch mark just after it is no numerator
                span = max(e[2] - e[0], 0.05 * height)
                centre = (e[0] + e[2]) / 2
                if min(centre + span / 2, f[2]) - max(centre - span / 2, f[0]) < 0.5 * span:
                    continue
                over = f[1] - 0.4 * height <= e[3] <= f[1] + 0.12 * height
                under = f[3] - 0.12 * height <= e[1] <= f[3] + 0.4 * height
                if (upper and over) or (not upper and under):
                    out.append(g)
            return out

        above, below = beside(True), beside(False)
        if above and below:
            used |= {id(g) for g in (*above, *below, bar)}
            upper = sorted(above, key=lambda g: _frame(g.box, turn)[0])
            lower = sorted(below, key=lambda g: _frame(g.box, turn)[0])
            tokens.append(Token("fraction", tuple(upper), tuple(lower), f[0], (f[1] + f[3]) / 2))
    for g, f in placed:
        if id(g) in used:
            continue
        wide, tall = f[2] - f[0], f[3] - f[1]
        middle = (f[1] + f[3]) / 2
        small = max(wide, tall) <= 0.42 * height and wide < 1.8 * tall
        dash = wide >= 1.8 * tall and tall <= 0.2 * height
        kind: TokenKind = "mark" if small or dash else "glyph"
        tokens.append(Token(kind, (g,), (), f[0], middle))
    lines: list[list[Token]] = []
    middles: list[float] = []
    for token in sorted((t for t in tokens if t.kind != "mark"), key=lambda t: t.middle):
        for index, middle in enumerate(middles):
            if abs(middle - token.middle) <= 0.5 * height:
                lines[index].append(token)
                break
        else:
            lines.append([token])
            middles.append(token.middle)
    for token in tokens:
        if token.kind == "mark" and lines:
            nearest = min(range(len(lines)), key=lambda i: abs(middles[i] - token.middle))
            lines[nearest].append(token)
    order = sorted(range(len(lines)), key=lambda i: middles[i])
    return [sorted(lines[i], key=lambda t: t.along) for i in order]


def _layout_score(label: Label, turn: Turn) -> int:
    """How well `turn` fits how dimensions are written: a fraction after the whole number, inch
    and foot marks high on the line."""
    score = 0
    for row in rows(label, turn):
        mains = [t for t in row if t.kind == "glyph"]
        if not mains:
            continue
        first = mains[0].along
        top = min(_frame(t.glyphs[0].box, turn)[1] for t in mains)
        bottom = max(_frame(t.glyphs[0].box, turn)[3] for t in mains)
        for token in row:
            if token.kind == "fraction":
                score += 1 if token.along > first else -1
            elif token.kind == "mark":
                f = _frame(token.glyphs[0].box, turn)
                if f[2] - f[0] < 1.8 * (f[3] - f[1]):  # a tick, not a dash
                    score += 1 if (f[1] + f[3]) / 2 < (top + bottom) / 2 else -1
    return score


def turns(label: Label) -> tuple[Turn, ...]:
    """The ways the label may read: upright, or up / down the page (the better fitting one, both
    when nothing tells)."""
    tallest = max(g.size for g in label.glyphs)
    big = [g for g in label.glyphs if g.size >= 0.55 * tallest]
    # Neighbours side by side read upright; neighbours stacked read up or down the page. A
    # nearly square glyph (a 9 turned) says little by its own shape, so its neighbours decide.
    upright = sideways = 0
    for i, a in enumerate(big):
        for b in big[i + 1 :]:
            size = max(a.size, b.size)
            gap_x = max(a.box[0], b.box[0]) - min(a.box[2], b.box[2])
            gap_y = max(a.box[1], b.box[1]) - min(a.box[3], b.box[3])
            if -gap_y >= 0.5 * min(a.tall, b.tall) and gap_x <= 0.6 * size:
                upright += 1
            elif -gap_x >= 0.5 * min(a.wide, b.wide) and gap_y <= 0.6 * size:
                sideways += 1
    if upright == sideways:
        upright = sum(g.tall > g.wide * 1.05 for g in big)
        sideways = sum(g.wide > g.tall * 1.05 for g in big)
    if upright > sideways:
        return (0,)
    options: tuple[Turn, ...] = (90, 270) if sideways > upright else (0, 90, 270)
    scores = {turn: _layout_score(label, turn) for turn in options}
    best = max(scores.values())
    return tuple(turn for turn in options if scores[turn] == best)


# ---- the re-typeset picture ----------------------------------------------------------------------


@dataclass(slots=True)
class _Place:
    paths: tuple[InkPath, ...]
    scale: float
    """Pixels per point."""
    origin: tuple[float, float]
    """The frame point drawn at `at`."""
    at: tuple[float, float]
    pen: float
    """Pixels per point for the stroke width: the row's, so a scaled-up fraction is not bolder."""


def _draw(places: Sequence[_Place], turn: Turn, height_px: int) -> np.ndarray | None:
    strokes: list[tuple[list[np.ndarray], int]] = []
    fills: list[list[np.ndarray]] = []
    for place in places:
        for path in place.paths:
            shapes: list[np.ndarray] = []
            for line in path.polylines:
                if not line:
                    continue
                points: list[list[float]] = []
                for x, y in line:
                    a, d = _point(x, y, turn)
                    points.append(
                        [
                            (a - place.origin[0]) * place.scale + place.at[0],
                            (d - place.origin[1]) * place.scale + place.at[1],
                        ]
                    )
                shapes.append(np.round(np.array(points)).astype(np.int32))
            if path.filled and any(len(s) >= 3 for s in shapes):
                fills.append([s for s in shapes if len(s) >= 3])
            else:
                thickness = max(2, round(max(path.width, 0.15) * place.pen))
                strokes.append((shapes, thickness))
    drawn = [s for shapes, _ in strokes for s in shapes] + [s for f in fills for s in f]
    if not drawn:
        return None
    every = np.vstack(drawn)
    width = int(every[:, 0].max()) + 2 * _FRAME_PX
    height = int(max(every[:, 1].max(), height_px)) + 2 * _FRAME_PX
    if width * height > 4_000_000 or every.min() < -_FRAME_PX:
        return None
    picture = np.full((height, width), 255, np.uint8)
    for shapes, thickness in strokes:
        for shape in shapes:
            if len(shape) >= 2:
                cv2.polylines(picture, [shape], False, 0, thickness)
    if fills:
        # One even-odd fill: the hole of an outlined 0 is its own subpath and stays open.
        mask = np.zeros_like(picture)
        for fill in fills:
            for shape in fill:
                one = np.zeros_like(picture)
                cv2.fillPoly(one, [shape], 1)
                mask ^= one
        picture[mask > 0] = 0
    return picture


def inch_mark_at_end(row: Sequence[Token]) -> int:
    """How many tokens at the row's end make an inch mark (two ticks after the last character),
    or 0. A single tick (a foot mark) is never one."""
    end = len(row)
    while end > 0 and row[end - 1].kind == "mark":
        end -= 1
    ticks = [t for t in row[end:] if t.glyphs[0].wide < 1.8 * t.glyphs[0].tall]
    strokes = sum(len(t.glyphs[0].paths) for t in ticks)
    return len(row) - end if end and strokes == 2 and len(ticks) == len(row) - end else 0


def row_picture(
    row: Sequence[Token], turn: Turn, height_px: int = 48, *, inch_mark: bool = True
) -> np.ndarray | None:
    """One row redrawn upright, dark ink on white: glyphs and marks at their own geometry, each
    stacked fraction moved onto the line (numerator, a drawn `/`, denominator, row-tall).
    `inch_mark=False` leaves out a trailing inch mark (an engine that reads `"` as `99`)."""
    if not inch_mark and (drop := inch_mark_at_end(row)):
        row = row[: len(row) - drop]
    mains = [t for t in row if t.kind != "fraction"]
    parts = [g for t in row for g in (*t.glyphs, *t.below)]
    if not parts:
        return None
    if mains:
        boxes = [_frame(t.glyphs[0].box, turn) for t in mains if t.kind == "glyph"] or [
            _frame(t.glyphs[0].box, turn) for t in mains
        ]
    else:
        boxes = [_frame(g.box, turn) for g in parts]
    top, bottom = min(b[1] for b in boxes), max(b[3] for b in boxes)
    scale = height_px / max(bottom - top, 1e-3)
    margin = float(_FRAME_PX)
    cursor = margin
    # Glyphs between fractions keep their true places: `anchor` ties a frame point to a pixel.
    anchor: tuple[float, float] | None = None
    places: list[_Place] = []
    slashes: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for token in row:
        if token.kind == "fraction":
            cursor += 0.7 * height_px
            for index, group in enumerate((token.glyphs, token.below)):
                framed = [_frame(g.box, turn) for g in group]
                start, high = min(b[0] for b in framed), min(b[1] for b in framed)
                end, low = max(b[2] for b in framed), max(b[3] for b in framed)
                part_scale = height_px / max(low - high, 1e-3)
                paths = tuple(p for g in group for p in g.paths)
                places.append(_Place(paths, part_scale, (start, high), (cursor, margin), scale))
                cursor += (end - start) * part_scale + 0.12 * height_px
                if index == 0:
                    cursor += 0.1 * height_px
                    slashes.append(
                        (
                            (int(cursor + 0.36 * height_px), int(margin) + 2),
                            (int(cursor + 0.04 * height_px), int(margin) + height_px - 2),
                        )
                    )
                    cursor += 0.52 * height_px
            anchor = None
            continue
        f = _frame(token.glyphs[0].box, turn)
        if anchor is None:
            anchor = (f[0], cursor)
        at = anchor[1] + (f[0] - anchor[0]) * scale
        places.append(_Place(token.glyphs[0].paths, scale, (f[0], top), (at, margin), scale))
        cursor = max(cursor, at + (f[2] - f[0]) * scale)
    picture = _draw(places, turn, height_px)
    if picture is None:
        return None
    for slash_top, slash_foot in slashes:
        cv2.line(picture, slash_top, slash_foot, 0, max(2, height_px // 14))
    framed_picture: np.ndarray = cv2.copyMakeBorder(
        picture, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=255
    )
    return framed_picture


def angle_of(turn: Turn) -> str:
    """How a reading direction is said in a reading's label."""
    return {0: "upright", 90: "up the page", 270: "down the page"}[turn]


#: The two row heights (pixels) each label is drawn at: two passes per engine.
HEIGHTS_PX: Final = (40, 60)
