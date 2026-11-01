"""Read only the text the page shows, and AutoCAD's SHX text, which is the vendor's own (#1745).

**Invisible text is in the file but not on the page.** pdfplumber reads every character the content
stream sets, including text on a layer that is switched off, text set invisible (render mode 3) and
text under a white-out box. None of it is flagged. A number nobody can see is not on the drawing,
so a character is read only when the page *as shown* has ink at its spot (`InkPicture.shows`): the
page drawn by PDFium (which hides switched-off layers and invisible text, and paints white-outs) at
`INK_DPI`, and any pixel in the character's box darker than the paper by more than `_INK_LEVEL`.
The test is on the page as drawn, not on the text's colour: red, blue and black ink all count.

**The one named exception: AutoCAD SHX text.** AutoCAD draws SHX lettering as strokes and keeps the
real string in the file, either as invisible text over the strokes (`PDFSHX=2`, the default since
2024) or as comments authored `AutoCAD SHX Text` (`PDFSHX=1`). Both are the vendor's text. The
first needs nothing special: its strokes are ink at its spot, so the check above keeps it. The
second is turned into invisible text at the comment's rectangle (`shx_comment_text`), so the same
check keeps it **only where strokes are drawn under it**: a stale comment over an empty spot is not
read, and its box is the comment's, over the strokes.

**A scan's own OCR text is not typed text** (#1750, item 4). A scanned page is a picture; the
scanner's software often lays its own reading of the picture over it as invisible text (render
mode 3), or under it (text drawn first, the picture painted over it). That text has ink at its
spot, the picture's, so the check above keeps it, and it would be read like the vendor's CAD text,
which the app trusts most. It is another program's OCR guess. A character is the scanner's
(`InkPicture.scanned`) only when all of these hold, so CAD text is never taken for it:

- the page is a scan: its pictures cover at least `SCAN_SHARE` of it;
- the character is inside a picture;
- it has no ink of its own: it is invisible (render mode 3, or 7: invisible and clipping), or a
  picture painted after it covers it;
- the page draws no line work at its spot (the page drawn again with its pictures and text taken
  out): AutoCAD's hidden SHX text sits over its own strokes, so it stays the vendor's text even on
  a page with a big picture.

Visible text over a scan (a stamp or a note typed on it) has ink of its own and stays typed. The
readers keep the scanner's characters apart: `verify_value` never reads them as the drawing's text
(it reads the picture), `dim_labels` lists them as unverified OCR labels (`label_origin.SCAN`),
and `page_health` says the page is a scan whose text came from the scanner's OCR.

Source: the front-door research note (local, 2026-10-19) §1.6-1.7, §2 · Verification:
`tests/review/assistant/test_visible_text.py`, `tests/extraction/test_scan_text.py`
"""

from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pikepdf

from extraction.pdfium_guard import pdfium
from extraction.pdfium_guard import pdfium_raw as pdfium_c

__all__ = [
    "INK_DPI",
    "SCAN_SHARE",
    "SHX_AUTHOR",
    "InkPicture",
    "ink_picture",
    "is_shx_comment",
    "shx_comment_text",
]

#: The dpi the page is drawn at to see where it shows ink: a 4 pt character is still 5 pixels.
INK_DPI: Final = 100
#: How much darker than white a pixel must be to be ink (anti-aliasing of a light grey counts).
_INK_LEVEL: Final = 12
#: How far round a character's box ink is looked for (points).
_INK_PAD_PT: Final = 1.0
#: The author AutoCAD writes on the comments that hold SHX text (`PDFSHX=1`).
SHX_AUTHOR: Final = "AutoCAD SHX Text"
#: The resource name of the font the SHX comments' text is set in.
_SHX_FONT: Final = "/GVShxText"
#: A page is a scan when its pictures cover at least this share of it (`page_health` says
#: "scanned picture" from the same share): only there can text be the scanner's.
SCAN_SHARE: Final = 0.5
#: How deep into pasted drawings (form objects) pictures and text are looked for.
_DEPTH: Final = 6
#: A picture with fewer pixels than this cannot be a scan of half a page (half a letter sheet at
#: 100 dpi is 465,000): a page with no bigger picture is never looked at again, cheaply. Walking a
#: CAD page's objects to find its pictures costs about half a second (30,000 objects).
_SCAN_MIN_PX: Final = 250_000
#: Text that leaves no ink of its own: invisible, and invisible while clipping.
_NO_INK_MODES: Final = frozenset(
    {pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE, pdfium_c.FPDF_TEXTRENDERMODE_CLIP}
)


