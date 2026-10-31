"""The set's material and finish tables, read by code once per set (plan row M12b, 2026-11-01).

Until now the page reviewer knew a finish only by the tags printed on its own page. A vendor sheet
prints a small material table (tag, then what the tag means: `ST-03  QZ-16 Color: Nova Grey, Quartz
by GV`), a countertop drawing prints its stone in a labelled cell (`COLOR:  ST-02 QZ-19 Snow
White`), and an architect's set prints a ruled finish schedule. On a client sheet of the tuned set
the vendor's table row named one stone while the item's countertop drawing and the set's other
tables named another, and GV's reviewer corrected the table row (issues 9 and 10 of the 2026-10-31
failure map).

This module reads every page's tables once (`build_index`, from `SetContext.build`):

- **ruled tables** (`ruled_tables`): pdfplumber's `lines` strategy, searched only around a header
  row the page's text prints (two of TAG, CODE, MARK, MATERIAL, FINISH, DESCRIPTION, MANUFACTURER,
  COLOR on one line); a table is kept only when its header row names such a column and its rows
  start with a tag;
- **the unruled material table** a vendor prints on each sheet (`table_rows`): a column of at least
  two different tags, each with the words to its right (the idea `page_reviewer.finish_facts` uses,
  with each line given to the nearest tag of the column, so a row never takes its neighbour's
  words);
- **labelled cells** (`labelled_cells`): `COLOR: ST-02 QZ-19 Snow White` on a countertop drawing.

Then the **set legend** (`Legend`): per tag, the description the most pages agree on, with the
pages. Two descriptions agree when they share a product code (`QZ-16`, `AB1051`, `#512`) or, when
one has none, most of their words. Per page (`facts`): the legend lines for its families, its table
rows that disagree with the set's majority or with the item's other drawing
(`SetContext.pair_pages`), and the callout tags its own table does not list.

**Never red.** These are please-check leads for the reviewer (and, with `SCHEDULE_RED` on, code's own
please-check card at the table row). The red path cannot take them cleanly: its text reds
(`architect_items`) rest on the architect's drawing, which governs; here both sides are the vendor's,
so code proves the set contradicts itself, not which row is wrong (on the tuned set the majority
agrees with the WRONG row's description of its own tag; the right answer is another tag). A red
would need a should-be rule the owner has not set. `SCHEDULE_RED` stays off until measured.

Plain code on printed text and drawn lines: no model call, $0.
Verification: `tests/review/assistant/test_schedule_index.py` (made-up tables).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant.set_context import TAG, family_of

if TYPE_CHECKING:
    from app.review.assistant.set_context import SetContext

__all__ = [
    "SCHEDULE_RED",
    "Disagreement",
    "Group",
    "Legend",
    "Row",
    "ScheduleIndex",
    "Table",
    "agree",
    "build_index",
    "disagreements",
    "facts",
    "labelled_cells",
    "log_block",
    "read_grid",
    "red_on",
    "ruled_tables",
    "table_rows",
]

_log = logging.getLogger(__name__)

#: Code's own please-check card at a table row that disagrees (`set_context.check_page`). Off until
#: measured; the env `GV_SCHEDULE_RED=1` switches it on. Never a red (see the module's docstring).
SCHEDULE_RED: bool = os.environ.get("GV_SCHEDULE_RED") == "1"

#: A header phrase of a material or finish table.
HEADER: Final = re.compile(
    r"\b(TAGS?|CODES?|MARKS?|SYMBOLS?|KEY|MATERIALS?|FINISH(?:ES)?|DESCRIPTIONS?|MANUFACTURERS?|"
    r"COLOU?RS?)\b",
    re.IGNORECASE,
)
#: The header of a table's tag column.
_TAG_HEADER: Final = re.compile(r"\b(TAGS?|CODES?|MARKS?|SYMBOLS?|KEY|NO\.?|ID)\b", re.IGNORECASE)
#: A labelled finish cell: the label, then the tag and its words (`COLOR: ST-02 QZ-19 Snow White`).
_CELL_LABEL: Final = re.compile(r"^(COLOU?R|MATERIAL|FINISH|STONE)\s*[:：]?$", re.IGNORECASE)
_CELL_INLINE: Final = re.compile(
    r"^(?:COLOU?R|MATERIAL|FINISH|STONE)\s*[:：]\s*(.+)$", re.IGNORECASE
)
#: A product code inside a description: letters then digits (QZ-16, AB1051, AB426, RAL 9004)
#: or a number sign (#512).
_CODE: Final = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{2,}[- ]?\d+[A-Z0-9]*|#\s?\d+)(?![A-Za-z0-9/])")
#: Words that say nothing about which material it is.
_STOP: Final = frozenset(
    [
        "the",
        "and",
        "to",
        "by",
        "with",
        "for",
        "of",
        "see",
        "spec",
        "color",
        "colour",
        "finish",
        "match",
        "gv",
        "note",
        "notes",
    ]
)
#: A header phrase has at most this many words (a long note naming MATERIAL is not a header).
HEADER_WORDS: Final = 4
#: A header cell or a tag cell of a ruled table has at most this many characters.
HEADER_CHARS: Final = 40
#: Two tags are in one column when their left edges are this close (points).
COLUMN_PT: Final = 4.0
#: Two rows of an unruled table are at most this far apart, centre to centre (points).
ROW_GAP_PT: Final = 40.0
#: A description line belongs to the nearest tag of its column within this (points, centres).
ROW_REACH_PT: Final = 12.0
#: A description starts at most this far right of its tag (points; `finish_facts` reads 320).
DESC_REACH_PT: Final = 320.0
#: A description's next phrase on its line starts at most this far past the one before (points).
DESC_GAP_PT: Final = 80.0
#: A labelled cell's tag starts at most this far right of its label (points).
LABEL_REACH_PT: Final = 80.0
#: A table covering more than this share of its page is the sheet's frame, not a schedule.
FRAME_SHARE: Final = 0.5
#: The ruled reading's time on one set, at most (seconds); the unruled reading always runs. The
#: set's text is read first (about 1.4 s a page) and a reviewer waits for it at most
#: `page_reviewer.SET_CONTEXT_WAIT_S` (120 s), so this stays small.
RULED_BUDGET_S: Final = 15.0
#: Two header cells on one line at most this far apart are a header row (points).
HEADER_ROW_PT: Final = 300.0
#: The ruled search's area around a header row (points of the page as shown).
CROP_ABOVE_PT: Final = 30.0
CROP_BELOW_PT: Final = 900.0
CROP_SIDE_PT: Final = 300.0
#: A majority needs at least this many pages to say a row disagrees with it.
MIN_LEGEND_PAGES: Final = 2
#: Legend lines per page, at most.
MAX_LEGEND_LINES: Final = 12
#: Description characters shown in a fact.
_SHOWN: Final = 70
_MAX_PAGES_LISTED: Final = 10

Box = tuple[float, float, float, float]


def red_on() -> bool:
    return SCHEDULE_RED


# ---- what a row says -------------------------------------------------------------------------------


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def codes(description: str) -> frozenset[str]:
    """The product codes a description prints, spelled one way (`QZ-16` and `QZ16`: one)."""
    return frozenset(re.sub(r"[-\s]", "", c).upper() for c in _CODE.findall(description))


def _words(description: str) -> frozenset[str]:
    plain = _CODE.sub(" ", description)
    return frozenset(w for w in re.findall(r"[a-z]{3,}", plain.lower()) if w not in _STOP)


def agree(a: str, b: str) -> bool:
    """Whether two descriptions name the same material: they share a product code when both print
    one, else most of their words agree (half or more, Jaccard). A description with no words to
    compare agrees (nothing says otherwise)."""
    ca, cb = codes(a), codes(b)
    if ca and cb:
        return bool(ca & cb)
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return True
    return len(wa & wb) / len(wa | wb) >= 0.5


def _grain(description: str) -> str:
    from app.review.assistant.page_reviewer import _GRAIN_WORDS, _PLAIN_WORDS

    if _GRAIN_WORDS.search(description):
        return "grain"
    if _PLAIN_WORDS.search(description):
        return "plain"
    return "?"


def _tag_of(text: str) -> str | None:
    """The tag a cell is (the whole cell; a family of two letters or more: never a cabinet code)."""
    cleaned = _clean(text).strip(" :;,")
    if not cleaned or not TAG.fullmatch(cleaned):
        return None
    family = family_of(cleaned)
    return cleaned if family is not None and len(family) >= 2 else None


def _described(description: str) -> bool:
    return len(description) >= 6 and sum(ch.isalpha() for ch in description) >= 3


@dataclass(frozen=True, slots=True)
class Row:
    """One table row: a tag and what the table says it is."""

    tag: str
    description: str
    page: int
    box: Box
    """The tag's cell (points of the page as shown)."""
    text_box: Box | None
    """The description's cells, together."""
    source: str
    """`ruled` (a ruled table), `table` (an unruled table) or `cell` (a labelled cell)."""
    grain: str = "?"


