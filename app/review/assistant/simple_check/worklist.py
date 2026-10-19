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
  own PDF, #1719) is numbered after the vendor's file (`set_pages`: its page 1 is set page 6 after a
  5-page vendor file), so its drawings and labels have ids of their own (`p6.a1`, `p6.D3`). Each
  `Drawing` keeps its file and its own page (`file_page`) for its pictures. Its architect drawings
  are the candidates a plain vendor drawing (one whole page, no GV area) is paired with
  (`_pair_across`).
- **Every page is said to be covered or skipped** (`WorkList.pages`); `uncovered` lists any page with
  a drawing that no item covers and no reason explains (a test holds it empty on a real set).
- **Hard items** (no labels on the vendor drawing, an architect drawing with no labels, a very dense
  drawing) are marked for the stronger model.

Read with the plan check's answers and code's assumptions laid over the page map
(`full_check_plan.read_page_map(assume=True)`), as the roster reads it.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from app.review.assistant.full_check_plan import DIVIDER_NOTE, read_page_map
from app.review.assistant.product_scope import named_products
from app.review.assistant.simple_check.schema import (
    Box,
    Drawing,
    Label,
    PageCover,
    Skipped,
    WorkItem,
    WorkList,
    label_ref,
)
from app.review.assistant.simple_check.set_pages import set_pages

