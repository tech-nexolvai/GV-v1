"""Hand-lettered dimensions written on one line: feet and inches, inline fractions (#1662).

Some architects letter their dimensions in a CAD hand-lettering font, exported to the PDF as pen
strokes (`2'-6 1/2"`, `3'-10"`, `5 3/8"`). `ink_labels` was made for the vendor's lettering, and
this kind breaks its grouping: a character is several strokes that only touch (a `3` of two arcs,
a bold stroke drawn twice), a zero carries a slash, a fraction is written inline with a slanted
`/`, and the foot mark and dash stand a letter space apart (`2'` and `-6 1/2"` fall apart). This
module finds such dimensions on the vectors alone; `dim_labels` has the OCR engines name the digits.

1. **Characters.** Strokes that overlap along the reading direction (by half the narrower one)
   and overlap or touch across it are one character.
2. **Rows.** Two characters of a size whose bands overlap are neighbours a small gap apart (a
   wider gap when only smaller marks lie between them: `2 ' - 6`); a smaller mark is a neighbour
   of a taller character it sits beside, inside its band.
3. **Roles,** from each character's own vectors against the row's letter height: a digit; a `1`
   (one straight upright stroke, full height); a slashed zero (an oval and a straight stroke
   across it); a slash (one straight slanted stroke, full height); a dash (flat, mid-height); a
   tick (short and at the top, up to a quarter of a digit above it: straight, or one small path
   of a few points, a filled wedge when a CAD set plots TrueType text as outlines or a bent
   stroke in an SHX stroke font).
   Two twin ticks at the row's end are an inch mark, a single tick a foot mark. A **diagonal
   fraction** (a numerator raised before a long slash, a denominator lowered after it, as CAD
   fonts print `1/8`) is one character that knows its parts (`_diagonal`), only when the vectors
   show it unambiguously; it spells a fraction like an inline one.
4. **A dimension or nothing.** The roles must spell feet and inches with an optional inline
   fraction (`F'-I N/D"`), inches with an optional fraction (`I N/D"`) or a fraction alone
   (`N/D"`), and end in an inch mark. A space between two digits (more than 0.45 of the letter
   height) parts the whole inches from the numerator. The row reads in exactly one direction.
5. **A picture for OCR** (`tokens`): the row turned upright, each character at its own place (a
   diagonal fraction moved onto the line: numerator, a drawn `/`, denominator, row-tall). A
   `1` the vectors know is drawn with a short flag (a bare upright stroke is no `1` to Tesseract),
   and a slashed zero without its slash.
6. **The value** (`value`): an OCR line's digits are laid out as the vectors say (feet, inches,
   numerator, denominator). A reading is no reading of this label when its count of digits is
   not the vectors', when it names a digit the vectors know otherwise (a `1` or a `0`), or when it
   reads a `1` where the vectors show a wide character (a hand-lettered `7`: both engines read
   this font's `7` as `1`). The value needs inches under 12 after feet, no leading zero, a
   fraction a drawing prints (`verify_value._fraction_ok`), and at most 600".
7. **Chains and notes.** Two dimensions of a chain lettered end to end on one line are each tried
   alone (cut after an inch mark that more characters follow). A number with an `=` or a `:`
   beside it in its row (`SCALE: 1" = 1'-0"`) is part of a note (`signs_beside`), never a label;
   nor is the end of a fraction the vectors left apart (`in_fraction`: a digit's ink stacked a
   hair above or below its first digit, or a slash just before it).

Nothing here reads a value: `dim_labels` shows the pictures to two OCR engines and keeps a label
only when both read the same value and every valid reading of either agrees.

Source: issue #1662 (local study of a real architect set); outlined lettering, diagonal fractions,
chains and notes: final-plan change #5 (a real upload's reference set, read sideways) ·
Verification: `tests/review/assistant/test_hand_labels.py`,
`tests/review/assistant/test_outlined_labels.py`
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final

import numpy as np

from app.review.assistant import ink_labels
from app.review.assistant import verify_value as vv

__all__ = ["Lettered", "in_fraction", "lettered", "signs_beside", "text_of", "tokens", "value"]

type Box = tuple[float, float, float, float]
type Turn = ink_labels.Turn

#: Two strokes of one character overlap along the row by this share of the narrower one.
_SAME_CHARACTER: Final = 0.5
#: ... and lie at most this far apart across it (points; a bold stroke drawn twice).
_ACROSS_PT: Final = 0.35
#: Neighbours of a size: heights within this ratio; a gap of at most this many letter heights
#: (`_WIDE_GAP` when only smaller marks lie between them).
_SIZE_RATIO: Final = 0.7
_GAP: Final = 0.9
_WIDE_GAP: Final = 1.6
#: A mark is smaller than this share of the character it sits beside.
_MARK_RATIO: Final = 0.6
#: A gap between two digits wider than this share of the letter height is a space.
_SPACE: Final = 0.45
#: A straight stroke leaning more than this (width over height) is a slash, less a `1`.
_SLANT: Final = 0.2
#: Characters further apart than this along the row are never neighbours (points).
_REACH_PT: Final = 12.0
#: Letters smaller than this (points) are specks or line work, no lettering.
_MIN_LETTER_PT: Final = 1.0
#: A diagonal fraction: its slash spans at least this share of the character's height; each part
#: is a quarter to seven tenths of the slash's height, and keeps to its side of the slash's middle
#: (within this share of its height).
_SLASH_SHARE: Final = 0.75
_PART_MIN: Final = 0.25
_PART_MAX: Final = 0.7
_PART_SLACK: Final = 0.15
#: How far along the row (letter heights) an `=` or `:` beside a dimension is looked for.
_SIGN_REACH: Final = 2.0
#: A character of more paths than this is hatching or a detail's line work.
_MAX_CHAR_PATHS: Final = 8
#: A mark may stand this share of its digit's height above it (a CAD font's tick), and still be
#: part of the row.
_MARK_ABOVE: Final = 0.25
#: A tick drawn as one small path (a filled wedge, a short bent stroke) has at most this many
#: points.
_WEDGE_POINTS: Final = 10
#: A stacked fraction's other part stands at most this share of a letter above or below its digit
#: (`in_fraction`).
_STACK_GAP: Final = 0.35
#: The largest value read (inches), as `dim_labels`.
_MAX_IN: Final = Decimal(600)

#: The roles a dimension may spell: `d` digit, `_` space between digits, `F` foot mark, `M` dash,
#: `S` slash, `I` inch mark.
_DIMENSION: Final = re.compile(
    r"^(?:(?P<feet>d{1,2})FM*(?P<inch>d{1,2})(?:_(?P<top>d{1,2})S(?P<bottom>d{1,2}))?"
    r"|(?P<inches>d{1,3})(?:_(?P<top2>d{1,2})S(?P<bottom2>d{1,2}))?"
    r"|(?P<top3>d{1,2})S(?P<bottom3>d{1,2}))I$"
)


@dataclass(frozen=True, slots=True)
class _Char:
    paths: tuple[ink_labels.InkPath, ...]
    frame: Box
    """`(along start, top, along end, bottom)` in the reading frame."""
    fraction: tuple[tuple[_Char, ...], tuple[_Char, ...]] | None = None
    """A diagonal fraction (`_diagonal`): its numerator's and its denominator's digits; the
    character's paths hold them and the slash between them."""

    @property
    def wide(self) -> float:
        return self.frame[2] - self.frame[0]

    @property
    def tall(self) -> float:
        return self.frame[3] - self.frame[1]


