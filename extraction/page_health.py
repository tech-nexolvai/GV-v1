"""A cheap per-page health check, in plain sentences, no AI (#1745).

Every page of a set gets about fifteen facts, read from the working copy (`extraction/working_copy.py`)
and from what the front door saw in the original (`WorkingCopy.facts`), each with one plain
sentence for the reviewer when it matters:

| fact | how |
|---|---|
| `typed_text` | characters of the drawing (its own content and pasted drawings) |
| `hidden_text` | characters with no ink at their spot (`visible_text`): not used |
| `invisible_objects` | text objects set invisible (render mode 3, PDFium) |
| `shx_hidden_text` | of those, the ones over drawn strokes: AutoCAD's SHX text, used |
| `shx_comments` | comments authored `AutoCAD SHX Text`: the vendor's text, used |
| `layers_off` | optional content switched off by default (`/OCProperties /D /OFF`) |
| `no_letter_map` | fonts that cannot be turned into letters (Type3, or Type0 without ToUnicode), and the share of characters that are codes |
| `drawn_letters` | few characters and many letter-sized strokes: lettering drawn as lines |
| `picture_share` | the part of the page that is a picture |
| `turned`, `moved`, `user_unit` | what the front door fixed |
| `not_read` | why a page could not be read |
| `annotations` | annotations by kind; how many are markups (never read) |
| `flattened_markup` | a heuristic only: a markup layer, or a markup tool's file in stages |
| `revisions`, `signed` | the original saved in stages; signed |
| `size_in` | the sheet's size in inches |

A suspected flattened markup is a **warning only**: it never makes anything red.

Source: the front-door research note (local, 2026-10-19) §5-6 · Verification:
`tests/extraction/test_page_health.py`
"""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Mapping
from typing import Any, Final

import pdfplumber
import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_c  # type: ignore[import-untyped]

from extraction.stamp_text import carries_drawing
from extraction.visible_text import InkPicture, ink_picture, is_shx_comment

__all__ = ["page_health", "set_health"]

#: Fewer characters than this, with at least `_STROKES_MIN` letter-sized strokes, is a page whose
#: lettering is drawn as lines.
_TYPED_FEW: Final = 20
_STROKES_MIN: Final = 200
#: A letter-sized stroke: its box no larger than this on either side (points; a quarter inch).
_LETTER_PT: Final = 18.0
#: Layer names that say a markup tool put them there.
_MARKUP_LAYER_WORDS: Final = ("markup", "comment", "bluebeam", "review")
#: Words in a Producer that name a markup tool.
_MARKUP_TOOLS: Final = ("bluebeam", "revu")


