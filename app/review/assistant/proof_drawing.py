"""The zero-click red/green proof drawing (#1581), drawn by code at the end of every full check.

What the vendor gets, with no human step (the rule is `proof.py`):

1. **Cover**: the counts (Wrong, clouded; Right; Not compared), the rule in one line, the stamp "Checked automatically by GV Review: <date>" ("… and reviewed by <name>" once a
   reviewer decided anything), every assumption code made about the page map, and the list of the
   clouded items.
2. **The vendor's sheets**: a tight red cloud round each wrong value code proved, a numbered red
   marker, and a short note in clear space, never cut and kept off the sheet's printed numbers
   (#1804): the C-number and the item, then `46"  should be  37"` (red, grey, green). The
   tight-cloud and clear-space layout is the cloud drawing's own (`chat_drawing`, `cloud_layout`).
3. **Proof pages** (#1804): one card per red mark, two to a page, in the clouded order (biggest
   first): the C-number and a plain title; one sentence (`proof_words.sentence`, the letter's and
   the register's too); the vendor's snippet with its value ringed red beside the reference's
   (architect, brand prototype, the vendor's other span) ringed green, each zoomed by the value's
   own printed size so it reads on any sheet; a worked sum, GV's rule or GV's reviewer's value
   where there is no reference spot; then the sum written out, the reference and how it was
   checked, in the vendor's words. Snippets are rendered from the copy without earlier review
   markup.
4. **Notes pages**: a short green list of what is right (an item the reviewer marked not a problem
   is Right there, noted "(reviewer)": the vendor's files have three outcomes only, #1766), and what
   code settled by itself. What could not be compared (#1619, the architect-first rules
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
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from xml.sax.saxutils import escape

import pikepdf
from PIL import Image as PILImage
from PIL import ImageDraw
from reportlab.lib.colors import Color, white  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.review.assistant import proof_words
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
from app.review.assistant.cloud_layout import (
    NOTE_WIDTH_PT,
    InkMap,
    marker_spot,
    place_notes,
    snapped,
)
from app.review.assistant.finding_checks import wrong_part
from app.review.assistant.likely_problems import GROUP as LIKELY_GROUP
from app.review.assistant.likely_problems import HEADING as LIKELY_HEADING
from app.review.assistant.likely_problems import LETTER_LEAD as LIKELY_LEAD
from app.review.assistant.likely_problems import SHORT_WORDS as LIKELY_SHORT
from app.review.assistant.likely_problems import please_check
from app.review.assistant.proof import (
    RULE_LINE,
    architect_file,
    governs,
    proof_counts,
    proof_group,
    proof_of,
    should_be_value,
    vendor_value,
)
from app.review.assistant.rule_notes import HEADING as RULE_NOTE_HEADING
from app.review.assistant.vendor_text import internal, item_name, reviewer_right, vendor_text
from extraction.pdfium_guard import pdfium

__all__ = [
    "GREEN",
    "LEGEND_LINES",
    "NOT_COMPARED_TITLE",
    "RED",
    "STAMP_WORDS",
    "Legend",
    "ProofCounts",
    "Shot",
    "contrast",
    "item_words",
    "not_compared_pdf",
    "number_boxes",
    "panel_size",
    "proof_markup_pdf",
    "snippet",
    "stamp_line",
    "tag",
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
#: A rule note (#1839): neutral slate, never red, green or amber (decided, nothing for the vendor).
_NOTE_SLATE: Final = Color(0.2, 0.27, 0.38)
#: A likely problem (2026-10-23): dark amber, never the red of a proved Wrong.
_LIKELY_AMBER: Final = Color(0.6, 0.35, 0.0)
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
#: The review result's colour on the cover (#1622): red to resubmit or rejected, dark amber as
#: noted, green approved, grey when GV's person decides the rest or the check did not finish.
_ACTION_COLOURS: Final[Mapping[str, Color]] = {
    "revise_resubmit": _RED,
    "rejected": _RED,
    "approved_as_noted": Color(0.63, 0.29, 0.02),
    "approved": Color(0.0, 0.47, 0.2),
    "review_needed": Color(0.4, 0.4, 0.4),
    "not_for_sending": Color(0.4, 0.4, 0.4),
}
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
    rule_note: int = 0
    """Sizes the architect specifies that break a GV rule (#1839): decided, never clouded."""
    likely_problem: int = 0
    """The whole-drawing look's claims code could not prove (2026-10-23): never clouded."""


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
    green, one line each (cut with "…"; the proof page has the whole words). On round 2 (#1603), in
    the comment letter's words (#1766): "Closed in round 2: was 46", now 37"" in green, "Previous
    comment not addressed: now 46"" in red, or the item headed "New" in red."""
    finding = mark.finding
    vendor = vendor_text(vendor_value(finding), "") or ""
    value = vendor_text(should_be_value(finding), "") or ""
    source = vendor_text(governs(finding), "") or ""
    room = NOTE_WIDTH_PT - 2 * _NOTE_PAD - _NOTE_BAR - _prefix_width(mark)
    item = item_words(finding)
    rounds = _round(mark)
    head: list[_Run] = [(item, _NOTE_BOLD, _DARK)]
    if rounds is not None and rounds.status == "fixed":
        return [
            _fit(head, room),
            _fit(
                [
                    ("Closed in round 2: ", _NOTE_FONT, GREEN),
                    ("was ", _NOTE_FONT, GREEN),
                    (_vendor(rounds.was), _NOTE_BOLD, GREEN),
                    (", now ", _NOTE_FONT, GREEN),
                    (_vendor(rounds.now), _NOTE_BOLD, GREEN),
                ],
                room,
            ),
        ]
    if rounds is not None and rounds.status == "still_wrong":
        middle = [
            ("Previous comment not addressed: now ", _NOTE_FONT, RED),
            (_vendor(rounds.now), _NOTE_BOLD, RED),
        ]
        if rounds.now != rounds.was:
            middle.append((f" (was {_vendor(rounds.was)})", _NOTE_FONT, RED))
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
        head = [("New ", _NOTE_BOLD, RED), (item, _NOTE_BOLD, _DARK)]
    # The note says only what is wrong and what it should be, never cut (#1804): the item (up to
    # two lines), then `98"  should be  97 1/2"`. The reference and the proof are on the card.
    value = proof_words.should_text(finding) or value
    shown = "missing" if proof_words.missing(vendor) else vendor
    pair: list[_Run] = [
        (shown, _NOTE_BOLD, RED),
        ("  should be  ", _NOTE_FONT, _GREY),
        (value, _NOTE_BOLD, GREEN),
    ]
    lines = _wrapped(head, room, rows=2)
    if sum(_measure(t, f) for t, f, _c in pair) <= room:
        return [*lines, pair]
    return [
        *lines,
        _fit([pair[0]], room),
        _fit([("should be  ", _NOTE_FONT, _GREY), pair[2]], room),
    ]


def _wrapped(runs: Sequence[_Run], room: float, rows: int) -> list[list[_Run]]:
    """A heading's pieces on at most `rows` lines of `room` points (word by word; the last line cut
    with "…" only when even `rows` lines cannot hold it)."""
    words: list[_Run] = [
        (word, font, colour) for text, font, colour in runs for word in text.split()
    ]
    lines: list[list[_Run]] = []
    current: list[_Run] = []
    used = 0.0
    for word, font, colour in words:
        width = _measure(word + " ", font)
        if current and used + width > room:
            lines.append(current)
            current, used = [], 0.0
            if len(lines) == rows:
                break
        current.append((word + " ", font, colour))
        used += width
    else:
        if current:
            lines.append(current)
        return [_fit(line, room) for line in lines]
    # Words left over: the last line is cut with "…".
    rest = [w for w, _f, _c in words][sum(len(line) for line in lines) :]
    last = lines[-1]
    tail_font, tail_colour = last[-1][1], last[-1][2]
    lines[-1] = _fit([*last, (" ".join(rest), tail_font, tail_colour)], room)
    return [_fit(line, room) for line in lines]


def item_words(finding: Mapping[str, Any]) -> str:
    """A clouded item's name as the vendor reads it (`vendor_text.item_name`: never a record id or
    internal words), else "Item on page N" when there are no words to name it by."""
    fallback = f"Item on page {finding.get('page')}" if finding.get("page") else "Item"
    return item_name(dict(finding)) or fallback


def tag(finding: Mapping[str, Any], number: int) -> str:
    """What a clouded item is called on the drawing: its stable comment number ("C-07", #1622)
    when it has one, else its place in the list."""
    label = finding.get("comment_no")
    return str(label) if label else str(number)


def _marker_words(finding: Mapping[str, Any], number: int) -> str:
    """The marker circle's words: the comment number's digits ("07"), else the place."""
    words = tag(finding, number)
    return words.removeprefix("C-")


def _prefix_width(mark: Mark) -> float:
    return _measure(f"{tag(mark.finding, mark.number)}  ", _NOTE_BOLD)


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


#: The pasted drawings' digits are kept off as cells of this size (points): few boxes, all covered.
_CELL_PT: Final = 6.0


def _digit_cells(pdf: Path, index: int) -> list[Box]:
    """Where the page's pasted drawings print digits, as `_CELL_PT` cells (a note must not cover
    them either)."""
    cells = {
        (int((c.box[0] + c.box[2]) / 2 // _CELL_PT), int((c.box[1] + c.box[3]) / 2 // _CELL_PT))
        for c in _printed_chars(pdf, index)
        if c.text.isdigit()
    }
    return [(i * _CELL_PT, j * _CELL_PT, (i + 1) * _CELL_PT, (j + 1) * _CELL_PT) for i, j in cells]


def number_boxes(document: Any, index: int) -> list[Box]:
    """Every printed piece of text on a page that holds a digit (shown points, origin top-left):
    the sheet's own numbers, which a note must never cover (#1804). Empty when the page has no
    text layer (a drawn or scanned sheet: the least-ink place keeps notes off its strokes)."""
    out: list[Box] = []
    with _PDFIUM_LOCK:
        try:
            page = document[index]
            height = page.get_height()
            text = page.get_textpage()
            try:
                for n in range(text.count_rects()):
                    left, bottom, right, top = text.get_rect(n)
                    words = text.get_text_bounded(left, bottom, right, top)
                    if any(ch.isdigit() for ch in words):
                        out.append((left, height - top, right, height - bottom))
            finally:
                text.close()
        except Exception as failed:  # noqa: BLE001 - notes are then placed by ink only
            _log.warning("cloud drawing number boxes failed: %s", type(failed).__name__)
            return []
    return out


def _layout(
    size: tuple[float, float],
    placed: list[_Placed],
    ink: InkMap | None,
    numbers: Sequence[Box] = (),
) -> None:
    """Markers, then notes, for one sheet's clouds (in place). A note goes clear of the sheet's
    printed numbers (`numbers`) where it can; only a note with no such place falls back to the
    least-ink place among the clouds, markers and other notes."""
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
    kept_off: list[Box] = [_grown(box, 1.5) for box in numbers]
    first: list[Box | None] = (
        place_notes(size, sizes, ink, [*blocked, *kept_off]) if kept_off else [None] * len(sizes)
    )
    for item, found in zip(ordered, first, strict=True):
        item.note = found
    left = [n for n, item in enumerate(ordered) if item.note is None]
    if left:
        clear: list[Box] = list(blocked)
        clear += [item.note for item in ordered if item.note is not None]
        again = place_notes(size, [sizes[n] for n in left], ink, clear)
        for n, box in zip(left, again, strict=True):
            ordered[n].note = box


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
        prefix = tag(item.mark.finding, item.mark.number)
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
        label = _marker_words(item.mark.finding, item.mark.number)
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


def _tighten(
    source: Path, marks: Sequence[Mark], spots: dict[tuple[int, int], tuple[Box, Box | None]]
) -> None:
    """A small box that holds more than the value (two stacked labels, a value and its note) cut
    to the value's own printed text (#1804), so the cloud and the proof's ring never take in a
    neighbouring number. `_spots` searches only big boxes; this does the small ones, in place,
    within the same time budget. A value drawn as lines (no text) keeps its box."""
    budget = _Budget(_SNAP_BUDGET_S)
    for mark in marks:
        key = (mark.number, mark.page)
        if _later(mark) or key not in spots or not mark.value or not budget.left():
            continue
        box, area = spots[key]
        if area is not None or max(box[2] - box[0], box[3] - box[1]) > 54.0:
            continue
        try:
            found = [s for s in _printed(source, mark, box) if _overlap(s, box)]
        except Exception as failed:  # noqa: BLE001 - the box is clouded then
            _log.warning("proof drawing tighten failed: %s", type(failed).__name__)
            continue
        tight = snapped(found, pad=1.5)
        if tight is not None and _area(tight) < 0.8 * _area(box):
            spots[key] = (tight, None)


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


# ---- the pages after the sheets -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Shot:
    """A snippet of a drawing for a proof card: the picture, the window of the sheet it shows
    (shown points, origin top-left) and the value's spot in it, ringed when drawn (#1804)."""

    png: bytes
    window: Box
    spot: Box


