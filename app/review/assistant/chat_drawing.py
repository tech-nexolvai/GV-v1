"""Evidence crops and the marked-up PDF of a chat answer's findings (chat v2), drawn by code.

**Loaded by name, never imported.** `app/api/` must not reach a PDF renderer by import
(`tests/api/test_no_heavy_work.py`); `chat_findings.py` loads this module with `import_module` the
first time a finding needs a picture or a reviewer asks for the marked-up PDF, as
`page_pictures.py` loads its renderer.

**Boxes are in PDF points of the page as it is shown** (the crop box, after the page's rotation),
origin top-left: what `pdftotext -bbox` prints and what a picture measures (points = pixels x 72 /
dpi). `crop_png` cuts that area (padded) from the original upload page; `markup_pdf` turns it back
into the page's own drawing space (rotation and box origin included) to draw a cloud there.

**The cloud drawing looks like a reviewer's markup (#1537)**: tight clouds at the value (its
printed text, found by `verify_value.printed_spots`, else at most 2.5 in), a numbered marker and a
leader to a note in clear space (`cloud_layout`), only Wrong items clouded by default, drawn on a
copy without earlier review markup, and notes pages grouped by page with a picture of each spot.

**pdfium is not thread-safe**: every use of it here holds one lock (helpers report findings from
their own threads, and a reviewer may ask for a marked-up PDF while an answer is being written).
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from xml.sax.saxutils import escape

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from reportlab.lib.colors import Color, white  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.review.assistant import proof
from app.review.assistant.chat_findings import decision_help_lines
from app.review.assistant.chat_trust import (
    CLOUD_WORDS,
    TRUST_WORDS,
    cloud_kind,
    needs_text,
    shown_counts,
)
from app.review.assistant.cloud_layout import (
    MAX_CLOUD_PT,
    NOTE_LINES,
    NOTE_WIDTH_PT,
    InkMap,
    capped,
    marker_spot,
    place_notes,
    snapped,
    wrap,
)
from app.review.assistant.finding_checks import to_inches
from app.review.assistant.impact import BIGGEST_FIRST, biggest_line
from extraction.stamp_text import carries_drawing, pasted_picture
from vocabulary.product_registry import registry

__all__ = [
    "CLEAN_WORDS",
    "LEGEND",
    "LEGEND_CLOUDED",
    "MARKUP_TITLE",
    "MAX_CLOUD_PT",
    "PDFIUM_LOCK",
    "REPORT_TITLE",
    "Base",
    "Entry",
    "Mark",
    "Section",
    "also_marks",
    "check_report_pdf",
    "clone_pdf",
    "cloud_kind",
    "counts_line",
    "crop_png",
    "draw_markup",
    "entry_lines",
    "markup_pdf",
    "needs_text",
    "note_text",
    "page_sizes",
    "trust_tag",
]

_log = logging.getLogger(__name__)

MARKUP_TITLE: Final = "AI markup: draft, not GV's signed review"
#: Evidence crops: this many dots per inch, padded by this many points, at most this many pixels a
#: side (a big box is drawn at a lower dpi rather than cut).
CROP_DPI: Final = 170
CROP_PAD_PT: Final = 24.0
CROP_MAX_PX: Final = 1600
_PDFIUM_LOCK: Final = threading.Lock()
#: The same lock for the set pack and the `zoom` tool (`set_pack.py`).
PDFIUM_LOCK: Final = _PDFIUM_LOCK

type Box = tuple[float, float, float, float]


def page_sizes(pdf: Path) -> list[tuple[float, float]]:
    """Each page's shown size in points (width, height), rotation applied."""
    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            return [tuple(document[n].get_size()) for n in range(len(document))]
        finally:
            document.close()


def crop_png(pdf: Path, page_number: int, box: Box, *, cache: Path | None = None) -> bytes | None:
    """The area `box` (x0, y0, x1, y1; points, origin top-left) of a page, padded, as a PNG; `None`
    when the page does not exist or the box is outside it.

    With `cache` (the set pack's `hires/` folder, #1548) it is cut from the page's 300-dpi picture,
    drawn once on first need (`page_cache`), instead of rendering the page again."""
    if cache is not None:
        cut = _cached_crop(pdf, page_number, box, cache)
        if cut is not None:
            return cut
    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            if not 1 <= page_number <= len(document):
                return None
            page = document[page_number - 1]
            width, height = page.get_size()
            x0, y0, x1, y1 = box
            left = max(0.0, min(x0, x1) - CROP_PAD_PT)
            top = max(0.0, min(y0, y1) - CROP_PAD_PT)
            right = min(width, max(x0, x1) + CROP_PAD_PT)
            bottom = min(height, max(y0, y1) + CROP_PAD_PT)
            if right - left < 1 or bottom - top < 1:
                return None
            # The box must touch the page itself, not only its padding.
            if (
                max(x0, x1) <= 0
                or max(y0, y1) <= 0
                or min(x0, x1) >= width
                or min(y0, y1) >= height
            ):
                return None
            scale = min(CROP_DPI / 72, CROP_MAX_PX / max(right - left, bottom - top))
            bitmap = page.render(
                scale=scale,
                crop=(left, height - bottom, width - right, top),
                may_draw_forms=True,
            )
            picture = bitmap.to_pil()
        finally:
            document.close()
    out = io.BytesIO()
    picture.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _padded(box: Box, width: float, height: float) -> Box | None:
    x0, y0, x1, y1 = box
    if max(x0, x1) <= 0 or max(y0, y1) <= 0 or min(x0, x1) >= width or min(y0, y1) >= height:
        return None
    left = max(0.0, min(x0, x1) - CROP_PAD_PT)
    top = max(0.0, min(y0, y1) - CROP_PAD_PT)
    right = min(width, max(x0, x1) + CROP_PAD_PT)
    bottom = min(height, max(y0, y1) + CROP_PAD_PT)
    if right - left < 1 or bottom - top < 1:
        return None
    return left, top, right, bottom


