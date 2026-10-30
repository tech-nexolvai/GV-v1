"""What code adds to the page reviewer's first message beyond its own page (2026-10-31). Plain code
on printed text and GV's own picture folder: no model call, nothing decided.

Why: on a real three-file job (the vendor's cabinet sheets and GV's stone-top sheets joined into one
shop file, plus a design set whose items appear only as tags on enlarged furniture plans) the page
reviewers missed what needs more of the design (the room the item is in, its walls and free ends,
its outlets, a hole the design does not show), followed a wrong vendor item number to another
design item, and never compared a cabinet sheet with GV's stone-top sheet of the same item. Code
now hands each reviewer:

- `reference_pictures`: GV's rule pictures and examples of what GV's reviewer marks, from a folder
  GV keeps (`GV_ASSISTANT_PAGE_REVIEWER_PICTURES`), the same for every page of a check (so the
  prompt cache pays for them once);
- `view_sheet`: whether a design page that prints the item's tag is an elevation, section or detail
  (pictured around the tag beside the plan's room);
- `item_room_facts`: the page's title names a room the design gives to ANOTHER item while the
  page's own tag sits in a different room: the item number may be wrong;
- `other_file_facts`: the same item on the other file of a joined shop upload (the stone top of
  this cabinet run, or the reverse): its page, sizes, sink and appliance models, seams and title;
- `date_fact`: the sheet is dated before the design set's latest revision.

Every fact is a please-check for the reviewer to look at, never a verdict; a fact is given only
when code found both sides of it (never guessed).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from app.review.assistant.set_context import SetContext, family_of, item_key, tags_in

__all__ = [
    "MAX_FACT_LINES",
    "MAX_REFERENCE_BYTES",
    "MAX_REFERENCE_PICTURES",
    "ReferencePicture",
    "date_fact",
    "dates_in",
    "item_room_facts",
    "model_tokens",
    "other_file_facts",
    "reference_pictures",
    "room_labels",
    "same_item",
    "title_block",
    "view_sheet",
]

_log = logging.getLogger(__name__)

Box = Sequence[float]

#: Lines one facts block holds at most (each block is short: the page itself comes first).
MAX_FACT_LINES: Final = 10

# ---- GV's reference pictures ---------------------------------------------------------------------

#: Pictures at most, their PNG bytes in all at most (a request carries every picture as base64), and
#: each picture's longest side (pixels: many pictures in one request must each stay under 2,000).
MAX_REFERENCE_PICTURES: Final = 16
MAX_REFERENCE_BYTES: Final = 8_000_000
REFERENCE_EDGE_PX: Final = 1600
#: A caption is cut at this many characters.
_CAPTION_CHARS: Final = 400
GROUPS: Final = ("rule", "example")


@dataclass(frozen=True, slots=True)
class ReferencePicture:
    """One picture of GV's folder, as a PNG at most `REFERENCE_EDGE_PX` on a side."""

    png: bytes
    width: int
    height: int
    caption: str
    group: str
    """`rule` (one of GV's rule pictures) or `example` (GV's comment on another job's drawing)."""