def set_health(
    data: bytes, name: str, intake: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Every page's health of the working copy `data` (upload `name`); `intake` is the upload's
    listing item (its `intake_facts`, `working_changes`, `pages_not_read`)."""
    with pikepdf.open(io.BytesIO(data)) as pdf:
        count = len(pdf.pages)
    return [page_health(data, name, number, intake) for number in range(1, count + 1)]


def page_health(
    data: bytes, name: str, number: int, intake: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """One page's facts and plain sentences (`says`)."""
    item: Mapping[str, Any] = intake or {}
    raw = item.get("intake_facts")
    original: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    pages = original.get("pages")
    pages = pages if isinstance(pages, Mapping) else {}
    found = pages.get(str(number), pages.get(number))
    before: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    facts: dict[str, Any] = {}
    says: list[str] = []
    vendor = _vendor_copy(data, number - 1)
    ink = ink_picture(vendor)
    _text_facts(vendor, ink, facts)
    _object_facts(vendor, ink, facts)
    _file_facts(data, number - 1, facts)
    facts["turned"] = int(before.get("rotation") or 0)
    facts["moved"] = bool(before.get("moved"))
    facts["user_unit"] = float(before.get("user_unit") or 1.0)
    facts["revisions"] = int(original.get("revisions") or 1)
    facts["signed"] = bool(original.get("signed"))
    producer = str(original.get("producer") or "").lower()
    if any(tool in producer for tool in _MARKUP_TOOLS) and facts["revisions"] > 1:
        facts["flattened_markup"] = True
    not_read = [
        str(entry.get("why"))
        for entry in item.get("pages_not_read") or []
        if isinstance(entry, Mapping) and entry.get("page") == number
    ]
    if not_read:
        facts["not_read"] = not_read[0]
    says.extend(_sentences(facts))
    return {"file": name, "page": number, "facts": facts, "says": says}


# ---- reading the page ----------------------------------------------------------------------------


def _vendor_copy(data: bytes, index: int) -> bytes:
    """The page with its own content and its pasted drawings merged in (no other annotation): what
    the readers read (`verify_value._vendor_page`, without its SHX step, counted apart here)."""
    with pikepdf.open(io.BytesIO(data)) as source:
        out = pikepdf.new()
        out.pages.append(source.pages[index])
        # Its layers come with it, so a layer switched off stays off (`visible_text`).
        if "/OCProperties" in source.Root:
            layers = source.make_indirect(source.Root["/OCProperties"])
            out.Root["/OCProperties"] = out.copy_foreign(layers)
        page = out.pages[0]
        annotations = page.obj.get("/Annots") or pikepdf.Array()
        page.obj["/Annots"] = pikepdf.Array([a for a in annotations if carries_drawing(a)])
        out.flatten_annotations(mode="all")
        buffer = io.BytesIO()
        out.save(buffer)
        return buffer.getvalue()


def _text_facts(vendor: bytes, ink: InkPicture, facts: dict[str, Any]) -> None:
    with pdfplumber.open(io.BytesIO(vendor)) as document:
        page = document.pages[0]
        chars = [c for c in page.chars if str(c.get("text", "")).strip()]
        hidden = sum(
            1
            for c in chars
            if not ink.shows((float(c["x0"]), float(c["top"]), float(c["x1"]), float(c["bottom"])))
        )
        codes = sum(
            1
            for c in chars
            if str(c["text"]).startswith("(cid:") or "" <= str(c["text"])[0] <= ""
        )
        strokes = sum(
            1
            for item in (*page.curves, *page.lines)
            if float(item["x1"]) - float(item["x0"]) <= _LETTER_PT
            and float(item["bottom"]) - float(item["top"]) <= _LETTER_PT
        )
        area = float(page.width) * float(page.height)
        pictures = sum(
            max(0.0, float(i["x1"]) - float(i["x0"]))
            * max(0.0, float(i["bottom"]) - float(i["top"]))
            for i in page.images
        )
        facts["typed_text"] = len(chars)
        facts["hidden_text"] = hidden
        facts["code_share"] = round(codes / len(chars), 3) if chars else 0.0
        facts["drawn_letters"] = len(chars) < _TYPED_FEW and strokes >= _STROKES_MIN
        facts["picture_share"] = round(min(1.0, pictures / area), 3) if area else 0.0
        facts["size_in"] = [round(float(page.width) / 72, 1), round(float(page.height) / 72, 1)]


def _object_facts(vendor: bytes, ink: InkPicture, facts: dict[str, Any]) -> None:
    """Invisible text objects (render mode 3) from PDFium, and those over drawn strokes (SHX)."""
    document = pdfium.PdfDocument(vendor)
    invisible = shx = 0
    try:
        page = document[0]
        height = page.get_height()
        for obj in page.get_objects(max_depth=4):
            if obj.type != pdfium_c.FPDF_PAGEOBJ_TEXT:
                continue
            if (
                pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw)
                != pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE
            ):
                continue
            invisible += 1
            left, bottom, right, top = obj.get_bounds()
            if ink.shows((left, height - top, right, height - bottom)):
                shx += 1
    finally:
        document.close()
    facts["invisible_objects"] = invisible
    facts["shx_hidden_text"] = shx


def _file_facts(data: bytes, index: int, facts: dict[str, Any]) -> None:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[index]
        kinds: Counter[str] = Counter()
        shx = markups = 0
        for annotation in page.obj.get("/Annots") or []:
            if not isinstance(annotation, pikepdf.Dictionary):
                continue
            if is_shx_comment(annotation):
                shx += 1
                continue
            kinds[str(annotation.get("/Subtype", "/Unknown")).lstrip("/")] += 1
            if not carries_drawing(annotation) and annotation.get("/Subtype") != pikepdf.Name.Link:
                markups += 1
        facts["annotations"] = dict(sorted(kinds.items()))
        facts["markups"] = markups
        facts["shx_comments"] = shx
        facts["layers_off"] = _layers_off(pdf)
        facts["no_letter_map"] = _fonts_without_map(page)
        if any(word in name.lower() for name in _layer_names(pdf) for word in _MARKUP_LAYER_WORDS):
            facts["flattened_markup"] = True


def _layer_names(pdf: pikepdf.Pdf) -> list[str]:
    properties = pdf.Root.get("/OCProperties")
    groups = properties.get("/OCGs") if isinstance(properties, pikepdf.Dictionary) else None
    return [
        str(g.get("/Name", ""))
        for g in groups or []
        if isinstance(g, pikepdf.Dictionary) and str(g.get("/Name", "")) != "GV Review markup"
    ]


