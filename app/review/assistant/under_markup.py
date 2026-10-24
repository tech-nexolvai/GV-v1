"""Read under GV's markup: an area of a page drawn without the reviewers' marks (#1548).

claude.ai, on a set whose earlier rounds carry GV's review, listed a page's annotations, removed
only the reviewers' ones (the typed notes and boxes) and kept the pasted drawings, then read the
vendor's values that GV's boxes had covered. Where a yellow box was baked INSIDE a pasted drawing,
it emptied the small filled-rectangle forms inside that drawing. This module does the same, by code:

1. **The reviewers' annotations are removed** (every kind that is not a pasted drawing:
   `extraction.stamp_text.carries_drawing` keeps `/Stamp` and a `/Square` holding a drawing).
2. **Boxes baked inside a pasted drawing are hidden where code can tell them apart**: inside each
   kept drawing that meets the area, (a) a small sub-drawing (form XObject) that only fills
   rectangles in a colour is emptied, and (b) a small rectangle filled in a colour in the drawing's
   own content is left unpainted. Grey and black fills (the vendor's own hatching, masks behind
   text) are never touched.
3. **The drawing's own text in the area** (as `pdftotext -bbox` gives it), read from the copy without
   the reviewers' annotations: text under an opaque box is still in the drawing's text, so it is
   listed even where nothing could be hidden. On GV's combined review sheets text set in colour
   is marked: inside a pasted drawing that may be GV's note, not the vendor's. On an ordinary
   upload every colour is the drawing's own ink (the colour rule; the label reader's own test,
   `verify_value.combined_sheet_at`), so its coloured text is never marked.

It returns two pictures of the area (as the page shows it, and with the markup removed) and plain
lines saying what was removed and how. Nothing is changed in the upload; the copy lives in memory.

**Loaded by name, never imported** by `app/api/` (pikepdf, pdfplumber, pypdfium2). pdfium is held
under `chat_drawing.PDFIUM_LOCK`.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pdfplumber
import pikepdf
from PIL import Image

from app.review.assistant.chat_drawing import PDFIUM_LOCK
from app.review.assistant.page_pictures import fitting_dpi
from extraction.pdfium_guard import pdfium
from extraction.reader import pixel_placement
from extraction.stamp_text import carries_drawing

__all__ = ["UnderMarkup", "read_under_markup"]

_log = logging.getLogger(__name__)

#: A sub-drawing or rectangle counts as a baked box when it covers at most this share of its
#: pasted drawing (a whole-drawing fill is the vendor's background, never a reviewer's box).
_SMALL_SHARE: Final = 0.08
#: A fill is "in a colour" when its channels differ by more than this (0..1): yellow, red, green.
_COLOURED: Final = 0.25
#: Words listed at most.
_MAX_WORDS: Final = 120
#: Sub-drawings searched this deep inside a pasted drawing.
_MAX_DEPTH: Final = 4
_PATH_OPS: Final = frozenset({"m", "l", "c", "v", "y", "h", "re"})
_FILLS: Final = frozenset({"f", "F", "f*", "B", "B*", "b", "b*"})
_PAINTS: Final = _FILLS | frozenset({"S", "s", "n"})
#: Operators a box-only sub-drawing may hold (state, colour, rectangles, painting).
_BOX_ONLY: Final = frozenset(
    {"q", "Q", "cm", "w", "J", "j", "M", "d", "ri", "i", "gs", "re", "W", "W*", "n"}
    | {"g", "G", "rg", "RG", "k", "K", "cs", "CS", "sc", "SC", "scn", "SCN"}
    | _FILLS
    | {"S", "s"}
)

type Box = tuple[float, float, float, float]


@dataclass(slots=True)
class UnderMarkup:
    """What `read_under_markup` found."""

    as_shown: bytes
    """The area as the page shows it (GV's markup included), PNG."""
    under: bytes
    """The area with the reviewers' annotations removed and baked boxes hidden, PNG."""
    width: int
    height: int
    dpi: int
    box: Box
    removed: list[dict[str, Any]] = field(default_factory=list)
    """The reviewers' annotations over the area: `{kind, author, text}`."""
    removed_elsewhere: int = 0
    """The reviewers' annotations removed elsewhere on the page."""
    hidden_forms: int = 0
    """Small filled-rectangle sub-drawings emptied inside the pasted drawings."""
    hidden_fills: int = 0
    """Small rectangles filled in a colour left unpainted inside the pasted drawings."""
    drawings: int = 0
    """Pasted drawings that meet the area."""
    words: list[dict[str, Any]] = field(default_factory=list)
    """The drawing's text in the area: `{text, box, coloured}`, in reading order. `coloured` only
    on GV's combined sheets: on an ordinary upload every colour is the drawing's own ink."""

    def lines(self) -> list[str]:
        x0, y0, x1, y1 = self.box
        out = [
            (
                f"Box {x0:g},{y0:g} to {x1:g},{y1:g} pt, {self.dpi} dpi. Picture 1: as the page "
                "shows it (GV's markup included). Picture 2: under the markup."
            )
        ]
        if self.removed:
            listed = "; ".join(
                f"{item['kind']}"
                + (f" by {item['author']}" if item.get("author") else "")
                + (f" ({item['text']!r})" if item.get("text") else "")
                for item in self.removed[:12]
            )
            out.append(
                f"Removed {len(self.removed)} reviewer annotation(s) over this area: {listed}."
            )
        else:
            out.append("No reviewer annotation lies over this area.")
        if self.removed_elsewhere:
            out.append(f"Also removed {self.removed_elsewhere} elsewhere on the page.")
        if self.drawings:
            hidden = []
            if self.hidden_forms:
                hidden.append(
                    f"emptied {self.hidden_forms} small filled-box sub-drawing(s) inside the pasted "
                    "drawing"
                )
            if self.hidden_fills:
                hidden.append(f"left {self.hidden_fills} small coloured filled box(es) unpainted")
            out.append(
                (
                    "Inside the pasted drawing: " + " and ".join(hidden) + " (boxes baked in by an "
                    "earlier review)."
                )
                if hidden
                else "Nothing baked into the pasted drawing looked like a reviewer's box; anything "
                "drawn into it stays (compare the two pictures)."
            )
        if self.words:
            shown = " | ".join(
                word["text"] + (" [coloured: may be GV's]" if word["coloured"] else "")
                for word in self.words[:_MAX_WORDS]
            )
            more = len(self.words) - _MAX_WORDS
            out.append(
                "The drawing's own text in this area (its text layer, also what lies under an "
                f"opaque box): {shown}" + (f" ... and {more} more." if more > 0 else ".")
            )
        else:
            out.append(
                "The drawing has no text in this area (its numbers may be drawn as lines: read the "
                "picture)."
            )
        return out


# ---- geometry ------------------------------------------------------------------------------------


def _shown_frame(page: pikepdf.Page) -> Callable[[float, float], tuple[float, float]]:
    """PDF user space (x, y up) to the page as shown (points, origin top-left, rotation applied)."""
    box = [float(v) for v in page.obj.get("/CropBox", page.mediabox)]
    llx, lly, urx, ury = (
        min(box[0], box[2]),
        min(box[1], box[3]),
        max(box[0], box[2]),
        max(box[1], box[3]),
    )
    rotation = int(page.obj.get("/Rotate", 0) or 0) % 360

    def shown(x: float, y: float) -> tuple[float, float]:
        if rotation == 90:
            return y - lly, x - llx
        if rotation == 180:
            return urx - x, y - lly
        if rotation == 270:
            return ury - y, urx - x
        return x - llx, ury - y

    return shown


def _rect_shown(shown: Callable[[float, float], tuple[float, float]], rect: Iterable[Any]) -> Box:
    x0, y0, x1, y1 = (float(v) for v in rect)
    a, b = shown(x0, y0), shown(x1, y1)
    return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


def _meets(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# ---- baked boxes inside a pasted drawing ---------------------------------------------------------


def _coloured(operands: Sequence[Any]) -> bool:
    numbers: list[float] = []
    for value in operands:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            return False  # a pattern's name: not a plain colour
    if len(numbers) == 3:
        return max(numbers) - min(numbers) > _COLOURED
    if len(numbers) == 4:  # CMYK: cyan, magenta or yellow ink, not only black
        return max(numbers[:3]) - min(numbers[:3]) > _COLOURED
    return False


def _bbox_area(form: pikepdf.Object) -> float:
    try:
        x0, y0, x1, y1 = (float(v) for v in form.get("/BBox", [0, 0, 0, 0]))
    except (TypeError, ValueError):
        return 0.0
    return abs(x1 - x0) * abs(y1 - y0)


def _box_only_form(form: pikepdf.Object) -> bool:
    """A sub-drawing that only fills rectangles in a colour (a reviewer's box made a form)."""
    try:
        instructions = pikepdf.parse_content_stream(form)
    except (pikepdf.PdfError, TypeError, ValueError):
        return False
    filled = coloured = False
    for instruction in instructions:
        operands, name = list(instruction.operands), str(instruction.operator)
        if name not in _BOX_ONLY:
            return False
        if name in ("rg", "k", "sc", "scn") and _coloured(operands):
            coloured = True
        if name in _FILLS:
            filled = True
    return filled and coloured


def _hide_fills(form: pikepdf.Object, limit: float) -> int:
    """Leave unpainted each small rectangle filled in a colour in `form`'s own content."""
    try:
        instructions = pikepdf.parse_content_stream(form)
    except (pikepdf.PdfError, TypeError, ValueError):
        return 0
    out: list[Any] = []
    path: list[Any] = []
    colour_stack: list[bool] = []
    fill_coloured = False
    hidden = 0
    for instruction in instructions:
        operands, name = list(instruction.operands), str(instruction.operator)
        if name == "q":
            colour_stack.append(fill_coloured)
        elif name == "Q" and colour_stack:
            fill_coloured = colour_stack.pop()
        elif name in ("rg", "k", "sc", "scn"):
            fill_coloured = _coloured(operands)
        elif name in ("g",):
            fill_coloured = False
        if name in _PATH_OPS:
            path.append(instruction)
            continue
        if name in _PAINTS and path:
            rectangles = [p for p in path if str(p.operator) == "re"]
            small = bool(rectangles) and all(
                abs(float(p.operands[2]) * float(p.operands[3])) <= limit for p in rectangles
            )
            out.extend(path)
            if name in _FILLS and fill_coloured and len(rectangles) == len(path) and small:
                out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("n")))
                hidden += 1
            else:
                out.append(instruction)
            path = []
            continue
        out.extend(path)
        path = []
        out.append(instruction)
    out.extend(path)
    if hidden:
        form.write(pikepdf.unparse_content_stream(out))
    return hidden