__all__ = [
    "DENSE_LABELS",
    "MIN_DRAWING_PT",
    "build_worklist",
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
_MAX_NOTE_CHARS: Final = 80


# ---- reading the pack --------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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


def _assign_labels(
    halves: Sequence[_Half], labels: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Each label to the nearest drawing within reach whose role matches the label's side (as
    `dim_labels` gives the side). Returns the labels no drawing took."""
    left: list[Mapping[str, Any]] = []
    for label in labels:
        box = _box(label.get("box"))
        if box is None:
            continue
        centre = _centre(box)
        best: tuple[float, float, _Half] | None = None
        for half in halves:
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


def build_worklist(folder: Path, main_file: str | None = None) -> WorkList:
    """The work list of the set pack in `folder` (its page map with the plan check's answers and
    code's assumptions laid over it, its labels, scales and notes). Raises `FileNotFoundError` when
    the pack has no page map yet."""
    page_map = read_page_map(folder, assume=True, main_file=main_file)
    if page_map is None:
        raise FileNotFoundError(f"no page map in {folder}")
    return worklist_from(
        page_map,
        dims=_read(folder / "dims.json"),
        scales=_read(folder / "scales.json"),
        notes=_read(folder / "notes.json"),
        main_file=main_file,
    )


def worklist_from(
    page_map: Mapping[str, Any],
    *,
    dims: Any = None,
    scales: Any = None,
    notes: Any = None,
    main_file: str | None = None,
) -> WorkList:
    """The work list from the pack's documents (pure: `build_worklist` reads them)."""
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
    scales_pages = _by_page(scales)
    notes_pages = _by_page(notes)
    skipped: list[Skipped] = []
    cover: dict[int, PageCover] = {}

    def skip(page: int, reason: str, drawing: str | None = None) -> None:
        skipped.append(Skipped(file=file, page=page, drawing=drawing, reason=reason))

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
    halves_by_area: dict[str | None, list[_Half]] = {}
    vendor_halves: list[_Half] = []
    across: list[_Half] = []
    """Architect drawings a plain vendor drawing may be paired with (#1722)."""
    separate_entries = sorted(
        (e for e in entries if str(e.get("file")) != file and pages.joined(str(e.get("file")))),
        key=lambda e: pages.to_set(str(e.get("file")), int(e["page"])),
    )
    for entry in [*mine, *separate_entries]:
        entry_file = str(entry.get("file"))
        own = int(entry["page"])
        page = pages.to_set(entry_file, own)
        separate = entry_file != file
        if separate:
            # A separate file's page: only its architect drawings are read; its own skips are
            # said with its file and its own page.
            if entry.get("skip") or not any(
                isinstance(h, Mapping) and h.get("role") == "architect"
                for h in entry.get("halves") or []
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
        raw_halves = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
        if separate:
            raw_halves = [h for h in raw_halves if h.get("role") == "architect"]
        if not raw_halves:
            skip(page, DIVIDER_NOTE)
            cover[page] = PageCover(page=page, how="skipped", reason=DIVIDER_NOTE)
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
            )
            drawings[half.ref] = drawing
            if separate or (half.from_upload and half.role == "architect"):
                across.append(half)
            if separate:
                continue
            halves_by_area.setdefault(key, []).append(half)
            if half.role == "vendor":
                vendor_halves.append(half)

    items: list[WorkItem] = []
    used_architect: dict[int, list[str]] = {}
    used_reference: dict[int, list[str]] = {}
    for half in vendor_halves:
        entry = known[half.page]
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
        pairing: str | None = None
        if not architect and half.from_upload:
            # A plain vendor drawing (a separate file, no GV area): paired across the set (#1723).
            chosen, pairing = _pair_across(drawings[half.ref], [drawings[h.ref] for h in across])
            architect = tuple(chosen)
        reference = tuple(
            drawings[h.ref] for h in same_area if h.role == "brand_prototype" and h.ref
        )
        vendor = drawings[half.ref]
        if not architect:
            skip(
                half.page,
                pairing
                or (
                    "no architect drawing for this area in the set, so there is nothing to "
                    "compare it with (code)"
                ),
                half.ref,
            )
            continue
        # Rounds within the area: a drawing with no round in an area that has rounds is never
        # taken as the newest (verify trusts only the newest round).
        others = [
            h for h in same_area if key is not None and h.role == "vendor" and h.ref != half.ref
        ]
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
        owner = _area_owner(entry, known)
        text = " ".join(
            [
                _plain(owner.get("section")),
                _plain(owner.get("area")),
                *_phrases_in(dims_pages.get((half.file, half.page)), vendor.crop_box),
                *vendor.notes,
                *vendor.tags,
            ]
        )
        products = tuple(sorted(named_products(text, hints=True)))
        notes_page = notes_pages.get((half.file, half.page)) or {}
        only_tags = tuple(
            _plain(t.get("text") if isinstance(t, Mapping) else t)
            for t in notes_page.get("architect_only_tags") or []
        )
        hard: list[str] = []
        if not vendor.labels:
            hard.append("the vendor drawing has no numbered labels (code read none of its numbers)")
        if any(not d.labels for d in architect):
            hard.append("an architect drawing has no numbered labels (hand lettering)")
        if len(vendor.labels) > DENSE_LABELS:
            hard.append(f"a dense drawing: {len(vendor.labels)} vendor labels")
        items.append(
            WorkItem(
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
            )
        )
        for drawing in architect:
            used_architect.setdefault(drawing.page, []).append(half.ref)
        for drawing in reference:
            used_reference.setdefault(drawing.page, []).append(half.ref)

    # Architect and prototype drawings no item looks at: the vendor did not submit that area.
    looked = {d.ref for item in items for d in (*item.architect, *item.reference)}
    for ref, drawing in drawings.items():
        if drawing.role != "vendor" and ref not in looked:
            skipped.append(
                Skipped(
                    file=drawing.file,
                    page=drawing.file_page or drawing.page,
                    drawing=ref,
                    reason=(
                        f"the {drawing.role.replace('_', ' ')}'s drawing of an area with no vendor "
                        "drawing in this set: the vendor did not submit it (code)"
                        if drawing.file_page is None
                        else "the architect's drawing no vendor drawing was paired with (code)"
                    ),
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
        skipped=tuple(skipped),
        pages=tuple(cover[p] for p in sorted(cover)),
        uncovered=tuple(uncovered),
    )


def _pair_across(
    vendor: Drawing, candidates: Sequence[Drawing]
) -> tuple[list[Drawing], str | None]:
    """The architect drawing a plain vendor drawing is compared with, and how code chose it; or
    none and why. Separate files are paired automatically in #1723; until then none is chosen."""
    if not candidates:
        return [], None
    return [], (
        "a vendor drawing of a separate file: code does not pair it with the architect's file "
        "yet, so it is not compared (code)"
    )


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
