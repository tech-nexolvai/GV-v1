"""The vendor's room matrix against the architect's room schedule, by code: $0, no AI (2026-11-04).

A hotel job's vendor often sends a room or vanity matrix: a table of every room by room type, with
the item each room gets (a standard vanity, an accessible one). The architect's cover sheet prints
the room schedule: every room by room type, with an accessible (ADA) mark. A real set's helper
paired the two pages but compared only sizes, so it found "nothing to compare", while many rooms
carried another room type on the two lists. Code reads both tables from the drawings' own
text with positions (`dims.json` phrases) and the table's ruling lines (the pack's line work):

- **A room table** (`read_table`): a page printing a room-type column heading on its own
  (`rooms.TYPE_HEADING`: ROOM TYPE, ROOM DESCRIPTION) and at least `rooms.ROOM_TABLE_MIN` room
  numbers in cells of their own. The heading's column is bounded by the vertical lines round it;
  its rows by the horizontal lines across it. Each room number belongs to the row whose band holds
  it; the row's name is what its room-type cell prints. When the table has two such columns (a
  code column "QQ" and a description column "DOUBLE QUEEN"), the one with the longer names is
  read. A TOTAL column gives each row's printed count; a TOTAL row the table's.
- **The accessible rooms**: a row whose name (or its code cell, or the item group it is in: the
  vendor's "ADA Bathroom Vanity" spanning several rows) prints ADA, ACCESSIBLE, HANDICAP or HC.
- **Which tables**: the vendor's table and the architect's whose room sets overlap most (at least
  `MIN_OVERLAP` of the rooms on either), one each.

It compares (one finding per difference, never one per room):

- (a) **the room set**: rooms on one list and not the other;
- (b) **the accessible rooms**: the rooms the vendor gives its accessible item against the rooms
  the architect marks accessible;
- (c) **the room type of each room** on both lists, names normalised (case, plurals, ADA words,
  common abbreviations, STANDARD / TYPICAL / TYPE left out): one finding per pair of names (the
  vendor's "King" against the architect's "DOUBLE QUEEN"), listing the rooms. "Suite" against
  "Studio" is a difference.

(a) and (b) are a Wrong (status `different`, `text_proof`: the trust rule's `text` witness, its own
kind of check `room_list` in `check_record`, so red only once that kind's record is clean) only
when code read both tables whole: every room number printed as text in a named row, none twice,
every row's count equal to its printed total and the table's printed total equal to the rooms
read. Otherwise, and always for (c), a please-check (the whole-drawing look's mark, `likely_only`:
never clouded). Pure code: JSON and line work in, finding dicts out.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from app.review.assistant.simple_check import rooms as rooms_module
from app.review.assistant.simple_check import views as views_module

__all__ = [
    "CREATED_BY",
    "KIND",
    "MAX_LISTED",
    "MIN_OVERLAP",
    "Row",
    "Table",
    "add_to_answer",
    "check_pack",
    "compare",
    "read_table",
    "type_key",
]

_log = logging.getLogger(__name__)

type Box = tuple[float, float, float, float]
type Point = tuple[float, float]
type Segment = tuple[Point, Point]

#: Who made the findings (the chat finding's `created_by`).
CREATED_BY: Final = "room_matrix"
#: The `text_proof` kind of a room-list Wrong (and its `check_record` kind).
KIND: Final = "room_list"
#: The vendor's table and the architect's are the same job's when the rooms on both are at least
#: this share of the rooms on either.
MIN_OVERLAP: Final = 0.5
#: The rooms a finding's words list, at most (the rest are counted).
MAX_LISTED: Final = 24
#: Lines this close (points) are one line; a line this close to a column's middle crosses it.
_SAME_LINE_PT: Final = 1.5
#: A heading of the total column, printed on its own.
_TOTAL_HEADING: Final = re.compile(r"(?:TOTALS?|QTY|QUANTITY|COUNT)\s*:?")
#: A total row's name (TOTAL, SUB TOTAL, GRAND TOTAL).
_TOTAL_ROW: Final = re.compile(r"\b(?:SUB\s*-?\s*|GRAND\s+)?TOTALS?\b")
#: The item column's heading (the vendor's item code), printed on its own.
_ITEM_HEADING: Final = re.compile(r"ITEMS?\s*(?:CODES?|NO\.?|NUMBERS?|#)?\s*:?")
#: The item description column's heading, printed on its own.
_DESCRIPTION_HEADING: Final = re.compile(r"(?:ITEM\s+)?DESCRIPTIONS?\s*:?")
#: An accessible room or item.
_ACCESSIBLE: Final = re.compile(r"\b(?:ADA|ACCESSIBLE|HANDICAP(?:PED)?|HC)\b")
#: A count printed in a total cell.
_COUNT: Final = re.compile(r"\d{1,4}")
#: Room-type words written short, and their full word (`type_key`).
_WORDS: Final[Mapping[str, str]] = {
    "DBL": "DOUBLE",
    "DBLE": "DOUBLE",
    "TWO": "DOUBLE",
    "QN": "QUEEN",
    "KG": "KING",
    "STD": "STANDARD",
    "EXT": "EXTENDED",
    "EXTD": "EXTENDED",
    "CONN": "CONNECTING",
    "CONNECT": "CONNECTING",
    "CONNECTED": "CONNECTING",
    "STE": "SUITE",
    "STU": "STUDIO",
    "ACCESSIBLE": "ADA",
    "HANDICAP": "ADA",
    "HANDICAPPED": "ADA",
    "HC": "ADA",
    "BDRM": "BEDROOM",
    "BR": "BEDROOM",
}
#: Words that say nothing of a room's type ("STANDARD KING" is a "King").
_FILLER: Final = frozenset({"STANDARD", "TYPICAL", "TYPE", "ROOM", "ROOMS", "GUEST", "GUESTROOM"})


@dataclass(frozen=True, slots=True)
class Row:
    """One room-type row of a room table."""

    name: str
    """What its room-type cell prints."""
    box: Box
    rooms: Mapping[str, Box]
    """Its room numbers, each with its cell, in reading order."""
    total: int | None
    """The count its total cell prints, or `None`."""
    accessible: bool
    """Its name, its code cell or its item group prints ADA (or ACCESSIBLE, HANDICAP, HC)."""


@dataclass(frozen=True, slots=True)
class Table:
    """A room table code read (`read_table`)."""

    file: str
    page: int
    rows: tuple[Row, ...]
    heading: Box
    """The room-type column's heading."""
    total: int | None = None
    """The table's printed total (its TOTAL row's largest count), or `None`."""
    doubts: tuple[str, ...] = ()
    """Why the table is not read whole (none: every room in a named row, none twice, every row's
    count its printed total, the table's total the rooms read)."""

    @property
    def whole(self) -> bool:
        return not self.doubts

    def rooms(self) -> dict[str, Row]:
        return {room: row for row in self.rows for room in row.rooms}

    def where(self) -> str:
        return f"page {self.page}"


