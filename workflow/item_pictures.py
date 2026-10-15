"""Item pictures: one countertop item's own drawing, zoomed, with boxes only on numbered lines (#1322).

**Why.** Today the line finder (Rekha's row question) is shown the whole sheet at about 100 dpi: the
architect's drawing, the notes, the title block and the vendor's drawing together, with up to six
code-ranked boxes. The vendor's drawing is often a quarter to a half of that picture, its numbers
are 6 to 10 pixels tall, and some boxes sit on things that are not dimension lines at all (the
stone's outline, a faucet, a row of material tags, the notes, the title block). A GV reviewer looks
at the item instead: the vendor's front view, its plan and its section, zoomed in. This module
builds that picture set, by code, at no cost (design: drawing context §3, PR-B).

**Behind `GV_ITEM_PICTURES`, off by default, and wired into no reader.** Nothing asks a model about
these pictures yet; showing them to the line finder is PR-C, and it ships only on a Pariksha
ACCEPT on all three AI sets.

**What it builds, per page:**

1. **The item map** (`build_item_map`). Each vendor drawing on the page (a pasted drawing that holds
   a dimension row, or the whole page when nothing is pasted) is one *item*. Inside it, code finds
   the drawing's views by the white space between them (`view_regions`): the ink is cut where a
   band of paper at least `gap_pt` wide runs right across, and small pieces (walls, notes,
   captions) join the view beside them. A view's heading is never needed, because vendors' view
   titles are often wrong (a "plan view" caption under a front elevation).
2. **The boxes.** Code's ranked candidate rows (`extraction/geometry/rows.py`) are kept in rank
   order, at most `boxes_per_item` per item, after dropping the ones that cannot be a dimension
   line with printed numbers (`junk_reason`): the same line found twice, a line whose labels are
   only words or a title block's fields, a row of material tags such as `AB-12`, and a line whose
   only "labels" are marks too small to be printed characters (the speckle of a stone top, a
   faucet's outline, hatching). **Every candidate that is not boxed is listed with its reason**;
   nothing vanishes.
3. **The pictures** (`build_item_pictures`). One picture per view (V1 is the view holding the
   item's best-ranked box; then the other views that hold a box or a dimension row, at most
   `views_per_item`), rendered from the vector at the largest dpi that keeps the picture within
   the readers' limits (`long_edge_px`, `front_tokens` for V1 and `other_tokens` for the rest,
   capped at `max_dpi`). Anything outside the vendor's drawing is painted white. Each box is drawn
   round its line *and all its own labels*, with its edges moved off any other printed text, so a
   box border never runs through its own numbers; its number tag sits beside the box on blank
   paper, or in the white margin on the left with a dotted leader when no blank spot is near.

**Never a source of reading.** Rows are built from the vendor's pasted drawing only, with every
coloured mark dropped before the row builder sees it (`extraction/rows.py`), so GV's markup can
never be boxed. GV's marks drawn inside an earlier round's drawing stay visible, as they do today,
and the readers' questions keep telling them so.

**Deterministic.** The same page, rows and settings give the same bytes (`evidence.crop.encode_png`,
no clock, no randomness, every tie broken by position).

Source: issue #1322 · Verification: `tests/workflow/test_item_pictures.py`
"""

from __future__ import annotations

import functools
import re
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from evidence.coordinates import PageTransform, PdfPoint
from evidence.crop import decode_rgb_png, encode_png
from extraction.geometry.rows import (
    Box,
    CountertopRowCandidate,
    LabelKind,
    PageInk,
    PageRows,
    SlotLabel,
)
from extraction.slot_reader.bedrock import ROW_BOX_COLOURS
from extraction.slot_reader.claude_output import visual_tokens

__all__ = [
    "ITEM_PICTURES_ENV",
    "MEASURED_ITEM_SETTINGS",
    "BoxedRow",
    "Item",
    "ItemFrame",
    "ItemMap",
    "ItemPage",
    "ItemPicture",
    "ItemPictureSet",
    "ItemPictureSettings",
    "ItemView",
    "LabelSort",
    "RowNote",
    "RowReason",
    "build_item_map",
    "build_item_pictures",
    "item_frames",
    "item_pictures_enabled",
    "junk_reason",
    "label_sort",
    "pdf_item_pictures",
    "view_regions",
]

#: The switch. Off unless set to a yes word; anything unrecognised is refused, never read as on.
ITEM_PICTURES_ENV: Final = "GV_ITEM_PICTURES"


