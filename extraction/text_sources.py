"""What kinds of text one page carries, and which of them this system can read (formats phase 1).

**A vendor does not choose how its PDF stores its numbers, and usually does not know.** The same
elevation reaches a reviewer in several forms, depending on the program that made the file and the
button that was pressed:

| Kind | How it got that way | Read by |
|---|---|---|
| `EXACT_TEXT` | exported with its fonts kept (TrueType, or AutoCAD `PDFSHX=2`) | the vector route, exactly |
| `CAD_TEXT_NOTES` | AutoCAD export with SHX fonts (`PDFSHX=1`, its default) | the CAD-text route, exactly |
| `STAMP_TEXT` | a text PDF pasted into a review set (Bluebeam snapshot): `AI_Set_1`, part of `AI_Set_2` | the stamp-text route, exactly |
| `STAMP_COLOURED_TEXT` | coloured text inside a pasted drawing: on `AI_Set_1`, a reviewer's markup snapped with the sheet | not read: it may be the answer |
| `UNDECODED_TEXT` | font text whose characters map to nothing | not read: there is no text to read |
| `DRAWN_SHAPES` | printed to PDF: every character a pen stroke (most of `AI_Set_2`) | the shape reader and model readers |
| `SCANNED` | a picture of a drawing | OCR and model readers |

A page can carry several at once — a title block in text over a drawing printed as strokes is
ordinary — so this reports every kind it finds, with counts, and never picks one.

**It reads nothing and decides nothing.** It counts what is in the file, so the pipeline and its
report can say, page by page, which forms arrived and whether any of them went unread. A kind nobody
reads is named as such rather than disappearing into a page that "had no dimensions".

**No thresholds.** A kind is present when the file holds at least one of it. `SCANNED` is the one
kind defined by an absence: images, with no text and no drawn paths anywhere on the page. Deciding
that a page is "mostly" an image would take a proportion measured on real scans, and there are none.

Source: formats phase 1 (the request of 2026-10-01: read whatever form a vendor sends, with no
blockers) · Verification: `tests/extraction/test_text_sources.py`
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import pdfplumber
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]

from extraction.annotations import DrawingLayer, _layer_of, _subtype
from extraction.reader import UnreadablePdf
from extraction.stamp_text import stamp_character_counts

__all__ = ["READ_BY", "TextKind", "TextSources", "survey_page"]


class TextKind(StrEnum):
    """One form a page's text can take, from the most exact to the least."""

    EXACT_TEXT = "exact_text"
    CAD_TEXT_NOTES = "cad_text_notes"
    STAMP_TEXT = "stamp_text"
    STAMP_COLOURED_TEXT = "stamp_coloured_text"
    UNDECODED_TEXT = "undecoded_text"
    DRAWN_SHAPES = "drawn_shapes"
    SCANNED = "scanned"


#: Which route reads each kind, or `None` where none does. Kept here, next to the kinds, so a new
#: route is one line and a kind nobody reads can never be mistaken for one somebody does.
READ_BY: Final[dict[TextKind, str | None]] = {
    TextKind.EXACT_TEXT: "vector",
    TextKind.CAD_TEXT_NOTES: "cad_text",
    TextKind.STAMP_TEXT: "stamp_text",
    TextKind.STAMP_COLOURED_TEXT: None,
    TextKind.UNDECODED_TEXT: None,
    TextKind.DRAWN_SHAPES: "glyph + vision",
    TextKind.SCANNED: "ocr + vision",
}