# ---- reading one table -----------------------------------------------------------------------


def _plain(text: object) -> str:
    return " ".join(str(text or "").split())


def _upper(text: object) -> str:
    return _plain(text).upper()


def _centre(box: Box) -> Point:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _union(boxes: Iterable[Box]) -> Box:
    found = list(boxes)
    return (
        min(b[0] for b in found),
        min(b[1] for b in found),
        max(b[2] for b in found),
        max(b[3] for b in found),
    )


def _lines(segments: Sequence[Segment]) -> tuple[list[Box], list[Box]]:
    """The page's horizontal and vertical lines, each as a box (x0, y0, x1, y1)."""
    across: list[Box] = []
    down: list[Box] = []
    for (x0, y0), (x1, y1) in segments:
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[3] - box[1] <= 0.5 and box[2] - box[0] > 2:
            across.append(box)
        elif box[2] - box[0] <= 0.5 and box[3] - box[1] > 2:
            down.append(box)
    return across, down


def _column(heading: Box, down: Sequence[Box]) -> tuple[float, float] | None:
    """The column under a heading: between the nearest vertical lines left and right of its
    middle that run from the heading's band down."""
    cx = _centre(heading)[0]
    reach = [d for d in down if d[1] <= heading[3] + 4 and d[3] > heading[3] + 4]
    left = [d[0] for d in reach if d[0] < cx]
    right = [d[0] for d in reach if d[0] > cx]
    if not left or not right:
        return None
    return max(left), min(right)


