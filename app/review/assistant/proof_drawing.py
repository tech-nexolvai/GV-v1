"""The zero-click red/green proof drawing (#1581), drawn by code at the end of every full check.

What the vendor gets, with no human step (the rule is `proof.py`):

1. **Cover**: the counts (Wrong, clouded; Right; Nothing to compare), the rule in one line, the stamp "Checked automatically by GV Review: <date>" ("… and reviewed by <name>" once a
   reviewer decided anything), every assumption code made about the page map, and the list of the
   clouded items.
2. **The vendor's sheets**: a tight red cloud round each wrong value code proved, a numbered red
   marker, and a short note in clear space: the item, "Vendor: 46"" in red and "Should be: 37"
   (architect 3'-1", page 28)" in green. The tight-cloud and clear-space layout is the cloud
   drawing's own (`chat_drawing`, `cloud_layout`).
3. **Proof pages**: one entry per clouded item, in the clouded order (biggest first): the
   architect's crop with the governing value boxed in green beside the vendor's crop with the
   wrong value boxed in red, one line naming the check that proved it, and the pages. Crops are cut
   from the hi-res page cache (#1557); a page already drawn is never drawn again.
4. **Notes pages**: a short green list of what is right; what the reviewer marked not a problem,
   and what code settled by itself. What could not be compared (#1619, the architect-first rules
   R1-R10, `classification.py`) is NOT on this drawing: it is the separate "Not compared" PDF
   (`not_compared_pdf`), grouped by reason, each row with why and what would settle it.

Page labels: the cover is "Cover", the vendor's sheets keep their own numbers (1, 2, …), then
"Proof 1…" and "Notes 1…", so "page 12" in a note is page 12 in any PDF viewer.

**Loaded by name, never imported** (`chat_findings.make_proof_file`): `app/api/` must not reach a
PDF renderer by import (`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from xml.sax.saxutils import escape

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image as PILImage
from PIL import ImageDraw
from reportlab.lib.colors import Color, white  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
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

from app.review.assistant.chat_drawing import (
    _CLOUD_PAD,
    _DARK,
    _GREY,
    _MARKER_RADIUS,
    _NOTE_BAR,
    _NOTE_BOLD,
    _NOTE_FONT,
    _NOTE_LEADING,
    _NOTE_PAD,
    _NOTE_SIZE,
    _PDFIUM_LOCK,
    _RED,
    _STAMP_AT,
    Base,
    Mark,
    _cloud,
    _ink,
    _measure,
    _outlined,
    _overlay,
    _padded,
    _printed,
    _shown_size,
    _spots,
    also_marks,
    clone_pdf,
    crop_png,
    settled_entry_lines,
)
from app.review.assistant.cloud_layout import NOTE_WIDTH_PT, InkMap, marker_spot, place_notes
from app.review.assistant.proof import (
    GROUP_WORDS,
    LEFT_CAPTIONS,
    RULE_LINE,
    governs,
    proof_counts,
    proof_group,
    proof_line,
    should_be,
    should_be_value,
    vendor_own,
    vendor_value,
)

__all__ = [
    "GREEN",
    "LEGEND_LINES",
    "NOT_COMPARED_TITLE",
    "RED",
    "STAMP_WORDS",
    "ProofCounts",
    "contrast",
    "not_compared_pdf",
    "proof_markup_pdf",
    "stamp_line",
]

_log = logging.getLogger(__name__)

type Box = tuple[float, float, float, float]
type Placed = tuple[int, Mapping[str, Any], int | None, Box | None, str]

#: The vendor's wrong value: the cloud drawing's red (contrast 6.0:1 on white).
RED: Final = _RED
#: What it should be: a dark, print-safe green (contrast 5.6:1 on white; it stays a dark grey,
#: never washed out, in greyscale print). `contrast` measures both; a test keeps them >= 4.5:1.
GREEN: Final = Color(0.0, 0.47, 0.2)
#: Grey for the lists code could not prove.
_LIST_GREY: Final = Color(0.4, 0.4, 0.4)
STAMP_WORDS: Final = "Checked automatically by GV Review"
LEGEND_LINES: Final = (
    "Red cloud and red words: the vendor's value that is wrong.",
    "Green words: what it should be, and which drawing or rule says so.",
    "What could not be compared is listed apart, with the reason, in the Not-compared list.",
)
#: The separate list's title (and its file name's end, `chat_findings`).
NOT_COMPARED_TITLE: Final = "Not compared"
#: The proof crops: at least this much of the drawing round the value (points), at most this big
#: on the page (points per crop).
_CONTEXT_W: Final = 220.0
_CONTEXT_H: Final = 140.0
_CROP_MAX_W: Final = 0.46
_CROP_MAX_H: Final = 210.0
#: The search for printed values on the architect's side stops after this many seconds.
_SNAP_BUDGET_S: Final = 20.0
#: The key a round-2 mark's finding carries its `round_check.RoundItem` under (#1603).
_ROUND_KEY: Final = "_round_two"
#: The small heading on each sheet with clouds.
_SHEET_STAMP_SIZE: Final = 9.0
_NOTE_ROWS: Final = 3


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def contrast(colour: Color, against: Color = white) -> float:
    """The WCAG contrast ratio of two colours (1 to 21)."""

    def luminance(c: Color) -> float:
        return 0.2126 * _linear(c.red) + 0.7152 * _linear(c.green) + 0.0722 * _linear(c.blue)

    light, dark = sorted((luminance(colour), luminance(against)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def stamp_line(date: str, reviewers: Sequence[str] = ()) -> str:
    """ "Checked automatically by GV Review: 2026-10-18", "… and reviewed by Anant" once a
    reviewer decided anything."""
    line = f"{STAMP_WORDS}: {date}"
    names = [name for name in dict.fromkeys(reviewers) if name]
    return f"{line}, and reviewed by {', '.join(names)}" if names else line


@dataclass(frozen=True, slots=True)
class ProofCounts:
    clouded: int
    right: int
    not_compared: int
    dismissed: int


# ---- the note on the sheet ------------------------------------------------------------------------

type _Run = tuple[str, str, Color]
"""One piece of a note's line: (text, font, colour)."""