@dataclass(frozen=True, slots=True)
class Lettered:
    """One hand-lettered dimension found on the vectors."""

    label: ink_labels.Label
    """Its characters' own strokes (for `dim_labels`' room and line checks)."""
    turn: Turn
    chars: tuple[_Char, ...]
    roles: tuple[str, ...]
    layout: tuple[int, int, int, int]
    """How many digits the feet, the inches, the numerator and the denominator have."""
    known: tuple[str | None, ...]
    """Per digit: `1` or `0` when the vectors know it, else `None`."""
    ones: tuple[bool, ...]
    """Per digit: whether its strokes may be a `1` (`_may_be_one`)."""


@dataclass(frozen=True, slots=True)
class _Drawn:
    """A stroke drawn for OCR only (an `ink_labels.InkPath`)."""

    box: Box
    polylines: tuple[tuple[tuple[float, float], ...], ...]
    filled: bool = False
    width: float = 0.3


def _frame(box: Box, turn: Turn) -> Box:
    return ink_labels._frame(box, turn)


def _unframe(along: float, across: float, turn: Turn) -> tuple[float, float]:
    """A reading-frame point back on the page (the inverse of `ink_labels._point`)."""
    if turn == 90:
        return across, -along
    if turn == 270:
        return -across, along
    return along, across