def _bands(across: Sequence[Box], x: float, top: float) -> list[float]:
    """The heights of the horizontal lines crossing `x` from `top` down, one per line."""
    found = sorted(
        b[1] for b in across if b[0] - _SAME_LINE_PT <= x <= b[2] + _SAME_LINE_PT and b[1] >= top
    )
    out: list[float] = []
    for y in found:
        if not out or y - out[-1] > _SAME_LINE_PT:
            out.append(y)
    return out


def _band_of(y: float, edges: Sequence[float]) -> int | None:
    for n in range(len(edges) - 1):
        if edges[n] < y < edges[n + 1]:
            return n
    return None


def _in(box: Box, column: tuple[float, float]) -> bool:
    return column[0] < _centre(box)[0] < column[1]


@dataclass(slots=True)
class _Column:
    heading: Box
    span: tuple[float, float]
    names: dict[int, list[tuple[str, Box]]] = field(default_factory=dict)


def _cells(
    phrases: Sequence[tuple[str, Box]], span: tuple[float, float], edges: Sequence[float]
) -> dict[int, list[tuple[str, Box]]]:
    """The phrases of one column, by band (in reading order)."""
    out: dict[int, list[tuple[str, Box]]] = {}
    for text, box in sorted(phrases, key=lambda p: (p[1][1], p[1][0])):
        band = _band_of(_centre(box)[1], edges)
        if band is not None and _in(box, span):
            out.setdefault(band, []).append((text, box))
    return out


def _heading(
    phrases: Sequence[tuple[str, Box]], pattern: re.Pattern[str], near: Box, down: Sequence[Box]
) -> tuple[Box, tuple[float, float]] | None:
    """A column heading printed on its own on the heading row of `near` (the room-type heading),
    with its column."""
    for text, box in phrases:
        if pattern.fullmatch(_upper(text)) and box[1] < near[3] + 4 and box[3] > near[1] - 30:
            column = _column(box, down)
            if column is not None:
                return box, column
    return None


