"""The simple full check's work list: every vendor drawing, with its area's architect drawings.

The roster's helpers chose what to read and ran out of money after one or two pages, so two thirds
of a set's pages were never compared (pipeline audit, 2026-10-19). Here code decides coverage, from
the set pack alone ($0, no PDF opened, no picture drawn):

- **One item per vendor drawing** (each vendor `half` of the page map, every round): its page, its
  drawing's box, its round, its area, the products its printed words name, and code's numbered labels
  on it (`dims.json`, each label given to the drawing nearest it, as `dim_labels` gives its side).
- **Its architect drawings** are every architect drawing of the same area (the section and area GV
  typed, carried over pages as `full_check_brief.page_lines` does), plus a page the reviewer or code
  paired it with in the plan check (`pair_page`). The governing one first (R5): a detail, section
  or enlarged view, then the larger printed scale, then the one on the same page.
- **Brand prototype drawings** of the area are listed as reference, not compared (R1: the ID set
  governs unless it points to the prototype).
- **Skipped, with code's reason:** divider and general pages (no pasted drawing), a page the plan
  check skipped, a mark too small to be a drawing, a drawing with no heading saying whose it is, a
  vendor drawing whose area has no architect drawing, an architect drawing of an area the vendor did
  not submit, a page of another upload. Nothing is skipped for money.
- **Separate files** (#1722): a file whose drawings took their role from its upload (the architect's
  own PDF, #1719) is numbered after the vendor's file from a round thousand (`set_pages`: its page 1
  is set page 1001), so its drawings and labels have ids of their own (`p1001.a1`, `p1001.D3`). Each
  `Drawing` keeps its file and its own page (`file_page`) for its pictures. Its architect drawings
  are the candidates a plain vendor drawing (one whole page, no GV area) is paired with
  (`_pair_across`).
- **Two-file sets of several projects** (2026-10-23): the set index (`set_index.py`, read by AI and
  checked by code) places every page in its project; a vendor drawing is paired only with views
  of its own project's architect sheets, never a reference set (R1) nor a key plan, and is
  "drawing needed" before any paid compare when the architect's file lacks its project or prints
  no sizes of it (`DRAWING_NEEDED`). A table or notes page is skipped (`NO_DRAWING_NOTE`).
- **Every page is said to be covered or skipped** (`WorkList.pages`); `uncovered` lists any page with
  a drawing that no item covers and no reason explains (a test holds it empty on a real set).
- **Hard items** (no labels on the vendor drawing, an architect drawing with no labels, a very dense
  drawing) are marked for the stronger model.

Read with the plan check's answers and code's assumptions laid over the page map
(`full_check_plan.read_page_map(assume=True)`), as the roster reads it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from app.review.assistant import dim_pairs, view_scales
from app.review.assistant.full_check_plan import DIVIDER_NOTE, read_page_map
from app.review.assistant.product_scope import named_products
from app.review.assistant.sheet_revisions import exact_copies, older_copies
from app.review.assistant.simple_check import pairing as pairing_module
from app.review.assistant.simple_check import projects as projects_module
from app.review.assistant.simple_check import views as views_module
from app.review.assistant.simple_check.schema import (
    AcrossCandidate,
    AcrossPair,
    Box,
    Drawing,
    Label,
    PageCover,
    Sheet,
    Skipped,
    ViewsDisagree,
    WorkItem,
    WorkList,
    label_ref,
)
from app.review.assistant.simple_check.set_pages import set_pages

#: The AI page matcher's saved answers in the set pack (`page_match.py`, #1798).
MATCH_FILE: Final = "page_match.json"
#: The set index in the set pack (`set_index.py`: each page's project, set and views, read by AI
#: and checked by code).
INDEX_FILE: Final = "set_index.json"

#: Code's ranking of the architect items for each vendor drawing paired across the uploads, kept
#: between builds of one unchanged set pack (a full check builds its work list again as each
#: matcher answer lands): (vendor drawing, its candidates) -> (the ranking, code's own pick).
type RankMemo = dict[
    tuple[str, tuple[str, ...]], tuple[tuple[pairing_module.Scored, ...], str | None]
]

__all__ = [
    "CALLOUT_CLEAR",
    "CALLOUT_REACH_PT",
    "COMMON_WORD_MIN",
    "COMMON_WORD_SHARE",
    "DENSE_LABELS",
    "DRAWING_NEEDED",
    "INDEX_FILE",
    "ITEM_TAG_MIN",
    "ITEM_TAG_SHARE",
    "KEY_PLAN_TAGS",
    "MATCH_FILE",
    "MIN_DRAWING_PT",
    "MIN_PAGE_DIMS",
    "NO_DRAWING_NOTE",
    "NUMBERS_UNREAD",
    "RankMemo",
    "architect_views_findings",
    "build_worklist",
    "combined_files",
    "worklist_from",
]

_log = logging.getLogger(__name__)

#: A pasted "drawing" smaller than this on either side (points) is a mark or a stamp, not a drawing.
MIN_DRAWING_PT: Final = 36.0
#: A label belongs to the nearest drawing within this many points of it (`dim_labels._side`).
_LABEL_REACH_PT: Final = 36.0
#: Room kept round a drawing's box and its labels in its pictures (points).
_MARGIN_PT: Final = 6.0
#: A vendor drawing with more labels than this goes to the stronger model.
DENSE_LABELS: Final = 100
_ROLE_LETTER: Final = {"vendor": "v", "architect": "a", "brand_prototype": "b"}
_LABEL_SIDE: Final = {"vendor": "vendor", "architect": "architect", "brand_prototype": "brand"}
_TITLE: Final = re.compile(r"\b(ELEVATION|SECTION|DETAIL|PLAN|ENLARGED|VIEW)S?\b", re.IGNORECASE)
_DETAIL: Final = re.compile(r"\b(DETAIL|SECTION|ENLARGED)S?\b", re.IGNORECASE)
_SCALE: Final = re.compile(
    r"(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(?:\"|''|in\b)?\s*=\s*1\s*'\s*-?\s*0?\s*\"?",
    re.IGNORECASE,
)
_MAX_NOTES: Final = 12
#: At most this many joined phrases per drawing (`_stacked_in`).
_MAX_STACKED: Final = 2_000
_MAX_NOTE_CHARS: Final = 80
#: A page of the vendor's file that prints fewer dimensions than this (with an inch, foot or
#: millimetre mark), whose text code read whole (typed, no pen-stroke letters) and on which GV
#: headed no drawing, has no drawing to compare: a table, notes or pictures.
MIN_PAGE_DIMS: Final = 3
#: The words of an unpaired drawing's reason when code knows, before any paid compare, that the
#: architect's drawing of it is not in the upload (the letter's "drawings needed" list).
DRAWING_NEEDED: Final = "drawing needed"
#: A view the set index says prints no sizes counts as such when code reads at most this many
#: labels in it (a stray room height on an outline).
MAX_SIZELESS_LABELS: Final = 2
NO_DRAWING_NOTE: Final = (
    "a table, notes or pictures page: it prints no dimensions, so there is no drawing to "
    "compare (code)"
)
#: A page-map entry's key when its numbers are drawn as lines code could not read
#: (`set_pack.NUMBERS_UNREAD`): a drawing all the same, never "a table, notes or pictures page".
NUMBERS_UNREAD: Final = "numbers_unread"
_UNIT: Final = re.compile(r"[\"'\u2032\u2033]|\bmm\b", re.IGNORECASE)
#: An item-like tag (`pairing.item_tags_in`) names one piece when the vendor prints it on at most
#: this share of its drawings (or `ITEM_TAG_MIN` drawings): `LM-004`, printed on a third of a real
#: set's drawings, is a laminate; `XX-801`, on the few sheets of one piece, is the piece.
ITEM_TAG_SHARE: Final = 0.1
ITEM_TAG_MIN: Final = 3
#: A plan printing this many item-like tags is a key or furniture plan, not an item's drawing.
KEY_PLAN_TAGS: Final = 8
#: A key plan's callout points to the item whose tag is printed this close to it (points), when no
#: other callout is nearly as close (`CALLOUT_CLEAR` times as far, or more).
CALLOUT_REACH_PT: Final = 100.0
CALLOUT_CLEAR: Final = 1.5
#: A naming word the vendor prints on more than this share of its drawings (and more than
#: `COMMON_WORD_MIN` of them) is its title block's or its brand's, never evidence of a piece.
COMMON_WORD_SHARE: Final = 0.25
COMMON_WORD_MIN: Final = 10


# ---- reading the pack --------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sheet(combined: Mapping[str, bool] | None, file: str) -> Sheet | None:
    """A file's kind for its drawings (`schema.Drawing.sheet`), `None` when not known."""
    if combined is None or file not in combined:
        return None
    return "combined" if combined[file] else "ordinary"


def _by_page(document: Any, key: str = "pages") -> dict[tuple[str, int], Mapping[str, Any]]:
    out: dict[tuple[str, int], Mapping[str, Any]] = {}
    entries = document.get(key) if isinstance(document, Mapping) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            out[(str(entry.get("file")), int(entry["page"]))] = entry
    return out


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


def _distance(point: tuple[float, float], box: Box) -> float:
    x, y = point
    return math.hypot(max(box[0] - x, 0.0, x - box[2]), max(box[1] - y, 0.0, y - box[3]))