def _cached_crop(pdf: Path, page_number: int, box: Box, cache: Path) -> bytes | None:
    """`crop_png` from the hi-res page cache; `None` when the cache cannot give it (the caller
    renders it as before)."""
    try:
        page_cache = import_module("app.review.assistant.page_cache")
        sizes = page_cache.page_sizes(cache, pdf)
        if not 1 <= page_number <= len(sizes):
            return None
        size = sizes[page_number - 1]
        area = _padded(box, size.width, size.height)
        if area is None:
            return None
        scale = min(CROP_DPI / 72, CROP_MAX_PX / max(area[2] - area[0], area[3] - area[1]))
        picture = page_cache.cut(cache, pdf, page_number, area, scale)
    except Exception as failed:  # noqa: BLE001 - rendered directly instead
        _log.warning("assistant evidence crop cache failed: %s", type(failed).__name__)
        return None
    if picture is None:
        return None
    out = io.BytesIO()
    picture.save(out, format="PNG", compress_level=3)
    return out.getvalue()


# ---- the cloud drawing ---------------------------------------------------------------------------
#
# How it looks (#1537), like a reviewer's markup:
# - a tight red cloud round each Wrong value (snapped to the value's printed text, else at most 2.5
#   in, with the AI's whole area outlined thinly when it was bigger), a filled numbered marker at
#   the cloud, and a leader line to a short note in the clearest nearby space (`cloud_layout`);
# - a "Your decision" item gets only its small numbered amber marker on the sheet (its full entry
#   is on the notes pages), unless the caller asks for amber clouds too (`cloud_decisions`);
# - on a set that carries earlier review markup (GV's boxes, notes, ticks), the drawing is made on a
#   copy without it; the pasted drawings (`/Stamp`) stay;
# - notes pages: counts, legend, then every item grouped by page with a small picture of its spot.

_RED: Final = Color(0.78, 0.05, 0.09)
_AMBER: Final = Color(0.78, 0.45, 0.0)
_DARK: Final = Color(0.1, 0.1, 0.1)
_GREY: Final = Color(0.55, 0.55, 0.55)
_NOTE_FONT: Final = "Helvetica"
_NOTE_BOLD: Final = "Helvetica-Bold"
_NOTE_SIZE: Final = 8.5
_TAG_SIZE: Final = 7.5
_NOTE_LEADING: Final = 10.5
_NOTE_PAD: Final = 4.0
_NOTE_BAR: Final = 3.0
_MARKER_RADIUS: Final = 7.0
_CLOUD_PAD: Final = 4.0
#: A box longer than this on a side is snapped to its value's printed text when code finds it (a
#: smaller box is already tight); the search stops after this many seconds for the whole drawing.
_SNAP_FROM_PT: Final = 54.0
_SNAP_BUDGET_S: Final = 25.0
#: The ink map's render: pixels per point.
_INK_PX_PER_PT: Final = 0.5
#: The notes pages' pictures: size in points, pixels per point of the picture.
_THUMB_W: Final = 150.0
_THUMB_H: Final = 96.0
_THUMB_PX_PER_PT: Final = 2.0
#: Where the page-1 stamp sits (shown points from the top-left), kept clear of notes.
_STAMP_AT: Final = (24.0, 30.0)
_STAMP_SIZE: Final = 14.0
#: Annotation types that are a reviewer's markup: left out of a clean copy unless they are a pasted
#: drawing or picture (`carries_drawing`, `pasted_picture`).
_REVIEW_MARKUP: Final = frozenset(
    {
        "/FreeText",
        "/Square",
        "/Circle",
        "/Ink",
        "/PolyLine",
        "/Polygon",
        "/Line",
        "/Text",
        "/Highlight",
        "/Underline",
        "/StrikeOut",
        "/Squiggly",
        "/Caret",
        "/Popup",
        "/FileAttachment",
        "/Sound",
    }
)
_OVERLAY_NAME: Final = "/GVCloudDrawing"

CLEAN_WORDS: Final = "Drawn on a copy without earlier review markup"
LEGEND: Final = (
    "Red cloud, red numbered circle and note: Wrong. The vendor's value is wrong; the note says "
    'what it needs to be, and ends in grey with "AI reading only" or "Likely" when no '
    "independent check confirmed it. Amber numbered circle (no cloud): not compared; its entry below "
    "says what to decide. Right items are not marked; they are listed at the end."
)
LEGEND_CLOUDED: Final = (
    "Red cloud, red numbered circle and note: Wrong. The vendor's value is wrong; the note says "
    'what it needs to be, and ends in grey with "AI reading only" or "Likely" when no '
    "independent check confirmed it. Amber cloud and amber numbered circle: not compared "
    "(the AI cannot call it right or wrong). Right items are not marked; they are listed at the "
    "end."
)


@dataclass(slots=True)
class Base:
    """The copy of the vendor's file the cloud drawing is drawn on (`clone_pdf`)."""

    pdf: pikepdf.Pdf
    dropped: int
    """How many earlier review marks the copy left out (0: none, or the copy keeps them)."""


def _is_review_markup(annotation: Any) -> bool:
    if str(annotation.get("/Subtype")) not in _REVIEW_MARKUP:
        return False
    return not (carries_drawing(annotation) or pasted_picture(annotation))


def clone_pdf(source: Path, clean: bool | None = None) -> Base:
    """A copy of `source` to draw on. `clean`: leave out the earlier review markup (FreeText,
    Square, Circle, Ink, PolyLine, ... annotations) and keep the pasted drawings (`/Stamp`); `None`
    (the default): clean when the file has any such markup. Made ahead of time by a full check
    (`chat_findings.markup_base`)."""
    pdf = pikepdf.open(source)
    dropped = 0
    for page in pdf.pages:
        annotations = page.obj.get("/Annots")
        if not isinstance(annotations, pikepdf.Array):
            continue
        kept = [a for a in annotations if not _is_review_markup(a)]
        dropped += len(annotations) - len(kept)
        if clean is not False and len(kept) != len(annotations):
            page.obj["/Annots"] = pikepdf.Array(kept)
    return Base(pdf, dropped if clean is not False else 0)


@dataclass(frozen=True, slots=True)
class Mark:
    """One finding drawn on the sheet."""

    number: int
    finding: Mapping[str, Any]
    page: int
    box: Box
    """The AI's box (shown points, origin top-left)."""
    kind: str
    """`wrong` (red) or `check` (amber: your decision)."""
    cloud: bool
    """A cloud, a numbered marker and a note; else the numbered marker only."""
    value: str
    """The value printed at the box (the vendor's, or the architect's on an architect box)."""
    extra: bool = False
    """The same item on another page (#1554, `also_on`): marked there too, listed once."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One item on the notes pages."""

    number: int
    finding: Mapping[str, Any]
    kind: str | None
    """`wrong`, `check` or `None` (right)."""
    lines: Sequence[str]
    """The lines under its heading."""
    picture: bool = False
    """Show a small picture of its spot (when it was drawn on the sheet)."""