def read_table(
    phrases: Sequence[tuple[str, Box]],
    segments: Sequence[Segment] | None,
    file: str,
    page: int,
) -> Table | None:
    """The room table a page prints (see the module), or `None`: no room-type heading, no ruling
    lines round it, or fewer than `rooms.ROOM_TABLE_MIN` room numbers in its rows."""
    if not segments or not rooms_module.table_rooms(text for text, _box in phrases):
        return None
    across, down = _lines(segments)
    columns: list[_Column] = []
    for text, box in phrases:
        if not rooms_module.TYPE_HEADING.fullmatch(_upper(text)):
            continue
        span = _column(box, down)
        if span is None:
            continue
        edges = _bands(across, (span[0] + span[1]) / 2, box[1] - 40)
        if len(edges) < 3:
            continue
        columns.append(_Column(box, span, _cells(phrases, span, edges)))
    if not columns:
        return None
    # Two room-type columns (a code "QQ" and its description "DOUBLE QUEEN"): the longer names.
    names = max(
        columns,
        key=lambda c: sum(len(t) for cells in c.names.values() for t, _b in cells)
        / max(1, sum(len(cells) for cells in c.names.values())),
    )
    heading, span = names.heading, names.span
    edges = _bands(across, (span[0] + span[1]) / 2, heading[1] - 40)
    first = _band_of(_centre(heading)[1], edges)
    if first is None or first + 1 >= len(edges):
        return None
    top = edges[first + 1]
    table_lines = [b for b in across if abs(b[1] - top) <= _SAME_LINE_PT and b[0] <= span[0] + 2]
    left = min((b[0] for b in table_lines), default=span[0])
    right = max((b[2] for b in table_lines), default=span[1])
    # The table's own lines only: a page border or a title block's line is no row of it.
    across = [b for b in across if b[0] >= left - 5 and b[2] <= right + 5]
    edges = _bands(across, (span[0] + span[1]) / 2, heading[1] - 40)
    first = _band_of(_centre(heading)[1], edges)
    if first is None or first + 2 >= len(edges):
        return None
    bottom = edges[-1]
    inside = [
        (text, box)
        for text, box in phrases
        if left <= _centre(box)[0] <= right and top < _centre(box)[1] < bottom
    ]
    labels = _cells(inside, span, edges)
    others = [c for c in columns if c is not names]
    codes = _cells(inside, others[0].span, edges) if others else {}
    total_heading = _heading(phrases, _TOTAL_HEADING, heading, down)
    totals = _cells(inside, total_heading[1], edges) if total_heading else {}
    item = _heading(phrases, _ITEM_HEADING, heading, down)
    described = _heading(phrases, _DESCRIPTION_HEADING, heading, down)
    groups: list[tuple[float, float, str]] = []
    if item is not None:
        item_edges = _bands(across, (item[1][0] + item[1][1]) / 2, heading[1] - 40)
        item_cells = _cells(inside, item[1], item_edges)
        description = _cells(inside, described[1], item_edges) if described else {}
        for band in set(item_cells) | set(description):
            words = " ".join(t for t, _b in [*item_cells.get(band, []), *description.get(band, [])])
            groups.append((item_edges[band], item_edges[band + 1], _upper(words)))
    doubts: list[str] = []
    rows: list[Row] = []
    seen: dict[str, int] = {}
    total_rows: list[int] = []
    for band in range(first + 1, len(edges) - 1):
        name = " ".join(t for t, _b in labels.get(band, []))
        in_band = [
            (text, box)
            for text, box in inside
            if _band_of(_centre(box)[1], edges) == band and _centre(box)[0] > span[1]
        ]
        found: dict[str, Box] = {}
        for text, box in in_band:
            cell = rooms_module.cell_rooms(text)
            if total_heading is not None and _in(box, total_heading[1]):
                continue
            for room in sorted(cell or ()):
                if room in found or room in seen:
                    doubts.append(f"room {room} is printed twice")
                found.setdefault(room, box)
        counts = [int(t) for t, _b in totals.get(band, []) if _COUNT.fullmatch(_plain(t))]
        row_words = " ".join(t for t, b in inside if _band_of(_centre(b)[1], edges) == band)
        if _TOTAL_ROW.search(_upper(name)) or (not name and _TOTAL_ROW.search(_upper(row_words))):
            if found:
                doubts.append(f"a total row prints room numbers ({', '.join(sorted(found))})")
            total_rows.extend(counts)
            continue
        if not name:
            if found:
                doubts.append(f"rooms {', '.join(sorted(found))} are in a row with no room type")
            continue
        mid = (edges[band] + edges[band + 1]) / 2
        group = next((words for y0, y1, words in groups if y0 < mid < y1), "")
        code = " ".join(t for t, _b in codes.get(band, []))
        accessible = bool(
            _ACCESSIBLE.search(_upper(name))
            or _ACCESSIBLE.search(_upper(code))
            or _ACCESSIBLE.search(group)
        )
        total = counts[0] if len(counts) == 1 else None
        if total_heading is None or len(counts) != 1:
            doubts.append(f'the row "{name}" prints no single total')
        elif total != len(found):
            doubts.append(f'the row "{name}" lists {len(found)} rooms but its total is {total}')
        for room in found:
            seen[room] = band
        box = _union([b for _t, b in labels.get(band, [])])
        rows.append(Row(name, box, found, total, accessible))
    loose = [
        room
        for text, box in phrases
        if (cell := rooms_module.cell_rooms(text))
        and not (left <= _centre(box)[0] <= right and top < _centre(box)[1] < bottom)
        and box[1] > heading[1]
        and left - 20 <= _centre(box)[0] <= right + 20
        for room in cell
    ]
    if loose:
        doubts.append(f"rooms {', '.join(sorted(loose)[:6])} are printed outside the table's rows")
    grand = max(total_rows) if total_rows else None
    if grand is None:
        doubts.append("the table prints no total")
    elif grand != len(seen):
        doubts.append(f"the table lists {len(seen)} rooms but its total is {grand}")
    if len(seen) < rooms_module.ROOM_TABLE_MIN:
        return None
    return Table(file, page, tuple(rows), heading, grand, tuple(dict.fromkeys(doubts)))


# ---- comparing two tables --------------------------------------------------------------------