def _straight(path: ink_labels.InkPath) -> tuple[np.ndarray, np.ndarray] | None:
    """A path's two ends when all of it lies on one straight line (a stroke drawn there and back
    too), else `None`."""
    points = [np.array(point, float) for line in path.polylines for point in line]
    if len(points) < 2:
        return None
    stack = np.array(points)
    distances = np.sqrt(((stack[:, None, :] - stack[None, :, :]) ** 2).sum(axis=2))
    i, j = np.unravel_index(int(distances.argmax()), distances.shape)
    length = float(distances[i, j])
    if length < 1e-6:
        return None
    start, end = stack[i], stack[j]
    unit = (end - start) / length
    off = np.abs(unit[0] * (stack[:, 1] - start[1]) - unit[1] * (stack[:, 0] - start[0]))
    return (start, end) if float(off.max()) <= 0.06 * length else None


def _characters(paths: Sequence[ink_labels.InkPath], turn: Turn) -> list[_Char]:
    """Strokes overlapping along the row and touching across it, as characters in reading order;
    a diagonal fraction (`_diagonal`) is one character that knows its parts."""
    return [_diagonal(char, turn) or char for char in _grouped(paths, turn)]


def _grouped(paths: Sequence[ink_labels.InkPath], turn: Turn) -> list[_Char]:
    """Strokes overlapping along the row and touching across it, as characters in reading order."""
    items = sorted(((p, _frame(p.box, turn)) for p in paths), key=lambda item: item[1][0])
    sets = ink_labels._Sets(len(items))
    for i, (_a, a) in enumerate(items):
        for j in range(i + 1, len(items)):
            b = items[j][1]
            if b[0] > a[2] + 0.01:
                break
            over = min(a[2], b[2]) - max(a[0], b[0])
            if over < _SAME_CHARACTER * min(a[2] - a[0], b[2] - b[0]) - 0.01:
                continue
            if max(a[1], b[1]) - min(a[3], b[3]) > _ACROSS_PT:
                continue
            sets.join(i, j)
    out = []
    for group in sets.groups():
        frames = [items[i][1] for i in group]
        out.append(
            _Char(
                tuple(items[i][0] for i in group),
                (
                    min(f[0] for f in frames),
                    min(f[1] for f in frames),
                    max(f[2] for f in frames),
                    max(f[3] for f in frames),
                ),
            )
        )
    return sorted(out, key=lambda c: c.frame[0])


def _diagonal(char: _Char, turn: Turn) -> _Char | None:
    """The character as a diagonal fraction (`1/8` printed as a small `1` raised before a slash and
    a small `8` lowered after it, as CAD lettering fonts print fractions), or `None`. The vectors
    must show it unambiguously: one straight stroke rising along the row across the character's
    whole height (the slash), every other stroke wholly above it on its left half (the numerator)
    or wholly below it on its right half (the denominator), each part at most two digits of a
    size (a quarter to seven tenths of the slash's height), the numerator starting first."""
    if len(char.paths) < 3:
        return None
    found: _Char | None = None
    for slash in char.paths:
        ends = _straight(slash)
        if ends is None:
            continue
        (a0, d0), (a1, d1) = (ink_labels._point(float(x), float(y), turn) for x, y in ends)
        if a1 < a0:
            (a0, d0), (a1, d1) = (a1, d1), (a0, d0)
        tall, wide = d0 - d1, a1 - a0
        if tall < _SLASH_SHARE * char.tall or wide < _SLANT * tall:
            continue  # not rising along the row across the whole character
        middle = (d0 + d1) / 2
        upper: list[ink_labels.InkPath] = []
        lower: list[ink_labels.InkPath] = []
        for path in char.paths:
            if path is slash:
                continue
            f = _frame(path.box, turn)
            centre = (f[0] + f[2]) / 2
            at = d0 + (d1 - d0) * (centre - a0) / wide  # the slash's height there
            (upper if (f[1] + f[3]) / 2 < at else lower).append(path)
        if not upper or not lower:
            continue
        top = ink_labels._union([_frame(p.box, turn) for p in upper])
        bottom = ink_labels._union([_frame(p.box, turn) for p in lower])
        if top[3] > middle + _PART_SLACK * tall or bottom[1] < middle - _PART_SLACK * tall:
            continue  # a part reaches across the slash's middle: no fraction's layout
        if top[0] >= bottom[0] or top[2] >= bottom[2]:
            continue  # the numerator must come first along the row
        if not all(
            _PART_MIN * tall <= part[3] - part[1] <= _PART_MAX * tall for part in (top, bottom)
        ):
            continue
        numerator, denominator = _grouped(upper, turn), _grouped(lower, turn)
        if not (1 <= len(numerator) <= 2 and 1 <= len(denominator) <= 2):
            continue
        if found is not None:
            return None  # two ways to read it: ambiguous
        found = _Char(char.paths, char.frame, (tuple(numerator), tuple(denominator)))
    return found