@dataclass(frozen=True, slots=True)
class InkPicture:
    """A page drawn as shown, in grey, and its scale (pixels per point)."""

    grey: np.ndarray
    scale: float
    scanner: np.ndarray | None = None
    """Where the page's characters are a scan's own OCR text (`_scanner_text`), on the same
    pixels as `grey`; `None` on every page that is not a scan with such text."""

    def scanned(self, box: tuple[float, float, float, float]) -> bool:
        """Whether a character at `box` (shown points, origin top-left) is a scan's own OCR text,
        not typed: its middle is on a spot the scanner's text covers."""
        if self.scanner is None:
            return False
        height, width = self.scanner.shape
        x = max(0, min(width - 1, int((box[0] + box[2]) / 2 * self.scale)))
        y = max(0, min(height - 1, int((box[1] + box[3]) / 2 * self.scale)))
        return bool(self.scanner[y, x])

    def shows(self, box: tuple[float, float, float, float]) -> bool:
        """Whether the page shows ink anywhere in `box` (shown points, origin top-left), or within
        `_INK_PAD_PT` of it: a font without widths gives its characters boxes of no width."""
        height, width = self.grey.shape
        pad = _INK_PAD_PT
        x0 = max(0, min(width - 1, int((box[0] - pad) * self.scale)))
        y0 = max(0, min(height - 1, int((box[1] - pad) * self.scale)))
        x1 = max(x0 + 1, min(width, int(np.ceil((box[2] + pad) * self.scale))))
        y1 = max(y0 + 1, min(height, int(np.ceil((box[3] + pad) * self.scale))))
        return bool((self.grey[y0:y1, x0:x1] < 255 - _INK_LEVEL).any())


def ink_picture(data: bytes, page_index: int = 0, dpi: int = INK_DPI) -> InkPicture:
    """Page `page_index` of `data` as shown (annotations drawn as they are in the file)."""
    document = pdfium.PdfDocument(data)
    try:
        page = document[page_index]
        scale = dpi / 72
        picture = page.render(scale=scale, may_draw_forms=True, grayscale=True)
        grey = np.asarray(picture.to_pil().convert("L"))
        scanner = _scanner_text(page, grey.shape, scale) if _big_picture(data, page_index) else None
    finally:
        document.close()
    return InkPicture(grey=grey, scale=scale, scanner=scanner)


# ---- a scan's own OCR text (#1750) ---------------------------------------------------------------

type _Pixels = tuple[int, int, int, int]


def _big_picture(data: bytes, page_index: int) -> bool:
    """Whether the page (or a drawing pasted in it) holds a picture of at least `_SCAN_MIN_PX`
    pixels: read from its resources, not its content, so it costs next to nothing. A picture
    written inline in the content (not as a resource) is not looked for: scanners write theirs as
    resources."""
    seen: set[tuple[int, int]] = set()

    def big(resources: Any, depth: int) -> bool:
        if not isinstance(resources, pikepdf.Dictionary) or depth > _DEPTH:
            return False
        for xobject in (resources.get("/XObject") or {}).values():
            if not isinstance(xobject, pikepdf.Stream):
                continue
            if xobject.objgen != (0, 0):
                if xobject.objgen in seen:
                    continue
                seen.add(xobject.objgen)
            kind = xobject.get("/Subtype")
            if kind == pikepdf.Name.Image:
                try:
                    pixels = int(xobject.get("/Width", 0)) * int(xobject.get("/Height", 0))
                except (TypeError, ValueError):
                    continue
                if pixels >= _SCAN_MIN_PX:
                    return True
            elif kind == pikepdf.Name.Form and big(xobject.get("/Resources"), depth + 1):
                return True
        return False

    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            return big(pdf.pages[page_index].obj.get("/Resources"), 0)
    except (pikepdf.PdfError, IndexError, ValueError):
        return False