def type_key(name: str) -> tuple[str, ...]:
    """A room type's name for comparing: its words upper case, written out (`_WORDS`), plurals
    made singular, `_FILLER` words left out, in any order ("STANDARD KING" is "King")."""
    words: set[str] = set()
    for word in re.findall(r"[A-Z]+|\d+", _upper(name)):
        word = _WORDS.get(word, word)
        if len(word) > 3 and word.endswith("S") and not word.endswith("SS"):
            word = _WORDS.get(word[:-1], word[:-1])
        if word not in _FILLER:
            words.add(word)
    return tuple(sorted(words))


def _listed(rooms: Sequence[str]) -> str:
    """`room 101` or `rooms 101, 201` (at most `MAX_LISTED`, the rest counted)."""
    found = sorted(rooms, key=lambda r: (len(r), r))
    if len(found) == 1:
        return f"room {found[0]}"
    shown = ", ".join(found[:MAX_LISTED])
    more = len(found) - MAX_LISTED
    return f"rooms {shown} (and {more} more)" if more > 0 else f"rooms {shown}"


def _them(rooms: Sequence[str]) -> str:
    return "it" if len(rooms) == 1 else "them"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _box_dict(box: Box, table: Table) -> dict[str, Any]:
    return {
        "page": table.page,
        "file": table.file,
        "x0": round(box[0], 1),
        "y0": round(box[1], 1),
        "x1": round(box[2], 1),
        "y1": round(box[3], 1),
    }


def _mark(kind: str, words: str, why: str) -> dict[str, Any]:
    from app.review.assistant import likely_problems

    return {
        "by": "whole_drawing_look",
        "proof": "likely_only",
        "kind": kind,
        "claim": "please_check",
        "sure": "likely",
        "why": why[:400],
        "words": f"{likely_problems.LETTER_LEAD}: {words}"[:600],
    }


def _finding(
    vendor: Table,
    architect: Table,
    rooms: Sequence[str],
    *,
    item: str,
    shown: tuple[str, str],
    why: str,
    words: str,
    boxes: tuple[Box, Box],
    also: Sequence[Box] = (),
    proved: str | None = None,
    kind: str = "labels",
) -> dict[str, Any]:
    """One finding: a Wrong carrying `text_proof` when `proved` names what code proved (both
    tables read whole), else a please-check (the look's mark, never clouded)."""
    from app.review.assistant import likely_problems

    finding: dict[str, Any] = {
        "page": vendor.page,
        "item": item[:160],
        "category": "other",
        "vendor": shown[0][:200],
        "architect": shown[1][:200],
        "status": "different" if proved else "check",
        "why": _cap(why)[:600],
        "verified": False,
        "source": "architect",
        "vendor_box": _box_dict(boxes[0], vendor),
        "arch_box": _box_dict(boxes[1], architect),
        "paired_by": "code",
        "created_by": CREATED_BY,
        "also_on": [
            {"page": vendor.page, "vendor": room, "vendor_box": _box_dict(box, vendor)}
            for room, box in list(zip(rooms[1:], also, strict=False))[:12]
        ],
    }
    if proved:
        finding["text_proof"] = {
            "kind": KIND,
            "what": proved,
            "rooms": list(rooms),
            "vendor": {"page": vendor.page, "file": vendor.file, "total": vendor.total},
            "architect": {"page": architect.page, "file": architect.file, "total": architect.total},
        }
    else:
        finding[likely_problems.FIELD] = _mark(kind, words, _cap(why))
    return finding


def _whole(vendor: Table, architect: Table) -> str | None:
    """Why the two tables are not both read whole, else `None`."""
    for table, whose in ((vendor, "the vendor's"), (architect, "the architect's")):
        if table.doubts:
            return f"{whose} table: {table.doubts[0]}"
    return None