@dataclass(frozen=True, slots=True)
class _ProofItem:
    number: int
    finding: Mapping[str, Any]
    where: str
    """Where it is marked ("Marked on page 23."), or why it is not."""
    vendor: Shot | None
    reference: Shot | None


#: A proof card's snippet: the value's height on the card (points) it is zoomed to, the share of
#: the panel the value may fill at most, the zoom limits, and the picture's pixels per card point.
_VALUE_PT: Final = 15.0
_VALUE_MIN_PT: Final = 9.0
_VALUE_SHARE: Final = 0.7
_ZOOM_MAX: Final = 6.0
_SNIPPET_PX_PER_PT: Final = 2.2
_SNIPPET_MAX_PX: Final = 2400
#: The ring round a value: its line width and how far it stands off the value (card points).
_RING_W: Final = 2.0
_RING_PAD: Final = 4.0
#: A value this much longer one way than the other gets a rounded box, not an ellipse.
_LONG: Final = 3.2


def snippet(
    pdf: Path,
    page: int,
    spot: Box,
    panel: tuple[float, float],
    sizes: _Sizes,
    view: Box | None = None,
) -> Shot | None:
    """A snippet of `pdf`'s `page` round the value at `spot`, cut to the panel's own shape and
    zoomed by the value's own size (#1804): the value is drawn about `_VALUE_PT` high on the card
    (never more than `_VALUE_SHARE` of the panel), so a 5 pt value on a large sheet is as readable
    as a 14 pt one on a small sheet, and the window shows as much round it as the panel holds.
    `view`: an area that should show too (a dimension string round its total): the zoom goes down
    to fit it, but never below `_VALUE_MIN_PT` for the value. Rendered from the PDF at
    `_SNIPPET_PX_PER_PT` per card point. `None` when the page or the spot is not there."""
    size = sizes.of(pdf, page)
    if size is None:
        return None
    width, height = size
    x0, y0, x1, y1 = spot
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 <= 0 or y1 <= 0 or x0 >= width or y0 >= height:
        return None
    pw, ph = panel
    w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(str(pdf))
        except Exception as failed:  # noqa: BLE001 - no snippet then, never a failed drawing
            _log.warning("proof snippet open failed: %s", type(failed).__name__)
            return None
    try:
        if not 1 <= page <= len(document):
            return None
        printed = _text_height(pdf, page - 1, (x0, y0, x1, y1))
        text_h = min(h, w * 1.6)
        share = _VALUE_SHARE
        if printed is not None and max(w, h) <= 8 * printed:
            # The value's own printed height when the drawing's text gives it (a box drawn round
            # a small value is often much bigger than the value).
            text_h, share = printed, 1.0
        zoom = min(_VALUE_PT / text_h, share * pw / w, share * ph / h, _ZOOM_MAX)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if view is not None:
            v0, v1 = sorted((view[0], view[2]))
            u0, u1 = sorted((view[1], view[3]))
            fit = min(_VALUE_SHARE * pw / max(v1 - v0, 1.0), _VALUE_SHARE * ph / max(u1 - u0, 1.0))
            if fit < zoom:
                zoom = max(fit, min(zoom, _VALUE_MIN_PT / text_h))
                cx, cy = (max(v0, 0.0) + min(v1, width)) / 2, (max(u0, 0.0) + min(u1, height)) / 2
        zoom = max(zoom, min(pw / width, ph / height))
        win_w, win_h = min(pw / zoom, width), min(ph / zoom, height)
        # The value stays inside the window, whatever the view's centre.
        cx = min(max(cx, x1 - win_w / 2 + 2), x0 + win_w / 2 - 2)
        cy = min(max(cy, y1 - win_h / 2 + 2), y0 + win_h / 2 - 2)
        left = min(max(cx - win_w / 2, 0.0), width - win_w)
        top = min(max(cy - win_h / 2, 0.0), height - win_h)
        window = (left, top, left + win_w, top + win_h)
        drawn = min(pw / win_w, ph / win_h)
        scale = min(drawn * _SNIPPET_PX_PER_PT, _SNIPPET_MAX_PX / max(win_w, win_h))
        with _PDFIUM_LOCK:
            bitmap = document[page - 1].render(
                scale=scale,
                crop=(left, height - (top + win_h), width - (left + win_w), top),
                may_draw_forms=True,
            )
            picture = bitmap.to_pil().convert("RGB")
    except Exception as failed:  # noqa: BLE001 - no snippet then
        _log.warning("proof snippet render failed: %s", type(failed).__name__)
        return None
    finally:
        with _PDFIUM_LOCK:
            document.close()
    out = io.BytesIO()
    picture.save(out, format="PNG", optimize=True)
    return Shot(out.getvalue(), window, (x0, y0, x1, y1))


