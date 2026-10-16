"""Evidence crops and the marked-up PDF of a chat answer's findings (chat v2), drawn by code.

**Loaded by name, never imported.** `app/api/` must not reach a PDF renderer by import
(`tests/api/test_no_heavy_work.py`); `chat_findings.py` loads this module with `import_module` the
first time a finding needs a picture or a reviewer asks for the marked-up PDF, as
`page_pictures.py` loads its renderer.

**Boxes are in PDF points of the page as it is shown** (the crop box, after the page's rotation),
origin top-left: what `pdftotext -bbox` prints and what a picture measures (points = pixels x 72 /
dpi). `crop_png` cuts that area (padded) from the original upload page; `markup_pdf` turns it back
into the page's own drawing space (rotation and box origin included) to draw a cloud there.

**pdfium is not thread-safe**: every use of it here holds one lock (helpers report findings from
their own threads, and a reviewer may ask for a marked-up PDF while an answer is being written).
"""

from __future__ import annotations

import io
import logging
import textwrap
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color, white  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant.chat_trust import cloud_kind, needs_text

__all__ = [
    "MARKUP_TITLE",
    "REPORT_TITLE",
    "check_report_pdf",
    "cloud_kind",
    "crop_png",
    "markup_pdf",
    "needs_text",
    "page_sizes",
]

_log = logging.getLogger(__name__)

MARKUP_TITLE: Final = "AI markup: draft, not GV's signed review"
#: Evidence crops: this many dots per inch, padded by this many points, at most this many pixels a
#: side (a big box is drawn at a lower dpi rather than cut).
CROP_DPI: Final = 170
CROP_PAD_PT: Final = 24.0
CROP_MAX_PX: Final = 1600
_RED: Final = Color(0.85, 0.05, 0.05)
_AMBER: Final = Color(0.85, 0.5, 0.0)
_CLOUD_PAD: Final = 6.0
_CLOUD_RADIUS: Final = 5.0
_NOTE_SIZE: Final = 8.0
_NOTE_CHARS: Final = 110
_PDFIUM_LOCK: Final = threading.Lock()

type Box = tuple[float, float, float, float]


def page_sizes(pdf: Path) -> list[tuple[float, float]]:
    """Each page's shown size in points (width, height), rotation applied."""
    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            return [tuple(document[n].get_size()) for n in range(len(document))]
        finally:
            document.close()


def crop_png(pdf: Path, page_number: int, box: Box) -> bytes | None:
    """The area `box` (x0, y0, x1, y1; points, origin top-left) of a page, padded, as a PNG; `None`
    when the page does not exist or the box is outside it."""
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


# ---- the marked-up PDF ---------------------------------------------------------------------------


def _shown_to_user(page: Any, x: float, y: float) -> tuple[float, float]:
    """A shown point (points, origin top-left of the shown page) in the page's drawing space."""
    box = page.cropbox
    llx, lly, urx, ury = (float(v) for v in (box.left, box.bottom, box.right, box.top))
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    if rotation == 90:
        return llx + y, lly + x
    if rotation == 180:
        return urx - x, lly + y
    if rotation == 270:
        return urx - y, ury - x
    return llx + x, ury - y


def _cloud(c: canvas.Canvas, x0: float, y0: float, x1: float, y1: float) -> None:
    """Scallops bulging outward along the four edges of a box (drawing space, y up)."""
    r = _CLOUD_RADIUS
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


def _note_text(number: int, finding: Mapping[str, Any]) -> str:
    vendor, architect = finding.get("vendor"), str(finding.get("architect"))
    if cloud_kind(finding) == "wrong":
        needs = needs_text(finding)
        text = f"AI {number} {finding.get('item')}: Vendor: {vendor} \u2014 Needs: {needs}"
        if needs != architect:
            text += f" (architect {architect})"
    else:
        text = f"AI {number} ? Check: {finding.get('item')}: vendor {vendor}; architect {architect}"
    return text if len(text) <= _NOTE_CHARS else text[: _NOTE_CHARS - 1] + "\u2026"