def compare(vendor: Table, architect: Table) -> list[dict[str, Any]]:
    """The findings of the vendor's room table against the architect's (see the module)."""
    out: list[dict[str, Any]] = []
    mine, theirs = vendor.rooms(), architect.rooms()
    doubt = _whole(vendor, architect)
    v_where = f"the vendor's room matrix (page {vendor.page})"
    a_where = f"the architect's room schedule (page {architect.page} of its file)"
    read = "read by code from both tables' printed text"
    proof_words = (
        "both tables read whole by code: every room in a named row, every row's count equal to "
        "its printed total"
    )
    held = f" Code could not prove it: {doubt}." if doubt else ""
    # (a) the room set.
    only_vendor = [r for r in mine if r not in theirs]
    only_architect = [r for r in theirs if r not in mine]
    if only_vendor:
        rooms = sorted(only_vendor, key=lambda r: (len(r), r))
        out.append(
            _finding(
                vendor,
                architect,
                rooms,
                item=f"{_cap(_listed(rooms))}: on the vendor's matrix, not on the architect's "
                "schedule",
                shown=(_listed(rooms), "not on the room schedule"),
                why=f"{v_where} lists {_listed(rooms)}; {a_where} does not list {_them(rooms)} "
                f"({read}).{held}",
                words=f"{_listed(rooms)} {'is' if len(rooms) == 1 else 'are'} on the vendor's matrix but not on the "
                "architect's room schedule",
                boxes=(mine[rooms[0]].rooms[rooms[0]], architect.heading),
                also=[mine[r].rooms[r] for r in rooms[1:]],
                proved=None if doubt else proof_words,
                kind="extra_piece",
            )
        )
    if only_architect:
        rooms = sorted(only_architect, key=lambda r: (len(r), r))
        out.append(
            _finding(
                vendor,
                architect,
                rooms,
                item=f"{_cap(_listed(rooms))}: on the architect's schedule, not on the vendor's "
                "matrix",
                shown=("not on the vendor's matrix", _listed(rooms)),
                why=f"{a_where} lists {_listed(rooms)}; {v_where} does not list {_them(rooms)} "
                f"({read}).{held}",
                words=f"{_listed(rooms)} {'is' if len(rooms) == 1 else 'are'} on the architect's room schedule but not on "
                "the vendor's matrix",
                boxes=(vendor.heading, theirs[rooms[0]].rooms[rooms[0]]),
                proved=None if doubt else proof_words,
                kind="missing_piece",
            )
        )
    both = [r for r in mine if r in theirs]
    # (b) the accessible rooms.
    for rooms, (given, marked) in (
        (
            [r for r in both if mine[r].accessible and not theirs[r].accessible],
            ("gives its accessible item to", "does not mark {} accessible"),
        ),
        (
            [r for r in both if theirs[r].accessible and not mine[r].accessible],
            ("does not give its accessible item to", "marks {} accessible"),
        ),
    ):
        if not rooms:
            continue
        rooms = sorted(rooms, key=lambda r: (len(r), r))
        first = rooms[0]
        marked = marked.format(_them(rooms))
        out.append(
            _finding(
                vendor,
                architect,
                rooms,
                item=f"{_cap(_listed(rooms))}: accessible (ADA) on one list only",
                shown=(
                    f"{mine[first].name} ({'accessible' if mine[first].accessible else 'not accessible'})",
                    f"{theirs[first].name} ({'accessible' if theirs[first].accessible else 'not accessible'})",
                ),
                why=f"{v_where} {given} {_listed(rooms)}; {a_where} {marked} ({read})." f"{held}",
                words=f"{_listed(rooms)}: the vendor's matrix {given} {_them(rooms)}, the "
                f"architect's room schedule {marked}",
                boxes=(mine[first].rooms[first], theirs[first].rooms[first]),
                also=[mine[r].rooms[r] for r in rooms[1:]],
                proved=None if doubt else proof_words,
                kind="count",
            )
        )
    # (c) the room type of each room on both lists (the accessible differences are (b)'s).
    pairs: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for room in both:
        if mine[room].accessible != theirs[room].accessible:
            continue
        key = (type_key(mine[room].name), type_key(theirs[room].name))
        if key[0] != key[1]:
            pairs.setdefault(key, []).append(room)
    for rooms in sorted(pairs.values(), key=lambda rs: (-len(rs), rs)):
        rooms = sorted(rooms, key=lambda r: (len(r), r))
        first = rooms[0]
        v_name, a_name = mine[first].name, theirs[first].name
        out.append(
            _finding(
                vendor,
                architect,
                rooms,
                item=f'{_cap(_listed(rooms))}: room type "{v_name}" on the vendor\'s matrix, '
                f'"{a_name}" on the architect\'s schedule',
                shown=(v_name, a_name),
                why=f'{v_where} lists {_listed(rooms)} as "{v_name}"; {a_where} lists '
                f'{_them(rooms)} as "{a_name}" ({read}).',
                words=f'{_listed(rooms)}: the vendor\'s matrix lists {_them(rooms)} as "{v_name}"; '
                f'the architect\'s room schedule lists {_them(rooms)} as "{a_name}"',
                boxes=(mine[first].rooms[first], theirs[first].rooms[first]),
                also=[mine[r].rooms[r] for r in rooms[1:]],
            )
        )
    return out


# ---- the set pack ----------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tables_of(folder: Path, uploads: Path | None, files: Iterable[str]) -> list[Table]:
    """Every room table on the pages of `files` in the set pack (`folder`), the line work read
    from the pack (else the uploads in `uploads`)."""
    dims = _read_json(folder / "dims.json")
    wanted = set(files)
    out: list[Table] = []
    if not isinstance(dims, Mapping):
        return out
    line_work = import_module("app.review.assistant.vendor_consistency").line_work(
        uploads or folder, folder
    )
    for entry in dims.get("pages") or []:
        if not isinstance(entry, Mapping) or str(entry.get("file")) not in wanted:
            continue
        phrases = views_module.phrases_of(entry)
        if not rooms_module.table_rooms(text for text, _box in phrases):
            continue
        file, page = str(entry["file"]), int(entry["page"])
        try:
            segments = line_work(file, page)
        except Exception:  # noqa: BLE001 - no line work: the table is not read
            segments = None
        found = read_table(phrases, segments, file, page)
        if found is not None:
            out.append(found)
    return out


def _overlap(one: Table, other: Table) -> float:
    mine, theirs = set(one.rooms()), set(other.rooms())
    return len(mine & theirs) / max(1, len(mine | theirs))


def check_pack(
    folder: Path | None, uploads: Path | None, main_file: str | None = None
) -> list[dict[str, Any]]:
    """The findings over a two-file set pack: each vendor room table (of `main_file`, when given)
    against the architect's room table it overlaps most, when that one is clear (`MIN_OVERLAP`,
    and no other architect table as close)."""
    if folder is None:
        return []
    page_map = _read_json(folder / "page_map.json")
    roles = page_map.get("upload_roles") if isinstance(page_map, Mapping) else None
    if not isinstance(roles, Mapping):
        return []
    vendor_files = [
        str(f) for f, r in roles.items() if r == "vendor" and (main_file in (None, str(f)))
    ]
    architect_files = [str(f) for f, r in roles.items() if r == "architect"]
    if not vendor_files or not architect_files:
        return []
    vendors = tables_of(folder, uploads, vendor_files)
    if not vendors:
        return []
    architects = tables_of(folder, uploads, architect_files)
    out: list[dict[str, Any]] = []
    for table in vendors:
        ranked = sorted(architects, key=lambda a: -_overlap(table, a))
        if not ranked or _overlap(table, ranked[0]) < MIN_OVERLAP:
            continue
        if len(ranked) > 1 and _overlap(table, ranked[1]) >= _overlap(table, ranked[0]):
            continue
        out += compare(table, ranked[0])
    return out


def _already(finding: Mapping[str, Any], live: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        f.get("created_by") == CREATED_BY
        and f.get("page") == finding.get("page")
        and f.get("item") == finding.get("item")
        for f in live
    )


def add_to_answer(engine: Any) -> int:
    """The engine's hook (a full check, at its finish, $0): the vendor's room tables against the
    architect's, each finding added through the finding book (its checks and trust rule), unless
    the answer already has it; a please-check's mark and every room's spot laid on after. Returns
    how many were added."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return 0
    from app.review.assistant import likely_problems

    workspace = engine.tools.workspace
    book = engine.book
    made = check_pack(
        workspace.set_pack, workspace.uploads, str(book.main_file) if book.main_file else None
    )
    added = 0
    with engine.finding_lock:
        live = book.live()
        for finding in made:
            if _already(finding, live):
                continue
            mark = finding.pop(likely_problems.FIELD, None)
            also = finding.pop("also_on", [])
            stored = book.add_code_finding(finding, created_by=CREATED_BY)
            if stored is None:
                continue
            laid = {
                **stored,
                **({likely_problems.FIELD: mark} if mark else {}),
                "also_on": [{**entry, "finding_id": stored["id"]} for entry in also],
            }
            if book.replace(laid):
                engine._show_finding(laid)
                live.append(laid)
                added += 1
    return added
