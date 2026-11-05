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
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from app.review.assistant import dim_pairs, title_block, view_scales
from app.review.assistant.full_check_plan import DIVIDER_NOTE, flag_mode, read_page_map
from app.review.assistant.product_scope import named_products
from app.review.assistant.reference_page import guess_words
from app.review.assistant.sheet_revisions import exact_copies, older_copies
from app.review.assistant.simple_check import pairing as pairing_module
from app.review.assistant.simple_check import projects as projects_module
from app.review.assistant.simple_check import rooms as rooms_module
from app.review.assistant.simple_check import signature as signature_module
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
    WrongTag,
    label_ref,
)
from app.review.assistant.simple_check.set_pages import set_pages
from vocabulary.dashes import dashes_to_hyphen

#: The AI page matcher's saved answers in the set pack (`page_match.py`, #1798).
MATCH_FILE: Final = "page_match.json"
#: The set index in the set pack (`set_index.py`: each page's project, set and views, read by AI
#: and checked by code).
INDEX_FILE: Final = "set_index.json"

#: Code's ranking of the architect items for each vendor drawing paired across the uploads, kept
#: between builds of one unchanged set pack (a full check builds its work list again as each
#: matcher answer lands): (vendor drawing, its candidates) -> (the ranking, code's own pick).
type RankMemo = dict[
    tuple[str, tuple[str, ...]],
    tuple[tuple[pairing_module.Scored, ...], str | None]
    | tuple[tuple[pairing_module.Scored, ...], str | None, Any],
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
    "MATCH_PER_PAGE",
    "MATCH_TARGETS",
    "MIN_DRAWING_PT",
    "MIN_PAGE_DIMS",
    "NO_DRAWING_NOTE",
    "NUMBERS_UNREAD",
    "ON_DRAWINGS",
    "PAIRING_TARGETS_ENV",
    "ROOM_CLEAR",
    "ROOM_REACH_PT",
    "TAG_PAGES_MAX",
    "RankMemo",
    "architect_views_findings",
    "build_worklist",
    "combined_files",
    "per_view_pairing",
    "signature_words",
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
#: A number printed with no unit mark at all (`101`, `12`): on a room table's page it is a room
#: number or a count, never a size (`_facts`).
_BARE_NUMBER: Final = re.compile(r"\d{1,4}")
#: An item-like tag (`pairing.item_tags_in`) names one piece when the vendor prints it on at most
#: this share of its drawings (or `ITEM_TAG_MIN` drawings): `LM-004`, printed on a third of a real
#: set's drawings, is a laminate; `XX-801`, on the few sheets of one piece, is the piece. A tag the
#: vendor's own title block names as its sheet's item, numbered as an item, names its piece however
#: many sheets draw it (`_title_block_items`): one vanity drawn on 5 of a 9-sheet set's sheets.
ITEM_TAG_SHARE: Final = 0.1
ITEM_TAG_MIN: Final = 3
#: A plan printing this many item-like tags is a key or furniture plan, not an item's drawing.
KEY_PLAN_TAGS: Final = 8
#: A key plan's callout points to the item whose tag is printed this close to it (points), when no
#: other callout is nearly as close (`CALLOUT_CLEAR` times as far, or more).
CALLOUT_REACH_PT: Final = 100.0
CALLOUT_CLEAR: Final = 1.5
#: The vendor's item tag printed on at most this many pages of the architect's file names the
#: item's architect page (`_tag_page`); printed on more, it is a finish or a type code (every
#: room's plan, a legend, a schedule): no evidence of which page shows the piece.
TAG_PAGES_MAX: Final = 3
#: The room of an item the architect shows only as its tag on a plan (`_room_page`): the room name
#: printed nearest the tag, within this reach (points; about 17 feet at 1/4" = 1'-0") ...
ROOM_REACH_PT: Final = 300.0
#: ... when the next other room name is at least this many times as far (else the plan's
#: elevation callout beside the tag must point to the room's elevations).
ROOM_CLEAR: Final = 2.0
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
    folder: Path,
    main_file: str | None = None,
    *,
    memo: RankMemo | None = None,
    flag: bool | None = None,
    per_view: bool | None = None,
) -> WorkList:
    """The work list of the set pack in `folder` (its page map with the plan check's answers and
    code's assumptions laid over it, its labels, scales and notes). Raises `FileNotFoundError` when
    the pack has no page map yet. `memo`: code's ranking across the uploads kept between builds of
    one unchanged pack (`RankMemo`). `flag`: flag mode's best guesses (`_best_guesses`); `None`:
    the setting (`full_check_plan.flag_mode`). `per_view`: pair with the architect's drawings one
    by one (rebuild plan Step 3); `None`: the setting (`per_view_pairing`)."""
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
        flag=flag_mode() if flag is None else flag,
        per_view=per_view_pairing() if per_view is None else per_view,
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
    flag: bool = False,
    per_view: bool | None = None,
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
    page is checked. `flag` (flag mode, Anant 2026-11-04): a drawing whose pairing is unsure is
    paired with its best guess (`_best_guesses`). `per_view` (rebuild plan Step 3): each titled
    view of an architect sheet is a target of its own (`_ranked_by_view`); `None`: the setting
    (`per_view_pairing`), `False`: whole sheets, as before."""
    if per_view is None:
        per_view = per_view_pairing()
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
    read_index = _index_pages(set_index)
    # A vendor page that is a copy of an architect's sheet (the same sheet number and title, a key
    # plan of an older issue): paired with it by code (`_sheet_pair`), even with no dimension.
    sheet_ids = _sheet_ids(read_index, dims_pages)
    sheet_twins = _sheet_twins(file, sheet_ids, architect_files)
    kept_for_sheet: set[int] = set()
    """The vendor's pages with no drawing to compare but its copy of an architect's sheet: skipped
    after all when no architect page pairs with them by the sheet number."""

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
            if (entry_file, own) not in sheet_twins:
                skip(page, NO_DRAWING_NOTE)
                cover[page] = PageCover(page=page, how="skipped", reason=NO_DRAWING_NOTE)
                continue
            kept_for_sheet.add(page)
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
    # A room or vanity matrix, a room schedule (`rooms.table_rooms`): its numbers are rooms and
    # counts. Read as inches they made a room matrix and the architect's room schedule look like
    # one piece's drawings (rooms 301 and 302 as two 301" and 302" overall widths).
    room_tables = {
        key: table
        for key, entry in dims_pages.items()
        if (table := rooms_module.table_rooms(t for t, _b in views_module.phrases_of(entry)))
    }

    def sized(text: object, key: tuple[str, int]) -> bool:
        """Whether a label may be a size: on a room table's page, never a bare number."""
        return key not in room_tables or not _BARE_NUMBER.fullmatch(_plain(text))

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
                # A keynote code an older pack read as a sheet number (`views.keynote_code`).
                if not views_module.keynote_code(str(s))
            ),
        ]
        side = _LABEL_SIDE.get(drawing.role, drawing.role)
        raw = [
            dict(label)
            for label in dims_page.get("labels") or []
            if isinstance(label, Mapping)
            and label.get("side") == side
            and sized(label.get("text"), (drawing.file, own))
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
            values=pairing_module.values_of(
                [lb for lb in drawing.labels if sized(lb.text, (drawing.file, own))]
            ),
            references=pairing_module.references_in(texts),
            spans=_spans_with(raw, scales_page, side) if drawing.role == "vendor" else None,
            rows=(tuple(raw), scales_page, side),
            oriented=oriented,
            size_rows=pairing_module.size_rows(raw),
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
            view_titles=tuple(drawing.titles or ()) if drawing.role != "vendor" else (),
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
    on_drawings = _drawing_answers(matches)
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
    across_context.architect_text = _architect_text(
        entries, architect_files, dims_pages, notes_pages, read_index, texts or {}, across_context
    )
    across_context.sheet_ids = sheet_ids
    across_context.sheet_twins = sheet_twins
    separate_items = any(c.file != file for c in items_across)
    # A copy of an architect's sheet with no drawing to compare (a key plan) counts in no share
    # of the vendor's drawings (which tags and words are its title block's).
    mine_vendor = [h for h in vendor_halves if h.file == file and h.page not in kept_for_sheet]
    for half in mine_vendor:
        known_vendor = facts_of(drawings[half.ref])
        across_context.tag_drawings.update(known_vendor.item_tags or ())
        across_context.word_drawings.update(known_vendor.words)
    across_context.item_limit = max(ITEM_TAG_MIN, ITEM_TAG_SHARE * len(mine_vendor))
    across_context.named_items = _title_block_items(
        {(drawings[h.ref].file, drawings[h.ref].own_page) for h in mine_vendor}, dims_pages
    )
    per_page = Counter((h.file, h.file_page or h.page) for h in vendor_halves)
    for half in vendor_halves:
        page_read = read_index.get(f"{half.file}#{half.file_page or half.page}")
        if page_read is not None:
            across_context.marked[half.ref] = _marked_words(page_read)
            title = _plain(page_read.get("item"))
            if title and per_page[(half.file, half.file_page or half.page)] == 1:
                # The page's item is this drawing's only when it is the page's one vendor drawing.
                across_context.index_titles[half.ref] = title
            if per_page[(half.file, half.file_page or half.page)] == 1:
                across_context.index_views[half.ref] = tuple(
                    str(v.get("title") or "")
                    for v in page_read.get("views") or []
                    if isinstance(v, Mapping)
                )
    across_context.word_limit = max(COMMON_WORD_MIN, COMMON_WORD_SHARE * len(mine_vendor))
    across_context.per_view = per_view
    if per_view:
        across_context.unit_aliases = signature_module.unit_aliases(
            {h.ref: t for h in mine_vendor if (t := across_context.index_titles.get(h.ref))}
        )
        across_context.phrases_at = lambda drawing: views_module.phrases_of(
            dims_pages.get((drawing.file, drawing.own_page)), drawing.crop_box
        )
    vendor_tags = across_context.item_tags()
    for half in mine_vendor:
        named = across_context.index_titles.get(half.ref)
        if not named:
            continue
        drawn = drawings[half.ref]
        place = across_context.places.get((drawn.file, drawn.own_page)) or projects_module.Place()
        across_context.vendor_items[half.ref] = _VendorItem(
            page=drawn.own_page,
            title=named,
            words=_item_words(named, place.name),
            tags=(facts_of(drawn).item_tags or frozenset()) & vendor_tags,
        )
    for half in vendor_halves:
        # The room or unit numbers a vendor page lists (`_room_numbers_page`): its text layer
        # (`page_texts`), else its printed phrases; only the page's one vendor drawing's.
        own = half.file_page or half.page
        if per_page[(half.file, own)] != 1:
            continue
        page_text = (texts or {}).get((half.file, own))
        printed_text = (
            [page_text]
            if page_text
            else [text for text, _box in views_module.phrases_of(dims_pages.get((half.file, own)))]
        )
        table = room_tables.get((half.file, own), frozenset())
        listed = rooms_module.vendor_rooms(printed_text) | table
        if len(listed) >= rooms_module.MIN_ROOMS:
            across_context.vendor_rooms[half.ref] = (
                listed,
                rooms_module.printed_rooms(printed_text, rooms_module.has_short(listed)) | table,
            )
            if table:
                across_context.room_list_tables.add(half.ref)
    across_context.room_tables = {
        key: found for key, found in room_tables.items() if key in across_context.architect_text
    }

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
            on_drawings=half.ref in on_drawings,
        )
        if half.page in kept_for_sheet and (decided is None or decided.sheet is None):
            continue  # no architect page pairs with it by its sheet number: skipped as before
        if decided is None:
            continue
        misled = across_context.misled.get(half.ref)
        if misled is not None:
            decided = decided.model_copy(
                update={"wrong_tag": misled, "why": f"{decided.why}; {misled.why}"}
            )
        across_pairs[half.ref] = decided
    if across_pairs:
        marked = _with_marks(facts_of, across_context)
        # The swap audit first (a crossed pair lends nothing to its sheets), then again on the
        # sheets that took a pair.
        _swap_audit(across_pairs, marked, by_item, across_context)
        if _item_sheets(across_pairs, marked, across_context):
            _swap_audit(across_pairs, marked, by_item, across_context)
        if flag:
            # Last: a guess never lends its page to the vendor's other sheets nor joins the audit.
            _best_guesses(across_pairs, marked, by_item, across_context)
            if per_view:
                _swap_guesses(across_pairs, marked, by_item, across_context)
    for half in vendor_halves:
        decided = across_pairs.get(half.ref)
        if decided is not None and decided.chosen and half.from_upload:
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
        if half.page in kept_for_sheet and half.ref not in across_pairs:
            if half.page not in cover:
                skip(half.page, NO_DRAWING_NOTE)
                cover[half.page] = PageCover(page=half.page, how="skipped", reason=NO_DRAWING_NOTE)
            continue
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
            mirrored=across_pairs[half.ref].mirrored if half.ref in across_pairs else False,
            views_disagree=(
                across_pairs[half.ref].views_disagree if half.ref in across_pairs else None
            ),
            pairing_guess=across_pairs[half.ref].guess if half.ref in across_pairs else False,
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


#: A saved matcher answer's `question` when it was shown drawings one by one, not whole pages
#: (`page_match`, per drawing).
ON_DRAWINGS: Final = "drawings"


def _drawing_answers(document: Any) -> frozenset[str]:
    """The vendor drawings whose saved matcher answer was given on drawings (`ON_DRAWINGS`)."""
    found = document.get("answers") if isinstance(document, Mapping) else None
    return frozenset(
        str(ref)
        for ref, entry in (found.items() if isinstance(found, Mapping) else ())
        if isinstance(entry, Mapping) and entry.get("question") == ON_DRAWINGS
    )


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
    file: str | None = None
    """Its file (`page` is its page there)."""


@dataclass(frozen=True, slots=True)
class _SheetId:
    """A page's own sheet number and titles (`_sheet_ids`)."""

    number: str
    """Normalised (`views.normal_sheet`): `A 2.1` is `A2.1`."""
    printed: str
    """As the page prints it."""
    titles: Mapping[frozenset[str], str]
    """Each of its titles (its views', its item's) as a set of words, to the title as printed."""


#: A title's words for comparing two sheets' titles (`_SheetId.titles`): letters and digits.
_TITLE_WORD: Final = re.compile(r"[A-Z0-9]+")