def _overlap(a: Box, b: Box) -> float:
    """The share of the smaller box that the two boxes share."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / smaller if smaller > 0 else 0.0


def _union(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _plain(text: object) -> str:
    return " ".join(str(text or "").split())


def _scale_ratio(note: str | None) -> Fraction | None:
    """A printed scale note as a ratio of the drawing to the thing (`1/2" = 1'-0"` -> 1/24)."""
    if not note:
        return None
    found = _SCALE.search(note)
    if found is None:
        return None
    try:
        whole, _, rest = found.group(1).strip().partition(" ")
        value = Fraction(whole) + (Fraction(rest) if rest else 0)
    except (ValueError, ZeroDivisionError):
        return None
    return value / 12 if value > 0 else None


# ---- one page's drawings -----------------------------------------------------------------------


@dataclass(slots=True)
class _Half:
    file: str
    page: int
    index: int
    role: str
    round: int | None
    heading: str | None
    box: Box
    labels: list[Mapping[str, Any]] = field(default_factory=list)
    ref: str = ""
    file_page: int | None = None
    """The page in its own file, when `page` is a set page of a separate file (#1722)."""
    from_upload: bool = False
    """Its role came from its upload (a separate plain file, #1719): no GV area, no heading."""
    picture: bool = False
    """A pasted picture GV headed (#1835): it holds only the labels read inside it."""


def _assign_labels(
    halves: Sequence[_Half], labels: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Each label to the nearest drawing within reach whose role matches the label's side (as
    `dim_labels` gives the side). A headed pasted picture (#1835) takes only the labels read
    inside it (`origin: "picture"`), before any other drawing; every other label goes to the
    pasted drawings as before. Returns the labels no drawing took."""
    left: list[Mapping[str, Any]] = []
    drawings = [half for half in halves if not half.picture]
    for label in labels:
        box = _box(label.get("box"))
        if box is None:
            continue
        centre = _centre(box)
        if label.get("origin") == "picture":
            holding = [
                half
                for half in halves
                if half.picture
                and _distance(centre, half.box) == 0
                and _LABEL_SIDE.get(half.role) == label.get("side")
            ]
            if holding:
                holding[0].labels.append(label)
                continue
        best: tuple[float, float, _Half] | None = None
        for half in drawings:
            distance = _distance(centre, half.box)
            area = (half.box[2] - half.box[0]) * (half.box[3] - half.box[1])
            if best is None or (distance, area) < (best[0], best[1]):
                best = (distance, area, half)
        if (
            best is None
            or best[0] > _LABEL_REACH_PT
            or _LABEL_SIDE.get(best[2].role) != label.get("side")
        ):
            left.append(label)
            continue
        best[2].labels.append(label)
    return left


def _vendor_ids(page: int, halves: Sequence[_Half]) -> None:
    """The vendor drawings' ids, shared by every builder of the simple check: `p{page}.v{round}`
    (`v0` when the round is not known), and `.1`, `.2`... when one page has two vendor drawings of
    one round, in reading order (top to bottom, then left to right)."""
    ordered = sorted(halves, key=lambda h: (h.round or 0, h.box[1], h.box[0]))
    total: dict[int, int] = {}
    for half in ordered:
        total[half.round or 0] = total.get(half.round or 0, 0) + 1
    seen: dict[int, int] = {}
    for half in ordered:
        number = half.round or 0
        seen[number] = seen.get(number, 0) + 1
        base = f"p{page}.v{number}"
        half.ref = base if total[number] == 1 else f"{base}.{seen[number]}"


def _label(page: int, raw: Mapping[str, Any]) -> Label | None:
    box = _box(raw.get("box"))
    label_id = raw.get("id")
    if box is None or not isinstance(label_id, str) or not re.fullmatch(r"D[1-9]\d*", label_id):
        return None
    mm = raw.get("mm")
    return Label(
        ref=label_ref(page, label_id),
        page=page,
        id=label_id,
        text=_plain(raw.get("text")),
        inches=None if raw.get("inches") is None else str(raw.get("inches")),
        box=box,
        side=str(raw.get("side") or "unknown"),
        orientation=raw.get("orientation") if isinstance(raw.get("orientation"), str) else None,
        route=raw.get("route") if isinstance(raw.get("route"), str) else None,
        mm=None if mm is None else str(mm),
        origin=raw.get("origin") if isinstance(raw.get("origin"), str) else None,
    )


def _drawing_notes(
    notes_page: Mapping[str, Any] | None, half: _Half
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The tags and notes `notes.json` keeps for the drawing (matched by role and box)."""
    best: tuple[float, Mapping[str, Any]] | None = None
    role = {"brand_prototype": "brand"}.get(half.role, half.role)
    for drawing in (notes_page or {}).get("drawings") or []:
        if not isinstance(drawing, Mapping) or str(drawing.get("role")) not in (role, half.role):
            continue
        box = _box(drawing.get("box"))
        if box is None:
            continue
        share = _overlap(box, half.box)
        if share > 0.5 and (best is None or share > best[0]):
            best = (share, drawing)
    if best is None:
        return (), ()
    tags: list[str] = []
    for tag in best[1].get("tags") or []:
        text = _plain(tag.get("text") if isinstance(tag, Mapping) else tag)
        if text and text not in tags:
            tags.append(text)
    notes: list[str] = []
    for note in best[1].get("notes") or []:
        text = _plain(note.get("text") if isinstance(note, Mapping) else note)
        if text and text not in notes:
            notes.append(text[:_MAX_NOTE_CHARS])
    return tuple(tags), tuple(notes[:_MAX_NOTES])


def _drawing_scale(
    scales_page: Mapping[str, Any] | None,
    dims_page: Mapping[str, Any] | None,
    half: _Half,
) -> tuple[float | None, str | None]:
    """The drawing's measured scale (its most detailed view: the fewest inches per point) and the
    printed scale note on it."""
    side = _LABEL_SIDE.get(half.role)
    measured: list[float] = []
    note: str | None = None
    for entry in (scales_page or {}).get("scales") or []:
        if not isinstance(entry, Mapping) or entry.get("side") != side:
            continue
        box = _box(entry.get("box"))
        if box is None or _overlap(box, half.box) <= 0.5:
            continue
        try:
            value = float(entry.get("in_per_pt") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            measured.append(value)
        if note is None and isinstance(entry.get("note"), str):
            note = _plain(entry["note"])
    if note is None:
        for printed in (dims_page or {}).get("scale_notes") or []:
            box = _box(printed.get("box")) if isinstance(printed, Mapping) else None
            if box is not None and _distance(_centre(box), half.box) <= _LABEL_REACH_PT:
                note = _plain(printed.get("text"))
                break
    return (min(measured) if measured else None), note


def _stacked_in(dims_page: Mapping[str, Any] | None, box: Box) -> list[str]:
    """The printed phrases of a drawing joined down their lines, two or three at a time: a title
    block's cell wraps its words ("Tall Cabinets (Left) @" over "Study Corner"), and an item's
    title is found whole only in the joined text (#1798). A phrase continues in one that starts
    within a line and a half below it and shares some of its width."""
    found: list[tuple[str, Box]] = []
    for phrase in (dims_page or {}).get("phrases") or []:
        if not isinstance(phrase, Mapping):
            continue
        at = _box(phrase.get("box"))
        text = _plain(phrase.get("text"))
        if at is not None and text and _distance(_centre(at), box) <= _LABEL_REACH_PT:
            found.append((text, at))
    below: dict[int, list[int]] = {}
    for n, (_text, a) in enumerate(found):
        height = max(1.0, a[3] - a[1])
        for m, (_other, b) in enumerate(found):
            if m == n:
                continue
            if a[1] + 0.5 * height <= b[1] <= a[3] + 1.5 * height and min(a[2], b[2]) > max(
                a[0], b[0]
            ):
                below.setdefault(n, []).append(m)
    out: list[str] = []
    for n, (text, _a) in enumerate(found):
        for m in below.get(n, ()):
            out.append(f"{text} {found[m][0]}")
            out.extend(f"{text} {found[m][0]} {found[k][0]}" for k in below.get(m, ()))
    return out[:_MAX_STACKED]


def _phrases_in(dims_page: Mapping[str, Any] | None, box: Box) -> list[str]:
    out: list[str] = []
    for phrase in (dims_page or {}).get("phrases") or []:
        if not isinstance(phrase, Mapping):
            continue
        found = _box(phrase.get("box"))
        if found is not None and _distance(_centre(found), box) <= _LABEL_REACH_PT:
            out.append(_plain(phrase.get("text")))
    return out


# ---- areas -------------------------------------------------------------------------------------


def _area_owner(
    entry: Mapping[str, Any], known: Mapping[int, Mapping[str, Any]]
) -> Mapping[str, Any]:
    if not entry.get("area") and isinstance(entry.get("area_from_page"), int):
        return known.get(entry["area_from_page"]) or entry
    return entry


def _area_key(entry: Mapping[str, Any], known: Mapping[int, Mapping[str, Any]]) -> str | None:
    """The page's area within its section, as `full_check_brief` keys it (two sections may reuse
    an area's title)."""
    owner = _area_owner(entry, known)
    area = owner.get("area")
    if not area:
        return None
    return " ".join(f"{owner.get('section') or ''} / {area}".split()).lower()


# ---- the work list -----------------------------------------------------------------------------


def _main_file(page_map: Mapping[str, Any], main_file: str | None) -> str | None:
    files = [str(name) for name in page_map.get("files") or []]
    if main_file is not None and main_file in files:
        return main_file
    seen = {str(e.get("file")) for e in page_map.get("pages") or [] if isinstance(e, Mapping)}
    if main_file is not None and main_file in seen:
        return main_file
    return files[0] if files else (next(iter(seen)) if len(seen) == 1 else None)


def build_worklist(
    folder: Path, main_file: str | None = None, *, memo: RankMemo | None = None
) -> WorkList:
    """The work list of the set pack in `folder` (its page map with the plan check's answers and
    code's assumptions laid over it, its labels, scales and notes). Raises `FileNotFoundError` when
    the pack has no page map yet. `memo`: code's ranking across the uploads kept between builds of
    one unchanged pack (`RankMemo`)."""
    page_map = read_page_map(folder, assume=True, main_file=main_file)
    if page_map is None:
        raise FileNotFoundError(f"no page map in {folder}")
    annotations = _read(folder / "annotations.json")
    return worklist_from(
        page_map,
        texts=_page_texts(folder, page_map),
        dims=_read(folder / "dims.json"),
        scales=_read(folder / "scales.json"),
        notes=_read(folder / "notes.json"),
        annotations=annotations,
        matches=_read(folder / MATCH_FILE),
        health=_read(folder / "health.json"),
        set_index=_read(folder / INDEX_FILE),
        main_file=main_file,
        combined=combined_files(page_map, annotations),
        memo=memo,
    )


def _page_texts(folder: Path, page_map: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    """Each page's printed text (kept in the pack by the vendor check, `page_texts`), for the
    older copies of a sheet (`sheet_revisions`). Empty when it cannot be read: then no page is
    left out as an older copy."""
    files = [str(name) for name in page_map.get("files") or [] if name]
    if not files:
        return {}
    try:
        from app.review.assistant.vendor_consistency import page_texts

        return page_texts(folder, files)
    except Exception as failed:  # noqa: BLE001 - the work list goes on, every page checked
        _log.warning("work list: page texts not read: %s", type(failed).__name__)
        return {}


def combined_files(page_map: Mapping[str, Any], annotations: Any) -> Mapping[str, bool] | None:
    """Each file of the pack: whether it is one of GV's combined review sheets (the colour rule,
    decided 2026-10-19). As the label reader's test (`gv_marks.gv_combined`): a file with a pasted
    drawing (a PDF stamp, its box readable or not) is a combined sheet; a file with none is an
    ordinary upload, read in every colour. `None` when the pack has no annotations list (unknown:
    the prompts then use the strict words).

    A file with a rectangle annotation (`/Square`) and no stamp is left out (unknown, the strict
    words): the label reader's own test (`gv_marks.gv_combined`, via `stamp_text.carries_drawing`)
    counts a snapshot pasted as a rectangle with the drawing inside it, and this list cannot tell
    one from a reviewer's rectangle, so it never calls such a file ordinary."""
    if not isinstance(annotations, list):
        return None
    files = page_map.get("files") if isinstance(page_map, Mapping) else None
    names = [str(name) for name in files] if isinstance(files, list) else []
    stamped: set[str] = set()
    boxed: set[str] = set()
    for entry in annotations:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("kind") == "Stamp":  # with or without a readable box (`gv_combined`)
            stamped.add(str(entry.get("file")))
        elif entry.get("kind") == "Square":
            boxed.add(str(entry.get("file")))
    return {name: name in stamped for name in names if name in stamped or name not in boxed}


def worklist_from(
    page_map: Mapping[str, Any],
    *,
    dims: Any = None,
    scales: Any = None,
    notes: Any = None,
    annotations: Any = None,
    matches: Any = None,
    main_file: str | None = None,
    combined: Mapping[str, bool] | None = None,
    memo: RankMemo | None = None,
    health: Any = None,
    set_index: Any = None,
    texts: Mapping[tuple[str, int], str] | None = None,
) -> WorkList:
    """The work list from the pack's documents (pure: `build_worklist` reads them). `annotations`
    (the pack's `annotations.json`): GV's typed item titles are read from it, only where one is
    exactly an architect item's title (never a review comment). `matches`: the AI page
    matcher's saved answers (`page_match.MATCH_FILE`). `combined`: each file's kind
    (`combined_files`); a file it does not name is of unknown kind. `memo`: code's ranking across
    the uploads from an earlier build of the same pack (`RankMemo`), so a build that only has new
    matcher answers ranks nothing again. `health`: the pack's `health.json` (which pages code read
    whole as typed text: a page of them printing no dimension has no drawing, `NO_DRAWING_NOTE`).
    `set_index`: the set index (`set_index.INDEX_FILE`, each page's read checked by code): the projects
    and reference sets a vendor drawing is paired within, and the views' sizes; without it every
    architect item is a candidate (no project is known). `texts`: each page's printed text
    (`vendor_consistency.page_texts`): a vendor page that is an older copy of a sheet printed
    again later (an earlier revision, `sheet_revisions`) is skipped, said so; without it every
    page is checked."""
    file = _main_file(page_map, main_file)
    if file is None:
        return WorkList(file="", items=(), skipped=(), pages=())
    entries = [
        e
        for e in page_map.get("pages") or []
        if isinstance(e, Mapping) and isinstance(e.get("page"), int)
    ]
    mine = sorted((e for e in entries if str(e.get("file")) == file), key=lambda e: int(e["page"]))
    known = {int(e["page"]): e for e in mine}
    dims_pages = _by_page(dims)
    known_any = {
        (str(e.get("file")), int(e["page"])): e for e in entries if isinstance(e.get("page"), int)
    }
    scales_pages = _by_page(scales)
    notes_pages = _by_page(notes)
    health_pages = _by_page(health)
    skipped: list[Skipped] = []
    cover: dict[int, PageCover] = {}

    def skip(page: int, reason: str, drawing: str | None = None) -> None:
        skipped.append(Skipped(file=file, page=page, drawing=drawing, reason=reason))

    # An older copy of a sheet printed again later (an earlier revision): only the newest is
    # checked (owner, 2026-10-23).
    older = older_copies(
        page_map,
        texts or {},
        file,
        exact=exact_copies(file, dims, texts or {}),
        index=set_index,
    )

    pages = set_pages(page_map, file)
    # Another upload's pages: one file per work list (label ids name pages, not files), except a
    # separate file read by its upload, numbered after the vendor's (#1722).
    for entry in entries:
        if not pages.joined(str(entry.get("file"))) and entry.get("halves"):
            skipped.append(
                Skipped(
                    file=str(entry.get("file")),
                    page=int(entry["page"]),
                    reason=f"another upload: this work list compares {file} (code)",
                )
            )

    drawings: dict[str, Drawing] = {}
    pictures: set[str] = set()
    """The refs of the drawings that are headed pasted pictures (#1835)."""
    halves_by_area: dict[str | None, list[_Half]] = {}
    vendor_halves: list[_Half] = []
    across: list[_Half] = []
    """Architect drawings a plain vendor drawing may be paired with (#1722)."""
    separate_entries = sorted(
        (e for e in entries if str(e.get("file")) != file and pages.joined(str(e.get("file")))),
        key=lambda e: pages.to_set(str(e.get("file")), int(e["page"])),
    )
    roles = page_map.get("upload_roles")
    upload_roles = dict(roles) if isinstance(roles, Mapping) else {}
    architect_files = frozenset(n for n, r in upload_roles.items() if r == "architect")
    by_set_page: dict[int, Mapping[str, Any]] = {}

    def wanted(entry_file: str, half: Mapping[str, Any]) -> bool:
        """A separate file's drawing the work list reads: the architect's; and the vendor's of an
        earlier round uploaded beside the re-issued file (#1728), read by its upload."""
        if half.get("role") == "architect":
            return True
        return (
            half.get("role") == "vendor"
            and bool(half.get("from_upload"))
            and upload_roles.get(entry_file) == "vendor"
        )

    for entry in [*mine, *separate_entries]:
        entry_file = str(entry.get("file"))
        own = int(entry["page"])
        page = pages.to_set(entry_file, own)
        by_set_page[page] = entry
        separate = entry_file != file
        if separate:
            # A separate file's page: only its architect drawings (and an earlier round's vendor
            # drawings) are read; its own skips are said with its file and its own page.
            if entry.get("skip") or not any(
                isinstance(h, Mapping) and wanted(entry_file, h) for h in entry.get("halves") or []
            ):
                skipped.append(
                    Skipped(
                        file=entry_file,
                        page=own,
                        reason=str(entry.get("skip") or DIVIDER_NOTE),
                    )
                )
                continue
        elif entry.get("skip"):
            skip(page, str(entry["skip"]))
            cover[page] = PageCover(page=page, how="skipped", reason=str(entry["skip"]))
            continue
        elif own in older:
            skip(page, older[own].reason)
            cover[page] = PageCover(page=page, how="skipped", reason=older[own].reason)
            continue
        raw_halves = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
        if separate:
            raw_halves = [h for h in raw_halves if wanted(entry_file, h)]
        if not raw_halves:
            skip(page, DIVIDER_NOTE)
            cover[page] = PageCover(page=page, how="skipped", reason=DIVIDER_NOTE)
            continue
        if (
            not separate
            and not entry.get(NUMBERS_UNREAD)
            and _prints_no_drawing(
                dims_pages.get((entry_file, own)), health_pages.get((entry_file, own)), raw_halves
            )
        ):
            skip(page, NO_DRAWING_NOTE)
            cover[page] = PageCover(page=page, how="skipped", reason=NO_DRAWING_NOTE)
            continue
        halves: list[_Half] = []
        counts: dict[str, int] = {}
        for index, raw in enumerate(raw_halves):
            box = _box(raw.get("box"))
            role = str(raw.get("role") or "general")
            if box is None:
                skip(page, "a drawing with no box in the page map (code)")
                continue
            letter = _ROLE_LETTER.get(role)
            half = _Half(
                file=entry_file,
                page=page,
                index=index,
                role=role,
                round=raw.get("round") if isinstance(raw.get("round"), int) else None,
                heading=_plain(raw.get("heading")) or None,
                box=box,
                file_page=own if separate else None,
                from_upload=bool(raw.get("from_upload")),
                picture=bool(raw.get("picture")),
            )
            halves.append(half)
            if box[2] - box[0] < MIN_DRAWING_PT or box[3] - box[1] < MIN_DRAWING_PT:
                skip(
                    page,
                    f"a mark of {box[2] - box[0]:.0f} x {box[3] - box[1]:.0f} pt, too small to be "
                    "a drawing (code)",
                    f"p{page}.h{index + 1}",
                )
                half.role = "mark"
                continue
            if letter is None:
                skip(
                    page,
                    "a drawing with no heading saying whose it is, and the plan check did not "
                    "settle it (code)",
                    f"p{page}.h{index + 1}",
                )
                continue
            if letter != "v":
                counts[letter] = counts.get(letter, 0) + 1
                half.ref = f"p{page}.{letter}{counts[letter]}"
        _vendor_ids(page, [h for h in halves if h.role == "vendor"])
        dims_page = dims_pages.get((entry_file, own))
        left = _assign_labels(
            [h for h in halves if h.ref],
            [lb for lb in (dims_page or {}).get("labels") or [] if isinstance(lb, Mapping)],
        )
        if left:
            _log.debug("simple check work list: p%d has %d labels on no drawing", page, len(left))
        key = None if separate else _area_key(entry, known)
        for half in halves:
            if not half.ref:
                continue
            labels = tuple(found for raw in half.labels if (found := _label(page, raw)) is not None)
            boxes = [half.box, *(label.box for label in labels)]
            x0, y0, x1, y1 = _union(boxes)
            crop = (
                max(0.0, x0 - _MARGIN_PT),
                max(0.0, y0 - _MARGIN_PT),
                x1 + _MARGIN_PT,
                y1 + _MARGIN_PT,
            )
            tags, printed = _drawing_notes(notes_pages.get((entry_file, own)), half)
            titles = tuple(t for t in (half.heading or "", *printed) if t and _TITLE.search(t))
            in_per_pt, note = _drawing_scale(scales_pages.get((entry_file, own)), dims_page, half)
            drawing = Drawing(
                ref=half.ref,
                file=entry_file,
                page=page,
                file_page=half.file_page,
                half=half.index,
                role=half.role,  # type: ignore[arg-type]
                round=half.round,
                heading=half.heading,
                box=half.box,
                crop_box=tuple(round(v, 2) for v in crop),  # type: ignore[arg-type]
                labels=labels,
                titles=titles,
                tags=tags,
                notes=printed,
                scale_in_per_pt=in_per_pt,
                scale_note=note,
                detail=any(_DETAIL.search(t) for t in titles),
                sheet=_sheet(combined, entry_file),
            )
            drawings[half.ref] = drawing
            if half.picture:
                pictures.add(half.ref)
            if half.role == "architect" and (separate or half.from_upload):
                across.append(half)
            if separate:
                if half.role == "vendor":
                    vendor_halves.append(half)
                continue
            halves_by_area.setdefault(key, []).append(half)
            if half.role == "vendor":
                vendor_halves.append(half)

    places = _places(entries)
    typed = _typed_titles(annotations)

    known_facts: dict[str, pairing_module.Facts] = {}

    def facts_of(drawing: Drawing) -> pairing_module.Facts:
        if drawing.ref not in known_facts:
            known_facts[drawing.ref] = _facts(drawing)
        return known_facts[drawing.ref]

    def _facts(drawing: Drawing) -> pairing_module.Facts:
        """What code knows of a drawing for pairing across files (#1723, #1798): its printed
        values (with their direction), the references, tags and naming words printed on it, its
        place in the set's divider pages, its labels' drawn rows for `dim_pairs`; a vendor
        drawing's printed words (to find an architect item's title in its title block)."""
        own = drawing.own_page
        dims_page = dims_pages.get((drawing.file, own)) or {}
        printed = _phrases_in(dims_page, drawing.crop_box)
        texts = [
            *printed,
            *(drawing.titles or ()),
            *(drawing.notes or ()),
            *(drawing.heading or "",),
            *(
                str(s)
                for s in (known_any.get((drawing.file, own)) or {}).get("sheet_numbers") or []
            ),
        ]
        side = _LABEL_SIDE.get(drawing.role, drawing.role)
        raw = [
            dict(label)
            for label in dims_page.get("labels") or []
            if isinstance(label, Mapping)
            and label.get("side") == side
            and (box := _box(label.get("box"))) is not None
            and _distance(_centre(box), drawing.box) <= _LABEL_REACH_PT
            # A headed pasted picture's facts are only the labels read inside it (#1835).
            and (
                drawing.ref not in pictures
                or (label.get("origin") == "picture" and _distance(_centre(box), drawing.box) == 0)
            )
        ]
        scales_page = scales_pages.get((drawing.file, own)) or {}
        oriented = tuple(
            (str(label.get("orientation")), value)
            for label in raw
            if label.get("orientation") in ("h", "v")
            and (value := _inches_of(label.get("inches"))) is not None
        )
        return pairing_module.Facts(
            ref=drawing.ref,
            values=pairing_module.values_of(drawing.labels),
            references=pairing_module.references_in(texts),
            spans=_spans_with(raw, scales_page, side) if drawing.role == "vendor" else None,
            rows=(tuple(raw), scales_page, side),
            oriented=oriented,
            tags=pairing_module.tags_in([*texts, *(drawing.tags or ())]),
            markers=pairing_module.markers_in(texts)[0],
            hand=pairing_module.markers_in(texts)[1],
            item_tags=(
                pairing_module.item_tags_in([*texts, *(drawing.tags or ())])
                if drawing.role == "vendor"
                else None
            ),
            words=pairing_module.words_in(texts),
            views=pairing_module.views_in([*(drawing.titles or ()), *(drawing.heading or "",)]),
            section=places.get((drawing.file, own)),
            text=(
                " ; ".join(
                    pairing_module.title_key(t)
                    for t in (" ".join(printed), *_stacked_in(dims_page, drawing.crop_box))
                )
                if drawing.role == "vendor"
                else ""
            ),
            phrases=(
                tuple(
                    dict.fromkeys(
                        key
                        for t in (*printed, *_stacked_in(dims_page, drawing.crop_box))
                        if len((key := pairing_module.title_key(t)).split()) >= 2
                    )
                )
                if drawing.role == "vendor"
                else ()
            ),
        )

    # A vendor drawing with no architect drawing of its own (a plain vendor file, or a page of
    # the vendor's file whose area the architect's file holds) is paired across the uploads by
    # code and the AI page matcher (#1723, #1798), never by the plan check's page-before guess;
    # the drawings of every round paired with one architect item are one area for their rounds
    # (#1728).
    answers = _saved_answers(matches)
    read_index = _index_pages(set_index)
    items_across, across_context = _enrich(
        _items_across(across, drawings, file, architect_files, by_set_page, facts_of),
        dims_pages,
        notes_pages,
        projects_module.places(read_index, entries, _printed_words(dims_pages, annotations)),
        {
            (str(one.get("file")), int(one["page"])): one
            for one in read_index.values()
            if isinstance(one.get("page"), int)
        },
    )
    separate_items = any(c.file != file for c in items_across)
    mine_vendor = [h for h in vendor_halves if h.file == file]
    for half in mine_vendor:
        known_vendor = facts_of(drawings[half.ref])
        across_context.tag_drawings.update(known_vendor.item_tags or ())
        across_context.word_drawings.update(known_vendor.words)
    across_context.item_limit = max(ITEM_TAG_MIN, ITEM_TAG_SHARE * len(mine_vendor))
    per_page = Counter((h.file, h.file_page or h.page) for h in vendor_halves)
    for half in vendor_halves:
        page_read = read_index.get(f"{half.file}#{half.file_page or half.page}")
        if page_read is not None:
            across_context.marked[half.ref] = _marked_words(page_read)
            title = _plain(page_read.get("item"))
            if title and per_page[(half.file, half.file_page or half.page)] == 1:
                # The page's item is this drawing's only when it is the page's one vendor drawing.
                across_context.index_titles[half.ref] = title
    across_context.word_limit = max(COMMON_WORD_MIN, COMMON_WORD_SHARE * len(mine_vendor))

    def own_area(half: _Half) -> tuple[list[_Half], tuple[Drawing, ...]]:
        """The drawings of a vendor drawing's own area (GV's typed area, or its own page), and
        the architect's among them and on a page paired with it, the governing one first."""
        entry = by_set_page[half.page]
        key = _area_key(entry, known)
        same_area = halves_by_area.get(key, []) if key is not None else []
        if key is None:
            # No area typed: only the drawings on its own page belong with it.
            same_area = [h for hs in halves_by_area.values() for h in hs if h.page == half.page]
        pair = entry.get("pair_page")
        paired = (
            [h for hs in halves_by_area.values() for h in hs if h.page == pair]
            if isinstance(pair, int)
            else []
        )
        architect = _governing_first(
            [drawings[h.ref] for h in [*same_area, *paired] if h.role == "architect"], half.page
        )
        return same_area, architect

    across_pairs: dict[str, AcrossPair] = {}
    groups: dict[str, list[_Half]] = {}
    by_item = {c.id: c for c in items_across}
    for half in vendor_halves:
        if not half.from_upload and (not separate_items or own_area(half)[1]):
            continue
        decided = _pair_across(
            drawings[half.ref],
            items_across,
            facts_of,
            typed.get((half.file, half.file_page or half.page), frozenset()),
            answers.get(half.ref),
            _plain(by_set_page[half.page].get("section")) or None,
            memo=memo,
            context=across_context,
        )
        if decided is None:
            continue
        across_pairs[half.ref] = decided
        if decided.chosen and half.from_upload:
            # The group is named by the governing architect drawing its rounds were paired with.
            governing = _governing_first(_paired_drawings(decided, by_item), half.page)
            groups.setdefault(governing[0].ref, []).append(half)
    group_of = {h.ref: key for key, hs in groups.items() for h in hs}
    by_view = {v.ref: v for c in items_across for v in c.views}

    items: list[WorkItem] = []
    alone: list[WorkItem] = []
    round_groups: list[tuple[str, int, int, str]] = []
    used_architect: dict[int, list[str]] = {}
    used_reference: dict[int, list[str]] = {}
    for half in vendor_halves:
        entry = by_set_page[half.page]
        key = _area_key(entry, known)
        same_area, architect = own_area(half)
        pairing: str | None = None
        if half.ref in across_pairs:
            crossed = across_pairs[half.ref]
            pairing = crossed.why
            architect = _governing_first(_paired_drawings(crossed, by_item), half.page)
            if not crossed.chosen:
                pairing = f"{crossed.why} (code)"
        reference = tuple(
            drawings[h.ref] for h in same_area if h.role == "brand_prototype" and h.ref
        )
        vendor = drawings[half.ref]
        prototype = False
        if not architect and reference and not half.from_upload:
            # R1 (Anant, 2026-10-20): the architect gives no elevation of this area, so its brand
            # prototype elevation is the reference (as GV's reviewer does). Wherever the architect
            # gives a drawing, the architect governs and the prototype stays reference only.
            architect, reference, prototype = _governing_first(reference, half.page), (), True
        alone_drawing = not architect
        if alone_drawing:
            skip(
                half.page,
                pairing
                or (
                    "no architect drawing for this area in the set, so there is nothing to "
                    "compare it with (code)"
                ),
                half.ref,
            )
            # Still an item of its own for GV's rules that need only the vendor's values
            # (`WorkList.alone`): never sent to a compare call.
        # Rounds within the area: a drawing with no round in an area that has rounds is never
        # taken as the newest (verify trusts only the newest round).
        others = [
            h for h in same_area if key is not None and h.role == "vendor" and h.ref != half.ref
        ]
        if half.ref in group_of:
            others = [h for h in groups[group_of[half.ref]] if h.ref != half.ref]
        later = [
            h.ref
            for h in others
            if h.round is not None and (half.round is None or h.round > half.round)
        ]
        earlier = [
            h.ref
            for h in others
            if h.round is not None and half.round is not None and h.round < half.round
        ]
        newest = not later
        owner = _area_owner(entry, known) if half.file == file else entry
        text = " ".join(
            [
                _plain(owner.get("section")),
                _plain(owner.get("area")),
                *_phrases_in(
                    dims_pages.get((half.file, half.file_page or half.page)), vendor.crop_box
                ),
                *vendor.notes,
                *vendor.tags,
            ]
        )
        products = tuple(sorted(named_products(text, hints=True)))
        notes_page = notes_pages.get((half.file, half.file_page or half.page)) or {}
        only_tags = tuple(
            _plain(t.get("text") if isinstance(t, Mapping) else t)
            for t in notes_page.get("architect_only_tags") or []
        )
        hard: list[str] = []
        if not vendor.labels:
            hard.append("the vendor drawing has no numbered labels (code read none of its numbers)")
        if any(not d.labels for d in architect):
            hard.append(
                ("the brand prototype drawing" if prototype else "an architect drawing")
                + " has no numbered labels (hand lettering)"
            )
        if len(vendor.labels) > DENSE_LABELS:
            hard.append(f"a dense drawing: {len(vendor.labels)} vendor labels")
        made = WorkItem(
            id=half.ref,
            file=half.file,
            page=half.page,
            section=_plain(owner.get("section")) or None,
            area=_plain(owner.get("area")) or None,
            vendor=vendor,
            architect=architect,
            reference=reference,
            round=half.round,
            newest_round=newest,
            earlier_rounds=tuple(earlier),
            later_rounds=tuple(later),
            products=products,
            architect_only_tags=tuple(t for t in only_tags if t),
            hard=tuple(hard),
            paired_by=pairing,
            prototype_reference=prototype,
            opposite_hand=(
                across_pairs[half.ref].opposite_hand if half.ref in across_pairs else None
            ),
            views_disagree=(
                across_pairs[half.ref].views_disagree if half.ref in across_pairs else None
            ),
        )
        if alone_drawing:
            alone.append(made)
            continue
        items.append(made)
        if half.ref in group_of and len(groups[group_of[half.ref]]) > 1:
            round_groups.append(
                (half.file, half.file_page or half.page, half.page, group_of[half.ref])
            )
        for drawing in () if prototype else architect:
            used_architect.setdefault(drawing.page, []).append(half.ref)
        for drawing in (*reference, *(architect if prototype else ())):
            used_reference.setdefault(drawing.page, []).append(half.ref)

    # Architect and prototype drawings no item looks at: the vendor did not submit that area.
    looked = {d.ref for item in items for d in (*item.architect, *item.reference)}
    # A view of a sheet stands for the sheet's drawing.
    looked |= {ref.rsplit(".view", 1)[0] for ref in looked if ref in by_view}
    across_refs = {h.ref for h in across}
    separate_file = any(h.file != file for h in across)
    for ref, drawing in drawings.items():
        if drawing.role != "vendor" and ref not in looked:
            if drawing.file != file:
                reason = "the architect's drawing no vendor drawing was paired with (code)"
            elif ref in across_refs and separate_file:
                reason = (
                    "an architect's drawing inside the vendor's file: the architect's own file "
                    "is compared instead (code)"
                )
            elif ref in across_refs:
                reason = "the architect's drawing no vendor drawing was paired with (code)"
            else:
                reason = (
                    f"the {drawing.role.replace('_', ' ')}'s drawing of an area with no vendor "
                    "drawing in this set: the vendor did not submit it (code)"
                )
            skipped.append(
                Skipped(
                    file=drawing.file,
                    page=drawing.own_page,
                    drawing=ref,
                    reason=reason,
                )
            )

    on_page: dict[int, list[str]] = {}
    for item in items:
        on_page.setdefault(item.page, []).append(item.id)
    uncovered: list[int] = []
    for entry in mine:
        page = int(entry["page"])
        if page in cover:
            continue
        if page in on_page:
            cover[page] = PageCover(page=page, how="vendor", items=tuple(on_page[page]))
        elif page in used_architect:
            cover[page] = PageCover(
                page=page, how="architect", items=tuple(dict.fromkeys(used_architect[page]))
            )
        elif page in used_reference:
            cover[page] = PageCover(
                page=page, how="reference", items=tuple(dict.fromkeys(used_reference[page]))
            )
        else:
            reasons = [s.reason for s in skipped if s.page == page and s.file == file]
            if reasons:
                cover[page] = PageCover(page=page, how="skipped", reason="; ".join(reasons))
            else:
                uncovered.append(page)
    return WorkList(
        file=file,
        items=tuple(items),
        alone=tuple(alone),
        skipped=tuple(skipped),
        pages=tuple(cover[p] for p in sorted(cover)),
        uncovered=tuple(uncovered),
        across=tuple(across_pairs.values()),
        round_groups=tuple(round_groups),
    )


_PROJECT_SECTION: Final = re.compile(r"^\s*\d{1,3}\s*[.)]\s*\S")


def _places(entries: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int], str]:
    """Each page's place in its file's divider pages (#1798): the project (a divider whose title
    is not a numbered section: "PROJECT A MILLWORK", "7") and the section ("1. STRAIGHT RUNS /
    2 WALLS") GV typed, as `pairing.section_key`. A page under no section has none."""
    out: dict[tuple[str, int], str] = {}
    by_file: dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        by_file.setdefault(str(entry.get("file")), []).append(entry)
    for name, pages in by_file.items():
        project = ""
        for entry in sorted(pages, key=lambda e: int(e["page"])):
            section = _plain(entry.get("section"))
            if not entry.get("halves") and section and not _PROJECT_SECTION.match(section):
                project = section
                continue
            key = pairing_module.section_key(project, section) if section else None
            if key is not None and section != project:
                out[(name, int(entry["page"]))] = key
    return out


def _typed_titles(annotations: Any) -> dict[tuple[str, int], frozenset[str]]:
    """The text boxes GV typed on each page, normalised (`pairing.title_key`). Only a box that
    is exactly an architect item's title is used (`_pair_across`): never a review comment."""
    out: dict[tuple[str, int], set[str]] = {}
    for one in annotations if isinstance(annotations, list) else []:
        if not isinstance(one, Mapping) or one.get("kind") != "FreeText":
            continue
        if not isinstance(one.get("page"), int):
            continue
        key = pairing_module.title_key(one.get("text"))
        if key:
            out.setdefault((str(one.get("file")), int(one["page"])), set()).add(key)
    return {where: frozenset(found) for where, found in out.items()}


def _inches_of(value: object) -> Fraction | None:
    try:
        found = Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return found if found > 0 else None


def _printed_words(
    dims_pages: Mapping[tuple[str, int], Mapping[str, Any]], annotations: Any
) -> dict[tuple[str, int], str]:
    """Each page's printed words: code's phrases and GV's typed boxes."""
    out: dict[tuple[str, int], list[str]] = {}
    for where, page in dims_pages.items():
        out.setdefault(where, []).extend(
            _plain(p.get("text")) for p in page.get("phrases") or [] if isinstance(p, Mapping)
        )
    for one in annotations if isinstance(annotations, list) else []:
        if (
            isinstance(one, Mapping)
            and one.get("kind") == "FreeText"
            and isinstance(one.get("page"), int)
        ):
            out.setdefault((str(one.get("file")), int(one["page"])), []).append(
                _plain(one.get("text"))
            )
    return {where: " ".join(words) for where, words in out.items()}


def _index_pages(document: Any) -> dict[str, Mapping[str, Any]]:
    """The set index's checked page reads (`set_index.load`'s shape), empty when there is none."""
    pages = document.get("pages") if isinstance(document, Mapping) else None
    return {str(k): v for k, v in (pages or {}).items() if isinstance(v, Mapping)}


def _saved_answers(document: Any) -> dict[str, tuple[tuple[str, ...], pairing_module.MatchAnswer]]:
    """The AI page matcher's saved answers (`page_match.MATCH_FILE`): per vendor drawing, the
    candidates it was shown and its answer."""
    out: dict[str, tuple[tuple[str, ...], pairing_module.MatchAnswer]] = {}
    found = document.get("answers") if isinstance(document, Mapping) else None
    for ref, entry in (found or {}).items() if isinstance(found, Mapping) else ():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("matches"), list):
            continue
        shown = entry.get("candidates")
        if not isinstance(shown, list):
            continue
        labels = entry.get("labels")
        out[str(ref)] = (
            tuple(str(c) for c in shown),
            pairing_module.MatchAnswer(
                matches=tuple(str(m) for m in entry["matches"]),
                confident=bool(entry.get("confident")),
                reason=_without_labels(
                    _plain(entry.get("reason"))[:200],
                    labels if isinstance(labels, Mapping) else {},
                ),
            ),
        )
    return out


#: The AI page matcher's own picture labels ("A5"): private to its question (`page_match`).
_MATCH_LABEL: Final = re.compile(r"\bA\d{1,3}\b(?!\.?\d)")
#: A word before "A2" that makes it a name printed on the drawings ("UNIT A2", "SECTION A5"), not
#: the matcher's picture label.
_PRINTED_NAME: Final = re.compile(
    r"\b(?:unit|type|section|detail|sheet|elevation|view|room|ref|no|number|drawing|plan)\.?\s*$",
    re.IGNORECASE,
)


def _without_labels(reason: str, labels: Mapping[str, Any]) -> str:
    """The matcher's reason with its picture labels ("the plan on A5") turned into the architect
    item each one stands for (`p1009`, then said as a page a person finds, `_human_ids`). When one
    could be a name printed on the drawings ("UNIT A2", "section A5"), or is no label it was
    shown, which page it means is not known: the reason is left out (a wrong page named would
    mislead the reviewer who picks)."""
    found = list(_MATCH_LABEL.finditer(reason))
    if any(m.group(0) not in labels or _PRINTED_NAME.search(reason[: m.start()]) for m in found):
        return ""
    return _MATCH_LABEL.sub(lambda m: str(labels[m.group(0)]), reason)


def _items_across(
    across: Sequence[_Half],
    drawings: Mapping[str, Drawing],
    file: str,
    architect_files: frozenset[str],
    by_set_page: Mapping[int, Mapping[str, Any]],
    facts_of: Callable[[Drawing], pairing_module.Facts],
) -> list[AcrossCandidate]:
    """The architect items a vendor drawing may be paired with across the uploads: the drawings
    of each page of the architect's own file; only when it has none, the pages of another file
    (or of the vendor's file) whose title names the architect (#1723)."""
    named = [h for h in across if h.ref]
    own = [h for h in named if h.file in architect_files] or [h for h in named if h.file != file]
    pool = own or named
    pages: dict[int, list[Drawing]] = {}
    for half in pool:
        pages.setdefault(half.page, []).append(drawings[half.ref])
    out: list[AcrossCandidate] = []
    for page, found in sorted(pages.items()):
        entry = by_set_page.get(page) or {}
        first = found[0]
        out.append(
            AcrossCandidate(
                id=f"p{page}",
                file=first.file,
                page=first.own_page,
                drawings=tuple(found),
                title=_plain(entry.get("area")) or None,
                section=_plain(entry.get("section")) or None,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class _KeyPlan:
    """A key plan's printed item tags and callouts (`views.callouts`)."""

    item: str
    """The key plan's item id."""
    page: int
    project: str | None
    tags: tuple[tuple[str, str, Box], ...]
    """Each tag printed on it: normalised (`pairing.item_tags_in`), as printed, where."""
    marks: tuple[views_module.Callout, ...]


@dataclass(slots=True)
class _Across:
    """What code knows of the architect's items across the uploads, beyond each item's facts:
    the projects of GV's divider pages, the key plans, each project's sheets and views, and how
    many of the vendor's drawings print each item-like tag."""

    places: Mapping[tuple[str, int], projects_module.Place] = field(default_factory=dict)
    projects: frozenset[str] = frozenset()
    """The architect's projects (normalised), the brand prototype's left out."""
    plans: list[_KeyPlan] = field(default_factory=list)
    sheets: dict[tuple[str | None, str, str], str] = field(default_factory=dict)
    """(project, sheet number, view number) -> the view's drawing ref (sheet numbers repeat
    from one project to the next: each project's set has its own IN7.0)."""
    view_page: dict[str, str] = field(default_factory=dict)
    """A view's drawing ref -> its item's id."""
    tag_drawings: Counter[str] = field(default_factory=Counter)
    """How many of the vendor's drawings print each item-like tag."""
    item_limit: float = math.inf
    """An item-like tag printed on more of the vendor's drawings than this names a finish."""
    marked: dict[str, tuple[frozenset[str], frozenset[str]]] = field(default_factory=dict)
    """A drawing's variant and opposite-hand words the set index read on it (checked printed),
    by the drawing's ref: on top of code's own (`pairing.markers_in`)."""
    no_sizes: set[str] = field(default_factory=set)
    """The views the set index says print no sizes of the piece (an outline, a schedule), and
    the plans whose every size code reads is a room or wall size (`room_only`)."""
    room_only: set[str] = field(default_factory=set)
    """Of those, the plans that print only room and wall sizes (`_room_only`): they print sizes,
    none of a piece."""
    word_drawings: Counter[str] = field(default_factory=Counter)
    """How many of the vendor's drawings print each naming word."""
    index_titles: dict[str, str] = field(default_factory=dict)
    """The item title the set index read on a drawing's page (checked printed), by the drawing's
    ref: a whole page's drawings only, never a view of a sheet (`pairing.Facts.index_title`)."""
    word_limit: float = math.inf
    """A naming word printed on more of the vendor's drawings than this is its title block's or
    its brand's (`COPYRIGHT`, `DRAWN`, the brand's name): no evidence of which piece a drawing shows."""

    def item_tags(self) -> frozenset[str]:
        """The vendor's item tags (`ITEM_TAG_SHARE`): every item-like tag it prints on few of its
        drawings."""
        return frozenset(t for t, n in self.tag_drawings.items() if n <= self.item_limit)


def _view_drawings(
    drawing: Drawing,
    dims_page: Mapping[str, Any] | None,
    notes_page: Mapping[str, Any] | None,
    found: Sequence[views_module.View],
    boxed: bool = False,
) -> tuple[Drawing, ...]:
    """Each titled view of an architect sheet as its own drawing (`views.find_views`): its full
    title, the labels, tags and notes printed in it (`views.owner`), its printed scale. `boxed`:
    the views' `title_box` is the whole view's box (the set index's read, checked by code), and a
    printed thing belongs to the view whose box holds it."""
    if len(found) < 2:
        return ()
    printed = views_module.phrases_of(dims_page, drawing.box)
    block = views_module.title_block_left(printed)
    edge = math.inf if block is None else block

    def owner(box: Box) -> int | None:
        point = _centre(box)
        if point[0] >= edge:
            return None
        if boxed:
            return next(
                (n for n, v in enumerate(found) if _distance(point, v.title_box) == 0), None
            )
        return views_module.owner(point, found)

    owned_labels: dict[int, list[Label]] = {}
    for label in drawing.labels:
        at = owner(label.box)
        if at is not None:
            owned_labels.setdefault(at, []).append(label)
    owned_boxes: dict[int, list[Box]] = {}
    owned_text: dict[int, list[str]] = {}
    for text, box in printed:
        at = owner(box)
        if at is not None:
            owned_boxes.setdefault(at, []).append(box)
            owned_text.setdefault(at, []).append(text)
    owned_tags: dict[int, list[str]] = {}
    for one in (notes_page or {}).get("drawings") or []:
        if not isinstance(one, Mapping) or one.get("role") != "architect":
            continue
        for tag in one.get("tags") or []:
            where = _box(tag.get("box")) if isinstance(tag, Mapping) else None
            if where is None or _distance(_centre(where), drawing.box) > 0:
                continue
            at = owner(where)
            if at is not None:
                owned_tags.setdefault(at, []).append(_plain(tag.get("text")))
    out: list[Drawing] = []
    for n, view in enumerate(found):
        labels = tuple(owned_labels.get(n, ()))
        box = _union([view.title_box, *(lb.box for lb in labels), *owned_boxes.get(n, ())])
        note = next(
            (t for t in owned_text.get(n, ()) if views_module.is_scale(t)),
            None,
        )
        ratio = _scale_ratio(note)
        out.append(
            drawing.model_copy(
                update={
                    "ref": f"{drawing.ref}.view{n + 1}",
                    "heading": None,
                    "box": tuple(round(v, 2) for v in box),
                    "crop_box": tuple(
                        round(v, 2)
                        for v in (
                            max(0.0, box[0] - _MARGIN_PT),
                            max(0.0, box[1] - _MARGIN_PT),
                            box[2] + _MARGIN_PT,
                            box[3] + _MARGIN_PT,
                        )
                    ),
                    "labels": labels,
                    "titles": (view.title,),
                    "tags": tuple(dict.fromkeys(owned_tags.get(n, ()))),
                    "notes": (),
                    "scale_in_per_pt": (
                        round(1 / (float(ratio) * 72), 6) if ratio else drawing.scale_in_per_pt
                    ),
                    "scale_note": note or drawing.scale_note,
                    "detail": view.kind == "detail",
                }
            )
        )
    return tuple(out)


def _key_plan(
    found: Sequence[views_module.View], read: Sequence[Mapping[str, Any]], tags: int
) -> bool:
    """A key plan: a plan of a whole floor or of a building system (the set index's checked read
    says so of every view on the sheet), or a plan that tags at least `KEY_PLAN_TAGS` items (code
    counts them). It shows where pieces are, never a piece's sizes: it is never paired as an
    item's drawing, and its callouts point to the elevations (`_linked`)."""
    if read and all(v.get("kind") == "plan" and v.get("whole_floor") for v in read):
        return True
    if tags < KEY_PLAN_TAGS:
        return False
    kinds = [v.get("kind") for v in read] or [v.kind for v in found]
    return not kinds or all(k == "plan" for k in kinds)


def _index_views(
    read: Sequence[Mapping[str, Any]], found: Sequence[views_module.View]
) -> list[tuple[int | None, Mapping[str, Any]]]:
    """Each view of the set index's read with the code-found view it is (same number, or a title
    of the same words), `None` when code found none."""
    out: list[tuple[int | None, Mapping[str, Any]]] = []
    for one in read:
        title = projects_module.key(one.get("title"))
        number = str(one.get("number") or "")
        at = next(
            (
                n
                for n, v in enumerate(found)
                if (
                    number
                    and v.number == number
                    and set(title.split()) & set(projects_module.key(v.title).split())
                )
                or projects_module.key(v.title) == title
            ),
            None,
        )
        out.append((at, one))
    return out


def _enrich(
    items: Sequence[AcrossCandidate],
    dims_pages: Mapping[tuple[str, int], Mapping[str, Any]],
    notes_pages: Mapping[tuple[str, int], Mapping[str, Any]],
    places: Mapping[tuple[str, int], projects_module.Place],
    index: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
) -> tuple[list[AcrossCandidate], _Across]:
    """The architect's items with their project, whether a reference set or a key plan holds
    them, and their titled views (code's, or the set index's checked boxes when code finds too
    few); and what the pairing needs across them (`_Across`)."""
    context = _Across(places=places)
    index = index or {}
    found_views: dict[str, tuple[views_module.View, ...]] = {}
    phrases: dict[str, list[views_module.Phrase]] = {}
    sheet_of: dict[str, str] = {}
    out: list[AcrossCandidate] = []
    for item in items:
        place = places.get((item.file, item.page)) or projects_module.Place()
        dims_page = dims_pages.get((item.file, item.page))
        notes_page = notes_pages.get((item.file, item.page))
        printed = views_module.phrases_of(dims_page)
        found = views_module.find_views(printed)
        whole = len(item.drawings) == 1
        read = [
            v
            for v in (index.get((item.file, item.page)) or {}).get("views") or []
            if isinstance(v, Mapping)
        ]
        boxed = False
        if len(found) < 2:
            from_read = [
                views_module.View(
                    title=str(v.get("title")),
                    number=str(v.get("number") or "") or None,
                    sheet=None,
                    title_box=tuple(float(x) for x in v["box"]),  # type: ignore[arg-type]
                    kind=str(v.get("kind")) if v.get("kind") in ("plan", "elevation") else None,
                )
                for v in read
                if isinstance(v.get("box"), list) and len(v["box"]) == 4
            ]
            if len(from_read) >= 2:
                found, boxed = tuple(from_read), True
                read = [v for v in read if isinstance(v.get("box"), list)]
        views = (
            _view_drawings(item.drawings[0], dims_page, notes_page, found, boxed) if whole else ()
        )
        page_read = index.get((item.file, item.page)) or {}
        title = _plain(page_read.get("item"))
        if title:
            for drawing in item.drawings:
                context.index_titles[drawing.ref] = title
        if not views:
            words = _marked_words(page_read)
            for drawing in item.drawings:
                context.marked[drawing.ref] = words
        placed = 0
        for at, one in _index_views(read, found):
            if at is None or at >= len(views):
                continue
            placed += 1
            context.marked[views[at].ref] = _marked_words(one)
            if not one.get("prints_sizes"):
                context.no_sizes.add(views[at].ref)
            if one.get("kind") == "schedule":
                context.no_sizes.add(views[at].ref)
            if (
                one.get("kind") == "plan"
                and one.get("prints_sizes")
                and (_CEILING_PLAN.search(str(one.get("title") or "")) or _room_only(views[at]))
            ):
                # A plan whose every size is a room or wall size (a unit plan), or a reflected
                # ceiling plan (its sizes place lights and soffits): it prints sizes, none of a
                # piece (owner, 2026-10-23).
                context.no_sizes.add(views[at].ref)
                context.room_only.add(views[at].ref)
        if read and not any(v.get("prints_sizes") for v in read):
            context.no_sizes.add(item.id)
        elif (
            read
            and views
            and placed == len(read) == len(views)
            and all(v.ref in context.no_sizes for v in views)
            and any(v.ref in context.room_only for v in views)
        ):
            # Every view read and placed: elevations with no sizes, plans with room sizes only.
            context.no_sizes.add(item.id)
        tags = pairing_module.item_tags_in(
            [
                _plain(t.get("text"))
                for d in (notes_page or {}).get("drawings") or []
                if isinstance(d, Mapping)
                for t in d.get("tags") or []
                if isinstance(t, Mapping)
            ]
        )
        key_plan = whole and _key_plan(found, read, len(tags))
        found_views[item.id] = found if views else ()
        phrases[item.id] = printed
        sheet = views_module.sheet_number(printed, found)
        if sheet is not None:
            sheet_of[item.id] = sheet
        for view, drawing in zip(found, views, strict=False):
            context.view_page[drawing.ref] = item.id
            if sheet is not None and view.number and (view.sheet in (None, sheet)):
                context.sheets.setdefault((place.project, sheet, view.number), drawing.ref)
        out.append(
            item.model_copy(
                update={
                    "project": place.name,
                    "reference": place.reference,
                    "key_plan": key_plan,
                    "views": views,
                }
            )
        )
    context.projects = frozenset(
        p for item in out if not item.reference and (p := _project_of(item)) is not None
    )
    sheets = frozenset(sheet_of.values())
    for item in out:
        if not item.key_plan:
            continue
        notes_page = notes_pages.get((item.file, item.page))
        printed_tags: list[tuple[str, str, Box]] = []
        for one in (notes_page or {}).get("drawings") or []:
            for tag in one.get("tags") or [] if isinstance(one, Mapping) else []:
                box = _box(tag.get("box")) if isinstance(tag, Mapping) else None
                text = _plain(tag.get("text")) if isinstance(tag, Mapping) else ""
                keys = pairing_module.item_tags_in([text])
                if box is not None and len(keys) == 1:
                    printed_tags.append((next(iter(keys)), text, box))
        context.plans.append(
            _KeyPlan(
                item=item.id,
                page=item.page,
                project=_project_of(item),
                tags=tuple(printed_tags),
                marks=views_module.callouts(phrases[item.id], (), sheets),
            )
        )
    return out, context


#: A plan's string that runs wall to wall: its sizes add up to at least this (inches, 16 feet: a
#: room or a unit; a run of millwork that long is rare, and a piece's own string is shorter).
ROOM_STRING_IN: Final = Fraction(192)
#: A plan prints room and wall sizes only when code reads at least this many sizes in it.
ROOM_MIN_SIZES: Final = 4
#: A room plan may also print up to this many sizes off its room strings, each under
#: `ROOM_STRAY_UNDER_IN` (a wall's thickness) or whole feet with no inches (a graphic scale's
#: `10'`).
ROOM_MAX_STRAY: Final = 2
ROOM_STRAY_UNDER_IN: Final = Fraction(12)
_WHOLE_FEET: Final = re.compile(r"\d+\s*['\u2032]")
#: A reflected ceiling plan's title: its sizes place the ceiling's lights and soffits.
_CEILING_PLAN: Final = re.compile(r"\bRCP\b|\bREFLECTED\s+CEILING\b", re.IGNORECASE)
#: A size printed with an inch or a foot mark (a keynote's `8.1` or a door number is none).
_SIZE_MARK: Final = re.compile(r"[\"'\u2032\u2033]")


def _room_only(view: Drawing) -> bool:
    """Whether every size code reads in a plan is a room or wall size (owner, 2026-10-23: tell
    room and wall sizes from piece sizes): each printed size (an inch or foot mark, read from
    the text) lies on a string (one dimension line: the same direction, lined up within
    `_ROW_PT`) whose sizes add up to `ROOM_STRING_IN` or more, wall to wall. A piece's size
    (a counter's 25 1/2", a cabinet's 30") on a string of its own fails it. At least
    `ROOM_MIN_SIZES` sizes; any label not read from the text, or turned, fails it too: when
    unsure, the plan keeps its sizes."""
    sizes: list[Label] = []
    for label in view.labels:
        text = " ".join(str(label.text or "").split())
        if not _SIZE_MARK.search(text):
            if re.fullmatch(r"\d+(?:\.\d+)?", text):
                continue  # a keynote or a door number, no size
            return False
        if (label.route or "text") != "text" or label.orientation not in ("h", "v"):
            return False
        if _inches_of(label.inches) is None:
            return False
        sizes.append(label)
    if len(sizes) < ROOM_MIN_SIZES:
        return False
    stray = 0
    on_room_strings = 0
    for label in sizes:
        across = 1 if label.orientation == "h" else 0
        middle = (label.box[across] + label.box[across + 2]) / 2
        row = [
            other
            for other in sizes
            if other.orientation == label.orientation
            and abs((other.box[across] + other.box[across + 2]) / 2 - middle) <= _ROW_PT
        ]
        if sum((_inches_of(o.inches) or Fraction(0)) for o in row) >= ROOM_STRING_IN:
            on_room_strings += 1
            continue
        value = _inches_of(label.inches) or Fraction(0)
        text = " ".join(str(label.text or "").split())
        if value < ROOM_STRAY_UNDER_IN or _WHOLE_FEET.fullmatch(text):
            # A wall's thickness, or a graphic scale's "10'": no piece's size.
            stray += 1
            continue
        return False
    return stray <= ROOM_MAX_STRAY and on_room_strings >= ROOM_MIN_SIZES


def _marked_words(read: Mapping[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
    """The variant and opposite-hand words of a set index read (checked printed), in capitals."""
    return (
        frozenset(" ".join(str(w).upper().split()) for w in read.get("variant_words") or []),
        frozenset(" ".join(str(w).upper().split()) for w in read.get("hand_words") or []),
    )


def _with_marks(
    facts_of: Callable[[Drawing], pairing_module.Facts], context: _Across
) -> Callable[[Drawing], pairing_module.Facts]:
    """`facts_of` with the set index's variant and hand words added to code's own."""

    def facts(drawing: Drawing) -> pairing_module.Facts:
        found = facts_of(drawing)
        title = context.index_titles.get(drawing.ref)
        if title:
            # The hand words the title's tidy-up leaves out ("REVERSED UNITS") always flag it.
            found = dataclasses.replace(
                found,
                index_title=title,
                hand=found.hand | pairing_module.markers_in([title])[1],
            )
        extra = context.marked.get(drawing.ref)
        if not extra or not (extra[0] or extra[1]):
            return found
        return dataclasses.replace(
            found, markers=found.markers | extra[0], hand=found.hand | extra[1]
        )

    return facts


def _project_of(item: AcrossCandidate) -> str | None:
    return projects_module.key(item.project) if item.project else None


def _item_facts(
    item: AcrossCandidate, facts_of: Callable[[Drawing], pairing_module.Facts], section: str | None
) -> tuple[pairing_module.Facts, list[pairing_module.Facts]]:
    """An architect item's facts: its drawings' together, its typed title's words and key."""
    members = [facts_of(d) for d in item.drawings]
    title = pairing_module.title_key(item.title) if item.title else None
    merged = pairing_module.Facts(
        ref=item.id,
        values=tuple(v for m in members for v in m.values),
        references=frozenset(r for m in members for r in m.references),
        oriented=tuple(v for m in members for v in m.oriented),
        tags=frozenset(t for m in members for t in m.tags),
        words=frozenset(w for m in members for w in m.words)
        | pairing_module.words_in([item.title or ""]),
        views=frozenset(v for m in members for v in m.views),
        section=section,
        title=title,
        view_count=max(1, len(item.views)),
        whole_sheet=bool(item.views),
        markers=frozenset(w for m in members for w in m.markers),
        hand=frozenset(w for m in members for w in m.hand),
        index_title=next((m.index_title for m in members if m.index_title), None),
        place=(item.file, item.page),
    )
    return merged, members


def _view_facts(
    item: AcrossCandidate, facts_of: Callable[[Drawing], pairing_module.Facts], section: str | None
) -> list[pairing_module.Facts]:
    """Each titled view's facts (its own words, tags and sizes; its full title as its title)."""
    return [
        dataclasses.replace(
            facts_of(view),
            section=section,
            title=pairing_module.title_key(view.titles[0]) if view.titles else None,
            view_count=len(item.views),
        )
        for view in item.views
    ]


def _needed(why: str) -> str:
    """A reason that the architect's drawing is missing, said up front: no paid compare."""
    return f"{pairing_module.UNPAIRED}: {DRAWING_NEEDED}: {why}"


def _vendor_project(vendor: Drawing, context: _Across) -> tuple[str | None, str | None]:
    """The architect's project the vendor drawing is in (normalised), from the set index (the
    project its page prints, or the page introducing it prints), matched to the architect's
    projects (`projects.match`); or `None`, and the reason when its project is in the vendor's
    file but not in the architect's (a drawing needed)."""
    if not context.projects:
        return None, None
    place = context.places.get((vendor.file, vendor.own_page)) or projects_module.Place()
    if place.project is None:
        return None, None
    siblings = {
        p.project
        for (file, _page), p in context.places.items()
        if file == vendor.file and p.project is not None
    }
    mine = projects_module.match(place.project, context.projects, siblings)
    if mine is None:
        return None, _needed(
            f"its page is in {place.name}, and the architect's file has no drawings of that project"
        )
    return mine, None


def _pair_across(
    vendor: Drawing,
    candidates: Sequence[AcrossCandidate],
    facts_of: Callable[[Drawing], pairing_module.Facts],
    typed: frozenset[str],
    saved: tuple[tuple[str, ...], pairing_module.MatchAnswer] | None,
    section: str | None = None,
    *,
    memo: RankMemo | None = None,
    context: _Across | None = None,
) -> AcrossPair | None:
    """How a vendor drawing is paired across the uploads (#1798), or `None` when there is no
    architect item to pair it with. Code ranks every item (`pairing.rank`): a title GV typed or
    the vendor printed that is an item's title, the same section of the divider pages, printed
    references, spans lined up, tags and naming words, sizes, a key plan's callout beside its
    item tag. Its own clear pick (`pairing.clear_winner`, or a printed reference / the same
    printed values, `pairing.decide`) and the AI page matcher's saved answer then meet in
    `pairing.agree`; a sheet of several titled views is then narrowed to one view
    (`pairing.narrow`). Only items of the vendor drawing's own project are candidates
    (`projects`), never the brand prototype (R1: reference only) nor a key plan. `memo`: the
    ranking of an earlier build (only the answer is new: the agreement rule runs again on it)."""
    context = context or _Across()
    facts_of = _with_marks(facts_of, context)
    mine_facts = facts_of(vendor)
    everything = [
        c for c in candidates if c.drawings and all(d.ref != vendor.ref for d in c.drawings)
    ]
    if not everything:
        return None
    project, refused = _vendor_project(vendor, context)
    pool = [
        c
        for c in everything
        if not c.reference
        and not c.key_plan
        and (project is None or _project_of(c) in (project, None))
        and refused is None
    ]
    item_tags = (mine_facts.item_tags or frozenset()) & context.item_tags()
    # An item-like tag the vendor prints on many of its drawings is a material (`LM-004`, a
    # laminate on a third of a real set's drawings): no evidence of which piece this one is.
    common = (mine_facts.item_tags or frozenset()) - item_tags
    # So is a word it prints on many of its drawings (its title block's, its brand's).
    boiler = frozenset(
        w for w in mine_facts.words if context.word_drawings.get(w, 0) > context.word_limit
    )
    if not pool:
        why = (
            refused
            or _honest_unpaired(vendor, everything, item_tags, project, facts_of)
            or (
                f"{pairing_module.UNPAIRED}: the architect's file holds no drawing it may be "
                "compared with (only the brand prototype or key plans)"
            )
        )
        return AcrossPair(
            vendor=vendor,
            section=None,
            candidates=(),
            why=why,
            needed=f": {DRAWING_NEEDED}: " in why,
        )
    if all(c.id in context.no_sizes for c in pool):
        # The set index read every drawing of its project: none prints a piece's sizes.
        return AcrossPair(
            vendor=vendor,
            section=None,
            candidates=(),
            why=_needed(
                f"the architect's drawings of its project ({len(pool)} pages) print no sizes of "
                "any piece (outlines, schedules or plans only)"
            ),
            needed=True,
        )
    linked = _linked(item_tags, pool, context)
    key = (vendor.ref, tuple(c.id for c in pool))
    known = memo.get(key) if memo is not None else None
    if known is None:
        known = _ranked_across(vendor, pool, facts_of, typed, item_tags, linked, common, boiler)
        if memo is not None:
            memo[key] = known
    ranked, code_pick = known
    asked = tuple(s.ref for s in ranked if s.contradicts is None)
    answer = None
    if saved is not None:
        answer = pairing_module.reuse(saved[0], saved[1], asked)
    disagree = _views_disagree(mine_facts, ranked, {c.id: c for c in pool}, facts_of)
    if disagree is not None and (
        answer is None or {disagree.governing_ref, disagree.other_ref} & set(answer.matches)
    ):
        # The architect's own plan and elevation of the piece print different sizes: compared
        # with the view that governs (GV's view roles), and GV asks the architect.
        governing = disagree.governing_ref.split(".")[0]
        other = disagree.other_ref.split(".")[0]
        sizes = "; ".join(
            f"{_inch(o.inches)} on its {disagree.other} against {_inch(g.inches)} on its "
            f"{disagree.governing}"
            for g, o in disagree.spans
        )
        return AcrossPair(
            vendor=vendor,
            section=section if mine_facts.section is not None else None,
            candidates=tuple(
                pool_item.model_copy(
                    update={
                        "score": s.score,
                        "evidence": s.evidence,
                        "contradicts": s.contradicts,
                        "strong": s.strong,
                    }
                )
                for s in ranked
                if (pool_item := {c.id: c for c in pool}.get(s.ref)) is not None
            ),
            code_pick=governing,
            asked=asked,
            answer=None if answer is None else answer.matches,
            chosen=(governing,),
            why=(
                f"code paired it with {governing}, the architect's {disagree.governing}, which "
                f"governs {disagree.governs}; the architect's own drawings disagree ({other}, its "
                f"{disagree.other}, shows the same piece: {sizes}): GV asks the architect, and "
                "these sizes are never red against the vendor"
            ),
            views_disagree=disagree,
        )
    if not asked:
        chosen: tuple[str, ...] = ()
        why = f"{pairing_module.UNPAIRED}: every architect item is ruled out by code ({ranked[0].contradicts})"
    else:
        chosen, why = pairing_module.agree(ranked, code_pick, answer)
    by_id = {c.id: c for c in pool}
    picked_views: list[str] = []
    whole: set[str] = set()
    """The chosen items of several views compared whole (their page is titled the vendor's item)."""
    if chosen:
        kept: list[str] = []
        failed: list[str] = []
        shown: list[str] = []
        for item_id in chosen:
            item = by_id[item_id]
            if not item.views:
                kept.append(item_id)
                continue
            own = {v.ref for v in item.views}
            vendor_facts = dataclasses.replace(
                mine_facts,
                tags=mine_facts.tags - common,
                words=mine_facts.words - boiler,
                item_tags=item_tags,
                linked={r: w for r, w in linked.items() if r in own},
            )
            item_views = _view_facts(item, facts_of, _section_of(item, facts_of))
            best, problem = pairing_module.narrow(vendor_facts, item_views)
            page_title = _item_facts(item, facts_of, None)[0].index_title
            if (
                best is None
                and pairing_module.same_item(mine_facts.index_title, page_title)
                and not _view_ruled_out(vendor_facts, item_views, page_title)
            ):
                # The page's own title names the vendor's item exactly: the whole page is its
                # drawing, whichever of its views shows the piece.
                kept.append(item_id)
                whole.add(item_id)
                shown.append(f"the whole of {item_id}: its page is titled the vendor's item")
                continue
            if best is None:
                failed.append(f"{item_id} shows it ({why}), but {problem}")
                continue
            kept.append(item_id)
            picked_views.append(best.ref)
            view = next(v for v in item.views if v.ref == best.ref)
            title = view.titles[0] if view.titles else best.ref
            shown.append(f'its view "{title}" on {item_id} ({pairing_module.evidence_words(best)})')
        if kept:
            chosen = tuple(kept)
            if shown:
                why = f"{why}; {'; '.join(shown)}"
        else:
            chosen = ()
            why = f"{pairing_module.UNPAIRED}: {failed[0]}"
    if chosen:
        paired = [
            *(v for v in picked_views),
            *(item_id for item_id in chosen if not by_id[item_id].views or item_id in whole),
        ]
        drawings_of = {v.ref: v for c in pool for v in c.views}
        sizeless = [
            ref
            for ref in paired
            if ref in context.no_sizes
            and (
                ref in context.room_only
                or sum(
                    len(d.labels)
                    for d in ((drawings_of[ref],) if ref in drawings_of else by_id[ref].drawings)
                )
                <= MAX_SIZELESS_LABELS
            )
        ]
        if sizeless and len(sizeless) == len(paired):
            where = ", ".join(
                (
                    f'"{drawings_of[r].titles[0]}" on {drawings_of[r].ref.split(".")[0]}'
                    if r in drawings_of and drawings_of[r].titles
                    else r
                )
                for r in sizeless
            )
            why = _needed(
                f"the architect's drawing of it ({where}) prints no sizes of the piece; {why}"
            )
            chosen, picked_views = (), []
    hand: str | None = None
    if chosen:
        drawings_of = {v.ref: v for c in pool for v in c.views}
        targets = [facts_of(drawings_of[r]) for r in picked_views if r in drawings_of] + [
            _item_facts(by_id[i], facts_of, None)[0]
            for i in chosen
            if not by_id[i].views or i in whole
        ]
        hand = next(
            (h for t in targets if (h := pairing_module.opposite_hand(mine_facts, t))), None
        )
        if hand is not None:
            why = f"{why}; {hand}: sizes are compared, positions from an end are not"
    if not chosen and DRAWING_NEEDED not in why and saved is not None:
        # The item, not the project: the AI page matcher looked at every architect page of the
        # project and named only drawings that give no sizes of it, and code agrees.
        shown_only = (
            _shown_only(saved[1], everything, item_tags, context, by_id, project)
            if set(by_id) <= set(saved[0])
            else None
        )
        if shown_only is not None:
            why = _needed(f"{shown_only}; {why.removeprefix(f'{pairing_module.UNPAIRED}: ')}")
    if not chosen and DRAWING_NEEDED not in why:
        honest = _honest_unpaired(vendor, everything, item_tags, project, facts_of)
        prefix = f"{pairing_module.UNPAIRED}: "
        if honest is not None:
            why = f"{honest}; {why.removeprefix(prefix)}"
    unsure = () if chosen or DRAWING_NEEDED in why else _unsure(ranked, code_pick, answer)
    if unsure:
        # The AI page matcher is sure of an architect page and code cannot confirm it: the drawing
        # is in the upload, so both pages are named in the reason, never "drawing needed".
        either = " or ".join(unsure)
        why = (
            f"{pairing_module.UNPAIRED}: {pairing_module.PAIRING_UNSURE}: {either}?; "
            f"{why.removeprefix(f'{pairing_module.UNPAIRED}: ')}"
        )
    why = _human_ids(why, everything, context)
    return AcrossPair(
        vendor=vendor,
        section=section if mine_facts.section is not None else None,
        candidates=tuple(
            by_id[s.ref].model_copy(
                update={
                    "score": s.score,
                    "evidence": s.evidence,
                    "contradicts": s.contradicts,
                    "strong": s.strong,
                }
            )
            for s in ranked
        ),
        code_pick=code_pick,
        asked=asked,
        answer=None if answer is None else answer.matches,
        chosen=chosen,
        views=tuple(picked_views),
        why=why,
        needed=f": {DRAWING_NEEDED}: " in why,
        opposite_hand=hand,
        unsure=unsure,
    )


#: A label of one of two drawings' strings lies on the same dimension line as another when their
#: centres are this close across it (points).
_ROW_PT: Final = 3.0


def _inch(value: object) -> str:
    """`72"`, `30 1/2"` from a label's inches."""
    found = _inches_of(value)
    if found is None:
        return f'{value}"'
    whole, rest = divmod(found.numerator, found.denominator)
    if not rest:
        return f'{whole}"'
    part = f"{rest}/{found.denominator}"
    return f'{whole} {part}"' if whole else f'{part}"'


def _strings(drawing: Drawing) -> list[tuple[tuple[Label, ...], Label]]:
    """The drawing's horizontal strings that add up, as code read them from the text: (its parts
    left to right, its overall). A part row is two labels or more on one dimension line; its
    overall is a label alone on its own line, over the parts, that is their exact sum."""
    rows: list[list[Label]] = []
    for label in sorted(
        (
            one
            for one in drawing.labels
            if one.orientation == "h"
            and (one.route or "text") == "text"
            and _inches_of(one.inches) is not None
        ),
        key=lambda one: (one.box[1] + one.box[3]) / 2,
    ):
        middle = (label.box[1] + label.box[3]) / 2
        if rows and abs((rows[-1][0].box[1] + rows[-1][0].box[3]) / 2 - middle) <= _ROW_PT:
            rows[-1].append(label)
        else:
            rows.append([label])
    out: list[tuple[tuple[Label, ...], Label]] = []
    for row in rows:
        if len(row) < 2:
            continue
        parts = tuple(sorted(row, key=lambda one: one.box[0]))
        total = sum((_inches_of(one.inches) or Fraction(0)) for one in parts)
        left, right = parts[0].box[0], parts[-1].box[2]
        overalls = [
            alone[0]
            for alone in rows
            if len(alone) == 1
            and left <= (alone[0].box[0] + alone[0].box[2]) / 2 <= right
            and _inches_of(alone[0].inches) == total
        ]
        if len(overalls) == 1:
            out.append((parts, overalls[0]))
    return out


def _views_disagree(
    vendor: pairing_module.Facts,
    ranked: Sequence[pairing_module.Scored],
    pool: Mapping[str, AcrossCandidate],
    facts_of: Callable[[Drawing], pairing_module.Facts],
) -> ViewsDisagree | None:
    """The architect's own plan and elevation of the vendor's piece printing different sizes for
    the same string (owner, 2026-10-23), when code is sure of it, else `None` (the pairing goes
    on as before). All must hold:

    - the vendor's drawing is an elevation (its titles name only that view);
    - code holds strong evidence that the architect's plan of one item is this piece (ruled out
      only because it is a plan), and exactly one other item, a single elevation not ruled out,
      shares a naming word with that plan;
    - both print one string of as many parts that adds up, at least one part the same and at
      least one different (`_strings`).

    The elevation governs cabinet widths (GV's view roles), so it is the drawing compared with."""
    if vendor.views != frozenset({"elevation"}):
        return None
    plans = [
        s
        for s in ranked
        if s.strong
        and s.contradicts is not None
        and s.contradicts.startswith("its title names a plan")
        and s.ref in pool
    ]
    if len(plans) != 1:
        return None
    plan_item = pool[plans[0].ref]
    if plan_item.views or len(plan_item.drawings) != 1:
        return None
    plan = plan_item.drawings[0]
    plan_facts = facts_of(plan)
    elevations = []
    for scored in ranked:
        item = pool.get(scored.ref)
        if scored.contradicts is not None or item is None or item.views or len(item.drawings) != 1:
            continue
        facts = facts_of(item.drawings[0])
        if facts.views == frozenset({"elevation"}) and facts.words & plan_facts.words:
            elevations.append(item.drawings[0])
    if len(elevations) != 1:
        return None
    elevation = elevations[0]
    found: list[list[tuple[str, Label, Label]]] = []
    for e_parts, e_overall in _strings(elevation):
        for p_parts, p_overall in _strings(plan):
            if len(e_parts) != len(p_parts):
                continue
            count = len(e_parts)
            spans = [
                (f"width of part {n} of {count}", e, p)
                for n, (e, p) in enumerate(zip(e_parts, p_parts, strict=True), start=1)
                if _inches_of(e.inches) != _inches_of(p.inches)
            ]
            if not spans or len(spans) == count:
                continue
            if _inches_of(e_overall.inches) != _inches_of(p_overall.inches):
                spans.insert(0, ("overall width", e_overall, p_overall))
            found.append(spans)
    if len(found) != 1:
        return None
    return ViewsDisagree(
        governing="elevation",
        other="plan",
        governs="cabinet widths",
        governing_ref=elevation.ref,
        other_ref=plan.ref,
        file=elevation.file,
        governing_page=elevation.own_page,
        other_page=plan.own_page,
        spans=tuple((e, p) for _name, e, p in found[0]),
        names=tuple(name for name, _e, _p in found[0]),
        title=(elevation.titles[0] if elevation.titles else ""),
    )


def architect_views_findings(items: Sequence[WorkItem]) -> list[dict[str, Any]]:
    """GV's questions to the architect where its own two views of a piece print different sizes
    (each work item's `views_disagree`, read by code from the drawings' text): one code finding
    per span, in the chat finding shape of a cross-view one on the architect's side, so it is
    classified "Ask the architect" (`rule_notes.views_note_of`). The governing view's size is
    `architect`, the other view's `vendor` (both are the architect's). Never red."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        found = item.views_disagree
        if found is None:
            continue
        title = _plain(found.title).capitalize() or "Item"
        for (governing, other), name in zip(
            found.spans, found.names or ("size",) * len(found.spans), strict=False
        ):
            if (governing.ref, other.ref) in seen:
                continue
            seen.add((governing.ref, other.ref))
            out.append(
                {
                    "page": item.page,
                    "vs_page": found.governing_page,
                    "item": f"{title}: {name}",
                    "category": item.products[0] if len(item.products) == 1 else "other",
                    "architect": governing.text,
                    "vendor": other.text,
                    "status": "check",
                    "why": (
                        f"The architect's own drawings disagree: its {found.other} (page "
                        f"{found.other_page}) shows {other.text}, its {found.governing} (page "
                        f"{found.governing_page}) shows {governing.text}. GV compares "
                        f"{found.governs} with the {found.governing}; GV asks the architect."
                    ),
                    "verified": True,
                    "source": "cross_view",
                    "reference_side": "architect",
                    "arch_box": _box_of(governing, found.file, found.governing_page),
                    "vendor_box": _box_of(other, found.file, found.other_page),
                    "dims": {
                        "architect": _label_of(governing, found.file, found.governing_page),
                        "vendor": _label_of(other, found.file, found.other_page),
                        "views": {
                            "governing": found.governing,
                            "other": found.other,
                            "governing_page": found.governing_page,
                            "other_page": found.other_page,
                            "governs": found.governs,
                        },
                    },
                }
            )
    return out


def _box_of(label: Label, file: str, page: int) -> dict[str, Any]:
    x0, y0, x1, y1 = label.box
    return {"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": file}


def _label_of(label: Label, file: str, page: int) -> dict[str, Any]:
    return {
        "id": label.id,
        "text": label.text,
        "inches": label.inches,
        "route": label.route or "text",
        "page": page,
        "file": file,
        "box": list(label.box),
    }


def _shown_only(
    answer: pairing_module.MatchAnswer,
    everything: Sequence[AcrossCandidate],
    item_tags: frozenset[str],
    context: _Across,
    pool: Mapping[str, AcrossCandidate],
    project: str | None,
) -> str | None:
    """How the architect shows an item with no drawing of its sizes, when two witnesses agree
    (else `None`: the drawing is left as not found). The AI page matcher, shown every architect
    page of the vendor drawing's project (and code knows that project), named only:

    - key plans, and one of them prints the vendor's item tag with no callout within twice
      `CALLOUT_REACH_PT` of it (nothing points from it to an elevation): "only as a tag on a
      plan";
    - sheets whose every view the set index reads as printing no sizes of the piece, and code
      reads at most `MAX_SIZELESS_LABELS` labels in each, or a plan of room and wall sizes only
      (`_room_only`) or a reflected ceiling plan: "the architect shows this item without
      sizes"."""
    if not answer.matches:
        return None
    by = {c.id: c for c in everything}
    named = [by[m] for m in dict.fromkeys(answer.matches) if m in by]
    if (
        project is None
        or len(named) != len(answer.matches)
        or any(c.reference or _project_of(c) != project for c in named)
    ):
        return None
    if all(c.key_plan for c in named):
        plans = {c.id for c in named}
        for plan in context.plans:
            if plan.item not in plans:
                continue
            for key, printed, box in plan.tags:
                if key not in item_tags:
                    continue
                near = min((views_module.gap(box, m.box) for m in plan.marks), default=math.inf)
                if near > 2 * CALLOUT_REACH_PT:
                    return (
                        f"the architect shows this item only as a tag on a plan ({printed} on "
                        f"its page {plan.page}, no callout beside it), with no sizes"
                    )
        return None
    if any(c.key_plan or c.id not in pool for c in named):
        return None
    for item in named:
        drawings = item.views or item.drawings
        if item.id not in context.no_sizes:
            return None
        if any(
            (v.ref not in context.no_sizes and item.views)
            or (len(v.labels) > MAX_SIZELESS_LABELS and v.ref not in context.room_only)
            for v in drawings
        ):
            return None
    pages = ", ".join(str(c.page) for c in named)
    rooms = any(v.ref in context.room_only for c in named for v in c.views)
    return (
        "the architect shows this item without sizes (its file's page "
        f"{pages}: {'elevations with no sizes, plans with room and wall sizes only' if rooms else 'no view prints its sizes'})"
    )


def _view_ruled_out(
    vendor: pairing_module.Facts, views: Sequence[pairing_module.Facts], page_title: str | None
) -> bool:
    """Whether code rules out a view of a sheet for the vendor's drawing: then the whole sheet is
    never its drawing (a standard and an accessible vanity on one sheet). A view unmarked against
    a marked vendor drawing is not ruled out when the sheet's own title carries the vendor's
    variant marker ("ACCESSIBLE ... VANITY": every view on it is the accessible one)."""
    title_marks = pairing_module.markers_in([page_title or ""])[0]
    for scored in pairing_module.rank(vendor, views):
        if scored.contradicts is None:
            continue
        view = next(v for v in views if v.ref == scored.ref)
        if title_marks and vendor.markers and not view.markers:
            continue
        return True
    return False


def _unsure(
    ranked: Sequence[pairing_module.Scored],
    code_pick: str | None,
    answer: pairing_module.MatchAnswer | None,
) -> tuple[str, ...]:
    """The architect items a person confirms between when the AI page matcher is sure of one and
    code could not confirm it (`pairing.agree` refused it): the matcher's pick, then code's own
    pick or code's first other item (code's pick first when code rules the matcher's out). Empty
    when the matcher was not sure or found none, or when code rules its pick out and holds no
    pick of its own (another unit, nothing to choose)."""
    if answer is None or not answer.confident or not answer.matches:
        return ()
    by = {s.ref: s for s in ranked}
    pick = answer.matches[0]
    scored = by.get(pick)
    if scored is None:
        return ()
    if pick == code_pick:
        # Code and the matcher agree on the item, and something later (which view) is unsure.
        return (pick,)
    other = code_pick if code_pick not in (None, pick) else None
    if scored.contradicts is not None:
        # Code rules the matcher's pick out and picked another: code's first, then the matcher's.
        return () if other is None else (other, pick)
    if other is None:
        other = next((s.ref for s in ranked if s.contradicts is None and s.ref != pick), None)
    return (pick,) if other is None else (pick, other)


def _human_ids(why: str, everything: Sequence[AcrossCandidate], context: _Across) -> str:
    """The reason with code's private ids said as pages a person finds in the architect's file:
    a view "p1208.a1.view5" is 'architect page 12, view "HALL ELEVATION"', an item "p1009" is
    'architect page 9 ("STANDARD KITCHEN")', the title the set index read on it, when there is one.
    """
    files = {c.file for c in everything}
    found: list[tuple[str, str]] = []
    for item in everything:
        page = f"architect page {item.page}" + (f" of {item.file}" if len(files) > 1 else "")
        title = next(
            (context.index_titles[d.ref] for d in item.drawings if d.ref in context.index_titles),
            None,
        )
        named = f'{page} ("{title}")' if title else page
        for view in item.views:
            words = f'{page}, view "{view.titles[0]}"' if view.titles else named
            found.append((view.ref, words))
        found.extend((d.ref, named) for d in item.drawings)
        found.append((item.id, named))
    for ref, words in sorted(found, key=lambda one: -len(one[0])):
        if ref in why:
            why = re.sub(rf"(?<![\w.]){re.escape(ref)}(?!\.?\w)", words.replace("\\", r"\\"), why)
    return why


def _honest_unpaired(
    vendor: Drawing,
    everything: Sequence[AcrossCandidate],
    item_tags: frozenset[str],
    project: str | None,
    facts_of: Callable[[Drawing], pairing_module.Facts],
) -> str | None:
    """What code can say of an unpaired vendor drawing's item: no architect drawing of its
    project prints its item tag, but a key plan of the project does (a tag gives no sizes), or
    only the brand prototype does (R1: reference only)."""
    if not item_tags:
        return None

    def printing(items: Iterable[AcrossCandidate]) -> list[tuple[AcrossCandidate, list[str]]]:
        out = []
        for item in items:
            tags = frozenset(t for d in item.drawings for t in facts_of(d).tags)
            hits = sorted(item_tags & tags)
            if hits:
                out.append((item, hits))
        return out

    mine = [c for c in everything if project is None or _project_of(c) in (project, None)]
    if printing(c for c in mine if not c.reference and not c.key_plan):
        return None
    plans = printing(c for c in mine if c.key_plan and not c.reference)
    if plans:
        item, hits = plans[0]
        return (
            f"{pairing_module.UNPAIRED}: its item tag {', '.join(hits)} is printed only on a "
            f"key plan of the architect's file (page {item.page}), which gives no sizes"
        )
    prototype = printing(c for c in everything if c.reference)
    if prototype:
        item, hits = prototype[0]
        return (
            f"{pairing_module.UNPAIRED}: only the brand prototype shows its item code "
            f"{', '.join(hits)} (the architect's file page {item.page}): reference only, never "
            "compared (R1)"
        )
    return None


def _linked(
    item_tags: frozenset[str], pool: Sequence[AcrossCandidate], context: _Across
) -> dict[str, str]:
    """The candidates (items and their views) a key plan of the vendor drawing's project points
    to (fix: follow plan callouts), with the evidence in words. One of the vendor's item tags is
    printed on the plan; the callout nearest to it is within `CALLOUT_REACH_PT`, no other callout
    is nearly as near (`CALLOUT_CLEAR`), and no other item's tag is nearer to that callout (a
    callout in the next room does not point to this piece); the callout names one view (a
    four-way callout does not say which view shows the piece)."""
    in_pool = {c.id for c in pool}
    projects = {_project_of(c) for c in pool}
    universe = context.item_tags()
    out: dict[str, str] = {}
    for plan in context.plans:
        if plan.project not in projects or not plan.marks:
            continue
        for key, printed, box in plan.tags:
            if key not in item_tags:
                continue
            near = sorted((views_module.gap(box, m.box), n) for n, m in enumerate(plan.marks))
            if near[0][0] > CALLOUT_REACH_PT:
                continue
            if len(near) > 1 and near[1][0] < CALLOUT_CLEAR * max(near[0][0], 1.0):
                continue
            mark = plan.marks[near[0][1]]
            rivals = [
                views_module.gap(other, mark.box)
                for other_key, _text, other in plan.tags
                if other_key != key and other_key in universe
            ]
            if rivals and min(rivals) < near[0][0]:
                continue
            if len(mark.numbers) != 1:
                continue
            view = context.sheets.get((plan.project, mark.sheet, mark.numbers[0]))
            if view is None or context.view_page.get(view) not in in_pool:
                continue
            words = (
                f"the architect's key plan (page {plan.page}) prints {printed} beside a callout "
                f"to view {mark.numbers[0]} of sheet {mark.sheet}"
            )
            out.setdefault(view, words)
            out.setdefault(context.view_page[view], words)
    return out


def _ranked_across(
    vendor: Drawing,
    pool: Sequence[AcrossCandidate],
    facts_of: Callable[[Drawing], pairing_module.Facts],
    typed: frozenset[str],
    item_tags: frozenset[str] | None = None,
    linked: Mapping[str, str] | None = None,
    common: frozenset[str] = frozenset(),
    boiler: frozenset[str] = frozenset(),
) -> tuple[tuple[pairing_module.Scored, ...], str | None]:
    """Code's ranking of the architect items for a vendor drawing (`pairing.rank`) and its own
    clear pick (`pairing.clear_winner`, or `pairing.decide`): the slow part of `_pair_across`."""
    mine = facts_of(vendor)
    built = {c.id: _item_facts(c, facts_of, _section_of(c, facts_of)) for c in pool}
    spans = mine.spans

    def item_spans(item: pairing_module.Facts) -> int:
        if spans is None:
            return 0
        return max((spans(m) for m in built[item.ref][1]), default=0)

    titles = {c.id: built[c.id][0].title for c in pool}
    typed_hits = {cid for cid, title in titles.items() if title and title in typed}
    text = mine.text
    if typed_hits:
        # GV's typed title box naming an architect item counts as that title printed on it.
        text = " ".join([text, *(titles[cid] or "" for cid in sorted(typed_hits))])
    vendor_facts = pairing_module.Facts(
        ref=mine.ref,
        values=mine.values,
        references=mine.references,
        spans=item_spans if spans is not None else None,
        oriented=mine.oriented,
        tags=mine.tags - common,
        words=mine.words - boiler,
        views=mine.views,
        section=mine.section,
        text=text,
        phrases=mine.phrases,
        item_tags=item_tags if item_tags is not None else mine.item_tags,
        linked={r: w for r, w in (linked or {}).items() if r in built},
        # The vendor's own variant and hand markers: a marker on one side only rules a pair
        # out (accessible against standard) or flags it (opposite hand), whichever side has it.
        markers=mine.markers,
        hand=mine.hand,
        index_title=mine.index_title,
    )
    ranked = pairing_module.rank(vendor_facts, [built[c.id][0] for c in pool])
    by_rank = {s.ref: s for s in ranked}
    pick = pairing_module.clear_winner(ranked)
    code_pick = pick.ref if pick is not None else None
    if code_pick is None:
        # The printed reference or the same printed values (`decide`, #1723), drawing by drawing.
        legacy = pairing_module.decide(mine, [m for c in pool for m in built[c.id][1]])
        if legacy.chosen is not None:
            owner = next(c.id for c in pool if any(d.ref == legacy.chosen for d in c.drawings))
            if by_rank[owner].contradicts is None:
                code_pick = owner
    return ranked, code_pick


def _paired_drawings(pair: AcrossPair, by_item: Mapping[str, AcrossCandidate]) -> list[Drawing]:
    """The architect drawings a vendor drawing was paired with: the chosen views of a sheet of
    several views, else the chosen items' drawings."""
    out: list[Drawing] = []
    for item_id in pair.chosen:
        item = by_item[item_id]
        views = [v for v in item.views if v.ref in pair.views]
        out.extend(views or item.drawings)
    return out


def _prints_no_drawing(
    dims_page: Mapping[str, Any] | None,
    health_page: Mapping[str, Any] | None,
    halves: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether a page of the vendor's file has no drawing to compare (`NO_DRAWING_NOTE`): GV
    headed no drawing on it, code read its text whole (typed text, no pen-stroke letters), and it
    prints fewer than `MIN_PAGE_DIMS` dimensions with a unit (a schedule's row numbers are none)."""
    if any(h.get("heading") for h in halves):
        return False
    facts = (health_page or {}).get("facts")
    if not isinstance(facts, Mapping) or facts.get("drawn_letters") or not facts.get("typed_text"):
        return False
    printed = 0
    for label in (dims_page or {}).get("labels") or []:
        if isinstance(label, Mapping) and (
            label.get("mm") is not None or _UNIT.search(str(label.get("text") or ""))
        ):
            printed += 1
    return printed < MIN_PAGE_DIMS


def _section_of(
    item: AcrossCandidate, facts_of: Callable[[Drawing], pairing_module.Facts]
) -> str | None:
    """An architect item's place in the divider pages (its first drawing's page)."""
    return facts_of(item.drawings[0]).section if item.drawings else None


def _spans_with(
    raw: Sequence[Mapping[str, Any]], scales_page: Mapping[str, Any], side: str
) -> Callable[[pairing_module.Facts], int]:
    """How many of a vendor drawing's spans code's drawn-position pairing (`dim_pairs`, in real
    inches at each drawing's measured scale) lines up with a candidate's, never by value."""

    def spans(candidate: pairing_module.Facts) -> int:
        if candidate.rows is None:
            return 0
        theirs, their_scales, their_side = candidate.rows
        labels = [{**label, "id": f"v{label.get('id')}", "side": "vendor"} for label in raw] + [
            {**label, "id": f"a{label.get('id')}", "side": "architect"} for label in theirs
        ]
        scales = [
            scale
            for page, as_side in ((scales_page, side), (their_scales, their_side))
            for entry in page.get("scales") or []
            if isinstance(entry, Mapping)
            and entry.get("side") == as_side
            and (
                scale := view_scales.from_dict(
                    {**entry, "side": "vendor" if as_side == side else "architect"}
                )
            )
            is not None
        ]
        checks = [
            {**check, "id": f"{prefix}{check.get('id')}"}
            for page, prefix in ((scales_page, "v"), (their_scales, "a"))
            for check in page.get("labels") or []
            if isinstance(check, Mapping)
        ]
        if not scales or not labels:
            return 0
        try:
            found = dim_pairs.page_pairs(0, labels, scales, checks)
        except Exception:  # noqa: BLE001 - no layout evidence, never a stop
            _log.debug("simple check pairing: drawn rows not paired", exc_info=True)
            return 0
        return len(found.get("pairs") or [])

    return spans


def _governing_first(drawings: Sequence[Drawing], vendor_page: int) -> tuple[Drawing, ...]:
    """The architect drawings, each once, the governing one first (R5): a detail, section or
    enlarged view; then the larger printed scale; then the larger measured scale (fewer inches per
    point); then the one on the vendor drawing's page; then page order."""
    unique: dict[str, Drawing] = {}
    for drawing in drawings:
        unique.setdefault(drawing.ref, drawing)

    def key(drawing: Drawing) -> tuple[bool, float, float, bool, int, int]:
        printed = _scale_ratio(drawing.scale_note)
        return (
            not drawing.detail,
            -float(printed) if printed is not None else 0.0,
            drawing.scale_in_per_pt if drawing.scale_in_per_pt is not None else math.inf,
            drawing.page != vendor_page,
            drawing.page,
            drawing.half,
        )

    return tuple(sorted(unique.values(), key=key))