def _as_png(path: Path) -> tuple[bytes, int, int]:
    """The picture as PNG bytes, made smaller when it is larger than `REFERENCE_EDGE_PX`."""
    from PIL import Image

    with Image.open(path) as opened:
        picture = opened.convert("RGBA" if opened.mode in ("RGBA", "LA", "P") else "RGB")
    edge = max(picture.size)
    if edge > REFERENCE_EDGE_PX:
        scale = REFERENCE_EDGE_PX / edge
        picture = picture.resize(
            (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
        )
    out = io.BytesIO()
    picture.save(out, format="PNG", optimize=True)
    return out.getvalue(), picture.width, picture.height


def _excluded(entry: Mapping[str, Any], names: Sequence[str]) -> bool:
    """The entry is kept off this job: one of its `exclude_projects` is part of an upload's name
    (any case): an example from this very job would show the reviewer its answer."""
    raw = entry.get("exclude_projects") or []
    wanted = (
        [str(p).strip().lower() for p in raw if str(p).strip()] if isinstance(raw, list) else []
    )
    known = [name.lower() for name in names]
    return any(project in name for project in wanted for name in known)


def reference_pictures(folder: Path | None, names: Sequence[str] = ()) -> list[ReferencePicture]:
    """GV's reference pictures from `folder` (its `manifest.json`: a list of `{"file", "caption",
    "group": "rule" | "example", "exclude_projects"?}`), in the manifest's order, at most
    `MAX_REFERENCE_PICTURES` and `MAX_REFERENCE_BYTES` in all. `names`: the job's upload names,
    for `exclude_projects`. A missing or broken folder, or a picture that cannot be read, is a
    logged warning and no picture: the review goes on without it."""
    if folder is None:
        return []
    root = Path(folder)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.warning("page reviewer: GV's reference pictures could not be read (no manifest)")
        return []
    if not isinstance(manifest, list):
        _log.warning("page reviewer: GV's reference pictures manifest is not a list")
        return []
    out: list[ReferencePicture] = []
    used = 0
    for entry in manifest:
        if len(out) >= MAX_REFERENCE_PICTURES:
            break
        if not isinstance(entry, Mapping):
            continue
        name, group = str(entry.get("file") or ""), str(entry.get("group") or "")
        caption = " ".join(str(entry.get("caption") or "").split())[:_CAPTION_CHARS]
        path = root / name
        # Only a picture inside the folder itself (never a path that leads out of it).
        if not name or Path(name).name != name or group not in GROUPS or not caption:
            _log.warning("page reviewer: reference picture entry %r skipped", name[:80])
            continue
        if _excluded(entry, names):
            continue
        try:
            png, width, height = _as_png(path)
        except Exception:  # noqa: BLE001 - that picture is left out; the others go on
            _log.warning("page reviewer: reference picture %r could not be read", name[:80])
            continue
        if used + len(png) > MAX_REFERENCE_BYTES:
            _log.warning("page reviewer: reference picture %r left out (size cap)", name[:80])
            continue
        used += len(png)
        out.append(ReferencePicture(png, width, height, caption, group))
    return out


# ---- a page's title block ------------------------------------------------------------------------

#: A sheet's title block: a strip along its right side or its bottom (shares of the page).
TITLE_RIGHT: Final = 0.75
TITLE_BOTTOM: Final = 0.8


def _box(phrase: Mapping[str, Any]) -> list[float] | None:
    raw = phrase.get("box")
    if isinstance(raw, Mapping):
        try:
            return [float(raw[k]) for k in ("x0", "y0", "x1", "y1")]
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return [float(v) for v in raw]
    return None


def title_block(
    found: Sequence[Mapping[str, Any]], size: tuple[float, float] | None = None
) -> list[Mapping[str, Any]]:
    """The phrases in the sheet's title block (its right or bottom strip, `TITLE_RIGHT`,
    `TITLE_BOTTOM`). `size`: the page's (width, height) in points; without it the printed text's
    own extent stands in."""
    boxes = [(p, b) for p in found if (b := _box(p)) is not None]
    if not boxes:
        return []
    width, height = size or (max(b[2] for _p, b in boxes), max(b[3] for _p, b in boxes))
    return [p for p, b in boxes if b[0] >= TITLE_RIGHT * width or b[1] >= TITLE_BOTTOM * height]


# ---- which design sheet: plan, or elevation / section / detail -----------------------------------

_VIEW_WORDS: Final = re.compile(r"\b(?:ELEVATIONS?|SECTIONS?|DETAILS?)\b", re.IGNORECASE)
_PLAN_WORD: Final = re.compile(r"\bPLANS?\b", re.IGNORECASE)
#: Words this close to a tag (points, edge to edge) are the tag's own view's (its view title).
VIEW_NEAR_PT: Final = 250.0


def _gap(a: Box, b: Box) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def view_sheet(found: Sequence[Mapping[str, Any]], box: Box) -> bool:
    """Whether the tag at `box` sits in an elevation, section or detail rather than a plan: the
    words near it name one (and none names a plan), or the sheet's title does (and not a plan)."""
    near = [
        str(p.get("text") or "")
        for p in found
        if (b := _box(p)) is not None and _gap(b, box) <= VIEW_NEAR_PT
    ]
    if any(_VIEW_WORDS.search(w) for w in near) and not any(_PLAN_WORD.search(w) for w in near):
        return True
    title = [str(p.get("text") or "") for p in title_block(found)]
    return any(_VIEW_WORDS.search(w) for w in title) and not any(
        _PLAN_WORD.search(w) for w in title
    )


# ---- the item number against the room ------------------------------------------------------------

#: A room label this close to a tag (points, edge to edge) is the room the tag is in.
ROOM_NEAR_PT: Final = 250.0
#: Words that are never a room's name (parts, notes, trades and view words), on the design's
#: labels and in the sheet's description alike.
# fmt: off
_NOT_ROOM: Final = frozenset({
    "PLAN", "PLANS", "ENLARGED", "ELEVATION", "ELEVATIONS", "SECTION", "SECTIONS", "DETAIL",
    "DETAILS", "SCALE", "NOTE", "NOTES", "SEE", "TYP", "TYPICAL", "ABOVE", "BELOW", "WALL",
    "WALLS", "LINE", "FLOOR", "CEILING", "OUTLET", "OUTLETS", "GFI", "GFCI", "DATA", "POWER",
    "SINK", "SINKS", "COUNTER", "COUNTERTOP", "COUNTERTOPS", "TOP", "TOPS", "CABINET", "CABINETS",
    "CASEWORK", "MILLWORK", "BASE", "UPPER", "TALL", "DRAWER", "DRAWERS", "DOOR", "DOORS", "SHELF",
    "SHELVES", "PANEL", "PANELS", "FILLER", "FILLERS", "QUARTZ", "STONE", "SOLID", "SURFACE",
    "LAMINATE", "WOOD", "VENEER", "FINISH", "FINISHES", "MATCH", "EQUAL", "CLEAR", "ALIGN",
    "CENTER", "CENTERED", "OPEN", "OPENING", "OTHERS", "OWNER", "CONTRACTOR", "EXISTING", "FIELD",
    "VERIFY", "REFRIGERATOR", "DISHWASHER", "MICROWAVE", "APPLIANCE", "APPLIANCES", "EQUIPMENT",
    "ITEM", "ITEMS", "DESCRIPTION", "PROJECT", "SHEET", "DRAWING", "DRAWINGS", "TITLE", "DATE",
    "REVISION", "REVISIONS", "ISSUE", "ISSUED", "SHOP", "SUBMITTAL", "APPROVED", "APPROVAL",
    "CHECKED", "DRAWN", "JOB", "SIZE", "SHORT", "LONG", "LEFT", "RIGHT", "FRONT", "BACK", "SIDE",
    "END", "ENDS", "SPLASH", "BACKSPLASH", "SUPPORT", "BRACKET", "LEGS", "LEG", "WITH", "FROM",
    "THIS", "THAT", "INTO", "OVER", "UNDER",
})
# fmt: on
_ROOM_WORD: Final = re.compile(r"[A-Z][A-Z'&/-]*")


def _room_words(text: str) -> list[str]:
    """The words of `text` that could name a room: letters only, 4 or more, not `_NOT_ROOM`."""
    return [w for w in re.findall(r"[A-Z]+", text.upper()) if len(w) >= 4 and w not in _NOT_ROOM]


def room_labels(found: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[float]]]:
    """The room labels a design plan prints: short phrases in capitals (1 to 4 words, a room
    number after them allowed), with a word that could name a room, never a tag or a size."""
    out: list[tuple[str, list[float]]] = []
    for phrase in found:
        text = " ".join(str(phrase.get("text") or "").split())
        box = _box(phrase)
        if box is None or not text or text != text.upper() or tags_in(text):
            continue
        words = text.split()
        while words and words[-1].isdigit():  # a room number printed after its name
            words.pop()
        if not 1 <= len(words) <= 4 or not all(_ROOM_WORD.fullmatch(w) for w in words):
            continue
        if _room_words(" ".join(words)):
            out.append((" ".join(words), box))
    return out