def _sheet_ids(
    index: Mapping[str, Mapping[str, Any]],
    dims_pages: Mapping[tuple[str, int], Mapping[str, Any]],
) -> dict[tuple[str, int], _SheetId]:
    """Each page's own sheet number, by file and page: the set index's read of its title block
    (checked printed on the page, `set_index`), when it is a sheet number (letters and a number,
    `views.normal_sheet`: `A5.1`, `IN 2.0`; a bare `4` or `1/2` is none) and code's own read of
    the sheet's largest sheet number (`views.sheet_number`) does not say another. With its titles
    (the set index's views and item, checked printed)."""
    out: dict[tuple[str, int], _SheetId] = {}
    for one in index.values():
        if not isinstance(one, Mapping) or not isinstance(one.get("page"), int):
            continue
        key = (str(one.get("file")), int(one["page"]))
        printed = _plain(one.get("sheet"))
        number = views_module.normal_sheet(printed) if printed else None
        if number is None:
            continue
        own = views_module.sheet_number(views_module.phrases_of(dims_pages.get(key)))
        if own is not None and own != number:
            continue  # the title block's largest number says another sheet: not sure which
        titles = [
            str(v.get("title") or "") for v in one.get("views") or [] if isinstance(v, Mapping)
        ]
        words = {
            found: _plain(title)
            for title in (*titles, str(one.get("item") or ""))
            if (found := frozenset(_TITLE_WORD.findall(title.upper())))
        }
        out[key] = _SheetId(number, printed, words)
    return out


def _sheet_twins(
    vendor_file: str,
    sheet_ids: Mapping[tuple[str, int], _SheetId],
    architect_files: frozenset[str],
) -> dict[tuple[str, int], tuple[tuple[str, int], ...]]:
    """Each page of the vendor's file that prints the sheet number and a title of one or more of
    the architect's pages (its copy of the architect's sheet), with those pages; only a number
    no other page of the vendor's file prints. The project is checked when pairing
    (`_sheet_pair`)."""
    mine = Counter(
        sheet.number for (file, _page), sheet in sheet_ids.items() if file == vendor_file
    )
    out: dict[tuple[str, int], tuple[tuple[str, int], ...]] = {}
    for key, sheet in sheet_ids.items():
        if key[0] != vendor_file or mine[sheet.number] != 1 or not sheet.titles:
            continue
        twins = tuple(
            other
            for other, theirs in sorted(sheet_ids.items())
            if other[0] in architect_files
            and theirs.number == sheet.number
            and sheet.titles.keys() & theirs.titles.keys()
        )
        if twins:
            out[key] = twins
    return out


