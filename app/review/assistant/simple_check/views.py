"""The titled views of one architect sheet, its own sheet number, and the plan callouts on it. Code,
$0, from the set pack's printed phrases (`dims.json`).

An architect's sheet of a separate upload is one whole-page drawing, but it often holds ten
elevations of different pieces ("1 STUDY ELEVATION" ... "10 PANTRY 4"). Paired
as one item, a vendor drawing is compared with the whole sheet, and code's evidence for the sheet is
every view's words and sizes together. So each titled view is its own item (`find_views`):

- **A view title** is printed the architect's way: a view-number bubble (`5`) just left of the
  title (`STAFF ROOM ELEVATION`), with the sheet number under the number (`IN 7.1`) or the
  scale under the title (`1/4" = 1'-0"`). The words printed on the same line right after it are
  part of the title (`HALL ELEVATION @` + `WAITING AREA`): the full title is kept. A sheet's
  title block word (`ELEVATIONS`) has no bubble and is no view.
- **What belongs to a view** (`owner`): its drawing sits above its title. A point goes to a title
  below it, not far past the nearest one (`SLACK_PT`), whose bubble is the last one left of the
  point (`LEFT_REACH_PT`). A point below every title belongs to no view.
- **The sheet's own number** (`sheet_number`): printed large in its title block (`IN 7.1`), or,
  when that is not read, the number most of its view bubbles carry. Written with or without a space
  (`IN 7.0` is `IN7.0`).
- **Callouts on a plan** (`callouts`): a sheet number (`IN7.0`, or `IN` over `7.0`) with view
  numbers round it, not beside a view title: "view 5 of sheet IN7.0 looks at this spot".

Pure: plain values in and out. The work list (`worklist.py`) uses it for the architect's separate
file only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "LEFT_REACH_PT",
    "ROW_PT",
    "SLACK_PT",
    "Callout",
    "View",
    "callouts",
    "find_views",
    "gap",
    "is_scale",
    "normal_sheet",
    "owner",
    "phrases_of",
    "sheet_number",
    "title_block_left",
]

type Box = tuple[float, float, float, float]
type Phrase = tuple[str, Box]

#: Titles whose tops are this close (points) are one row of views.
ROW_PT: Final = 30.0
#: A view's dimensions and tags may reach this far left of its number bubble (points).
LEFT_REACH_PT: Final = 60.0
#: A point belongs to a title at most this much further down than the nearest title below it.
SLACK_PT: Final = 250.0
#: A view-number bubble: one or two digits, maybe a letter (`5`, `10`, `3A`).
_NUMBER: Final = re.compile(r"\d{1,2}[A-Z]?")
#: A sheet number: letters and a number (`IN7.0`, `IN 7.1`, `A-501`, `A13.02`).
_SHEET: Final = re.compile(r"([A-Z]{1,3})\s?-?\s?(\d{1,3}(?:\.\d{1,2})?[A-Z]?)")
#: A sheet's number part printed on its own line under its letters (`IN` over `7.0`).
_SHEET_DIGITS: Final = re.compile(r"\d{1,2}\.\d{1,2}")
#: A printed scale (`1/4" = 1'-0"`, `SCALE: 3/8" = 1'-0"`).
_SCALE: Final = re.compile(r"=\s*1\s*'")
_LETTERS: Final = re.compile(r"[A-Za-z]")
#: A bubble sits this close left of its title (points, between the bubble's right and the title's
#: left), its middle within the title's height.
_BUBBLE_GAP_PT: Final = 45.0
#: The sheet number under a bubble, or the scale under a title, starts within this (points).
_UNDER_PT: Final = 28.0
#: The words after a title on its line join it within this gap (points).
_JOIN_GAP_PT: Final = 25.0
#: A callout's view numbers sit within this of its sheet number (points).
_CALLOUT_PT: Final = 28.0
#: A title block's sheet number is printed at least this many times the usual phrase's height.
_BIG: Final = 2.5
#: A title block down the right side holds its sheet number in the sheet's right fifth; it starts
#: a little left of that number.
_BLOCK_SHARE: Final = 0.2
_BLOCK_PT: Final = 40.0
_KINDS: Final = (
    ("detail", re.compile(r"\b(?:DETAILS?|SECTIONS?)\b")),
    ("plan", re.compile(r"\b(?:PLANS?|RCP)\b")),
    ("elevation", re.compile(r"\b(?:ELEVATIONS?|ELEV\.?)\b")),
    ("detail", re.compile(r"\bENLARGED\b")),
)


def _box(value: object) -> Box | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _centre(box: Box) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _inside(point: tuple[float, float], box: Box) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _plain(text: object) -> str:
    return " ".join(str(text or "").split())


def normal_sheet(text: str) -> str | None:
    """A printed sheet number normalised (`IN 7.0`, `IN-7.0` and `IN7.0` are `IN7.0`), or `None`
    when the text is not one."""
    found = _SHEET.fullmatch(_plain(text).upper())
    if found is None:
        return None
    return f"{found.group(1)}{found.group(2)}"


def phrases_of(dims_page: Mapping[str, Any] | None, within: Box | None = None) -> list[Phrase]:
    """A page's printed phrases (`dims.json`): text and box, those inside `within` when given."""
    out: list[Phrase] = []
    for phrase in (dims_page or {}).get("phrases") or []:
        if not isinstance(phrase, Mapping):
            continue
        box = _box(phrase.get("box"))
        text = _plain(phrase.get("text"))
        if box is None or not text:
            continue
        if within is not None and not _inside(_centre(box), within):
            continue
        out.append((text, box))
    return out


