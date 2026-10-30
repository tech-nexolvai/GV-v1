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
- `title_block`: the sheet's description strip first (`sheet_fields`: DESCRIPTION, ITEM NO.,
  OVERALL SIZE as labelled), then its right or bottom strip;
- `room_facts`: the sheet's description names a room the design does not put the sheet's tag in:
  the design's item of another family with the same number is in that room (a vendor's tag took
  another item's family), or the design puts the tag elsewhere. The design's room names may be
  in a symbol font (read as the letters they stand for);
- `other_file_facts`: the same item on the other file of a joined shop upload (the stone top of
  this cabinet run, or the reverse), also under the same number in another family when both
  sheets name one room: its page, sizes (code's reading of the drawn numbers when the text has
  none), sink and appliance models, seams and title;
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

from app.review.assistant.set_context import SetContext, family_of, tags_in
from app.review.assistant.symbol_font import decoded

__all__ = [
    "MAX_FACT_LINES",
    "MAX_REFERENCE_BYTES",
    "MAX_REFERENCE_PICTURES",
    "ReferencePicture",
    "SheetField",
    "date_fact",
    "dates_in",
    "model_tokens",
    "other_file_facts",
    "reference_pictures",
    "room_facts",
    "room_labels",
    "same_item",
    "sheet_description",
    "sheet_fields",
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
#: A field's value is printed under its label at most this far (points, label's bottom to the
#: value's top), or after it on its line at most this far (points, edge to edge).
FIELD_BELOW_PT: Final = 60.0
FIELD_AFTER_PT: Final = 200.0
#: The labels of a sheet's description strip that say what the item is and where it goes
#: (`DESCRIPTION`, `ITEM NO.`, `OVERALL SIZE`, `ROOM NAME`): two of them on one strip mark it
#: (2026-10-31: a vendor's strip, on every sheet, under its material table, which the right and
#: bottom strips picked instead). A one-word label that is also a plain word counts only with
#: its colon (`ROOM:`, `LOCATION:`).
_ITEM_LABEL: Final = re.compile(
    r"^(DESCRIPTION|ITEM\s*(?:NO\.?|NUMBER|#)|OVERALL\s+SIZES?|ROOM\s*(?:NAME|NO\.?|#)|"
    r"(?:ROOM|LOCATION|AREA)(?=\s*[:：]))\s*[:：]?\s*",
    re.IGNORECASE,
)
#: The strip's other labels (the project, who drew it, its date, version and page): part of the
#: strip when on its line, never enough alone (a revision table prints `DATE` and `DESCRIPTION`).
_OTHER_LABEL: Final = re.compile(
    r"^(PROJECT\s*(?:NAME|NO\.?|#)?|JOB\s*(?:NAME|NO\.?|#)?|DRAWN\s+BY|CHECKED\s+BY|"
    r"APPROVED\s+BY|REV(?:ISION)?\.?\s*DATE|DATE|VERSION|REV(?:ISION)?\.?|PAGE|SHEET\s*(?:NO\.?|#)?|"
    r"SCALE|QTY|QUANTITY)(?=\s*[:：]|\s*$)\s*[:：]?\s*",
    re.IGNORECASE,
)
#: The fields that say what the item is and where it goes (a room's name among their words).
_DESCRIBING: Final = ("DESCRIPTION", "ROOM", "LOCATION", "AREA")


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


def _text(phrase: Mapping[str, Any]) -> str:
    """A phrase's text on one line, symbol-font characters as the letters they stand for (a design
    set printed its room names in a font that stores `PANTRY` as private-use characters)."""
    return " ".join(decoded(str(phrase.get("text") or "")).split())


def _heuristic_block(
    found: Sequence[Mapping[str, Any]], size: tuple[float, float] | None
) -> list[Mapping[str, Any]]:
    boxes = [(p, b) for p in found if (b := _box(p)) is not None]
    if not boxes:
        return []
    width, height = size or (max(b[2] for _p, b in boxes), max(b[3] for _p, b in boxes))
    return [p for p, b in boxes if b[0] >= TITLE_RIGHT * width or b[1] >= TITLE_BOTTOM * height]


@dataclass(frozen=True, slots=True)
class SheetField:
    """One labelled field of a sheet's strip: its label as printed, its value (the phrases under
    the label, or after it on its line) and the phrases both are printed in."""

    label: str
    value: str
    box: tuple[float, float, float, float]
    phrases: tuple[int, ...]
    """The indexes of the label's and the value's phrases in the page's phrases."""

    @property
    def describing(self) -> bool:
        """The field says what the item is or where it goes (`DESCRIPTION`, `ROOM NAME`)."""
        return self.label.upper().startswith(_DESCRIBING)

    @property
    def item(self) -> bool:
        return self.label.upper().startswith("ITEM")


def _same_line(a: Box, b: Box) -> bool:
    middle = (a[1] + a[3]) / 2
    return b[1] <= middle <= b[3]


def _overlap(a: Box, b: Box) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > 0


def sheet_fields(found: Sequence[Mapping[str, Any]]) -> list[SheetField]:
    """The sheet's description strip, field by field: two of its item labels (`_ITEM_LABEL`) on one
    line, and the strip's other labels on that line (`_OTHER_LABEL`). A field's value is what the
    label's phrase prints after it, else the phrases after it on its line (up to the next label,
    `FIELD_AFTER_PT`), else the phrases under it (`FIELD_BELOW_PT`) that sit under no other label
    of the strip. Empty when the sheet has no such strip (never guessed)."""
    labels: list[tuple[int, str, str, Box, bool]] = []
    for index, phrase in enumerate(found):
        text, box = _text(phrase), _box(phrase)
        if box is None or not text:
            continue
        for pattern, main in ((_ITEM_LABEL, True), (_OTHER_LABEL, False)):
            hit = pattern.match(text)
            if hit is not None:
                label = " ".join(hit.group(1).upper().split())
                labels.append((index, label, text[hit.end() :].strip(), box, main))
                break
    strip: list[tuple[int, str, str, Box, bool]] = []
    for first in labels:
        row = [one for one in labels if _same_line(first[3], one[3])]
        if len({one[1] for one in row if one[4]}) >= 2 and len(row) > len(strip):
            strip = row
    if not strip:
        return []
    taken = {one[0] for one in labels}
    out: list[SheetField] = []
    for index, label, inline, at, _main in sorted(strip, key=lambda one: one[3][0]):
        parts: list[tuple[int, Box]] = []
        if not inline:
            parts = _after(found, at, strip, taken) or _under(found, at, strip, taken)
        used = (index, *(i for i, _b in parts))
        boxes = [at, *(b for _i, b in parts)]
        value = " ".join([inline, *(_text(found[i]) for i, _b in parts)]).strip()
        whole = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        out.append(SheetField(label, value, whole, used))
    return out


def _after(
    found: Sequence[Mapping[str, Any]],
    box: Box,
    strip: Sequence[tuple[int, str, str, Box, bool]],
    taken: Collection[int],
) -> list[tuple[int, Box]]:
    """The phrases after the label on its line, up to the strip's next label."""
    ends = [b[0] for _i, _l, _t, b, _m in strip if b[0] > box[2] and _same_line(box, b)]
    stop = min([box[2] + FIELD_AFTER_PT, *ends])
    out: list[tuple[int, Box]] = []
    for index, phrase in enumerate(found):
        b = _box(phrase)
        if index in taken or b is None or not _same_line(box, b):
            continue
        if box[2] <= b[0] and b[2] <= stop:
            out.append((index, b))
    return sorted(out, key=lambda one: one[1][0])


def _under(
    found: Sequence[Mapping[str, Any]],
    box: Box,
    strip: Sequence[tuple[int, str, str, Box, bool]],
    taken: Collection[int],
) -> list[tuple[int, Box]]:
    """The phrases under the label (`FIELD_BELOW_PT`) that sit under no other label of the strip."""
    others = [b for _i, _l, _t, b, _m in strip if list(b) != list(box)]
    out: list[tuple[int, Box]] = []
    for index, phrase in enumerate(found):
        b = _box(phrase)
        if index in taken or b is None or not _overlap(b, box):
            continue
        if not box[3] - 1.0 <= b[1] <= box[3] + FIELD_BELOW_PT:
            continue
        if any(_overlap(b, other) for other in others):
            continue  # a note across the strip, or another field's value
        out.append((index, b))
    return sorted(out, key=lambda one: (one[1][1], one[1][0]))


def title_block(
    found: Sequence[Mapping[str, Any]], size: tuple[float, float] | None = None
) -> list[Mapping[str, Any]]:
    """The phrases in the sheet's title block: its description strip first, each field as one
    phrase `LABEL: value` (`sheet_fields`), then the phrases of its right or bottom strip
    (`TITLE_RIGHT`, `TITLE_BOTTOM`) the strip did not take. `size`: the page's (width, height) in
    points; without it the printed text's own extent stands in."""
    fields = sheet_fields(found)
    used = {index for field in fields for index in field.phrases}
    first: list[Mapping[str, Any]] = [
        {"text": f"{field.label}: {field.value}" if field.value else field.label, "box": field.box}
        for field in fields
    ]
    taken = {id(found[index]) for index in used}
    return first + [p for p in _heuristic_block(found, size) if id(p) not in taken]


#: A header over a sheet's own item (`DRAWING # 3`, then the item's tag, then its name: a stone
#: shop's sheets): its lines share a left edge and follow each other at most a line apart.
_HEADER_LABEL: Final = re.compile(
    r"^(?:DRAWING|DWG|SHEET|ITEM)\s*(?:NO\.?|#|NUMBER)", re.IGNORECASE
)
HEADER_EDGE_PT: Final = 4.0
HEADER_LINES: Final = 4


def sheet_description(
    found: Sequence[Mapping[str, Any]],
) -> tuple[list[str], str]:
    """The sheet's own item tags and the words that say what the item is and where it goes: the
    description strip's item and describing fields (`sheet_fields`), else a header that prints
    the item's tag under a drawing number and its name under the tag. `([], "")` when the sheet
    has neither (never guessed)."""
    fields = sheet_fields(found)
    tags = [t for field in fields if field.item for t in tags_in(field.value)]
    words = "; ".join(field.value for field in fields if field.describing and field.value)
    if tags or words:
        return list(dict.fromkeys(tags)), words
    for phrase in found:
        box = _box(phrase)
        if box is None or not _HEADER_LABEL.match(_text(phrase)):
            continue
        lines = _stack(found, box)
        tag = next((t for t, _b in lines if tags_in(t) == [t]), None)
        if tag is None:
            continue
        after = [t for t, _b in lines[[t for t, _b in lines].index(tag) + 1 :]]
        name = "; ".join(t for t in after if re.search(r"[A-Za-z]{3}", t) and not tags_in(t))
        return [tag], name
    return [], ""


def _stack(found: Sequence[Mapping[str, Any]], box: Box) -> list[tuple[str, Box]]:
    """The lines under `box` sharing its left edge, each at most a line under the one before."""
    lines: list[tuple[str, Box]] = []
    last = box
    for _ in range(HEADER_LINES):
        height = last[3] - last[1]
        below = [
            (_text(p), b)
            for p in found
            if (b := _box(p)) is not None
            and abs(b[0] - box[0]) <= HEADER_EDGE_PT
            and last[3] - 1.0 <= b[1] <= last[3] + height
        ]
        if not below:
            break
        last = min(below, key=lambda one: one[1][1])[1]
        lines.append(next(one for one in below if one[1] is last))
    return lines


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

#: A room label this close to a tag (points, edge to edge) is the room the tag is in, when it is
#: clearly the nearest: no label of another room within `ROOM_CLEAR` times its distance (plus
#: `ROOM_SLACK_PT`); two rooms about as near are no room (never a guess). A plan prints no walls
#: code reads, so the nearest label may be the next room's (a meeting-room item's tag beside the
#: corridor's label, 2026-10-31): a tag is said to be IN a room only when its label is within
#: `ROOM_IN_PT` (else "nearest the room name"), and the room a sheet names is said to be
#: elsewhere only when the same plan prints it, and no further than `ROOM_FAR_PT` from the tag.
ROOM_NEAR_PT: Final = 250.0
ROOM_IN_PT: Final = 100.0
ROOM_FAR_PT: Final = 500.0
ROOM_CLEAR: Final = 1.5
ROOM_SLACK_PT: Final = 12.0
#: Items listed in the room the sheet names, at most.
MAX_ROOM_ITEMS: Final = 3
#: A room's name printed on two lines (`STAFF` over `LOUNGE`): the second line's top at most this
#: many line heights under the first's bottom, their middles at most half the wider apart.
ROOM_LINE_GAP: Final = 0.8
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
    "THIS", "THAT", "INTO", "OVER", "UNDER", "FURNITURE", "LEVEL", "NUMBER", "ELEV",
})
#: Words of a room's name that say only that it is a room (`WORK AREA`, `BREAKFAST SERVING AREA`):
#: never enough to call two rooms one.
_ANY_ROOM: Final = frozenset({"AREA", "AREAS", "ROOM", "ROOMS", "SPACE", "ZONE", "STATION"})
# fmt: on
_ROOM_WORD: Final = re.compile(r"[A-Z][A-Z'&/-]*")