def _overlay(width: float, height: float, draw: Any) -> Any:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height), invariant=1)
    draw(c)
    c.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def _draw_marks(
    c: canvas.Canvas, page: Any, marks: Sequence[tuple[int, Mapping[str, Any], Box]]
) -> None:
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    for number, finding, (x0, y0, x1, y1) in marks:
        ax, ay = _shown_to_user(page, min(x0, x1), min(y0, y1))
        bx, by = _shown_to_user(page, max(x0, x1), max(y0, y1))
        left, right = min(ax, bx) - _CLOUD_PAD, max(ax, bx) + _CLOUD_PAD
        low, high = min(ay, by) - _CLOUD_PAD, max(ay, by) + _CLOUD_PAD
        wrong = cloud_kind(finding) == "wrong"
        colour = _RED if wrong else _AMBER
        c.setStrokeColor(colour)
        c.setLineWidth(1.2)
        _cloud(c, left, low, right, high)
        # The note reads left to right on the shown page, just right of the box's top corner; a
        # wrong item's note starts with a drawn cross (the standard fonts have no such sign).
        note = _note_text(number, finding)
        nx, ny = _shown_to_user(page, max(x0, x1) + _CLOUD_PAD + 8, min(y0, y1) - _CLOUD_PAD)
        c.saveState()
        c.translate(nx, ny)
        c.rotate(rotation)
        c.setFont("Helvetica-Bold", _NOTE_SIZE)
        lead = _NOTE_SIZE + 2 if wrong else 0
        width = c.stringWidth(note, "Helvetica-Bold", _NOTE_SIZE) + lead
        c.setFillColor(white)
        c.rect(-2, -_NOTE_SIZE - 2, width + 4, _NOTE_SIZE + 4, stroke=0, fill=1)
        c.setFillColor(colour)
        if wrong:
            c.setLineWidth(1.4)
            c.line(0, -_NOTE_SIZE + 1, _NOTE_SIZE - 2, -1)
            c.line(0, -1, _NOTE_SIZE - 2, -_NOTE_SIZE + 1)
        c.drawString(lead, -_NOTE_SIZE + 1, note)
        c.restoreState()


def _stamp(c: canvas.Canvas, page: Any) -> None:
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    x, y = _shown_to_user(page, 24, 30)
    c.saveState()
    c.translate(x, y)
    c.rotate(rotation)
    c.setFont("Helvetica-Bold", 14)
    width = c.stringWidth(MARKUP_TITLE, "Helvetica-Bold", 14)
    c.setFillColor(white)
    c.rect(-3, -5, width + 6, 20, stroke=0, fill=1)
    c.setFillColor(_RED)
    c.drawString(0, 0, MARKUP_TITLE)
    c.restoreState()