def _printed_chars(pdf: Path, index: int) -> Sequence[Any]:
    """The page's printed characters, with those of its pasted drawings (`verify_value`'s own
    reader, cached per file version); none when it cannot read them."""
    try:
        verify = import_module("app.review.assistant.verify_value")
        return tuple(verify._read_page(pdf, index).chars)
    except Exception as failed:  # noqa: BLE001 - the box's own size is used then
        _log.warning("proof drawing text read failed: %s", type(failed).__name__)
        return ()


def _text_height(pdf: Path, index: int, spot: Box) -> float | None:
    """The height of the printed digits inside `spot` (their median), or `None` when the drawing
    has no such text there (drawn lines, a scan)."""
    sizes = sorted(
        float(char.size)
        for char in _printed_chars(pdf, index)
        if char.text.isdigit() and char.size > 0.5 and _overlap(char.box, spot)
    )
    return sizes[len(sizes) // 2] if sizes else None


class _Snippet(Flowable):  # type: ignore[misc]
    """A snippet on a proof card: the picture fitted into the panel, a thin frame, and a ring in
    `colour` round the value (vector strokes over the picture: exact colour, crisp in print)."""

    def __init__(self, shot: Shot, width: float, height: float, colour: Color) -> None:
        super().__init__()
        self.shot, self.width, self.height, self.colour = shot, width, height, colour

    def draw(self) -> None:
        c = self.canv
        left, top, right, bottom = self.shot.window
        scale = min(self.width / (right - left), self.height / (bottom - top))
        shown_w, shown_h = (right - left) * scale, (bottom - top) * scale
        ox, oy = (self.width - shown_w) / 2, (self.height - shown_h) / 2
        c.saveState()
        c.setFillColor(Color(0.97, 0.97, 0.97))
        c.rect(0, 0, self.width, self.height, stroke=0, fill=1)
        c.drawImage(_reader(self.shot.png), ox, oy, width=shown_w, height=shown_h, mask=None)
        c.setStrokeColor(Color(0.78, 0.78, 0.78))
        c.setLineWidth(0.6)
        c.rect(0, 0, self.width, self.height, stroke=1, fill=0)
        clip = c.beginPath()
        clip.rect(0, 0, self.width, self.height)
        c.clipPath(clip, stroke=0, fill=0)
        x0, y0, x1, y1 = self.shot.spot
        a0 = ox + (x0 - left) * scale - _RING_PAD
        a1 = ox + (x1 - left) * scale + _RING_PAD
        b0 = oy + shown_h - (y1 - top) * scale - _RING_PAD
        b1 = oy + shown_h - (y0 - top) * scale + _RING_PAD
        c.setStrokeColor(self.colour)
        c.setLineWidth(_RING_W)
        w, h = a1 - a0, b1 - b0
        if max(w / max(h, 1.0), h / max(w, 1.0)) >= _LONG:
            c.roundRect(a0, b0, w, h, min(8.0, min(w, h) / 2), stroke=1, fill=0)
        else:
            # An ellipse just round the value (its half-axes 1.3 times the padded box's).
            ex, ey = (a0 + a1) / 2, (b0 + b1) / 2
            rx, ry = w / 2 * 1.3, h / 2 * 1.3
            c.ellipse(ex - rx, ey - ry, ex + rx, ey + ry, stroke=1, fill=0)
        c.restoreState()


class Legend(Flowable):  # type: ignore[misc]
    """The cover's legend, drawn as the marks look on the sheets (#1804): a red cloud round a
    value with its C-number marker, the note's green should-be, and what each means."""

    HEIGHT: Final = 58.0

    def __init__(self, width: float) -> None:
        super().__init__()
        self.width, self.height = width, self.HEIGHT

    def draw(self) -> None:
        c = self.canv
        c.saveState()
        c.setStrokeColor(Color(0.85, 0.85, 0.85))
        c.setLineWidth(0.6)
        c.rect(0, 0, self.width, self.height, stroke=1, fill=0)
        y = self.height / 2
        # The cloud round a value, and its marker.
        c.setFont(_NOTE_FONT, 12)
        c.setFillColor(_DARK)
        c.drawString(32, y - 4, '46"')
        c.setStrokeColor(RED)
        c.setLineWidth(1.4)
        _cloud(c, 26, y - 10, 58, y + 12, 4.0)
        c.setFillColor(RED)
        c.setStrokeColor(white)
        c.circle(24, y + 13, _MARKER_RADIUS, stroke=1, fill=1)
        c.setFillColor(white)
        c.setFont(_NOTE_BOLD, 7.5)
        c.drawCentredString(24, y + 10.4, "07")
        # The note's words.
        x = 74.0
        pieces: list[tuple[str, str, Color]] = [
            ('46"', _NOTE_BOLD, RED),
            ("  should be  ", _NOTE_FONT, _GREY),
            ('37"', _NOTE_BOLD, GREEN),
        ]
        for text, font, colour in pieces:
            c.setFont(font, 11)
            c.setFillColor(colour)
            c.drawString(x, y - 4, text)
            x += _measure(text, font, 11)
        lines = (
            ("Red cloud: ", "the vendor's value that is wrong.", RED),
            ("Green: ", "what it should be (the proof card names the drawing or rule).", GREEN),
            ("07 = C-07: ", "the comment number in the letter and the issue register.", _DARK),
        )
        x = max(x + 24, 230.0)
        for row, (head, rest, colour) in enumerate(lines):
            ty = self.height - 16 - row * 14
            c.setFont(_NOTE_BOLD, 9.5)
            c.setFillColor(colour)
            c.drawString(x, ty, head)
            c.setFont(_NOTE_FONT, 9.5)
            c.setFillColor(_DARK)
            c.drawString(x + _measure(head, _NOTE_BOLD, 9.5), ty, rest)
        c.restoreState()


def _reader(png: bytes) -> Any:
    from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]

    return ImageReader(io.BytesIO(png))


