"""What the vendor changed between two rounds of one drawing (chat v3, revision diff): $0, code only.

Vendor sets come in rounds. On GV's combined sheets a FreeText heading under each vendor drawing
says which round it is ("First set of comments", "Second set of comments"); the round-2 drawing
usually sits on the page after its round-1 page, sometimes beside it on the same page, sometimes in
a later file. This module answers three questions a reviewer asks about a re-submission:

- **Which drawings are rounds of one another?** `pair_rounds` reads the headings (and the item title
  printed on the sheet) and pairs round N with round N+1, each pair with a confidence and a reason.
  Deterministic: the same file always gives the same pairs.
- **Where did the drawing change?** `diff_pages` compares two drawings as PICTURES. Each of our pages
  holds its drawing as one flattened path (or a pasted picture), so there is nothing to compare as
  vectors. Both drawings are rendered drawings-only (GV's annotations removed: only the pasted
  `/Stamp` drawings are kept, `extraction.rasterise.render_page(vendor_only=True)`), every coloured
  pixel is dropped (drawings are plotted in black and grey, so colour is GV's ink, SKILL.md section
  5), then lined up (scale and shift), differenced, and the differences grouped into zones. When the
  two cannot be lined up well, it refuses (`aligned: false`) rather than reporting a page full of
  "changes" that are only a bad overlay.
- **Did the vendor fix what round 1 asked for?** `round_status` checks each round-1 finding's box
  against the zones: a change at its spot needs a cheap re-read of the new number (`changed_check`),
  no change means it is still as it was (`still_wrong`). Zones with no finding and no GV markup
  nearby are `unrequested_changes`: the vendor changed something nobody asked for.

A zone says WHERE the ink changed, never what the new value is: that is a reading, done by the
engine's re-read. This module never decides that something is fixed when a number is involved.

`reports/revision_delta.py` (code check outcomes between two package revisions) answers a different
question from the same idea: it compares recorded check outcomes, keyed by item and rule; this
compares the drawings themselves. Its "refuse rather than approximate" rule is kept here: a delta
across drawings that do not line up is refused, because it would read as a drawing that changed
everywhere.

Coordinates are PDF points of the page as shown (rotation applied), origin top-left, like every
chat box (CONTRACT2 `report_finding`).

The chat engine loads this module by name (`import_module`): `app/api/` must not reach a PDF
renderer by import (`tests/api/test_no_heavy_work.py`). For the same reason the renderer itself
(`extraction.rasterise`) is loaded by name at the first render.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Final

import cv2
import numpy as np
import pikepdf
from numpy.typing import NDArray

__all__ = [
    "COMPARE_ROUNDS_TOOL",
    "compare_rounds",
    "diff_pages",
    "gv_markup_near",
    "pair_rounds",
    "round_status",
]

type Box = tuple[float, float, float, float]
type Mask = NDArray[np.bool_]
type Grey = NDArray[np.uint8]

_POINTS: Final = 72.0
#: A grey level darker than this is ink (0 black .. 255 white).
_INK_BELOW: Final = 160
#: A pixel whose colour channels differ by more than this is coloured, so GV's ink, never the drawing.
_COLOURED_SPREAD: Final = 50
#: A pixel whose brightest channel is below this is dark enough to be the drawing's ink.
_DARK_BELOW: Final = 120
#: The share of ink that must land on ink (within a pixel or two) for two drawings to count as lined
#: up. Below it the overlay is not trusted and no zones are reported.
_ALIGNED_MATCH: Final = 0.80
#: The largest page area rendered at once (pixels): an E-size sheet at 100 dpi is about 17 million.
_MAX_PIXELS: Final = 60_000_000
#: The most zones reported; the rest are counted, never silently dropped.
_MAX_ZONES: Final = 40
#: Points around a heading's drawing kept in its region, so a shifted round still fits.
_REGION_PAD_PT: Final = 12.0
#: Points within which a finding's box or GV's markup counts as "at" a zone.
_NEAR_PT: Final = 18.0

_RENDER_LOCK: Final = threading.Lock()

_ORDINALS: Final[Mapping[str, int]] = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}
_SET_OF_COMMENTS: Final = re.compile(
    r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+(?:set|round)\s+of\s+comments?\b",
    re.IGNORECASE,
)
_ROUND_N: Final = re.compile(r"\b(?:round|rev(?:ision)?)\.?\s*(\d{1,2})\b", re.IGNORECASE)
#: An item title printed on the sheet: "2. Buffet", "12. Reception desk".
_ITEM_TITLE: Final = re.compile(r"^\s*(\d{1,3})\s*[.)]\s+(\S.{0,80})$")


# ---- pages, boxes and annotations ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Annotation:
    subtype: str
    text: str
    box: Box  # points, page as shown, origin top-left


@dataclass(frozen=True, slots=True)
class _PageFacts:
    width: float
    height: float
    annotations: tuple[_Annotation, ...]

    @property
    def stamps(self) -> tuple[Box, ...]:
        return tuple(a.box for a in self.annotations if a.subtype == "/Stamp")


def _shown_box(rect: Sequence[float], frame: Sequence[float], rotation: int) -> Box:
    """A PDF rectangle (origin bottom-left) as points of the page as shown (origin top-left)."""
    x0, y0, x1, y1 = (
        min(rect[0], rect[2]),
        min(rect[1], rect[3]),
        max(rect[0], rect[2]),
        max(rect[1], rect[3]),
    )
    cx0, cy0, cx1, cy1 = frame
    if rotation == 90:
        return (y0 - cy0, x0 - cx0, y1 - cy0, x1 - cx0)
    if rotation == 180:
        return (cx1 - x1, y0 - cy0, cx1 - x0, y1 - cy0)
    if rotation == 270:
        return (cy1 - y1, cx1 - x1, cy1 - y0, cx1 - x0)
    return (x0 - cx0, cy1 - y1, x1 - cx0, cy1 - y0)


def _text_of(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    return re.sub(r"\s+", " ", text).strip()


def _page_facts(path: Path) -> list[_PageFacts]:
    """Every page's size as shown and its annotations (subtype, text, box), in order."""
    facts: list[_PageFacts] = []
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            frame = [float(v) for v in (page.obj.get("/CropBox") or page.mediabox)]
            frame = [min(frame[0], frame[2]), min(frame[1], frame[3]), max(frame[0], frame[2])] + [
                max(frame[1], frame[3])
            ]
            rotation = int(page.obj.get("/Rotate", 0) or 0) % 360
            width, height = frame[2] - frame[0], frame[3] - frame[1]
            if rotation in (90, 270):
                width, height = height, width
            found: list[_Annotation] = []
            for annotation in page.obj.get("/Annots") or []:
                rect = annotation.get("/Rect")
                if rect is None or len(rect) != 4:
                    continue
                found.append(
                    _Annotation(
                        subtype=str(annotation.get("/Subtype")),
                        text=_text_of(annotation.get("/Contents")),
                        box=_shown_box([float(v) for v in rect], frame, rotation),
                    )
                )
            facts.append(_PageFacts(width, height, tuple(found)))
    return facts