def _hide_baked(form: pikepdf.Object, drawing_area: float, depth: int = 0) -> tuple[int, int]:
    """(sub-drawings emptied, fills hidden) inside one pasted drawing's appearance."""
    forms = fills = 0
    limit = drawing_area * _SMALL_SHARE
    fills += _hide_fills(form, limit)
    resources = form.get("/Resources")
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    if not isinstance(xobjects, pikepdf.Dictionary) or depth >= _MAX_DEPTH:
        return forms, fills
    for key in list(xobjects.keys()):
        child = xobjects[key]
        if not isinstance(child, pikepdf.Stream) or child.get("/Subtype") != "/Form":
            continue
        area = _bbox_area(child)
        if 0 < area <= limit and _box_only_form(child):
            child.write(b"")
            forms += 1
            continue
        more_forms, more_fills = _hide_baked(child, drawing_area, depth + 1)
        forms += more_forms
        fills += more_fills
    return forms, fills


# ---- the copy, the pictures, the words -----------------------------------------------------------


def _annotation_words(annotation: pikepdf.Object) -> dict[str, Any]:
    text = str(annotation.get("/Contents") or "").replace("\r", " ").strip()
    author = str(annotation.get("/T") or "").strip()
    return {
        "kind": str(annotation.get("/Subtype") or "").lstrip("/") or "annotation",
        "author": author or None,
        "text": " ".join(text.split())[:80] or None,
    }