@dataclass(frozen=True, slots=True)
class Table:
    page: int
    box: Box
    source: str
    header: tuple[str, ...]
    rows: tuple[Row, ...]


def _union(boxes: Sequence[Sequence[float]]) -> Box | None:
    if not boxes:
        return None
    return (
        min(float(b[0]) for b in boxes),
        min(float(b[1]) for b in boxes),
        max(float(b[2]) for b in boxes),
        max(float(b[3]) for b in boxes),
    )


def _row(tag: str, description: str, page: int, box: Sequence[float], text: Sequence[Sequence[float]], source: str) -> Row:  # fmt: skip
    description = _clean(description)
    return Row(
        tag=tag,
        description=description,
        page=page,
        box=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
        text_box=_union(text),
        source=source,
        grain=_grain(description),
    )


# ---- ruled tables ----------------------------------------------------------------------------------

#: One cell: its text and its box (points as shown), or `None` for a merged cell's empty slot.
Cell = tuple[str, Box | None]


def read_grid(page: int, grid: Sequence[Sequence[Cell]], box: Box) -> Table | None:
    """A ruled table's grid as a material table, or `None`: a header row (in the first three) that
    names a material or finish column, every header cell short and none a tag; then the rows whose
    tag column holds a tag, each with its other cells as the description."""
    header_at: int | None = None
    for n, row in enumerate(grid[:3]):
        texts = [_clean(t) for t, _b in row if _clean(t)]
        if (
            texts
            and any(HEADER.search(t) for t in texts)
            and all(len(t) <= HEADER_CHARS for t in texts)
            and not any(_tag_of(t) for t in texts)
        ):
            header_at = n
            break
    if header_at is None:
        return None
    header = [_clean(t) for t, _b in grid[header_at]]
    body = grid[header_at + 1 :]
    column: int | None = next(
        (i for i, name in enumerate(header) if name and _TAG_HEADER.search(name)), None
    )
    if column is None:
        counts = [
            sum(1 for row in body if i < len(row) and _tag_of(row[i][0]))
            for i in range(len(header))
        ]
        column = max(range(len(counts)), key=lambda i: (counts[i], -i)) if any(counts) else None
    if column is None:
        return None
    rows: list[Row] = []
    for row in body:
        if column >= len(row):
            continue
        text, cell = row[column]
        tag = _tag_of(text)
        if tag is None or cell is None:
            continue
        others = [(t, b) for i, (t, b) in enumerate(row) if i != column and _clean(t)]
        description = " ".join(_clean(t) for t, _b in others)
        if _described(description):
            boxes = [b for _t, b in others if b is not None]
            rows.append(_row(tag, description, page, cell, boxes, "ruled"))
    if not rows:
        return None
    return Table(page, box, "ruled", tuple(header), tuple(rows))