def _rows(chars: Sequence[_Char]) -> list[list[_Char]]:
    """Characters side by side on one row (see the module's step 2), each row in reading order."""
    sets = ink_labels._Sets(len(chars))
    for i, a in enumerate(chars):
        for j in range(i + 1, len(chars)):
            b = chars[j]
            if b.frame[0] > a.frame[2] + _REACH_PT:
                break
            tall = max(a.tall, b.tall, 0.3)
            gap = b.frame[0] - a.frame[2]
            if gap < -0.2 * tall:
                continue
            low = min(a.tall, b.tall)
            if low >= _SIZE_RATIO * tall:
                over = min(a.frame[3], b.frame[3]) - max(a.frame[1], b.frame[1])
                if over < 0.6 * low:
                    continue
                top = min(a.frame[1], b.frame[1]) - 0.2 * tall
                bottom = max(a.frame[3], b.frame[3]) + 0.2 * tall
                marks = any(
                    chars[k].tall < _MARK_RATIO * low
                    and chars[k].frame[0] >= a.frame[2] - 0.1
                    and chars[k].frame[2] <= b.frame[0] + 0.1
                    and chars[k].frame[1] >= top
                    and chars[k].frame[3] <= bottom
                    for k in range(i + 1, j)
                )
                if gap <= _GAP * tall or (marks and gap <= _WIDE_GAP * tall):
                    sets.join(i, j)
            elif low < _MARK_RATIO * tall:
                big, mark = (a, b) if a.tall > b.tall else (b, a)
                inside = (
                    # A CAD font's foot and inch marks may stand a little above its digits.
                    mark.frame[1] >= big.frame[1] - _MARK_ABOVE * big.tall
                    and mark.frame[3] <= big.frame[3] + 0.15 * big.tall
                )
                if inside and gap <= _GAP * big.tall:
                    sets.join(i, j)
    return [sorted((chars[i] for i in group), key=lambda c: c.frame[0]) for group in sets.groups()]


def _wedge(char: _Char) -> bool:
    """Whether a character is one small path of a few points: a tick of a lettering font exported
    as outlines (a small filled wedge: the foot and inch marks of a CAD drawing's TrueType text
    plotted as shapes) or of a CAD stroke font (a short bent stroke: an SHX font's tick). A ring
    (a degree sign, a dot drawn round) has many points and is none."""
    if len(char.paths) != 1:
        return False
    path = char.paths[0]
    return 3 <= sum(len(line) for line in path.polylines) <= _WEDGE_POINTS


def _slashed(char: _Char) -> bool:
    """Whether one straight slanted stroke crosses most of a character of several strokes, with
    strokes on both sides of it: a diagonal fraction's slash between its parts (`1/8` printed
    askew), never one digit (a `7`'s stroke has its bar on one side only). A slashed zero is told
    apart before this (`_zero_slash`)."""
    if len(char.paths) < 3:
        return False
    for path in char.paths:
        ends = _straight(path)
        if ends is None:
            continue
        x0, y0, x1, y1 = path.box
        longer, shorter = max(x1 - x0, y1 - y0), min(x1 - x0, y1 - y0)
        if shorter < _SLANT * longer or longer < 0.75 * max(char.tall, char.wide):
            continue
        (ax, ay), (bx, by) = (float(v) for v in ends[0]), (float(v) for v in ends[1])
        sides = set()
        for other in char.paths:
            if other is path:
                continue
            cx, cy = (other.box[0] + other.box[2]) / 2, (other.box[1] + other.box[3]) / 2
            cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            sides.add(cross > 0)
        if len(sides) == 2:
            return True
    return False


def _zero_slash(char: _Char) -> ink_labels.InkPath | None:
    """The slash of a slashed zero (`Ø`: an oval and one straight stroke across its box), or
    `None`."""
    if len(char.paths) != 2:
        return None
    for slash, oval in (char.paths, char.paths[::-1]):
        ends = _straight(slash)
        if ends is None or _straight(oval) is not None:
            continue
        if sum(len(line) for line in oval.polylines) < 8:
            continue
        x0, y0, x1, y1 = oval.box
        inside = all(x0 - 0.3 <= p[0] <= x1 + 0.3 and y0 - 0.3 <= p[1] <= y1 + 0.3 for p in ends)
        across = abs(float(ends[0][0] - ends[1][0])) > 0.3 * (x1 - x0)
        if inside and across:
            return slash
    return None


