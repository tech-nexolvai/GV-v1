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

__all__ = ["MARKUP_TITLE", "crop_png", "markup_pdf", "page_sizes"]

_log = logging.getLogger(__name__)

MARKUP_TITLE: Final = "AI markup: draft, not GV's signed review"
#: Evidence crops: this many dots per inch, padded by this many points, at most this many pixels a
#: side (a big box is drawn at a lower dpi rather than cut).
CROP_DPI: Final = 170
CROP_PAD_PT: Final = 24.0
CROP_MAX_PX: Final = 1600
_RED: Final = Color(0.85, 0.05, 0.05)
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
    text = (
        f"AI {number}: {finding.get('item')} — architect {finding.get('architect')}; "
        f"vendor {finding.get('vendor')}"
    )
    return text if len(text) <= _NOTE_CHARS else text[: _NOTE_CHARS - 1] + "…"


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
        c.setStrokeColor(_RED)
        c.setLineWidth(1.2)
        _cloud(c, left, low, right, high)
        # The note reads left to right on the shown page, just right of the box's top corner.
        note = _note_text(number, finding)
        nx, ny = _shown_to_user(page, max(x0, x1) + _CLOUD_PAD + 8, min(y0, y1) - _CLOUD_PAD)
        c.saveState()
        c.translate(nx, ny)
        c.rotate(rotation)
        c.setFont("Helvetica-Bold", _NOTE_SIZE)
        width = c.stringWidth(note, "Helvetica-Bold", _NOTE_SIZE)
        c.setFillColor(white)
        c.rect(-2, -_NOTE_SIZE - 2, width + 4, _NOTE_SIZE + 4, stroke=0, fill=1)
        c.setFillColor(_RED)
        c.drawString(0, -_NOTE_SIZE + 1, note)
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


def markup_pdf(
    source: Path,
    findings: Sequence[tuple[int, Mapping[str, Any], int | None, Box | None, str]],
    out: Path,
) -> None:
    """A copy of `source` with a red cloud and a short note at each placed finding, the draft stamp
    on page 1, and a notes page listing every finding. `findings`: (number, finding, page, box,
    where) with `page`/`box` `None` when the finding cannot be placed on this file (`where` says
    why, for the notes page)."""
    writer = PdfWriter(clone_from=PdfReader(str(source)))
    by_page: dict[int, list[tuple[int, Mapping[str, Any], Box]]] = {}
    listed: list[tuple[int, Mapping[str, Any], str]] = []
    for number, finding, page_number, box, where in findings:
        if page_number is not None and box is not None:
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
    lines: list[tuple[str, bool]] = []
    for number, finding, where in listed:
        status = str(finding.get("status") or "")
        checked = "verified" if finding.get("verified") else "not verified"
        vs = f" (architect page {finding['vs_page']})" if finding.get("vs_page") else ""
        head = f"AI {number}  page {finding.get('page')}{vs}  {finding.get('item')}  "
        lines.append((head + f"[{status}, {checked}]", True))
        lines.append(
            (f"Architect: {finding.get('architect')}   Vendor: {finding.get('vendor')}", False)
        )
        if finding.get("why"):
            lines.append((f"Why: {finding['why']}", False))
        lines.append((where, False))
    for page in _notes_pages(width, height, lines):
        writer.add_page(page)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)