@dataclass(frozen=True, slots=True)
class View:
    """One titled view of an architect sheet."""

    title: str
    """Its full title as printed (`HALL ELEVATION @ WAITING AREA`)."""
    number: str | None
    """The number in its bubble (`7`)."""
    sheet: str | None
    """The sheet number under its bubble, normalised (`IN7.0`)."""
    title_box: Box
    """The title with its bubble and the sheet number under it."""
    kind: str | None
    """`plan` (a reflected ceiling plan, `RCP`, too), `elevation` or `detail` (a section, or an
    enlarged view that is no plan, counts as a detail), from its title; `None` when the title
    names none (`PANTRY 1`)."""


def _kind(title: str) -> str | None:
    upper = title.upper()
    for kind, pattern in _KINDS:
        if pattern.search(upper):
            return kind
    return None


def _height(box: Box) -> float:
    return box[3] - box[1]


def _bubble_for(title: Box, phrases: Sequence[Phrase]) -> Phrase | None:
    """The view-number bubble just left of a title, its middle within the title's height."""
    best: tuple[float, Phrase] | None = None
    middle = _centre(title)[1]
    reach = max(_height(title), 12.0)
    for text, box in phrases:
        if _NUMBER.fullmatch(text) is None:
            continue
        gap = title[0] - box[2]
        if not -4.0 <= gap <= _BUBBLE_GAP_PT or abs(_centre(box)[1] - middle) > reach:
            continue
        if best is None or gap < best[0]:
            best = (gap, (text, box))
    return best[1] if best else None


def _sheet_under(bubble: Box, phrases: Sequence[Phrase]) -> tuple[str, Box] | None:
    """The sheet number printed under a view-number bubble (`IN 7.0`, or `IN` over `7.0`)."""
    cx = _centre(bubble)[0]
    found: list[tuple[float, str, Box]] = []
    for text, box in phrases:
        gap = box[1] - bubble[3]
        if not -2.0 <= gap <= _UNDER_PT or abs(_centre(box)[0] - cx) > 30.0:
            continue
        sheet = normal_sheet(text)
        if sheet is not None and not _NUMBER.fullmatch(text):
            found.append((gap, sheet, box))
    if found:
        _gap, sheet, box = min(found)
        return sheet, box
    return None


def _scale_under(title: Box, phrases: Sequence[Phrase]) -> bool:
    for text, box in phrases:
        if _SCALE.search(text) is None:
            continue
        if -2.0 <= box[1] - title[3] <= _UNDER_PT and -15.0 <= box[0] - title[0] <= 80.0:
            return True
    return False


def is_scale(text: str) -> bool:
    """Whether a printed phrase is a scale (`1/4" = 1'-0"`)."""
    return _SCALE.search(text) is not None