def _roles(row: Sequence[_Char]) -> tuple[list[str], float]:
    """Each character's role (`D` digit, `1`, `0`, `S` slash, `M` dash, `T` tick, `?` none) and
    the row's letter height."""
    # A diagonal fraction stands taller than the row's digits (its numerator raised, its
    # denominator lowered): the letter height is the other characters', unless only marks are.
    sized = [c for c in row if c.fraction is None]
    fractions = [c.tall for c in row if c.fraction is not None]
    if not sized or (fractions and max(c.tall for c in sized) < 0.4 * max(fractions)):
        sized = list(row)
    highest = max(c.tall for c in sized)
    letter = float(np.median([c.tall for c in sized if c.tall >= 0.7 * highest]))
    if letter < _MIN_LETTER_PT:
        return ["?"] * len(row), letter
    full = [c.frame[1] for c in sized if c.tall >= 0.75 * letter]
    top = float(np.median(full)) if full else min(c.frame[1] for c in row)
    out = []
    for c in row:
        middle = (c.frame[1] + c.frame[3]) / 2
        if c.fraction is not None:
            out.append("/")
        elif c.tall >= 0.75 * letter:
            ends = _straight(c.paths[0]) if len(c.paths) == 1 else None
            if ends is not None:
                out.append("S" if c.wide / c.tall >= _SLANT else "1")
            elif _zero_slash(c) is not None:
                out.append("0")
            elif _slashed(c):
                out.append("?")  # a slash across other strokes: no digit (a fraction drawn askew)
            elif len(c.paths) > _MAX_CHAR_PATHS:
                out.append("?")  # hatching or a detail's line work, not one character
            else:
                out.append("D")
        elif (
            c.tall <= 0.2 * letter
            and 0.2 * letter <= c.wide <= letter
            and top + 0.3 * letter <= middle <= top + 0.85 * letter
        ):
            out.append("M")
        elif (
            c.tall <= _MARK_RATIO * letter
            and c.wide <= 0.25 * letter
            and c.frame[1] <= top + 0.3 * letter
            and (all(_straight(p) is not None for p in c.paths) or _wedge(c))
        ):
            out.append("T")
        else:
            out.append("?")
    return out, letter


def _spelled(row: Sequence[_Char], roles: Sequence[str], letter: float) -> str | None:
    """The row's roles as a pattern for `_DIMENSION` (two twin ticks at the end are `I`, a single
    tick `F`, a wide gap between two digits `_`), or `None` when a tick is in no place a
    dimension puts one."""
    out: list[str] = []
    index = 0
    while index < len(row):
        role = roles[index]
        if role in "D10":
            if (
                out
                and out[-1] == "d"
                and row[index].frame[0] - row[index - 1].frame[2] >= (_SPACE * letter)
            ):
                out.append("_")
            out.append("d")
        elif role == "/":
            numerator, denominator = row[index].fraction or ((), ())
            if out and out[-1] == "d":
                out.append("_")
            out.append("d" * len(numerator) + "S" + "d" * len(denominator))
        elif role in "SM":
            out.append(role)
        elif role == "T" and index == len(row) - 2 and roles[-1] == "T":
            a, b = row[index], row[index + 1]
            if index and a.frame[0] < row[index - 1].frame[2] - 0.1 * letter:
                return None  # the "mark" stands inside the character before it: line work
            size = max(a.tall, b.tall)
            twins = (
                abs(a.tall - b.tall) <= 0.25 * size
                and abs(a.frame[1] - b.frame[1]) <= 0.25 * size
                and b.frame[0] - a.frame[2] <= 0.6 * letter
            )
            if not twins:
                return None
            out.append("I")
            break
        elif role == "T" and index < len(row) - 1:
            out.append("F")
        else:
            return None
        index += 1
    return "".join(out)