def _one_page(data: bytes, index: int) -> pikepdf.Pdf:
    source = pikepdf.open(io.BytesIO(data))
    out = pikepdf.new()
    out.pages.append(source.pages[index])
    return out


def _render(document: bytes, box: Box, scale: float) -> Image.Image:
    left, top, right, bottom = box
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(document)
        try:
            page = pdf[0]
            width, height = page.get_size()
            bitmap = page.render(
                scale=scale,
                crop=(left, height - bottom, width - right, top),
                may_draw_forms=True,
            )
            picture: Image.Image = bitmap.to_pil().convert("RGB")
            return picture
        finally:
            pdf.close()


def _png(picture: Image.Image) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", compress_level=3)
    return buffer.getvalue()


def _words(document: bytes, box: Box, combined: bool = True) -> list[dict[str, Any]]:
    """The text in `box` (shown points) of the one-page `document`, as pdftotext -bbox would list
    it, with its colour. `combined`: the file is one of GV's combined sheets, the only file where
    text in colour may be GV's (on an ordinary upload nothing is marked coloured)."""
    found: list[dict[str, Any]] = []
    with pdfplumber.open(io.BytesIO(document)) as opened:
        page = opened.pages[0]
        place = pixel_placement(page, 7200)
        origin, right, down = place(0, 0), place(1000, 0), place(0, 1000)
        ax, ay = (right.x - origin.x) / 1000, (right.y - origin.y) / 1000
        bx, by = (down.x - origin.x) / 1000, (down.y - origin.y) / 1000

        def shown(x: float, top: float) -> tuple[float, float]:
            return (
                (origin.x + ax * x + bx * top) / 100,
                (origin.y + ay * x + by * top) / 100,
            )

        words = page.extract_words(
            keep_blank_chars=False, extra_attrs=["non_stroking_color"], use_text_flow=False
        )
        for word in words:
            corners = [
                shown(float(word[x]), float(word[y]))
                for x in ("x0", "x1")
                for y in ("top", "bottom")
            ]
            xs, ys = [c[0] for c in corners], [c[1] for c in corners]
            spot = (min(xs), min(ys), max(xs), max(ys))
            cx, cy = (spot[0] + spot[2]) / 2, (spot[1] + spot[3]) / 2
            if not (box[0] <= cx <= box[2] and box[1] <= cy <= box[3]):
                continue
            colour = word.get("non_stroking_color")
            values = list(colour) if isinstance(colour, list | tuple) else []
            found.append(
                {
                    "text": str(word["text"]),
                    "box": tuple(round(v, 1) for v in spot),
                    "coloured": combined and _coloured(values),
                }
            )
    found.sort(key=lambda w: (round(w["box"][1] / 4), w["box"][0]))
    return found