@dataclass(frozen=True, slots=True)
class Section:
    heading: str
    entries: Sequence[Entry]
    by_page: bool = True


@dataclass(slots=True)
class _Placed:
    mark: Mark
    spot: Box
    """The cloud (or, for a marker only, the value's spot)."""
    area: Box | None
    """The AI's whole area, outlined dashed, when the cloud was cut down to size."""
    marker: tuple[float, float] = (0.0, 0.0)
    note: Box | None = None
    lines: tuple[str, ...] = ()
    tag: str = ""
    """The grey trust words at the note's end ("AI reading only", "Likely"); "" when Confirmed."""
    tag_inline: bool = True
    """The tag ends the last line; else it has a line of its own."""


class _Budget:
    def __init__(self, seconds: float) -> None:
        self._until = time.monotonic() + seconds

    def left(self) -> bool:
        return time.monotonic() < self._until


def note_text(number: int, finding: Mapping[str, Any]) -> str:
    """The words of a finding's note on the sheet (and the first line of its notes-page entry),
    after its sign: "× 12 Outlet height: vendor 46", make it 37" (architect 3'-1")"."""
    del number
    vendor, architect = finding.get("vendor"), str(finding.get("architect"))
    if cloud_kind(finding) == "wrong":
        needs = needs_text(finding)
        if to_inches(needs) is None:
            # Not one value (a sentence, a chain): what the architect shows, not "make it".
            return f"{finding.get('item')}: vendor {vendor}; architect {architect}"
        text = f"{finding.get('item')}: vendor {vendor}, make it {needs}"
        if needs != architect:
            text += f" (architect {architect})"
        return text
    return f"Not compared: {finding.get('item')}: vendor {vendor}; architect {architect}"


def _sign(kind: str | None) -> str:
    return {"wrong": "×", "check": "?"}.get(str(kind), "✓")


def _colour(kind: str | None) -> Color:
    return _RED if kind == "wrong" else _AMBER


def _box_value(finding: Mapping[str, Any]) -> str:
    """The value printed at the box drawn on: the vendor's at a vendor box, else the architect's."""
    if isinstance(finding.get("vendor_box"), Mapping):
        return str(finding.get("vendor") or "")
    return str(finding.get("architect") or "")


def _shown_size(page: Any) -> tuple[float, float]:
    box = page.cropbox
    width, height = float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
    rotation = int(page.obj.get("/Rotate", 0) or 0) % 360
    return (height, width) if rotation in (90, 270) else (width, height)


def _shown_space(c: canvas.Canvas, page: Any) -> None:
    """Make the canvas draw in the shown page's points, origin bottom-left, y up (whatever the
    page's rotation and crop box)."""
    llx, lly, urx, ury = (float(v) for v in page.cropbox)
    rotation = int(page.obj.get("/Rotate", 0) or 0) % 360
    if rotation == 90:
        c.transform(0, 1, -1, 0, urx, lly)
    elif rotation == 180:
        c.transform(-1, 0, 0, -1, urx, ury)
    elif rotation == 270:
        c.transform(0, -1, 1, 0, llx, ury)
    else:
        c.translate(llx, lly)