def _may_be_one(char: _Char, turn: Turn) -> bool:
    """Whether a character's strokes may be a `1`: narrow (`verify_value._ONE_MAX_WIDTH`), and no
    flat stretch of stroke across its top longer than a quarter of its height. A hand-lettered `7`
    with an upright stem is a `1` to both OCR engines; its flat top bar tells it apart (a `1`'s
    flag slants down)."""
    if char.wide / max(char.tall, 1e-6) >= vv._ONE_MAX_WIDTH:
        return False
    top = char.frame[1] + 0.3 * char.tall
    for path in char.paths:
        for line in path.polylines:
            points = [ink_labels._point(float(x), float(y), turn) for x, y in line]
            for (a0, d0), (a1, d1) in pairwise(points):
                along, across = abs(a1 - a0), abs(d1 - d0)
                flat = across <= 0.2 * along and along >= 0.25 * char.tall
                if flat and max(d0, d1) <= top:
                    return False
    return True


def _found(row: Sequence[_Char], turn: Turn) -> Lettered | None:
    if len(row) < 3:
        return None
    roles, letter = _roles(row)
    spelled = _spelled(row, roles, letter)
    match = None if spelled is None else _DIMENSION.match(spelled)
    if match is None:
        return None
    groups = match.groupdict()
    feet = len(groups["feet"] or "")
    inches = len(groups["inch"] or groups["inches"] or "")
    top = len(groups["top"] or groups["top2"] or groups["top3"] or "")
    bottom = len(groups["bottom"] or groups["bottom2"] or groups["bottom3"] or "")
    digits: list[tuple[_Char, str]] = []
    for c, r in zip(row, roles, strict=True):
        if r in "D10":
            digits.append((c, r))
        elif r == "/" and c.fraction is not None:
            digits += [(part, "D") for part in (*c.fraction[0], *c.fraction[1])]
    if all(r == "1" for _c, r in digits):
        # Only bare straight strokes: a line's piece with two dashes beside it reads `1"` as
        # well as a lettered one does, so no dimension rests on bare strokes alone.
        return None
    glyphs = tuple(
        ink_labels.Glyph(c.paths, ink_labels._union([p.box for p in c.paths])) for c in row
    )
    return Lettered(
        label=ink_labels.Label(glyphs, ink_labels._union([g.box for g in glyphs])),
        turn=turn,
        chars=tuple(row),
        roles=tuple(roles),
        layout=(feet, inches, top, bottom),
        known=tuple(r if r in "10" else None for _c, r in digits),
        ones=tuple(_may_be_one(c, turn) for c, _r in digits),
    )


def _chain_parts(row: Sequence[_Char]) -> list[list[_Char]]:
    """The row cut after each pair of ticks that two more characters follow (an inch mark ending
    one dimension of a chain), or nothing when there is no such pair."""
    roles, _letter = _roles(row)
    cuts = [
        index + 2
        for index in range(len(row) - 3)
        if roles[index] == roles[index + 1] == "T" and roles[index + 2] not in "TM"
    ]
    if not cuts:
        return []
    bounds = [0, *cuts, len(row)]
    return [list(row[a:b]) for a, b in pairwise(bounds) if b > a]


def lettered(paths: Sequence[ink_labels.InkPath]) -> list[Lettered]:
    """Every hand-lettered dimension among `paths` (character-sized ink), in each reading
    direction. A row that spells a dimension in two directions is dropped (which way it reads is
    not known)."""
    found: list[Lettered] = []
    for turn in (0, 90, 270):
        for row in _rows(_characters(paths, turn)):
            item = _found(row, turn)
            if item is not None:
                found.append(item)
                continue
            # Two dimensions of a chain lettered end to end on one line (`1/8"` then `1 1/2"`):
            # each part that ends in its own inch mark is tried alone.
            for part in _chain_parts(row):
                item = _found(part, turn)
                if item is not None:
                    found.append(item)
    out = []
    for item in found:
        twice = any(
            other is not item
            and other.turn != item.turn
            and _overlap(item.label.box, other.label.box) > 0.5
            for other in found
        )
        if not twice:
            out.append(item)
    return out