def _sheet_pair(
    vendor: Drawing,
    everything: Sequence[AcrossCandidate],
    project: str | None,
    context: _Across,
) -> AcrossPair | None:
    """The architect page a vendor drawing is a copy of (2026-11-04: a key plan pairs by its
    printed sheet number, never by page order): the vendor's page prints a sheet number and a
    title (`_sheet_ids`), exactly one architect page of its project prints the same number under
    the same title, and no other page of the vendor's file prints that number (`_sheet_twins`).
    Code decides (the paid matcher is not asked), the whole sheet is compared, a key plan too:
    its printed words (room tags, callouts) are what may differ. Else `None`."""
    mine = context.sheet_ids.get((vendor.file, vendor.own_page))
    if mine is None or (vendor.file, vendor.own_page) not in context.sheet_twins:
        return None
    twins = [
        c
        for c in everything
        if not c.reference
        and (theirs := context.sheet_ids.get((c.file, c.page))) is not None
        and theirs.number == mine.number
        and mine.titles.keys() & theirs.titles.keys()
        and (project is None or _same_project(c, project, context))
    ]
    if len(twins) != 1:
        return None
    (twin,) = twins
    theirs = context.sheet_ids[(twin.file, twin.page)]
    shared = mine.titles.keys() & theirs.titles.keys()
    title = min(theirs.titles[words] for words in shared)
    context.sheet_paired[vendor.ref] = twin.id
    words = (
        f"both print the sheet number {theirs.printed} under the title {title}: the vendor's page "
        "is a copy of the architect's sheet (it may be an older issue), compared whole"
    )
    why = f"paired with {twin.id}: {words}"
    return AcrossPair(
        vendor=vendor,
        candidates=(twin.model_copy(update={"score": 0.0, "evidence": (words,), "strong": True}),),
        code_pick=twin.id,
        chosen=(twin.id,),
        why=_human_ids(why, everything, context),
        code_paired=True,
        sheet=theirs.printed,
    )


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
    """An item-like tag printed on more of the vendor's drawings than this names a finish, unless
    the vendor's title block names it as its sheet's item (`named_items`)."""
    named_items: frozenset[str] = frozenset()
    """The tags the vendor's own title blocks name as their sheet's item, numbered as items
    (`_title_block_items`): the piece's number on every sheet that draws it."""
    tag_paired: dict[str, str] = field(default_factory=dict)
    """The vendor drawings the architect's own printed item tag paired (`_tag_page`), with the
    architect item: the swap audit doubts them only on strong evidence (`pairing.swap_is_strong`)."""
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
    printed: dict[str, tuple[views_module.Phrase, ...]] = field(default_factory=dict)
    """Each architect item's printed text and tags, with where (by the item's id): its page's text
    layer and the tags read on it (`_tag_page`)."""
    architect_text: dict[tuple[str, int], tuple[str, ...]] = field(default_factory=dict)
    """Every page of the architect's file (a reference set's left out), by file and page: what it
    prints (its text layer, the tags read on it, the set index's checked item tags), whether or
    not it holds a drawing (`_tag_page` counts the pages printing a tag)."""
    tag_pages: dict[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    """The pages of `architect_text` printing each item tag, as `_tag_page` found them (kept: the
    work list is built again as each matcher answer lands)."""
    callouts: dict[str, tuple[views_module.Callout, ...]] = field(default_factory=dict)
    """Each architect item's elevation callouts (`views.callouts`), by the item's id: where a plan
    the vendor's room numbers paired points (`_room_numbers_page`)."""
    vendor_rooms: dict[str, tuple[frozenset[str], frozenset[str]]] = field(default_factory=dict)
    """A vendor drawing's room or unit numbers (`rooms.vendor_rooms`: printed after a room or unit
    label) and every room-shaped number its page prints (`rooms.printed_rooms`), by the drawing's
    ref: only the page's one vendor drawing."""
    room_numbers: dict[bool, dict[tuple[str, int], frozenset[str]]] = field(default_factory=dict)
    """Each page of `architect_text`'s room numbers (`rooms.printed_rooms`, the title block's left
    out), with a unit's short numbers (`2A`) or not (the key), as `_room_numbers` found them."""
    room_list_tables: set[str] = field(default_factory=set)
    """The vendor drawings whose page is a room table (a room or vanity matrix, `rooms.table_rooms`):
    their rooms are its cells'."""
    room_tables: dict[tuple[str, int], frozenset[str]] = field(default_factory=dict)
    """The architect's pages that are room tables (a room schedule), with their rooms."""
    room_paired: dict[str, str] = field(default_factory=dict)
    """The vendor drawings the room numbers paired (`_room_numbers_page`), with the architect item:
    like a pair by the printed item tag, the swap audit doubts them only on strong evidence."""
    sheet_ids: dict[tuple[str, int], _SheetId] = field(default_factory=dict)
    """Each page's own sheet number and titles (`_sheet_ids`), by file and page."""
    sheet_twins: dict[tuple[str, int], tuple[tuple[str, int], ...]] = field(default_factory=dict)
    """The vendor's pages that are copies of an architect's sheet (`_sheet_twins`)."""
    sheet_paired: dict[str, str] = field(default_factory=dict)
    """The vendor drawings paired by the sheet number both print (`_sheet_pair`), with the
    architect item: the swap audit never undoes them."""
    vendor_items: dict[str, _VendorItem] = field(default_factory=dict)
    """Each of the vendor's drawings with an item title (the page's one), by ref
    (`_misleading_tag`)."""
    misled: dict[str, WrongTag] = field(default_factory=dict)
    """The vendor drawings whose item number does not fit (`_misleading_tag`), by ref."""
    room_names: dict[str, str] | None = None
    """The rooms the architect's plans place item tags in (`_tag_rooms`), each room's letters
    (`_room_key`) to its name as printed; worked out once."""
    per_view: bool = False
    """Pair with the architect's drawings one by one (`per_view_pairing`, rebuild plan Step 3),
    not whole sheets."""
    signatures: dict[str, signature_module.Signature] = field(default_factory=dict)
    """Each drawing's page signature, by its ref, as worked out (`_signature`)."""
    unit_aliases: dict[str, str] = field(default_factory=dict)
    """A vendor drawing's unit type its title gives only by the vendor's own type code
    (`signature.unit_aliases`), by its ref."""
    phrases_at: Callable[[Drawing], Sequence[views_module.Phrase]] | None = None
    """A drawing's printed phrases with their boxes (the fixtures of its signature)."""
    index_views: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """The view titles the set index read on a vendor drawing's page (checked printed), by the
    drawing's ref: the page's one vendor drawing only (the kinds of view it draws)."""

    def item_tags(self) -> frozenset[str]:
        """The vendor's item tags (`ITEM_TAG_SHARE`): every item-like tag it prints on few of its
        drawings, or that its title blocks name as their sheet's item (`named_items`)."""
        return frozenset(
            t for t, n in self.tag_drawings.items() if n <= self.item_limit or t in self.named_items
        )


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
        # The page's hand words (its title's, and the set index's for the page) are every view's:
        # a view paired alone is still flagged opposite-hand. Flagging greys positions from an end,
        # never makes a red.
        title_hand = (pairing_module.markers_in([title])[1] if title else frozenset()) | (
            _marked_words(page_read)[1] if views else frozenset()
        )
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
        if title_hand:
            # The page title's hand ("... REVERSED UNITS") is every view's: a view paired alone is
            # still flagged opposite-hand (the title's tidy-up leaves the hand words out).
            for drawn in views:
                variant, hand = context.marked.get(drawn.ref, (frozenset(), frozenset()))
                context.marked[drawn.ref] = (variant, hand | title_hand)
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
        context.printed[item.id] = (*printed, *_tags_read(notes_page))
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
                file=item.file,
            )
        )
    for item in out:
        if item.reference:
            continue
        plan = next((p for p in context.plans if p.item == item.id), None)
        context.callouts[item.id] = (
            plan.marks
            if plan is not None
            else views_module.callouts(phrases[item.id], found_views[item.id], sheets)
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
    """The variant and opposite-hand words of a set index read (checked printed), in capitals. A
    hand word that only names the mirror on the wall ("MIRROR", "LED MIRROR") is left out
    (`pairing.fixture_mirror`): a bathroom mirror never says the drawing is flipped."""
    return (
        frozenset(" ".join(str(w).upper().split()) for w in read.get("variant_words") or []),
        frozenset(
            " ".join(str(w).upper().split())
            for w in read.get("hand_words") or []
            if not pairing_module.fixture_mirror(str(w))
        ),
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


def _title_block_items(
    pages: Iterable[tuple[str, int]], dims_pages: Mapping[tuple[str, int], Mapping[str, Any]]
) -> frozenset[str]:
    """The item tags the vendor's own title blocks name as their sheet's item: the ITEM cell code
    reads on each of its `pages` (`title_block.cells`, the text layer), numbered as an item
    (`pairing.numbered_as_item`: 100 or more; a finish or a material is numbered from 1). An item
    is drawn on as many sheets as it needs (its elevation, its top, its frame, its countertop: a
    real vanity on 5 of a 9-sheet set's sheets), so how many sheets print its number says nothing
    against it; a finish (`LA-002`, on most of a set's sheets) or a product's model number (a sink
    printed on five different vanities' sheets) is never in the item cell."""
    out: set[str] = set()
    for key in sorted(pages):
        found = [
            {"text": text, "box": list(box)}
            for text, box in views_module.phrases_of(dims_pages.get(key))
        ]
        for cell in title_block.cells(found):
            if cell.name == "item":
                out.update(
                    t
                    for t in pairing_module.item_tags_in([cell.value])
                    if pairing_module.numbered_as_item(t)
                )
    return frozenset(out)


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
        view_count=max(1, len(item.views), *(m.view_count for m in members)),
        whole_sheet=bool(item.views),
        markers=frozenset(w for m in members for w in m.markers),
        hand=frozenset(w for m in members for w in m.hand),
        index_title=next((m.index_title for m in members if m.index_title), None),
        place=(item.file, item.page),
        size_rows=tuple(r for m in members for r in m.size_rows),
        view_titles=(
            tuple(t for v in item.views for t in v.titles[:1])
            if item.views
            else tuple(t for m in members for t in m.view_titles)
        ),
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
    projects by every word each file's index read of the job (`projects.match`, `vocabulary`); or
    `None`, and the reason when its project is in the vendor's file but not in the architect's (a
    drawing needed)."""
    if not context.projects:
        return None, None
    place = context.places.get((vendor.file, vendor.own_page)) or projects_module.Place()
    if place.project is None:
        return None, None
    siblings = projects_module.siblings_of(context.places, vendor.file, place.source)
    mine = projects_module.match(
        place.project, context.projects, siblings, projects_module.vocabulary(context.places)
    )
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
    on_drawings: bool = False,
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
    ranking of an earlier build (only the answer is new: the agreement rule runs again on it).

    Per drawing (`_Across.per_view`, rebuild plan Step 3): every titled view of a sheet is ranked
    on its own facts and its page signature (`_ranked_by_view`), the page is decided as before on
    its best view's evidence, and the views compared are chosen with the page, before the pick:
    the matcher's picks on it, else code's best view and the views of the same piece
    (`_view_group`), never narrowed after the pick. The matcher is shown at most
    `MATCH_TARGETS` drawings (`AcrossPair.targets`). `on_drawings`: `saved` was given on those
    drawings (`page_match`); else on whole pages, and it is read on pages as before, the views
    then being code's."""
    context = context or _Across()
    facts_of = _with_marks(facts_of, context)
    mine_facts = facts_of(vendor)
    everything = [
        c for c in candidates if c.drawings and all(d.ref != vendor.ref for d in c.drawings)
    ]
    if not everything:
        return None
    project, refused = _vendor_project(vendor, context)
    if refused is None:
        # The vendor's copy of an architect's sheet (its sheet number and title): that sheet.
        copied = _sheet_pair(vendor, everything, project, context)
        if copied is not None:
            return copied
    pool = [
        c
        for c in everything
        if not c.reference
        and not c.key_plan
        and (project is None or _same_project(c, project, context))
        and refused is None
    ]
    item_tags = (mine_facts.item_tags or frozenset()) & context.item_tags()
    # The vendor's item number that does not fit (2026-11-04): the number its own other sheet of
    # the item prints pairs it instead when the architect prints that one in the room the vendor
    # names; else neither number does.
    misled = _misleading_tag(vendor, item_tags, project, context) if refused is None else None
    if misled is not None:
        context.misled[vendor.ref] = misled
        item_tags = (
            pairing_module.item_tags_in([misled.other or ""]) & context.item_tags()
            if misled.confirmed
            else frozenset()
        )
    # An item-like tag the vendor prints on many of its drawings is a material (`LM-004`, a
    # laminate on a third of a real set's drawings): no evidence of which piece this one is.
    common = (mine_facts.item_tags or frozenset()) - item_tags
    # So is a word it prints on many of its drawings (its title block's, its brand's).
    boiler = frozenset(
        w for w in mine_facts.words if context.word_drawings.get(w, 0) > context.word_limit
    )
    # The architect's own printed tag names the item's page (owner, 2026-10-31).
    tagged = _tag_page(item_tags, everything, pool, project if refused is None else None, context)
    tag_plan = tagged if tagged is not None and tagged.plan else None
    if not pool:
        why = (
            refused
            or (_needed(_tag_only_words(tag_plan)) if tag_plan is not None else None)
            or _honest_unpaired(vendor, everything, item_tags, project, facts_of, context)
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
                _tag_only_words(tag_plan)
                if tag_plan is not None
                else (
                    f"the architect's drawings of its project ({len(pool)} pages) print no "
                    "sizes of any piece (outlines, schedules or plans only)"
                )
            ),
            needed=True,
        )
    linked = _linked(item_tags, pool, context)
    by_view: _ByView | None = None
    if context.per_view:
        key = (f"{vendor.ref}#views", tuple(c.id for c in pool))
        found_view = memo.get(key) if memo is not None else None
        if found_view is None:
            found_view = _ranked_by_view(
                vendor, pool, facts_of, typed, item_tags, linked, common, boiler, context
            )
            if memo is not None:
                memo[key] = found_view
        ranked, code_pick, by_view = found_view  # type: ignore[misc]
    else:
        key = (vendor.ref, tuple(c.id for c in pool))
        known = memo.get(key) if memo is not None else None
        if known is None:
            known = _ranked_across(vendor, pool, facts_of, typed, item_tags, linked, common, boiler)
            if memo is not None:
                memo[key] = known
        ranked, code_pick = known  # type: ignore[misc]
    # The $0 code pass (plan change 6): two kinds of code evidence, a clear margin, no rival
    # named by code. Its pick is code's own; the paid matcher is not asked about it
    # (`page_match.to_ask`), and a saved matcher answer naming another item still refuses it.
    sure = pairing_module.code_pass(ranked)
    code_paired = sure is not None and code_pick in (None, sure.ref)
    if code_paired and sure is not None:
        code_pick = sure.ref
    asked = tuple(s.ref for s in ranked if s.contradicts is None)
    answer = None
    answer_views: dict[str, tuple[str, ...]] = {}
    """Per drawing: the views the matcher picked on each page it named."""
    saved_answer = saved[1] if saved is not None else None
    """The matcher's saved answer as it gave it, on pages (held when nothing confirms it)."""
    saved_shown: tuple[str, ...] = saved[0] if saved is not None else ()
    """The pages the matcher was shown whole (none when it was shown drawings)."""
    if saved is not None and by_view is not None and on_drawings:
        answer, answer_views, saved_answer = _drawing_answer(saved, by_view)
        saved_shown = ()
    elif saved is not None:
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
            asked=asked if by_view is None else by_view.asked,
            answer=None if answer is None else answer.matches,
            chosen=(governing,),
            why=_human_ids(
                f"code paired it with {governing}, the architect's {disagree.governing}, which "
                f"governs {disagree.governs}; the architect's own drawings disagree ({other}, its "
                f"{disagree.other}, shows the same piece: {sizes}): GV asks the architect, and "
                "these sizes are never red against the vendor",
                everything,
                context,
            ),
            views_disagree=disagree,
            **_by_view_fields(by_view, answer_views),
        )
    swap_why: str | None = None
    swapped: pairing_module.SizeMatch | None = None
    if asked:
        ranked, swap_why, swapped = _swap_checked(
            mine_facts, ranked, code_pick, answer, pool, facts_of
        )
    if not asked:
        chosen: tuple[str, ...] = ()
        why = f"{pairing_module.UNPAIRED}: every architect item is ruled out by code ({ranked[0].contradicts})"
    else:
        chosen, why = pairing_module.agree(ranked, code_pick, answer)
        if not chosen and swap_why:
            why = f"{why}; {swap_why}"
    tag_pair = tagged if tagged is not None and not tagged.plan else None
    if tag_pair is not None and any(
        s.ref == tag_pair.item and s.contradicts is not None for s in ranked
    ):
        # Code rules the page out (another variant, another section): never paired by its tag.
        tag_pair = None
    if tag_pair is not None and tag_pair.views:
        # A sheet of several views is ruled out view by view (`narrow`): an accessible vendor
        # unit is never paired with a view printing no accessible marker, its tag or not.
        sheet = next(c for c in pool if c.id == tag_pair.item)
        views_facts = [
            v
            for v in _view_facts(sheet, facts_of, _section_of(sheet, facts_of))
            if v.ref in tag_pair.views
        ]
        if any(s.contradicts is not None for s in pairing_module.rank(mine_facts, views_facts)):
            tag_pair = None
    # The tag's page and another page code and the matcher agreed on: a person confirms.
    rivals: tuple[str, ...] = ()
    if tag_pair is not None and tag_pair.room is not None:
        # Through the tag's room (not the architect's own tag on the drawing): a page the AI page
        # matcher is sure of, or code's own clear pick, is held against it (owner's rule, 25 Oct:
        # never a guessed pair), as is a page code and the matcher agreed on.
        held_room = _held_page(ranked, code_pick, answer or saved_answer) if not chosen else None
        if _held_weakly(held_room, ranked, misled):
            why = f"{why}; {_NOT_HELD.format(page=held_room)}"
            held_room = None
        others = tuple(c for c in (*chosen, held_room) if c is not None and c != tag_pair.item)
        if not others:
            # The paid matcher is still asked (a page it is sure of is held against the room's).
            chosen, code_pick = (tag_pair.item,), tag_pair.item
            why = f"paired with {tag_pair.item} through its room: {_room_words(tag_pair)}"
        else:
            rivals = (tag_pair.item, *others)
            why = (
                f"{pairing_module.UNPAIRED}: {tag_pair.item} through its room "
                f"({_room_words(tag_pair)}), but {' and '.join(others)} "
                f"{'is' if len(others) == 1 else 'are'} held for it "
                f"({why.removeprefix(f'{pairing_module.UNPAIRED}: ')})"
            )
            chosen = ()
    elif tag_pair is not None:
        if chosen in ((), (tag_pair.item,)):
            # The architect's own printed tag decides: the paid matcher is not asked about it.
            chosen, code_pick, code_paired = (tag_pair.item,), tag_pair.item, True
            context.tag_paired[vendor.ref] = tag_pair.item
            why = (
                f"paired with {tag_pair.item}: it prints the vendor's item tag "
                f"{tag_pair.printed}, the architect's own tag (no other elevation or section of "
                "the architect's prints it)"
            )
        else:
            rivals = (tag_pair.item, *chosen)
            why = (
                f"{pairing_module.UNPAIRED}: {tag_pair.item} prints the vendor's item tag "
                f"{tag_pair.printed}, but code and the AI page matcher name "
                f"{' and '.join(chosen)} ({why})"
            )
            chosen = ()
    # The vendor's printed room numbers name the architect's page about those rooms (2026-11-04),
    # when no printed item tag decided: as strong as the tag (code decides, the paid matcher is not
    # asked); a page code and the matcher agreed on other than it and its callouts' sheets is held
    # against it, as against the tag.
    room_added: tuple[str, ...] = ()
    """The sheets the room plan's callouts point to, added by code (not agreed by the matcher)."""
    room_pair = (
        _room_numbers_page(vendor, mine_facts, everything, pool, project, facts_of, context)
        if tagged is None and refused is None
        else None
    )
    if room_pair is not None:
        others = tuple(c for c in chosen if c != room_pair.item and c not in room_pair.sheets)
        if not others:
            agreed = tuple(c for c in chosen if c in room_pair.sheets)
            room_added = tuple(c for c in room_pair.sheets if c not in agreed)
            chosen = (room_pair.item, *agreed, *room_added)
            code_pick, code_paired = room_pair.item, True
            context.room_paired[vendor.ref] = room_pair.item
            why = f"paired with {room_pair.item}: {_rooms_words(room_pair)}"
        else:
            rivals = (room_pair.item, *others)
            why = (
                f"{pairing_module.UNPAIRED}: {room_pair.item} is about the vendor's rooms "
                f"({_rooms_words(room_pair)}), but code and the AI page matcher name "
                f"{' and '.join(others)} ({why})"
            )
            chosen = ()
    by_id = {c.id: c for c in pool}
    if room_pair is not None:
        # The room plan may be a key or furniture plan (a suite's plan printing its tags), never
        # one of the matcher's candidates: it is listed with the candidates, never asked about.
        by_id.setdefault(room_pair.item, next(c for c in everything if c.id == room_pair.item))
    picked_views: list[str] = []
    whole: set[str] = set()
    """The chosen items of several views compared whole (their page is titled the vendor's item)."""
    if chosen:
        kept: list[str] = []
        failed: list[str] = []
        shown: list[str] = []
        for item_id in chosen:
            item = by_id[item_id]
            if room_pair is not None and item_id == room_pair.item:
                # The page about the vendor's rooms: compared whole (its rooms' plan).
                kept.append(item_id)
                if item.views:
                    whole.add(item_id)
                continue
            if not item.views:
                kept.append(item_id)
                continue
            if (
                room_pair is not None
                and item_id in room_added
                and not any(s.ref == item_id and s.contradicts is not None for s in ranked)
                and (
                    pointed := _pointed_view(
                        dataclasses.replace(
                            mine_facts,
                            tags=mine_facts.tags - common,
                            words=mine_facts.words - boiler,
                            item_tags=item_tags,
                        ),
                        [
                            v
                            for v in _view_facts(item, facts_of, _section_of(item, facts_of))
                            if v.ref in room_pair.sheets[item_id]
                        ],
                    )
                )
                is not None
            ):
                # The room plan's elevation callout points to this sheet, and code finds the
                # vendor's piece on the view it points to.
                kept.append(item_id)
                picked_views.append(pointed.ref)
                view = next(v for v in item.views if v.ref == pointed.ref)
                title = view.titles[0] if view.titles else pointed.ref
                shown.append(
                    f'the plan\'s elevation callout points to its view "{title}" on {item_id} '
                    f"({pairing_module.evidence_words(pointed)})"
                )
                continue
            if room_pair is not None and item_id in room_added:
                # Pointed to, but code does not find the vendor's piece on it: not added.
                shown.append(
                    f"the plan's elevation callouts point to {item_id}, but code does not find "
                    "the vendor's piece on the views they point to, so it is not compared"
                )
                continue
            if tag_pair is not None and item_id == tag_pair.item:
                # The views of the sheet that print the vendor's item tag, or that are titled as
                # elevations of the room the plan prints its tag in.
                kept.append(item_id)
                picked_views.extend(tag_pair.views)
                what = (
                    f"printing {tag_pair.printed}"
                    if tag_pair.room is None
                    else f"of the room {tag_pair.room}"
                )
                shown.append(
                    f"its views {what} on {item_id}: "
                    + ", ".join(
                        f'"{v.titles[0]}"' if v.titles else v.ref
                        for v in item.views
                        if v.ref in tag_pair.views
                    )
                )
                continue
            picked = (
                answer_views.get(item_id) or by_view.views.get(item_id)
                if by_view is not None
                else None
            )
            if picked and by_view is not None:
                # Per drawing: the views were chosen with the page (the matcher's picks on it,
                # else code's best view and the views of the same piece), never after the pick.
                kept.append(item_id)
                picked_views.extend(picked)
                drawn = {v.ref: v for v in item.views}
                shown.append(
                    "its "
                    + ("view " if len(picked) == 1 else "views ")
                    + ", ".join(
                        f'"{drawn[r].titles[0]}"' if drawn[r].titles else r
                        for r in picked
                        if r in drawn
                    )
                    + f" on {item_id}"
                    + (
                        " (the AI page matcher's pick)"
                        if item_id in answer_views
                        else f" ({pairing_module.evidence_words(_scored_of(by_view, picked[0]))})"
                    )
                )
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
                # Look-alike views of the paired sheet, none with strong evidence: the one that
                # prints the vendor's sizes clearly best (the swap check among the sheet's views).
                best = _view_by_sizes(vendor_facts, item_views)
            if best is None:
                # The views of the vendor's room, all of one title ("STAFF LOUNGE 1" to
                # "4"): compared with all of them, as with a plan and an elevation of one piece.
                room = pairing_module.room_views(vendor_facts, item_views)
                if not room:
                    # The sheet's one plan and one elevation, titled by their kind only: two
                    # drawings of its one item, compared with both (2026-10-27).
                    room = pairing_module.plan_and_elevation(vendor_facts, item_views)
                if room:
                    kept.append(item_id)
                    picked_views.extend(v.ref for v in room)
                    shown.append(f"{pairing_module.evidence_words(room[0])} on {item_id}")
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
        if (
            sizeless
            and len(sizeless) == len(paired)
            and room_pair is not None
            and (left := tuple(c for c in room_added if c not in chosen))
        ):
            # The rooms' plan prints no sizes of the piece, and its elevation callouts point to a
            # sheet code does not find the piece on: never "drawing needed" (the drawing may be on
            # that sheet). A person confirms, and the paid matcher is asked.
            rivals = (room_pair.item, *left)
            why = (
                f"{pairing_module.UNPAIRED}: {room_pair.item} is about the vendor's rooms "
                f"({_rooms_words(room_pair)}) but prints no sizes of the piece, and its elevation "
                f"callouts point to {' and '.join(left)}, where code does not find the piece"
            )
            chosen, picked_views, code_paired = (), [], False
            context.room_paired.pop(vendor.ref, None)
        elif sizeless and len(sizeless) == len(paired):
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
    mirrored = False
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
        proof = pairing_module.mirror_proof(mine_facts, targets)
        if proof is not None:
            # Code's mirror proof (plan change 6b): the vendor's run of widths reads backwards on
            # the paired drawing's line, and never forwards. Left and right swap in later checks.
            mirrored = True
            run = ", ".join(pairing_module.size_words(v) for v in proof.parts)
            hand = (
                f"code's mirror proof: the vendor's widths {run} read in reverse order on the "
                "architect's drawing (a mirrored unit)"
            )
            why = f"{why}; {hand}: left and right are swapped when ends are compared"
        elif (
            hand is None
            and swapped is not None
            and any(pairing_module.size_match(mine_facts, t).mirrored for t in targets)
        ):
            # The vendor's parts read backwards on the paired drawing's line (too few to prove).
            hand = "the vendor's parts read in reverse order on the architect's: a mirrored unit"
        if hand is None and not mirrored and by_view is not None:
            # Per drawing: the vendor's fixtures read only backwards on a paired view.
            hand = _hand_by_fixtures(vendor.ref, picked_views, by_view.signatures)
        if hand is not None and not mirrored:
            why = f"{why}; {hand}: sizes are compared, positions from an end are not"
    # A page the AI page matcher is sure of, or code's own clear pick, other than the tag's plan
    # (owner's rule, 25 Oct: "no drawing to compare with" only when code is sure): the vendor's
    # tag may be its wrong number for the piece, and that page may draw it.
    held = (
        _held_page(ranked, code_pick, answer or saved_answer)
        if tag_plan is not None and not chosen
        else None
    )
    if _held_weakly(held, ranked, misled):
        why = f"{why}; {_NOT_HELD.format(page=held)}"
        held = None
    if not chosen and not rivals and DRAWING_NEEDED not in why and tag_plan is not None:
        if held is None:
            # The architect's only page printing the vendor's item tag is a plan of tags, and no
            # other page is held: that is its page, with nothing to compare (never "pairing
            # unsure" between other pages, nor "no drawing found").
            why = _needed(
                f"{_tag_only_words(tag_plan)}; {why.removeprefix(f'{pairing_module.UNPAIRED}: ')}"
            )
        else:
            # Both are named for a person: the held page and the plan printing the tag.
            rivals = (held, tag_plan.item)
            why = (
                f"{pairing_module.UNPAIRED}: its item tag {tag_plan.printed} is printed on a plan, "
                f"{tag_plan.item}, with no sizes, but {held} is held for it "
                f"({why.removeprefix(f'{pairing_module.UNPAIRED}: ')})"
            )
    if not chosen and not rivals and DRAWING_NEEDED not in why and saved is not None:
        # The item, not the project: the AI page matcher looked at every architect page of the
        # project and named only drawings that give no sizes of it, and code agrees.
        shown_only = (
            _shown_only(saved[1], everything, item_tags, context, by_id, project)
            if saved_shown and set(by_id) <= set(saved_shown)
            else None
        )
        if shown_only is not None:
            why = _needed(f"{shown_only}; {why.removeprefix(f'{pairing_module.UNPAIRED}: ')}")
    if not chosen and not rivals and DRAWING_NEEDED not in why:
        honest = _honest_unpaired(vendor, everything, item_tags, project, facts_of, context)
        prefix = f"{pairing_module.UNPAIRED}: "
        if honest is not None:
            why = f"{honest}; {why.removeprefix(prefix)}"
    # The saved answer as given: a pick code rules out now is still what the matcher was sure of.
    unsure = (
        ()
        if chosen or DRAWING_NEEDED in why
        else _unsure(ranked, code_pick, answer or saved_answer)
    )
    if not unsure and not chosen and code_paired and DRAWING_NEEDED not in why:
        # Code's own pass named the item, and something later (which view) is unsure.
        unsure = (str(code_pick),)
    if rivals and not chosen:
        # The architect's own tag names one page, code and the matcher another: both are named.
        unsure = rivals
    if unsure:
        # A page is named (the matcher's, sure or not, or code's own) and the pair is not
        # confirmed: the drawing may be in the upload, so the candidates are named in the
        # reason, never "drawing needed".
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
        )
        + (
            (
                by_id[room_pair.item].model_copy(
                    update={"score": 0.0, "evidence": (_rooms_words(room_pair),), "strong": True}
                ),
            )
            if room_pair is not None and all(s.ref != room_pair.item for s in ranked)
            else ()
        ),
        code_pick=code_pick,
        asked=asked if by_view is None else by_view.asked,
        answer=None if answer is None else answer.matches,
        chosen=chosen,
        views=tuple(picked_views),
        why=why,
        needed=f": {DRAWING_NEEDED}: " in why,
        opposite_hand=hand,
        unsure=unsure,
        code_paired=code_paired,
        mirrored=mirrored,
        **_by_view_fields(by_view, answer_views),
    )