def _nearest(
    box: Box, rooms: Sequence[tuple[str, list[float]]], limit: float
) -> list[tuple[str, list[float]]]:
    near = [(room, b) for room, b in rooms if _gap(b, box) <= limit]
    return sorted(near, key=lambda found: _gap(found[1], box))


def item_room_facts(
    title: Sequence[str],
    spots: Sequence[tuple[str, int, Box]],
    design_pages: Mapping[int, Sequence[Mapping[str, Any]]],
    item_tags: Collection[str],
) -> list[str]:
    """The sheet's description names a room (`title`: its title-block phrases) that the design's
    plans give to ANOTHER item of the same family, while this item's own tag (`spots`: the page's
    tag, the design plan page and the tag's box there) sits in a different room: the item number
    may be wrong (a vendor's wrong number led the reviewers to another design item, 2026-10-31).
    `item_tags`: the design's item tags (printed on few pages). Only when both sides are found."""
    rooms = {page: room_labels(found) for page, found in design_pages.items()}
    lines: list[str] = []
    for tag, page, box in spots:
        here = _nearest(box, rooms.get(page, []), ROOM_NEAR_PT)
        if not here:
            continue
        here_words = {w for room, _b in here for w in _room_words(room)}
        family, key = family_of(tag), item_key(tag)
        for phrase in title:
            for word in dict.fromkeys(_room_words(phrase)):
                if word in here_words:
                    continue  # the sheet names the room its tag is in: as it should be
                other = _holder(word, rooms, design_pages, item_tags, family, key)
                if other is None:
                    continue
                room, other_tag, other_page = other
                line = (
                    f"This sheet is {tag} '{' '.join(phrase.split())[:80]}'. On the design plan "
                    f"p{page}, {tag} is in {here[0][0]}; the design's {room} holds {other_tag} "
                    f"(p{other_page}). The item number may be wrong: please check."
                )
                if line not in lines:
                    lines.append(line)
    return lines[:MAX_FACT_LINES]