def find_views(phrases: Sequence[Phrase]) -> tuple[View, ...]:
    """The titled views among a sheet's phrases, in reading order (rows top to bottom, each row
    left to right). See the module for what a view title is."""
    used: set[int] = set()
    views: list[View] = []
    order = sorted(range(len(phrases)), key=lambda n: (phrases[n][1][1], phrases[n][1][0]))
    for n in order:
        text, box = phrases[n]
        if n in used or len(_LETTERS.findall(text)) < 3 or _SCALE.search(text):
            continue
        if normal_sheet(text) is not None:
            continue
        bubble = _bubble_for(box, phrases)
        if bubble is None:
            continue
        sheet = _sheet_under(bubble[1], phrases)
        if sheet is None and not _scale_under(box, phrases):
            continue
        # The words printed right after the title on its line belong to it.
        words, end = [text], box
        while True:
            nxt = next(
                (
                    m
                    for m in order
                    if m != n
                    and m not in used
                    and abs(phrases[m][1][1] - end[1]) <= 4.0
                    and 0.0 <= phrases[m][1][0] - end[2] <= _JOIN_GAP_PT
                    and _LETTERS.search(phrases[m][0])
                ),
                None,
            )
            if nxt is None:
                break
            used.add(nxt)
            words.append(phrases[nxt][0])
            end = phrases[nxt][1]
        used.add(n)
        title = " ".join(words)
        parts = [bubble[1], box, end, *((sheet[1],) if sheet else ())]
        title_box = (
            min(b[0] for b in parts),
            min(b[1] for b in parts),
            max(b[2] for b in parts),
            max(b[3] for b in parts),
        )
        views.append(
            View(
                title=title,
                number=bubble[0],
                sheet=sheet[0] if sheet else None,
                title_box=title_box,
                kind=_kind(title),
            )
        )
    return tuple(sorted(views, key=lambda v: (_row_key(v, views), v.title_box[0])))


def _row_key(view: View, views: Sequence[View]) -> float:
    """The top of the first title of the view's row (titles within `ROW_PT` of each other)."""
    top = view.title_box[1]
    return min(v.title_box[1] for v in views if abs(v.title_box[1] - top) <= ROW_PT)


def owner(point: tuple[float, float], views: Sequence[View]) -> int | None:
    """The index of the view a printed point belongs to (its drawing sits above its title): among
    the titles below the point, no further down than `SLACK_PT` past the nearest one, the title
    whose bubble is the last one left of the point (within `LEFT_REACH_PT`), else the leftmost of
    them. Views of a sheet are not always in rows (a short view beside a tall one), so the nearest
    title below alone would give a tall view's top to the short view under its neighbour. `None`:
    below every title."""
    x, y = point
    below = [(v.title_box[1] - y, n) for n, v in enumerate(views) if v.title_box[1] >= y - 4.0]
    if not below:
        return None
    nearest = min(dy for dy, _n in below)
    near = sorted(
        (n for dy, n in below if dy <= nearest + SLACK_PT), key=lambda n: views[n].title_box[0]
    )
    left = [n for n in near if views[n].title_box[0] <= x + LEFT_REACH_PT]
    if left:
        # The last bubble left of the point; on a tie of columns, the nearer title.
        column = views[left[-1]].title_box[0]
        same = [n for n in left if abs(views[n].title_box[0] - column) <= ROW_PT]
        return min(same, key=lambda n: views[n].title_box[1])
    return near[0]