def _gap(first: Box, second: Box) -> float:
    """The distance between two boxes (0 when they touch or overlap)."""
    dx = max(0.0, max(first[0], second[0]) - min(first[2], second[2]))
    dy = max(0.0, max(first[1], second[1]) - min(first[3], second[3]))
    return float(np.hypot(dx, dy))


def _overlap_x(first: Box, second: Box) -> float:
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0]))


def _round_of(text: str) -> int | None:
    match = _SET_OF_COMMENTS.search(text)
    if match:
        return _ORDINALS[match.group(1).lower()]
    match = _ROUND_N.search(text)
    if match and len(text) <= 40:  # a short label, not a sentence that mentions a revision
        number = int(match.group(1))
        return number if number >= 1 else None
    return None


@dataclass(frozen=True, slots=True)
class _Heading:
    page: int  # from 1
    round: int
    text: str
    box: Box
    drawing: Box | None  # the pasted drawing the heading labels, when there is one


def _drawing_for(heading: Box, stamps: Sequence[Box]) -> Box | None:
    """The pasted drawing a heading labels: the nearest stamp it sits under, over or inside."""
    best: tuple[float, Box] | None = None
    for stamp in stamps:
        if _overlap_x(heading, stamp) <= 0:
            continue
        distance = _gap(heading, stamp)
        if best is None or distance < best[0]:
            best = (distance, stamp)
    return best[1] if best is not None and best[0] <= 120 else None


def _headings(facts: Sequence[_PageFacts]) -> list[_Heading]:
    found: list[_Heading] = []
    for index, page in enumerate(facts):
        for annotation in page.annotations:
            if annotation.subtype == "/Stamp" or not annotation.text:
                continue
            number = _round_of(annotation.text)
            if number is None:
                continue
            found.append(
                _Heading(
                    index + 1,
                    number,
                    annotation.text,
                    annotation.box,
                    _drawing_for(annotation.box, page.stamps),
                )
            )
    return found


def _titles(facts: Sequence[_PageFacts]) -> tuple[list[str | None], list[str | None]]:
    """Each page's own item title, and the title in force (the last one printed on a drawing page).

    A section divider ("2. BACK WALL") has no drawing on it, so it never names an item."""
    own: list[str | None] = []
    current: list[str | None] = []
    last: str | None = None
    for page in facts:
        title: str | None = None
        if page.stamps:
            for annotation in page.annotations:
                if annotation.subtype != "/FreeText":
                    continue
                if _ITEM_TITLE.match(annotation.text) and _round_of(annotation.text) is None:
                    title = annotation.text
                    break
        own.append(title)
        if title is not None:
            last = title
        current.append(last if page.stamps else None)
    return own, current