def _fit(runs: Sequence[_Run], room: float) -> list[_Run]:
    """The line's pieces cut to `room` points, the last word cut ending with "…"."""
    out: list[_Run] = []
    used = 0.0
    for text, font, colour in runs:
        width = _measure(text, font)
        if used + width <= room:
            out.append((text, font, colour))
            used += width
            continue
        cut = text
        while cut and used + _measure(cut + "…", font) > room:
            cut = cut[:-1]
        out.append((cut.rstrip() + "…", font, colour))
        break
    return out


def _round(mark: Mark) -> Any:
    """The round-2 status a mark carries (`round_check.RoundItem`), or `None`."""
    return mark.finding.get(_ROUND_KEY)


def _later(mark: Mark) -> bool:
    """A round-1 item's mark on its round-2 page (#1603)."""
    rounds = _round(mark)
    return rounds is not None and rounds.status in ("fixed", "still_wrong")


def _grown(box: Box, pad: float = 2.0) -> Box:
    x0, y0, x1, y1 = box
    return (min(x0, x1) - pad, min(y0, y1) - pad, max(x0, x1) + pad, max(y0, y1) + pad)


def note_lines(mark: Mark) -> list[list[_Run]]:
    """A clouded item's note: its item, "Vendor: 46"" in red, "Should be: 37" (architect …)" in
    green, one line each (cut with "…"; the proof page has the whole words). On round 2 (#1603):
    "Fixed in round 2: was 46", now 37"" in green, "Still wrong in round 2: now 46"" in red, or
    the item headed "NEW" in red."""
    finding = mark.finding
    vendor = vendor_value(finding)
    value = should_be_value(finding)
    source = governs(finding)
    room = NOTE_WIDTH_PT - 2 * _NOTE_PAD - _NOTE_BAR - _prefix_width(mark)
    item = str(finding.get("item") or "Item")
    rounds = _round(mark)
    head: list[_Run] = [(item, _NOTE_BOLD, _DARK)]
    if rounds is not None and rounds.status == "fixed":
        return [
            _fit(head, room),
            _fit(
                [
                    ("Fixed in round 2: ", _NOTE_FONT, GREEN),
                    ("was ", _NOTE_FONT, GREEN),
                    (rounds.was, _NOTE_BOLD, GREEN),
                    (", now ", _NOTE_FONT, GREEN),
                    (str(rounds.now), _NOTE_BOLD, GREEN),
                ],
                room,
            ),
        ]
    if rounds is not None and rounds.status == "still_wrong":
        middle = [
            ("Still wrong in round 2: now ", _NOTE_FONT, RED),
            (str(rounds.now), _NOTE_BOLD, RED),
        ]
        if rounds.now != rounds.was:
            middle.append((f" (was {rounds.was})", _NOTE_FONT, RED))
        return [
            _fit(head, room),
            _fit(middle, room),
            _fit(
                [("Should be: ", _NOTE_FONT, GREEN), (value, _NOTE_BOLD, GREEN)]
                + ([(f" ({source})", _NOTE_FONT, GREEN)] if source else []),
                room,
            ),
        ]
    if rounds is not None and rounds.status == "new":
        head = [("NEW ", _NOTE_BOLD, RED), (item, _NOTE_BOLD, _DARK)]
    return [
        _fit(head, room),
        _fit([("Vendor: ", _NOTE_FONT, RED), (vendor, _NOTE_BOLD, RED)], room),
        _fit(
            [("Should be: ", _NOTE_FONT, GREEN), (value, _NOTE_BOLD, GREEN)]
            + ([(f" ({source})", _NOTE_FONT, GREEN)] if source else []),
            room,
        ),
    ]


def _prefix_width(mark: Mark) -> float:
    return _measure(f"{mark.number}  ", _NOTE_BOLD)


def _note_size(mark: Mark, lines: Sequence[Sequence[_Run]]) -> tuple[float, float]:
    widest = max((sum(_measure(t, f) for t, f, _c in line) for line in lines), default=0.0)
    width = _prefix_width(mark) + widest + 2 * _NOTE_PAD + _NOTE_BAR
    height = len(lines) * _NOTE_LEADING + 2 * _NOTE_PAD - (_NOTE_LEADING - _NOTE_SIZE)
    return width, height


@dataclass(slots=True)
class _Placed:
    mark: Mark
    spot: Box
    area: Box | None
    marker: tuple[float, float] = (0.0, 0.0)
    note: Box | None = None
    lines: list[list[_Run]] = field(default_factory=list)


def _sheet_stamp_box(count: int) -> Box:
    x, y = _STAMP_AT
    width = _measure(_sheet_stamp_words(count), _NOTE_BOLD, _SHEET_STAMP_SIZE)
    return (x - 4, y - _SHEET_STAMP_SIZE - 3, x + width + 8, y + 5)


def _sheet_stamp_words(count: int) -> str:
    items = "1 correction" if count == 1 else f"{count} corrections"
    return f"GV Review: {items} on this page (red: the vendor's value; green: what it should be)"


def _layout(size: tuple[float, float], placed: list[_Placed], ink: InkMap | None) -> None:
    """Markers, then notes, for one sheet's clouds (in place)."""
    count = len({item.mark.number for item in placed})
    blocked: list[Box] = [_sheet_stamp_box(count)]
    taken: list[tuple[float, float]] = []
    r = _MARKER_RADIUS
    for item in placed:
        x0, y0, x1, y1 = item.spot
        grown = (x0 - _CLOUD_PAD - 0.4 * r, y0 - _CLOUD_PAD - 0.4 * r, x1, y1)
        item.marker = marker_spot(grown, r, taken, size)
        taken.append(item.marker)
        blocked.append(
            (x0 - _CLOUD_PAD - 3, y0 - _CLOUD_PAD - 3, x1 + _CLOUD_PAD + 3, y1 + _CLOUD_PAD + 3)
        )
        mx, my = item.marker
        blocked.append((mx - r, my - r, mx + r, my + r))
    ordered = sorted(placed, key=lambda item: item.mark.number)
    sizes = []
    for item in ordered:
        item.lines = note_lines(item.mark)
        width, height = _note_size(item.mark, item.lines)
        sizes.append((item.spot, width, height))
    for item, box in zip(ordered, place_notes(size, sizes, ink, blocked), strict=True):
        item.note = box


def _draw_sheet(c: canvas.Canvas, height: float, placed: Sequence[_Placed]) -> None:
    """One sheet's clouds, leaders, notes and markers, in shown points with y up."""

    def up(y: float) -> float:
        return height - y

    for item in placed:
        x0, y0, x1, y1 = item.spot
        fixed = item.mark.kind == "fixed"
        colour = GREEN if fixed else RED
        if fixed:
            c.setStrokeColor(GREEN)
            c.setLineWidth(1.2)
            c.rect(x0 - 2, up(y1) - 2, x1 - x0 + 4, y1 - y0 + 4, stroke=1, fill=0)
        elif item.area is not None and _outlined(item.area):
            a0, b0, a1, b1 = item.area
            c.saveState()
            c.setStrokeColor(RED)
            c.setStrokeAlpha(0.7)
            c.setLineWidth(0.6)
            c.setDash(3, 3)
            c.rect(a0, up(b1), a1 - a0, b1 - b0, stroke=1, fill=0)
            c.restoreState()
        left, right = x0 - _CLOUD_PAD, x1 + _CLOUD_PAD
        low, high = up(y1) - _CLOUD_PAD, up(y0) + _CLOUD_PAD
        radius = max(2.5, min(5.0, min(right - left, high - low) / 4))
        if not fixed:
            c.setStrokeColor(RED)
            c.setLineWidth(1.4)
            _cloud(c, left, low, right, high, radius)
        if item.note is None:
            continue
        n0, m0, n1, m1 = item.note
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
        prefix = f"{item.mark.number}"
        lead = _prefix_width(item.mark)
        tx = n0 + _NOTE_BAR + _NOTE_PAD
        ty = m0 + _NOTE_PAD + _NOTE_SIZE * 0.8
        c.setFont(_NOTE_BOLD, _NOTE_SIZE)
        c.drawString(tx, up(ty), prefix)
        for row, line in enumerate(item.lines):
            x = tx + lead
            for text, font, ink in line:
                c.setFillColor(ink)
                c.setFont(font, _NOTE_SIZE)
                c.drawString(x, up(ty + row * _NOTE_LEADING), text)
                x += _measure(text, font)
    for item in placed:
        mx, my = item.marker
        c.setFillColor(GREEN if item.mark.kind == "fixed" else RED)
        c.setStrokeColor(white)
        c.setLineWidth(0.8)
        c.circle(mx, up(my), _MARKER_RADIUS, stroke=1, fill=1)
        if item.mark.kind == "fixed":
            # A check mark: fixed in round 2.
            r = _MARKER_RADIUS
            c.setStrokeColor(white)
            c.setLineWidth(1.6)
            path = c.beginPath()
            path.moveTo(mx - 0.45 * r, up(my))
            path.lineTo(mx - 0.1 * r, up(my) - 0.38 * r)
            path.lineTo(mx + 0.5 * r, up(my) + 0.42 * r)
            c.drawPath(path, stroke=1, fill=0)
            continue
        label = str(item.mark.number)
        size = 7.5 if len(label) < 3 else 6.0
        c.setFillColor(white)
        c.setFont(_NOTE_BOLD, size)
        c.drawCentredString(mx, up(my) - size * 0.35, label)
    numbers = {item.mark.number for item in placed if item.mark.kind != "fixed"}
    if numbers:
        _sheet_stamp(c, height, len(numbers))


def _sheet_stamp(c: canvas.Canvas, height: float, count: int) -> None:
    x, y = _STAMP_AT
    x0, y0, x1, y1 = _sheet_stamp_box(count)
    c.setFillColor(white)
    c.setStrokeColor(_GREY)
    c.setLineWidth(0.5)
    c.rect(x0, height - y1, x1 - x0, y1 - y0, stroke=1, fill=1)
    head = f"GV Review: {'1 correction' if count == 1 else f'{count} corrections'} on this page "
    pieces: list[tuple[str, Color]] = [
        (head, _DARK),
        ("(", _DARK),
        ("red: the vendor's value", RED),
        ("; ", _DARK),
        ("green: what it should be", GREEN),
        (")", _DARK),
    ]
    cursor = x
    c.setFont(_NOTE_BOLD, _SHEET_STAMP_SIZE)
    for text, colour in pieces:
        c.setFillColor(colour)
        c.drawString(cursor, height - y, text)
        cursor += c.stringWidth(text, _NOTE_BOLD, _SHEET_STAMP_SIZE)


# ---- the proof crops ------------------------------------------------------------------------------


class _Sizes:
    """Each file's page sizes (shown points), read once."""

    def __init__(self) -> None:
        self._known: dict[Path, list[tuple[float, float]]] = {}

    def of(self, pdf: Path, page: int) -> tuple[float, float] | None:
        if pdf not in self._known:
            drawing = import_module("app.review.assistant.chat_drawing")
            try:
                self._known[pdf] = drawing.page_sizes(pdf)
            except Exception as failed:  # noqa: BLE001 - no crop then, never a failed drawing
                _log.warning("proof drawing page sizes failed: %s", type(failed).__name__)
                self._known[pdf] = []
        sizes = self._known[pdf]
        return sizes[page - 1] if 1 <= page <= len(sizes) else None


def _context(spot: Box, size: tuple[float, float]) -> Box:
    """The spot grown to at least `_CONTEXT_W` x `_CONTEXT_H` round its centre, inside the page."""
    width, height = size
    x0, y0, x1, y1 = spot
    w, h = max(x1 - x0 + 40.0, _CONTEXT_W), max(y1 - y0 + 30.0, _CONTEXT_H)
    w, h = min(w, width), min(h, height)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(cx - w / 2, 0.0), width - w)
    top = min(max(cy - h / 2, 0.0), height - h)
    return (left, top, left + w, top + h)


def boxed_crop(
    pdf: Path,
    page: int,
    spot: Box,
    colour: Color,
    sizes: _Sizes,
    cache: Path | None,
) -> bytes | None:
    """A crop of the drawing round `spot` with `spot` boxed in `colour`, as a PNG: cut from the
    hi-res page cache when there is one (`crop_png`), else drawn once from the PDF."""
    size = sizes.of(pdf, page)
    if size is None:
        return None
    area = _context(spot, size)
    png = crop_png(pdf, page, area, cache=cache)
    if png is None:
        return None
    padded = _padded(area, *size)
    if padded is None:
        return png
    left, top, right, bottom = padded
    picture = PILImage.open(io.BytesIO(png)).convert("RGB")
    sx, sy = picture.width / (right - left), picture.height / (bottom - top)
    x0, y0, x1, y1 = spot
    pad = 3.0
    rect = (
        (x0 - pad - left) * sx,
        (y0 - pad - top) * sy,
        (x1 + pad - left) * sx,
        (y1 + pad - top) * sy,
    )
    draw = ImageDraw.Draw(picture)
    rgb = tuple(round(v * 255) for v in (colour.red, colour.green, colour.blue))
    draw.rectangle(rect, outline=rgb, width=max(3, round(picture.width / 260)))
    out = io.BytesIO()
    picture.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _box_of(raw: object) -> tuple[str | None, int, Box] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        page = int(raw["page"])
        coords = (float(raw["x0"]), float(raw["y0"]), float(raw["x1"]), float(raw["y1"]))
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("file")
    return (None if name is None else str(name)), page, coords


class _Budget:
    def __init__(self, seconds: float) -> None:
        self._until = time.monotonic() + seconds

    def left(self) -> bool:
        return time.monotonic() < self._until


def _tight(pdf: Path, page: int, box: Box, value: str, budget: _Budget) -> Box:
    """The value's printed text inside or near a big box (the cloud drawing's own search), else
    the box."""
    if max(box[2] - box[0], box[3] - box[1]) <= 54.0 or not value or not budget.left():
        return box
    mark = Mark(0, {}, page, box, "wrong", True, value)
    try:
        spots = _printed(pdf, mark, box)
    except Exception as failed:  # noqa: BLE001 - the AI's box is boxed then
        _log.warning("proof drawing value search failed: %s", type(failed).__name__)
        return box
    return spots[0] if spots else box


# ---- the pages after the sheets -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ProofItem:
    number: int
    finding: Mapping[str, Any]
    where: str
    """Where it is marked ("Marked on page 23."), or why it is not."""
    vendor_png: bytes | None
    architect_png: bytes | None


def _styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle("body", fontName=_NOTE_FONT, fontSize=9.5, leading=12.5, textColor=_DARK)
    return {
        "body": body,
        "small": ParagraphStyle("small", parent=body, fontSize=8.5, leading=11),
        "grey": ParagraphStyle("grey", parent=body, fontSize=9, leading=11.5, textColor=_LIST_GREY),
        "title": ParagraphStyle(
            "title", parent=body, fontName=_NOTE_BOLD, fontSize=20, leading=25, spaceAfter=4
        ),
        "stamp": ParagraphStyle(
            "stamp", parent=body, fontName=_NOTE_BOLD, fontSize=12, leading=16, spaceAfter=8
        ),
        "section": ParagraphStyle(
            "section",
            parent=body,
            fontName=_NOTE_BOLD,
            fontSize=13,
            leading=17,
            spaceBefore=10,
            spaceAfter=4,
            keepWithNext=1,
        ),
        "head": ParagraphStyle(
            "head", parent=body, fontName=_NOTE_BOLD, fontSize=11, leading=14, keepWithNext=1
        ),
        "caption": ParagraphStyle(
            "caption", parent=body, fontName=_NOTE_BOLD, fontSize=8.5, leading=11
        ),
    }


def _hex(colour: Color) -> str:
    return str(colour.hexval()).replace("0x", "#")


def _coloured(label: str, value: str, colour: Color) -> str:
    return (
        f'<font color="{_hex(colour)}">{escape(label)}<b>{escape(value)}</b></font>'
        if value
        else ""
    )


def _image(png: bytes, max_w: float, max_h: float) -> Image:
    picture = PILImage.open(io.BytesIO(png))
    scale = min(max_w / picture.width, max_h / picture.height)
    return Image(io.BytesIO(png), width=picture.width * scale, height=picture.height * scale)


def _cover(
    styles: Mapping[str, ParagraphStyle],
    *,
    set_name: str,
    stamp: str,
    counts: Mapping[str, int],
    clouded: Sequence[_ProofItem],
    assumptions: Sequence[str],
    width: float,
) -> list[Any]:
    body, grey = styles["body"], styles["grey"]
    story: list[Any] = [
        Paragraph(escape(f"Cloud drawing: {set_name}"), styles["title"]),
        Paragraph(escape(stamp), styles["stamp"]),
    ]
    tiles = [
        ("Wrong", counts["clouded"], RED, "proved by code, clouded"),
        ("Right", counts["right"], GREEN, "matches the architect"),
        ("Nothing to compare", counts["not_compared"], _LIST_GREY, "listed apart, with the reason"),
    ]
    cells = [
        [
            Paragraph(
                f'<font size="22" color="{_hex(colour)}"><b>{number}</b></font><br/>'
                f"<b>{escape(words)}</b><br/>"
                f'<font color="{_hex(_LIST_GREY)}">{escape(hint)}</font>',
                ParagraphStyle("tile", parent=body, leading=15),
            )
            for words, number, colour, hint in tiles
        ]
    ]
    grid = Table(cells, colWidths=[min(190.0, (width - 72) / 3)] * 3, hAlign="LEFT")
    grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBEFORE", (0, 0), (-1, -1), 2, Color(0.85, 0.85, 0.85)),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story += [grid, Spacer(1, 14)]
    extra = []
    if counts["dismissed"]:
        extra.append(f"{counts['dismissed']} marked not a problem by the reviewer")
    if extra:
        story.append(Paragraph(escape("Also: " + "; ".join(extra) + "."), grey))
    story.append(Paragraph(f"<b>The rule:</b> {escape(RULE_LINE)}", body))
    story.append(Spacer(1, 4))
    legend = [
        _coloured("", LEGEND_LINES[0], RED),
        _coloured("", LEGEND_LINES[1], GREEN),
        f'<font color="{_hex(_LIST_GREY)}">{escape(LEGEND_LINES[2])}</font>',
    ]
    story += [Paragraph(line, body) for line in legend]
    story.append(
        Paragraph(
            "Page numbers are the vendor file's own pages; each clouded item has a proof entry "
            "after the sheets.",
            grey,
        )
    )
    if assumptions:
        story.append(Paragraph("Assumed by code (nobody was asked)", styles["section"]))
        story += [Paragraph(escape(f"Assumed: {line}"), body) for line in assumptions]
    story.append(Paragraph(f"Clouded items ({len(clouded)})", styles["section"]))
    if not clouded:
        story.append(Paragraph("None: code proved no wrong value on this set.", grey))
    rows = [
        [
            Paragraph(f"<b>{item.number}</b>", body),
            Paragraph(escape(str(item.finding.get("page") or "")), body),
            Paragraph(escape(str(item.finding.get("item") or "")), body),
            Paragraph(_coloured("", vendor_value(item.finding), RED), body),
            Paragraph(_coloured("", should_be(item.finding), GREEN), body),
        ]
        for item in clouded
    ]
    if rows:
        head = [
            Paragraph(f"<b>{escape(words)}</b>", styles["small"])
            for words in ("#", "Page", "Item", "Vendor", "Should be")
        ]
        usable = width - 72
        table = Table(
            [head, *rows],
            colWidths=[24, 36, usable * 0.34, usable * 0.22, usable * 0.44 - 60],
            repeatRows=1,
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, Color(0.8, 0.8, 0.8)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(table)
    return story


#: A round-2 status as the cover says it, and its colour (#1603).
_ROUND_WORDS: Final[Mapping[str, tuple[str, Color]]] = {
    "fixed": ("Fixed", GREEN),
    "still_wrong": ("Still wrong", RED),
    "new": ("NEW", RED),
    "also_in_round_1": ("Still wrong (also in round 1)", RED),
    "now_right": ("Now right (was not compared)", GREEN),
    "still_not_compared": ("Still not compared", _LIST_GREY),
}


def _round_section(
    styles: Mapping[str, ParagraphStyle],
    rounds: Any,
    numbers: Mapping[str, int],
    width: float,
) -> list[Any]:
    """The cover's round-2 summary (#1603): one line, then each item code compared."""
    line = rounds.line() if rounds is not None else None
    if line is None:
        return []
    body = styles["body"]
    story: list[Any] = [
        Paragraph("Round 2: what the vendor did", styles["section"]),
        Paragraph(escape(line), body),
        Paragraph(
            escape(
                "Code lines round 2 up with round 1 by the values printed in both and compares "
                "the printed values at each item's spot; where it cannot, the item is listed in "
                "grey on the notes pages, never guessed."
            ),
            styles["grey"],
        ),
    ]
    rows = []
    for item in rounds.items:
        if item.status not in _ROUND_WORDS:
            continue
        words, colour = _ROUND_WORDS[item.status]
        rows.append(
            [
                Paragraph(f"<b>{numbers.get(item.finding_id, '')}</b>", body),
                Paragraph(escape(str(item.page or "")), body),
                Paragraph(escape(item.item), body),
                Paragraph(_coloured("", words, colour), body),
                Paragraph(_coloured("", item.was, RED), body),
                Paragraph(
                    _coloured("", str(item.now or ""), GREEN if item.status == "fixed" else RED),
                    body,
                ),
                Paragraph(_coloured("", item.should_be, GREEN), body),
            ]
        )
    if rows:
        usable = width - 72
        head = [
            Paragraph(f"<b>{escape(words)}</b>", styles["small"])
            for words in ("#", "Page", "Item", "Round 2", "Was", "Now", "Should be")
        ]
        table = Table(
            [head, *rows],
            colWidths=[
                24,
                36,
                usable * 0.3,
                usable * 0.14,
                usable * 0.14,
                usable * 0.14,
                usable * 0.28 - 60,
            ],
            repeatRows=1,
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, Color(0.8, 0.8, 0.8)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(table)
    return story


def _proof_entries(
    styles: Mapping[str, ParagraphStyle], items: Sequence[_ProofItem], width: float
) -> list[Any]:
    body, small, grey = styles["body"], styles["small"], styles["grey"]
    usable = width - 72
    max_w = usable * _CROP_MAX_W
    story: list[Any] = [Paragraph("Proof: how code proved each clouded item", styles["section"])]
    for item in items:
        finding = item.finding
        vendor_page = finding.get("page")
        box = _box_of(finding.get("arch_box"))
        architect_page = box[1] if box is not None else finding.get("vs_page")
        head = Paragraph(
            f'<font color="{_hex(RED)}"><b>{item.number}</b></font>&nbsp;&nbsp;'
            f"<b>{escape(str(finding.get('item') or ''))}</b>",
            styles["head"],
        )
        left: list[Any] = [Paragraph(_coloured("Governs: ", governs(finding), GREEN), small)]
        own = vendor_own(finding)
        if item.architect_png is not None:
            left.insert(0, _image(item.architect_png, max_w, _CROP_MAX_H))
        elif own == "sum":
            parts = " + ".join(str(part) for part in finding.get("parts") or [])
            summed = should_be_value(finding)
            left.insert(
                0,
                Paragraph(
                    escape(f"{parts} = " if parts else "The parts add up to ")
                    + _coloured("", summed, GREEN)
                    + "<br/>"
                    + escape(f"The vendor prints {vendor_value(finding)} for their total."),
                    body,
                ),
            )
        else:
            left.insert(
                0,
                Paragraph(
                    _coloured("", str(finding.get("architect") or ""), GREEN)
                    + "<br/>"
                    + escape("(no architect's picture: the value is a rule or a sum, not a spot)"),
                    body,
                ),
            )
        right: list[Any] = [Paragraph(_coloured("Vendor drew: ", "", RED), small)]
        if item.vendor_png is not None:
            right.insert(0, _image(item.vendor_png, max_w, _CROP_MAX_H))
        else:
            right.insert(0, Paragraph(escape("No picture of the vendor's spot."), grey))
        right[-1] = Paragraph(_coloured("Vendor drew: ", _as_printed(finding), RED), small)
        pair = Table(
            [
                [
                    Paragraph(escape(LEFT_CAPTIONS[own]), styles["caption"]),
                    Paragraph("Vendor (red: the wrong value)", styles["caption"]),
                ],
                [left, right],
            ],
            colWidths=[usable / 2, usable / 2],
            hAlign="LEFT",
        )
        pair.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ]
            )
        )
        pages = f"Vendor page {vendor_page}"
        if architect_page and architect_page != vendor_page:
            pages += f"; architect page {architect_page}"
        elif box is not None:
            pages += " (architect's drawing on the same sheet)"
        lines = [
            Paragraph(
                _coloured("Vendor: ", vendor_value(finding), RED)
                + "&nbsp;&nbsp;&nbsp;"
                + _coloured("Should be: ", should_be(finding), GREEN),
                body,
            ),
            Paragraph(f"<b>Proved by:</b> {escape(proof_line(finding))}", body),
            Paragraph(escape(f"{pages}. {item.where}"), grey),
        ]
        story.append(KeepTogether([head, Spacer(1, 3), pair, Spacer(1, 3), *lines, Spacer(1, 12)]))
    return story