def _notes_pages(width: float, height: float, lines: Sequence[tuple[str, bool]]) -> list[Any]:
    """The notes listed again, on as many pages as they need (bold: a heading line)."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height), invariant=1)
    wrap = max(40, int((width - 72) / 5.2))

    def heading() -> float:
        c.setFillColor(_RED)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(36, height - 40, MARKUP_TITLE)
        c.setFillColor(Color(0, 0, 0))
        return height - 64

    y = heading()
    for text, bold in lines:
        for n, part in enumerate(textwrap.wrap(text, wrap) or [""]):
            if y < 40:
                c.showPage()
                y = heading()
            c.setFont("Helvetica-Bold" if bold and n == 0 else "Helvetica", 9.5)
            c.drawString(36 if n == 0 else 52, y, part)
            y -= 13
        y -= 3
    c.save()
    buffer.seek(0)
    return list(PdfReader(buffer).pages)


LEGEND: Final = (
    "Red cloud with a cross: the vendor's value is wrong; the note says what it needs to be. "
    "Amber cloud with ?: to check. Right items are not clouded; they are listed below."
)


def markup_pdf(
    source: Path,
    findings: Sequence[tuple[int, Mapping[str, Any], int | None, Box | None, str]],
    out: Path,
) -> None:
    """A copy of `source` with a cloud and a short note at each placed finding that is not right
    (red: wrong, amber: to check), the draft stamp on page 1, and a legend and notes page listing
    every finding. `findings`: (number, finding, page, box, where) with `page`/`box` `None` when
    the finding cannot be placed on this file (`where` says why, for the notes page)."""
    writer = PdfWriter(clone_from=PdfReader(str(source)))
    by_page: dict[int, list[tuple[int, Mapping[str, Any], Box]]] = {}
    listed: list[tuple[int, Mapping[str, Any], str]] = []
    for number, finding, page_number, box, where in findings:
        if cloud_kind(finding) is None:
            where = "Right: not clouded."
        elif page_number is not None and box is not None:
            if 1 <= page_number <= len(writer.pages):
                by_page.setdefault(page_number, []).append((number, finding, box))
            else:
                where = f"Not marked: the file has no page {page_number}."
        listed.append((number, finding, where))
    for n, page in enumerate(writer.pages, start=1):
        marks = by_page.get(n, [])
        if not marks and n != 1:
            continue
        media = page.mediabox
        size = (float(media.right), float(media.top))

        def draw(c: canvas.Canvas, page: Any = page, marks: Any = marks, n: int = n) -> None:
            if marks:
                _draw_marks(c, page, marks)
            if n == 1:
                _stamp(c, page)

        page.merge_page(_overlay(size[0], size[1], draw))
    first = writer.pages[0]
    width, height = float(first.cropbox.width), float(first.cropbox.height)
    if int(first.get("/Rotate", 0) or 0) % 180 == 90:
        width, height = height, width
    lines: list[tuple[str, bool]] = [(LEGEND, False)]
    for number, finding, where in listed:
        kind = {"wrong": "Wrong", "check": "To check", None: "Right"}[cloud_kind(finding)]
        checked = str(finding.get("trust_level") or ("verified" if finding.get("verified") else ""))
        vs = f" (architect page {finding['vs_page']})" if finding.get("vs_page") else ""
        head = f"AI {number}  page {finding.get('page')}{vs}  {finding.get('item')}  "
        lines.append((head + f"[{kind}{', ' + checked if checked else ''}]", True))
        lines.append(
            (f"Architect: {finding.get('architect')}   Vendor: {finding.get('vendor')}", False)
        )
        if cloud_kind(finding) == "wrong":
            lines.append((f"Needs: {needs_text(finding)}", False))
        if finding.get("why"):
            lines.append((f"Why: {finding['why']}", False))
        lines.append((where, False))
    for page in _notes_pages(width, height, lines):
        writer.add_page(page)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)


# ---- the check report ----------------------------------------------------------------------------

REPORT_TITLE: Final = "AI check report: draft, not GV's signed review"


def check_report_pdf(
    out: Path,
    summary: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    not_checked: Sequence[str],
) -> None:
    """The full check's report: a cover (set, products, date, model and effort, counts, coverage,
    trust), then What's wrong, To check, What's right and Not checked."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import landscape, letter  # type: ignore[import-untyped]
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
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

    story: list[Any] = [Paragraph(escape(REPORT_TITLE), styles["Title"])]
    for label, value in summary.items():
        story.append(Paragraph(f"<b>{escape(str(label))}:</b> {escape(str(value))}", body))
    number = {id(f): n for n, f in enumerate(findings, start=1)}
    wrong = [f for f in findings if cloud_kind(f) == "wrong"]
    check = [f for f in findings if cloud_kind(f) == "check"]
    right = [f for f in findings if cloud_kind(f) is None]
    sections = (
        (
            f"What's wrong ({len(wrong)})",
            ["AI", "Page", "Item", "Vendor", "Needs", "Architect", "Source", "Trust"],
            [
                [
                    number[id(f)],
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    needs_text(f),
                    f.get("architect"),
                    f.get("source"),
                    f.get("trust_level"),
                ]
                for f in wrong
            ],
            [26, 34, 170, 90, 70, 90, 80, 60],
        ),
        (
            f"To check ({len(check)})",
            ["AI", "Page", "Item", "Vendor", "Architect", "Why", "Trust"],
            [
                [
                    number[id(f)],
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    f.get("architect"),
                    f.get("why"),
                    f.get("trust_level"),
                ]
                for f in check
            ],
            [26, 34, 170, 90, 90, 150, 60],
        ),
        (
            f"What's right ({len(right)})",
            ["AI", "Page", "Item", "Value", "Matches"],
            [
                [
                    number[id(f)],
                    f.get("page"),
                    f.get("item"),
                    f.get("vendor"),
                    f"{f.get('source')}: {f.get('architect')}",
                ]
                for f in right
            ],
            [26, 34, 230, 120, 210],
        ),
    )
    for title, header, rows, widths in sections:
        story += [Spacer(1, 10), Paragraph(escape(title), styles["Heading2"])]
        story.append(table(header, rows, widths) if rows else p("None.", body))
    story += [Spacer(1, 10), Paragraph("Not checked", styles["Heading2"])]
    story += [p(line, body) for line in not_checked] or [p("Nothing left out.", body)]
    story.insert(len(summary) + 1, PageBreak())
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