def _same_title(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return False
    return re.sub(r"\W+", " ", first).strip().lower() == re.sub(r"\W+", " ", second).strip().lower()


def _box_dict(box: Box | None) -> dict[str, float] | None:
    if box is None:
        return None
    return {
        "x0": round(box[0], 1),
        "y0": round(box[1], 1),
        "x1": round(box[2], 1),
        "y1": round(box[3], 1),
    }


def pair_rounds(pdf_path: Path) -> list[dict[str, Any]]:
    """Pairs of drawings that are round N and round N+1 of one item, from the sheet's headings.

    Each pair: `{round1_page, round2_page, round_from, round_to, title, confidence, reason,
    round1_region, round2_region}` (pages from 1; regions are the labelled drawing's box in points,
    origin top-left, or None when no drawing sits by the heading). Rules, strongest first:

    - both headings on ONE page (the two rounds side by side): 0.95;
    - the next round's heading on the NEXT page, and that page prints no other item title: 0.9;
    - a later page with the next round's heading and the SAME item title: 0.75.

    A round heading with no partner is not paired (it is a single round).
    """
    facts = _page_facts(Path(pdf_path))
    headings = _headings(facts)
    own, current = _titles(facts)
    used: set[int] = set()
    pairs: list[dict[str, Any]] = []
    for index, first in enumerate(headings):
        if index in used:
            continue
        title = current[first.page - 1]
        choice: tuple[int, float, str] | None = None
        for other_index, second in enumerate(headings):
            if other_index in used or other_index == index or second.round != first.round + 1:
                continue
            if second.page == first.page:
                candidate = (
                    other_index,
                    0.95,
                    f"page {first.page} has both headings ({first.text!r} and {second.text!r})",
                )
            elif second.page == first.page + 1 and (
                own[second.page - 1] is None or _same_title(own[second.page - 1], title)
            ):
                candidate = (
                    other_index,
                    0.9,
                    f"{first.text!r} on page {first.page}, {second.text!r} on the next page",
                )
            elif second.page > first.page and _same_title(current[second.page - 1], title):
                candidate = (
                    other_index,
                    0.75,
                    (
                        f"{first.text!r} on page {first.page} and {second.text!r} on page "
                        f"{second.page}, both under the same item title"
                    ),
                )
            else:
                continue
            if choice is None or candidate[1] > choice[1]:
                choice = candidate
        if choice is None:
            continue
        other_index, confidence, reason = choice
        second = headings[other_index]
        used.update({index, other_index})
        pairs.append(
            {
                "round1_page": first.page,
                "round2_page": second.page,
                "round_from": first.round,
                "round_to": second.round,
                "title": title,
                "confidence": confidence,
                "reason": reason,
                "round1_region": _box_dict(first.drawing),
                "round2_region": _box_dict(second.drawing),
            }
        )
    pairs.sort(key=lambda pair: (pair["round1_page"], pair["round2_page"]))
    return pairs


def _markup_boxes(facts: _PageFacts) -> list[Box]:
    """GV's annotations on a page (notes, clouds, lines), not the drawings, headings or rules."""
    boxes: list[Box] = []
    for annotation in facts.annotations:
        if annotation.subtype in ("/Stamp", "/Link", "/Popup", "/Widget"):
            continue
        if _round_of(annotation.text) is not None or _ITEM_TITLE.match(annotation.text):
            continue
        x0, _, x1, _ = annotation.box
        if x1 - x0 >= 0.9 * facts.width:  # a separator rule across the sheet
            continue
        boxes.append(annotation.box)
    return boxes


# ---- rendering and cleaning ----------------------------------------------------------------------


def _render(data: bytes, page_index: int, dpi: int) -> NDArray[np.uint8]:
    """The page as shown, drawings only (GV's annotations removed), as an RGB array."""
    digest = hashlib.sha256(data + page_index.to_bytes(4, "big")).hexdigest()
    render_page = import_module("extraction.rasterise").render_page
    with _RENDER_LOCK:
        page = render_page(
            data,
            page_index,
            document_version_id=uuid.uuid4(),
            page_content_hash=digest,
            dpi=dpi,
            maximum_pixels=_MAX_PIXELS,
            vendor_only=True,
        )
    rgb = np.frombuffer(page.rgb_bytes, dtype=np.uint8)
    return rgb.reshape(page.height_px, page.width_px, 3)


def _clean(rgb: NDArray[np.uint8]) -> tuple[Grey, Mask]:
    """Greyscale with every coloured pixel turned white (GV's ink), and where the colour was."""
    channels = rgb.astype(np.int16)
    brightest = channels.max(axis=2)
    # A dark pixel is the drawing's black line seen through a light wash GV painted over it (dark
    # olive under a yellow highlight), so it stays ink; GV's red, blue and green ink is bright.
    coloured = ((brightest - channels.min(axis=2)) > _COLOURED_SPREAD) & (brightest >= _DARK_BELOW)
    grey = np.asarray(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), dtype=np.uint8)
    grey[coloured] = 255
    return grey, np.asarray(coloured, dtype=bool)