def _as_printed(finding: Mapping[str, Any]) -> str:
    """The vendor's value as the finding words it (what is printed there, and any note)."""
    return str(finding.get("vendor") or "")


def _list_entries(
    styles: Mapping[str, ParagraphStyle],
    heading: str,
    findings: Sequence[Mapping[str, Any]],
    line: Callable[[Mapping[str, Any]], str],
    colour: Color,
) -> list[Any]:
    style = ParagraphStyle("list", parent=styles["grey"], textColor=colour)
    story: list[Any] = [Paragraph(escape(f"{heading} ({len(findings)})"), styles["section"])]
    if not findings:
        return [*story, Paragraph("None.", style)]
    for finding in sorted(findings, key=lambda f: int(f.get("page") or 0)):
        story.append(
            Paragraph(
                f"<b>p.{escape(str(finding.get('page')))}&nbsp;&nbsp;"
                f"{escape(str(finding.get('item') or ''))}</b>: {escape(line(finding))}",
                style,
            )
        )
    return story


def _right_line(finding: Mapping[str, Any]) -> str:
    source = "architect" if finding.get("source") in (None, "architect", "gv_review") else ""
    source = source or str(finding.get("source") or "").replace("_", " ")
    return f"{finding.get('vendor')} matches {source} {finding.get('architect')}"


def _dismissed_line(finding: Mapping[str, Any]) -> str:
    decision = finding.get("decision") or {}
    by = decision.get("by") if isinstance(decision, Mapping) else None
    note = decision.get("note") if isinstance(decision, Mapping) else None
    words = f"vendor {finding.get('vendor')}; architect {finding.get('architect')}. "
    words += f"Not a problem ({by})" if by else "Not a problem"
    return words + (f": {note}" if note else ".")


def _pages_pdf(size: tuple[float, float], parts: Sequence[tuple[str, list[Any]]]) -> list[bytes]:
    """Each part (its title, its story) as its own PDF of `size` pages."""
    width, height = size
    margin = 36.0
    out: list[bytes] = []
    for title, story in parts:
        buffer = io.BytesIO()

        def header(c: canvas.Canvas, document: Any, title: str = title) -> None:
            c.saveState()
            c.setFillColor(_GREY)
            c.setFont(_NOTE_FONT, 8)
            c.drawString(margin, height - margin + 14, f"GV Review · {title}")
            c.drawRightString(width - margin, margin - 18, f"{title} {document.page}")
            c.restoreState()

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
        frame = Frame(margin, margin, width - 2 * margin, height - 2 * margin, id="body")
        document.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=header)])
        document.build(story or [Paragraph("None.", _styles()["grey"])])
        out.append(buffer.getvalue())
    return out


def _labels(pdf: pikepdf.Pdf, sheets: int, proofs: int) -> None:
    """Page labels: "Cover", the vendor's own page numbers, "Proof 1…", "Notes 1…"."""
    nums: list[Any] = [
        0,
        pikepdf.Dictionary(P=pikepdf.String("Cover")),
        1,
        pikepdf.Dictionary(S=pikepdf.Name.D, St=1),
    ]
    if proofs:
        nums += [1 + sheets, pikepdf.Dictionary(S=pikepdf.Name.D, P=pikepdf.String("Proof "))]
    if len(pdf.pages) > 1 + sheets + proofs:
        nums += [
            1 + sheets + proofs,
            pikepdf.Dictionary(S=pikepdf.Name.D, P=pikepdf.String("Notes ")),
        ]
    pdf.Root.PageLabels = pdf.make_indirect(pikepdf.Dictionary(Nums=pikepdf.Array(nums)))


# ---- the whole drawing ----------------------------------------------------------------------------


def proof_markup_pdf(
    source: Path,
    placed: Sequence[Placed],
    out: Path,
    *,
    set_name: str,
    stamp: str,
    base: Base | None = None,
    files: Mapping[str, Path] | None = None,
    cache: Path | None = None,
    assumptions: Sequence[str] = (),
    settled: Sequence[Mapping[str, Any]] = (),
    rounds: Any = None,
    not_compared: int | None = None,
) -> ProofCounts:
    """The zero-click cloud drawing of a full check (see the module's words) written to `out`.

    `placed`: every finding, biggest first (`chat_findings.place_findings`): (number, finding,
    page, box, where), `page`/`box` `None` when it cannot be placed on the vendor's file. `files`:
    the chat's uploads by name, for the architect's crops (the vendor's file is `source`).
    `cache`: the set pack's hi-res page cache. `assumptions`: what code assumed about the page map,
    one line each. `settled`: what code settled by itself (#1554), listed on the notes pages.
    `rounds`: the automatic round-2 check (`round_check.RoundReport`, #1603): each round-1 item
    marked on its round-2 page (fixed: green check; still wrong: red cloud), new round-2 items
    headed "NEW", a summary on the cover and the items code could not compare in grey.
    `not_compared`: how many rows the Not-compared list has (its product scope), shown on the
    cover; else counted here. Returns the counts the cover shows."""
    base = base if base is not None else clone_pdf(source)
    pdf = base.pdf
    known = dict(files or {})
    known.setdefault(source.name, source)
    findings = [finding for _n, finding, *_rest in placed]
    counts = proof_counts(findings)
    cover_counts = {
        **counts,
        "not_compared": counts["not_compared"] if not_compared is None else not_compared,
    }
    by_round = rounds.by_finding() if rounds is not None else {}
    crop_source = source
    try:
        pages = len(pdf.pages)
        clouded: list[tuple[int, Mapping[str, Any], int | None, Box | None, str]] = []
        for _n, finding, page, box, where in placed:
            if proof_group(finding) != "clouded":
                continue
            shown = dict(finding)
            found = by_round.get(str(finding.get("id")))
            if found is not None and found.status == "new":
                shown[_ROUND_KEY] = found
            if page is not None and box is not None and not 1 <= page <= pages:
                page, box, where = None, None, f"Not marked: the file has no page {page}."
            clouded.append((len(clouded) + 1, shown, page, box, where))
        marks: list[Mark] = []
        for number, finding, page, box, _where in clouded:
            vendor_box = finding.get("vendor_box")
            value = str(
                finding.get("vendor")
                if isinstance(vendor_box, Mapping)
                else finding.get("architect")
            )
            if page is not None and box is not None:
                marks.append(Mark(number, finding, page, box, "wrong", True, value or ""))
            marks += also_marks(number, finding, "wrong", True, source, pages)
            # The same item on its round-2 page (#1603): fixed (green check) or still wrong.
            later = by_round.get(str(finding.get("id")))
            if (
                later is not None
                and later.status in ("fixed", "still_wrong")
                and later.spot is not None
                and later.page is not None
                and later.file in (None, source.name)
                and 1 <= later.page <= pages
            ):
                marks.append(
                    Mark(
                        number,
                        {**finding, _ROUND_KEY: later, "page": later.page},
                        later.page,
                        later.spot,
                        "fixed" if later.status == "fixed" else "wrong",
                        later.status != "fixed",
                        str(later.now or ""),
                        extra=True,
                    )
                )
        by_page: dict[int, list[Mark]] = {}
        for mark in marks:
            by_page.setdefault(mark.page, []).append(mark)
        # A round-2 mark sits on code's own label (already tight), so it needs no search; it may
        # share its number and page with the round-1 mark (both rounds on one sheet).
        spots = _spots(source, [m for m in marks if not _later(m)])
        laid: dict[int, list[_Placed]] = {}
        clean = io.BytesIO()
        if by_page or base.dropped:
            pdf.save(clean, deterministic_id=True)
        # The proof crops come from the copy without earlier review markup (GV's boxes and notes
        # would hide the vendor's value); the same bytes give the same hi-res cache pages, so a
        # remade drawing cuts them again without drawing anything.
        crop_source = source
        if base.dropped:
            crop_source = out.with_name(f"{out.name}.clean.pdf")
            crop_source.parent.mkdir(parents=True, exist_ok=True)
            crop_source.write_bytes(clean.getvalue())
        if by_page:
            with _PDFIUM_LOCK:
                document = pdfium.PdfDocument(clean.getvalue())
            try:
                for number in sorted(by_page):
                    items = [
                        (
                            _Placed(m, _grown(m.box), None)
                            if _later(m)
                            else _Placed(m, *spots[(m.number, m.page)])
                        )
                        for m in by_page[number]
                    ]
                    _layout(_shown_size(pdf.pages[number - 1]), items, _ink(document, number - 1))
                    laid[number] = items
            finally:
                with _PDFIUM_LOCK:
                    document.close()
        for number, items in sorted(laid.items()):

            def draw(c: canvas.Canvas, height: float, items: Any = items) -> None:
                _draw_sheet(c, height, items)

            _overlay(pdf, pdf.pages[number - 1], draw)
        # The proof crops: the vendor's spot (as clouded) and the architect's governing value.
        sizes = _Sizes()
        budget = _Budget(_SNAP_BUDGET_S)
        cloud_spot = {
            item.mark.number: item.spot
            for items in laid.values()
            for item in items
            if not item.mark.extra
        }
        proof_items: list[_ProofItem] = []
        for number, finding, page, box, where in clouded:
            vendor_png = None
            if page is not None and box is not None:
                spot = cloud_spot.get(number, box)
                vendor_png = boxed_crop(crop_source, page, spot, RED, sizes, cache)
            architect_png = None
            arch = _box_of(finding.get("arch_box"))
            if arch is not None and finding.get("source") not in ("rules", "vendor_arithmetic"):
                name, arch_page, arch_area = arch
                arch_pdf = known.get(name) if name else source
                if arch_pdf == source:
                    arch_pdf = crop_source
                if arch_pdf is not None and arch_pdf.is_file():
                    tight = _tight(
                        arch_pdf, arch_page, arch_area, str(finding.get("architect") or ""), budget
                    )
                    architect_png = boxed_crop(arch_pdf, arch_page, tight, GREEN, sizes, cache)
            proof_items.append(_ProofItem(number, finding, where, vendor_png, architect_png))
        marked = io.BytesIO()
        pdf.save(marked, deterministic_id=True)
    finally:
        pdf.close()
        if crop_source != source:
            crop_source.unlink(missing_ok=True)

    size = _shown_size_of(marked.getvalue())
    styles = _styles()
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for finding in findings:
        groups.setdefault(proof_group(finding), []).append(finding)
    folded = [e for e in settled if e.get("outcome") in ("merged", "not_a_difference")]
    notes: list[Any] = []
    nothing = cover_counts["not_compared"]
    notes.append(
        Paragraph(
            escape(
                f"Nothing to compare ({nothing}): listed with the reason and what would settle "
                f'it in the separate list, "{set_name} - {NOT_COMPARED_TITLE}".'
            ),
            styles["grey"],
        )
    )
    notes += _list_entries(styles, "Right", groups.get("right", []), _right_line, GREEN)
    if groups.get("dismissed"):
        notes += _list_entries(
            styles, GROUP_WORDS["dismissed"], groups["dismissed"], _dismissed_line, _LIST_GREY
        )
    unmatched = [
        i for i in (rounds.items if rounds is not None else []) if i.status == "not_compared"
    ]
    if unmatched:
        notes.append(
            Paragraph(
                escape(f"Round 2: could not be compared by code ({len(unmatched)})"),
                styles["section"],
            )
        )
        notes += [
            Paragraph(
                f"<b>{escape(item.item)}</b>: was {escape(item.was)}; should be "
                f"{escape(item.should_be)}. Why: {escape(item.reason or '')}",
                styles["grey"],
            )
            for item in unmatched
        ]
    if folded:
        notes.append(
            Paragraph(f"Settled automatically by GV Review ({len(folded)})", styles["section"])
        )
        notes += [
            Paragraph(
                f"<b>p.{escape(str(e.get('page')))}&nbsp;&nbsp;{escape(str(e.get('item') or ''))}"
                f"</b>: {escape(' '.join(settled_entry_lines(e)))}",
                styles["grey"],
            )
            for e in folded
        ]
    cover = _cover(
        styles,
        set_name=set_name,
        stamp=stamp,
        counts=cover_counts,
        clouded=proof_items,
        assumptions=assumptions,
        width=size[0],
    )
    numbers = {str(item.finding.get("id")): item.number for item in proof_items}
    cover += _round_section(styles, rounds, numbers, size[0])
    parts = [("Cover", cover)]
    if proof_items:
        parts.append(("Proof", _proof_entries(styles, proof_items, size[0])))
    parts.append(("Notes", notes))
    made = _pages_pdf(size, parts)
    with pikepdf.open(io.BytesIO(marked.getvalue())) as sheets:
        final = pikepdf.new()
        with pikepdf.open(io.BytesIO(made[0])) as front:
            final.pages.extend(front.pages)
            final.pages.extend(sheets.pages)
            proof_pages = 0
            for extra in made[1:]:
                with pikepdf.open(io.BytesIO(extra)) as back:
                    if extra is made[1] and proof_items:
                        proof_pages = len(back.pages)
                    final.pages.extend(back.pages)
            _labels(final, len(sheets.pages), proof_pages)
            final.docinfo["/Title"] = pikepdf.String(f"Cloud drawing: {set_name}")
            out.parent.mkdir(parents=True, exist_ok=True)
            final.save(out, deterministic_id=True)
    return ProofCounts(**counts)