def _holder(
    word: str,
    rooms: Mapping[int, Sequence[tuple[str, list[float]]]],
    design_pages: Mapping[int, Sequence[Mapping[str, Any]]],
    item_tags: Collection[str],
    family: str | None,
    key: str | None,
) -> tuple[str, str, int] | None:
    """The design room named `word` and the item of `family` (another item than `key`) printed
    nearest it: `(room, tag, page)`, or `None`."""
    if family is None:
        return None
    best: tuple[float, str, str, int] | None = None
    for page, labels in sorted(rooms.items()):
        for room, room_box in labels:
            if word not in _room_words(room):
                continue
            for phrase in design_pages.get(page, []):
                b = _box(phrase)
                if b is None or _gap(b, room_box) > ROOM_NEAR_PT:
                    continue
                for found in tags_in(str(phrase.get("text") or "")):
                    if found in item_tags and family_of(found) == family and item_key(found) != key:
                        gap = _gap(b, room_box)
                        if best is None or gap < best[0]:
                            best = (gap, room, found, page)
    return None if best is None else (best[1], best[2], best[3])


# ---- the same item on the other file of a joined shop upload -------------------------------------

#: An item's tag printed on more pages than this is a sheet reference or a code, not an item.
MAX_ITEM_PAGES: Final = 6
#: Other pages listed per sheet, and sizes per page (the largest), at most.
MAX_OTHER_PAGES: Final = 3
MAX_OTHER_SIZES: Final = 6
#: A sink or appliance model as printed (HSS-UMS-7, K-3821): parts joined by hyphens, letters and
#: digits both, never an item or finish tag.
_MODEL: Final = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]+(?:-[A-Z0-9]+){1,4}\b")
_INCHES: Final = re.compile(r"(?:\d+'-?\s?)?\d+(?:[ -]\d+/\d+)?\s?\"")
_SEAM: Final = re.compile(r"\b(?:joint|seam)s?\b", re.IGNORECASE)
#: A tag's family, number, letter and suffix, as `set_context` reads them.
_TAG_PARTS: Final = re.compile(r"^([A-Z]{1,5})-?(\d{2,3})([A-Z]?)(\.[A-Z]{1,3})?$")


def _strip_t(text: str) -> str:
    return text.removesuffix("T")


def same_item(a: str, b: str) -> bool:
    """`a` and `b` name one item, one of them maybe with the top's T (`BOH-801` and `BOH-801T`,
    `ML-018.C` and `ML-018T.C`, `BOH-802.C` and `BOH-802.CT`); never `801A` and `801B`."""
    pa, pb = _TAG_PARTS.match(a), _TAG_PARTS.match(b)
    if pa is None or pb is None:
        return False
    if a == b:
        return True
    if pa.group(1) != pb.group(1) or int(pa.group(2)) != int(pb.group(2)):
        return False
    if _strip_t(pa.group(3) or "") != _strip_t(pb.group(3) or ""):
        return False
    sa, sb = _strip_t(pa.group(4) or ""), _strip_t(pb.group(4) or "")
    return sa == sb or not sa or not sb


def model_tokens(found: Sequence[Mapping[str, Any]]) -> list[str]:
    """The sink and appliance models a page prints (`_MODEL`), each once, item and finish tags left
    out."""
    out: dict[str, None] = {}
    for phrase in found:
        for token in _MODEL.findall(str(phrase.get("text") or "")):
            if not any(t == token for t in tags_in(token)) and not token[0].isdigit():
                out.setdefault(token, None)
    return list(out)[:8]


def _largest_sizes(found: Sequence[Mapping[str, Any]]) -> list[str]:
    from app.review.assistant.finding_checks import to_inches

    sizes: dict[str, Decimal] = {}
    for phrase in found:
        for printed in _INCHES.finditer(str(phrase.get("text") or "")):
            text = printed.group(0).strip()
            value = to_inches(text)
            if value is not None and value > 0:
                sizes.setdefault(text, value)
    ranked = sorted(sizes.items(), key=lambda item: -item[1])
    return [text for text, _v in ranked[:MAX_OTHER_SIZES]]