def signs_beside(item: Lettered, paths: Sequence[ink_labels.InkPath]) -> bool:
    """Whether two small marks stand one above the other in line with the dimension, within two
    letter heights before or after it: an `=` or a `:` (`SCALE: 1" = 1'-0"`), so the number is part
    of a note, not a dimension. A dimension's own line runs below or beside the lettering, never
    as two short marks in its row."""
    turn = item.turn
    frame = _frame(item.label.box, turn)
    heights = [c.tall for c, r in zip(item.chars, item.roles, strict=True) if r in "D10"]
    letter = float(np.median(heights)) if heights else frame[3] - frame[1]
    reach = _SIGN_REACH * letter
    own = {id(path) for c in item.chars for path in c.paths}
    marks: list[Box] = []
    for path in paths:
        if id(path) in own:
            continue
        f = _frame(path.box, turn)
        if f[1] < frame[1] - 0.1 * letter or f[3] > frame[3] + 0.1 * letter:
            continue  # not in the row
        if not (frame[0] - reach <= f[2] <= frame[0] or frame[2] <= f[0] <= frame[2] + reach):
            continue
        if f[3] - f[1] > 0.35 * letter or f[2] - f[0] > 1.2 * letter:
            continue  # a letter or a line, not a sign's piece
        marks.append(f)
    for index, a in enumerate(marks):
        for b in marks[index + 1 :]:
            over = min(a[2], b[2]) - max(a[0], b[0])
            if over < 0.5 * min(a[2] - a[0], b[2] - b[0]):
                continue
            if a[3] <= b[1] or b[3] <= a[1]:  # one above the other, apart
                return True
    return False


def in_fraction(
    item: Lettered,
    paths: Sequence[ink_labels.InkPath],
    strokes: Sequence[tuple[tuple[float, float], tuple[float, float]]] = (),
) -> bool:
    """Whether the dimension found is only the end of a fraction the vectors did not join to it,
    so its value is not the printed one: a straight slanted stroke as tall as its letters just
    before its first character (the `4"` after the slash of `5'-3 3/4"`), or a digit's ink stacked
    right above or below its first digit (the `1"` over the `4` of a stacked `4'-4 1/4"`).
    A dimension's own tick (one straight stroke) or line (longer than a letter) is neither.
    `strokes`: the page's straight line pieces near it (a slash plotted as a plain line is no
    character-sized path)."""
    turn = item.turn
    frame = _frame(item.label.box, turn)
    heights = [c.tall for c, r in zip(item.chars, item.roles, strict=True) if r in "D10"]
    letter = float(np.median(heights)) if heights else frame[3] - frame[1]
    if letter <= 0:
        return False
    first = next(
        (c for c, r in zip(item.chars, item.roles, strict=True) if r in "D10"), item.chars[0]
    )
    own = {id(path) for c in item.chars for path in c.paths}

    def slash(f: Box) -> bool:
        wide, tall = f[2] - f[0], f[3] - f[1]
        return (
            0.6 * letter <= tall <= 2.5 * letter
            and _SLANT * tall <= wide <= 1.5 * letter
            and frame[1] <= (f[1] + f[3]) / 2 <= frame[3]
            and -0.2 * letter <= first.frame[0] - f[2] <= 0.8 * letter
        )

    for (ax, ay), (bx, by) in strokes:
        if slash(_frame((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)), turn)):
            return True  # a fraction's slash just before it, in the row
    stacked: list[ink_labels.InkPath] = []
    nearest = float("inf")
    for path in paths:
        if id(path) in own:
            continue
        f = _frame(path.box, turn)
        wide, tall = f[2] - f[0], f[3] - f[1]
        if slash(f) and _thin(path):
            return True  # a fraction's slash just before it, in the row
        if wide > 1.5 * letter or tall > 1.5 * letter:
            continue  # line work, not a character
        along = min(f[2], first.frame[2]) - max(f[0], first.frame[0])
        if along < 0.5 * min(wide, first.wide) or tall < 0.15 * letter:
            continue
        # A stacked fraction's other part stands a hair away (CAD fonts: about a sixth of a
        # letter), as tall as the digit; a tag or a symbol near the number stands further off.
        gap = max(f[1] - first.frame[3], first.frame[1] - f[3])
        if 0 <= gap <= 0.6 * letter:
            stacked.append(path)
            nearest = min(nearest, gap)
    if not stacked or nearest > _STACK_GAP * letter:
        return False
    box = ink_labels._union([_frame(p.box, turn) for p in stacked])
    if box[3] - box[1] < 0.8 * letter:
        return False
    # A digit has a bent stroke; a dimension's tick (a short slash, or a thin sliver drawn as a
    # few points, often twice over) and a line's piece are straight.
    return any(not _thin(path) for path in stacked)


def _thin(path: ink_labels.InkPath) -> bool:
    """Whether all of a path lies within a sliver along one straight line: no more than a quarter
    of its length from it (a stroke drawn there and back, a thick tick outlined)."""
    points = [np.array(point, float) for line in path.polylines for point in line]
    if len(points) < 2:
        return True
    stack = np.array(points)
    distances = np.sqrt(((stack[:, None, :] - stack[None, :, :]) ** 2).sum(axis=2))
    i, j = np.unravel_index(int(distances.argmax()), distances.shape)
    length = float(distances[i, j])
    if length < 1e-6:
        return True
    start, end = stack[i], stack[j]
    unit = (end - start) / length
    off = np.abs(unit[0] * (stack[:, 1] - start[1]) - unit[1] * (stack[:, 0] - start[0]))
    return float(off.max()) <= 0.25 * length