def _item_sheets(
    pairs: dict[str, AcrossPair],
    facts_of: Callable[[Drawing], pairing_module.Facts],
    context: _Across,
) -> bool:
    """The vendor's own sheets of one item (plan change 6): vendor drawings side by side in the
    vendor's file whose set index item titles name the same item (`pairing.same_item`, checked
    printed), of one project, a title no other run of pages carries. When one of them is paired
    and the others are not (pairing unsure, never "drawing needed"), the others are paired with
    the same architect drawing, unless code rules it out for them, the matcher's first pick is
    another page or none, code picked another, or they name another hand or variant. The matcher
    is still asked about them (code's own pass did not pair them). A mirror code proved on one
    sheet is not proved on the others: their ends from the left or right are not compared. In
    place; whether any sheet took a pair."""
    took = False
    order = sorted(pairs, key=lambda ref: (pairs[ref].vendor.file, pairs[ref].vendor.own_page))
    titles = {ref: facts_of(pairs[ref].vendor).index_title for ref in order}

    def project(ref: str) -> str | None:
        vendor = pairs[ref].vendor
        return (
            context.places.get((vendor.file, vendor.own_page)) or projects_module.Place()
        ).project

    runs: list[list[str]] = []
    for ref in order:
        last = runs[-1][-1] if runs else None
        if (
            last is not None
            and pairs[last].vendor.file == pairs[ref].vendor.file
            and pairs[ref].vendor.own_page - pairs[last].vendor.own_page <= 1
            and pairing_module.same_item(titles[last], titles[ref])
            and project(last) == project(ref)
        ):
            runs[-1].append(ref)
        else:
            runs.append([ref])
    seen = Counter(pairing_module.unit_title(titles[run[0]])[0] for run in runs if titles[run[0]])
    for run in runs:
        title = titles[run[0]]
        if len(run) < 2 or not title or seen[pairing_module.unit_title(title)[0]] != 1:
            continue
        anchors = [pairs[r] for r in run if pairs[r].chosen]
        if not anchors or len({(a.chosen, a.views) for a in anchors}) != 1:
            continue
        anchor = anchors[0]
        mine = facts_of(anchor.vendor)
        for ref in run:
            pair = pairs[ref]
            if pair.chosen or pair.needed or pair.views_disagree is not None:
                continue
            open_ = {c.id for c in pair.candidates if c.contradicts is None}
            theirs = facts_of(pair.vendor)
            if (
                not set(anchor.chosen) <= open_
                or (
                    pair.answer is not None
                    and (not pair.answer or pair.answer[0] not in anchor.chosen)
                )
                or (pair.code_pick is not None and pair.code_pick not in anchor.chosen)
                or theirs.hand != mine.hand
                or theirs.markers != mine.markers
            ):
                continue
            pages = ", ".join(
                f"architect page {c.page}" for c in pair.candidates if c.id in anchor.chosen
            )
            hand = anchor.opposite_hand
            if anchor.mirrored:
                hand = (
                    f"its page {anchor.vendor.own_page} of the same item is a mirrored unit "
                    "(code's mirror proof there)"
                )
            pairs[ref] = pair.model_copy(
                update={
                    "chosen": anchor.chosen,
                    "views": anchor.views,
                    "unsure": (),
                    "opposite_hand": hand,
                    "mirrored": False,
                    "why": (
                        f'paired with {pages}: the vendor\'s sheets of one item ("{title}", '
                        f"its page {anchor.vendor.own_page} and this one) and its page "
                        f"{anchor.vendor.own_page} is paired with it"
                        + (
                            f"; {hand}: sizes are compared, positions from an end are not"
                            if hand
                            else ""
                        )
                    ),
                }
            )
            took = True
    return took


def _best_guesses(
    pairs: dict[str, AcrossPair],
    facts_of: Callable[[Drawing], pairing_module.Facts],
    by_item: Mapping[str, AcrossCandidate],
    context: _Across | None = None,
) -> None:
    """Flag mode (Anant, 2026-11-04: "when not sure, the page is still compared and every flag
    says 'best guess: architect page N'"): each vendor drawing left unpaired with a page named for
    it is paired with that page as a best guess (`AcrossPair.guess`), compared whole:

    - "pairing unsure": the first of `AcrossPair.unsure`, in its order (the AI page matcher's pick,
      sure or not, else code's own; the page the architect's printed tag or the vendor's rooms
      name before a page held against it);
    - else the matcher's pick code rules out (another variant or section: code's rule-out never
      blocks a flag; it is in the reason);
    - else code's own clear pick.

    Never a drawing code knows needs another drawing (`AcrossPair.needed`: the project is not in
    the architect's file, the item is only a tag on a plan, its drawing prints no sizes), nor one
    with no page named (the matcher and code found none, or the matcher has not answered yet:
    Continue asks again).

    Per drawing (rebuild plan Step 3, `context.per_view`): the guess is narrowed to the page's
    views (`AcrossPair.page_views`: the matcher's picks on it, else code's best view and the views
    of the same piece), never the whole sheet when it holds several; and the mirror checks run on
    it as on a pair: code's mirror proof (`pairing.mirror_proof`, one elevation against one) and
    the fixtures read backwards (`signature.order_match`) flag it opposite-hand. Then the swap
    audit runs on the guesses (`_swap_guesses`). In place."""
    for ref, pair in list(pairs.items()):
        if pair.chosen or pair.needed:
            continue
        named = [
            *pair.unsure,
            *(pair.answer or ()),
            *((pair.code_pick,) if pair.code_pick is not None else ()),
        ]
        offered = [c for c in dict.fromkeys(named) if c in by_item]
        if not offered:
            continue
        pick = offered[0]
        item = by_item[pick]
        views = dict(pair.page_views).get(pick, ()) if context is not None else ()
        drawn = {v.ref: v for v in item.views}
        views = tuple(v for v in views if v in drawn)
        vendor_facts = facts_of(pair.vendor)
        targets = (
            [facts_of(drawn[v]) for v in views] if views else [_item_facts(item, facts_of, None)[0]]
        )
        hand = next(
            (h for t in targets if (h := pairing_module.opposite_hand(vendor_facts, t))), None
        )
        if views and context is not None and context.per_view:
            proof = pairing_module.mirror_proof(vendor_facts, targets)
            if proof is not None:
                run = ", ".join(pairing_module.size_words(v) for v in proof.parts)
                hand = (
                    f"code's mirror proof on the guess: the vendor's widths {run} read in reverse "
                    "order on the architect's drawing (a mirrored unit)"
                )
            elif hand is None:
                hand = _hand_by_fixtures(pair.vendor.ref, views, context.signatures)
        others = [f"architect page {by_item[c].page}" for c in offered if c != pick]
        reason = pair.why.removeprefix(f"{pairing_module.UNPAIRED}: ")
        if reason.startswith(f"{pairing_module.PAIRING_UNSURE}: ") and "?; " in reason:
            # Its candidates are named apart; the rest is why code could not confirm it.
            reason = reason.split("?; ", 1)[1]
        why = (
            f"{guess_words(item.page)} (not confirmed"
            + (
                f"; other candidate{'s' if len(others) > 1 else ''} {' and '.join(others)}"
                if others
                else ""
            )
            + f"): {reason}"
            + (f"; {hand}: sizes are compared, positions from an end are not" if hand else "")
        )
        if views:
            on = ", ".join(f'"{drawn[v].titles[0]}"' if drawn[v].titles else v for v in views)
            why = f"{why}; compared with its view{'s' if len(views) > 1 else ''} {on}"
        pairs[ref] = pair.model_copy(
            update={
                "chosen": (pick,),
                "views": views,
                "guess": True,
                "unsure": tuple(offered),
                "opposite_hand": hand,
                "mirrored": False,
                "why": why,
            }
        )


def _swap_guesses(
    pairs: dict[str, AcrossPair],
    facts_of: Callable[[Drawing], pairing_module.Facts],
    by_item: Mapping[str, AcrossCandidate],
    context: _Across,
) -> None:
    """The pair-swap audit on best guesses (per drawing, rebuild plan Step 3). The audit on pairs
    (`_swap_audit`) makes two crossed pairs unsure; a guess is unsure already, so here it only
    chooses which of two look-alikes each vendor drawing is guessed on. Two vendor drawings of
    alike signatures (the same room words, unit type and accessible marker), guessed on two
    different drawings, swap their guesses when their printed sizes fit each other's guess
    better, strongly (`pairing.swap_audit` and `pairing.swap_is_strong`: both fit the other's
    better, or one by more than one shared size). Each guess is swapped at most once; both stay
    best guesses, and the reason says so. In place."""

    def key(pair: AcrossPair) -> tuple[frozenset[str], str | None, bool] | None:
        found = context.signatures.get(pair.vendor.ref)
        return None if found is None else (found.rooms, found.unit, found.accessible)

    guessed = [ref for ref, pair in pairs.items() if pair.guess and pair.chosen]
    done: set[str] = set()
    for n, one in enumerate(guessed):
        for two in guessed[n + 1 :]:
            if one in done or two in done:
                continue
            a, b = pairs[one], pairs[two]
            if (a.chosen, a.views) == (b.chosen, b.views) or key(a) is None or key(a) != key(b):
                continue
            first = (facts_of(a.vendor), [facts_of(d) for d in _paired_drawings(a, by_item)])
            second = (facts_of(b.vendor), [facts_of(d) for d in _paired_drawings(b, by_item)])
            found = pairing_module.swap_audit(first, second)
            if found is None or not pairing_module.swap_is_strong(first, second):
                continue
            words = (
                f"the swap audit: the sizes of vendor pages {a.vendor.own_page} and "
                f"{b.vendor.own_page} fit each other's guesses better ({found[0]} points against "
                f"{found[1]}), so the guesses were swapped"
            )
            for ref, mine, theirs in ((one, a, b), (two, b, a)):
                pairs[ref] = mine.model_copy(
                    update={
                        "chosen": theirs.chosen,
                        "views": theirs.views,
                        "unsure": tuple(dict.fromkeys((*theirs.chosen, *mine.unsure))),
                        "opposite_hand": next(
                            (
                                h
                                for d in _paired_drawings(theirs, by_item)
                                if (
                                    h := pairing_module.opposite_hand(
                                        facts_of(mine.vendor), facts_of(d)
                                    )
                                )
                            ),
                            None,
                        ),
                        "why": (
                            f"{guess_words(by_item[theirs.chosen[0]].page)} (not confirmed): "
                            f"{words}; before the swap: {mine.why}"
                        ),
                    }
                )
            done.update((one, two))