def header_rows(found: Sequence[Mapping[str, Any]]) -> list[Box]:
    """Where a page prints a table's header row: one phrase naming two header words or more
    (`TAG MATERIAL COLOR`), or two short header phrases (`HEADER_WORDS` words at most) on one line
    within `HEADER_ROW_PT`. A lone `MATERIAL SPEC` or `COLOR:` cell is not a header row (the vendor's
    sheets print one on every page; their tables are read unruled)."""
    cells: list[Box] = []
    out: list[Box] = []
    for phrase in found:
        b = _box(phrase)
        text = _clean(str(phrase.get("text") or ""))
        if b is None or not text:
            continue
        named = {m.group(0).upper().rstrip("S") for m in HEADER.finditer(text)}
        if len(named) >= 2 and len(text.split()) <= 2 * HEADER_WORDS:
            out.append(b)
        elif named and len(text.split()) <= HEADER_WORDS:
            cells.append(b)
    for n, a in enumerate(cells):
        for b in cells[n + 1 :]:
            gap = max(a[0], b[0]) - min(a[2], b[2])
            if abs(_middle(a) - _middle(b)) <= 3 and gap <= HEADER_ROW_PT:
                joined = _union([a, b])
                if joined is not None:
                    out.append(joined)
    return out


@dataclass(frozen=True, slots=True)
class _Frame:
    """A pdfplumber page's `(x, top)` to points as shown and back (its rotation applied, as
    `under_markup` places the words)."""

    ox: float
    oy: float
    ax: float
    ay: float
    bx: float
    by: float
    bounds: tuple[float, float, float, float]

    @classmethod
    def of(cls, page: Any) -> _Frame:
        from extraction.reader import pixel_placement

        place = pixel_placement(page, 7200)
        o, r, d = place(0, 0), place(1000, 0), place(0, 1000)
        left, top, right, bottom = (float(v) for v in page.bbox)
        return cls(
            ox=o.x / 100,
            oy=o.y / 100,
            ax=(r.x - o.x) / 100_000,
            ay=(r.y - o.y) / 100_000,
            bx=(d.x - o.x) / 100_000,
            by=(d.y - o.y) / 100_000,
            bounds=(left, top, right, bottom),
        )

    def shown(self, b: Sequence[float]) -> Box:
        corners = [(float(x), float(y)) for x in (b[0], b[2]) for y in (b[1], b[3])]
        xs = [self.ox + self.ax * x + self.bx * y for x, y in corners]
        ys = [self.oy + self.ay * x + self.by * y for x, y in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    def plumber(self, b: Box) -> Box:
        det = self.ax * self.by - self.bx * self.ay
        corners = [(x - self.ox, y - self.oy) for x in (b[0], b[2]) for y in (b[1], b[3])]
        xs = [(dx * self.by - self.bx * dy) / det for dx, dy in corners]
        ys = [(self.ax * dy - self.ay * dx) / det for dx, dy in corners]
        left, top, right, bottom = self.bounds
        return (max(left, min(xs)), max(top, min(ys)), min(right, max(xs)), min(bottom, max(ys)))


def _ruled_on(page: Any, number: int, rows: Sequence[Box]) -> list[Table]:
    """One page's ruled material tables, searched around its header rows."""
    from app.review.assistant.symbol_font import decoded

    frame = _Frame.of(page)
    area = float(page.width) * float(page.height)
    settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    seen: set[tuple[int, ...]] = set()
    out: list[Table] = []
    for row in rows:
        crop = frame.plumber(
            (
                row[0] - CROP_SIDE_PT,
                row[1] - CROP_ABOVE_PT,
                row[2] + CROP_SIDE_PT,
                row[3] + CROP_BELOW_PT,
            )
        )
        if crop[2] - crop[0] < 2 or crop[3] - crop[1] < 2:
            continue
        for table in page.crop(crop).find_tables(settings):
            x0, top, x1, bottom = (float(v) for v in table.bbox)
            key = tuple(round(v) for v in table.bbox)
            if key in seen or (x1 - x0) * (bottom - top) > FRAME_SHARE * area:
                continue
            seen.add(key)
            grid: list[list[Cell]] = []
            for found_row, texts in zip(table.rows, table.extract(), strict=False):
                grid.append(
                    [
                        (decoded(str(t or "")), frame.shown(c) if c is not None else None)
                        for c, t in zip(found_row.cells, texts, strict=False)
                    ]
                )
            read = read_grid(number, grid, frame.shown(table.bbox))
            if read is not None:
                out.append(read)
    return out


def ruled_tables(
    pdf: Path, pages: Mapping[int, Sequence[Mapping[str, Any]]], budget_s: float = RULED_BUDGET_S
) -> tuple[list[Table], int]:
    """The ruled material tables of `pdf` (pdfplumber `lines`), searched only around the header
    rows a page's text prints (`header_rows`: from `CROP_ABOVE_PT` above a row to `CROP_BELOW_PT`
    below it, `CROP_SIDE_PT` to each side), within `budget_s` for the set. Returns the tables and
    how many pages were searched."""
    import pdfplumber

    wanted = {p: rows for p, found in sorted(pages.items()) if (rows := header_rows(found))}
    out: list[Table] = []
    searched = 0
    if not wanted:
        return out, searched
    deadline = monotonic() + budget_s
    with pdfplumber.open(str(pdf)) as opened:
        for number, rows in wanted.items():
            if monotonic() > deadline:
                _log.warning("set schedule: ruled tables not read from p%s on (time)", number)
                break
            if not 1 <= number <= len(opened.pages):
                continue
            page = opened.pages[number - 1]
            searched += 1
            try:
                out += _ruled_on(page, number, rows)
            except Exception:  # noqa: BLE001 - that page's ruled tables are left out
                _log.warning("set schedule: ruled tables of p%s not read", number)
            finally:
                page.close()
    return out, searched


# ---- unruled tables and labelled cells -------------------------------------------------------------


def _box(phrase: Mapping[str, Any]) -> Box | None:
    raw = phrase.get("box")
    if not isinstance(raw, Sequence) or len(raw) != 4:
        return None
    try:
        return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    except (TypeError, ValueError):
        return None


def _middle(b: Box) -> float:
    return (b[1] + b[3]) / 2


def table_rows(page: int, found: Sequence[Mapping[str, Any]]) -> list[Table]:
    """The unruled material tables of a page: a column of at least two different tags (left edges
    within `COLUMN_PT`, rows at most `ROW_GAP_PT` apart, no tag twice), each tag with the lines to
    its right, every line given to the nearest tag of the column (within `ROW_REACH_PT`). A table
    with fewer than two described rows is a strip of tags, not a table."""
    boxed = [(p, b) for p in found if (b := _box(p)) is not None]
    tags = [(t, b) for p, b in boxed if (t := _tag_of(str(p.get("text") or ""))) is not None]
    tags.sort(key=lambda item: (item[1][0], _middle(item[1])))
    columns: list[list[tuple[str, Box]]] = []
    for tag, b in tags:
        home = next((c for c in columns if abs(c[0][1][0] - b[0]) <= COLUMN_PT), None)
        if home is None:
            columns.append([(tag, b)])
        else:
            home.append((tag, b))
    runs: list[list[tuple[str, Box]]] = []
    for column in columns:
        column.sort(key=lambda item: _middle(item[1]))
        run: list[tuple[str, Box]] = []
        for item in column:
            if run and _middle(item[1]) - _middle(run[-1][1]) > ROW_GAP_PT:
                runs.append(run)
                run = []
            run.append(item)
        runs.append(run)
    # A table's column: two tags or more, none twice. A lone tag (`QZ-16` above its colour
    # words) is description text; a column of the same tag repeated is callouts.
    runs = [r for r in runs if len(r) >= 2 and len({t for t, _b in r}) == len(r)]
    column_boxes = {b for r in runs for _t, b in r}
    out: list[Table] = []
    for run in runs:
        mine_boxes = {b for _t, b in run}
        left = max(b[2] for _t, b in run)
        top, bottom = _middle(run[0][1]) - ROW_REACH_PT, _middle(run[-1][1]) + ROW_REACH_PT
        lines: dict[int, list[tuple[str, Box]]] = {n: [] for n in range(len(run))}
        for phrase, b in boxed:
            if b in column_boxes or not (top <= _middle(b) <= bottom):
                continue
            if b[0] < left - 1 or b[0] - left > DESC_REACH_PT:
                continue
            # Another table's column of tags to the right ends this table's rows there.
            if any(
                tb[0] > left and tb[0] <= b[0] and abs(_middle(tb) - _middle(b)) <= ROW_REACH_PT
                for tb in column_boxes - mine_boxes
            ):
                continue
            distances = [abs(_middle(b) - _middle(rb)) for _t, rb in run]
            nearest = min(range(len(run)), key=lambda n: (distances[n], n))
            if distances[nearest] <= ROW_REACH_PT:
                lines[nearest].append((_clean(str(phrase.get("text") or "")), b))
        rows: list[Row] = []
        for n, (tag, b) in enumerate(run):
            mine = _touching(lines[n], left)
            description = " ".join(t for t, _b in mine)
            if _described(description):
                rows.append(_row(tag, description, page, b, [lb for _t, lb in mine], "table"))
        if len(rows) >= 2:
            box = _union([r.box for r in rows] + [r.text_box for r in rows if r.text_box])
            if box is not None:
                out.append(Table(page, box, "table", (), tuple(rows)))
    return out


def _touching(found: Sequence[tuple[str, Box]], left: float) -> list[tuple[str, Box]]:
    """A row's description: on each of its lines, the phrases from the tag column rightward while
    each starts within `DESC_GAP_PT` of the one before (the first within `DESC_GAP_PT` of the
    column); a phrase past a wider gap is another column of the sheet, not this row's words."""
    by_line: list[list[tuple[str, Box]]] = []
    for item in sorted(found, key=lambda item: (_middle(item[1]), item[1][0])):
        line = next((ln for ln in by_line if abs(_middle(ln[0][1]) - _middle(item[1])) <= 3), None)
        if line is None:
            by_line.append([item])
        else:
            line.append(item)
    kept: list[tuple[str, Box]] = []
    for line in by_line:
        edge = left
        for text, b in sorted(line, key=lambda item: item[1][0]):
            if b[0] - edge > DESC_GAP_PT:
                break
            kept.append((text, b))
            edge = max(edge, b[2])
    return sorted(kept, key=lambda item: (item[1][1], item[1][0]))


def labelled_cells(page: int, found: Sequence[Mapping[str, Any]]) -> list[Row]:
    """The finish cells a drawing labels (`COLOR: ST-02 QZ-19 Snow White`): a label phrase, then
    on its line (within `LABEL_REACH_PT`) a phrase that starts with a tag and goes on with words; or
    both in one phrase."""
    boxed = [(p, b) for p in found if (b := _box(p)) is not None]
    out: list[Row] = []

    def split(text: str) -> tuple[str, str] | None:
        head, _space, rest = _clean(text).partition(" ")
        tag = _tag_of(head)
        if tag is None or not _described(rest):
            return None
        return tag, rest

    for phrase, b in boxed:
        text = _clean(str(phrase.get("text") or ""))
        inline = _CELL_INLINE.match(text)
        if inline:
            got = split(inline.group(1))
            if got:
                out.append(_row(got[0], got[1], page, b, [b], "cell"))
            continue
        if not _CELL_LABEL.match(text):
            continue
        beside = [
            (other, ob)
            for other, ob in boxed
            if other is not phrase
            and 0 <= ob[0] - b[2] <= LABEL_REACH_PT
            and abs(_middle(ob) - _middle(b)) <= 5
        ]
        for other, ob in sorted(beside, key=lambda item: item[1][0])[:1]:
            got = split(str(other.get("text") or ""))
            if got:
                out.append(_row(got[0], got[1], page, ob, [ob], "cell"))
    return out


# ---- the set legend --------------------------------------------------------------------------------


@dataclass(slots=True)
class Group:
    """The rows of one tag that agree (`agree`), with their pages."""

    description: str
    pages: list[int] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Legend:
    """One tag across the set: its descriptions, the most pages first."""

    tag: str
    groups: tuple[Group, ...]

    @property
    def majority(self) -> Group | None:
        """The description most pages agree on; `None` on a tie."""
        if not self.groups:
            return None
        if len(self.groups) > 1 and len(self.groups[1].pages) == len(self.groups[0].pages):
            return None
        return self.groups[0]


def _legend(rows: Mapping[int, Sequence[Row]]) -> dict[str, Legend]:
    by_tag: dict[str, list[Row]] = {}
    for page in sorted(rows):
        for row in rows[page]:
            by_tag.setdefault(row.tag, []).append(row)
    out: dict[str, Legend] = {}
    for tag, found in by_tag.items():
        groups: list[Group] = []
        for row in found:
            home = next((g for g in groups if agree(g.rows[0].description, row.description)), None)
            if home is None:
                home = Group(row.description)
                groups.append(home)
            home.rows.append(row)
            if row.page not in home.pages:
                home.pages.append(row.page)
        for group in groups:
            # The words most of its rows print (ties: the longest), shown for the group.
            spelled: dict[str, int] = {}
            for row in group.rows:
                spelled[row.description] = spelled.get(row.description, 0) + 1
            group.description = max(spelled, key=lambda d: (spelled[d], len(d)))
            group.pages.sort()
        groups.sort(key=lambda g: (-len(g.pages), g.pages[0]))
        out[tag] = Legend(tag, tuple(groups))
    return out


@dataclass(slots=True)
class ScheduleIndex:
    """Every page's material rows and the set's legend (`build_index`)."""

    tables: list[Table] = field(default_factory=list)
    cells: list[Row] = field(default_factory=list)
    rows: dict[int, list[Row]] = field(default_factory=dict)
    legend: dict[str, Legend] = field(default_factory=dict)
    ruled_pages: int = 0
    """Pages the ruled reading searched."""


def build_index(
    pages: Mapping[int, Sequence[Mapping[str, Any]]],
    pdf: Path | None = None,
    budget_s: float = RULED_BUDGET_S,
) -> ScheduleIndex:
    """The set's tables and legend from every page's phrases (and its drawn lines when `pdf` is
    given). A page's tag keeps its first row: a ruled table's, then an unruled table's, then a
    labelled cell's."""
    index = ScheduleIndex()
    if pdf is not None:
        try:
            ruled, index.ruled_pages = ruled_tables(pdf, pages, budget_s)
            index.tables += ruled
        except Exception:  # noqa: BLE001 - the unruled tables still count
            _log.warning("set schedule: the ruled tables of %s were not read", pdf.name)
    for page in sorted(pages):
        index.tables += table_rows(page, pages[page])
        index.cells += labelled_cells(page, pages[page])
    for row in [r for t in index.tables for r in t.rows] + index.cells:
        mine = index.rows.setdefault(row.page, [])
        if all(r.tag != row.tag for r in mine):
            mine.append(row)
    index.legend = _legend(index.rows)
    return index


# ---- per page --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Disagreement:
    """A page's table row that disagrees with the set's majority (`against="set"`) or with the
    item's other drawing (`against="drawing"`)."""

    row: Row
    against: str
    other_tag: str
    other_description: str
    other_pages: tuple[int, ...]
    other_item: str | None = None


def _pages(pages: Sequence[int]) -> str:
    shown = ", ".join(f"p{p}" for p in pages[:_MAX_PAGES_LISTED])
    more = len(pages) - _MAX_PAGES_LISTED
    return shown + (f" (+{more} more)" if more > 0 else "")


def _said(description: str) -> str:
    text = description if len(description) <= _SHOWN else description[: _SHOWN - 1] + "…"
    return f"'{text}'"


def disagreements(index: ScheduleIndex, context: SetContext, page: int) -> list[Disagreement]:
    """`page`'s table rows that disagree: with the set's majority for the same tag (at least
    `MIN_LEGEND_PAGES` pages, more than the row's own group), or with the item's other drawing
    when that drawing lists one row of the family and none of `page`'s rows of it matches (same tag
    and agreeing words)."""
    mine = index.rows.get(page, [])
    out: list[Disagreement] = []
    for row in mine:
        legend = index.legend.get(row.tag)
        majority = legend.majority if legend else None
        if (
            legend is not None
            and majority is not None
            and page not in majority.pages
            and len(majority.pages) >= MIN_LEGEND_PAGES
            and not agree(majority.description, row.description)
        ):
            out.append(
                Disagreement(row, "set", row.tag, majority.description, tuple(majority.pages))
            )
    seen: set[tuple[str, int]] = set()
    for other_page, other_item, _mine in context.pair_pages(page):
        theirs_all = index.rows.get(other_page, [])
        families = sorted({f for r in mine if (f := family_of(r.tag)) is not None})
        for family in families:
            here = [r for r in mine if family_of(r.tag) == family]
            theirs = [r for r in theirs_all if family_of(r.tag) == family]
            if len(theirs) != 1:
                continue
            there = theirs[0]
            if any(r.tag == there.tag and agree(r.description, there.description) for r in here):
                continue
            for row in here:
                if (row.tag, other_page) in seen:
                    continue
                seen.add((row.tag, other_page))
                out.append(
                    Disagreement(
                        row, "drawing", there.tag, there.description, (other_page,), other_item
                    )
                )
    return out


def _legend_line(legend: Legend) -> str:
    majority = legend.majority
    if majority is not None and len(legend.groups) == 1:
        return f"  {legend.tag}: {_said(majority.description)} ({_pages(majority.pages)})"
    parts = "; ".join(f"{_said(g.description)} ({_pages(g.pages)})" for g in legend.groups[:3])
    lead = "most pages" if majority is not None else "split, no majority"
    return f"  {legend.tag} ({lead}): {parts}"


def facts(index: ScheduleIndex, context: SetContext, page: int) -> list[str]:
    """The schedule facts for `page`'s reviewer (please-check leads, never red): the set legend of
    the families this page uses, its table rows that disagree (`disagreements`), and the callout
    tags its own table does not list, with what the set's tables say they are."""
    if not index.legend:
        return []
    mine = index.rows.get(page, [])
    printed = context.tags_on(page)
    families = {f for r in mine if (f := family_of(r.tag)) is not None}
    families |= {f for t in printed if (f := family_of(t)) is not None and t in index.legend}
    shown = sorted(
        (legend for tag, legend in index.legend.items() if family_of(tag) in families),
        key=lambda legend: (legend.tag not in printed, legend.tag),
    )
    lines: list[str] = []
    if shown:
        tables = len({t.page for t in index.tables} | {c.page for c in index.cells})
        lines.append(
            f"Set legend (code read the material/finish tables on {tables} page(s); per tag, "
            "what the most pages say it is):"
        )
        lines += [_legend_line(legend) for legend in shown[:MAX_LEGEND_LINES]]
    for found in disagreements(index, context, page):
        row = found.row
        if found.against == "set":
            lines.append(
                f"This page's table row {row.tag} {_said(row.description)} — set legend "
                f"({_pages(list(found.other_pages))}): {found.other_tag} "
                f"{_said(found.other_description)}. The row disagrees with the rest of the set "
                "(please check)."
            )
        else:
            other = found.other_pages[0]
            legend = index.legend.get(found.other_tag)
            majority = legend.majority if legend else None
            backed = (
                f" (set legend {found.other_tag}: {_pages(majority.pages)})"
                if majority is not None and agree(majority.description, found.other_description)
                else ""
            )
            lines.append(
                f"This page's table row {row.tag} {_said(row.description)} — the item's "
                f"other drawing p{other} ({found.other_item}) lists {found.other_tag} "
                f"{_said(found.other_description)}{backed}. One of them is wrong (please check)."
            )
    if mine:
        listed = {r.tag for r in mine}
        row_families = {family_of(t) for t in listed}
        for tag in printed:
            if tag in listed or family_of(tag) not in row_families:
                continue
            legend = index.legend.get(tag)
            majority = legend.majority if legend else None
            said = (
                f"; the set's tables say {tag} is {_said(majority.description)} "
                f"({_pages(majority.pages)})"
                if majority is not None
                else ""
            )
            lines.append(
                f"Callouts here print {tag}, which this page's own table does not list{said}. "
                "The callout or the table is wrong (please check)."
            )
    return lines


def log_block(index: ScheduleIndex, context: SetContext) -> dict[str, Any]:
    """For the run's log: tables found, legend tags and row disagreements over the set."""
    found = sum(len(disagreements(index, context, page)) for page in sorted(index.rows))
    return {
        "schedule_tables": len(index.tables),
        "ruled_tables": sum(1 for t in index.tables if t.source == "ruled"),
        "ruled_pages_searched": index.ruled_pages,
        "labelled_cells": len(index.cells),
        "legend_tags": len(index.legend),
        "split_tags": sum(1 for legend in index.legend.values() if legend.majority is None),
        "row_disagreements": found,
    }