class _Panel(Flowable):  # type: ignore[misc]
    """A proof card's panel with no snippet: words in its middle (the worked sum, GV's rule, the
    reviewer's value), on the same light ground and frame as a snippet."""

    def __init__(self, story: Sequence[Any], width: float, height: float) -> None:
        super().__init__()
        self.story, self.width, self.height = list(story), width, height

    def draw(self) -> None:
        c = self.canv
        c.saveState()
        c.setFillColor(Color(0.97, 0.97, 0.97))
        c.setStrokeColor(Color(0.78, 0.78, 0.78))
        c.setLineWidth(0.6)
        c.rect(0, 0, self.width, self.height, stroke=1, fill=1)
        c.restoreState()
        heights = [part.wrap(self.width - 24, self.height)[1] for part in self.story]
        y = (self.height + sum(heights)) / 2
        for part, h in zip(self.story, heights, strict=True):
            y -= h
            part.drawOn(c, 12, y)


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


def _vendor(value: object) -> str:
    """A value or line as the vendor may read it: as written, unless it holds GV's own words
    (`vendor_text.internal`), then what the guard leaves (#1766)."""
    words = "" if value is None else str(value)
    return words if not internal(words) else (vendor_text(words, "") or "")


def _coloured(label: str, value: str, colour: Color) -> str:
    value = _vendor(value)
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
    action: Any = None,
    action_stamp: str | None = None,
) -> list[Any]:
    body, grey = styles["body"], styles["grey"]
    story: list[Any] = [
        Paragraph(escape(f"Cloud drawing: {set_name}"), styles["title"]),
        Paragraph(escape(stamp), styles["stamp"]),
    ]
    if action is not None:
        # The send-ready package's review result (#1622, `review_action`), as the letter says it,
        # with the standard disclaimer.
        colour = _ACTION_COLOURS.get(action.action, _DARK)
        # The vendor's words: "Under review" while GV's person decides the rest, never approved.
        reason = action.vendor_reason
        why = f"{reason} {action.note}" if action.note else reason
        story += [
            Paragraph(
                f'<font color="{_hex(colour)}"><b>{escape(action_stamp or action.vendor_words)}</b></font>',
                styles["head"],
            ),
            Paragraph(f"{escape(why)} Action required by: {escape(action.required_by)}.", body),
            Paragraph(escape(action.disclaimer), styles["small"]),
            Spacer(1, 8),
        ]
    tiles = [
        ("Wrong", counts["clouded"], RED, "red marks, each with a proof card"),
        # The reviewer's "Not a problem" is Right on the vendor's files (#1766).
        ("Right", counts["right"] + counts["dismissed"], GREEN, "matches the architect"),
        ("Not compared", counts["not_compared"], _LIST_GREY, "listed apart, with the reason"),
    ]
    if counts.get("rule_note"):
        # #1839: decided, never clouded: the architect's size breaks a GV rule; GV asks.
        tiles.insert(
            2,
            (
                "Ask the architect",
                counts["rule_note"],
                _NOTE_SLATE,
                "the architect's size breaks a GV rule",
            ),
        )
    if counts.get("likely_problem"):
        # 2026-10-23: the whole-drawing look's claims code could not prove: never red.
        tiles.insert(
            1,
            (
                LIKELY_SHORT,
                counts["likely_problem"],
                _LIKELY_AMBER,
                "please check and confirm (not proved)",
            ),
        )
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
    grid = Table(
        cells, colWidths=[min(190.0, (width - 72) / len(tiles))] * len(tiles), hAlign="LEFT"
    )
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
    story.append(Paragraph(f"<b>The rule:</b> {escape(RULE_LINE)}", body))
    story.append(Spacer(1, 4))
    story += [Spacer(1, 4), Legend(width - 72), Spacer(1, 4)]
    story.append(
        Paragraph(
            "Page numbers are the vendor file's own pages. Each red mark has a proof card after "
            "the sheets, under its C-number. " + escape(LEGEND_LINES[2]),
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
            Paragraph(f"<b>{escape(tag(item.finding, item.number))}</b>", body),
            Paragraph(escape(str(item.finding.get("page") or "")), body),
            Paragraph(escape(item_words(item.finding)), body),
            Paragraph(_coloured("", vendor_text(vendor_value(item.finding), "") or "", RED), body),
            Paragraph(_coloured("", _cover_should_be(item.finding), GREEN), body),
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
            colWidths=[34, 32, usable * 0.34, usable * 0.22, usable * 0.44 - 66],
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


def _cover_should_be(finding: Mapping[str, Any]) -> str:
    """The cover's green column: `97 1/2" (Architect drawing, arch.pdf page 1)`."""
    value = proof_words.should_text(finding)
    reference = proof_words.reference_words(finding, note=False)
    return vendor_text(f"{value} ({reference})" if value else reference, "") or ""


#: A round-2 status as the cover says it, and its colour (#1603), in the comment letter's words:
#: Closed / Previous comment not addressed / New (#1766).
_ROUND_WORDS: Final[Mapping[str, tuple[str, Color]]] = {
    "fixed": ("Closed", GREEN),
    "still_wrong": ("Previous comment not addressed", RED),
    "new": ("New", RED),
    "also_in_round_1": ("Previous comment not addressed (also in round 1)", RED),
    "now_right": ("Now right (was not compared)", GREEN),
    "still_not_compared": ("Still not compared", _LIST_GREY),
}


def _round_section(
    styles: Mapping[str, ParagraphStyle],
    rounds: Any,
    numbers: Mapping[str, str],
    width: float,
) -> list[Any]:
    """The cover's round-2 summary (#1603): one line, then each item code compared."""
    line = rounds.line() if rounds is not None else None
    if line is None:
        return []
    body = styles["body"]
    story: list[Any] = [
        Paragraph("Round 2: what the vendor did", styles["section"]),
        Paragraph(escape(_vendor(line)), body),
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
                Paragraph(escape(_vendor(item.item)), body),
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
                38,
                36,
                usable * 0.3,
                usable * 0.14,
                usable * 0.14,
                usable * 0.14,
                usable * 0.28 - 74,
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


#: A proof card: its inner padding, the gap between its two panels, and the room its words take
#: besides the panels (points), so two cards fit on a page.
_CARD_PAD: Final = 10.0
_PANEL_GAP: Final = 16.0
_CARD_WORDS_PT: Final = 168.0


def panel_size(size: tuple[float, float]) -> tuple[float, float]:
    """A proof card's panel (width, height) on a page of `size`: half the card each, about twice
    as wide as tall, low enough that two cards fit on a page."""
    width, height = size
    usable = width - 72
    pw = (usable - 2 * _CARD_PAD - _PANEL_GAP) / 2
    ph = min(pw * 0.45, max(100.0, (height - 72 - 48) / 2 - _CARD_WORDS_PT))
    return pw, ph


def _runs_markup(runs: Sequence[proof_words.Run]) -> str:
    """The sentence's pieces as Paragraph markup: the vendor's value red, the should-be green."""
    out: list[str] = []
    for words, role in runs:
        text = escape(_vendor(words) if role != "plain" else words)
        if role == "vendor":
            out.append(f'<font color="{_hex(RED)}"><b>{text}</b></font>')
        elif role == "should":
            out.append(f'<font color="{_hex(GREEN)}"><b>{text}</b></font>')
        else:
            out.append(text)
    return "".join(out)


_REFERENCE_CAPTIONS: Final[Mapping[str, str]] = {
    "architect": "Architect drawing",
    "prototype": "Brand prototype (reference)",
    "span": "Vendor's other dimension of the same span",
    "pages": "Vendor's other drawing of the same item",
    "sum": "The vendor's own parts, added up",
    "rule": "GV rule",
    "reviewer": "GV's reviewer",
}
_REFERENCE_SHOWS: Final[Mapping[str, str]] = {
    "architect": "Architect shows ",
    "prototype": "Brand prototype shows ",
    "span": "Other dimension shows ",
    "pages": "One of them is wrong: ",
    "sum": "Parts add up to ",
    "rule": "GV rule requires ",
    "reviewer": "Should be ",
}


def _card_styles(styles: Mapping[str, ParagraphStyle]) -> dict[str, ParagraphStyle]:
    body = styles["body"]
    return {
        "tag": ParagraphStyle(
            "tag", parent=body, fontName=_NOTE_BOLD, fontSize=10, leading=12, textColor=white,
            alignment=1,
        ),
        "title": ParagraphStyle("card_title", parent=body, fontName=_NOTE_BOLD, fontSize=11.5,
                                leading=14),
        "where": ParagraphStyle("card_where", parent=body, fontSize=8.5, leading=11,
                                textColor=_LIST_GREY, alignment=2),
        "sentence": ParagraphStyle("sentence", parent=body, fontSize=12, leading=15.5),
        "caption": ParagraphStyle("card_caption", parent=body, fontName=_NOTE_BOLD, fontSize=7.5,
                                  leading=10, textColor=_LIST_GREY),
        "under": ParagraphStyle("under", parent=body, fontSize=9.5, leading=12),
        "label": ParagraphStyle("fact_label", parent=body, fontName=_NOTE_BOLD, fontSize=8.5,
                                leading=11, textColor=_LIST_GREY),
        "fact": ParagraphStyle("fact", parent=body, fontSize=8.5, leading=11),
        "big": ParagraphStyle("big", parent=body, fontName=_NOTE_BOLD, fontSize=14, leading=18),
        "line": ParagraphStyle("panel_line", parent=body, fontSize=11, leading=14),
    }  # fmt: skip


def _where_words(finding: Mapping[str, Any], areas: Mapping[int, str]) -> str:
    """ "Vendor page 4 · Kitchenette": the vendor's page and its area, when known."""
    page = finding.get("page")
    if not page:
        return ""
    area = areas.get(int(page), "") if isinstance(page, int) else ""
    area = vendor_text(area, "") or ""
    return f"Vendor page {page}" + (f" · {area}" if area else "")


def _reference_caption(finding: Mapping[str, Any], kind: str) -> str:
    words = _REFERENCE_CAPTIONS[kind]
    box = _box_of(finding.get("arch_box"))
    if kind in ("architect", "prototype", "span", "pages") and box is not None:
        separate = architect_file(finding)
        if separate is not None:
            return f"{words} · {separate} page {box[1]}"
        if box[1] != finding.get("page"):
            return f"{words} · page {box[1]}"
        return f"{words} · same sheet"
    return words


def _sum_panel(
    finding: Mapping[str, Any], cs: Mapping[str, ParagraphStyle], pw: float, ph: float
) -> Any:
    parts = [_vendor(str(part)) for part in finding.get("parts") or [] if _vendor(str(part))]
    total = _vendor(should_be_value(finding))
    printed = _vendor(vendor_value(finding))
    wrong = wrong_part(finding)
    overall = _vendor(str(finding.get("printed_total") or ""))
    if wrong is not None and printed in parts and overall:
        # The drawing names the wrong part (#1887): the overall less the other parts.
        rest = list(parts)
        rest.remove(printed)
        parts = [overall, *(f"- {part}" for part in rest)]
    story: list[Any] = []
    if parts and len(parts) * cs["line"].leading + 40 <= ph:
        story += [
            Paragraph(escape(("+ " if n and wrong is None else "") + part), cs["line"])
            for n, part in enumerate(parts)
        ]
    elif parts:
        story.append(
            Paragraph(escape((" " if wrong is not None else " + ").join(parts)), cs["line"])
        )
    story += [
        Paragraph(_coloured("= ", total, GREEN), cs["big"]),
        Paragraph(_coloured("printed ", printed, RED), cs["under"]),
    ]
    return _Panel(story, pw, ph)


def _words_panel(
    finding: Mapping[str, Any],
    kind: str,
    cs: Mapping[str, ParagraphStyle],
    pw: float,
    ph: float,
) -> Any:
    """The right panel when there is no snippet of the reference: GV's rule, GV's reviewer, or the
    architect's value with no spot to show."""
    should = proof_words.should_text(finding)
    story: list[Any] = [Paragraph(_coloured("", should, GREEN), cs["big"])]
    worked = proof_words.worked_line(finding)
    if worked:
        story.append(Paragraph(escape(worked), cs["fact"]))
    if kind == "reviewer":
        story.append(
            Paragraph(escape("No drawing prints this value: GV's reviewer gave it."), cs["fact"])
        )
    elif kind == "architect":
        printed = _vendor(str(finding.get("architect") or ""))
        if printed and printed != should:
            story.append(Paragraph(escape(f"Printed {printed}"), cs["fact"]))
        story.append(Paragraph(escape("No picture of the architect's spot."), cs["fact"]))
    return _Panel(story, pw, ph)


def _where_marked(where: str, label: str) -> str:
    """Where the cloud of an item with no vendor's spot is: "The C-09 cloud is on page 17, at the
    architect's value." (the placing's "Marked on page 17."), else the placing's own words."""
    found = re.match(r"Marked on page (\d+)\.?$", where.strip())
    if found:
        return f"The {label} cloud is on page {found.group(1)}, at the architect's value."
    return _vendor(where)


def _card(
    item: _ProofItem,
    styles: Mapping[str, ParagraphStyle],
    usable: float,
    panel: tuple[float, float],
    areas: Mapping[int, str],
) -> Any:
    """One red mark's proof card (#1804): its C-number and title, the one sentence, the vendor's
    snippet ringed red beside the reference ringed green, then the sum, the reference and how it
    was checked."""
    cs = _card_styles(styles)
    finding = item.finding
    pw, ph = panel
    kind = proof_words.reference_kind(finding)
    inner = usable - 2 * _CARD_PAD
    rounds = finding.get(_ROUND_KEY)
    title = escape(item_words(finding))
    if rounds is not None and getattr(rounds, "status", None) == "new":
        title = f'<font color="{_hex(RED)}">New</font>&nbsp;&nbsp;{title}'
    pill = Table([[Paragraph(escape(tag(finding, item.number)), cs["tag"])]], colWidths=[46])
    pill.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), RED),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    head = Table(
        [[pill, Paragraph(title, cs["title"]), Paragraph(escape(_where_words(finding, areas)), cs["where"])]],
        colWidths=[56, inner - 56 - 190, 190],
    )  # fmt: skip
    head.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    said = Paragraph(_runs_markup(proof_words.sentence_runs(finding)), cs["sentence"])
    vendor_words = _vendor(vendor_value(finding))
    absent = proof_words.missing(vendor_words)
    shown = (
        _coloured("", "Not on the vendor's sheets", RED)
        if absent
        else _coloured("Vendor shows ", vendor_words, RED)
    )
    if item.vendor is not None:
        left: Any = _Snippet(item.vendor, pw, ph, RED)
    else:
        why = (
            "Every vendor sheet of the area was searched for it."
            if absent
            else "No picture of the vendor's spot."
        )
        left = _Panel([Paragraph(shown, cs["big"]), Paragraph(escape(why), cs["fact"])], pw, ph)
    if item.reference is not None:
        right: Any = _Snippet(item.reference, pw, ph, GREEN)
    elif kind == "sum":
        right = _sum_panel(finding, cs, pw, ph)
    else:
        right = _words_panel(finding, kind, cs, pw, ph)
    vendor_page = finding.get("page")
    shows = _REFERENCE_SHOWS[kind]
    panels = Table(
        [
            [
                Paragraph(escape(f"VENDOR'S DRAWING · PAGE {vendor_page}".upper()), cs["caption"]),
                Paragraph(escape(_reference_caption(finding, kind).upper()), cs["caption"]),
            ],
            [left, right],
            [
                Paragraph(shown, cs["under"]),
                Paragraph(_coloured(shows, proof_words.should_text(finding), GREEN), cs["under"]),
            ],
        ],
        colWidths=[pw + _PANEL_GAP, pw],
        hAlign="LEFT",
    )
    panels.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    facts: list[tuple[str, str]] = []
    summed = proof_words.sum_line(finding)
    if summed:
        facts.append(("Sum", summed))
    worked = proof_words.worked_line(finding)
    if worked and kind != "rule":
        facts.append(("Worked out", worked))
    reference = proof_words.reference_words(finding)
    facts.append(("Reference", reference))
    checked = proof_words.checked_words(finding)
    if not (checked == proof_words.REVIEWER_WORDS and reference.startswith(checked)):
        facts.append(("Checked", checked))
    if item.vendor is None and item.where:
        facts.append(("Where", _where_marked(item.where, tag(finding, item.number))))
    rows = [
        [Paragraph(escape(label), cs["label"]), Paragraph(escape(_vendor(text)), cs["fact"])]
        for label, text in facts
        if _vendor(text)
    ]
    table = Table(rows, colWidths=[70, inner - 70], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    card = Table(
        [[head], [said], [panels], [table]],
        colWidths=[usable],
        hAlign="LEFT",
    )
    card.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, Color(0.8, 0.8, 0.8)),
                ("LINEBEFORE", (0, 0), (0, -1), 3, RED),
                ("LEFTPADDING", (0, 0), (-1, -1), _CARD_PAD),
                ("RIGHTPADDING", (0, 0), (-1, -1), _CARD_PAD),
                ("TOPPADDING", (0, 0), (-1, 0), _CARD_PAD),
                ("TOPPADDING", (0, 1), (-1, -1), 6),
                ("BOTTOMPADDING", (0, -1), (-1, -1), _CARD_PAD),
            ]
        )
    )
    return card