def _overlap(a: Box, b: Box) -> float:
    """How much of the smaller box the two share (0 to 1)."""
    wide = min(a[2], b[2]) - max(a[0], b[0])
    tall = min(a[3], b[3]) - max(a[1], b[1])
    if wide <= 0 or tall <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return wide * tall / smaller


def _flagged_one(char: _Char, turn: Turn) -> _Drawn:
    """A `1` the vectors know, drawn with a short flag at its top (as a `1` is printed)."""
    start, top, end, bottom = char.frame
    middle, tall = (start + end) / 2, bottom - top
    strokes = (
        ((middle - 0.22 * tall, top + 0.2 * tall), (middle, top)),
        ((middle, top), (middle, bottom)),
    )
    lines = tuple(tuple(_unframe(a, d, turn) for a, d in stroke) for stroke in strokes)
    xs = [p[0] for line in lines for p in line]
    ys = [p[1] for line in lines for p in line]
    return _Drawn((min(xs), min(ys), max(xs), max(ys)), lines, False, char.paths[0].width)


def tokens(item: Lettered) -> list[ink_labels.Token]:
    """The row as `ink_labels.row_picture` draws it: each character at its own place, a known `1`
    flagged, a slashed zero without its slash, ticks and dashes as marks."""
    out = []
    for char, role in zip(item.chars, item.roles, strict=True):
        paths: list[ink_labels.InkPath] = list(char.paths)
        if role == "1":
            paths = [_flagged_one(char, item.turn)]
        elif role == "0":
            slash = _zero_slash(char)
            paths = [p for p in paths if p is not slash]
        glyph = ink_labels.Glyph(tuple(paths), ink_labels._union([p.box for p in paths]))
        frame = _frame(glyph.box, item.turn)
        if role == "/" and char.fraction is not None:
            # Moved onto the line by `ink_labels.row_picture`: numerator, a drawn `/`, denominator.
            above, below = (
                tuple(
                    ink_labels.Glyph(c.paths, ink_labels._union([p.box for p in c.paths]))
                    for c in part
                )
                for part in char.fraction
            )
            out.append(
                ink_labels.Token("fraction", above, below, frame[0], (frame[1] + frame[3]) / 2)
            )
            continue
        kind: ink_labels.TokenKind = "mark" if role in "TM" else "glyph"
        out.append(ink_labels.Token(kind, (glyph,), (), frame[0], (frame[1] + frame[3]) / 2))
    return out


def value(item: Lettered, line: str) -> Decimal | None:
    """The value an OCR line gives when its digits are laid out as the vectors say, or `None`
    when the line is no reading of this label (see the module's step 6)."""
    digits = "".join(ch for ch in vv._plain(line) if ch.isdigit())
    if len(digits) != sum(item.layout):
        return None
    for digit, known, one in zip(digits, item.known, item.ones, strict=True):
        if known is not None and digit != known:
            return None
        if known is None and digit == "1" and not one:
            return None  # a `1` where the vectors draw something else: a hand-lettered 7
    feet, inches, top, bottom = item.layout
    parts = []
    start = 0
    for size in (feet, inches, top, bottom):
        parts.append(digits[start : start + size])
        start += size
    if any(len(part) > 1 and part.startswith("0") for part in parts):
        return None
    whole = Decimal(int(parts[1])) if parts[1] else Decimal(0)
    if feet:
        if whole >= 12:
            return None
        whole += 12 * int(parts[0])
    if top:
        if not vv._fraction_ok(parts[2], parts[3]):
            return None
        whole += Decimal(int(parts[2])) / Decimal(int(parts[3]))
    if not 0 < whole <= _MAX_IN:
        return None
    return whole


def text_of(item: Lettered, inches: Decimal) -> str:
    """The value written as the drawing letters it (`2'-0 1/2"`, `5 3/8"`, `3/4"`)."""
    if not item.layout[0]:
        return vv._inches_text(inches)
    feet = int(inches // 12)
    rest = inches - 12 * feet
    whole = int(rest)
    fraction = rest - whole
    written = f"{whole}" + ("" if not fraction else " " + vv._inches_text(fraction).rstrip('"'))
    return f"{feet}'-{written}\""
