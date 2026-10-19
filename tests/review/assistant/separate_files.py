"""Synthetic separate files for the separate-PDF tests (#1719): plain CAD-like sheets, no client file.

Each sheet is a page drawn with reportlab, the way a CAD export prints it: no pasted drawing (no
`/Stamp`), no GV heading; a title ("ARCH ELEVATION", "VENDOR ELEVATION"), an optional printed
reference ("REF. ELEV 2/A-501"), and a cabinet run with a chain of widths and its overall on two
dimension lines (ticks, extension lines, the values as text). A cover sheet prints words only.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise

from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

PAGE = (1224.0, 792.0)


@dataclass(frozen=True)
class Sheet:
    """One page: a title, an optional reference and a run of widths (inches) at `scale` points per
    inch, its left end at `x` points."""

    title: str
    widths: Sequence[float | Fraction] = ()
    reference: str | None = None
    scale: float = 6.0
    x: float = 200.0
    words: Sequence[str] = field(default_factory=tuple)
    height_in: float = 34.5


def _inches(value: float | Fraction) -> str:
    whole = int(value)
    rest = Fraction(value) - whole
    if not rest:
        return f'{whole}"'
    return f'{whole} {rest.numerator}/{rest.denominator}"' if whole else f'{rest}"'


def _sheet(page: canvas.Canvas, sheet: Sheet) -> None:
    _width, height = PAGE
    page.setFont("Helvetica-Bold", 18)
    page.drawString(60, 60, sheet.title)
    page.setFont("Helvetica", 10)
    if sheet.reference:
        page.drawString(60, 40, sheet.reference)
    for n, words in enumerate(sheet.words):
        page.drawString(60, height - 80 - 14 * n, words)
    if not sheet.widths:
        return
    s, x0 = sheet.scale, sheet.x
    floor = 200.0
    top = floor + sheet.height_in * s
    page.setLineWidth(1)
    edges = [x0]
    for w in sheet.widths:
        edges.append(edges[-1] + float(w) * s)
    # The cabinets.
    for left, right in pairwise(edges):
        page.rect(left, floor, right - left, top - floor)
    # Extension lines up to both dimension lines.
    chain_y, overall_y = top + 40, top + 80
    page.setLineWidth(0.3)
    for edge in edges:
        page.line(edge, top + 5, edge, chain_y + 6)
    for edge in (edges[0], edges[-1]):
        page.line(edge, chain_y + 6, edge, overall_y + 6)
    page.setFont("Helvetica", 9)
    for left, right, w in zip(edges, edges[1:], sheet.widths, strict=False):
        page.line(left, chain_y, right, chain_y)
        page.line(left - 3, chain_y - 3, left + 3, chain_y + 3)
        page.line(right - 3, chain_y - 3, right + 3, chain_y + 3)
        page.drawCentredString((left + right) / 2, chain_y + 3, _inches(w))
    page.line(edges[0], overall_y, edges[-1], overall_y)
    page.line(edges[0] - 3, overall_y - 3, edges[0] + 3, overall_y + 3)
    page.line(edges[-1] - 3, overall_y - 3, edges[-1] + 3, overall_y + 3)
    total = sum(Fraction(w) for w in sheet.widths)
    page.drawCentredString((edges[0] + edges[-1]) / 2, overall_y + 3, _inches(total))


def plain_pdf(sheets: Sequence[Sheet]) -> bytes:
    """A plain CAD-like PDF: one page per sheet."""
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=PAGE)
    for sheet in sheets:
        _sheet(page, sheet)
        page.showPage()
    page.save()
    return out.getvalue()


def cover(title: str = "PROJECT COVER SHEET") -> Sheet:
    """A page with words and no dimension: a general page."""
    return Sheet(title=title, words=("GENERAL NOTES", "ALL WORK PER CONTRACT DOCUMENTS"))