def _room_words(text: str) -> list[str]:
    """The words of `text` that could name a room: letters only, 4 or more, not `_NOT_ROOM`."""
    return [w for w in re.findall(r"[A-Z]+", text.upper()) if len(w) >= 4 and w not in _NOT_ROOM]


def _naming(text: str) -> set[str]:
    """The words of `text` that tell one room from another (`_room_words`, `_ANY_ROOM` left out)."""
    return {w for w in _room_words(text) if w not in _ANY_ROOM}


def room_labels(found: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[float]]]:
    """The room labels a design plan prints: short phrases in capitals (1 to 4 words, a room
    number after them allowed; a name printed on two lines read as one), with a word that could
    name a room, never a tag, a size or a word written sideways (a title block's turned text).
    Symbol-font letters are read as the letters they stand for."""
    lines: list[tuple[list[str], list[float]]] = []
    for phrase in found:
        text, box = _text(phrase), _box(phrase)
        if box is None or not text or text != text.upper() or tags_in(text):
            continue
        if box[3] - box[1] > box[2] - box[0]:
            continue  # written sideways
        words = text.split()
        while words and words[-1].isdigit():  # a room number printed after its name
            words.pop()
        if 1 <= len(words) <= 4 and all(_ROOM_WORD.fullmatch(w) for w in words):
            lines.append((words, box))
    lines.sort(key=lambda line: (line[1][1], line[1][0]))
    out: list[tuple[str, list[float]]] = []
    used: set[int] = set()
    for index, (words, box) in enumerate(lines):
        if index in used:
            continue
        words, box = list(words), list(box)
        for other, (more, below) in enumerate(lines):
            if other in used or other == index or len(words) + len(more) > 4:
                continue
            height = box[3] - box[1]
            wide = max(box[2] - box[0], below[2] - below[0])
            middle = abs((box[0] + box[2]) / 2 - (below[0] + below[2]) / 2)
            if -1.0 <= below[1] - box[3] <= ROOM_LINE_GAP * height and middle <= wide / 2:
                used.add(other)
                words += more
                box = [min(box[0], below[0]), box[1], max(box[2], below[2]), below[3]]
        if _room_words(" ".join(words)):
            out.append((" ".join(words), box))
    return out


def _nearest(
    box: Box, rooms: Sequence[tuple[str, list[float]]], limit: float
) -> list[tuple[str, list[float]]]:
    near = [(room, b) for room, b in rooms if _gap(b, box) <= limit]
    return sorted(near, key=lambda found: _gap(found[1], box))


def _room_at(box: Box, rooms: Sequence[tuple[str, list[float]]]) -> tuple[str, float] | None:
    """The room the tag at `box` is in and its label's distance: the nearest label
    (`ROOM_NEAR_PT`) when no label of another room is about as near (`ROOM_CLEAR`,
    `ROOM_SLACK_PT`); else `None`."""
    near = _nearest(box, rooms, ROOM_NEAR_PT)
    if not near:
        return None
    room, first = near[0]
    gap = _gap(first, box)
    bound = gap * ROOM_CLEAR + ROOM_SLACK_PT
    if any(_gap(b, box) <= bound and _naming(other) != _naming(room) for other, b in near[1:]):
        return None
    return room, gap


@dataclass(frozen=True, slots=True)
class _Spot:
    """Where the design prints an item tag on a plan, and the room label nearest it."""

    tag: str
    page: int
    box: Box
    room: str
    gap: float

    @property
    def inside(self) -> bool:
        """The room's label is right by the tag (`ROOM_IN_PT`): the tag is in that room."""
        return self.gap <= ROOM_IN_PT

    def where(self) -> str:
        if self.inside:
            return f"in {self.room} (design p{self.page})"
        return f"on design p{self.page}, nearest the room name {self.room}"


