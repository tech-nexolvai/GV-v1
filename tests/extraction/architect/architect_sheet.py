"""A synthetic architect's sheet: drawings printed as the page's own content, not pasted (#1163).

How an architect issues a set: each elevation is ordinary page content, with a **label block** under
it — a view bubble (a circle printing the view number over a sheet reference), the view title, and
the scale note under the title — inside a sheet border with a title block along the bottom. Written
by hand, byte by byte, like `combined_sheet.py`, whose invented architect's drawing it reuses
(`architect_stream`: cabinets `3' - 4"` and `2' - 2"`, a centre-line `1' - 5"`, a contradicted
`2' - 7"`, a red `9' - 9"`). **No client value appears here.**

* `architect_sheet()` — one view, its drawing placed at `DRAWING_ORIGIN`;
* `architect_sheet(views=2)` — two views side by side, clearly apart;
* `architect_sheet(views=2, crowded=True)` — the second view so close that a piece of the first
  drawing runs past the middle between their titles: not clearly separated;
* `architect_sheet(vendor=True)` — the vendor's kind of drawing (millimetres, `1:10`) under a title;
* `architect_sheet(titled=False)` — the scale note and bubble printed, but no view title;
* `architect_sheet(wall=True)` — a wall line right of the drawing running down past its title;
* `architect_sheet(origin=(dx, dy))` — the same page with a media box that starts at `(dx, dy)`;
* `pasted_sheet(origin=...)` — the architect's drawing pasted as a `/Stamp` on such a page, no
  heading (the crop crash).
"""

from __future__ import annotations

import zlib

from tests.extraction.architect.combined_sheet import architect_stream, vendor_stream

PAGE_HEIGHT = 800
#: Where the (first) drawing's own origin lands on the page, in PDF space.
DRAWING_ORIGIN = (50, 350)
SECOND_ORIGIN = (650, 350)
CROWDED_ORIGIN = (300, 350)


def _text(x: float, y: float, text: str, *, size: float) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"0 g BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET\n".encode()


def _circle(cx: float, cy: float, r: float) -> bytes:
    """A circle as four Bezier arcs, as CAD writes a view bubble."""
    k = 0.5523 * r
    return (
        f"0 0 0 RG 0.5 w {cx + r} {cy} m "
        f"{cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r} c "
        f"{cx - k} {cy + r} {cx - r} {cy + k} {cx - r} {cy} c "
        f"{cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r} c "
        f"{cx + k} {cy - r} {cx + r} {cy - k} {cx + r} {cy} c S\n"
    ).encode()


def _label_block(x: float, number: str, title: str | None, scale: str) -> bytes:
    """A bubble at `x`, the title to its right and the scale note under the title."""
    return (
        _circle(x + 18, 315, 16)
        + f"0 0 0 RG 0.3 w {x + 2} 315 m {x + 34} 315 l S\n".encode()
        + _text(x + 15, 320, number, size=7)
        + _text(x + 8, 305, "ID 9.9", size=5)
        + (b"" if title is None else _text(x + 44, 318, title, size=14))
        + _text(x + 44, 300, scale, size=8)
    )


def _placed(stream: bytes, origin: tuple[float, float]) -> bytes:
    return f"q 1 0 0 1 {origin[0]} {origin[1]} cm\n".encode() + stream + b"Q\n"


def _frame(width: float) -> bytes:
    """The sheet border and a title block along the bottom, with its own small print."""
    return (
        f"0 0 0 RG 0.8 w 20 20 {width - 40} 740 re S 20 80 m {width - 20} 80 l S\n".encode()
        + _text(30, 55, "SHEET X-101", size=12)
        + _text(30, 35, "SCALE: AS NOTED   PAGE 1 OF 1   SYNTHETIC TEST SHEET", size=6)
    )


#: With `wall=True`, a wall line drawn right of the drawing and down past its title, to here.
WALL_X, WALL_BOTTOM = 470, 285


def _page_content(
    *, views: int, crowded: bool, vendor: bool, titled: bool, wall: bool
) -> tuple[bytes, int]:
    width = 1200 if views == 2 else 600
    if vendor:
        body = _placed(vendor_stream(), (50, 400)) + _label_block(
            60, "7", "SYNTHETIC VENDOR STYLE ELEVATION", "1:10"
        )
        return _frame(width) + body, width
    body = _placed(architect_stream(scale_note=False), DRAWING_ORIGIN)
    body += _label_block(60, "3", "SYNTHETIC ELEVATION" if titled else None, '1/4" = 1\'-0"')
    if wall:
        body += f"0 0 0 RG 0.8 w {WALL_X} {WALL_BOTTOM} m {WALL_X} 600 l S\n".encode()
    if views == 2:
        origin = CROWDED_ORIGIN if crowded else SECOND_ORIGIN
        body += _placed(architect_stream(scale_note=False), origin)
        body += _label_block(origin[0] + 10, "4", "SECOND SYNTHETIC ELEVATION", '1/4" = 1\'-0"')
    return _frame(width) + body, width


def _pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _stream(body: bytes) -> bytes:
    compressed = zlib.compress(body)
    return (
        f"<< /Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode()
        + compressed
        + b"\nendstream"
    )


def architect_sheet(
    *,
    views: int = 1,
    crowded: bool = False,
    vendor: bool = False,
    titled: bool = True,
    wall: bool = False,
    origin: tuple[int, int] = (0, 0),
) -> bytes:
    """One page drawn as an architect issues it. `origin` moves the media box's corner (and the
    content with it), as a sheet cut out of a larger set."""
    body, width = _page_content(
        views=views, crowded=crowded, vendor=vendor, titled=titled, wall=wall
    )
    dx, dy = origin
    if dx or dy:
        body = _placed(body, (dx, dy))
    media = f"[{dx} {dy} {dx + width} {dy + PAGE_HEIGHT}]"
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox {media} /Contents 4 0 R "
                "/Resources << /Font << /F1 5 0 R >> >> >>"
            ).encode(),
            _stream(body),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )


#: The pasted drawing's `/Rect` before the page's corner is moved.
PASTED_RECT = (50, 350, 550, 650)


def pasted_sheet(*, origin: tuple[int, int] = (0, 0)) -> bytes:
    """The architect's drawing pasted as a `/Stamp` (at 1:1) on a page whose media box starts at
    `origin`, nothing else on it: no heading, no vendor's drawing."""
    dx, dy = origin
    x0, y0, x1, y1 = PASTED_RECT
    compressed = zlib.compress(architect_stream())
    appearance = (
        f"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [0 0 {x1 - x0} {y1 - y0}] "
        f"/Matrix [1 0 0 1 0 0] /Resources << /Font << /F1 7 0 R >> >> "
        f"/Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode()
        + compressed
        + b"\nendstream"
    )
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [{dx} {dy} {dx + 600} {dy + PAGE_HEIGHT}] "
                "/Annots [5 0 R] /Contents 4 0 R >>"
            ).encode(),
            b"<< /Length 0 >>\nstream\n\nendstream",
            (
                f"<< /Type /Annot /Subtype /Stamp /Rect [{x0 + dx} {y0 + dy} {x1 + dx} {y1 + dy}] "
                "/T (DESIGNER) /AP << /N 6 0 R >> >>"
            ).encode(),
            appearance,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )
