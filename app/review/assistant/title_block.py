"""The title block's cells, read by code and checked against the item's other drawing (2026-11-01).

GV's reviewer corrects a vendor sheet's title block from the rest of the set: the item cell of a
cabinet sheet whose top has its own countertop drawing names both items (it writes the countertop's
number in when the cell prints only the cabinet's), and the description says what the countertop
drawing of the same item says. The page reviewer found these by luck: the cells are small, so it
looked at them in a wide picture or not at all. A vendor uses one title-block template for its
whole set, so code reads the cells from the text layer by their anchor words:

- **Cells.** An anchor phrase (ITEM NO., DESCRIPTION, SHEET / PAGE, REV, DATE, PROJECT, OVERALL
  SIZE, DRAWN BY, SCALE...) and its value: written in the same phrase (`DATE: 10/07/2026`), right of
  it on its line (`PROJECT:` then the name), or below it in its column (a row of anchors over a row
  of values; each column runs halfway to its neighbouring anchors).
- **Against the item's other drawing** (`SetContext.pair_pages`: the same item number on another
  sheet, e.g. the cabinet sheet and its countertop drawing):
  - the item cell names the other drawing's item number too (a cell printing only `ABC-101.C` while
    the countertop drawing is `ABC-101.CT` lacks it);
  - the description holds every word of the other drawing's description (its DESCRIPTION cell, or
    the words printed with its item number when its template has no such cell); checked only on a
    page whose description is a DESCRIPTION cell;
  - the date is not older than the newest date on the set's sheets with the same title-block
    template (one round of the vendor's sheets); the other drawing's date is told, not checked
    (a stone drawing made later than its cabinet sheet is normal).

**Never red.** The cells and each mismatch are handed to the reviewer as facts; it reports what it
confirms as a please-check of kind F. A title block is text, not a size: the finding path's
code-proved reds are size items with two code-read labels and their geometry (`likely_problems`:
"code has no rule that could" prove a wrong title), and code's own cards here would duplicate the
reviewer's (the set check's duplicate test, `set_context._already`, knows only finish tags). Plain
code on printed text: no model call. Verification: `tests/review/assistant/test_title_block.py`.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.review.assistant.set_context import SetContext, tags_in

__all__ = ["Cell", "blocks", "cells", "check", "description_of", "sheet_date"]

_log = logging.getLogger(__name__)

#: Anchor words, by what the cell holds (longest spellings first, so `REV.DATE` is not `REV`), in
#: capitals as title blocks print them (a body note's "Item by others" is no cell).
ANCHORS: Final[tuple[tuple[str, str], ...]] = (
    ("item", r"ITEM\s*(?:NO\.?|NUMBER|#|CODE)|ITEM"),
    ("description", r"DESCRIPTION|DESC\.?|(?:DRAWING|SHEET)\s*TITLE"),
    ("project_no", r"(?:PROJECT|JOB)\s*(?:NO\.?|NUMBER|#)"),
    ("project", r"PROJECT\s*NAME|JOB\s*NAME|PROJECT|JOB"),
    ("size", r"OVERALL\s*SIZE|SIZE"),
    ("drawn_by", r"DRAWN\s*BY|DRAWN|DRAFTER"),
    ("checked_by", r"CHECKED\s*BY|APPROVED\s*BY"),
    ("rev_date", r"REV(?:ISION)?\.?\s*DATE"),
    ("rev", r"REV(?:ISION)?\.?\s*(?:NO\.?|#)?|VERSION"),
    ("date", r"DATE"),
    ("sheet", r"SHEET\s*(?:NO\.?|#)?|PAGE|(?:DWG|DRAWING)\s*(?:NO\.?|#)"),
    ("scale", r"SCALE"),
)
_ANCHOR: Final = re.compile(
    r"^(?P<anchor>" + "|".join(f"(?P<{name}>{pattern})" for name, pattern in ANCHORS) + r")"
    r"\s*[:#.]?\s*(?P<value>.*)$"
)
#: An anchor-and-value phrase has at most this many words (a body note "SIZE ..." is no cell).
MAX_CELL_WORDS: Final = 8
#: A value right of its anchor: on the same line (centres this close) and at most this far.
SAME_LINE_PT: Final = 3.0
RIGHT_PT: Final = 150.0
#: A value below its anchor: its top at most this far under the anchor's bottom.
BELOW_PT: Final = 50.0
#: Anchors are one header row when their centres are this close.
HEADER_ROW_PT: Final = 3.0
#: The outermost column runs this far past its anchor when it has no neighbour on that side.
EDGE_PT: Final = 100.0
#: A cell's value phrase has at most this many words (a copyright line is no cell).
MAX_VALUE_WORDS: Final = 12
#: The words printed with an item number (a template with no DESCRIPTION cell): the next line under
#: it, this close and left edges this close.
UNDER_TAG_PT: Final = 6.0
UNDER_TAG_LEFT_PT: Final = 20.0
#: Words that never tell two descriptions apart.
_STOP: Final = frozenset({"at", "the", "and", "of", "a", "an", "for", "in", "on", "to"})
_DATE: Final = re.compile(r"\b(\d{1,4})[/.-](\d{1,2})[/.-](\d{2,4})\b")
_SIZE: Final = re.compile(r"\d\s*(?:\"|''|”|mm\b)")


@dataclass(frozen=True, slots=True)
class Cell:
    """One title-block cell: what it holds, its anchor as printed, its value and the value's box."""

    name: str
    anchor: str
    value: str
    box: tuple[float, float, float, float]