def _crop(image: NDArray[Any], box: Box | None, dpi: int) -> tuple[NDArray[Any], tuple[int, int]]:
    """Part of a page picture (points box, padded) and its top-left corner in pixels."""
    if box is None:
        return image, (0, 0)
    scale = dpi / _POINTS
    height, width = image.shape[:2]
    x0 = max(0, int((box[0] - _REGION_PAD_PT) * scale))
    y0 = max(0, int((box[1] - _REGION_PAD_PT) * scale))
    x1 = min(width, int(np.ceil((box[2] + _REGION_PAD_PT) * scale)))
    y1 = min(height, int(np.ceil((box[3] + _REGION_PAD_PT) * scale)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("the region is outside the page")
    return image[y0:y1, x0:x1], (x0, y0)


# ---- lining up -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Fit:
    scale: float  # round-2 pixels per round-1 pixel
    shift: tuple[float, float]  # round-1 pixel x lies at round-2-resized pixel x + shift
    match: float  # 0..1, the share of ink landing on ink


def _ink_box(ink: Mask) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(ink)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _tolerance(dpi: int) -> int:
    return max(1, round(2 * dpi / 100))


def _dilate(mask: Mask, radius: int) -> Mask:
    if radius <= 0:
        return mask
    kernel = np.ones((2 * radius + 1, 2 * radius + 1), np.uint8)
    return cv2.dilate(mask.astype(np.uint8), kernel).astype(bool)


type Coverage = NDArray[np.float32]

#: A resized pixel at least this much covered by ink is ink (a thin line shrunk by 1.6 is grey).
_COVERED: Final = 0.3


def _coverage(grey: Grey) -> Coverage:
    """1 where the picture has ink, 0 on paper."""
    return np.asarray(grey < _INK_BELOW, dtype=np.float32)


def _resized(image: NDArray[Any], scale: float) -> NDArray[Any]:
    """A round-2 picture brought to round-1's size (scale = round-2 px per round-1 px).

    Coverage is resized, not grey: shrinking a thin black line makes it grey, and a grey line under
    the ink threshold would read as a line the vendor removed."""
    if abs(scale - 1.0) < 1e-6:
        return image
    height, width = image.shape[:2]
    size = (max(1, round(width / scale)), max(1, round(height / scale)))
    return np.asarray(cv2.resize(image, size, interpolation=cv2.INTER_AREA), dtype=image.dtype)


def _place(
    moving: NDArray[Any], shift: tuple[float, float], shape: tuple[int, ...], paper: float
) -> NDArray[Any]:
    """`moving` placed on round-1's canvas: output pixel x is moving's pixel x + shift."""
    matrix = np.array([[1.0, 0.0, -shift[0]], [0.0, 1.0, -shift[1]]], dtype=np.float64)
    placed = cv2.warpAffine(
        moving,
        matrix,
        (shape[1], shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=paper,
    )
    return np.asarray(placed, dtype=moving.dtype)


def _match(first: Mask, second: Mask, radius: int) -> float:
    """Two-way share of ink landing on ink (an F-score), 0 when either side has none."""
    count_first, count_second = int(first.sum()), int(second.sum())
    if count_first == 0 or count_second == 0:
        return 0.0
    recall = float((first & _dilate(second, radius)).sum()) / count_first
    precision = float((second & _dilate(first, radius)).sum()) / count_second
    if recall + precision == 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)


def _shift_for(fixed: Coverage, moving: Coverage) -> tuple[float, float]:
    """The shift lining `moving` up on `fixed` (phase correlation on the ink, same canvas size)."""
    height = max(fixed.shape[0], moving.shape[0])
    width = max(fixed.shape[1], moving.shape[1])
    height, width = cv2.getOptimalDFTSize(height), cv2.getOptimalDFTSize(width)
    canvas_fixed = np.zeros((height, width), np.float32)
    canvas_moving = np.zeros((height, width), np.float32)
    canvas_fixed[: fixed.shape[0], : fixed.shape[1]] = fixed
    canvas_moving[: moving.shape[0], : moving.shape[1]] = moving
    (dx, dy), _ = cv2.phaseCorrelate(canvas_fixed, canvas_moving)
    # Phase correlation is periodic: a shift past half the canvas is the same shift the other way.
    if dx > width / 2:
        dx -= width
    if dy > height / 2:
        dy -= height
    return float(dx), float(dy)


def _placed_ink(moving: Coverage, fit: _Fit, shape: tuple[int, ...]) -> Mask:
    placed = _place(_resized(moving, fit.scale), fit.shift, shape, 0.0)
    return np.asarray(placed >= _COVERED, dtype=bool)


def _try(fixed: Coverage, moving: Coverage, scale: float, radius: int) -> _Fit:
    resized = _resized(moving, scale)
    shift = _shift_for(fixed, resized)
    placed = _place(resized, shift, fixed.shape, 0.0) >= _COVERED
    return _Fit(scale, shift, _match(fixed >= _COVERED, placed, radius))


def _line_up(first: Coverage, second: Coverage, dpi: int) -> _Fit:
    """The scale and shift that best lay round 2 over round 1, and how well they match.

    Scale is searched (a re-pasted drawing is often a little bigger or smaller), starting from the
    ratio of the two drawings' ink extents, on a small copy first and then at full size."""
    box_first, box_second = _ink_box(first > 0), _ink_box(second > 0)
    if box_first is None or box_second is None:
        return _Fit(1.0, (0.0, 0.0), 0.0)
    guesses = {1.0}
    ratio_x = (box_second[2] - box_second[0]) / max(1, box_first[2] - box_first[0])
    ratio_y = (box_second[3] - box_second[1]) / max(1, box_first[3] - box_first[1])
    for ratio in (ratio_x, ratio_y, float(np.sqrt(ratio_x * ratio_y))):
        if 0.25 <= ratio <= 4.0:
            guesses.update(ratio * (1 + step / 100) for step in range(-6, 7, 2))
    # Coarse: a copy about 600 px on its long side.
    shrink = max(1.0, max(first.shape) / 600)
    small_first, small_second = _resized(first, shrink), _resized(second, shrink)
    coarse = max(
        (_try(small_first, small_second, scale, 1) for scale in sorted(guesses)),
        key=lambda fit: fit.match,
    )
    # Fine: around the best coarse scale, at full size.
    radius = _tolerance(dpi)
    candidates = [coarse.scale * (1 + step / 400) for step in range(-4, 5)]
    return max(
        (_try(first, second, scale, radius) for scale in candidates),
        key=lambda fit: fit.match,
    )


# ---- zones ---------------------------------------------------------------------------------------


def _zones(
    first_ink: Mask, placed_ink: Mask, dpi: int
) -> tuple[list[tuple[int, int, int, int, str, int]], int]:
    """Changed places in round-1 pixels: (x0, y0, x1, y1, kind, changed pixels), and how many more
    there were past the cap."""
    radius = _tolerance(dpi)
    removed = first_ink & ~_dilate(placed_ink, radius)
    added = placed_ink & ~_dilate(first_ink, radius)
    # Specks (a hatch or stipple texture resampled a little differently) are not changes either: a
    # changed digit at 100 dpi is a run of a dozen pixels or more.
    speck = max(4, round(8 * (dpi / 100) ** 2))
    removed, added = _without_specks(removed, speck), _without_specks(added, speck)
    changed = removed | added
    if not changed.any():
        return [], 0
    # Group nearby changes (the digits of one number, a box and its label) into one zone.
    joined = _dilate(changed, max(2, round(6 * dpi / 100)))
    count, labels, stats, _ = _components(joined)
    minimum = max(6, round(12 * (dpi / 100) ** 2))
    found: list[tuple[int, int, int, int, str, int]] = []
    for label in range(1, count):
        x, y, w, h, _ = (int(v) for v in stats[label])
        inside = labels[y : y + h, x : x + w] == label
        gone = int((removed[y : y + h, x : x + w] & inside).sum())
        new = int((added[y : y + h, x : x + w] & inside).sum())
        if gone + new < minimum:
            continue
        if new <= 0.1 * (gone + new):
            kind = "removed"
        elif gone <= 0.1 * (gone + new):
            kind = "added"
        else:
            kind = "changed"
        part = changed[y : y + h, x : x + w] & inside
        ys, xs = np.nonzero(part)
        found.append(
            (
                x + int(xs.min()),
                y + int(ys.min()),
                x + int(xs.max()) + 1,
                y + int(ys.max()) + 1,
                kind,
                gone + new,
            )
        )
    found.sort(key=lambda zone: -zone[5])
    extra = max(0, len(found) - _MAX_ZONES)
    kept = sorted(found[:_MAX_ZONES], key=lambda zone: (zone[1] // 20, zone[0]))
    return kept, extra


def _components(
    mask: Mask,
) -> tuple[int, NDArray[np.int32], NDArray[np.int32], NDArray[np.float64]]:
    """Connected pieces of a mask (8-connected): count, label image, stats, centres."""
    image: Any = mask.astype(np.uint8)
    count, labels, stats, centres = cv2.connectedComponentsWithStats(image, connectivity=8)
    return int(count), np.asarray(labels), np.asarray(stats), np.asarray(centres)


def _without_specks(mask: Mask, smallest: int) -> Mask:
    """`mask` without its connected pieces of fewer than `smallest` pixels."""
    count, labels, stats, _ = _components(mask)
    if count <= 1:
        return mask
    keep = stats[:, cv2.CC_STAT_AREA] >= smallest
    keep[0] = False
    return np.asarray(keep[labels], dtype=bool)


def _region_of(value: object) -> Box | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return (float(value["x0"]), float(value["y0"]), float(value["x1"]), float(value["y1"]))
    if isinstance(value, Sequence) and len(value) == 4:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    raise ValueError("a region is {x0, y0, x1, y1} in points")


def _regions_for(
    pdf_a: Path, page_a: int, pdf_b: Path, page_b: int
) -> tuple[Box | None, Box | None, str]:
    """The labelled drawings to compare on two pages, from the round headings when there are any."""
    if pdf_a == pdf_b:
        for pair in pair_rounds(pdf_a):
            if pair["round1_page"] == page_a and pair["round2_page"] == page_b:
                return (
                    _region_of(pair["round1_region"]),
                    _region_of(pair["round2_region"]),
                    pair["reason"],
                )
    regions: list[Box | None] = []
    for path, page in ((pdf_a, page_a), (pdf_b, page_b)):
        facts = _page_facts(path)[page - 1]
        labelled = [h.drawing for h in _headings([facts]) if h.drawing is not None]
        regions.append(labelled[0] if len(labelled) == 1 else None)
    return regions[0], regions[1], "no pairing heading for these two pages"


def diff_pages(
    pdf: Path,
    page_a: int,
    page_b: int,
    dpi: int = 100,
    *,
    pdf_b: Path | None = None,
    region_a: Mapping[str, float] | Box | None = None,
    region_b: Mapping[str, float] | Box | None = None,
    auto_regions: bool = True,
    pictures_dir: Path | None = None,
) -> dict[str, Any]:
    """Where round 2 (`page_b`) differs from round 1 (`page_a`), as zones; pages from 1.

    Returns `{aligned, match, scale, shift, zones, more_zones, region_a, region_b, reason,
    picture_id?, picture?}`. `shift` is in points: round-1 point p sits at round-2 point
    `region_b + scale * (p - region_a) + shift` (both drawings' top-left corners as origins). Each
    zone: `{id, kind: changed|added|removed, box_a, box_b, area}` (points, page as shown, origin
    top-left; `area` in square points of box_a). `aligned: false` means the two could not be laid
    over each other well enough (`match` below 0.8): no zones are given, because a bad overlay
    reads as change everywhere.

    `region_a` / `region_b` limit the comparison to one drawing on a sheet that holds several (the
    architect's and the vendor's): with `auto_regions`, the drawing each round heading labels is
    used. `pdf_b` is the round-2 file when it is not `pdf`. With `pictures_dir`, a side-by-side
    picture with the zones outlined is saved there as `<picture_id>.png`.
    """
    pdf = Path(pdf)
    second_pdf = Path(pdf_b) if pdf_b is not None else pdf
    reason = "regions given"
    box_a, box_b = _region_of(region_a), _region_of(region_b)
    if box_a is None and box_b is None and auto_regions:
        box_a, box_b, reason = _regions_for(pdf, page_a, second_pdf, page_b)
    if (box_a is None) != (box_b is None):
        box_a = box_b = None  # one drawing on one side only: compare whole pages
    data_a = pdf.read_bytes()
    data_b = data_a if second_pdf == pdf else second_pdf.read_bytes()
    rgb_a = _render(data_a, page_a - 1, dpi)
    rgb_b = _render(data_b, page_b - 1, dpi)
    rgb_a, corner_a = _crop(rgb_a, box_a, dpi)
    rgb_b, corner_b = _crop(rgb_b, box_b, dpi)
    grey_a, _ = _clean(np.ascontiguousarray(rgb_a))
    grey_b, _ = _clean(np.ascontiguousarray(rgb_b))
    cover_a, cover_b = _coverage(grey_a), _coverage(grey_b)
    fit = _line_up(cover_a, cover_b, dpi)
    to_points = _POINTS / dpi
    result: dict[str, Any] = {
        "aligned": fit.match >= _ALIGNED_MATCH,
        "match": round(fit.match, 3),
        "scale": round(fit.scale, 4),
        "shift": {
            "x": round(fit.shift[0] * fit.scale * to_points, 1),
            "y": round(fit.shift[1] * fit.scale * to_points, 1),
        },
        "region_a": _box_dict(
            _pixel_box_points(corner_a, rgb_a.shape, to_points) if box_a else None
        ),
        "region_b": _box_dict(
            _pixel_box_points(corner_b, rgb_b.shape, to_points) if box_b else None
        ),
        "pairing": reason,
        "zones": [],
        "more_zones": 0,
        "reason": None,
    }
    if not result["aligned"]:
        result["reason"] = (
            f"the two drawings do not line up (best ink match {fit.match:.0%}, needs "
            f"{_ALIGNED_MATCH:.0%}): they may be different drawings, laid out differently or largely redrawn, so "
            "no change zones are given. Compare them by eye."
        )
    placed = _place(_resized(grey_b, fit.scale), fit.shift, grey_a.shape, 255.0)
    zones: list[dict[str, Any]] = []
    if result["aligned"]:
        found, extra = _zones(cover_a > 0, _placed_ink(cover_b, fit, grey_a.shape), dpi)
        result["more_zones"] = extra
        for number, (x0, y0, x1, y1, kind, _) in enumerate(found, start=1):
            a = (
                (corner_a[0] + x0) * to_points,
                (corner_a[1] + y0) * to_points,
                (corner_a[0] + x1) * to_points,
                (corner_a[1] + y1) * to_points,
            )
            b = tuple(
                (corner + fit.scale * (value + offset)) * to_points
                for corner, value, offset in (
                    (corner_b[0], x0, fit.shift[0]),
                    (corner_b[1], y0, fit.shift[1]),
                    (corner_b[0], x1, fit.shift[0]),
                    (corner_b[1], y1, fit.shift[1]),
                )
            )
            zones.append(
                {
                    "id": f"Z{number}",
                    "kind": kind,
                    "box_a": {"page": page_a, **(_box_dict(a) or {})},
                    "box_b": {"page": page_b, **(_box_dict((b[0], b[1], b[2], b[3])) or {})},
                    "area": round((a[2] - a[0]) * (a[3] - a[1]), 1),
                    "_pixels": (x0, y0, x1, y1),
                }
            )
    if pictures_dir is not None:
        picture = _side_by_side(grey_a, placed, zones)
        picture_id = str(uuid.uuid4())
        Path(pictures_dir).mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(".png", picture)
        if ok:
            (Path(pictures_dir) / f"{picture_id}.png").write_bytes(encoded.tobytes())
            result["picture_id"] = picture_id
            result["picture"] = {
                "picture_id": picture_id,
                "width": int(picture.shape[1]),
                "height": int(picture.shape[0]),
            }
    for zone in zones:
        zone.pop("_pixels")
    result["zones"] = zones
    return result


def _pixel_box_points(corner: tuple[int, int], shape: tuple[int, ...], to_points: float) -> Box:
    return (
        corner[0] * to_points,
        corner[1] * to_points,
        (corner[0] + shape[1]) * to_points,
        (corner[1] + shape[0]) * to_points,
    )


#: Zone outline colours (BGR for OpenCV): changed amber, added green, removed red.
_KIND_COLOURS: Final[Mapping[str, tuple[int, int, int]]] = {
    "changed": (0, 140, 230),
    "added": (60, 160, 40),
    "removed": (40, 40, 210),
}
_PICTURE_EDGE: Final = 2576


def _side_by_side(
    first: Grey, second: Grey, zones: Sequence[Mapping[str, Any]]
) -> NDArray[np.uint8]:
    """Round 1 (left) and round 2 laid over it (right), zones outlined at the same place on both."""
    gap = 24
    height, width = first.shape
    canvas = np.full((height + 30, 2 * width + gap, 3), 255, np.uint8)
    canvas[30:, :width] = cv2.cvtColor(first, cv2.COLOR_GRAY2BGR)
    canvas[30:, width + gap :] = cv2.cvtColor(second, cv2.COLOR_GRAY2BGR)
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(canvas, "Round 1", (4, 21), font, 0.6, (60, 60, 60), 1, cv2.LINE_AA)
    cv2.putText(
        canvas, "Round 2 (lined up)", (width + gap + 4, 21), font, 0.6, (60, 60, 60), 1, cv2.LINE_AA
    )
    for zone in zones:
        x0, y0, x1, y1 = zone["_pixels"]
        colour = _KIND_COLOURS[zone["kind"]]
        for left in (0, width + gap):
            cv2.rectangle(
                canvas, (left + x0 - 3, 30 + y0 - 3), (left + x1 + 3, 30 + y1 + 3), colour, 2
            )
            cv2.putText(
                canvas,
                zone["id"],
                (left + x0, 30 + max(12, y0 - 6)),
                font,
                0.45,
                colour,
                1,
                cv2.LINE_AA,
            )
    longest = max(canvas.shape[:2])
    if longest > _PICTURE_EDGE:
        factor = _PICTURE_EDGE / longest
        canvas = cv2.resize(
            canvas,
            (round(canvas.shape[1] * factor), round(canvas.shape[0] * factor)),
            interpolation=cv2.INTER_AREA,
        )
    return np.asarray(canvas, dtype=np.uint8)


# ---- round status --------------------------------------------------------------------------------


def _finding_box(finding: Mapping[str, Any], page: int | None, file: str | None) -> Box | None:
    box = finding.get("vendor_box")
    if not isinstance(box, Mapping):
        return None
    if page is not None and box.get("page") != page:
        return None
    if file is not None and box.get("file") not in (None, file):
        return None
    try:
        return (float(box["x0"]), float(box["y0"]), float(box["x1"]), float(box["y1"]))
    except (KeyError, TypeError, ValueError):
        return None


def _zone_box(zone: Mapping[str, Any]) -> Box:
    box = zone["box_a"]
    return (float(box["x0"]), float(box["y0"]), float(box["x1"]), float(box["y1"]))


def _has_values(finding: Mapping[str, Any]) -> bool:
    return any(str(finding.get(side) or "").strip() for side in ("architect", "vendor"))


def round_status(
    findings_round1: Iterable[Mapping[str, Any]],
    zones: Iterable[Mapping[str, Any]],
    *,
    page: int | None = None,
    file: str | None = None,
    markup: Iterable[Mapping[str, Any] | Box] = (),
    near: float = _NEAR_PT,
) -> dict[str, Any]:
    """Each round-1 finding against round 2's change zones, and the changes nobody asked for.

    Per finding (by its `vendor_box`, which must be on round 1's `page` and `file` when given):

    - `changed_check`: its spot changed and it has values: the engine re-reads the new value
      (cheaply) and decides `fixed_in_later_round` or `still_wrong`. A picture change is not proof
      of a fix.
    - `fixed_in_later_round`: its spot changed and it carries no values to re-read.
    - `still_wrong`: a difference (`status` different/check) whose spot did not change.
    - `untouched`: a finding that was not a difference (`status` same) and whose spot did not change.
    - `not_on_page`: no vendor box on this round-1 page; nothing is said about it.

    `unrequested_changes`: zones with no round-1 finding and no GV markup (`markup` boxes:
    annotations or coloured ink on round 1, points origin top-left) within `near` points.
    """
    zone_list = [dict(zone) for zone in zones]
    markup_boxes = [_region_of(box) for box in markup]
    per_finding: list[dict[str, Any]] = []
    claimed: set[str] = set()
    counts: dict[str, int] = {}
    for finding in findings_round1:
        box = _finding_box(finding, page, file)
        if box is None:
            status, hits = "not_on_page", []
        else:
            hits = [zone["id"] for zone in zone_list if _gap(box, _zone_box(zone)) <= near]
            claimed.update(hits)
            if hits:
                status = "changed_check" if _has_values(finding) else "fixed_in_later_round"
            elif str(finding.get("status") or "") == "same":
                status = "untouched"
            else:
                status = "still_wrong"
        counts[status] = counts.get(status, 0) + 1
        per_finding.append(
            {
                "finding_id": finding.get("id"),
                "round_status": status,
                "zones": hits,
                "recheck_box": (
                    [z["box_b"] for z in zone_list if z["id"] in hits]
                    if status == "changed_check"
                    else []
                ),
            }
        )
    unrequested = [
        zone
        for zone in zone_list
        if zone["id"] not in claimed
        and not any(m is not None and _gap(m, _zone_box(zone)) <= near for m in markup_boxes)
    ]
    return {"findings": per_finding, "unrequested_changes": unrequested, "counts": counts}


def gv_markup_near(pdf: Path, page: int, *, dpi: int = 100, region: Box | None = None) -> list[Box]:
    """GV's markup on a round-1 page: its annotations and its coloured ink (points, top-left)."""
    facts = _page_facts(Path(pdf))[page - 1]
    boxes = _markup_boxes(facts)
    rgb = _render(Path(pdf).read_bytes(), page - 1, dpi)
    _, coloured = _clean(rgb)
    joined = _dilate(coloured, max(1, round(3 * dpi / 100)))
    count, _, stats, _ = _components(joined)
    to_points = _POINTS / dpi
    for label in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[label])
        if area < 6:
            continue
        boxes.append((x * to_points, y * to_points, (x + w) * to_points, (y + h) * to_points))
    if region is not None:
        boxes = [box for box in boxes if _gap(box, region) <= _NEAR_PT]
    return boxes


# ---- the chat tool -------------------------------------------------------------------------------

#: The tool the chat engine adds to the lead's tools (CONTRACT3 "Revision diff (for wiring)").
COMPARE_ROUNDS_TOOL: Final[Mapping[str, Any]] = {
    "name": "compare_rounds",
    "description": (
        "Compare two rounds of the vendor's drawing (a first submittal and a later one) by code, "
        "pixel by pixel with GV's markup removed ($0, no AI). Use it when the set holds rounds "
        "and the question is what the vendor changed or fixed. Give neither page to list the "
        "round pairs in the set; give round1_page (round2_page is found from the headings when "
        "omitted) to compare a pair. Returns the change zones (where ink changed, as boxes on "
        "both rounds, plus a picture of the overlay), and for this answer's findings on the "
        "round-1 page whether their spot changed. A zone says WHERE ink changed, not the new "
        "value: read the number in a zone before saying what the vendor changed. "
        'Example: {"round1_page": 31, "round2_page": 32}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            "round1_page": {
                "type": "integer",
                "minimum": 1,
                "description": "Page of the earlier round (from 1)",
            },
            "round2_page": {
                "type": "integer",
                "minimum": 1,
                "description": "Page of the later round (from 1)",
            },
            "round1_file": {
                "type": "string",
                "description": "Upload holding round 1, when the set has 2+ PDFs",
            },
            "round2_file": {
                "type": "string",
                "description": "Upload holding round 2, when it is a different PDF",
            },
        },
    },
}


def _fmt_box(box: Mapping[str, Any]) -> str:
    return f"p.{box['page']} ({box['x0']:.0f}, {box['y0']:.0f})-({box['x1']:.0f}, {box['y1']:.0f})"


def compare_rounds(
    arguments: Mapping[str, Any],
    pdf_paths: Mapping[str, Path],
    pictures_dir: Path,
    *,
    findings: Iterable[Mapping[str, Any]] = (),
    dpi: int = 100,
) -> dict[str, Any]:
    """Run the `compare_rounds` tool: `{text, pairs?, diff?, round_status?, picture?}`.

    `pdf_paths` maps upload names to real paths (a file argument names one; without it the only
    upload is used). `findings` are this answer's findings so far: those on the round-1 page get a
    round status. Raises `ValueError` with a plain reason for a bad argument (the engine returns it
    to the model as a refused tool call).
    """
    if not pdf_paths:
        raise ValueError("this chat has no uploaded drawing set")

    def path_for(name: object) -> tuple[str, Path]:
        if name is None or name == "":
            if len(pdf_paths) != 1:
                raise ValueError(f"name the file: one of {sorted(pdf_paths)}")
            only = next(iter(pdf_paths))
            return only, Path(pdf_paths[only])
        if str(name) not in pdf_paths:
            raise ValueError(f"no upload named {name!r}; uploads: {sorted(pdf_paths)}")
        return str(name), Path(pdf_paths[str(name)])

    file_a, path_a = path_for(arguments.get("round1_file"))
    page_a = arguments.get("round1_page")
    page_b = arguments.get("round2_page")
    if page_a is None:
        pairs = pair_rounds(path_a)
        if not pairs:
            return {"text": "No round headings found: this set has a single round.", "pairs": []}
        lines = [
            f"p.{p['round1_page']} -> p.{p['round2_page']} (round {p['round_from']} -> "
            f"{p['round_to']}{', ' + p['title'] if p['title'] else ''}; {p['reason']}; "
            f"confidence {p['confidence']})"
            for p in pairs
        ]
        return {"text": f"{len(pairs)} round pairs:\n" + "\n".join(lines), "pairs": pairs}
    if not isinstance(page_a, int) or (page_b is not None and not isinstance(page_b, int)):
        raise ValueError("pages are whole numbers from 1")
    file_b, path_b = (
        path_for(arguments.get("round2_file")) if arguments.get("round2_file") else (file_a, path_a)
    )
    pairing = None
    if page_b is None:
        if path_b != path_a:
            raise ValueError("give round2_page for a round in another file")
        pairing = next((p for p in pair_rounds(path_a) if p["round1_page"] == page_a), None)
        if pairing is None:
            raise ValueError(
                f"no later round found for page {page_a} from the headings: give round2_page"
            )
        page_b = pairing["round2_page"]
    assert isinstance(page_b, int)
    for path, page in ((path_a, page_a), (path_b, page_b)):
        total = len(_page_facts(path))
        if not 1 <= page <= total:
            raise ValueError(f"page {page} is outside the file's {total} pages")

    diff = diff_pages(path_a, page_a, page_b, dpi, pdf_b=path_b, pictures_dir=pictures_dir)
    head = f"Round 1 p.{page_a} vs round 2 p.{page_b}"
    if not diff["aligned"]:
        text = f"{head}: {diff['reason']}"
        return {"text": text, "diff": diff, "picture": diff.get("picture")}
    zones = diff["zones"]
    kinds = {
        kind: sum(1 for z in zones if z["kind"] == kind) for kind in ("changed", "added", "removed")
    }
    lines = [
        (
            f"{head}: lined up (ink match {diff['match']:.0%}, round 2 at {diff['scale']:.3f}x; "
            f"{diff['pairing']})."
        ),
        f"{len(zones)} change zones ({kinds['changed']} changed, {kinds['added']} added, "
        f"{kinds['removed']} removed)"
        + (f", and {diff['more_zones']} smaller ones not listed." if diff["more_zones"] else "."),
    ]
    lines += [
        f"{z['id']} {z['kind']}: round 1 {_fmt_box(z['box_a'])}, round 2 {_fmt_box(z['box_b'])}"
        for z in zones
    ]
    region = _region_of(diff["region_a"]) if diff["region_a"] else None
    markup = gv_markup_near(path_a, page_a, dpi=dpi, region=region)
    status = round_status(list(findings), zones, page=page_a, file=file_a, markup=markup)
    on_page = [f for f in status["findings"] if f["round_status"] != "not_on_page"]
    if on_page:
        lines.append(
            "Findings on round 1: "
            + "; ".join(f"{f['finding_id']} {f['round_status']}" for f in on_page)
            + ". changed_check = its spot changed: read the new value in round 2 before calling "
            "it fixed."
        )
    unrequested = status["unrequested_changes"]
    if unrequested:
        lines.append(
            "Changed with no finding or GV markup nearby (nobody asked for these): "
            + ", ".join(z["id"] for z in unrequested)
            + "."
        )
    lines.append(
        "Picture: round 1 left, round 2 lined up right; amber changed, green added, red removed. "
        "Zones show WHERE ink changed, not the new values."
    )
    return {
        "text": "\n".join(lines),
        "diff": diff,
        "round_status": status,
        "pairing": pairing,
        "picture": diff.get("picture"),
        "files": [file_a, file_b],
    }
