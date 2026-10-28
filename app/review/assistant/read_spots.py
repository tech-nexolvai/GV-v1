"""Where a second or third reading looks, and what it is asked (2026-10-28, the reading fix).

On a real upload four reds were never confirmed by their AI readings, and the readings were unsure
because of the picture and the question, not the reader:

- the second reading's picture was the finding's evidence crop, with GV's markup printed over the
  vendor's numbers (GV's yellow note with its own sizes over the vendor's overall);
- a vendor string's picture was the whole string (six values) and the question asked for "the
  value" of "Dimension string (elevation)", with the third reading told the value is "at the
  centre": a reader then picked one of the parts instead of the overall;
- a small dimension's picture was about 140 px a side and did not show what it measures.

So each side a reading looks at is a **read spot** (`spots`):

- **The number's own label**, where code has it (`dims.<side>`, matched to the set pack's label
  for its dimension line): the region is centred on the label and wide enough for both ends of
  its dimension line, at least `MIN_SIDE_PT` a side, so what it measures shows around it and the
  item's value is the one at the centre. A vendor string's side is the label of its printed
  overall (`overall`): the question asks for the overall, never one of its parts.
- **Else the finding's box**, padded, with the number of printed values code finds in it
  (`values`): the third reading is told "the value at the centre" only when the box holds at most
  one value (`centred`).

What each picture shows (2026-10-28, after a real upload's outlet items stayed unread):

- **A label with no dimension line** in the pack shows more of what it measures: its region is
  `NO_LINE_ALONG_PT` along the label's direction and `NO_LINE_ACROSS_PT` across it (a 6' - 0"
  outlet height showed neither the outlet nor the floor at the 96 pt minimum).
- **A long thin region** (one way at least `LONG_THIN` times the other: a label on a long
  dimension line) adds a close-up of the label (`zoom`, the `MIN_SIDE_PT` region around it), so a
  small stacked fraction is not lost in the drawing.
- **A label code reads up the page** is shown turned, its text upright, to both readings, and
  alone (`SideCrops.sideways`); only a box whose direction code does not know keeps the region as
  drawn beside its turned copy. At most two pictures a side (`SideCrops.pictures`).

The pictures are drawn **without GV's markup**: from the set pack's drawings-only copy of the
upload (`set_pack.drawings_only`: GV's comments dropped, its stickers hidden) with GV's layers
dropped too (`extraction.rasterise.render_region(vendor_only=True)`), as the third reading always
was; else from the upload with GV's layers dropped. The same pictures go to both readings.

A vendor string's architect side is code's sum of its parts, printed nowhere: no picture of it is
read. Pure code here, but for the drawing (the renderer is loaded by name).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from app.review.assistant.finding_checks import to_inches

_log = logging.getLogger(__name__)

__all__ = [
    "LONG_THIN",
    "MIN_SIDE_PT",
    "NO_LINE_ACROSS_PT",
    "NO_LINE_ALONG_PT",
    "PAD_PT",
    "ReadPictures",
    "ReadSpot",
    "around",
    "crop_hash",
    "draw",
    "sideways",
    "source_pdf",
    "spots",
    "values_in",
]

#: The least size of a read spot's region, each way (points; 96 pt is 1 1/3 inch of paper, 400 px
#: at 300 dpi): a small label's picture shows what its dimension measures around it.
MIN_SIDE_PT: Final = 96.0
#: The margin around a label and its dimension line's ends, or around a finding's box (points).
PAD_PT: Final = 24.0
#: The region of a label with no dimension line in the pack (points): this long along the label's
#: direction (three times the minimum) and this wide across it (twice), so what it measures shows.
NO_LINE_ALONG_PT: Final = 3 * MIN_SIDE_PT
NO_LINE_ACROSS_PT: Final = 2 * MIN_SIDE_PT
#: A region at least this many times longer one way than the other is long and thin: the label's
#: close-up is added (`ReadSpot.zoom`).
LONG_THIN: Final = 2.5

type Box = tuple[float, float, float, float]
type LabelsOf = Callable[[str, int], Sequence[Mapping[str, Any]] | None]
"""A page's labels as the set pack's `dims.json` holds them, by (file, page)."""

_KEYS: Final = {"architect": "arch_box", "vendor": "vendor_box"}


@dataclass(frozen=True, slots=True)
class ReadSpot:
    """One side a reading looks at: the region drawn, and what the question may say of it."""

    side: str
    file: str | None
    page: int
    region: Box
    """The area drawn (points, origin top-left), clamped to the page when drawn."""
    values: int
    """The printed values code finds in the box the reading reads (a label's own box: 1)."""
    overall: bool = False
    """A vendor string's printed overall: the question asks for the overall."""
    tall: bool = False
    """Written sideways: a turned copy helps. The label's own direction decides (`sideways`); only
    a box whose direction code does not know is judged by its shape (taller than wide)."""
    upright_only: bool = False
    """The label's own direction says it is written up the page: the readings are shown its turned
    copy alone (text upright), never the region as drawn (`SideCrops.sideways`)."""
    zoom: Box | None = None
    """A close-up region around the label, for a long thin region (`LONG_THIN`); `None` else."""

    @property
    def centred(self) -> bool:
        """The item's value is the one at the centre of the picture (`CENTRE_WORDS` may be
        said): the region is centred on a box that holds at most one printed value."""
        return self.values <= 1

    def box(self, region: Box | None = None) -> dict[str, Any]:
        """The region drawn (`region`, else the spot's own) as a box for the renderer."""
        x0, y0, x1, y1 = region or self.region
        out: dict[str, Any] = {"page": self.page, "x0": x0, "y0": y0, "x1": x1, "y1": y1}
        if self.file is not None:
            out["file"] = self.file
        return out


def _four(raw: object) -> Box | None:
    if isinstance(raw, Mapping):
        try:
            return tuple(float(raw[k]) for k in ("x0", "y0", "x1", "y1"))  # type: ignore[return-value]
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            x0, y0, x1, y1 = (float(v) for v in raw)
        except (TypeError, ValueError):
            return None
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    return None


def _ends(label: Mapping[str, Any]) -> list[tuple[float, float]]:
    """Both ends of the label's dimension line (`dim_line`), when the pack knows it."""
    line = label.get("dim_line")
    out: list[tuple[float, float]] = []
    if isinstance(line, (list, tuple)):
        for end in line:
            if isinstance(end, (list, tuple)) and len(end) == 2:
                try:
                    out.append((float(end[0]), float(end[1])))
                except (TypeError, ValueError):
                    continue
    return out


def around(
    box: Box,
    ends: Sequence[tuple[float, float]] = (),
    *,
    least: tuple[float, float] = (MIN_SIDE_PT, MIN_SIDE_PT),
) -> Box:
    """A region centred on `box`, reaching both `ends` (a dimension line's) and the margin, at
    least `least` (width, height; `MIN_SIDE_PT` each way unless said): the box stays at the
    centre of the picture."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w = max([(x1 - x0) / 2, *(abs(x - cx) for x, _y in ends)]) + PAD_PT
    half_h = max([(y1 - y0) / 2, *(abs(y - cy) for _x, y in ends)]) + PAD_PT
    half_w = max(half_w, MIN_SIDE_PT / 2, least[0] / 2)
    half_h = max(half_h, MIN_SIDE_PT / 2, least[1] / 2)
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _least(label: Mapping[str, Any], ends: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """The least size of a label's region: `MIN_SIDE_PT` each way, or, with no dimension line in
    the pack (no `ends`), `NO_LINE_ALONG_PT` along the label's direction and `NO_LINE_ACROSS_PT`
    across it (along both, for a direction code does not know)."""
    if ends:
        return (MIN_SIDE_PT, MIN_SIDE_PT)
    orientation = label.get("orientation")
    if orientation == "h":
        return (NO_LINE_ALONG_PT, NO_LINE_ACROSS_PT)
    if orientation == "v":
        return (NO_LINE_ACROSS_PT, NO_LINE_ALONG_PT)
    return (NO_LINE_ALONG_PT, NO_LINE_ALONG_PT)


def _zoom(region: Box, own: Box) -> Box | None:
    """The label's close-up (`around(own)`, the `MIN_SIDE_PT` minimum) for a long thin region,
    one way at least `LONG_THIN` times the other; `None` else."""
    width, height = region[2] - region[0], region[3] - region[1]
    if min(width, height) <= 0 or max(width, height) < LONG_THIN * min(width, height):
        return None
    close = around(own)
    if (close[2] - close[0]) >= width and (close[3] - close[1]) >= height:
        return None
    return close


def values_in(box: Box, labels: Sequence[Mapping[str, Any]] | None) -> int:
    """How many printed values code finds in `box`: labels whose own box's centre lies in it."""
    x0, y0, x1, y1 = box
    count = 0
    for label in labels or ():
        own = _four(label.get("box")) if isinstance(label, Mapping) else None
        if own is None:
            continue
        cx, cy = (own[0] + own[2]) / 2, (own[1] + own[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            count += 1
    return count


def _pack_label(
    given: Mapping[str, Any], labels: Sequence[Mapping[str, Any]] | None
) -> Mapping[str, Any]:
    """The set pack's own copy of a finding's label (it has the dimension line), else the
    finding's copy."""
    for label in labels or ():
        if isinstance(label, Mapping) and str(label.get("id")) == str(given.get("id")):
            return label
    return given


def _same_value(label: Mapping[str, Any], value: object) -> bool:
    text = label.get("text")
    a = to_inches(text) if isinstance(text, str) else None
    b = to_inches(value) if isinstance(value, str) else None
    return a is not None and a == b


def sideways(label: Mapping[str, Any], box: Box) -> bool:
    """Whether a label's value is written sideways: its own direction when code read one
    (`orientation`: `h` along the page, `v` up it), whatever its box's shape (2026-10-28: a short
    `9"` and a stacked `254 [10]` are taller than wide, and their turned copies showed the
    horizontal text on its side); else, a direction code does not know (none, or turned at an
    angle), the box's shape: taller than wide."""
    orientation = label.get("orientation")
    if orientation in ("h", "v"):
        return bool(orientation == "v")
    return (box[3] - box[1]) > (box[2] - box[0])


def spots(finding: Mapping[str, Any], labels_of: LabelsOf | None = None) -> dict[str, ReadSpot]:
    """Each side a reading of `finding` looks at (see the module), by side; a side with no box is
    left out, and so is a vendor string's architect side (code's sum, printed nowhere)."""
    out: dict[str, ReadSpot] = {}
    string = finding.get("source") == "vendor_arithmetic"
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    for side, key in _KEYS.items():
        raw = finding.get(key)
        box = _four(raw)
        if box is None or not isinstance(raw, Mapping):
            continue
        if string and side == "architect":
            continue
        try:
            page = int(raw["page"])
        except (KeyError, TypeError, ValueError):
            continue
        file = str(raw["file"]) if raw.get("file") else None
        labels: Sequence[Mapping[str, Any]] | None = None
        if labels_of is not None and file is not None:
            try:
                labels = labels_of(file, page)
            except Exception:  # noqa: BLE001 - no labels: the finding's box alone
                labels = None
        given = (dims or {}).get(side) if isinstance(dims, Mapping) else None
        label = None
        if (
            isinstance(given, Mapping)
            and _four(given.get("box")) is not None
            and int(given.get("page") or page) == page
            and (given.get("file") in (None, file))
        ):
            label = _pack_label(given, labels)
            # A vendor string's label is its printed overall's: else the whole string is read.
            if string and not _same_value(label, finding.get("printed_total") or finding.get(side)):
                label = None
        if label is not None:
            own = _four(label.get("box"))
            assert own is not None
            ends = _ends(label)
            region = around(own, ends, least=_least(label, ends))
            known = label.get("orientation") in ("h", "v")
            tall = sideways(label, own)
            out[side] = ReadSpot(
                side=side,
                file=file,
                page=page,
                region=region,
                values=1,
                overall=string,
                tall=tall,
                upright_only=known and tall,
                # A close-up only where it fits two pictures a side (`SideCrops.pictures`): not
                # beside the region as drawn and its turned copy.
                zoom=_zoom(region, own) if known or not tall else None,
            )
            continue
        x0, y0, x1, y1 = box
        out[side] = ReadSpot(
            side=side,
            file=file,
            page=page,
            region=around(box),
            values=values_in(box, labels),
            overall=string,
            tall=(y1 - y0) > (x1 - x0),
        )
    return out


def crop_hash(pictures: Sequence[bytes], words: str) -> str:
    """The key of one reading: its pictures' bytes and its question's words (`sha256`). An
    identical picture drawn again is the same reading: it never takes another slot."""
    digest = hashlib.sha256()
    for picture in pictures:
        digest.update(len(picture).to_bytes(8, "big"))
        digest.update(picture)
    digest.update(words.encode("utf-8"))
    return digest.hexdigest()


def source_pdf(pack: Path | None, uploads: Path, name: str) -> Path:
    """The PDF a reading's picture is drawn from: the set pack's drawings-only copy of the upload
    (GV's comments dropped, its stickers hidden), else the upload itself (GV's layers are dropped
    either way when it is drawn)."""
    if pack is not None:
        try:
            made: Path = import_module("app.review.assistant.set_pack").drawings_only(
                pack, uploads, name
            )
            return made
        except Exception as failed:  # noqa: BLE001 - the upload, drawn without GV's layers
            _log.warning("reading picture: no drawings-only copy (%s)", type(failed).__name__)
    return uploads / name


@dataclass(frozen=True, slots=True)
class ReadPictures:
    """A reading's pictures of one finding (`draw`), by side, with their read spots."""

    crops: Mapping[str, Any]
    """`read_adjudication.SideCrops` by side: upright, and turned for a value written sideways."""
    spots: Mapping[str, ReadSpot]

    @property
    def centred(self) -> bool:
        """Every picture is centred on a box that holds one value (`ReadSpot.centred`)."""
        return bool(self.crops) and all(self.spots[side].centred for side in self.crops)

    @property
    def overall(self) -> bool:
        """The vendor's picture is a string's printed overall: its overall is asked."""
        return any(self.spots[side].overall for side in self.crops)

    def upright(self) -> dict[str, bytes]:
        return {side: bytes(crop.upright) for side, crop in self.crops.items()}

    def for_reading(self) -> dict[str, list[tuple[str, bytes]]]:
        """What each side's reading is shown (`SideCrops.pictures`): (kind, picture) pairs, the
        same for the second and the third reading."""
        return {side: list(crop.pictures()) for side, crop in self.crops.items()}

    def key(self, words: str) -> str:
        """`crop_hash` of every picture (upright, turned and close-up, side by side) and
        `words`."""
        pictures: list[bytes] = []
        for side in sorted(self.crops):
            crop = self.crops[side]
            pictures.append(side.encode("ascii"))
            pictures.append(bytes(crop.upright))
            if crop.turned is not None:
                pictures.append(bytes(crop.turned))
            if getattr(crop, "zoom", None) is not None:
                pictures.append(b"zoom")
                pictures.append(bytes(crop.zoom))
        return crop_hash(pictures, words)


type PdfFor = Callable[[str | None], Path | None]
"""The PDF a side's picture is drawn from, by the box's file name (`source_pdf`), or `None`."""


def draw(
    finding: Mapping[str, Any],
    *,
    labels_of: LabelsOf | None,
    pdf_for: PdfFor,
    render: Callable[..., Any] | None = None,
) -> ReadPictures:
    """The reading's pictures of `finding`: each read spot's region drawn without GV's markup
    (`read_adjudication.render_crops`, the PDF `pdf_for` names), up to 300 dpi, turned too for a
    value written sideways, with the label's close-up for a long thin region (turned the same
    way). A side that cannot be drawn is left out. Not thread-safe (pdfium): the caller holds the
    drawing lock."""
    adjudication = import_module("app.review.assistant.read_adjudication")
    found = spots(finding, labels_of)
    crops: dict[str, Any] = {}
    for side, spot in found.items():
        pdf = pdf_for(spot.file)
        if pdf is None or not pdf.is_file():
            continue
        kwargs: dict[str, Any] = {"pad": 0.0, "tall": spot.tall}
        if render is not None:
            kwargs["render"] = render
        drawn = adjudication.render_crops(pdf, spot.box(), **kwargs)
        if drawn is None:
            continue
        zoom: bytes | None = None
        if spot.zoom is not None:
            close = adjudication.render_crops(pdf, spot.box(spot.zoom), **kwargs)
            if close is not None:
                zoom = close.turned if spot.upright_only and close.turned else close.upright
        crops[side] = replace(drawn, sideways=spot.upright_only, zoom=zoom)
    return ReadPictures(crops=crops, spots=found)