Box = tuple[float, float, float, float]


def _box(phrase: Mapping[str, Any]) -> Box | None:
    raw = phrase.get("box")
    if isinstance(raw, Mapping):
        raw = [raw.get(k) for k in ("x0", "y0", "x1", "y1")]
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1)


def _text(phrase: Mapping[str, Any]) -> str:
    return " ".join(str(phrase.get("text") or "").split())


def _has_value(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text))


def _anchor(text: str) -> tuple[str, str, str, bool] | None:
    """(what the cell holds, the anchor as printed, the value in the same phrase, whether a colon
    follows the anchor) or `None`."""
    found = _ANCHOR.match(text)
    if found is None or len(text.split()) > MAX_CELL_WORDS:
        return None
    rest = text[found.end("anchor") :]
    if rest[:1].isalpha():
        return None  # `DATED`, `SIZES`, `PAGEANT`: words, not anchors
    value = found.group("value").strip()
    marked = rest.lstrip()[:1] in (":", "#")
    if value and not marked and not re.search(r"\d", value):
        return None  # `JOB SPECIFICATIONS` is a heading; `ITEM 101` and `DATE: ...` are cells
    name = next(n for n, _p in ANCHORS if found.group(n))
    return name, found.group("anchor").strip(), value, ":" in rest[:3]


def cells(found: Sequence[Mapping[str, Any]]) -> list[Cell]:
    """The page's title-block cells, each name once (its first cell with a value), in page order."""
    boxed = [(p, b, _text(p)) for p in found if (b := _box(p)) is not None and _text(p)]
    anchors: list[tuple[str, str, str, bool, Box]] = []
    for _p, box, text in boxed:
        got = _anchor(text)
        if got is not None:
            anchors.append((*got, box))
    anchor_boxes = {a[4] for a in anchors}
    out: dict[str, Cell] = {}
    for name, anchor, value, colon, box in anchors:
        if name in out:
            continue
        if value:
            if _has_value(value):
                out[name] = Cell(name, anchor, value, box)
            continue  # a blank cell (`DATE: ______`)
        middle = (box[1] + box[3]) / 2
        right = [
            (b[0] - box[2], text, b)
            for _p, b, text in boxed
            if b not in anchor_boxes
            and abs((b[1] + b[3]) / 2 - middle) <= SAME_LINE_PT
            and 0 <= b[0] - box[2] <= RIGHT_PT
            and _has_value(text)
        ]
        header = sorted(
            (a[4] for a in anchors if abs((a[4][1] + a[4][3]) / 2 - middle) <= HEADER_ROW_PT),
            key=lambda b: b[0],
        )
        column = _column(box, header)
        below = [
            (b, text)
            for _p, b, text in boxed
            if b not in anchor_boxes
            and 0 < b[1] - box[3] <= BELOW_PT
            and column[0] <= (b[0] + b[2]) / 2 <= column[1]
            and len(text.split()) <= MAX_VALUE_WORDS
            and _has_value(text)
        ]
        if right and (colon or not below or len(header) == 1):
            _gap, text, b = min(right, key=lambda item: item[0])
            out[name] = Cell(name, anchor, text, b)
        elif below:
            below.sort(key=lambda item: (round(item[0][1]), item[0][0]))
            joined = " ".join(text for _b, text in below)
            union = (
                min(b[0] for b, _t in below),
                min(b[1] for b, _t in below),
                max(b[2] for b, _t in below),
                max(b[3] for b, _t in below),
            )
            out[name] = Cell(name, anchor, joined, union)
    return sorted(out.values(), key=lambda c: (c.box[1], c.box[0]))