def read_under_markup(pdf: Path, page_number: int, box: Box, dpi: int | None = None) -> UnderMarkup:
    """The area `box` (points of the page as shown, origin top-left) of page `page_number` (from 1)
    of `pdf`, as shown and under GV's markup. Raises `ValueError` with a plain reason for a page or
    box not on the file."""
    from app.review.assistant.verify_value import combined_sheet_at

    data = pdf.read_bytes()
    with pikepdf.open(io.BytesIO(data)) as source:
        count = len(source.pages)
    combined = combined_sheet_at(pdf)  # the colour rule: coloured text may be GV's only here
    if not 1 <= page_number <= count:
        raise ValueError(f"there is no page {page_number} (the file has {count})")
    original = _one_page(data, page_number - 1)
    page = original.pages[0]
    shown = _shown_frame(page)
    frame = [float(v) for v in page.obj.get("/CropBox", page.mediabox)]
    width, height = abs(frame[2] - frame[0]), abs(frame[3] - frame[1])
    if int(page.obj.get("/Rotate", 0) or 0) % 180 == 90:
        width, height = height, width
    x0, y0, x1, y1 = box
    area = (
        max(0.0, min(x0, x1)),
        max(0.0, min(y0, y1)),
        min(width, max(x0, x1)),
        min(height, max(y0, y1)),
    )
    if area[2] - area[0] < 2 or area[3] - area[1] < 2:
        raise ValueError(
            f"the box is not on page {page_number} (the page is {round(width)} x "
            f"{round(height)} points, origin top-left)"
        )
    removed: list[dict[str, Any]] = []
    elsewhere = forms = fills = drawings = 0
    kept = pikepdf.Array()
    for annotation in page.obj.get("/Annots") or pikepdf.Array():
        rect = annotation.get("/Rect")
        spot = _rect_shown(shown, rect) if rect is not None and len(rect) == 4 else None
        over = spot is not None and _meets(spot, area)
        subtype = annotation.get("/Subtype")
        if subtype in ("/Link", "/Popup", "/Widget"):
            kept.append(annotation)
            continue
        if carries_drawing(annotation):
            kept.append(annotation)
            appearance = annotation.get("/AP")
            normal = appearance.get("/N") if isinstance(appearance, pikepdf.Dictionary) else None
            if over and isinstance(normal, pikepdf.Stream):
                drawings += 1
                emptied, unpainted = _hide_baked(normal, max(_bbox_area(normal), 1.0))
                forms += emptied
                fills += unpainted
            continue
        if over:
            removed.append(_annotation_words(annotation))
        else:
            elsewhere += 1
    as_shown = _flat(data, page_number - 1)
    page.obj["/Annots"] = kept
    original.flatten_annotations(mode="all")
    buffer = io.BytesIO()
    original.save(buffer)
    under = buffer.getvalue()
    left, top, right, bottom = area
    chosen = fitting_dpi_for(area) if dpi is None else min(fitting_dpi_for(area), max(72, dpi))
    scale = chosen / 72
    first = _render(as_shown, area, scale)
    second = _render(under, area, scale)
    try:
        words = _words(under, area, combined)
    except Exception as failed:  # noqa: BLE001 - the pictures still answer
        _log.warning("read_under_markup could not read the text: %s", type(failed).__name__)
        words = []
    return UnderMarkup(
        as_shown=_png(first),
        under=_png(second),
        width=second.width,
        height=second.height,
        dpi=chosen,
        box=(round(left, 1), round(top, 1), round(right, 1), round(bottom, 1)),
        removed=removed,
        removed_elsewhere=elsewhere,
        hidden_forms=forms,
        hidden_fills=fills,
        drawings=drawings,
        words=words,
    )


def _flat(data: bytes, index: int) -> bytes:
    """The page as it shows, every annotation drawn into it (for the "as shown" picture)."""
    with _one_page(data, index) as copy:
        copy.flatten_annotations(mode="all")
        buffer = io.BytesIO()
        copy.save(buffer)
        return buffer.getvalue()


def fitting_dpi_for(area: Box) -> int:
    """The sharpest dpi (at most 300) at which the area fits the model's picture limit."""
    return fitting_dpi(Decimal(str(area[2] - area[0])), Decimal(str(area[3] - area[1])))