def _plain_pages(
    pages: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[int, list[dict[str, Any]]]:
    return {page: [dict(p, text=_text(p)) for p in found] for page, found in pages.items()}


def _design_spots(
    pages: Mapping[int, Sequence[Mapping[str, Any]]],
    rooms: Mapping[int, Sequence[tuple[str, list[float]]]],
    most: int,
) -> list[_Spot]:
    """Every item tag the design prints on a plan (at most `most` pages in all: a finish code is
    printed on most), each with the room it is in (`_room_at`); a tag on an elevation, section or
    detail, or in no clear room, is left out."""
    printed: dict[str, list[tuple[int, Box]]] = {}
    for page, found in sorted(pages.items()):
        for phrase in found:
            box = _box(phrase)
            if box is None:
                continue
            for tag in tags_in(str(phrase.get("text") or "")):
                if len(family_of(tag) or "") >= 2:  # one letter spells a cabinet code (W70)
                    printed.setdefault(tag, []).append((page, box))
    out: list[_Spot] = []
    for tag, where in printed.items():
        if len({page for page, _b in where}) > most:
            continue
        for page, at in where:
            room = _room_at(at, rooms.get(page, []))
            if room is not None and not view_sheet(pages[page], at):
                out.append(_Spot(tag, page, at, room[0], room[1]))
                break
    return out


def _named_gaps(
    spot: _Spot, named: Collection[str], rooms: Mapping[int, Sequence[tuple[str, list[float]]]]
) -> list[float]:
    """How far from the tag the plan it is on prints a label naming one of `named` (points)."""
    return [_gap(b, spot.box) for room, b in rooms.get(spot.page, []) if _naming(room) & set(named)]


def _number(tag: str) -> tuple[int, str, str] | None:
    """A tag's number, letter and suffix with the top's T left out (`XYZ-301T`: 301, "", "")."""
    parts = _TAG_PARTS.match(tag)
    if parts is None:
        return None
    return (
        int(parts.group(2)),
        _strip_t(parts.group(3) or ""),
        _strip_t(parts.group(4) or ""),
    )


def room_facts(
    sheet_tags: Sequence[str],
    description: str,
    design_pages: Mapping[int, Sequence[Mapping[str, Any]]],
    most: int | None = None,
) -> list[str]:
    """The sheet's item number against the design's rooms (`sheet_tags`: the sheet's own item
    tags; `description`: what its description strip or header says the item is). Only when the
    description names a room the design's plans print, and that is not the room the design puts
    the tag in:

    - the design's item of ANOTHER family with this number is in the room the sheet names (a
      vendor's tag took another item's family, 2026-10-31): that item, and where the design puts
      the sheet's own tag, if anywhere;
    - else, when the plan that prints the tag also prints the room the sheet names (further than
      `ROOM_FAR_PT` from the tag): the room the design puts the tag in, and the design's items in
      the room the sheet names, if any.

    `design_pages`: the design's phrases by page; `most`: an item tag is printed on at most this
    many pages. Please-checks for the reviewer, never a verdict; a fact only when code read both
    sides of it."""
    pages = _plain_pages(design_pages)
    rooms = {page: room_labels(found) for page, found in pages.items()}
    known = {w for labels in rooms.values() for room, _b in labels for w in _naming(room)}
    named = _naming(description) & known
    if not named or not sheet_tags:
        return []
    spots = _design_spots(pages, rooms, most or MAX_ITEM_PAGES)
    said = " ".join(description.split())[:80]
    lines: list[str] = []
    for tag in dict.fromkeys(sheet_tags):
        number, family = _number(tag), family_of(tag)
        if number is None:
            continue
        own = next((s for s in spots if same_item(s.tag, tag)), None)
        gaps = _named_gaps(own, named, rooms) if own is not None else []
        if own is not None and (_naming(own.room) & named or min(gaps, default=1e9) <= ROOM_FAR_PT):
            continue  # the design puts the tag in (or by) the room the sheet names
        others = [
            s
            for s in spots
            if family_of(s.tag) != family and _number(s.tag) == number and _naming(s.room) & named
        ]
        if others:
            other = min(others, key=lambda s: (len(s.tag), s.tag))
            where = (
                f"which the design prints {own.where()}"
                if own is not None
                else "which the design does not print on a plan"
            )
            line = (
                f"This sheet names '{said}'. The design's {other.room} item with this number is "
                f"{other.tag} (design p{other.page}); this sheet uses {tag}, {where}. The item "
                "number may be wrong: please check."
            )
        elif own is not None and gaps:
            line = (
                f"This sheet names '{said}'. The design prints {tag} {own.where()}; the room the sheet "
                f"names is elsewhere on that plan (its name {round(min(gaps))} points from the tag)."
            )
            held = _held(spots, named, tag)
            if held:
                line += f" The design's {held[0].room} holds {', '.join(s.tag for s in held)}."
            line += " The room name or the item number may be wrong: please check."
        else:
            continue
        if line not in lines:
            lines.append(line)
    return lines[:MAX_FACT_LINES]


def _held(spots: Sequence[_Spot], named: Collection[str], tag: str) -> list[_Spot]:
    """The design's items in the room the sheet names (one room, at most `MAX_ROOM_ITEMS`; a top's
    own tag beside its item's left out), never the sheet's own item."""
    inside = [s for s in spots if _naming(s.room) & set(named)]
    if not inside:
        return []
    room = _naming(inside[0].room)
    out: list[_Spot] = []
    for spot in sorted(inside, key=lambda s: (len(s.tag), s.tag)):
        if _naming(spot.room) != room or same_item(spot.tag, tag):
            continue
        if not any(same_item(spot.tag, kept.tag) for kept in out):
            out.append(spot)
    return sorted(out[:MAX_ROOM_ITEMS], key=lambda s: s.tag)


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
    return _largest(_text(phrase) for phrase in found)


def _largest(texts: Iterable[str]) -> list[str]:
    from app.review.assistant.finding_checks import to_inches

    sizes: dict[str, Decimal] = {}
    for text in texts:
        for printed in _INCHES.finditer(text):
            size = printed.group(0).strip()
            value = to_inches(size)
            if value is not None and value > 0:
                sizes.setdefault(size, value)
    ranked = sorted(sizes.items(), key=lambda item: -item[1])
    return [text for text, _v in ranked[:MAX_OTHER_SIZES]]


def _source_of(page: int, parts: Sequence[Mapping[str, Any]]) -> str | None:
    for part in parts:
        first, count = int(part.get("first_page") or 0), int(part.get("pages") or 0)
        if first <= page < first + count:
            return str(part.get("name") or "")
    return None


def _other_family(a: str, b: str) -> bool:
    """`a` and `b` print one number (`same_item` but for the family) in two families."""
    return family_of(a) != family_of(b) and _number(a) is not None and _number(a) == _number(b)


def other_file_facts(
    context: SetContext,
    page: int,
    parts: Sequence[Mapping[str, Any]],
    size_of: Callable[[int], tuple[float, float] | None] = lambda _page: None,
    labels_of: Callable[[int], Sequence[Mapping[str, Any]]] = lambda _page: [],
) -> list[str]:
    """For a page of a joined shop upload (`parts`: the upload's `joined_from`, each with its
    `first_page` and `pages`), the pages of the OTHER files that print the same item (`same_item`),
    or the same number in another family when both sheets' descriptions name one room
    (`sheet_description`: a vendor's tag took another item's family, 2026-10-31): each one's
    page, title words, largest printed sizes, sink and appliance models and seams. Facts for the
    reviewer to compare (the top's length with the cabinet run, sinks, seams, the item number),
    never a verdict. `size_of`: a page's size, for its title block; `labels_of`: a page's
    dimension labels as code read them (the set pack's `dims.json`), for its sizes when its text prints none
    (a hand-drawn sheet)."""
    mine_source = _source_of(page, parts)
    if mine_source is None or len(parts) < 2:
        return []
    finish = context.finish_families()
    mine = [
        t
        for t in context.tags_on(page)
        if family_of(t) not in finish and len(context.tags.get(t, [])) <= MAX_ITEM_PAGES
    ]
    _tags, described = sheet_description(context.pages.get(page, []))
    lines: list[str] = []
    seen: set[int] = set()
    for other in sorted(context.pages):
        if len(seen) >= MAX_OTHER_PAGES:
            break
        source = _source_of(other, parts)
        if other == page or source in (None, mine_source):
            continue
        theirs = context.tags_on(other)
        found = context.pages.get(other, [])
        match = next(((m, t) for m in mine for t in theirs if same_item(m, t)), None)
        link = f"prints {match[1]} (this page: {match[0]})" if match else ""
        if match is None:
            _other_tags, their_words = sheet_description(found)
            shared = sorted(_naming(described) & _naming(their_words))
            match = next(((m, t) for m in mine for t in theirs if _other_family(m, t)), None)
            if match is None or not shared:
                continue
            link = (
                f"prints {match[1]}: the same number as this page's {match[0]} in another family, "
                f"and both sheets name {' '.join(shared)} ('{described[:60]}'; "
                f"'{their_words[:60]}'). One item under two numbers? Please check which number "
                "the design gives it"
            )
        seen.add(other)
        title = "; ".join(
            _text(p)[:60]
            for p in title_block(found, size_of(other))
            if re.search(r"[A-Za-z]{3}", str(p.get("text") or ""))
        )[:240]
        sizes = ", ".join(_largest_sizes(found))
        if not sizes:
            read = _largest(str(one.get("text") or "") for one in labels_of(other))
            sizes = f"{', '.join(read)} (code read the drawn numbers)" if read else ""
        strip = {i for field in sheet_fields(found) for i in field.phrases}
        # The strip's project number is no model (`SR-100-2`).
        models = ", ".join(model_tokens([p for i, p in enumerate(found) if i not in strip]))
        models = models or "none"
        joints = sum(1 for p in found if _SEAM.search(str(p.get("text") or "")))
        lines.append(
            f"p{other} (from {source}) {link}. "
            f"Its title block: '{title or 'not read'}'. Largest sizes: {sizes or 'none'}. "
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