def _swap_audit(
    pairs: dict[str, AcrossPair],
    facts_of: Callable[[Drawing], pairing_module.Facts],
    by_item: Mapping[str, AcrossCandidate],
    context: _Across,
) -> None:
    """The pair-swap audit (plan change 6c): two vendor drawings paired with two different
    architect pages of one title (the set index's item title, or one room's views), whose
    printed sizes fit each other's pages better (`pairing.swap_audit`), may be crossed: both
    become "pairing unsure" naming both pages. A pair the architect's own printed item tag made
    (`_Across.tag_paired`: the vendor's tag printed on that page and on no other elevation of the
    architect's) stays, unless the sizes are strong evidence against it
    (`pairing.swap_is_strong`: both drawings fit the other's page better, or one by more than one
    shared size); the other pair, resting on weaker evidence, still becomes "pairing unsure". In
    place."""

    def title(pair: AcrossPair) -> str | None:
        found = {
            pairing_module.unit_title(t)[0]
            for c in pair.chosen
            for d in by_item[c].drawings
            if (t := context.index_titles.get(d.ref))
        }
        if found:
            return f"page:{next(iter(found))}" if len(found) == 1 else None
        # No page title: one room's views ("CONFERENCE ROOM ELEVATION"), never a bare "PLAN".
        views = [v for c in pair.chosen for v in by_item[c].views if v.ref in pair.views]
        keys = {
            pairing_module.room_key(v.titles[0])
            for v in views
            if v.titles and pairing_module.piece_words(v.titles[0])
        }
        return f"view:{next(iter(keys))}" if len(keys) == 1 else None

    paired = [
        (ref, key)
        for ref, pair in pairs.items()
        if pair.chosen and (key := title(pair)) is not None
    ]
    crossed: dict[str, tuple[str, str]] = {}
    for n, (one, key) in enumerate(paired):
        for two, other_key in paired[n + 1 :]:
            a, b = pairs[one], pairs[two]
            if key != other_key or set(a.chosen) & set(b.chosen):
                continue
            first = (facts_of(a.vendor), [facts_of(d) for d in _paired_drawings(a, by_item)])
            second = (facts_of(b.vendor), [facts_of(d) for d in _paired_drawings(b, by_item)])
            found = pairing_module.swap_audit(first, second)
            if found is not None:
                why = (
                    f"the swap audit: the sizes of vendor pages {a.vendor.own_page} and "
                    f"{b.vendor.own_page} fit each other's architect pages better ({found[0]} "
                    f"points against {found[1]})"
                )
                strong = pairing_module.swap_is_strong(first, second)
                for ref, other in ((one, two), (two, one)):
                    printed = {
                        context.tag_paired.get(ref),
                        context.room_paired.get(ref),
                        context.sheet_paired.get(ref),
                    }
                    if strong or not printed & set(pairs[ref].chosen):
                        crossed.setdefault(ref, (other, why))
    for ref, (other, why) in crossed.items():
        pair, theirs = pairs[ref], pairs[other]
        pages = {c.id: c.page for c in pair.candidates}
        either = tuple(dict.fromkeys((*pair.chosen, *theirs.chosen)))
        named = " or ".join(f"architect page {pages[c]}" for c in either if c in pages)
        pairs[ref] = pair.model_copy(
            update={
                "chosen": (),
                "views": (),
                "unsure": either,
                "mirrored": False,
                "opposite_hand": None,
                "views_disagree": None,
                # Unsure now: the matcher is asked (code's own pass is not a pair any more).
                "code_paired": False,
                "why": (
                    f"{pairing_module.UNPAIRED}: {pairing_module.PAIRING_UNSURE}: {named}?; "
                    f"{why}, so the two pairs may be crossed; it was {pair.why}"
                ),
            }
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
        and not s.agree_only
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


def _tags_read(notes_page: Mapping[str, Any] | None) -> list[views_module.Phrase]:
    """The tags read on a page's architect drawings (`notes.json`), with where."""
    out: list[views_module.Phrase] = []
    for one in (notes_page or {}).get("drawings") or []:
        if not isinstance(one, Mapping) or one.get("role") != "architect":
            continue
        for tag in one.get("tags") or []:
            box = _box(tag.get("box")) if isinstance(tag, Mapping) else None
            text = _plain(tag.get("text")) if isinstance(tag, Mapping) else ""
            if box is not None and text:
                out.append((text, box))
    return out


def _architect_text(
    entries: Sequence[Mapping[str, Any]],
    architect_files: frozenset[str],
    dims_pages: Mapping[tuple[str, int], Mapping[str, Any]],
    notes_pages: Mapping[tuple[str, int], Mapping[str, Any]],
    read_index: Mapping[str, Mapping[str, Any]],
    texts: Mapping[tuple[str, int], str],
    context: _Across,
) -> dict[tuple[str, int], tuple[str, ...]]:
    """What every page of the architect's file prints (`_Across.architect_text`): its text layer
    (the page's text, its phrases), the tags read on it, the set index's checked item tags. A
    reference set's pages (the brand prototype, R1) are left out: they are never paired."""
    out: dict[tuple[str, int], tuple[str, ...]] = {}
    for entry in entries:
        key = (str(entry.get("file")), int(entry["page"]))
        if key[0] not in architect_files:
            continue
        place = context.places.get(key)
        if place is not None and place.reference:
            continue
        read = read_index.get(f"{key[0]}#{key[1]}") or {}
        out[key] = (
            texts.get(key) or "",
            *(text for text, _box in views_module.phrases_of(dims_pages.get(key))),
            *(text for text, _box in _tags_read(notes_pages.get(key))),
            *(_plain(t) for t in read.get("item_tags") or []),
        )
    return out


@dataclass(frozen=True, slots=True)
class _TagPage:
    """The architect's page that prints the vendor drawing's item tag (`_tag_page`)."""

    item: str
    """The architect item's id (its page)."""
    page: int
    printed: str
    """The tag as the architect prints it (`XX-801`)."""
    plan: bool
    """The page is a plan that shows the item only as its tag (a key or furniture plan)."""
    views: tuple[str, ...] = ()
    """A sheet of several views: the views that print the tag (an elevation, a section), or the
    views titled as elevations of the tag's room (`room`)."""
    room: str | None = None
    """Paired through the tag's room (`_room_page`): the room's name as the plan prints it, the
    page being the one sheet titling elevations of that room."""
    plan_page: int | None = None
    """Paired through the room: the page of the plan printing the tag."""
    callout: bool = False
    """Paired through the room: the plan's elevation callout beside the tag points to it too."""
    room_words: str | None = None
    """Only a tag on a plan: what code found of the tag's room (`_room_page`), for the reason."""


def _same_project(item: AcrossCandidate, project: str, context: _Across) -> bool:
    """Whether an architect item may be of the vendor drawing's project (`project`, normalised, one
    of the architect's): its page names no project, or the same job (`projects.same_job`: the same
    name, or the name read whole on some sheets and in part on others, "HOTEL NORTH" and "HOTEL
    NORTH, SPRINGFIELD", when no third project of the file shares the part). The one test every
    step uses (a real upload: the vendor's job matched the architect's full name, and the two
    sheets drawing its vanities, whose title blocks print the brand's name alone, were no
    candidates). A page naming a word the vendor's project lacks, on both sides, is another
    project's."""
    own = _project_of(item)
    return own is None or projects_module.same_job(own, project, context.projects)


def _tag_page(
    item_tags: frozenset[str],
    everything: Sequence[AcrossCandidate],
    pool: Sequence[AcrossCandidate],
    project: str | None,
    context: _Across,
) -> _TagPage | None:
    """The item's architect page by the architect's own printed tag (owner, 2026-10-31): the
    vendor prints its item tag (`XX-801`) in its title block, and the architect prints the same
    tag, exactly (`pairing.printed_item_tag`, a top's `-T` or a sink's `-SK` suffix allowed), on
    the drawing of the piece. When the tag is printed on one to `TAG_PAGES_MAX` pages of the
    architect's file (a reference set's left out), every one a drawing of the vendor's project
    (`_same_project`; the set index must know the project: never across projects):

    - exactly one of them is not a plan of tags (an elevation, a section, an item's sheet), code
      does not rule it out (`pool`: another variant or section is never paired), and on a sheet
      of several views one or more of its views print the tag: that page (those views);
    - all of them are key or furniture plans, and the room the first plan prints the tag in has
      its elevations titled on exactly one sheet of the project (`_room_page`, owner,
      2026-10-31): that sheet, its views of the room (any callout beside the tag must point to
      them);
    - all of them are key or furniture plans, no room pairs it, and nothing points from the tag
      to an elevation (no callout within `CALLOUT_REACH_PT` of it, the reach `_linked` follows; a
      room's elevation marker further off is the room's): the first plan, "only as a tag on a
      plan", with what code found of its room.

    Several of the vendor's item tags must name the same page. Else `None`: the tag does not
    decide, and the pairing goes on as before."""
    if not item_tags or project is None or not context.projects:
        return None
    by_place = {(c.file, c.page): c for c in everything if not c.reference}
    open_ = {c.id: c for c in pool}
    found: list[_TagPage] = []
    for tag in sorted(item_tags):
        if tag not in context.tag_pages:
            context.tag_pages[tag] = tuple(
                sorted(
                    key
                    for key, text in context.architect_text.items()
                    if pairing_module.printed_item_tag(text, tag) is not None
                )
            )
        pages = context.tag_pages[tag]
        if not pages:
            continue
        if len(pages) > TAG_PAGES_MAX:
            return None
        items = [by_place.get(key) for key in pages]
        if any(item is None or not _same_project(item, project, context) for item in items):
            return None
        printing = [item for item in items if item is not None]
        shows = [item for item in printing if not item.key_plan]
        if len(shows) > 1:
            return None
        if shows:
            item = open_.get(shows[0].id)
            if item is None:
                return None
            printed = pairing_module.printed_item_tag(
                [text for text, _box in context.printed.get(item.id, ())], tag
            )
            views: tuple[str, ...] = ()
            if item.views:
                views = tuple(
                    view.ref
                    for view in item.views
                    if any(
                        pairing_module.printed_item_tag([text], tag) is not None
                        and _distance(_centre(box), view.box) == 0
                        for text, box in context.printed.get(item.id, ())
                    )
                )
                if not views:
                    return None
            found.append(_TagPage(item.id, item.page, printed or tag, False, views))
            continue
        plan = printing[0]
        spots = [
            box
            for text, box in context.printed.get(plan.id, ())
            if pairing_module.printed_item_tag([text], tag) is not None
        ]
        marks = [m for p in context.plans if p.item == plan.id for m in p.marks]
        near = [
            m
            for m in marks
            if any(views_module.gap(box, m.box) <= CALLOUT_REACH_PT for box in spots)
        ]
        printed = pairing_module.printed_item_tag(
            [text for text, _box in context.printed.get(plan.id, ())], tag
        )
        room, room_words = _room_page(spots, plan, open_, near, context)
        if room is not None:
            # The one sheet titling elevations of the tag's room (owner, 2026-10-31).
            found.append(dataclasses.replace(room, printed=printed or tag))
            continue
        if near:
            # The plan's elevation callout beside the tag points from it to the elevations that
            # may draw it (`_linked`'s reach): not "only as a tag", the pairing goes on as before.
            return None
        found.append(_TagPage(plan.id, plan.page, printed or tag, True, room_words=room_words))
    if not found or len({(f.item, f.plan, f.views) for f in found}) != 1:
        return None
    return found[0]


#: Words that name a drawing, a note or a title block's field, never a room (`_room_labels`).
_NOT_ROOM: Final = frozenset(
    {
        "CHECKED",
        "DATE",
        "DETAIL",
        "DETAILS",
        "DRAWN",
        "ELEVATION",
        "ELEVATIONS",
        "ISSUE",
        "LEGEND",
        "NOTE",
        "NOTES",
        "PLAN",
        "PLANS",
        "REMARKS",
        "REV",
        "REVISION",
        "SCALE",
        "SCHEDULE",
        "SECTION",
        "SECTIONS",
        "SHEET",
        "TITLE",
        "TYP",
    }
)
#: A room's name as a plan prints it: capital letters and the signs of a name, never a digit (an
#: item tag, a size, a sheet number) nor a colon (a title block's field).
_ROOM_NAME: Final = re.compile(r"[A-Z][A-Z'&/. -]*")
#: A view's title naming a room's elevation, signs and spaces dropped (`_room_key`):
#: `STAFFLOUNGEELEVATION`.
_ROOM_ELEVATION: Final = re.compile(r"(?P<room>[A-Z]+?)ELEVATIONS?")


def _room_key(text: str) -> str:
    """A room's name for comparing: its letters only, so `STAFF LOUNGE` and `STAFFLOUNGE` (a plan's
    label and an elevation's title) are one room."""
    return re.sub(r"[^A-Z]", "", text.upper())


def _overprinted(word: str) -> bool:
    """Whether a word is a shorter one with each letter read twice (`IIDD`): text printed twice
    over itself, as a callout's `ID` is, never a room's name."""
    return len(word) % 2 == 0 and word[::2] == word[1::2]


def _room_labels(phrases: Sequence[views_module.Phrase]) -> list[views_module.Phrase]:
    """The room names a plan prints, with where: a phrase of capital letters (`_ROOM_NAME`) with a
    word of three letters or more, none of them `_NOT_ROOM`, left of the sheet's title block. A
    name printed on two lines ("COPY" over "ROOM", each line its own phrase) is one name."""
    edge = views_module.title_block_left(phrases)
    names: list[views_module.Phrase] = []
    for text, box in phrases:
        plain = " ".join(str(text).split())
        words = set(re.findall(r"[A-Z]+", plain))
        if (
            _ROOM_NAME.fullmatch(plain)
            and any(len(word) >= 3 and not _overprinted(word) for word in words)
            and not words & _NOT_ROOM
            and (edge is None or _centre(box)[0] < edge)
        ):
            names.append((plain, box))
    merged: list[views_module.Phrase] = []
    for text, box in sorted(names, key=lambda one: (one[1][1], one[1][0])):
        for n, (above, over) in enumerate(merged):
            line = max(over[3] - over[1], box[3] - box[1])
            overlap = min(over[2], box[2]) - max(over[0], box[0])
            narrower = min(over[2] - over[0], box[2] - box[0])
            if 0 <= box[1] - over[3] <= line / 2 and overlap >= narrower / 2:
                merged[n] = (f"{above} {text}", _union([over, box]))
                break
        else:
            merged.append((text, box))
    return merged


def _room_elevations(
    room: str, pool: Mapping[str, AcrossCandidate], context: _Across
) -> dict[str, tuple[str, ...]]:
    """The sheets of `pool` titling elevations of the room `room` (`_room_key`): a view's title
    is the room's name and ELEVATION, nothing else ("STAFF LOUNGE ELEVATION"; "TYPICAL STAFF
    LOUNGE ELEVATION" or "STAFF LOUNGE DETAIL" is not). Each sheet with its views so titled (none: a
    sheet of one view so titled, `views.find_views` on what it prints)."""

    def names_room(title: str) -> bool:
        found = _ROOM_ELEVATION.fullmatch(_room_key(title))
        return found is not None and found.group("room") == room

    out: dict[str, tuple[str, ...]] = {}
    for item in pool.values():
        if item.views:
            views = tuple(v.ref for v in item.views if any(names_room(t) for t in v.titles))
            if views:
                out[item.id] = views
        elif any(
            names_room(view.title)
            for view in views_module.find_views(context.printed.get(item.id, ()))
        ):
            out[item.id] = ()
    return out


def _room_page(
    spots: Sequence[Box],
    plan: AcrossCandidate,
    pool: Mapping[str, AcrossCandidate],
    near: Sequence[views_module.Callout],
    context: _Across,
) -> tuple[_TagPage | None, str | None]:
    """The architect's sheet of an item it shows only as its tag on a plan, through the tag's
    room (owner, 2026-10-31): a design set titles its elevations by room ("STAFF LOUNGE
    ELEVATION"), never by item tag, and prints the tags only on its furniture plans.

    The tag's room is the room name printed nearest the tag on the plan (`_room_labels`), within
    `ROOM_REACH_PT`. Exactly one sheet of the vendor's project (`pool`) titles elevations of that
    room (`_room_elevations`), and either the next other room name is `ROOM_CLEAR` times as far
    or more, or the plan's elevation callout beside the tag (`near`) points to the room's
    elevations: that sheet, its views of the room. A callout beside the tag pointing anywhere else
    pairs nothing. Else `None`, and what code found of the room, for the reason (`None`: no room
    name near the tag)."""
    if not spots:
        return None, None
    ranked = sorted(
        (min(views_module.gap(spot, box) for spot in spots), text)
        for text, box in _room_labels(context.printed.get(plan.id, ()))
    )
    if not ranked or ranked[0][0] > ROOM_REACH_PT:
        return None, None
    distance, name = ranked[0]
    room = _room_key(name)
    rival = next(((d, t) for d, t in ranked[1:] if _room_key(t) != room), None)
    clear = rival is None or rival[0] >= ROOM_CLEAR * max(distance, 1.0)
    sheets = _room_elevations(room, pool, context)
    if len(sheets) != 1:
        where = (
            "no sheet of the architect's titles an elevation of it"
            if not sheets
            else f"elevations of it are titled on {' and '.join(sorted(sheets))}"
        )
        return None, f"the room name printed nearest its tag on that plan is {name}; {where}"
    ((item_id, views),) = sheets.items()
    item = pool[item_id]
    # The plan's project when the set index read one on it, else the room's sheet's (a plan whose
    # page names no project: its callouts are read in the project of the sheet they point to).
    own = context.places.get((item.file, item.page)) or projects_module.Place()
    keys = (next((p.project for p in context.plans if p.item == plan.id), None), own.project)

    def view_of(sheet: str, number: str) -> str | None:
        return next(
            (ref for key in keys if (ref := context.sheets.get((key, sheet, number))) is not None),
            None,
        )

    def points_here(mark: views_module.Callout) -> bool:
        refs = [view_of(mark.sheet, number) for number in mark.numbers]
        return bool(refs) and all(
            ref is not None
            and context.view_page.get(ref) == item_id
            and (not views or ref in views)
            for ref in refs
        )

    if any(not points_here(mark) for mark in near):
        return None, None
    if not clear and not near:
        other = rival[1] if rival is not None else ""
        return None, (
            f"its tag is printed between the rooms {name} and {other} on that plan: which room "
            "it is in is not clear"
        )
    return (
        _TagPage(
            item_id,
            item.page,
            "",
            False,
            views,
            room=name,
            plan_page=plan.page,
            callout=bool(near),
        ),
        None,
    )


# ---- a vendor's item number that does not fit (2026-11-04) --------------------------------------


@dataclass(frozen=True, slots=True)
class _VendorItem:
    """One of the vendor's drawings, for `_misleading_tag` (the page's one vendor drawing)."""

    page: int
    """Its page in its own file."""
    title: str
    """The item title the set index read on its page (checked printed)."""
    words: frozenset[str]
    """The title's words that name the item (`_item_words`): empty when it names none."""
    tags: frozenset[str]
    """The item tags it prints (normalised, `pairing.item_tags_in`; the vendor's item tags)."""


#: Words of a vendor's sheet title that name its kind of drawing or part, never which item it is:
#: "COFFEE BAR B - MILLWORK" and "XX-512 COFFEE BAR B" (its cabinets' and its top's sheets) are one
#: item.
_KIND_WORDS: Final = frozenset(
    {
        "AT",
        "AND",
        "BASE",
        "BY",
        "CABINET",
        "CABINETS",
        "CASEWORK",
        "COUNTER",
        "COUNTERS",
        "COUNTERTOP",
        "COUNTERTOPS",
        "DETAIL",
        "DETAILS",
        "DRAWING",
        "FOR",
        "MILLWORK",
        "OF",
        "QUARTZ",
        "SHOP",
        "STONE",
        "THE",
        "TOP",
        "TOPS",
        "UPPER",
        "UPPERS",
        "WITH",
    }
)
#: An item tag inside a title (`XX-512`, `XX512T`, numbered as an item: three or four digits, as
#: `pairing.item_tags_in` reads it): never a word of the item's name ("TYPE 12" stays).
_TITLE_TAG: Final = re.compile(r"\b[A-Z]{1,4}[-_]?\d{3,4}[A-Z]{0,3}\b")


def _item_words(title: str, project: str | None) -> frozenset[str]:
    """The words of a vendor sheet's item title that name its item: its tags, the words of its
    kind of drawing or part (`_KIND_WORDS`) and of the job's name left out ("COFFEE BAR B -
    MILLWORK" and "XX-512 Coffee Bar B" are both COFFEE, BAR, B). Empty unless a word of 4 letters or more is left:
    a title naming only its kind names no item."""
    plain = _TITLE_TAG.sub(" ", dashes_to_hyphen(title).upper())
    job = set(_TITLE_WORD.findall((project or "").upper()))
    words = frozenset(
        w for w in _TITLE_WORD.findall(plain) if w not in _KIND_WORDS and w not in job
    )
    return words if any(len(w) >= 4 and w.isalpha() for w in words) else frozenset()


def _spot_rooms(plan: str, box: Box, context: _Across) -> list[tuple[float, str]]:
    """The room names a plan prints within `ROOM_REACH_PT` of a spot (`_room_labels`), nearest
    first, with how far."""
    return sorted(
        (gap, text)
        for text, label in _room_labels(context.printed.get(plan, ()))
        if (gap := views_module.gap(box, label)) <= ROOM_REACH_PT
    )


def _clear_room(near: Sequence[tuple[float, str]]) -> str | None:
    """The room a spot is in (`_room_page`'s rule): the nearest room name, when the next other
    room's name is `ROOM_CLEAR` times as far or more; else `None`."""
    if not near:
        return None
    distance, name = near[0]
    rival = next((d for d, t in near[1:] if _room_key(t) != _room_key(name)), None)
    return name if rival is None or rival >= ROOM_CLEAR * max(distance, 1.0) else None


def _tag_spots(tag: str, project: str | None, context: _Across) -> list[tuple[_KeyPlan, str, Box]]:
    """Where the architect's plans of the vendor's project print an item tag: (the plan, the tag
    as printed, where)."""
    return [
        (plan, printed, box)
        for plan in context.plans
        if project is None
        or plan.project is None
        or projects_module.same_job(plan.project, project, context.projects)
        for key, printed, box in plan.tags
        if key == tag
    ]


def _named_room(title: str, context: _Across) -> str | None:
    """The room a vendor sheet's title names: a room the architect's plans place an item tag in
    (`_clear_room`), its words printed in the title side by side ("CABINETS @ COFFEE BAR" names
    COFFEE BAR). The longest when one name holds another; `None` when the title names none, or two.
    """
    if context.room_names is None:
        context.room_names = {}
        for plan in context.plans:
            for _key, _printed, box in plan.tags:
                found = _clear_room(_spot_rooms(plan.item, box, context))
                if found is not None:
                    context.room_names.setdefault(_room_key(found), found)
    words = _TITLE_WORD.findall(dashes_to_hyphen(title).upper())
    named = [
        name
        for name in context.room_names.values()
        if (room := _TITLE_WORD.findall(name.upper()))
        and any(words[i : i + len(room)] == room for i in range(len(words) - len(room) + 1))
    ]
    longest = [n for n in named if not any(n != o and _room_key(n) in _room_key(o) for o in named)]
    return longest[0] if len({_room_key(n) for n in longest}) == 1 else None


def _printed_tag(tag: str, texts: Iterable[str]) -> str:
    """A normalised item tag (`XX512`) as `texts` print it (`XX-512`), else with a hyphen."""
    found = pairing_module.printed_item_tag(texts, tag)
    if found is not None:
        return found
    parts = re.match(r"([A-Z]+)(\d+)$", tag)
    return f"{parts.group(1)}-{parts.group(2)}" if parts else tag


def _misleading_tag(
    vendor: Drawing, item_tags: frozenset[str], project: str | None, context: _Across
) -> WrongTag | None:
    """The vendor's item number that points to an architect item that does not fit (Anant,
    2026-11-04: "pairing must survive a vendor's wrong item number"), or `None`. Three things
    printed on the drawings must agree, so the number is never doubted on one of them alone:

    1. **It does not fit.** The vendor's sheet title names a room (`_named_room`), and the
       architect's plans print the vendor's number only away from it (no room name within reach
       of it is that room), or the vendor prints the same number on its sheet of another item in
       another room.
    2. **The vendor's own other sheet of the same item** (its title names the same item,
       `_item_words`) prints one other item number.
    3. **The architect prints that other number in the room** the vendor's title names (the
       room nearest it, clearly, `_clear_room`), and in no other room: `confirmed`, and the
       pairing goes by the other number.

    With 1 and 2 and not 3, the vendor's two numbers for one item stay in doubt (`confirmed`
    false): neither number pairs it. With 1 alone (a vendor naming the room its own way, its
    sheets of the item agreeing on the number), nothing changes."""
    mine = context.vendor_items.get(vendor.ref)
    if len(item_tags) != 1 or mine is None or not mine.words:
        return None
    (tag,) = item_tags
    room = _named_room(mine.title, context)
    if room is None:
        return None
    spots = _tag_spots(tag, project, context)
    near = [(plan, _spot_rooms(plan.item, box, context)) for plan, _p, box in spots]
    tag_rooms = tuple(dict.fromkeys(name for _plan, found in near for _d, name in found[:2]))
    if any(_room_key(name) == _room_key(room) for _plan, found in near for _d, name in found):
        return None  # the architect prints it in the room the vendor names: it fits
    elsewhere = tuple(
        sorted(
            other.page
            for ref, other in context.vendor_items.items()
            if ref != vendor.ref
            and tag in other.tags
            and other.words != mine.words
            and (theirs := _named_room(other.title, context)) is not None
            and _room_key(theirs) != _room_key(room)
        )
    )
    if not (tag_rooms or elsewhere):
        return None
    same = [
        other
        for ref, other in context.vendor_items.items()
        if ref != vendor.ref and other.words == mine.words
    ]
    others = {t for other in same for t in other.tags} - {tag}
    if len(others) != 1:
        return None
    (right,) = others
    pages = tuple(sorted({o.page for o in same if right in o.tags}))
    printed = _printed_tag(
        tag, [mine.title, *vendor.tags, *vendor.notes, *(p for _pl, p, _b in spots)]
    )
    shown = _printed_tag(right, [o.title for o in same if right in o.tags])
    places = [
        (plan, box, _clear_room(_spot_rooms(plan.item, box, context)))
        for plan, _p, box in _tag_spots(right, project, context)
    ]
    confirmed = bool(places) and all(
        found is not None and _room_key(found) == _room_key(room) for _plan, _box, found in places
    )
    rooms_on: dict[int, list[str]] = {}
    for plan, found in near:
        for _d, name in found[:2]:
            if name not in rooms_on.setdefault(plan.page, []):
                rooms_on[plan.page].append(name)
    where = [
        f"the architect's plan (page {page}) prints it near the {' and the '.join(names)}"
        for page, names in rooms_on.items()
        if names
    ]
    if elsewhere:
        where.append(
            f"the vendor prints it on its {'page' if len(elsewhere) == 1 else 'pages'} "
            f"{', '.join(str(p) for p in elsewhere)} of another item"
        )
    theirs = next(o.title for o in same if right in o.tags)
    why = (
        f"the vendor's item number {printed} does not fit: this sheet names the room {room}, but "
        f"{'; '.join(where)}; the vendor's own {'page' if len(pages) == 1 else 'pages'} "
        f"{', '.join(str(p) for p in pages)} of the same item (\"{theirs}\") "
        f"{'prints' if len(pages) == 1 else 'print'} {shown}"
    )
    first = places[0] if confirmed else None
    why += (
        f", and the architect's plan (page {first[0].page}) prints {shown} in the {room}: code "
        f"goes by {shown}"
        if first is not None
        else f", which code does not find in the {room} on the architect's plans: code goes by "
        "neither number"
    )
    return WrongTag(
        printed=printed,
        room=room,
        tag_rooms=tag_rooms,
        tag_page=next((plan.page for plan, found in near if found), None),
        elsewhere=elsewhere,
        other=shown,
        other_pages=pages,
        confirmed=confirmed,
        arch_file=first[0].file if first is not None else None,
        arch_page=first[0].page if first is not None else None,
        arch_box=first[1] if first is not None else None,
        why=why,
    )


@dataclass(frozen=True, slots=True)
class _RoomPair:
    """The architect page the vendor's printed room numbers pair a drawing with
    (`_room_numbers_page`)."""

    item: str
    """The architect item's id (its page)."""
    rooms: tuple[str, ...]
    """The vendor's rooms, all printed on the page."""
    printed: int
    """How many room numbers the page prints."""
    sheets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Another sheet the page's elevation callouts point to (its item's id), with the views they
    point to: it is added when code finds the vendor's piece on one of them (`_pointed_view`)."""
    lists: bool = False
    """Both pages are room tables listing most of the job's rooms, the same rooms
    (`rooms.room_list_page`): a vendor's room matrix and the architect's room schedule."""