@dataclass(frozen=True, slots=True)
class TextSources:
    """Counts of every kind of text on one page, as the file holds them."""

    page_index: int
    content_characters: int
    """Characters in the page's own content stream, which a text PDF's numbers are."""

    cad_text_notes: int
    """AutoCAD `AutoCAD SHX Text` notes (`DrawingLayer.VENDOR_TEXT`)."""

    stamp_text_decoded: int
    """Characters of font text inside pasted drawings that map to real characters."""

    stamp_text_undecoded: int
    """Characters of font text inside pasted drawings that map to nothing."""

    stamp_text_coloured: int
    """Characters of font text inside pasted drawings set in colour, which may be a reviewer's markup
    snapped with the sheet (`extraction/stamp_text.py`)."""

    drawn_paths: int
    """Path objects, in the page's content and inside pasted drawings: line-work, and on a printed
    drawing every character too."""

    images: int
    """Images drawn on the page."""

    @property
    def kinds(self) -> tuple[TextKind, ...]:
        """Every kind present, most exact first."""
        present: list[TextKind] = []
        if self.content_characters:
            present.append(TextKind.EXACT_TEXT)
        if self.cad_text_notes:
            present.append(TextKind.CAD_TEXT_NOTES)
        if self.stamp_text_decoded:
            present.append(TextKind.STAMP_TEXT)
        if self.stamp_text_coloured:
            present.append(TextKind.STAMP_COLOURED_TEXT)
        if self.stamp_text_undecoded:
            present.append(TextKind.UNDECODED_TEXT)
        if self.drawn_paths:
            present.append(TextKind.DRAWN_SHAPES)
        if self.images and not (
            self.content_characters
            or self.cad_text_notes
            or self.stamp_text_decoded
            or self.stamp_text_coloured
            or self.stamp_text_undecoded
            or self.drawn_paths
        ):
            present.append(TextKind.SCANNED)
        return tuple(present)

    @property
    def unread(self) -> tuple[TextKind, ...]:
        """The kinds present that no route reads: what a reviewer must know went unread."""
        return tuple(kind for kind in self.kinds if READ_BY[kind] is None)

    def as_payload(self) -> dict[str, Any]:
        """The counts and the verdicts, as a page result records them."""
        return {
            "content_characters": self.content_characters,
            "cad_text_notes": self.cad_text_notes,
            "stamp_text_decoded": self.stamp_text_decoded,
            "stamp_text_undecoded": self.stamp_text_undecoded,
            "stamp_text_coloured": self.stamp_text_coloured,
            "drawn_paths": self.drawn_paths,
            "images": self.images,
            "kinds": [kind.value for kind in self.kinds],
            "not_read_yet": [kind.value for kind in self.unread],
        }


def survey_page(data: bytes, page_index: int) -> TextSources:
    """Count every kind of text one page carries. Reads the file; interprets none of it."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as plumbed:
            try:
                page = plumbed.pages[page_index]
            except IndexError as error:
                raise UnreadablePdf(
                    f"page {page_index} is beyond the {len(plumbed.pages)} pages in this document"
                ) from error
            content_characters = sum(1 for char in page.chars if char.get("text", "").strip())
            images = len(page.images)
            annotations = [annotation["data"] for annotation in page.annots]
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(f"page {page_index} could not be surveyed: {error}") from error

    cad_text_notes = sum(
        1
        for annotation in annotations
        if _layer_of(annotation, _subtype(annotation)) is DrawingLayer.VENDOR_TEXT
    )
    has_stamps = any(_subtype(annotation) == "Stamp" for annotation in annotations)
    stamp = stamp_character_counts(data, page_index) if has_stamps else None

    drawn_paths = 0
    document = pdfium.PdfDocument(data)
    try:
        pdf_page = document[page_index]
        for index in range(pdfium_raw.FPDFPage_CountObjects(pdf_page)):
            page_object = pdfium_raw.FPDFPage_GetObject(pdf_page, index)
            if pdfium_raw.FPDFPageObj_GetType(page_object) == pdfium_raw.FPDF_PAGEOBJ_PATH:
                drawn_paths += 1
        for index, annotation in enumerate(annotations):
            if _subtype(annotation) != "Stamp":
                continue
            handle = pdfium_raw.FPDFPage_GetAnnot(pdf_page, index)
            if not handle:
                continue
            try:
                for position in range(pdfium_raw.FPDFAnnot_GetObjectCount(handle)):
                    page_object = pdfium_raw.FPDFAnnot_GetObject(handle, position)
                    if pdfium_raw.FPDFPageObj_GetType(page_object) == pdfium_raw.FPDF_PAGEOBJ_PATH:
                        drawn_paths += 1
            finally:
                pdfium_raw.FPDFPage_CloseAnnot(handle)
    except Exception as error:
        raise UnreadablePdf(f"page {page_index} could not be surveyed: {error}") from error
    finally:
        document.close()

    return TextSources(
        page_index=page_index,
        content_characters=content_characters,
        cad_text_notes=cad_text_notes,
        stamp_text_decoded=0 if stamp is None else stamp.readable,
        stamp_text_undecoded=0 if stamp is None else stamp.unmapped,
        stamp_text_coloured=0 if stamp is None else stamp.coloured,
        drawn_paths=drawn_paths,
        images=images,
    )