def _cloud(c: canvas.Canvas, x0: float, y0: float, x1: float, y1: float, r: float = 5.0) -> None:
    """Scallops bulging outward along the four edges of a box (y up)."""
    for ax, ay, bx, by, start in (
        (x0, y0, x1, y0, 180),
        (x1, y0, x1, y1, 270),
        (x1, y1, x0, y1, 0),
        (x0, y1, x0, y0, 90),
    ):
        n = max(2, int(max(abs(bx - ax), abs(by - ay)) // (2 * r)))
        for i in range(n):
            cx = ax + (bx - ax) * (i + 0.5) / n
            cy = ay + (by - ay) * (i + 0.5) / n
            c.arc(cx - r, cy - r, cx + r, cy + r, start, 180)


def _measure(text: str, font: str = _NOTE_FONT, size: float = _NOTE_SIZE) -> float:
    return float(pdfmetrics.stringWidth(text, font, size))


def _prefix(mark: Mark) -> str:
    return f"{_sign(mark.kind)} {mark.number}"


def trust_tag(finding: Mapping[str, Any]) -> str:
    """The grey words a Wrong note ends with when no independent check confirmed it: the trust
    level's words ("AI reading only", "Likely"); "" for Confirmed and for other items."""
    if cloud_kind(finding) != "wrong":
        return ""
    level = str(finding.get("trust_level") or "")
    return "" if level in ("", "confirmed") else TRUST_WORDS.get(level, level)


def _note_lines(mark: Mark) -> tuple[tuple[str, ...], str, bool, float, float]:
    """A note's wrapped lines, its trust tag and whether the tag fits on the last line, and its
    size (points)."""
    lead = _measure(_prefix(mark) + "  ", _NOTE_BOLD)
    room = NOTE_WIDTH_PT - 2 * _NOTE_PAD - _NOTE_BAR - lead
    text = note_text(mark.number, mark.finding)
    tag = trust_tag(mark.finding)
    lines = tuple(wrap(text, room, _measure))
    inline = True
    widths = [_measure(line) for line in lines]
    if tag:
        spaced = _measure("  " + tag, _NOTE_FONT, _TAG_SIZE)
        if widths and widths[-1] + spaced <= room:
            widths[-1] += spaced
        else:
            inline = False
            lines = tuple(wrap(text, room, _measure, lines=NOTE_LINES - 1))
            widths = [_measure(line) for line in lines] + [_measure(tag, _NOTE_FONT, _TAG_SIZE)]
    rows = len(lines) + (0 if inline else 1)
    width = lead + max(widths, default=0.0) + 2 * _NOTE_PAD + _NOTE_BAR
    height = rows * _NOTE_LEADING + 2 * _NOTE_PAD - (_NOTE_LEADING - _NOTE_SIZE)
    return lines, tag, inline, width, height


def _ink(document: Any, index: int) -> InkMap | None:
    with _PDFIUM_LOCK:
        try:
            grey = document[index].render(scale=_INK_PX_PER_PT, grayscale=True).to_numpy()
        except Exception as failed:  # noqa: BLE001 - notes are then placed without the ink map
            _log.warning("cloud drawing ink map failed: %s", type(failed).__name__)
            return None
    if grey.ndim == 3:
        grey = grey[:, :, 0]
    return InkMap(grey, _INK_PX_PER_PT)


_SNAP_CACHE: dict[tuple[str, int, int, int, Box, str], list[Box]] = {}
_SNAP_CACHE_LOCK: Final = threading.Lock()
_SNAP_CACHE_SIZE: Final = 4096


def _printed(source: Path, mark: Mark, box: Box) -> list[Box]:
    """`verify_value.printed_spots` for a mark, remembered for this file version (a remade drawing,
    e.g. after the reviewer's decisions, finds them again at once)."""
    stat = source.stat()
    key = (str(source.resolve()), stat.st_mtime_ns, stat.st_size, mark.page, box, mark.value)
    with _SNAP_CACHE_LOCK:
        if key in _SNAP_CACHE:
            return _SNAP_CACHE[key]
    verify = import_module("app.review.assistant.verify_value")
    spots = list(
        verify.printed_spots(
            source, mark.page, dict(zip(("x0", "y0", "x1", "y1"), box, strict=True)), mark.value
        )
    )
    with _SNAP_CACHE_LOCK:
        if len(_SNAP_CACHE) >= _SNAP_CACHE_SIZE:
            _SNAP_CACHE.clear()
        _SNAP_CACHE[key] = spots
    return spots


def _spots(source: Path, marks: Sequence[Mark]) -> dict[tuple[int, int], tuple[Box, Box | None]]:
    """Where to cloud each mark (or put its marker), by number: the value's printed text when code
    finds it near a big box, else the box cut down to `MAX_CLOUD_PT` (with the whole box to
    outline). Clouds are snapped first; the search stops after `_SNAP_BUDGET_S` seconds."""
    budget = _Budget(_SNAP_BUDGET_S)
    out: dict[tuple[int, int], tuple[Box, Box | None]] = {}
    for mark in sorted(marks, key=lambda m: (not m.cloud, m.page, m.number)):
        x0, y0, x1, y1 = mark.box
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if max(box[2] - box[0], box[3] - box[1]) > _SNAP_FROM_PT and budget.left():
            found = snapped(_printed(source, mark, box))
            if found is not None:
                out[(mark.number, mark.page)] = (found, None)
                continue
        cut, was_cut = capped(box)
        out[(mark.number, mark.page)] = (cut, box if was_cut else None)
    return out


def _layout(
    size: tuple[float, float],
    placed: list[_Placed],
    ink: InkMap | None,
    first_page: bool,
) -> None:
    """Markers, then notes, for one page's marks (in place)."""
    blocked: list[Box] = []
    if first_page:
        x, y = _STAMP_AT
        blocked.append((x - 4, y - _STAMP_SIZE - 2, x + 440.0, y + 8))
    taken: list[tuple[float, float]] = []
    r = _MARKER_RADIUS
    for item in placed:
        x0, y0, x1, y1 = item.spot
        grown = (x0 - _CLOUD_PAD - 0.4 * r, y0 - _CLOUD_PAD - 0.4 * r, x1, y1)
        item.marker = marker_spot(grown, r, taken, size)
        taken.append(item.marker)
        if item.mark.cloud:
            blocked.append(
                (x0 - _CLOUD_PAD - 3, y0 - _CLOUD_PAD - 3, x1 + _CLOUD_PAD + 3, y1 + _CLOUD_PAD + 3)
            )
        mx, my = item.marker
        blocked.append((mx - r, my - r, mx + r, my + r))
    wanted = [item for item in placed if item.mark.cloud]
    wanted.sort(key=lambda item: (item.mark.kind != "wrong", item.mark.number))
    sizes = []
    for item in wanted:
        lines, tag, inline, width, height = _note_lines(item.mark)
        item.lines, item.tag, item.tag_inline = lines, tag, inline
        sizes.append((item.spot, width, height))
    for item, box in zip(wanted, place_notes(size, sizes, ink, blocked), strict=True):
        item.note = box


def _outlined(area: Box) -> bool:
    """Whether the AI's whole area is outlined round a cut-down cloud: only when it is at most
    twice the cloud's size (an outline round a whole elevation is clutter, not a pointer)."""
    return max(area[2] - area[0], area[3] - area[1]) <= 2 * MAX_CLOUD_PT


def _draw_page(c: canvas.Canvas, height: float, placed: Sequence[_Placed]) -> None:
    """One page's marks, in shown points with y up (`_shown_space`)."""

    def up(y: float) -> float:
        return height - y

    for item in placed:
        colour = _colour(item.mark.kind)
        x0, y0, x1, y1 = item.spot
        if item.area is not None and item.mark.cloud and _outlined(item.area):
            a0, b0, a1, b1 = item.area
            c.saveState()
            c.setStrokeColor(colour)
            c.setStrokeAlpha(0.7)
            c.setLineWidth(0.6)
            c.setDash(3, 3)
            c.rect(a0, up(b1), a1 - a0, b1 - b0, stroke=1, fill=0)
            c.restoreState()
        if item.mark.cloud:
            left, right = x0 - _CLOUD_PAD, x1 + _CLOUD_PAD
            low, high = up(y1) - _CLOUD_PAD, up(y0) + _CLOUD_PAD
            radius = max(2.5, min(5.0, min(right - left, high - low) / 4))
            c.setStrokeColor(colour)
            c.setLineWidth(1.2)
            _cloud(c, left, low, right, high, radius)
        if item.note is not None:
            n0, m0, n1, m1 = item.note
            # The leader: from the cloud's edge nearest the note to the note's nearest edge.
            cx = min(max((n0 + n1) / 2, x0 - _CLOUD_PAD), x1 + _CLOUD_PAD)
            cy = min(max((m0 + m1) / 2, y0 - _CLOUD_PAD), y1 + _CLOUD_PAD)
            ex, ey = min(max(cx, n0), n1), min(max(cy, m0), m1)
            c.setStrokeColor(colour)
            c.setLineWidth(0.8)
            c.line(cx, up(cy), ex, up(ey))
            c.setFillColor(white)
            c.setStrokeColor(_GREY)
            c.setLineWidth(0.5)
            c.rect(n0, up(m1), n1 - n0, m1 - m0, stroke=1, fill=1)
            c.setFillColor(colour)
            c.rect(n0, up(m1), _NOTE_BAR, m1 - m0, stroke=0, fill=1)
            prefix = _prefix(item.mark)
            lead = _measure(prefix + "  ", _NOTE_BOLD)
            tx = n0 + _NOTE_BAR + _NOTE_PAD
            ty = m0 + _NOTE_PAD + _NOTE_SIZE * 0.8
            c.setFont(_NOTE_BOLD, _NOTE_SIZE)
            c.drawString(tx, up(ty), prefix)
            c.setFillColor(_DARK)
            c.setFont(_NOTE_FONT, _NOTE_SIZE)
            for n, line in enumerate(item.lines):
                c.drawString(tx + lead, up(ty + n * _NOTE_LEADING), line)
            if item.tag:
                # The trust words, small and grey, after the note's last words or on their own line.
                last = len(item.lines) - 1
                if item.tag_inline and item.lines:
                    x = tx + lead + _measure(item.lines[last] + "  ")
                    row = last
                else:
                    x, row = tx + lead, len(item.lines)
                c.setFillColor(_GREY)
                c.setFont(_NOTE_FONT, _TAG_SIZE)
                c.drawString(x, up(ty + row * _NOTE_LEADING), item.tag)
    # The numbered markers last, on top of the clouds and leaders.
    for item in placed:
        mx, my = item.marker
        c.setFillColor(_colour(item.mark.kind))
        c.setStrokeColor(white)
        c.setLineWidth(0.8)
        c.circle(mx, up(my), _MARKER_RADIUS, stroke=1, fill=1)
        label = str(item.mark.number)
        size = 7.5 if len(label) < 3 else 6.0
        c.setFillColor(white)
        c.setFont(_NOTE_BOLD, size)
        c.drawCentredString(mx, up(my) - size * 0.35, label)


def _stamp(c: canvas.Canvas, height: float, title: str = MARKUP_TITLE) -> None:
    x, y = _STAMP_AT
    c.setFont(_NOTE_BOLD, _STAMP_SIZE)
    width = c.stringWidth(title, _NOTE_BOLD, _STAMP_SIZE)
    c.setFillColor(white)
    c.rect(x - 3, height - y - 5, width + 6, _STAMP_SIZE + 6, stroke=0, fill=1)
    c.setFillColor(_RED)
    c.drawString(x, height - y, title)


def _overlay(pdf: pikepdf.Pdf, page: Any, draw: Any) -> None:
    """Draw on `page` with `draw(canvas, shown_height)`, on top of its own content."""
    _llx, _lly, urx, ury = (float(v) for v in page.mediabox)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(max(urx, 1.0), max(ury, 1.0)), invariant=1)
    c.saveState()
    _shown_space(c, page)
    draw(c, _shown_size(page)[1])
    c.restoreState()
    c.save()
    with pikepdf.open(io.BytesIO(buffer.getvalue())) as drawn:
        form = pdf.copy_foreign(drawn.pages[0].as_form_xobject())
    name = page.add_resource(form, pikepdf.Name.XObject, name=pikepdf.Name(_OVERLAY_NAME))
    page.contents_add(b"q\n", prepend=True)
    page.contents_add(b"Q\nq " + name.unparse() + b" Do Q\n")


def _thumbnails(data: bytes, wanted: Mapping[int, list[tuple[int, Box]]]) -> dict[int, bytes]:
    """A small JPEG of each drawn mark's spot (by number), cut from the marked pages."""
    out: dict[int, bytes] = {}
    aspect = _THUMB_W / _THUMB_H
    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(data)
        try:
            for page_number, items in wanted.items():
                page = document[page_number - 1]
                width, height = page.get_size()
                for number, (x0, y0, x1, y1) in items:
                    w = max(x1 - x0 + 70.0, 200.0)
                    h = max(y1 - y0 + 50.0, 128.0)
                    w, h = max(w, h * aspect), max(h, w / aspect)
                    w, h = min(w, width), min(h, height)
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    left = min(max(cx - w / 2, 0.0), width - w)
                    top = min(max(cy - h / 2, 0.0), height - h)
                    scale = _THUMB_W * _THUMB_PX_PER_PT / w
                    bitmap = page.render(
                        scale=scale,
                        crop=(left, height - (top + h), width - (left + w), top),
                        may_draw_forms=True,
                    )
                    picture = bitmap.to_pil().convert("RGB")
                    buffer = io.BytesIO()
                    picture.save(buffer, format="JPEG", quality=72, optimize=True)
                    out[number] = buffer.getvalue()
        finally:
            document.close()
    return out


def _notes_pages(
    size: tuple[float, float],
    title: str,
    front: Sequence[str],
    sections: Sequence[Section],
    pictures: Mapping[int, bytes],
) -> bytes:
    """The notes pages: the title on each, the front lines (counts, legend), then each section's
    entries, grouped by page where asked, with a small picture of each drawn spot."""
    width, height = size
    body = ParagraphStyle("body", fontName=_NOTE_FONT, fontSize=9, leading=11.5, textColor=_DARK)
    lead = ParagraphStyle("lead", parent=body, fontName=_NOTE_BOLD, fontSize=10, leading=13)
    section = ParagraphStyle(
        "section",
        parent=body,
        fontName=_NOTE_BOLD,
        fontSize=12.5,
        leading=16,
        spaceBefore=8,
        keepWithNext=1,
    )
    page_head = ParagraphStyle(
        "page",
        parent=body,
        fontName=_NOTE_BOLD,
        fontSize=10,
        leading=13,
        spaceBefore=6,
        textColor=Color(0.3, 0.3, 0.3),
    )
    margin = 36.0
    columns = 2 if width > 900 else 1
    gutter = 24.0
    column = (width - 2 * margin - (columns - 1) * gutter) / columns
    frames = [
        Frame(
            margin + n * (column + gutter),
            margin,
            column,
            height - 2 * margin - 22,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        for n in range(columns)
    ]

    def header(c: canvas.Canvas, document: Any) -> None:
        c.saveState()
        c.setFillColor(_RED)
        c.setFont(_NOTE_BOLD, 14)
        c.drawString(margin, height - margin - 6, title)
        c.setFillColor(_GREY)
        c.setFont(_NOTE_FONT, 8)
        c.drawRightString(width - margin, margin - 18, f"Notes page {document.page}")
        c.restoreState()

    def entry(item: Entry) -> list[Any]:
        colour = _colour(item.kind) if item.kind else Color(0.25, 0.45, 0.25)
        hex_colour = colour.hexval().replace("0x", "#")
        head = (
            f'<font color="{hex_colour}"><b>{escape(_sign(item.kind))} {item.number}</b></font>'
            f"&nbsp;&nbsp;<b>{escape(str(item.finding.get('item')))}</b>"
        )
        if item.finding.get("vs_page"):
            head += (
                f'&nbsp;&nbsp;<font color="#666666">architect page {item.finding["vs_page"]}</font>'
            )
        text = [Paragraph(head, body)]
        text += [Paragraph(escape(line), body) for line in item.lines if line]
        picture = pictures.get(item.number) if item.picture else None
        if picture is None:
            return [*text, Spacer(1, 5)]
        image = Image(io.BytesIO(picture), width=_THUMB_W, height=_THUMB_H)
        grid = Table(
            [[image, text]], colWidths=[_THUMB_W + 8, column - _THUMB_W - 8], hAlign="LEFT"
        )
        grid.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LINEBEFORE", (0, 0), (0, 0), _NOTE_BAR, colour),
                ]
            )
        )
        return [grid]

    story: list[Any] = []
    heading: Any = None
    for n, line in enumerate(front):
        story.append(Paragraph(escape(line), lead if n == 0 else body))
        story.append(Spacer(1, 3))
    for part in sections:
        story.append(Paragraph(escape(f"{part.heading} ({len(part.entries)})"), section))
        if not part.entries:
            story.append(Paragraph("None.", body))
            continue
        current: object = object()
        for item in part.entries:
            if part.by_page and item.finding.get("page") != current:
                current = item.finding.get("page")
                heading = Paragraph(escape(f"Page {current}"), page_head)
            # A page's heading never ends a column: it goes with its first entry.
            story.append(KeepTogether([*([heading] if heading is not None else []), *entry(item)]))
            heading = None
    buffer = io.BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=(width, height),
        title=title,
        invariant=1,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )
    document.addPageTemplates([PageTemplate(id="notes", frames=frames, onPage=header)])
    document.build(story)
    return buffer.getvalue()