def _room_numbers(context: _Across, short: bool) -> dict[tuple[str, int], frozenset[str]]:
    """Each page of `architect_text`'s room numbers (`rooms.printed_rooms`; `short`: a unit's short
    numbers too), the title block's (`rooms.title_block`) left out. Kept in `context`."""
    if short not in context.room_numbers:
        found = {
            key: rooms_module.printed_rooms(text, short)
            for key, text in context.architect_text.items()
        }
        block = rooms_module.title_block(found)
        context.room_numbers[short] = {key: numbers - block for key, numbers in found.items()}
    return context.room_numbers[short]


def _page_in_project(key: tuple[str, int], project: str | None, context: _Across) -> bool:
    """Whether a page of the architect's file may be of the vendor drawing's project (`project`):
    its page names no project, or the same job (`_same_project`'s test)."""
    if project is None:
        return True
    place = context.places.get(key) or projects_module.Place()
    own = projects_module.key(place.name) if place.name else None
    return own is None or projects_module.same_job(own, project, context.projects)


def _pointed_sheets(
    plan: AcrossCandidate, pool: Mapping[str, AcrossCandidate], context: _Across
) -> dict[str, tuple[str, ...]]:
    """The other sheets of `pool` a plan's elevation callouts point to (`_Across.callouts`), each
    with the views they point to. A callout's view is looked up in the plan's project, else in any
    project of the file that has the sheet; a view two projects' sheets could be is left out."""
    own = (context.places.get((plan.file, plan.page)) or projects_module.Place()).project
    out: dict[str, list[str]] = {}
    for mark in context.callouts.get(plan.id, ()):
        for number in mark.numbers:
            ref = context.sheets.get((own, mark.sheet, number))
            if ref is None:
                refs = {
                    view
                    for (_project, sheet, at), view in context.sheets.items()
                    if sheet == mark.sheet and at == number
                }
                ref = refs.pop() if len(refs) == 1 else None
            item = context.view_page.get(ref) if ref is not None else None
            if item is not None and item != plan.id and item in pool and ref is not None:
                views = out.setdefault(item, [])
                if ref not in views:
                    views.append(ref)
    return {item: tuple(views) for item, views in out.items()}


