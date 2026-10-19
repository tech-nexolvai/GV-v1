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

Source: the front-door research note (local, 2026-10-19) §1.6-1.7, §2 · Verification:
`tests/review/assistant/test_visible_text.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]

__all__ = [
    "INK_DPI",
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


@dataclass(frozen=True, slots=True)
class InkPicture:
    """A page drawn as shown, in grey, and its scale (pixels per point)."""

    grey: np.ndarray
    scale: float

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
    finally:
        document.close()
    return InkPicture(grey=grey, scale=scale)


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