def draw_markup(
    source: Path,
    marks: Sequence[Mark],
    out: Path,
    *,
    title: str,
    front: Sequence[str],
    sections: Sequence[Section],
    base: Base | None = None,
) -> None:
    """The cloud drawing: `base` (a `clone_pdf(source)` copy, used once; else made here) with each
    mark drawn on its page, `title` stamped on page 1, then the notes pages (`front` lines, the
    note on a clean copy, and the `sections`). Marks on a page the file does not have are left
    out (the caller lists them)."""
    base = base if base is not None else clone_pdf(source)
    pdf = base.pdf
    try:
        pages = len(pdf.pages)
        by_page: dict[int, list[Mark]] = {}
        for mark in marks:
            if 1 <= mark.page <= pages:
                by_page.setdefault(mark.page, []).append(mark)
        laid: dict[int, list[_Placed]] = {}
        spots = _spots(source, [m for found in by_page.values() for m in found])
        if by_page:
            clean = io.BytesIO()
            pdf.save(clean)
            with _PDFIUM_LOCK:
                document = pdfium.PdfDocument(clean.getvalue())
            try:
                for number in sorted(by_page):
                    placed = [_Placed(m, *spots[(m.number, m.page)]) for m in by_page[number]]
                    page = pdf.pages[number - 1]
                    _layout(_shown_size(page), placed, _ink(document, number - 1), number == 1)
                    laid[number] = placed
            finally:
                with _PDFIUM_LOCK:
                    document.close()
        for number in sorted(set(laid) | {1}):
            page = pdf.pages[number - 1]
            placed = laid.get(number, [])

            def draw(
                c: canvas.Canvas, height: float, placed: Any = placed, n: int = number
            ) -> None:
                _draw_page(c, height, placed)
                if n == 1:
                    _stamp(c, height, title)

            _overlay(pdf, page, draw)
        marked = io.BytesIO()
        pdf.save(marked, deterministic_id=True)
        # One picture per item on the notes pages: its own page's spot, never an also-on copy.
        wanted = {
            number: [(item.mark.number, item.spot) for item in placed if not item.mark.extra]
            for number, placed in laid.items()
        }
        pictures = _thumbnails(marked.getvalue(), wanted) if wanted else {}
        first = pdf.pages[0]
        lines = list(front)
        if base.dropped:
            lines.insert(1, f"{CLEAN_WORDS} ({base.dropped} marks left out).")
        notes = _notes_pages(_shown_size(first), title, lines, sections, pictures)
        with (
            pikepdf.open(io.BytesIO(marked.getvalue())) as final,
            pikepdf.open(io.BytesIO(notes)) as listed,
        ):
            final.pages.extend(listed.pages)
            out.parent.mkdir(parents=True, exist_ok=True)
            final.save(out, deterministic_id=True)
    finally:
        pdf.close()


def _where(finding: Mapping[str, Any], where: str) -> str:
    level = finding.get("trust_level")
    checked = TRUST_WORDS.get(str(level), str(level)) if level else ""
    checked = checked or ("verified" if finding.get("verified") else "")
    return f"{where} {CLOUD_WORDS[cloud_kind(finding)]}{', ' + checked if checked else ''}."


def entry_lines(finding: Mapping[str, Any]) -> list[str]:
    """A finding's lines on the notes pages: vendor and what it needs (or the architect's), why,
    and a "Your decision" item's decision help."""
    vendor, architect = finding.get("vendor"), finding.get("architect")
    if cloud_kind(finding) == "wrong":
        needs = needs_text(finding)
        line = f"Vendor: {vendor}   Needs: {needs}"
        if needs != str(architect):
            line += f"   (architect {architect})"
    else:
        line = f"Vendor: {vendor}   Architect: {architect}"
    lines = [line]
    if finding.get("why"):
        lines.append(f"Why: {finding['why']}")
    # A "Your decision" item's reasoning (#1545): what it measures, our estimate, likely, confirm.
    lines.extend(decision_help_lines(finding))
    lines += settled_lines(finding)
    return lines


def settled_lines(finding: Mapping[str, Any]) -> list[str]:
    """What code's resolver (#1554) added: how it settled the item, the question left for a
    person, and the other pages of the same item."""
    lines: list[str] = []
    settled = finding.get("settled")
    if isinstance(settled, Mapping) and settled.get("how"):
        lines.append(f"Settled by GV Review: {settled['how']}")
    if finding.get("status") == "check" and finding.get("question"):
        lines.append(f"Question: {finding['question']}")
    for other in finding.get("also_on") or []:
        if not isinstance(other, Mapping):
            continue
        where = f"p.{other.get('page')}"
        if other.get("round"):
            where += f" (round {other.get('round')})"
        lines.append(f"Also on {where}: vendor {other.get('vendor')}")
    return lines


def settled_entry_lines(entry: Mapping[str, Any]) -> list[str]:
    """One settled-automatically item's lines on the notes pages and in the report."""
    words = SETTLED_WORDS.get(str(entry.get("outcome")), str(entry.get("outcome")))
    return [
        f"Vendor: {entry.get('vendor')}   Architect: {entry.get('architect')}",
        f"{words}. Settled by GV Review: {entry.get('how')}",
    ]


#: How a settled-automatically item ended (#1554), in the reviewer's words.
SETTLED_WORDS: Final[Mapping[str, str]] = {
    "wrong": "Wrong",
    "right": "Right",
    "merged": "Same item as another",
    "not_a_difference": "Not a difference",
}


def counts_line(findings: Sequence[Mapping[str, Any]], *, clouded_decisions: bool) -> str:
    counts = shown_counts(findings)
    decision = "amber clouds" if clouded_decisions else "amber numbers, not clouded"
    tags = [trust_tag(finding) for finding in findings if cloud_kind(finding) == "wrong"]
    unconfirmed = ", ".join(
        f"{tags.count(words)} {words}" for words in dict.fromkeys(t for t in tags if t)
    )
    unseen = f", {counts['not_compared']} not compared" if counts["not_compared"] else ""
    return (
        f"{len(findings)} items: {counts['wrong']} wrong (red clouds"
        f"{'; ' + unconfirmed if unconfirmed else ''}), "
        f"{counts['decision']} not compared ({decision}), "
        f"{counts['right']} right{unseen} (listed, not marked)."
    )


def also_marks(
    number: int,
    finding: Mapping[str, Any],
    kind: str,
    cloud: bool,
    source: Path,
    pages: int | None = None,
) -> list[Mark]:
    """The same item on its other pages (#1554, `also_on`): a mark at each listed spot on this
    file, with that page's own vendor value, so a Wrong or agreed item is clouded on every page it
    was found on. Listed once on the notes pages (the item's own entry)."""
    out: list[Mark] = []
    for other in finding.get("also_on") or []:
        if not isinstance(other, Mapping):
            continue
        box = other.get("vendor_box") or other.get("arch_box")
        if not isinstance(box, Mapping) or box.get("file") not in (None, source.name):
            continue
        try:
            page = int(box["page"])
            coords = (float(box["x0"]), float(box["y0"]), float(box["x1"]), float(box["y1"]))
        except (KeyError, TypeError, ValueError):
            continue
        if page < 1 or (pages is not None and page > pages) or page == finding.get("page"):
            continue
        value = str(other.get("vendor") or finding.get("vendor") or "")
        shown = {**finding, "page": page, "vendor": other.get("vendor") or finding.get("vendor")}
        out.append(Mark(number, shown, page, coords, kind, cloud, value, extra=True))
    return out


def markup_pdf(
    source: Path,
    findings: Sequence[tuple[int, Mapping[str, Any], int | None, Box | None, str]],
    out: Path,
    writer: Base | None = None,
    *,
    cloud_decisions: bool = False,
    settled: Sequence[Mapping[str, Any]] = (),
) -> None:
    """The cloud drawing of a chat answer's findings: a tight red cloud, a numbered marker and a
    note at each Wrong item; a numbered amber marker at each item needing your decision (with
    `cloud_decisions`, an amber cloud and note as well); the draft stamp on page 1; and notes pages
    listing every finding by page. `findings`: (number, finding, page, box, where) with
    `page`/`box` `None` when the finding cannot be placed on this file (`where` says why).
    `writer`: a fresh `clone_pdf(source)` made earlier (used once), else the copy is made here."""
    base = writer if writer is not None else clone_pdf(source)
    pages = len(base.pdf.pages)
    marks: list[Mark] = []
    drawn: list[Entry] = []
    elsewhere: list[Entry] = []
    right: list[Entry] = []
    unseen: list[Entry] = []
    for number, finding, page_number, box, where in findings:
        kind = cloud_kind(finding)
        if kind is None:
            # Not seen on both sides is not right (`mark_compared`): listed apart, as on screen.
            compared = finding.get("compared") is not False
            words = "Right: not clouded." if compared else "Not compared: not clouded."
            (right if compared else unseen).append(
                Entry(number, finding, None, [entry_lines(finding)[0], words])
            )
            continue
        if page_number is not None and box is not None and not 1 <= page_number <= pages:
            where, page_number = f"Not marked: the file has no page {page_number}.", None
        lines = [*entry_lines(finding), _where(finding, where)]
        if page_number is None or box is None:
            elsewhere.append(Entry(number, finding, kind, lines))
            continue
        cloud = kind == "wrong" or cloud_decisions
        marks.append(Mark(number, finding, page_number, box, kind, cloud, _box_value(finding)))
        marks += also_marks(number, finding, kind, cloud, source, pages)
        drawn.append(Entry(number, finding, kind, lines, picture=True))
    drawn.sort(key=lambda e: (int(e.finding.get("page") or 0), e.number))
    right.sort(key=lambda e: (int(e.finding.get("page") or 0), e.number))
    front = [
        counts_line([f for _n, f, *_rest in findings], clouded_decisions=cloud_decisions),
        LEGEND_CLOUDED if cloud_decisions else LEGEND,
    ]
    biggest = biggest_line([(n, f) for n, f, *_rest in findings])
    if biggest is not None:
        front.append(biggest)
    # The decision items code settled itself (#1554): the Wrong and Right ones are above with their
    # findings; the ones folded into another item or found not a difference are listed here.
    folded = [
        Entry(len(findings) + n, entry, None, settled_entry_lines(entry))
        for n, entry in enumerate(
            (e for e in settled if e.get("outcome") in ("merged", "not_a_difference")), start=1
        )
    ]
    sections = (
        Section("On the drawing", drawn),
        Section("Not on the drawing", elsewhere, by_page=False),
        Section("Right (not marked)", right),
        *((Section("Not compared (not marked)", unseen),) if unseen else ()),
        *((Section("Settled automatically by GV Review (not marked)", folded),) if folded else ()),
    )
    draw_markup(source, marks, out, title=MARKUP_TITLE, front=front, sections=sections, base=base)


# ---- the check report ----------------------------------------------------------------------------