def _big_sheet(phrases: Sequence[Phrase]) -> tuple[str, Box] | None:
    """The sheet number printed largest on the sheet (its title block): at least `_BIG` times the
    usual phrase's height."""
    heights = sorted(_height(box) for _text, box in phrases if _height(box) > 0)
    usual = heights[len(heights) // 2] if heights else 0.0
    big = [
        (_height(box), sheet, box)
        for text, box in phrases
        if (sheet := normal_sheet(text)) is not None
        and not _NUMBER.fullmatch(text)
        and usual > 0
        and _height(box) >= _BIG * usual
    ]
    if not big:
        return None
    _h, sheet, box = max(big)
    return sheet, box


def sheet_number(phrases: Sequence[Phrase], views: Sequence[View] = ()) -> str | None:
    """The sheet's own number: the sheet number printed largest (its title block, at least `_BIG`
    times the usual phrase's height), else the one most of its view bubbles carry."""
    found = _big_sheet(phrases)
    if found is not None:
        return found[0]
    counted = Counter(v.sheet for v in views if v.sheet)
    if counted:
        sheet, count = counted.most_common(1)[0]
        if count * 2 > len(views):
            return sheet
    return None


def title_block_left(phrases: Sequence[Phrase]) -> float | None:
    """Where the sheet's title block starts, when it runs down the sheet's right side (its large
    sheet number in the right `_BLOCK_SHARE` of the sheet): nothing right of it is in a view.
    `None`: not found (or the title block runs along the bottom, below every view title)."""
    found = _big_sheet(phrases)
    if found is None or not phrases:
        return None
    left = min(box[0] for _t, box in phrases)
    right = max(box[2] for _t, box in phrases)
    box = found[1]
    if right <= left or box[0] < left + (1 - _BLOCK_SHARE) * (right - left):
        return None
    return box[0] - _BLOCK_PT


@dataclass(frozen=True, slots=True)
class Callout:
    """A callout printed on a plan: the views of one sheet that look at the spot."""

    sheet: str
    """The sheet number, normalised (`IN7.1`)."""
    numbers: tuple[str, ...]
    """The view numbers round it (one, or four for a four-way callout)."""
    box: Box


def callouts(
    phrases: Sequence[Phrase], views: Sequence[View] = (), sheets: Collection[str] | None = None
) -> tuple[Callout, ...]:
    """The callouts on a plan (see the module): a sheet number (`IN7.0`, or `IN` printed over
    `7.0`) with view numbers within `_CALLOUT_PT` of it; a view title's own bubble is no callout.
    `sheets`: the sheet numbers the set's pages carry (normalised); a mark naming another (a tag
    such as `PA-505` beside a number) is no callout."""
    taken = [v.title_box for v in views]
    marks: list[tuple[str, Box]] = []
    for text, box in phrases:
        if any(_inside(_centre(box), t) for t in taken):
            continue
        sheet = normal_sheet(text)
        if sheet is not None and not _NUMBER.fullmatch(text) and not _SHEET_DIGITS.fullmatch(text):
            marks.append((sheet, box))
            continue
        if re.fullmatch(r"[A-Z]{1,3}", text):
            # The letters over the number (`IN` over `7.0`).
            cx = _centre(box)[0]
            under = [
                (b[1] - box[3], digits, b)
                for digits, b in phrases
                if _SHEET_DIGITS.fullmatch(digits)
                and -2.0 <= b[1] - box[3] <= 12.0
                and abs(_centre(b)[0] - cx) <= 12.0
            ]
            if under:
                _dist, digits, b = min(under)
                marks.append(
                    (
                        f"{text}{digits}",
                        (min(box[0], b[0]), box[1], max(box[2], b[2]), b[3]),
                    )
                )
    out: list[Callout] = []
    for sheet, box in marks:
        if sheets is not None and sheet not in sheets:
            continue
        numbers = sorted(
            {
                text
                for text, b in phrases
                if re.fullmatch(r"\d{1,2}", text)
                and _gap(box, b) <= _CALLOUT_PT
                and not _inside(_centre(b), box)
            },
            key=lambda t: int(t),
        )
        if numbers:
            out.append(Callout(sheet=sheet, numbers=tuple(numbers), box=box))
    return tuple(out)


def _gap(a: Box, b: Box) -> float:
    """The distance between two boxes' edges (0 when they touch or overlap)."""
    dx = max(a[0] - b[2], 0.0, b[0] - a[2])
    dy = max(a[1] - b[3], 0.0, b[1] - a[3])
    return math.hypot(dx, dy)


def gap(a: Box, b: Box) -> float:
    """The distance between two boxes' edges, in points (0 when they touch or overlap)."""
    return _gap(a, b)


def boxes_of(items: Iterable[Mapping[str, Any]]) -> list[Box]:
    """The boxes of printed things (`{"box": [...]}`), the readable ones."""
    return [b for one in items if (b := _box(one.get("box"))) is not None]
