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

**It reads only text set in black or grey.** A snapshot is a picture of a sheet *as it was*, and a
sheet snapped after somebody marked it up carries their markup inside it, merged with the drawing and
indistinguishable from it by structure: no layer, no tag, no separate annotation. Measured on the
client's first set (`AI_Set_1`): it has no `/FreeText` at all, and its reviewer's red, blue and green
corrections (`19-1/4"` over the vendor's `19 7/8"`) are text inside the pasted drawings. Read, they
would be the answer read as the question (the vendor-layer rule). Colour is the one thing that tells
them apart, so coloured text is not read and is counted instead; drawings are plotted in black. This
gives up any vendor text set in colour, which goes to the shape and model readers like any other
unread label. It cannot catch a reviewer who writes in black — in production there is no reviewer's
markup to catch, because vendors send drawings nobody has reviewed yet. The same rule decides which
paths count (`path_ink`): the reader puts a stacked fraction whose words came apart back together
round the bar drawn between its numerator and denominator (#880), and only a black or grey path is
taken for that bar.

Source: formats phase 1 · Verification: `tests/extraction/test_stamp_text.py`
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import pdfplumber
import pikepdf

from extraction.reader import PageContents, UnreadablePdf, read_page_contents

__all__ = [
    "StampCharacters",
    "StampText",
    "drawing_ink",
    "path_ink",
    "read_stamp_text",
    "stamp_character_counts",
    "stamps_only",
]

#: What pdfminer writes for a character its font maps to nothing.
_UNMAPPED: Final = "(cid:"

_NO_STAMP_TEXT: Final = "the pasted drawings on this page hold no font text"

#: How far apart a colour's components may be and the colour still be grey. A plotter writes black as
#: exact zeros; this absorbs only a colour written back through a profile.
_GREY: Final = 0.02


def drawing_ink(char: dict[str, Any]) -> bool:
    """Whether a character is set in black or grey: a drawing's ink, not a reviewer's.

    Grey is equal parts of red, green and blue, any one-component grey, or cyan, magenta and yellow in
    equal parts. Anything else — a colour, a pattern, a colour space this cannot read — is not.
    """
    colour = char.get("non_stroking_color")
    if colour is None:
        return True  # nothing set: the default fill, which is black
    if isinstance(colour, (int, float)):
        return True
    if not isinstance(colour, (tuple, list)) or not all(
        isinstance(part, (int, float)) for part in colour
    ):
        return False
    if len(colour) == 1:
        return True
    parts = [float(part) for part in colour]
    if len(parts) == 3:
        return max(parts) - min(parts) <= _GREY
    if len(parts) == 4:
        return max(parts[:3]) - min(parts[:3]) <= _GREY
    return False


def path_ink(path: dict[str, Any]) -> bool:
    """Whether a line, rectangle or curve is drawn in black or grey, by `drawing_ink`'s rule (#880).

    The colours it shows are the ones that count: its line's if it is stroked, its fill's if it is
    filled, both where it is both, as `annotations.VectorPath.drawing_ink` counts a path's. A path
    drawn neither way shows no ink. `read_stamp_text` hands it to the reader, which then takes a
    stacked fraction's bar only from such a path, so a reviewer's coloured line baked into the
    snapshot never stands in for the vendor's bar.
    """
    shown: list[object] = []
    if path.get("stroke"):
        shown.append(path.get("stroking_color"))
    if path.get("fill"):
        shown.append(path.get("non_stroking_color"))
    return bool(shown) and all(drawing_ink({"non_stroking_color": colour}) for colour in shown)


@dataclass(frozen=True, slots=True)
class StampText:
    """The readable text runs inside a page's pasted drawings, and how many characters were not."""

    contents: PageContents
    """Text runs only, in the original page's coordinates. `segments` is always empty: the stamps'
    line-work is read from their paths by `extraction/annotations.py`, and reading it twice would
    give every line a twin. `set_aside` holds the runs not read as numbers (#738)."""

    characters: StampCharacters


@dataclass(frozen=True, slots=True)
class StampCharacters:
    """Every character inside a page's pasted drawings, in exactly one of three counts."""

    readable: int
    """In black or grey, and mapped to a real character: what this module reads."""

    unmapped: int
    """Mapped to nothing by its font (#738's real case, where it exists)."""

    coloured: int
    """Mapped, but set in colour: possibly somebody's markup inside the snapshot, so not read."""


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


def _character_counts(flattened: bytes, page_index: int) -> StampCharacters:
    readable = unmapped = coloured = 0
    with pdfplumber.open(io.BytesIO(flattened)) as document:
        for char in document.pages[page_index].chars:
            text = str(char.get("text", ""))
            if text.startswith(_UNMAPPED):
                unmapped += 1
            elif not text.strip():
                continue
            elif drawing_ink(char):
                readable += 1
            else:
                coloured += 1
    return StampCharacters(readable=readable, unmapped=unmapped, coloured=coloured)


def read_stamp_text(
    data: bytes, page_index: int, *, document_version_id: UUID, dpi: int
) -> StampText:
    """The readable text runs inside this page's pasted drawings, exactly as the file holds them."""
    flattened = stamps_only(data, page_index)
    characters = _character_counts(flattened, page_index)
    if not characters.readable:
        empty = PageContents(
            page_index=page_index, texts=(), segments=(), unreadable_reason=_NO_STAMP_TEXT
        )
        return StampText(empty, characters)
    contents = read_page_contents(
        flattened,
        page_index,
        document_version_id=document_version_id,
        dpi=dpi,
        keep_char=drawing_ink,
        keep_path=path_ink,
    )
    texts = tuple(item for item in contents.texts if _UNMAPPED not in item.text)
    return StampText(
        PageContents(
            page_index=page_index,
            texts=texts,
            segments=(),
            unreadable_reason=None if texts else _NO_STAMP_TEXT,
            set_aside=contents.set_aside,
        ),
        characters,
    )


def stamp_character_counts(data: bytes, page_index: int) -> StampCharacters:
    """The characters inside the page's pasted drawings, counted for the survey."""
    return _character_counts(stamps_only(data, page_index), page_index)
