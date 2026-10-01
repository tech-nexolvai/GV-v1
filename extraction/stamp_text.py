"""Reading the font text inside a pasted drawing, exactly (formats phase 1).

**The drawings in a review set are pasted in, as `/Stamp` annotations** — a Bluebeam snapshot of the
vendor's sheet, and another of the architect's (#710). Where the sheet they were snapped from held
real text, the snapshot holds it too: font text inside the stamp's appearance stream. On the client's
sets that is most of `AI_Set_1`'s labels and over a thousand strings on `AI_Set_2`.

**#738 recorded this text as unreadable, and it is not.** pdfium's `FPDFTextObj_GetText` returned
nothing for every character, which was read as "the font has no character map". The cause was the
call: it decodes through the *page's* text page, and an annotation's objects are not on it. The
fonts map their characters; read through the appearance stream they decode exactly (`2' -5"`,
`ML-003-CUST`, `3/8" = 1'-0"`).

**How it is read: the page's own reader, on a copy that holds only the pasted drawings.** A private
copy of the page keeps its `/Stamp` annotations and nothing else — no reviewer markup, no other
annotation, none of the page's own content — and has them merged into its content (`qpdf`'s
annotation flattening, which places each appearance exactly as a viewer does). `read_page_contents`
then reads that copy as it reads any text PDF: the same words, the same rotations, the same joined
`984 [38 3/4]` and `2' -5"` tokens, and the same page boxes, so every coordinate is in the original
page's frame. One reader for text, wherever the text was put.

**What it never does.** It never reads the reviewer's layer — the copy has none — and never reads
the page's own content, which the vector route already has. A character whose font maps it to no
character comes back from pdfminer as `(cid:N)`; such text is dropped here and counted, because a
string of glyph numbers is not a reading.

Source: formats phase 1 · Verification: `tests/extraction/test_stamp_text.py`
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import pdfplumber
import pikepdf

from extraction.reader import PageContents, UnreadablePdf, read_page_contents

__all__ = ["StampText", "read_stamp_text", "stamp_character_counts", "stamps_only"]

#: What pdfminer writes for a character its font maps to nothing.
_UNMAPPED: Final = "(cid:"

_NO_STAMP_TEXT: Final = "the pasted drawings on this page hold no font text"


@dataclass(frozen=True, slots=True)
class StampText:
    """The readable text runs inside a page's pasted drawings, and how many characters were not."""

    contents: PageContents
    """Text runs only, in the original page's coordinates. `segments` is always empty: the stamps'
    line-work is read from their paths by `extraction/annotations.py`, and reading it twice would
    give every line a twin."""

    readable_characters: int
    unmapped_characters: int
    """Characters whose font maps them to nothing (#738's real case, where it exists)."""


def stamps_only(data: bytes, page_index: int) -> bytes:
    """A copy of the document in which this page shows only its pasted drawings, merged into it.

    The other pages are untouched, so `page_index` names the same page in the copy. The original
    bytes are never modified.
    """
    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            try:
                page = pdf.pages[page_index]
            except IndexError as error:
                raise UnreadablePdf(
                    f"page {page_index} is beyond the {len(pdf.pages)} pages in this document"
                ) from error
            annotations = page.obj.get("/Annots") or pikepdf.Array()
            page.obj["/Annots"] = pikepdf.Array(
                [
                    annotation
                    for annotation in annotations
                    if annotation.get("/Subtype") == pikepdf.Name("/Stamp")
                ]
            )
            page.obj["/Contents"] = pdf.make_stream(b"")
            pdf.flatten_annotations(mode="all")
            out = io.BytesIO()
            pdf.save(out)
            return out.getvalue()
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(
            f"page {page_index}'s pasted drawings could not be prepared: {error}"
        ) from error


def _character_counts(flattened: bytes, page_index: int) -> tuple[int, int]:
    with pdfplumber.open(io.BytesIO(flattened)) as document:
        texts = [str(char.get("text", "")) for char in document.pages[page_index].chars]
    unmapped = sum(1 for text in texts if text.startswith(_UNMAPPED))
    readable = sum(1 for text in texts if text.strip() and not text.startswith(_UNMAPPED))
    return readable, unmapped


def read_stamp_text(
    data: bytes, page_index: int, *, document_version_id: UUID, dpi: int
) -> StampText:
    """The readable text runs inside this page's pasted drawings, exactly as the file holds them."""
    flattened = stamps_only(data, page_index)
    readable, unmapped = _character_counts(flattened, page_index)
    if not readable:
        empty = PageContents(
            page_index=page_index, texts=(), segments=(), unreadable_reason=_NO_STAMP_TEXT
        )
        return StampText(empty, readable_characters=0, unmapped_characters=unmapped)
    contents = read_page_contents(
        flattened, page_index, document_version_id=document_version_id, dpi=dpi
    )
    texts = tuple(item for item in contents.texts if _UNMAPPED not in item.text)
    return StampText(
        PageContents(
            page_index=page_index,
            texts=texts,
            segments=(),
            unreadable_reason=None if texts else _NO_STAMP_TEXT,
        ),
        readable_characters=readable,
        unmapped_characters=unmapped,
    )


def stamp_character_counts(data: bytes, page_index: int) -> tuple[int, int]:
    """`(readable, unmapped)` characters inside the page's pasted drawings, for the survey."""
    return _character_counts(stamps_only(data, page_index), page_index)