def item_pictures_enabled(environ: Mapping[str, str]) -> bool:
    """Whether `GV_ITEM_PICTURES` switches the item pictures on. Unset or empty is off."""
    raw = environ.get(ITEM_PICTURES_ENV, "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    raise ValueError(f"{ITEM_PICTURES_ENV} must be true or false, not {raw!r}")


# ---------------------------------------------------------------------------
# Settings: every threshold, written down, no defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemPictureSettings:
    """Every length and limit the item map and pictures use. `MEASURED_ITEM_SETTINGS` is the set
    measured on AI Sets 1-3 (#1322)."""

    duplicate_end_pt: Decimal
    """Two rows whose first and last ticks are this close..."""
    duplicate_y_pt: Decimal
    """...and whose lines are this close are one line found twice."""
    character_minimum_pt: Decimal
    """A run of pen strokes is a printed character only when its box is at least this big both
    ways: a stone's speckle, a faucet's outline and hatching are smaller."""
    boxes_per_item: int
    frame_line_fraction: Decimal
    """A stroke spanning this much of its drawing's width or height is the drawing's border or a
    title block's rule, not part of a view: left out when the views are cut apart."""
    gap_pt: Decimal
    """A band of paper at least this wide running right across the drawing separates two views..."""
    gap_crossings: int
    """...even when this many thin strokes (a leader, a long dimension line) cross it."""
    big_view_fraction: Decimal
    """A piece of drawing covering at least this part of its drawing's area is a view of its own;
    a smaller piece (a wall pocket, a note, a caption) joins the nearest view..."""
    absorb_pt: Decimal
    """...when it is within this distance of it; otherwise it stays a piece of its own."""
    tick_reach_pt: Decimal
    """How far a row's ticks reach above and below its line: the band a box must clear."""
    margin_fraction: Decimal
    """Paper kept round a view, as a part of its width and height..."""
    margin_minimum_pt: Decimal
    """...and at least this much."""
    views_per_item: int
    max_dpi: int
    min_dpi: int
    long_edge_px: int
    """The readers' longest side (`claude_output`: 2576 px on the high-resolution tier)."""
    front_tokens: int
    """The most visual tokens V1 may cost (the readers' limit: 4784)."""
    other_tokens: int
    """The most visual tokens each other view may cost."""
    gutter_px: int
    """The white margin on the left of every picture, for the view tag and leader tags."""
    box_px: int
    """A box's line thickness."""
    clearance_fraction: Decimal
    """A box's edge stands this many label heights outside its own labels..."""
    clearance_minimum_px: int
    clearance_search: int
    """...and is moved up to this many times that distance further out to clear other text."""
    tag_px: int
    """A number tag's height."""


MEASURED_ITEM_SETTINGS: Final = ItemPictureSettings(
    duplicate_end_pt=Decimal(2),
    duplicate_y_pt=Decimal(3),
    character_minimum_pt=Decimal("3.5"),
    boxes_per_item=6,
    frame_line_fraction=Decimal("0.6"),
    gap_pt=Decimal(10),
    gap_crossings=2,
    big_view_fraction=Decimal("0.12"),
    absorb_pt=Decimal(40),
    tick_reach_pt=Decimal("1.5"),
    margin_fraction=Decimal("0.02"),
    margin_minimum_pt=Decimal(4),
    views_per_item=3,
    max_dpi=600,
    min_dpi=36,
    long_edge_px=2576,
    front_tokens=4784,
    other_tokens=2392,
    gutter_px=64,
    box_px=3,
    clearance_fraction=Decimal("0.4"),
    clearance_minimum_px=5,
    clearance_search=4,
    tag_px=40,
)

_ZERO: Final = Decimal(0)
_TWO: Final = Decimal(2)

# ---------------------------------------------------------------------------
# What a label is, by its shape only (code never reads a value here)
# ---------------------------------------------------------------------------


class LabelSort(StrEnum):
    NUMBER = "number"
    """Printed text with a digit that reads as a measurement (`30"`, `762`, `[24]`, `EQ`)."""
    STACKED = "stacked"
    """A stacked fraction: a number."""
    CHARACTERS = "characters"
    """Pen strokes the size of printed characters: may be a number, read later by the readers."""
    SMALL_MARKS = "small marks"
    """Pen strokes smaller than a printed character: speckle, hatching, a faucet's outline."""
    TAG = "tag"
    """A material or finish tag such as `AB-12` or `CD-003.E`: letters and digits, no inch mark."""
    WORDS = "words"
    """Words, a date or a title block's field: not a measurement."""


#: Letters, then digits, joined by a hyphen or a dot, with no inch mark: a material or finish tag.
_TAG: Final = re.compile(r"[A-Z]{1,5}[- ]?\d+[A-Z0-9.\-]*")
#: Two separators between digits (`01.07.2025`, `4/3/2026`): a date, not a fraction.
_DATE: Final = re.compile(r"\d+[./]\d+[./]\d+")
#: Words a dimension label carries beside its number (`95"(5EQ)`, `4"+1" Filler`, `52" VIF`).
_DIMENSION_WORDS: Final = frozenset(
    {"EQ", "VIF", "TYP", "CL", "MM", "FILLER", "FIELD", "CUT", "INCLUDING", "SCRIBE", "X", "CLR"}
)


def label_sort(label: SlotLabel, settings: ItemPictureSettings) -> LabelSort:
    """What one label looks like: a number, a tag, words, or marks of some size. Never a value."""
    if label.kind is LabelKind.GLYPHS:
        small = min(label.box.width, label.box.height) < settings.character_minimum_pt
        return LabelSort.SMALL_MARKS if small else LabelSort.CHARACTERS
    if label.stacked:
        return LabelSort.STACKED
    text = " ".join(label.lines).upper().strip()
    if _TAG.fullmatch(text) and '"' not in text:
        return LabelSort.TAG
    if _DATE.search(text):
        return LabelSort.WORDS
    words = [word for word in re.sub(r"[^A-Z ]", " ", text).split() if word not in _DIMENSION_WORDS]
    letters = sum(len(word) for word in words)
    if text in _DIMENSION_WORDS or (any(ch.isdigit() for ch in text) and letters <= 1):
        return LabelSort.NUMBER
    return LabelSort.WORDS


class RowReason(StrEnum):
    """Why a candidate row is not boxed."""

    DUPLICATE = "duplicate"
    NO_NUMBER = "no-number"
    TAGS = "material-tags"
    WORDS = "words"
    SMALL_MARKS = "small-marks"
    OUTSIDE = "outside-drawing"
    BEYOND = "beyond-boxes"


def _labels(candidate: CountertopRowCandidate) -> list[SlotLabel]:
    """Every label found for the row's slots, the nearer and the further ones."""
    return [label for slot in candidate.slots for label in slot.labels]


def _own_labels(candidate: CountertopRowCandidate) -> list[SlotLabel]:
    """The label each slot takes (its nearest): the row's own printed numbers, which its box
    holds. A further label the builder also found for a slot belongs to another line."""
    return [slot.label for slot in candidate.slots if slot.label is not None]


def junk_reason(
    candidate: CountertopRowCandidate,
    kept: Sequence[tuple[int, CountertopRowCandidate]],
    settings: ItemPictureSettings,
) -> tuple[RowReason, str] | None:
    """Why `candidate` cannot be a dimension line with printed numbers, or `None` when it may be.

    `kept` are the better-ranked rows already kept, with their page ranks. Only the shape of the
    row and its labels is looked at; no value is read and no row is chosen.
    """
    for rank, other in kept:
        if (
            abs(other.x0 - candidate.x0) <= settings.duplicate_end_pt
            and abs(other.x1 - candidate.x1) <= settings.duplicate_end_pt
            and abs(other.y - candidate.y) <= settings.duplicate_y_pt
        ):
            return RowReason.DUPLICATE, f"the same line as the row ranked {rank}, found twice"
    sorts = [label_sort(label, settings) for label in _labels(candidate)]
    count = {sort: sorts.count(sort) for sort in LabelSort}
    numbers = count[LabelSort.NUMBER] + count[LabelSort.STACKED]
    marks = count[LabelSort.CHARACTERS] + count[LabelSort.SMALL_MARKS]
    if numbers == 0 and count[LabelSort.TAG] >= 2:
        return RowReason.TAGS, "a row of material or finish tags (such as AB-12), not a dimension"
    if count[LabelSort.WORDS] >= 2 and count[LabelSort.WORDS] > numbers + marks:
        return RowReason.WORDS, "its labels are mostly words (a note, a legend or a title block)"
    if numbers == 0 and marks == 0:
        return RowReason.NO_NUMBER, "no printed number on the line: only words or tags"
    if (
        numbers == 0
        and count[LabelSort.CHARACTERS] == 0
        and count[LabelSort.TAG] == 0
        and count[LabelSort.WORDS] == 0
    ):
        return (
            RowReason.SMALL_MARKS,
            (
                "no printed number on the line: only marks smaller than a printed character "
                "(a stone's speckle, a faucet, hatching)"
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Frames and views
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemFrame:
    """One vendor drawing on the page, in pdfplumber's frame (page points, `top` downward)."""

    box: Box
    source: Literal["pasted drawing", "page"]
    """A pasted drawing (a stamp), or the whole page when its line-work is its own content."""


type _Rect = tuple[float, float, float, float]


def _rect(box: Box) -> _Rect:
    return (float(box.x0), float(box.top), float(box.x1), float(box.bottom))


def _box(rect: _Rect) -> Box:
    return Box(*(Decimal(str(round(value, 3))) for value in rect))


def _inside(x: float, y: float, rect: _Rect) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _pieces(ink: PageInk, frame: _Rect, settings: ItemPictureSettings) -> list[_Rect]:
    """The frame's strokes and characters as boxes, without the frame's border-like strokes."""
    width, height = frame[2] - frame[0], frame[3] - frame[1]
    longest = float(settings.frame_line_fraction)
    found: list[_Rect] = []

    def keep(rect: _Rect) -> None:
        middle_x, middle_y = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        if not _inside(middle_x, middle_y, frame):
            return
        if rect[2] - rect[0] >= longest * width or rect[3] - rect[1] >= longest * height:
            return
        found.append(rect)

    for line in ink.lines:
        keep(
            (
                float(min(line.x0, line.x1)),
                float(min(line.y0, line.y1)),
                float(max(line.x0, line.x1)),
                float(max(line.y0, line.y1)),
            )
        )
    for curve in ink.curves:
        keep(_rect(curve.box))
    for character in ink.characters:
        keep(_rect(character.box))
    return found


def _runs(
    pieces: Sequence[_Rect], low: float, high: float, axis: int, settings: ItemPictureSettings
) -> list[tuple[float, float]]:
    """The stretches along `axis` (0 across, 1 down) that hold ink, split where at least `gap_pt`
    of paper with at most `gap_crossings` strokes over it runs between them."""
    bins = int(high - low) + 2
    starts = [0] * (bins + 1)
    for piece in pieces:
        first = max(0, int(piece[axis] - low))
        last = min(bins - 1, int(piece[axis + 2] - low))
        starts[first] += 1
        starts[last + 1] -= 1
    runs: list[tuple[float, float]] = []
    covered = 0
    begin: int | None = None
    end = 0
    empty = 0
    gap = float(settings.gap_pt)
    for index in range(bins):
        covered += starts[index]
        if covered > settings.gap_crossings:
            if begin is not None and empty >= gap:
                runs.append((low + begin, low + end + 1))
                begin = None
            if begin is None:
                begin = index
            end = index
            empty = 0
        else:
            empty += 1
    if begin is not None:
        runs.append((low + begin, low + end + 1))
    return runs


def _tight(pieces: Sequence[_Rect]) -> _Rect:
    return (
        min(piece[0] for piece in pieces),
        min(piece[1] for piece in pieces),
        max(piece[2] for piece in pieces),
        max(piece[3] for piece in pieces),
    )


def _blocks(pieces: Sequence[_Rect], settings: ItemPictureSettings, depth: int = 0) -> list[_Rect]:
    """Cut the ink at its white bands, down then across, again inside each part (an XY cut)."""
    region = _tight(pieces)
    if depth >= 8:
        return [region]
    for axis in (1, 0) if depth % 2 == 0 else (0, 1):
        runs = _runs(pieces, region[axis], region[axis + 2], axis, settings)
        if len(runs) <= 1:
            continue
        found: list[_Rect] = []
        for low, high in runs:
            part = [piece for piece in pieces if low <= (piece[axis] + piece[axis + 2]) / 2 <= high]
            if part:
                found.extend(_blocks(part, settings, depth + 1))
        return found
    return [region]


def _gap(first: _Rect, second: _Rect) -> float:
    across = max(0.0, max(first[0], second[0]) - min(first[2], second[2]))
    down = max(0.0, max(first[1], second[1]) - min(first[3], second[3]))
    return max(across, down)


def _area(rect: _Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _union(first: _Rect, second: _Rect) -> _Rect:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def view_regions(ink: PageInk, frame: Box, settings: ItemPictureSettings) -> tuple[Box, ...]:
    """The drawing's views, as boxes in page points: big pieces of ink separated by white bands,
    with each small piece (a wall pocket, a note, a caption) joined to the nearest view within
    `absorb_pt`. Ordered top to bottom, then left to right. Empty when the frame holds no ink."""
    rect = _rect(frame)
    pieces = _pieces(ink, rect, settings)
    if not pieces:
        return ()
    blocks = _blocks(pieces, settings)
    big = float(settings.big_view_fraction) * _area(rect)
    views = [block for block in blocks if _area(block) >= big]
    small = sorted(
        (block for block in blocks if _area(block) < big), key=lambda block: (block[1], block[0])
    )
    if not views:
        views = [max(blocks, key=lambda block: (_area(block), -block[1], -block[0]))]
        small = [block for block in small if block != views[0]]
    apart: list[_Rect] = []
    for piece in small:
        nearest = min(range(len(views)), key=lambda index: (_gap(piece, views[index]), index))
        if _gap(piece, views[nearest]) <= float(settings.absorb_pt):
            views[nearest] = _union(views[nearest], piece)
        else:
            apart.append(piece)
    # A small piece far from every view stays a piece of its own: it is pictured only when it
    # holds a row, and listed otherwise.
    views.extend(apart)
    # Joining may make two views overlap: one swallowed by another is the same view.
    merged: list[_Rect] = []
    for view in sorted(views, key=lambda view: -_area(view)):
        for index, other in enumerate(merged):
            shared = max(0.0, min(view[2], other[2]) - max(view[0], other[0])) * max(
                0.0, min(view[3], other[3]) - max(view[1], other[1])
            )
            if shared >= 0.8 * _area(view):
                merged[index] = _union(other, view)
                break
        else:
            merged.append(view)
    return tuple(_box(view) for view in sorted(merged, key=lambda view: (view[1], view[0])))


# ---------------------------------------------------------------------------
# The item map
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowNote:
    """A candidate row that is not boxed, and why."""

    rank: int
    """The row's place among the page's candidates, 1 first (today's box number for 1-6)."""
    candidate: CountertopRowCandidate
    reason: RowReason
    why: str


@dataclass(frozen=True, slots=True)
class BoxedRow:
    number: int
    """The box number shown on the picture, unique on the page."""
    rank: int
    candidate: CountertopRowCandidate
    view: str
    """The tag of the view picture it is drawn on (`V1`, ...)."""
    band: Box
    """The line, its ticks and all its own labels: what the box is drawn round."""


@dataclass(frozen=True, slots=True)
class ItemView:
    tag: str
    region: Box
    boxes: tuple[int, ...]
    why: str


@dataclass(frozen=True, slots=True)
class Item:
    number: int
    frame: ItemFrame
    views: tuple[ItemView, ...]
    boxed: tuple[BoxedRow, ...]
    left_out: tuple[str, ...]
    """Views of the drawing not pictured, each with its reason."""


@dataclass(frozen=True, slots=True)
class ItemMap:
    page_index: int
    items: tuple[Item, ...]
    row_notes: tuple[RowNote, ...]
    """Every candidate row that is not boxed, with its reason: none vanishes."""
    notes: tuple[str, ...]
    """Drawings with no row, and anything else a person should know about the page."""

    @property
    def boxed(self) -> tuple[BoxedRow, ...]:
        return tuple(row for item in self.items for row in item.boxed)


def _band(candidate: CountertopRowCandidate, settings: ItemPictureSettings) -> _Rect:
    reach = float(settings.tick_reach_pt)
    rect: _Rect = (
        float(candidate.x0),
        float(candidate.y) - reach,
        float(candidate.x1),
        float(candidate.y) + reach,
    )
    for label in _own_labels(candidate):
        rect = _union(rect, _rect(label.box))
    return rect


def _with_overall(candidate: CountertopRowCandidate, band: _Rect) -> _Rect:
    if candidate.overall is None:
        return band
    rect = _union(band, _rect(candidate.overall.box))
    for label in candidate.overall.labels:
        rect = _union(rect, _rect(label.box))
    return rect


def _contains(frame: Box, candidate: CountertopRowCandidate, slack: Decimal) -> bool:
    return (
        frame.x0 - slack <= candidate.x0
        and candidate.x1 <= frame.x1 + slack
        and frame.top - slack <= candidate.y <= frame.bottom + slack
    )


def item_frames(ink: PageInk) -> tuple[ItemFrame, ...]:
    """The page's drawings as frames: every pasted drawing not lying inside another, or the whole
    page when nothing is pasted."""
    boxes = sorted(set(ink.drawing_boxes), key=lambda box: (box.top, box.x0, box.bottom, box.x1))
    top_level = [
        box
        for box in boxes
        if not any(
            other != box
            and other.x0 <= box.x0
            and box.x1 <= other.x1
            and other.top <= box.top
            and box.bottom <= other.bottom
            for other in boxes
        )
    ]
    if not top_level:
        return (ItemFrame(Box(_ZERO, _ZERO, ink.width, ink.height), "page"),)
    return tuple(ItemFrame(box, "pasted drawing") for box in top_level)


def _place_in_view(band: _Rect, views: Sequence[_Rect]) -> int:
    """The view a row belongs to: the one sharing most of its band, else the nearest."""

    def shared(view: _Rect) -> float:
        return max(0.0, min(band[2], view[2]) - max(band[0], view[0])) * max(
            0.0, min(band[3], view[3]) - max(band[1], view[1])
        )

    return min(
        range(len(views)),
        key=lambda index: (-shared(views[index]), _gap(band, views[index]), index),
    )


def build_item_map(
    page_index: int,
    rows: PageRows,
    ink: PageInk,
    frames: Sequence[ItemFrame],
    settings: ItemPictureSettings,
) -> ItemMap:
    """Which rows are boxed, on which item and view, and why every other row is not.

    `rows` are code's candidates for the page (already without the architect's rows); `frames`
    the page's vendor drawings (`item_frames`). Pure: nothing is read or rendered.
    """
    slack = settings.duplicate_end_pt
    notes: list[str] = []
    row_notes: list[RowNote] = []
    kept: list[tuple[int, CountertopRowCandidate]] = []
    by_frame: dict[int, list[tuple[int, CountertopRowCandidate]]] = {}
    for rank, candidate in enumerate(rows.candidates, start=1):
        holding = [
            index for index, frame in enumerate(frames) if _contains(frame.box, candidate, slack)
        ]
        if not holding:
            row_notes.append(
                RowNote(rank, candidate, RowReason.OUTSIDE, "outside every vendor drawing")
            )
            continue
        frame_index = min(
            holding, key=lambda index: (frames[index].box.width * frames[index].box.height, index)
        )
        reason = junk_reason(candidate, kept, settings)
        if reason is not None:
            row_notes.append(RowNote(rank, candidate, reason[0], reason[1]))
            continue
        kept.append((rank, candidate))
        by_frame.setdefault(frame_index, []).append((rank, candidate))

    for index, frame in enumerate(frames):
        if index not in by_frame:
            notes.append(
                f"drawing {index + 1} ({frame.source}) holds no dimension row that could be the "
                "countertop's, so it is not pictured"
            )
    order = sorted(by_frame, key=lambda index: by_frame[index][0][0])
    if len(order) > 1:
        notes.append(
            f"{len(order)} vendor drawings on this page hold dimension rows: each is its own "
            "item, with its own pictures and box numbers that continue from the one before"
        )
    items: list[Item] = []
    number = 0
    tag_number = 0
    for item_number, frame_index in enumerate(order, start=1):
        frame = frames[frame_index]
        rows_here = by_frame[frame_index]
        boxed_rows = rows_here[: settings.boxes_per_item]
        for rank, candidate in rows_here[settings.boxes_per_item :]:
            row_notes.append(
                RowNote(
                    rank,
                    candidate,
                    RowReason.BEYOND,
                    f"ranked below the {settings.boxes_per_item} rows kept on this drawing",
                )
            )
        regions = [_rect(view) for view in view_regions(ink, frame.box, settings)]
        if not regions:
            regions = [_rect(frame.box)]
        bands = {rank: _band(candidate, settings) for rank, candidate in rows_here}
        holds: dict[int, list[int]] = {}
        for rank, _candidate in rows_here:
            holds.setdefault(_place_in_view(bands[rank], regions), []).append(rank)
        boxed_ranks = {rank for rank, _candidate in boxed_rows}
        with_boxes = sorted(
            (view for view, ranks in holds.items() if boxed_ranks & set(ranks)),
            key=lambda view: min(rank for rank in holds[view] if rank in boxed_ranks),
        )
        with_rows = sorted(
            (view for view in holds if view not in with_boxes), key=lambda view: min(holds[view])
        )
        pictured = (with_boxes + with_rows)[: max(settings.views_per_item, len(with_boxes))]
        left_out = [
            f"a view of this drawing ({_where(regions[view])}) holds rows but is not pictured: "
            f"more than {settings.views_per_item} views of one drawing"
            for view in range(len(regions))
            if view in holds and view not in pictured
        ]
        empty = [view for view in range(len(regions)) if view not in holds]
        if empty:
            left_out.append(
                f"{len(empty)} other part(s) of this drawing hold no dimension row and are not "
                "pictured (notes, legends, a title block, details): "
                + "; ".join(_where(regions[view]) for view in empty)
            )
        candidates = dict(rows_here)
        boxed: list[BoxedRow] = []
        views: list[ItemView] = []
        for view in pictured:
            tag_number += 1
            tag = f"V{tag_number}"
            region = regions[view]
            numbers: list[int] = []
            for rank in holds.get(view, []):
                region = _union(region, _with_overall(candidates[rank], bands[rank]))
                if rank in boxed_ranks:
                    number += 1
                    numbers.append(number)
                    boxed.append(BoxedRow(number, rank, candidates[rank], tag, _box(bands[rank])))
            margin_x = max(
                float(settings.margin_minimum_pt),
                float(settings.margin_fraction) * (region[2] - region[0]),
            )
            margin_y = max(
                float(settings.margin_minimum_pt),
                float(settings.margin_fraction) * (region[3] - region[1]),
            )
            outer = _rect(frame.box)
            region = (
                max(outer[0], region[0] - margin_x),
                max(outer[1], region[1] - margin_y),
                min(outer[2], region[2] + margin_x),
                min(outer[3], region[3] + margin_y),
            )
            why = (
                "the view holding this drawing's best-ranked box"
                if not views
                else (
                    "another view of the same drawing, holding a box"
                    if numbers
                    else "another view of the same drawing (a plan, section or side), "
                    "holding a dimension row"
                )
            )
            views.append(ItemView(tag, _box(region), tuple(numbers), why))
        items.append(Item(item_number, frame, tuple(views), tuple(boxed), tuple(left_out)))
    row_notes.sort(key=lambda note: note.rank)
    return ItemMap(page_index, tuple(items), tuple(row_notes), tuple(notes))


def _where(rect: _Rect) -> str:
    return f"x {rect[0]:.0f}-{rect[2]:.0f} pt, y {rect[1]:.0f}-{rect[3]:.0f} pt"


# ---------------------------------------------------------------------------
# The pictures
# ---------------------------------------------------------------------------

type PixelBox = tuple[int, int, int, int]
"""`(left, top, right, bottom)` in a picture's pixels, right and bottom exclusive."""
type RegionRenderer = Callable[[PixelBox, int], bytes]
"""Renders a page's pixel box at a dpi (in `render_page`'s frame at that dpi) as an RGB PNG."""


@dataclass(frozen=True, slots=True)
class ItemPage:
    """What the pictures are cut from: the page's rows and ink, and where its points land."""

    page_index: int
    rows: PageRows
    ink: PageInk
    transform: PageTransform
    """The page's transform at any dpi; its own dpi is replaced for each picture."""
    page_height: Decimal
    """pdfplumber's page height, which its `top` is measured down from."""


@dataclass(frozen=True, slots=True)
class ItemPicture:
    item: int
    tag: str
    png: bytes
    dpi: int
    width: int
    height: int
    tokens: int
    region_px: PixelBox
    """The page pixels at `dpi` the picture shows (the left margin is added to these)."""
    boxes: tuple[tuple[int, PixelBox], ...]
    """Each box number with its rectangle in the picture's own pixels."""
    tags: tuple[tuple[int, PixelBox, bool], ...]
    """Each box number's tag in the picture's pixels, and whether a leader joins it to its box."""


@dataclass(frozen=True, slots=True)
class ItemPictureSet:
    item_map: ItemMap
    pictures: tuple[ItemPicture, ...]

    @property
    def tokens(self) -> int:
        return sum(picture.tokens for picture in self.pictures)


def _place(page: ItemPage, dpi: int) -> Callable[[float, float], tuple[int, int]]:
    transform = replace(page.transform, dpi=dpi)
    height = page.page_height

    def place(x: float, top: float) -> tuple[int, int]:
        point = transform.to_image(
            PdfPoint(x=Decimal(str(round(x, 4))), y=height - Decimal(str(round(top, 4))))
        )
        return (point.x, point.y)

    return place


def _pixel_box(rect: _Rect, place: Callable[[float, float], tuple[int, int]]) -> PixelBox:
    corners = [place(x, y) for x in (rect[0], rect[2]) for y in (rect[1], rect[3])]
    return (
        min(x for x, _y in corners),
        min(y for _x, y in corners),
        max(x for x, _y in corners),
        max(y for _x, y in corners),
    )


def _fits(width: int, height: int, tokens: int, settings: ItemPictureSettings) -> bool:
    return max(width, height) <= settings.long_edge_px and visual_tokens(width, height) <= tokens


def _dpi(
    region: _Rect, page: ItemPage, tokens: int, settings: ItemPictureSettings
) -> tuple[int, PixelBox]:
    """The largest dpi (at most `max_dpi`) whose picture fits the readers' limits."""
    width_pt, height_pt = region[2] - region[0], region[3] - region[1]
    gutter = settings.gutter_px
    side = max(width_pt, height_pt, 1.0)
    estimate = min(
        settings.max_dpi,
        int(72 * (settings.long_edge_px - gutter) / side),
        int(72 * 28 * (tokens / max(1.0, width_pt * height_pt)) ** 0.5),
    )
    dpi = max(settings.min_dpi, estimate + 2)
    while True:
        box = _pixel_box(region, _place(page, dpi))
        width, height = box[2] - box[0] + gutter, box[3] - box[1]
        if _fits(width, height, tokens, settings) or dpi <= settings.min_dpi:
            return dpi, box
        dpi -= 1


@functools.lru_cache(maxsize=64)
def _text_mask(text: str, height: int) -> tuple[int, tuple[tuple[bool, ...], ...]]:
    """`text` in Pillow's own scalable typeface, `height` pixels tall: its width and its pixels."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=height)
    left, top, right, bottom = font.getbbox(text)
    width = max(1, int(right - left))
    tall = max(1, int(bottom - top))
    image = Image.new("L", (width, tall), 0)
    ImageDraw.Draw(image).text((-left, -top), text, fill=255, font=font)
    data = image.tobytes()
    return width, tuple(
        tuple(data[y * width + x] >= 128 for x in range(width)) for y in range(tall)
    )


class _Canvas:
    def __init__(self, width: int, height: int, rgb: bytes) -> None:
        self.width = width
        self.height = height
        self.rgb = bytearray(rgb)

    def fill(self, box: PixelBox, colour: bytes) -> None:
        x0, y0 = max(0, box[0]), max(0, box[1])
        x1, y1 = min(self.width, box[2]), min(self.height, box[3])
        if x0 >= x1 or y0 >= y1:
            return
        run = colour * (x1 - x0)
        stride = self.width * 3
        for row in range(y0, y1):
            self.rgb[row * stride + x0 * 3 : row * stride + x1 * 3] = run

    def dark(self, box: PixelBox) -> int:
        """How many pixels in `box` are not near white: ink, or a mark already drawn."""
        x0, y0 = max(0, box[0]), max(0, box[1])
        x1, y1 = min(self.width, box[2]), min(self.height, box[3])
        if x0 >= x1 or y0 >= y1:
            return 1  # off the picture: never a clear spot
        stride = self.width * 3
        count = 0
        for row in range(y0, y1):
            line = self.rgb[row * stride + x0 * 3 : row * stride + x1 * 3]
            count += sum(1 for i in range(0, len(line), 3) if min(line[i : i + 3]) < 200)
        return count

    def outline(self, box: PixelBox, thickness: int, colour: bytes) -> None:
        x0, y0, x1, y1 = box
        self.fill((x0, y0, x1, y0 + thickness), colour)
        self.fill((x0, y1 - thickness, x1, y1), colour)
        self.fill((x0, y0, x0 + thickness, y1), colour)
        self.fill((x1 - thickness, y0, x1, y1), colour)

    def text(self, left: int, top: int, text: str, height: int, colour: bytes) -> None:
        _width, mask = _text_mask(text, height)
        for dy, row in enumerate(mask):
            for dx, inked in enumerate(row):
                if inked:
                    self.fill((left + dx, top + dy, left + dx + 1, top + dy + 1), colour)

    def png(self) -> bytes:
        return encode_png(self.width, self.height, bytes(self.rgb))


_WHITE: Final = bytes((255, 255, 255))
#: The view tag's colour: a dark slate, none of the reviewer's colours and not the vendor's black.
_VIEW_TAG: Final = bytes((40, 70, 90))
_BOX_COLOURS: Final = tuple(colour for _name, colour in ROW_BOX_COLOURS)


def _crosses(edge: PixelBox, obstacles: Iterable[PixelBox]) -> bool:
    return any(
        edge[0] < other[2] and other[0] < edge[2] and edge[1] < other[3] and other[1] < edge[3]
        for other in obstacles
    )


def _box_round(
    band: PixelBox, label_height: int, obstacles: Sequence[PixelBox], settings: ItemPictureSettings
) -> PixelBox:
    """A box round `band` (the row and all its own labels), each edge at least the clearance
    outside it and moved further out, up to `clearance_search` clearances, until it crosses no
    other printed text. Own labels are inside `band`, so the box never crosses them."""
    clear = max(
        settings.clearance_minimum_px, round(float(settings.clearance_fraction) * label_height)
    )
    thick = settings.box_px
    reach = clear * settings.clearance_search
    left, top, right, bottom = (
        band[0] - clear - thick,
        band[1] - clear - thick,
        band[2] + clear + thick,
        band[3] + clear + thick,
    )
    for step in range(reach - clear + 1):
        edge = (left - step, top, left - step + thick, bottom)
        if not _crosses(edge, obstacles):
            left -= step
            break
    for step in range(reach - clear + 1):
        edge = (right + step - thick, top, right + step, bottom)
        if not _crosses(edge, obstacles):
            right += step
            break
    for step in range(reach - clear + 1):
        edge = (left, top - step, right, top - step + thick)
        if not _crosses(edge, obstacles):
            top -= step
            break
    for step in range(reach - clear + 1):
        edge = (left, bottom + step - thick, right, bottom + step)
        if not _crosses(edge, obstacles):
            bottom += step
            break
    return (left, top, right, bottom)


def _picture(
    page: ItemPage,
    item: Item,
    view: ItemView,
    tokens: int,
    render: RegionRenderer,
    settings: ItemPictureSettings,
) -> ItemPicture:
    region = _rect(view.region)
    dpi, box_px = _dpi(region, page, tokens, settings)
    place = _place(page, dpi)
    width, height, rgb = decode_rgb_png(render(box_px, dpi))
    if (width, height) != (box_px[2] - box_px[0], box_px[3] - box_px[1]):
        raise ValueError(f"the renderer returned {width}x{height} pixels for a {box_px} region")
    gutter = settings.gutter_px
    canvas = _Canvas(width + gutter, height, _WHITE * ((width + gutter) * height))
    stride, inner = (width + gutter) * 3, width * 3
    for line in range(height):
        canvas.rgb[line * stride + gutter * 3 : (line + 1) * stride] = rgb[
            line * inner : (line + 1) * inner
        ]

    def local(rect: _Rect) -> PixelBox:
        found = _pixel_box(rect, place)
        return (
            found[0] - box_px[0] + gutter,
            found[1] - box_px[1],
            found[2] - box_px[0] + gutter,
            found[3] - box_px[1],
        )

    # Outside the vendor's drawing is painted white: another drawing or GV's notes beside it.
    frame = local(_rect(item.frame.box))
    canvas.fill((gutter, 0, max(gutter, frame[0]), height), _WHITE)
    canvas.fill((min(canvas.width, frame[2]), 0, canvas.width, height), _WHITE)
    canvas.fill((gutter, 0, canvas.width, max(0, frame[1])), _WHITE)
    canvas.fill((gutter, min(height, frame[3]), canvas.width, height), _WHITE)

    boxed = [row for row in item.boxed if row.view == view.tag]
    printed: list[tuple[int, PixelBox]] = [
        (id(character), local(_rect(character.box))) for character in page.ink.characters
    ]
    for candidate in (*page.rows.candidates, *page.rows.rejected):
        for label in _labels(candidate):
            printed.append((id(label), local(_rect(label.box))))
        if candidate.overall is not None:
            for label in candidate.overall.labels:
                printed.append((id(label), local(_rect(label.box))))
    boxes: list[tuple[int, PixelBox]] = []
    for row in boxed:
        own = {id(label) for label in _own_labels(row.candidate)}
        band = local(_rect(row.band))
        own_boxes = [local(_rect(label.box)) for label in _own_labels(row.candidate)]
        own_chars = [
            box
            for key, box in printed
            if key not in own and _crosses(box, [band]) and _inside_px(box, band)
        ]
        heights = [box[3] - box[1] for box in own_boxes] or [band[3] - band[1]]
        label_height = int(statistics.median(heights))
        obstacles = [box for key, box in printed if key not in own and box not in own_chars]
        boxes.append((row.number, _box_round(band, label_height, obstacles, settings)))
    for number, rect in boxes:
        canvas.outline(rect, settings.box_px, _BOX_COLOURS[(number - 1) % len(_BOX_COLOURS)])

    tags = _place_tags(canvas, boxes, settings)
    canvas.text(8, 8, view.tag, settings.tag_px // 2, _VIEW_TAG)
    return ItemPicture(
        item=item.number,
        tag=view.tag,
        png=canvas.png(),
        dpi=dpi,
        width=canvas.width,
        height=canvas.height,
        tokens=visual_tokens(canvas.width, canvas.height),
        region_px=box_px,
        boxes=tuple(boxes),
        tags=tuple(tags),
    )


def _inside_px(inner: PixelBox, outer: PixelBox) -> bool:
    return (
        outer[0] <= inner[0]
        and inner[2] <= outer[2]
        and outer[1] <= inner[1]
        and inner[3] <= outer[3]
    )


def _place_tags(
    canvas: _Canvas, boxes: Sequence[tuple[int, PixelBox]], settings: ItemPictureSettings
) -> list[tuple[int, PixelBox, bool]]:
    """Each box's number tag: beside its box on blank paper when there is a blank spot (left,
    right, above, below), else in the left margin with a dotted leader to the box. A tag never
    covers ink, another box or another tag."""
    size = settings.tag_px
    gap = 4
    placed: list[tuple[int, PixelBox, bool]] = []
    taken: list[PixelBox] = []
    outlines = [rect for _number, rect in boxes]
    for number, rect in boxes:
        digits = str(number)
        text_height = size - 12
        text_width, _mask = _text_mask(digits, text_height)
        width = max(size, text_width + 12)
        middle = (rect[1] + rect[3]) // 2 - size // 2
        spots = [
            (rect[0] - gap - width, middle),
            (rect[2] + gap, middle),
            (rect[0], rect[1] - gap - size),
            (rect[0], rect[3] + gap),
            (rect[0] - gap - width, rect[1]),
            (rect[0] - gap - width, rect[3] - size),
            (rect[2] + gap, rect[1]),
            (rect[2] + gap, rect[3] - size),
            (rect[2] - width, rect[1] - gap - size),
            (rect[2] - width, rect[3] + gap),
            (rect[0] - gap - width, rect[1] - gap - size),
            (rect[0] - gap - width, rect[3] + gap),
            (rect[2] + gap, rect[1] - gap - size),
            (rect[2] + gap, rect[3] + gap),
        ]
        chosen: PixelBox | None = None
        for x, y in spots:
            spot = (x, y, x + width, y + size)
            if (
                spot[0] >= settings.gutter_px
                and spot[2] <= canvas.width
                and spot[1] >= 0
                and spot[3] <= canvas.height
                and not _crosses(spot, taken)
                and not _crosses(spot, outlines)
                and canvas.dark(spot) == 0
            ):
                chosen = spot
                break
        leader = chosen is None
        if chosen is None:
            y = max(0, min(canvas.height - size, middle))
            spot = (4, y, 4 + min(width, settings.gutter_px - 8), y + size)
            while _crosses(spot, taken) and spot[3] < canvas.height:
                spot = (spot[0], spot[3] + gap, spot[2], spot[3] + gap + size)
            chosen = spot
        colour = _BOX_COLOURS[(number - 1) % len(_BOX_COLOURS)]
        canvas.fill(chosen, colour)
        tag_width, _mask = _text_mask(digits, text_height)
        canvas.text(
            chosen[0] + (chosen[2] - chosen[0] - tag_width) // 2,
            chosen[1] + 6,
            digits,
            text_height,
            _WHITE,
        )
        if leader:
            y = (chosen[1] + chosen[3]) // 2
            x = chosen[2]
            while x < rect[0]:
                canvas.fill((x, y - 1, min(rect[0], x + 6), y + 1), colour)
                x += 12
        taken.append(chosen)
        placed.append((number, chosen, leader))
    return placed


def build_item_pictures(
    page: ItemPage,
    item_map: ItemMap,
    *,
    render: RegionRenderer,
    settings: ItemPictureSettings,
) -> ItemPictureSet:
    """Every item's pictures, V1 first: V1 within `front_tokens`, every other view within
    `other_tokens`. `render` draws a page region (in `render_page`'s pixels at a dpi) from the
    vector; the stage renders the vendor's drawing only."""
    pictures = [
        _picture(
            page,
            item,
            view,
            settings.front_tokens if position == 0 else settings.other_tokens,
            render,
            settings,
        )
        for item in item_map.items
        for position, view in enumerate(item.views)
    ]
    return ItemPictureSet(item_map, tuple(pictures))


def pdf_item_pictures(
    data: bytes,
    page_index: int,
    rows: PageRows,
    ink: PageInk,
    *,
    settings: ItemPictureSettings = MEASURED_ITEM_SETTINGS,
    maximum_pixels: int = 20_000_000,
) -> ItemPictureSet:
    """The item map and pictures of one page of a PDF: its frames are its pasted drawings
    (`item_frames`), and each region is drawn again from the vector, the vendor's drawing only
    (`render_region(vendor_only=True)`). `rows` and `ink` are `page_rows_and_ink`'s, at any dpi.
    Read-only: the PDF is never changed."""
    import io

    import pdfplumber

    from extraction.rasterise import render_region
    from extraction.reader import page_frame

    with pdfplumber.open(io.BytesIO(data)) as document:
        transform, height = page_frame(document.pages[page_index], 72)
    page = ItemPage(page_index, rows, ink, transform, height)
    item_map = build_item_map(page_index, rows, ink, item_frames(ink), settings)

    def render(box: PixelBox, dpi: int) -> bytes:
        return render_region(
            data,
            page_index,
            box_px=box,
            dpi=dpi,
            maximum_pixels=maximum_pixels,
            vendor_only=True,
        )

    return build_item_pictures(page, item_map, render=render, settings=settings)