def _room_numbers_page(
    vendor: Drawing,
    mine: pairing_module.Facts,
    everything: Sequence[AcrossCandidate],
    pool: Sequence[AcrossCandidate],
    project: str | None,
    facts_of: Callable[[Drawing], pairing_module.Facts],
    context: _Across,
) -> _RoomPair | None:
    """The architect's page of a vendor drawing by the room numbers both print (2026-11-04): the
    vendor's sheet lists the rooms its piece is for (`_Across.vendor_rooms`, two or more, read
    after a room or unit label), and exactly one page of the architect's file in the vendor's
    project is about those rooms (`rooms.room_page`): it prints every one of them, they are at
    least half of the room numbers it prints, and it lists at most half of the job's rooms (a room
    schedule, a matrix, a floor or life-safety plan prints many; an enlarged plan of the room type
    prints its few). Pages of the vendor's project only, a reference set's left out, the title
    block's numbers (an address on every page) never rooms.

    Refused (`None`) when the vendor's page prints a job room outside its lists (a list code read
    only in part: never paired on part of it), the page is no architect item (a page with no
    drawing), code rules it out (another variant, an accessible unit), or it is another project's.
    The page may be a key or furniture plan (a suite's plan printing its tags), never paired by
    `rank`; its elevation callouts' sheets (`_pointed_sheets`) go with it."""
    read = context.vendor_rooms.get(vendor.ref)
    if read is None:
        return None
    listed, printed = read
    numbers = _room_numbers(context, rooms_module.has_short(listed))
    mine_project = {
        key: found for key, found in numbers.items() if _page_in_project(key, project, context)
    }
    printed_anywhere = frozenset().union(*mine_project.values()) if mine_project else frozenset()
    if (printed - listed) & printed_anywhere:
        return None
    job = rooms_module.job_rooms(mine_project)
    lists = (
        rooms_module.room_list_page(
            listed,
            {k: v for k, v in context.room_tables.items() if _page_in_project(k, project, context)},
            job,
        )
        if vendor.ref in context.room_list_tables
        else None
    )
    # A room matrix listing most of the job's rooms is about the architect's room schedule
    # listing the same rooms (two lists of the whole job); else the page about its rooms.
    found = lists or rooms_module.room_page(listed, mine_project, job)[0]
    if found is None:
        return None
    by_place = {(c.file, c.page): c for c in everything if not c.reference}
    plan = by_place.get(found.page)
    if plan is None or (project is not None and not _same_project(plan, project, context)):
        return None
    if any(
        s.contradicts is not None
        for s in pairing_module.rank(mine, [_item_facts(plan, facts_of, None)[0]])
    ):
        return None
    return _RoomPair(
        item=plan.id,
        rooms=tuple(sorted(found.rooms)),
        printed=found.printed,
        sheets={} if lists else _pointed_sheets(plan, {c.id: c for c in pool}, context),
        lists=lists is not None,
    )


def _pointed_view(
    vendor: pairing_module.Facts, views: Sequence[pairing_module.Facts]
) -> pairing_module.Scored | None:
    """The view a room plan's callout points to that code finds the vendor's piece on: strong code
    evidence of its own (`Scored.strong`: its sizes, its tag, its title), clearly ahead of the
    other pointed views (`narrow`), or the vendor's overall printed on it alone
    (`_view_by_sizes`). A callout pointing to a view says the view looks at a wall of the room,
    not that the piece is on it (a bath vanity is not on the bedroom's walls): else `None`."""
    if not views:
        return None
    if len(views) == 1:
        (one,) = pairing_module.rank(vendor, views)
        return one if one.contradicts is None and one.strong and one.score > 0 else None
    best, _problem = pairing_module.narrow(vendor, views)
    if best is not None and best.strong:
        return best
    return _view_by_sizes(vendor, views)


def _rooms_words(paired: _RoomPair) -> str:
    """How a drawing was paired by its room numbers (`_room_numbers_page`), in words."""
    if paired.lists:
        return (
            "it is the architect's room table listing the same rooms as the vendor's room table "
            f"({len(paired.rooms)} rooms on both of the {paired.printed} it lists; both list most "
            "of the job's rooms: a room matrix and the room schedule)"
        )
    rooms = ", ".join(paired.rooms)
    return (
        f"it prints the vendor's room numbers {rooms}, every one, and they are {len(paired.rooms)} "
        f"of the {paired.printed} room numbers it prints (no other architect page of the project "
        "is about these rooms; a schedule or plan listing many rooms says nothing)"
    )


#: Why a page is not held against the architect's page the vendor's other item number names
#: (`_held_weakly`).
_NOT_HELD: Final = (
    "{page} is not held for it: code's own evidence for it is weak, and the AI page matcher "
    "picked it from the sheet that prints the wrong item number"
)


def _held_weakly(
    held: str | None, ranked: Sequence[pairing_module.Scored], misled: WrongTag | None
) -> bool:
    """Whether a page held against the item's architect page (`_held_page`) is let go: the
    vendor's printed number did not fit, and its own other sheet of the item and the architect's
    plan (the room) agree on another (`_misleading_tag`, `confirmed`), while code's own evidence
    for the held page is weak (no `strong` evidence). The hold is there because a vendor's number
    may be wrong (owner's rule, 25 Oct); here code found the right one on three printed things,
    and the matcher picked the held page from the sheet printing the wrong one."""
    return (
        held is not None
        and misled is not None
        and misled.confirmed
        and not any(s.ref == held and s.strong for s in ranked)
    )


def _held_page(
    ranked: Sequence[pairing_module.Scored],
    code_pick: str | None,
    answer: pairing_module.MatchAnswer | None,
) -> str | None:
    """The architect page held for a vendor drawing although no pair is confirmed: the AI page
    matcher's first pick when it is sure of it, or code's own clear pick; one code rules out
    (another variant, another section) holds nothing."""
    open_ = {s.ref for s in ranked if s.contradicts is None}
    if answer is not None and answer.confident and answer.matches and answer.matches[0] in open_:
        return answer.matches[0]
    return code_pick if code_pick in open_ else None


def _tag_only_words(tagged: _TagPage) -> str:
    """Why an item whose architect page is a plan of tags is not compared: the plan's page and
    the tag, in the words the result screen keys on (`full_check_plan.tag_only_words`), and what
    code found of the tag's room (`_room_page`: no sheet titles its elevations, several do, or
    the room is not clear)."""
    room = "" if tagged.room_words is None else f"; {tagged.room_words}"
    return (
        f"the architect shows this item only as a tag on a plan ({tagged.printed} on its page "
        f"{tagged.page}; no elevation or section of the architect's prints it), with no "
        f"sizes{room}"
    )


def _room_words(paired: _TagPage) -> str:
    """How an item was paired through its room (`_room_page`), in words."""
    callout = (
        ", and the plan's elevation callout beside the tag points to them" if paired.callout else ""
    )
    return (
        f"the architect's plan (page {paired.plan_page}) prints the vendor's item tag "
        f"{paired.printed} in the room {paired.room}, and {paired.item} is the one sheet titling "
        f"elevations of that room{callout}"
    )


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
        or any(
            c.reference or _project_of(c) is None or not _same_project(c, project, context)
            for c in named
        )
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


def _swap_checked(
    vendor: pairing_module.Facts,
    ranked: tuple[pairing_module.Scored, ...],
    code_pick: str | None,
    answer: pairing_module.MatchAnswer | None,
    pool: Sequence[AcrossCandidate],
    facts_of: Callable[[Drawing], pairing_module.Facts],
) -> tuple[tuple[pairing_module.Scored, ...], str | None, pairing_module.SizeMatch | None]:
    """The swap check (`pairing.swap_check`) for a sure AI page matcher's pick that code has no
    pick of its own for, when other architect pages carry the pick's title (a kitchen drawn on four
    sheets; two units drawn alike): the pick's printed sizes must match the vendor's clearly better
    than every other page of its title. Then the pick holds strong code evidence, enough to agree
    with the matcher, never to pair without it (`pairing.with_sizes`); `pairing.agree` still
    decides (never against a contradiction, nor with another item clearly ahead). Otherwise
    `ranked` as it was, and why the swap check could not tell (`None`: it did not apply). With the
    pick's size match when it holds (a mirrored match flags the pair opposite-hand)."""
    if answer is None or not answer.confident or not answer.matches or code_pick is not None:
        return ranked, None, None
    pick = answer.matches[0]
    by = {s.ref: s for s in ranked}
    item = next((c for c in pool if c.id == pick), None)
    if item is None or pick not in by or by[pick].contradicts is not None:
        return ranked, None, None
    mine = _item_facts(item, facts_of, None)[0]
    title = pairing_module.unit_title(mine.index_title)[0] if mine.index_title else ""
    if not title:
        return ranked, None, None
    if vendor.index_title and not _shares_a_word(vendor.index_title, mine.index_title):
        # The vendor's own title names another item ("BENCH" against "KITCHEN"): no look-alikes.
        return ranked, None, None
    siblings = [
        facts
        for c in pool
        if c.id != pick
        and c.id in by
        and by[c.id].contradicts is None
        and (facts := _item_facts(c, facts_of, None)[0]).index_title
        and pairing_module.unit_title(facts.index_title)[0] == title
    ]
    if not siblings:
        return ranked, None, None
    match, problem = pairing_module.swap_check(vendor, mine, siblings)
    if match is None:
        return (
            ranked,
            (
                f'the swap check among the {len(siblings) + 1} pages titled "{mine.index_title}" '
                f"cannot tell: {problem}"
            ),
            None,
        )
    return (
        pairing_module.with_sizes(
            ranked, pick, match, [s.ref for s in siblings], str(mine.index_title)
        ),
        None,
        match,
    )


#: Words of a title too short to name anything ("AT", "OF", a unit type's letter).
_TITLE_WORD_LETTERS: Final = 4
#: Title words that name no item ("TYPE A" and "TYPE B" are two units).
_NOT_NAMING: Final = frozenset({"type", "unit", "units", "with"})


def _naming_words(title: str | None) -> set[str]:
    return {
        w
        for w in pairing_module.unit_title(title)[0].split()
        if len(w) >= _TITLE_WORD_LETTERS and w not in _NOT_NAMING
    }


def _shares_a_word(one: str, other: str | None) -> bool:
    """Whether every naming word of the vendor's title (`one`; `unit_title`'s tidy-up, 4 letters
    or more) starts a word of the page's title (`other`), or the other way round: "STANDARD
    STUDIO KITCHEN" names "STANDARD STUDIO KITCHENETTE", "KITCHEN" names "STANDARD KITCHEN";
    "STANDARD VANITY" never names "STANDARD KITCHEN", nor "BENCH" "KITCHEN"."""
    mine, theirs = _naming_words(one), _naming_words(other)
    return bool(mine) and all(any(w.startswith(t) or t.startswith(w) for t in theirs) for w in mine)


def _view_by_sizes(
    vendor: pairing_module.Facts, views: Sequence[pairing_module.Facts]
) -> pairing_module.Scored | None:
    """The view of a paired sheet whose printed sizes match the vendor drawing's clearly best (the
    swap check among the sheet's views, `pairing.swap_check`), or `None`. Only among look-alike
    views: when a view holds strong evidence (a key plan's callout, its item tag), `narrow`'s
    refusal stands. The vendor's overall must be printed on the view, not only added up; a view
    code rules out (another variant) is never picked, and still counts against the others."""
    if len(views) < 2:
        return None
    ranked = pairing_module.rank(vendor, views)
    if any(s.strong for s in ranked):
        return None
    open_ = {s.ref for s in ranked if s.contradicts is None}
    best = max(views, key=lambda v: pairing_module.size_match(vendor, v).points)
    if best.ref not in open_:
        return None
    others = [v for v in views if v.ref != best.ref]
    match, _ = pairing_module.swap_check(vendor, best, others)
    runner = max((pairing_module.size_match(vendor, v).points for v in others), default=0)
    if match is None or not match.printed or match.points - runner < 2:
        # Two points ahead: the overall printed on this view only, never one point by a sum.
        return None
    return pairing_module.Scored(
        best.ref, 0.0, (f"its sizes match clearly best: it {match.words()}",)
    )


def _unsure(
    ranked: Sequence[pairing_module.Scored],
    code_pick: str | None,
    answer: pairing_module.MatchAnswer | None,
) -> tuple[str, ...]:
    """The architect items a person confirms between when the AI page matcher names one (sure or
    not) and code could not confirm it (`pairing.agree` refused it): the matcher's pick, then
    code's own pick, its own next pick, or code's first other item (code's pick first when code
    rules the matcher's out). When the matcher names none, code's own clear pick alone. Empty
    when neither names a page, or when code rules the matcher's pick out for another variant or
    section and has no pick of its own: only then is the page "drawing needed" (owner,
    2026-10-25: "no drawing to compare with" must be very sure; on a real four-hotel upload 8
    pages whose right drawing was in the upload said "drawing needed" because the matcher was
    not sure of its own, right, pick)."""
    if answer is None:
        return ()
    if not answer.matches:
        return (code_pick,) if code_pick is not None else ()
    by = {s.ref: s for s in ranked}
    pick = answer.matches[0]
    scored = by.get(pick)
    if scored is None:
        return ()
    if pick == code_pick:
        # Code and the matcher agree on the item, and something later (which view) is unsure.
        return (pick,)
    other = code_pick if code_pick not in (None, pick) else None
    if scored.contradicts is not None and other is None:
        # The page whose title is the vendor's item (agree-only evidence) is the one to offer.
        other = next(
            (s.ref for s in ranked if s.contradicts is None and s.titled and s.ref != pick), None
        )
    if scored.contradicts is not None:
        # Code rules the matcher's pick out: code's own pick first when it has one. A unit type
        # alone (a type list code may read wrong) leaves the matcher's page to a person; any other
        # rule-out (another variant, another section) is no drawing of this item.
        if other is not None:
            return (other, pick)
        return (pick,) if "another unit type" in scored.contradicts else ()
    if other is None:
        # The matcher's own next pick, else code's first other item when code holds some
        # evidence for it (never a page offered for nothing).
        other = next(
            (m for m in answer.matches[1:] if m != pick and m in by and by[m].contradicts is None),
            next(
                (s.ref for s in ranked if s.contradicts is None and s.ref != pick and s.score > 0),
                None,
            ),
        )
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
    context: _Across,
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

    mine = [c for c in everything if project is None or _same_project(c, project, context)]
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