def _source_of(page: int, parts: Sequence[Mapping[str, Any]]) -> str | None:
    for part in parts:
        first, count = int(part.get("first_page") or 0), int(part.get("pages") or 0)
        if first <= page < first + count:
            return str(part.get("name") or "")
    return None


def other_file_facts(
    context: SetContext,
    page: int,
    parts: Sequence[Mapping[str, Any]],
    size_of: Callable[[int], tuple[float, float] | None] = lambda _page: None,
) -> list[str]:
    """For a page of a joined shop upload (`parts`: the upload's `joined_from`, each with its
    `first_page` and `pages`), the pages of the OTHER files that print the same item (`same_item`):
    each one's page, title words, largest printed sizes, sink and appliance models and seams. Facts
    for the reviewer to compare (the top's length with the cabinet run, sinks, seams, the item
    number), never a verdict. `size_of`: a page's size, for its title block."""
    mine_source = _source_of(page, parts)
    if mine_source is None or len(parts) < 2:
        return []
    finish = context.finish_families()
    mine = [
        t
        for t in context.tags_on(page)
        if family_of(t) not in finish and len(context.tags.get(t, [])) <= MAX_ITEM_PAGES
    ]
    lines: list[str] = []
    seen: set[int] = set()
    for other in sorted(context.pages):
        if len(seen) >= MAX_OTHER_PAGES:
            break
        source = _source_of(other, parts)
        if other == page or source in (None, mine_source):
            continue
        theirs = context.tags_on(other)
        match = next(((m, t) for m in mine for t in theirs if same_item(m, t)), None)
        if match is None:
            continue
        seen.add(other)
        found = context.pages.get(other, [])
        title = "; ".join(
            " ".join(str(p.get("text") or "").split())[:60]
            for p in title_block(found, size_of(other))
            if re.search(r"[A-Za-z]{3}", str(p.get("text") or ""))
        )[:240]
        sizes = ", ".join(_largest_sizes(found)) or "none"
        models = ", ".join(model_tokens(found)) or "none"
        joints = sum(1 for p in found if _SEAM.search(str(p.get("text") or "")))
        lines.append(
            f"p{other} (from {source}) prints {match[1]} (this page: {match[0]}). "
            f"Its title block: '{title or 'not read'}'. Largest sizes: {sizes}. "
            f"Sink or appliance models: {models}. Seams (JOINT) marked: {joints}."
        )
    return lines[:MAX_FACT_LINES]


# ---- the sheet's date against the design's latest revision ---------------------------------------

#: Month/day/year (US drawings; a first number over 12 is a day), or year/month/day.
_MDY: Final = re.compile(r"(?<![\d/.-])(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/.-])")
_YMD: Final = re.compile(r"(?<![\d/.-])(\d{4})([/.-])(\d{1,2})\2(\d{1,2})(?![\d/.-])")


def dates_in(text: str) -> list[tuple[dt.date, str]]:
    """The dates `text` prints, each with its printed form; a two-digit year only with slashes,
    years 2000 to 2099 only, and never an impossible date."""
    out: list[tuple[dt.date, str]] = []
    for found in _YMD.finditer(text):
        year, month, day = int(found.group(1)), int(found.group(3)), int(found.group(4))
        _add_date(out, year, month, day, found.group(0))
    for found in _MDY.finditer(text):
        first, second, raw_year = int(found.group(1)), int(found.group(3)), found.group(4)
        if len(raw_year) == 2 and found.group(2) != "/":
            continue
        year = int(raw_year) + (2000 if len(raw_year) == 2 else 0)
        month, day = (second, first) if first > 12 else (first, second)
        _add_date(out, year, month, day, found.group(0))
    return out


def _add_date(out: list[tuple[dt.date, str]], year: int, month: int, day: int, text: str) -> None:
    if not 2000 <= year <= 2099:
        return
    try:
        out.append((dt.date(year, month, day), text))
    except ValueError:
        return


def _latest(texts: Iterable[str]) -> tuple[dt.date, str] | None:
    found = [d for text in texts for d in dates_in(text)]
    return max(found, key=lambda d: d[0]) if found else None


def date_fact(sheet_title: Sequence[str], design_titles: Iterable[str]) -> str:
    """When the sheet's title block date (its latest) is before the design set's latest date
    (printed in its title blocks and revision tables): a line asking the reviewer to check the
    sheet follows that revision. Empty when either date is not found, or the sheet is not older."""
    mine, theirs = _latest(sheet_title), _latest(design_titles)
    if mine is None or theirs is None or mine[0] >= theirs[0]:
        return ""
    return (
        f"This sheet is dated {mine[1]}; the design set's latest revision is dated {theirs[1]}: "
        "check it follows that revision."
    )