def _scanner_text(page: Any, shape: tuple[int, ...], scale: float) -> np.ndarray | None:
    """Where the page's characters are a scan's own OCR text (see the module's rule), as a mask on
    the ink picture's pixels, or `None`. `page` is changed (its pictures and text taken out to
    see its line work): the caller closes it."""
    height, width = int(shape[0]), int(shape[1])
    textpage = page.get_textpage()
    try:
        if pdfium_c.FPDFText_CountChars(textpage.raw) <= 0:
            return None
        painted, pictures = _paint_order(page, width, height)
        covered = np.zeros((height, width), dtype=bool)
        for _order, (x0, y0, x1, y1) in pictures:
            covered[y0:y1, x0:x1] = True
        if not pictures or covered.mean() < SCAN_SHARE:
            return None  # not a scan: every page drawn in CAD ends here
        chars = _char_spots(page, textpage, width, height)
    finally:
        textpage.close()
    candidates: list[tuple[_Pixels, int | None]] = []
    for box, no_ink, handle in chars:
        x, y = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        if not covered[min(y, height - 1), min(x, width - 1)]:
            continue
        order = painted.get(handle) if handle is not None else None
        under = order is not None and any(
            later > order and p[0] <= x < p[2] and p[1] <= y < p[3] for later, p in pictures
        )
        if no_ink or under:
            candidates.append((box, handle))
    if not candidates:
        return None
    lines = _line_work(page, scale)
    if lines is None:
        return None  # line work unknown: no character is taken for the scanner's
    pad = max(1, round(_INK_PAD_PT * scale))

    def spot(box: _Pixels) -> tuple[slice, slice]:
        return slice(max(0, box[1] - pad), box[3] + pad), slice(max(0, box[0] - pad), box[2] + pad)

    # Line work under any character of a text object makes the whole object the vendor's text
    # (AutoCAD's hidden SHX text over its strokes): a string is never split between the two.
    drawn = {
        handle if handle is not None else id(box)
        for box, handle in candidates
        if (lines[spot(box)] < 255 - _INK_LEVEL).any()
    }
    mask = np.zeros((height, width), dtype=bool)
    for box, handle in candidates:
        if (handle if handle is not None else id(box)) not in drawn:
            mask[spot(box)] = True
    return mask if mask.any() else None


def _device(page: Any, width: int, height: int, x: float, y: float) -> tuple[int, int]:
    """A point in PDF page space as a pixel of the page drawn `width` by `height` (rotation and
    crop box applied, as `page.render` draws it)."""
    dx, dy = ctypes.c_int(), ctypes.c_int()
    pdfium_c.FPDF_PageToDevice(page.raw, 0, 0, width, height, 0, x, y, dx, dy)
    return dx.value, dy.value


def _pixels(
    page: Any, width: int, height: int, left: float, bottom: float, right: float, top: float
) -> _Pixels:
    a = _device(page, width, height, left, bottom)
    b = _device(page, width, height, right, top)
    x0, x1 = sorted((max(0, min(width, a[0])), max(0, min(width, b[0]))))
    y0, y1 = sorted((max(0, min(height, a[1])), max(0, min(height, b[1]))))
    return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)


def _char_spots(
    page: Any, textpage: Any, width: int, height: int
) -> list[tuple[_Pixels, bool, int | None]]:
    """Every character PDFium reads on the page (pasted drawings included): its box in pixels
    (its font's full height, so a scanner's glyphless font still has one), whether it leaves no
    ink of its own (`_NO_INK_MODES`), and its text object (for the order it is painted in)."""
    out: list[tuple[_Pixels, bool, int | None]] = []
    rect = pdfium_c.FS_RECTF()
    for index in range(pdfium_c.FPDFText_CountChars(textpage.raw)):
        code = pdfium_c.FPDFText_GetUnicode(textpage.raw, index)
        if code == 0 or chr(code).isspace() or pdfium_c.FPDFText_IsGenerated(textpage.raw, index):
            continue
        if not pdfium_c.FPDFText_GetLooseCharBox(textpage.raw, index, rect):
            continue
        handle = pdfium_c.FPDFText_GetTextObject(textpage.raw, index)
        mode = pdfium_c.FPDFTextObj_GetTextRenderMode(handle) if handle else None
        box = _pixels(page, width, height, rect.left, rect.bottom, rect.right, rect.top)
        out.append((box, mode in _NO_INK_MODES, _address(handle)))
    return out


def _address(handle: Any) -> int | None:
    return ctypes.cast(handle, ctypes.c_void_p).value if handle else None