def _proof_entries(
    styles: Mapping[str, ParagraphStyle],
    items: Sequence[_ProofItem],
    width: float,
    panel: tuple[float, float],
    areas: Mapping[int, str] | None = None,
) -> list[Any]:
    """The proof pages: one card per red mark, in the clouded order, two to a page."""
    usable = width - 72
    story: list[Any] = [
        Paragraph("Proof: how each red mark was checked", styles["section"]),
        Paragraph(
            escape(
                "Each card: on the left the vendor's drawing with the wrong value ringed in red; "
                "on the right what it should be, ringed in green, and where that comes from."
            ),
            styles["grey"],
        ),
        Spacer(1, 6),
    ]
    for item in items:
        story.append(KeepTogether([_card(item, styles, usable, panel, areas or {}), Spacer(1, 12)]))
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
                f"{escape(item_words(finding))}</b>: {escape(_vendor(line(finding)))}",
                style,
            )
        )
    return story


def _right_line(finding: Mapping[str, Any]) -> str:
    source = "architect" if finding.get("source") in (None, "architect", "gv_review") else ""
    source = source or str(finding.get("source") or "").replace("_", " ")
    return f"{finding.get('vendor')} matches {source} {finding.get('architect')}"


def _reviewer_right_line(finding: Mapping[str, Any]) -> str:
    """An item the reviewer marked not a problem, in the Right list (#1766): "vendor 46";
    architect 3'-1". Right (reviewer): <note>"."""
    words = (
        f"vendor {_vendor(finding.get('vendor'))}; architect {_vendor(finding.get('architect'))}. "
    )
    return words + reviewer_right(dict(finding)) + "."