def _column(box: Box, header: Sequence[Box]) -> tuple[float, float]:
    """The anchor's column: halfway to its neighbours in the header row (or `EDGE_PT` past it)."""
    centre = (box[0] + box[2]) / 2
    centres = [(b[0] + b[2]) / 2 for b in header]
    left = [c for c in centres if c < centre - 0.5]
    right = [c for c in centres if c > centre + 0.5]
    lo = (max(left) + centre) / 2 if left else min(box[0], centre - EDGE_PT)
    hi = (min(right) + centre) / 2 if right else max(box[2], centre + EDGE_PT)
    return lo, hi


def _cell(found: Sequence[Mapping[str, Any]], name: str) -> Cell | None:
    return next((c for c in cells(found) if c.name == name), None)


def description_of(found: Sequence[Mapping[str, Any]], tag: str | None = None) -> str | None:
    """A page's description: its DESCRIPTION cell, else the words printed on the line under its
    item number `tag` (a template with no such cell prints the item number over its name)."""
    cell = _cell(found, "description")
    if cell is not None:
        return cell.value
    if not tag:
        return None
    boxed = [(b, _text(p)) for p in found if (b := _box(p)) is not None and _text(p)]
    for box, text in boxed:
        if text != tag and not text.startswith(tag + " "):
            continue
        under = [
            (b, t)
            for b, t in boxed
            if 0 <= b[1] - box[3] <= UNDER_TAG_PT
            and abs(b[0] - box[0]) <= UNDER_TAG_LEFT_PT
            and re.search(r"[A-Za-z]{2,}", t)
            and not tags_in(t)
            and not _SIZE.search(t)
            and len(t.split()) <= MAX_VALUE_WORDS
        ]
        if under:
            return min(under, key=lambda item: item[0][1])[1]
    return None


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]


def sheet_date(found: Sequence[Mapping[str, Any]]) -> dt.date | None:
    """The newest date in the page's DATE and REV DATE cells (month first unless it cannot be)."""
    dates: list[dt.date] = []
    for cell in cells(found):
        if cell.name in ("date", "rev_date"):
            for found_date in _DATE.finditer(cell.value):
                parsed = _parse(*found_date.groups())
                if parsed is not None:
                    dates.append(parsed)
    return max(dates) if dates else None


def _parse(a: str, b: str, c: str) -> dt.date | None:
    try:
        if len(a) == 4:  # 2026/10/07
            return dt.date(int(a), int(b), int(c))
        year = int(c) + (2000 if len(c) == 2 else 0)
        first, second = int(a), int(b)
        if first > 12:  # 28/09/2026
            first, second = second, first
        return dt.date(year, first, second)
    except ValueError:
        return None