def _paint_order(
    page: Any, width: int, height: int
) -> tuple[dict[int, int], list[tuple[int, _Pixels]]]:
    """The order the page paints its objects in (pasted drawings opened where they are painted):
    each text object's place, and each picture's place and box in pixels."""
    texts: dict[int, int] = {}
    pictures: list[tuple[int, _Pixels]] = []
    count = 0

    def walk(form: Any, depth: int) -> None:
        nonlocal count
        total = (
            pdfium_c.FPDFFormObj_CountObjects(form)
            if form is not None
            else pdfium_c.FPDFPage_CountObjects(page.raw)
        )
        for index in range(max(0, total)):
            obj = (
                pdfium_c.FPDFFormObj_GetObject(form, index)
                if form is not None
                else pdfium_c.FPDFPage_GetObject(page.raw, index)
            )
            if not obj:
                continue
            count += 1
            kind = pdfium_c.FPDFPageObj_GetType(obj)
            if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
                address = _address(obj)
                if address is not None:
                    texts[address] = count
            elif kind == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                left, bottom, right, top = (ctypes.c_float() for _ in range(4))
                if pdfium_c.FPDFPageObj_GetBounds(obj, left, bottom, right, top):
                    box = _pixels(
                        page, width, height, left.value, bottom.value, right.value, top.value
                    )
                    pictures.append((count, box))
            elif kind == pdfium_c.FPDF_PAGEOBJ_FORM and depth < _DEPTH:
                walk(obj, depth + 1)

    walk(None, 0)
    return texts, pictures


def _line_work(page: Any, scale: float) -> np.ndarray | None:
    """The page drawn in grey with its pictures and its text taken out: only its line work."""
    try:

        def strip(form: Any, depth: int) -> None:
            total = (
                pdfium_c.FPDFFormObj_CountObjects(form)
                if form is not None
                else pdfium_c.FPDFPage_CountObjects(page.raw)
            )
            for index in reversed(range(max(0, total))):
                obj = (
                    pdfium_c.FPDFFormObj_GetObject(form, index)
                    if form is not None
                    else pdfium_c.FPDFPage_GetObject(page.raw, index)
                )
                if not obj:
                    continue
                kind = pdfium_c.FPDFPageObj_GetType(obj)
                if kind in (pdfium_c.FPDF_PAGEOBJ_TEXT, pdfium_c.FPDF_PAGEOBJ_IMAGE):
                    removed = (
                        pdfium_c.FPDFFormObj_RemoveObject(form, obj)
                        if form is not None
                        else pdfium_c.FPDFPage_RemoveObject(page.raw, obj)
                    )
                    if removed:
                        pdfium_c.FPDFPageObj_Destroy(obj)
                elif kind == pdfium_c.FPDF_PAGEOBJ_FORM and depth < _DEPTH:
                    strip(obj, depth + 1)

        strip(None, 0)
        picture = page.render(scale=scale, may_draw_forms=True, grayscale=True)
        return np.asarray(picture.to_pil().convert("L"))
    except Exception:  # noqa: BLE001 - line work unknown (`_scanner_text` takes nothing)
        return None


def is_shx_comment(annotation: Any) -> bool:
    """Whether an annotation is one of AutoCAD's SHX text comments (its author says so)."""
    author = annotation.get("/T")
    return author is not None and str(author).strip() == SHX_AUTHOR and "/Rect" in annotation


def shx_comment_text(pdf: pikepdf.Pdf, page: pikepdf.Page, comments: list[Any]) -> int:
    """Set each SHX comment's string as invisible text (render mode 3) filling its rectangle, on
    `page` of this copy, so it is read like AutoCAD's own hidden SHX text: only where the page
    shows strokes under it. Returns how many were set."""
    from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]

    parts: list[bytes] = []
    for comment in comments:
        text = str(comment.get("/Contents") or "").strip()
        try:
            rect = [float(v) for v in comment["/Rect"]]
        except (TypeError, ValueError, KeyError):
            continue
        if len(rect) != 4:
            continue
        x0, x1 = sorted((rect[0], rect[2]))
        y0, y1 = sorted((rect[1], rect[3]))
        width, height = x1 - x0, y1 - y0
        if not text or width <= 0 or height <= 0 or not text.isprintable():
            continue
        size = height
        natural = stringWidth(text, "Helvetica", size)
        stretch = 100 * width / natural if natural > 0 else 100
        words = text.encode("latin-1", "replace")
        words = words.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        parts.append(
            f"BT 3 Tr {_SHX_FONT} {size:.3f} Tf {stretch:.3f} Tz "
            f"{x0:.3f} {y0 + 0.2 * size:.3f} Td (".encode() + words + b") Tj ET\n"
        )
    if not parts:
        return 0
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    page.add_resource(font, pikepdf.Name.Font, name=pikepdf.Name(_SHX_FONT))
    page.contents_add(b"q\n" + b"".join(parts) + b"Q\n", prepend=False)
    return len(parts)