def _rule_note_line(finding: Mapping[str, Any]) -> str:
    """A rule note on the notes pages (#1839): "the architect specifies 5/8", as the vendor shows;
    GV's standard is 1"-2". GV asks the architect; no change is asked of the vendor"."""
    note = proof_of(finding).get("rule_note") or {}
    return (
        f"the architect specifies {note.get('architect')}, as the vendor shows; "
        f"{note.get('standard')}. GV asks the architect; no change is asked of the vendor"
    )


def _likely_line(finding: Mapping[str, Any]) -> str:
    """A likely problem on the notes pages: what to check, without the letter's lead words."""
    words = please_check(finding)
    return words.split(": ", 1)[1] if words.startswith(LIKELY_LEAD) else words


def _right_or_reviewer_line(finding: Mapping[str, Any]) -> str:
    if proof_group(finding) == "dismissed":
        return _reviewer_right_line(finding)
    return _right_line(finding)


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
    action: Any = None,
    action_stamp: str | None = None,
    not_compared: int | None = None,
    areas: Mapping[int, str] | None = None,
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
    `action`: the review result (`review_action.ReviewAction`, #1622) and `action_stamp` its stamp
    line, said under the stamp with the disclaimer. A finding's `comment_no` ("C-07", the
    package's stable comment number) is printed on its cloud, note and entries instead of its
    place in the list.
    `not_compared`: how many rows the Not-compared list has (its product scope), shown on the
    cover; else counted here. `areas`: the vendor's page -> its area's title, named on each proof
    card ("Vendor page 4 · Kitchen"). Returns the counts the cover shows."""
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
        _tighten(source, marks, spots)
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
                    _layout(
                        _shown_size(pdf.pages[number - 1]),
                        items,
                        _ink(document, number - 1),
                        [
                            *number_boxes(document, number - 1),
                            *_digit_cells(source, number - 1),
                        ],
                    )
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
        panel = panel_size(_shown_size(pdf.pages[0]))
        for number, finding, page, box, where in clouded:
            vendor_shot = None
            # Only the vendor's own spot is ringed red: an item placed at the architect's spot
            # (missing from the vendor's sheets) gets the words "not on the vendor's sheets".
            if (
                page is not None
                and box is not None
                and isinstance(finding.get("vendor_box"), Mapping)
            ):
                spot = cloud_spot.get(number, box)
                # A sum's snippet shows as much of its string as it can (the AI's box).
                view = box if proof_words.reference_kind(finding) == "sum" else None
                vendor_shot = snippet(crop_source, page, spot, panel, sizes, view)
            reference_shot = None
            arch = _box_of(finding.get("arch_box"))
            kind = proof_words.reference_kind(finding)
            if arch is not None and kind in ("architect", "prototype", "span", "pages"):
                name, arch_page, arch_area = arch
                arch_pdf = known.get(name) if name else source
                if arch_pdf == source:
                    arch_pdf = crop_source
                if arch_pdf is not None and arch_pdf.is_file():
                    tight = _tight(
                        arch_pdf, arch_page, arch_area, str(finding.get("architect") or ""), budget
                    )
                    reference_shot = snippet(arch_pdf, arch_page, tight, panel, sizes)
            proof_items.append(_ProofItem(number, finding, where, vendor_shot, reference_shot))
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
                f"Not compared ({nothing}): listed with the reason and what would settle "
                f'it in the separate list, "{set_name} - {NOT_COMPARED_TITLE}".'
            ),
            styles["grey"],
        )
    )
    noted = groups.get("rule_note", [])
    if noted:
        # #1839: decided, never clouded; nothing is asked of the vendor.
        notes += _list_entries(styles, RULE_NOTE_HEADING, noted, _rule_note_line, _NOTE_SLATE)
    likely = groups.get(LIKELY_GROUP, [])
    if likely:
        # 2026-10-23: the whole-drawing look's claims code could not prove: please check, never
        # a correction (no cloud, no "should be").
        notes += _list_entries(styles, LIKELY_HEADING, likely, _likely_line, _LIKELY_AMBER)
    # The reviewer's "Not a problem" is Right on the vendor's files, noted "(reviewer)" (#1766).
    right = [*groups.get("right", []), *groups.get("dismissed", [])]
    notes += _list_entries(styles, "Right", right, _right_or_reviewer_line, GREEN)
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
                f"<b>{escape(_vendor(item.item))}</b>: was {escape(_vendor(item.was))}; should be "
                f"{escape(_vendor(item.should_be))}. Why: "
                f"{escape(vendor_text(item.reason, 'the drawings do not settle it') or '')}",
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
                f"<b>p.{escape(str(e.get('page')))}&nbsp;&nbsp;{escape(_vendor(e.get('item')))}"
                f"</b>: {escape(_vendor(' '.join(settled_entry_lines(e))))}",
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
        action=action,
        action_stamp=action_stamp,
    )
    numbers = {str(item.finding.get("id")): tag(item.finding, item.number) for item in proof_items}
    cover += _round_section(styles, rounds, numbers, size[0])
    parts = [("Cover", cover)]
    if proof_items:
        parts.append(
            ("Proof", _proof_entries(styles, proof_items, size[0], panel_size(size), areas))
        )
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