def _layout(found: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """The template's fingerprint: which cells its title block has."""
    return frozenset(c.name for c in cells(found))


def _date_words(value: dt.date) -> str:
    return f"{value.month:02d}/{value.day:02d}/{value.year}"


def check(context: SetContext | None, page: int, found: Sequence[Mapping[str, Any]]) -> list[str]:
    """The mismatches between `page`'s title block and its item's other drawing, and its date
    against the newest date of the set's sheets with the same template, as fact lines."""
    if context is None or not context.built:
        return []
    mine = {c.name: c for c in cells(found)}
    out: list[str] = []
    item = mine.get("item")
    item_tags = tags_in(item.value) if item is not None else []
    seen: set[tuple[int, str]] = set()
    for other_page, other_tag, my_tag in context.pair_pages(page):
        if (other_page, other_tag) in seen:
            continue
        seen.add((other_page, other_tag))
        if item is not None and my_tag in item_tags and other_tag not in item_tags:
            out.append(
                f"Item cell prints {item.value!r}; the item's other drawing p{other_page} is "
                f"{other_tag}. GV's item cell names both items ({my_tag} / {other_tag}) when the "
                "item has its own countertop drawing: please check (kind F, box the item cell)."
            )
        here = mine.get("description")
        try:
            there_found = context.pages.get(other_page, [])
        except AttributeError:
            there_found = []
        there = description_of(there_found, other_tag)
        if here is not None and there:
            missing = [w for w in _words(there) if w not in set(_words(here.value))]
            if missing:
                out.append(
                    f"Description cell {here.value!r}; the item's other drawing p{other_page} "
                    f"({other_tag}) describes it as {there!r} (this cell lacks: "
                    f"{', '.join(dict.fromkeys(missing))}): please check (kind F, box the "
                    "description cell)."
                )
    date = sheet_date(found)
    if date is not None:
        layout = _layout(found)
        newest: tuple[dt.date, int] | None = None
        for other, phrases in sorted(context.pages.items()):
            if other == page or _layout(phrases) != layout:
                continue
            when = sheet_date(phrases)
            if when is not None and (newest is None or when > newest[0]):
                newest = (when, other)
        if newest is not None and date < newest[0]:
            out.append(
                f"Date {_date_words(date)} is older than the newest date on the set's sheets with "
                f"this title block ({_date_words(newest[0])}, p{newest[1]}): this sheet may not be "
                "the latest round (please check, kind F)."
            )
    return out


def _other_lines(context: SetContext | None, page: int) -> list[str]:
    """What the item's other drawing prints in its title block (told, not checked)."""
    if context is None or not context.built:
        return []
    lines: list[str] = []
    for other_page, other_tag, _mine in context.pair_pages(page)[:3]:
        phrases = context.pages.get(other_page, [])
        bits = [f"item {other_tag}"]
        described = description_of(phrases, other_tag)
        if described:
            bits.append(f"description {described!r}")
        when = sheet_date(phrases)
        if when is not None:
            bits.append(f"dated {_date_words(when)}")
        lines.append(f"The item's other drawing p{other_page}: " + ", ".join(bits) + ".")
    return lines


_LEAD: Final = (
    "Title block cells code read from this page's text layer (anchor: value; no zoom is needed to "
    "read them), the item's other drawing, and what differs. Each difference is a please-check of "
    "kind F that you confirm on the page (never red); report it once, boxed on the cell:"
)


def blocks(review: Any, context_of: Callable[[], SetContext] | None = None) -> list[dict[str, Any]]:
    """The page reviewer's hook (`_Run.opening`): the title-block facts as one text block, logged
    as `title_cells` and `title_mismatches`. `context_of`: the set's context (`_Run._context`); the
    cells are given on their own when the set is not read."""
    from app.review.assistant.untrusted import fence

    found = getattr(review, "found", []) or []
    context: SetContext | None = None
    if context_of is not None:
        try:
            context = context_of()
            if not context.built and not context.ready(0):
                context = None
        except Exception:  # noqa: BLE001 - the page's own cells are still given
            _log.warning("title block p%s: the set was not read", getattr(review, "page", "?"))
            context = None
    page = int(getattr(review, "page", 0) or 0)
    read = cells(found)
    mismatches = check(context, page, found)
    knowledge = getattr(review, "knowledge", None)
    if isinstance(knowledge, dict):
        knowledge["title_cells"] = {c.anchor: c.value for c in read}
        knowledge["title_mismatches"] = mismatches
    if not read:
        return []
    lines = ["Cells: " + "; ".join(f"{c.anchor}: {c.value}" for c in read)]
    lines += _other_lines(context, page)
    lines += [f"Differs: {m}" for m in mismatches] or ["Differs: nothing code could check."]
    return [{"type": "text", "text": _LEAD + "\n" + fence("\n".join(lines), "title_block")}]