def _shown_size_of(data: bytes) -> tuple[float, float]:
    """The first page's shown size (the cover, proof and notes pages take it)."""
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return _shown_size(pdf.pages[0])


# ---- the Not-compared list (#1619) ----------------------------------------------------------------

#: The list's page: US letter, landscape (a table, not a drawing).
_LIST_PAGE: Final = (792.0, 612.0)
#: The small picture of each spot (points on the list's page).
_SPOT_W: Final = 130.0
_SPOT_H: Final = 80.0


def not_compared_pdf(
    source: Path,
    rows: Sequence[Any],
    out: Path,
    *,
    set_name: str,
    stamp: str,
    cache: Path | None = None,
) -> int:
    """The separate "Not compared" PDF of a full check (#1619), written to `out`: the rows of
    `not_compared.rows` (every item that could not be compared under the architect-first rules),
    grouped by reason, each with its page, item, what the vendor shows, why it could not be
    compared, what is needed to compare it and a small picture of its spot (cut from the hi-res
    cache when there is one); then the set's own gaps code found (`not_compared.gap_rows`: finish
    codes not comparable, areas or rounds with no sheet). Nobody is asked to decide anything here.
    Returns how many item rows it lists (the workbook and the screen count the same rows)."""
    from app.review.assistant.classification import REASON_WORDS, REASONS, TRADE_WORDS

    styles = _styles()
    body, small, grey = styles["body"], styles["small"], styles["grey"]
    width, _height = _LIST_PAGE
    usable = width - 72
    sizes = _Sizes()
    items = [row for row in rows if getattr(row, "level", "item") == "item"]
    gaps = [row for row in rows if getattr(row, "level", "item") != "item"]

    def grouped(chosen: Sequence[Any]) -> dict[str, list[list[Any]]]:
        out: dict[str, list[list[Any]]] = {}
        for row in chosen:
            picture: Any = Paragraph("", small)
            if row.box is not None and row.page is not None and row.file in (None, source.name):
                png = boxed_crop(source, row.page, row.box, _LIST_GREY, sizes, cache)
                if png is not None:
                    picture = _image(png, _SPOT_W, _SPOT_H)
            out.setdefault(row.reason, []).append(
                [
                    Paragraph(escape("" if row.page is None else str(row.page)), body),
                    Paragraph(f"<b>{escape(row.item)}</b>", body),
                    Paragraph(escape(row.vendor), small),
                    Paragraph(escape(row.architect), small),
                    Paragraph(escape(row.why), small),
                    Paragraph(escape(row.needed), small),
                    picture,
                ]
            )
        return out

    total = len(items)
    story: list[Any] = [
        Paragraph(escape(f"{NOT_COMPARED_TITLE}: {set_name}"), styles["title"]),
        Paragraph(escape(stamp), styles["stamp"]),
        Paragraph(
            escape(
                f"{total} item{'' if total == 1 else 's'} could not be compared. The architect's "
                "drawings govern; these items are not on the cloud drawing, and nobody is asked to "
                "decide them: each row says why it could not be compared and what is needed."
            ),
            body,
        ),
        Spacer(1, 8),
    ]
    if not total:
        story.append(Paragraph("Every item could be compared.", grey))
    head = [
        Paragraph(f"<b>{escape(words)}</b>", small)
        for words in ("Page", "Item", "Vendor shows", "Architect shows", "Why not compared",
                      "What is needed", "Spot")
    ]  # fmt: skip
    widths = [30, usable * 0.15, usable * 0.11, usable * 0.13, usable * 0.2, usable * 0.16]
    widths.append(usable - sum(widths))

    def sections(chosen: Sequence[Any]) -> None:
        by_reason = grouped(chosen)
        for reason in REASONS:
            group = by_reason.get(reason)
            if not group:
                continue
            words = REASON_WORDS[reason]
            story.append(
                Paragraph(
                    escape(f"{TRADE_WORDS.get(reason, words)} ({len(group)})")
                    + f'<font size="9" color="{_hex(_LIST_GREY)}">&nbsp;&nbsp;{escape(words)}</font>',
                    # Not kept with its table: a long table starts on the same page and splits.
                    ParagraphStyle("list_section", parent=styles["section"], keepWithNext=0),
                )
            )
            table = Table([head, *group], colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LINEBELOW", (0, 0), (-1, -1), 0.25, Color(0.8, 0.8, 0.8)),
                        ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.append(table)

    sections(items)
    if gaps:
        story.append(Spacer(1, 10))
        story.append(
            Paragraph(escape(f"Found by code across the set ({len(gaps)})"), styles["section"])
        )
        story.append(
            Paragraph(
                escape(
                    "Code's finish check and consistency check list what they could not compare "
                    "for the set as a whole (not items of this answer)."
                ),
                grey,
            )
        )
        sections(gaps)
    made = _pages_pdf(_LIST_PAGE, [(NOT_COMPARED_TITLE, story)])[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(io.BytesIO(made)) as document:
        document.docinfo["/Title"] = pikepdf.String(f"{NOT_COMPARED_TITLE}: {set_name}")
        document.save(out, deterministic_id=True)
    return total


def _clip(text: object, limit: int) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"