def _layers_off(pdf: pikepdf.Pdf) -> list[str]:
    properties = pdf.Root.get("/OCProperties")
    default = properties.get("/D") if isinstance(properties, pikepdf.Dictionary) else None
    off = default.get("/OFF") if isinstance(default, pikepdf.Dictionary) else None
    return [str(g.get("/Name", "")) for g in off or [] if isinstance(g, pikepdf.Dictionary)]


def _fonts_without_map(page: pikepdf.Page) -> list[str]:
    """Fonts of the page (and of its forms and pasted drawings, a few levels down) that cannot be
    turned into letters: Type3 without ToUnicode, and Type0 without ToUnicode."""
    found: set[str] = set()
    seen: set[tuple[int, int]] = set()

    def walk(resources: Any, depth: int) -> None:
        if not isinstance(resources, pikepdf.Dictionary) or depth > 4:
            return
        for font in (resources.get("/Font") or {}).values():
            if not isinstance(font, pikepdf.Dictionary):
                continue
            kind = font.get("/Subtype")
            if kind in (pikepdf.Name.Type3, pikepdf.Name.Type0) and "/ToUnicode" not in font:
                found.add(str(font.get("/BaseFont", kind)).lstrip("/"))
        for xobject in (resources.get("/XObject") or {}).values():
            if isinstance(xobject, pikepdf.Stream) and xobject.objgen not in seen:
                if xobject.objgen != (0, 0):
                    seen.add(xobject.objgen)
                walk(xobject.get("/Resources"), depth + 1)

    walk(page.obj.get("/Resources"), 0)
    for annotation in page.obj.get("/Annots") or []:
        if isinstance(annotation, pikepdf.Dictionary) and carries_drawing(annotation):
            appearances = annotation.get("/AP")
            appearance = (
                appearances.get("/N") if isinstance(appearances, pikepdf.Dictionary) else None
            )
            if isinstance(appearance, pikepdf.Stream):
                walk(appearance.get("/Resources"), 1)
    return sorted(found)


# ---- plain sentences -----------------------------------------------------------------------------


def _sentences(facts: Mapping[str, Any]) -> list[str]:
    says: list[str] = []
    if facts.get("not_read"):
        return [str(facts["not_read"]).capitalize() + "."]
    typed = int(facts.get("typed_text") or 0)
    if typed >= _TYPED_FEW:
        says.append("This page has typed text: numbers are read directly.")
    if facts.get("drawn_letters"):
        says.append(
            "The lettering on this page is drawn with lines, not typed: it is read from its "
            "shapes, and positions are still exact."
        )
    if facts.get("shx_hidden_text") or facts.get("shx_comments"):
        says.append("AutoCAD stored the real text behind the drawn letters; it was used.")
    hidden = int(facts.get("hidden_text") or 0)
    if hidden:
        says.append(
            f"{hidden} characters on this page are in the file but not visible (a hidden layer, "
            "invisible text or a white-out box) and were not used."
        )
    if facts.get("layers_off"):
        says.append(
            "Layers switched off in this file: "
            + ", ".join(facts["layers_off"][:6])
            + "; only what the page shows is read."
        )
    if facts.get("no_letter_map") or float(facts.get("code_share") or 0) > 0.2:
        says.append(
            "Some text on this page is stored as codes without a letter map, so those numbers are "
            "read from the picture instead."
        )
    if float(facts.get("picture_share") or 0) > 0.5 and typed < _TYPED_FEW:
        says.append(
            "This page is a scanned picture: its numbers are read from the picture by two "
            "text-recognition engines, and only values both agree on are used."
        )
    if facts.get("turned"):
        says.append(f"The page was stored turned {facts['turned']} degrees; it was straightened.")
    if facts.get("moved"):
        says.append("The page was cropped or shifted; its visible area is used.")
    if float(facts.get("user_unit") or 1) != 1:
        says.append(
            f"This sheet uses a large-page scale (x{facts['user_unit']:g}); sizes were adjusted."
        )
    if facts.get("markups"):
        says.append(
            f"{facts['markups']} markups on this page were not read (they are comments, not the "
            "drawing)."
        )
    if facts.get("flattened_markup"):
        says.append(
            "This page may contain markups merged into the drawing; a cloud near them should be "
            "checked by eye."
        )
    if int(facts.get("revisions") or 1) > 1:
        says.append("The file was saved in several stages; the latest is read.")
    if facts.get("signed"):
        says.append(
            "The original's digital signature is untouched; the working copy is for checking only."
        )
    return says