# ---- per-drawing pairing (rebuild plan Step 3, 2026-11-05) ---------------------------------------

#: The setting that says what a vendor drawing is paired with: `page` (the default: the whole
#: sheet, as before Step 3) or `view` (each titled view of an architect sheet is a target of its
#: own, `_ranked_by_view`). `view` stays opt-in until a paid run has measured the AI matcher's new
#: question (owner rule, 2026-11-05: nothing changes a live run before it is measured).
PAIRING_TARGETS_ENV: Final = "GV_PAIRING_TARGETS"
#: The AI page matcher is shown at most this many of the architect's drawings: code's best by the
#: ranking and the page signature, at most `MATCH_PER_PAGE` of one sheet (so it sees several).
MATCH_TARGETS: Final = 5
MATCH_PER_PAGE: Final = 2


def per_view_pairing() -> bool:
    """Whether pairing ranks the architect's drawings one by one (`GV_PAIRING_TARGETS` = `view`;
    anything else, or no setting, pairs whole sheets), read from the environment as
    `full_check_plan.flag_mode` is, so every work-list build in the process pairs alike."""
    return os.environ.get(PAIRING_TARGETS_ENV, "page").strip().lower() == "view"


@dataclass(frozen=True, slots=True)
class _ByView:
    """Code's ranking of the architect's drawings one by one for one vendor drawing
    (`_ranked_by_view`)."""

    ranked: tuple[pairing_module.Scored, ...]
    """Every drawing target (a titled view, or a page with no titled views), code's best first,
    with the page signature laid over it (`signature.rerank`)."""
    targets: Mapping[str, AcrossCandidate]
    """Each target by its id, its signature in words."""
    page_of: Mapping[str, str]
    """A target's id -> its page item's id."""
    views: Mapping[str, tuple[str, ...]]
    """A page item's id -> the views of it code compares a pair with (`_view_group`)."""
    asked: tuple[str, ...]
    """The targets the AI page matcher is shown (`_matcher_targets`)."""
    signatures: Mapping[str, signature_module.Signature]
    """Each target's page signature, and the vendor drawing's (by its ref)."""


def _targets(pool: Sequence[AcrossCandidate]) -> list[AcrossCandidate]:
    """The drawing targets of the candidate pages: each titled view of a sheet of several, else
    the whole page; each linked to its page (`page_id`)."""
    out: list[AcrossCandidate] = []
    for item in pool:
        if not item.views:
            out.append(item.model_copy(update={"page_id": item.id}))
            continue
        out.extend(
            AcrossCandidate(
                id=view.ref,
                file=item.file,
                page=item.page,
                drawings=(view,),
                title=item.title,
                section=item.section,
                project=item.project,
                reference=item.reference,
                key_plan=item.key_plan,
                page_id=item.id,
            )
            for view in item.views
        )
    return out


def _page_title(item: AcrossCandidate, context: _Across) -> str | None:
    """The item title the set index read on a page (checked printed), when there is one."""
    return next(
        (context.index_titles[d.ref] for d in item.drawings if d.ref in context.index_titles), None
    )


def _view_facts_of(
    facts_of: Callable[[Drawing], pairing_module.Facts],
    titles: Mapping[str, str],
    counts: Mapping[str, int],
) -> Callable[[Drawing], pairing_module.Facts]:
    """`facts_of` for drawing targets: a view carries its page's item title (`titles`, by the
    view's ref) and the title's variant and hand words (a sheet titled "ACCESSIBLE VANITY" draws
    every view of it accessible), and how many titled views its sheet holds (`counts`): a view of
    a sheet of several is never ruled out by its kind alone (`pairing.rank`'s plan against an
    elevation), as the sheet was not; a vendor sheet titled "PLAN" often draws its elevation
    too."""

    def facts(drawing: Drawing) -> pairing_module.Facts:
        found = facts_of(drawing)
        if drawing.ref in counts:
            found = dataclasses.replace(found, view_count=counts[drawing.ref])
        title = titles.get(drawing.ref)
        if not title:
            return found
        variant, hand = pairing_module.markers_in([title])
        return dataclasses.replace(
            found,
            index_title=found.index_title or title,
            markers=found.markers | variant,
            hand=found.hand | hand,
        )

    return facts


def _signature(
    drawing: Drawing,
    facts: pairing_module.Facts,
    context: _Across,
    titles: Sequence[str],
    sheet: str | None = None,
) -> signature_module.Signature:
    """A drawing's page signature (`signature.signature_of`), worked out once per build."""
    known = context.signatures.get(drawing.ref)
    if known is None:
        known = signature_module.signature_of(
            facts,
            [t for t in titles if t],
            context.phrases_at(drawing) if context.phrases_at is not None else (),
            unit=context.unit_aliases.get(drawing.ref),
            sheets=(sheet,) if sheet else (),
            views=context.index_views.get(drawing.ref, ()),
        )
        context.signatures[drawing.ref] = known
    return known


def signature_words(found: signature_module.Signature) -> tuple[str, ...]:
    """A signature in words for the AI page matcher's caption: its room words, unit type, the
    accessible marker and at most four item tags."""
    words = [w.lower() for w in sorted(found.rooms)]
    if found.unit:
        words.append(found.unit.lower().replace("-", " "))
    if found.accessible:
        words.append("accessible")
    words.extend(sorted(found.tags)[:4])
    return tuple(words)


def _view_group(
    page_id: str,
    ranked: Sequence[pairing_module.Scored],
    page_of: Mapping[str, str],
    signatures: Mapping[str, signature_module.Signature],
) -> tuple[str, ...]:
    """The views of a page code compares a pair with: its best view not ruled out, then every
    other view of the page not ruled out that names the same room words, unit type and accessible
    marker (the plan, elevation and sections of one piece: "KITCHEN PLAN" and "KITCHEN MILLWORK
    ELEVATION", never the "ACC KITCHEN PLAN" beside them). A best view naming no room word is
    compared alone. Empty for a page with no titled views, or every view ruled out."""
    mine = [
        s
        for s in ranked
        if page_of.get(s.ref) == page_id and s.ref != page_id and s.contradicts is None
    ]
    if not mine:
        return ()
    best = signatures.get(mine[0].ref)
    if best is None or not best.rooms:
        return (mine[0].ref,)
    key = (best.rooms, best.unit, best.accessible)
    return tuple(
        s.ref
        for s in mine
        if (theirs := signatures.get(s.ref)) is not None
        and (theirs.rooms, theirs.unit, theirs.accessible) == key
    )


def _project(
    ranked: Sequence[pairing_module.Scored],
    page_of: Mapping[str, str],
    targets: Mapping[str, AcrossCandidate],
) -> tuple[pairing_module.Scored, ...]:
    """The pages' ranking from their drawings': each page scored as its best drawing not ruled
    out (ruled out only when every drawing of it is), its evidence said on that drawing. The
    agreement rule and the rules after it (an item tag's page, a room's page, the swap check)
    decide pages; the drawings are chosen with the page (`_view_group`)."""
    best: dict[str, pairing_module.Scored] = {}
    for scored in ranked:
        best.setdefault(page_of[scored.ref], scored)
    out: list[pairing_module.Scored] = []
    for page_id, scored in best.items():
        target = targets[scored.ref]
        drawn = target.drawings[0].titles if target.page_id != target.id else ()
        out.append(
            dataclasses.replace(
                scored,
                ref=page_id,
                evidence=(
                    (f'on its drawing "{drawn[0]}"', *scored.evidence) if drawn else scored.evidence
                ),
            )
        )
    order = {s.ref: n for n, s in enumerate(out)}
    return tuple(
        sorted(out, key=lambda s: (s.contradicts is None, s.score, -order[s.ref]), reverse=True)
    )


def _matcher_targets(
    ranked: Sequence[pairing_module.Scored], page_of: Mapping[str, str]
) -> tuple[str, ...]:
    """The drawings the AI page matcher is shown: code's best not ruled out, at most
    `MATCH_TARGETS`, at most `MATCH_PER_PAGE` of one page."""
    out: list[str] = []
    per_page: Counter[str] = Counter()
    for scored in ranked:
        if scored.contradicts is not None or per_page[page_of[scored.ref]] >= MATCH_PER_PAGE:
            continue
        out.append(scored.ref)
        per_page[page_of[scored.ref]] += 1
        if len(out) == MATCH_TARGETS:
            break
    return tuple(out)


def _ranked_by_view(
    vendor: Drawing,
    pool: Sequence[AcrossCandidate],
    facts_of: Callable[[Drawing], pairing_module.Facts],
    typed: frozenset[str],
    item_tags: frozenset[str],
    linked: Mapping[str, str],
    common: frozenset[str],
    boiler: frozenset[str],
    context: _Across,
) -> tuple[tuple[pairing_module.Scored, ...], str | None, _ByView]:
    """Code's ranking for a vendor drawing with each titled view of an architect sheet a target
    of its own (rebuild plan Step 3): the view's own facts (its words, tags and sizes, its overall
    width, the dense-sheet rule on its own sizes, its own accessible marker: `pairing.rank`),
    then its page signature against the vendor drawing's (`signature.rerank`: another unit type
    or accessible marker rules it out; room words, fixture order and widths re-rank). The pages'
    ranking is projected from their drawings' (`_project`); code's own pick is the clear winner
    among the pages (`pairing.clear_winner`), else the page of the drawing `pairing.decide` picks;
    each page's drawings to compare are chosen with it (`_view_group`)."""
    targets = _targets(pool)
    page_of = {t.id: str(t.page_id) for t in targets}
    by_id = {t.id: t for t in targets}
    pages = {item.id: item for item in pool}
    titles: dict[str, str] = {}
    for item in pool:
        title = _page_title(item, context)
        if title:
            titles.update({v.ref: title for v in item.views})
    counts = {v.ref: len(item.views) for item in pool for v in item.views}
    view_facts = _view_facts_of(facts_of, titles, counts)
    ranked, pick = _ranked_across(
        vendor, targets, view_facts, typed, item_tags, linked, common, boiler
    )
    vendor_sig = _signature(
        vendor,
        view_facts(vendor),
        context,
        [context.index_titles.get(vendor.ref) or "", *(vendor.titles or ()), vendor.heading or ""],
    )
    signatures: dict[str, signature_module.Signature] = {}
    for target in targets:
        page = pages[str(target.page_id)]
        own = list(target.drawings[0].titles or ()) if target.page_id != target.id else []
        sheet = context.sheet_ids.get((page.file, page.page))
        signatures[target.id] = _signature(
            target.drawings[0],
            _item_facts(target, view_facts, None)[0],
            context,
            own if signature_module.room_words(own) else [*own, _page_title(page, context) or ""],
            sheet.printed if sheet is not None else None,
        )
    ranked = signature_module.rerank(ranked, vendor_sig, signatures)
    projected = _project(ranked, page_of, by_id)
    winner = pairing_module.clear_winner(projected)
    code_pick = winner.ref if winner is not None else None
    if code_pick is None and pick is not None:
        # The printed reference or the same printed values (`pairing.decide`), drawing by drawing.
        page_id = page_of[pick]
        if any(s.ref == page_id and s.contradicts is None for s in projected):
            code_pick = page_id
    views = {
        item.id: group
        for item in pool
        if item.views and (group := _view_group(item.id, ranked, page_of, signatures))
    }
    return (
        projected,
        code_pick,
        _ByView(
            ranked=ranked,
            targets={
                t.id: t.model_copy(update={"signature": signature_words(signatures[t.id])})
                for t in targets
            },
            page_of=page_of,
            views=views,
            asked=_matcher_targets(ranked, page_of),
            signatures={**signatures, vendor.ref: vendor_sig},
        ),
    )


def _drawing_answer(
    saved: tuple[tuple[str, ...], pairing_module.MatchAnswer], by_view: _ByView
) -> tuple[
    pairing_module.MatchAnswer | None, dict[str, tuple[str, ...]], pairing_module.MatchAnswer
]:
    """A saved answer the AI page matcher gave on drawings (`page_match`, shown `_ByView.asked`):
    its pages, for the agreement rule, when it still holds (`pairing.reuse`: the same drawings
    shown, or fewer, its picks among them); the views it picked on each page; and its pages as
    it gave them (held against another page when nothing confirms it, `_held_page`)."""
    shown, answer = saved
    as_given = pairing_module.MatchAnswer(
        tuple(dict.fromkeys(by_view.page_of[m] for m in answer.matches if m in by_view.page_of)),
        answer.confident,
        answer.reason,
    )
    found = pairing_module.reuse(shown, answer, by_view.asked)
    if found is None:
        return None, {}, as_given
    views: dict[str, list[str]] = {}
    for ref in found.matches:
        page_id = by_view.page_of[ref]
        views.setdefault(page_id, [])
        if ref != page_id:
            views[page_id].append(ref)
    pages = pairing_module.MatchAnswer(tuple(views), found.confident, found.reason)
    return pages, {p: tuple(v) for p, v in views.items() if v}, as_given


def _scored_of(by_view: _ByView, ref: str) -> pairing_module.Scored:
    """A drawing target's code score (`_ByView.ranked`)."""
    return next(s for s in by_view.ranked if s.ref == ref)


def _by_view_fields(
    by_view: _ByView | None, answer_views: Mapping[str, tuple[str, ...]]
) -> dict[str, Any]:
    """The per-drawing fields of a pair (`AcrossPair.targets`, `AcrossPair.page_views`): the
    drawings the matcher is shown, scored, and each page's views to compare (the matcher's picks
    on it, else code's); nothing when pairing by whole pages."""
    if by_view is None:
        return {}
    scored = {s.ref: s for s in by_view.ranked}
    return {
        "targets": tuple(
            by_view.targets[ref].model_copy(
                update={
                    "score": scored[ref].score,
                    "evidence": scored[ref].evidence,
                    "contradicts": scored[ref].contradicts,
                    "strong": scored[ref].strong,
                }
            )
            for ref in by_view.asked
        ),
        "page_views": tuple(sorted({**by_view.views, **answer_views}.items())),
    }


def _hand_by_fixtures(
    vendor: str, views: Sequence[str], signatures: Mapping[str, signature_module.Signature]
) -> str | None:
    """Why a pair is compared opposite-hand by its fixtures (`signature.order_match`): the
    vendor's fixtures read only in reverse order on one of the paired views, else `None`."""
    mine = signatures.get(vendor)
    if mine is None:
        return None
    for ref in views:
        theirs = signatures.get(ref)
        if theirs is not None and signature_module.mirrored(mine, theirs):
            run = ", ".join(w.lower() for w in mine.order)
            return (
                f"the vendor's fixtures ({run}) read in reverse order on the architect's drawing: "
                "drawn opposite-hand"
            )
    return None


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