REPORT_TITLE: Final = "Check report: checked automatically by GV Review"
#: Under What's wrong (#1569): how the list is ordered, in plain words.
IMPACT_NOTE: Final = (
    "checked items before AI readings only, then what changes fabrication (overall sizes, "
    "cut-outs, heights, missing parts) and the size of the difference; tags and labels last."
)


def check_report_pdf(
    out: Path,
    summary: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    not_checked: Sequence[str],
    *,
    settled: Sequence[Mapping[str, Any]] = (),
) -> None:
    """The full check's report: a cover (set, products, date, model and effort, counts, coverage,
    trust, and the counts by product), then What's wrong, Your decision, What's right, Settled
    automatically (#1554: the decision items code settled itself, and how) and Not checked. Each
    section is grouped by product, in the product registry's order and words (#1547): a finding's
    category is a product id, or "other"."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import landscape, letter  # type: ignore[import-untyped]
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    body, small = styles["BodyText"], styles["BodyText"].clone("small", fontSize=8, leading=10)

    def p(text: object, style: Any = small) -> Any:
        return Paragraph(escape("" if text is None else str(text)), style)

    def table(
        header: Sequence[str], rows: Sequence[Sequence[object]], widths: Sequence[float]
    ) -> Any:
        data = [[p(h, small) for h in header], *[[p(c) for c in row] for row in rows]]
        grid = Table(data, colWidths=list(widths), repeatRows=1)
        grid.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999999")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return grid

    known = registry()
    rank = {category: n for n, category in enumerate(known.categories())}

    def group(f: Mapping[str, Any]) -> str:
        return known.category_label(str(f.get("category") or ""))

    def proof_cell(f: Mapping[str, Any]) -> str:
        """The item's place on the zero-click drawing (#1581), and the check that proved it."""
        found = proof.proof_of(f)
        words = proof.GROUP_WORDS[found["group"]]
        # A Not-compared item (#1619) says why, and what would settle it.
        settle = f" Needed: {found['settle']}." if found.get("settle") else ""
        detail = found["proof"] or (f"{found['reason']}.{settle}" if found["reason"] else None)
        return f"{words}: {detail}" if detail else words

    def by_product(chosen: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        last = len(rank) - 1  # an unknown category sorts with "other"
        return sorted(chosen, key=lambda f: rank.get(str(f.get("category") or ""), last))

    groups = proof.proof_counts(findings)
    summary = {
        **summary,
        "Cloud drawing": (
            f"{groups['clouded']} wrong clouded (proved by code), {groups['right']} right, "
            f"{groups['not_compared']} not compared (the Not-compared list)"
        ),
        "The rule": proof.RULE_LINE,
    }
    story: list[Any] = [Paragraph(escape(REPORT_TITLE), styles["Title"])]
    for label, value in summary.items():
        story.append(Paragraph(f"<b>{escape(str(label))}:</b> {escape(str(value))}", body))
    number = {id(f): n for n, f in enumerate(findings, start=1)}
    # The cloud drawing's own groups (#1581, #1619): Wrong = clouded (code proved it, or the
    # reviewer agreed), Right, and what could not be compared (with its reason).
    placed = {id(f): proof.proof_group(f) for f in findings}
    wrong = by_product([f for f in findings if placed[id(f)] == "clouded"])
    right = by_product([f for f in findings if placed[id(f)] == "right"])
    unseen = by_product([f for f in findings if placed[id(f)] == "not_compared"])
    per_product: dict[str, list[int]] = {}
    for kinds, chosen in enumerate((wrong, unseen, right)):
        for f in chosen:
            per_product.setdefault(group(f), [0, 0, 0])[kinds] += 1
    if per_product:
        words = "; ".join(
            f"{name}: {w} wrong, {r} right, {c} not compared"
            for name, (w, c, r) in per_product.items()
        )
        summary_count = len(summary) + 1
        story.append(Paragraph(f"<b>By product:</b> {escape(words)}", body))
    else:
        summary_count = len(summary)
    sections: list[tuple[str, list[str], list[list[Any]], list[int]]] = [
        (
            f"Wrong ({len(wrong)})",
            ["AI", "Product", "Page", "Item", "Vendor", "Should be", "Proof", "Trust"],
            [
                [
                    number[id(f)],
                    group(f),
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    proof.should_be(f),
                    proof_cell(f),
                    TRUST_WORDS.get(str(f.get("trust_level")), ""),
                ]
                for f in wrong
            ],
            [24, 58, 30, 128, 92, 132, 196, 56],
        ),
        (
            f"Right ({len(right)})",
            ["AI", "Product", "Page", "Item", "Value", "Matches"],
            [
                [
                    number[id(f)],
                    group(f),
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    f"{f.get('source')}: {f.get('architect')}",
                ]
                for f in right
            ],
            [26, 70, 34, 210, 110, 190],
        ),
    ]
    if unseen:
        sections.append(
            (
                f"Not compared ({len(unseen)})",
                ["AI", "Product", "Page", "Item", "Value", "Why not compared"],
                [
                    [
                        number[id(f)],
                        group(f),
                        f.get("page"),
                        f.get("item"),
                        f.get("vendor"),
                        proof.proof_of(f)["reason"] or "",
                    ]
                    for f in unseen
                ],
                [26, 70, 34, 210, 110, 190],
            )
        )
    if settled:
        sections.append(
            (
                f"Settled automatically by GV Review ({len(settled)})",
                ["Page", "Item", "Now", "How"],
                [
                    [
                        e.get("page"),
                        e.get("item"),
                        SETTLED_WORDS.get(str(e.get("outcome")), str(e.get("outcome"))),
                        e.get("how"),
                    ]
                    for e in settled
                ],
                [34, 200, 90, 396],
            )
        )
    for title, header, rows, widths in sections:
        story += [Spacer(1, 10), Paragraph(escape(title), styles["Heading2"])]
        if rows and title.startswith("What's wrong"):
            story.append(p(f"{BIGGEST_FIRST}: {IMPACT_NOTE}", small))
        story.append(table(header, rows, widths) if rows else p("None.", body))
    story += [Spacer(1, 10), Paragraph("Not checked", styles["Heading2"])]
    story += [p(line, body) for line in not_checked] or [p("Nothing left out.", body)]
    story.insert(summary_count + 1, PageBreak())
    out.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(out),
        pagesize=landscape(letter),
        title=REPORT_TITLE,
        invariant=1,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    document.build(story)
