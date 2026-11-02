"""A scan's own OCR text is told from typed text (#1750, item 4), on synthetic PDFs, $0.

Each page prints `36 1/4"` at the same spot, one way per case:
- over a full-page picture as invisible text (render mode 3), or under it (the picture painted
  over the text): the scanner's OCR text;
- typed visibly over the picture (a note typed on a scan), on a plain vector page, as AutoCAD's
  hidden SHX text over its strokes on a page with a full-page picture, or as invisible text over a
  small picture (a logo): typed, as before.
"""

from __future__ import annotations

import io

import numpy as np
import pikepdf
import pytest
from PIL import Image

from extraction.visible_text import ink_picture

WIDTH, HEIGHT = 600.0, 400.0
#: Where the value is printed, in shown points (origin top-left).
SPOT = (300.0, HEIGHT - 212.0, 340.0, HEIGHT - 198.0)
VALUE = b'BT /F1 10 Tf 300 200 Td (36 1/4") Tj ET\n'
INVISIBLE = b'BT 3 Tr /F1 10 Tf 300 200 Td (36 1/4") Tj ET\n'
CLIPPING = b'BT 7 Tr /F1 10 Tf 300 200 Td (36 1/4") Tj ET\n'
TYPED = b"0 g " + VALUE
SCAN = f"q {WIDTH} 0 0 {HEIGHT} 0 0 cm /Im0 Do Q\n".encode()
LOGO = b"q 120 0 0 60 290 180 cm /Im0 Do Q\n"


def strokes() -> bytes:
    """Letter-like strokes under the value: what AutoCAD's SHX lettering draws."""
    out = b"0 0 0 RG 0.6 w "
    for n in range(7):
        left = 300 + n * 5.5
        out += f"{left} 200 m {left + 2} 209 l {left + 4} 200 l S ".encode()
    return out


def page(content: bytes, *, rotate: int = 0) -> bytes:
    """One page with a scanned picture available as `/Im0` (dark where the value is printed)."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    # 1200 x 800 pixels: a scan at 144 dpi (a picture of fewer pixels is never a scan).
    pixels = np.full((800, 1200), 255, dtype=np.uint8)
    pixels[376:428, 592:692] = 40
    image = pikepdf.Stream(pdf, Image.fromarray(pixels).tobytes())
    image["/Type"] = pikepdf.Name.XObject
    image["/Subtype"] = pikepdf.Name.Image
    image["/Width"], image["/Height"] = 1200, 800
    image["/ColorSpace"] = pikepdf.Name.DeviceGray
    image["/BitsPerComponent"] = 8
    resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=font), XObject=pikepdf.Dictionary(Im0=image)
    )
    sheet = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, WIDTH, HEIGHT],
        Resources=resources,
        Contents=pdf.make_stream(content),
    )
    if rotate:
        sheet["/Rotate"] = rotate
    pdf.pages.append(pikepdf.Page(sheet))
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


@pytest.mark.parametrize(
    "content",
    [SCAN + INVISIBLE, SCAN + CLIPPING, TYPED + SCAN],
    ids=["invisible-over-picture", "invisible-clipping", "under-the-picture"],
)
def test_a_scans_own_ocr_text_is_the_scanners(content: bytes) -> None:
    ink = ink_picture(page(content))
    assert ink.shows(SPOT)  # the picture's ink is there: the old check kept it as typed
    assert ink.scanned(SPOT)


@pytest.mark.parametrize(
    "content",
    [
        TYPED,
        SCAN + TYPED,
        TYPED + INVISIBLE.replace(b"300 200", b"100 100"),
        SCAN + strokes() + INVISIBLE,
        LOGO + INVISIBLE,
    ],
    ids=[
        "vector-page",
        "typed-over-a-scan",
        "vector-page-with-invisible-text",
        "shx-over-strokes-on-a-picture",
        "invisible-over-a-logo",
    ],
)
def test_typed_text_is_never_the_scanners(content: bytes) -> None:
    """A vector page keeps no mask at all; a picture under typed text, line work under hidden
    text (AutoCAD's SHX) or a picture covering less than half the page leave the text typed."""
    ink = ink_picture(page(content))
    assert not ink.scanned(SPOT)
    if b"Im0" not in content:
        assert ink.scanner is None


def test_a_turned_scan_is_found_where_the_page_shows_it() -> None:
    """A scan stored turned 90 degrees: the mask is in the page as shown, like every reader's box
    (the value's spot turned with it)."""
    ink = ink_picture(page(SCAN + INVISIBLE, rotate=90))
    # Turned a quarter clockwise: shown x is the PDF's y from the bottom, shown y the PDF's x.
    assert ink.scanned((198.0, 300.0, 212.0, 340.0))
    assert not ink.scanned((20.0, 20.0, 40.0, 40.0))
