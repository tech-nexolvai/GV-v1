"""A synthetic combined sheet: an architect's drawing pasted above a vendor's, with headings (#1052).

Written by hand, byte by byte, the way `tests/extraction/test_annotations.py` builds its pages, so
every mark has a reason to be there. **No client value appears here**: the drawing is invented.

The architect's drawing (`/Stamp`, annotation 1) is pasted at 1:1 (`/Rect` = `/BBox`), prints
`1/4" = 1'-0"` — 1.5 pt per real inch — and draws, over a solid floor line:

* two cabinets, 40" and 26" wide (60 pt and 39 pt), dimensioned on row 1 as `3' - 4"` and
  `2' - 2"`, ticks on their solid sides, dashed grey extension lines through everything as the
  client's CAD draws them;
* row 2 under it: `1' - 5"` from the left cabinet's side to an outlet's centre line, dashed, with
  `CL` printed on it — a centre-line dimension;
* row 3: a span 36 pt wide (24") labelled `2' - 7"` — a label that disagrees with its drawn length;
* row 4: a span labelled only in red, `9' - 9"` — a reviewer's mark inside the snapshot.

With `extras=True` the floor line runs on to the right and two more spans stand on it, on a row of
their own:

* `3' - 0"` over a hatched strip of wood blocking (54 pt, parallel 45-degree strokes inside a
  rectangle): its ends look like a cabinet's sides, but it is not casework;
* `1' - 8"` from a wall to the side of a credenza standing apart (30 pt): a clearance between two
  different things, not one object's width.

The vendor's drawing (annotation 3) prints `1:10` and millimetre labels with bracketed inches.
Headings are `/FreeText` notes (annotations 0 and 2) above each drawing, as on the client's sheets.
"""

from __future__ import annotations

import zlib

PAGE_HEIGHT = 800

#: Where the architect's drawing is pasted on the page: `/Rect` in PDF space.
ARCH_RECT = (50, 430, 550, 730)
VENDOR_RECT = (50, 50, 550, 380)

#: Row 1's ticks in the architect's own space, and so on the page at x + 50.
LEFT, MIDDLE, RIGHT = 60, 120, 159
OUTLET = 85.5


def _dashed(x: float, y0: float, y1: float) -> bytes:
    """A grey dashed vertical, 3 on 3 off, exploded into pieces as the client's CAD writes it."""
    parts = [b"0.53 0.53 0.53 RG 0.2 w"]
    y = y0
    while y < y1:
        top = min(y + 3, y1)
        parts.append(f"{x} {y} m {x} {top} l S".encode())
        y += 6
    parts.append(b"0 0 0 RG")
    return b"\n".join(parts) + b"\n"


def _slash(x: float, y: float) -> bytes:
    return f"0.6 w {x - 2} {y - 2} m {x + 2} {y + 2} l S\n".encode()


def _row(y: float, ticks: list[float]) -> bytes:
    body = f"0.2 w {ticks[0] - 3} {y} m {ticks[-1] + 3} {y} l S\n".encode()
    return body + b"".join(_slash(x, y) for x in ticks)


def _text(x: float, y: float, text: str, *, size: int = 6, colour: bytes = b"0 g") -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return colour + f" BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET 0 g\n".encode()


def _extras() -> bytes:
    """Wood blocking drawn hatched, and a wall with a credenza standing apart from it."""
    parts = [
        b"0 0 0 RG 0.5 w",
        # The blocking: a rectangle on the floor line, filled with parallel strokes.
        b"300 60 m 354 60 l 354 72 l 300 72 l h S",
        b"0.2 w",
        *(f"{x} 60 m {x + 12} 72 l S".encode() for x in range(300, 343, 3)),
        b"0.5 w",
        # The wall, full height, and the credenza 30 pt to its right: two sides and a top.
        b"380 60 m 380 250 l S",
        b"410 60 m 410 90 l S 470 60 m 470 90 l S 410 90 m 470 90 l S",
    ]
    stream = b"\n".join(parts) + b"\n"
    for x in (300, 354, 380, 410):
        stream += _dashed(x, 40, 200)
    stream += _row(47, [300, 354]) + _text(318, 50, "3' - 0\"")
    stream += _row(47, [380, 410]) + _text(386, 50, "1' - 8\"")
    return stream


def architect_stream(*, centre_mark: bool = True, extras: bool = False) -> bytes:
    """The architect's drawing, in its own space (0..500 by 0..300)."""
    parts = [
        b"0 0 0 RG 0.5 w",
        # The floor line, and the two cabinets standing on it (solid sides).
        b"50 60 m 480 60 l S" if extras else b"50 60 m 260 60 l S",
        f"{LEFT} 60 m {LEFT} 120 l S {MIDDLE} 60 m {MIDDLE} 120 l S".encode(),
        f"{RIGHT} 60 m {RIGHT} 120 l S {LEFT} 120 m {RIGHT} 120 l S".encode(),
        # An outlet drawn above the cabinets: a small box on its centre line.
        f"{OUTLET - 3} 150 m {OUTLET + 3} 150 l {OUTLET + 3} 156 l {OUTLET - 3} 156 l h S".encode(),
    ]
    stream = b"\n".join(parts) + b"\n"
    # Extension lines: dashed, from the lowest row up through the drawing, at every tick.
    for x in (LEFT, MIDDLE, RIGHT, OUTLET):
        stream += _dashed(x, 3, 200)
    # Row 1: the cabinets' widths.
    stream += _row(40, [LEFT, MIDDLE, RIGHT])
    stream += _text(82.5, 43, "3' - 4\"") + _text(132, 43, "2' - 2\"")
    # Row 2: from the cabinet's side to the outlet's centre line.
    stream += _row(25, [LEFT, OUTLET])
    stream += _text(65, 28, "1' - 5\"")
    if centre_mark:
        stream += _text(OUTLET - 3, 205, "CL")
    # Row 3: a label that disagrees with the length drawn under it (36 pt is 24", not 30").
    stream += _row(10, [200, 236])
    stream += _text(210, 13, "2' - 7\"")
    # Row 4: labelled only in a reviewer's red.
    stream += _row(250, [300, 360])
    stream += _text(320, 253, "9' - 9\"", colour=b"1 0 0 rg")
    # The scale note, as the client's title bubble prints it.
    stream += _text(200, 280, '1/4" = 1\'-0"', size=8)
    if extras:
        stream += _extras()
    return stream


def vendor_stream() -> bytes:
    """The vendor's drawing: a ratio scale and millimetre labels with bracketed inches."""
    return (
        b"0 0 0 RG 0.5 w 40 100 m 300 100 l S\n"
        + _text(60, 110, "457 [18]")
        + _text(160, 110, "610 [24]")
        + _text(200, 20, "1:10", size=8)
    )


def _appearance(stream: bytes, rect: tuple[int, int, int, int], font_object: int) -> bytes:
    compressed = zlib.compress(stream)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    return (
        f"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [0 0 {width} {height}] "
        f"/Matrix [1 0 0 1 0 0] /Resources << /Font << /F1 {font_object} 0 R >> >> "
        f"/Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode()
        + compressed
        + b"\nendstream"
    )


def _stamp(rect: tuple[int, int, int, int], appearance_object: int) -> bytes:
    return (
        f"<< /Type /Annot /Subtype /Stamp /Rect [{rect[0]} {rect[1]} {rect[2]} {rect[3]}] "
        f"/T (DESIGNER) /AP << /N {appearance_object} 0 R >> >>".encode()
    )


def _note(text: str, rect: tuple[int, int, int, int]) -> bytes:
    return (
        f"<< /Type /Annot /Subtype /FreeText /Rect [{rect[0]} {rect[1]} {rect[2]} {rect[3]}] "
        f"/Contents ({text}) /T (REVIEWER) >>".encode()
    )


def combined_sheet(
    *, headings: bool = True, centre_mark: bool = True, extras: bool = False
) -> bytes:
    """The whole one-page PDF. `headings=False` leaves the two labels off."""
    annotations = [
        _note("ID SET ELEVATION", (10, 760, 300, 780)),
        _stamp(ARCH_RECT, 9),
        _note("VENDOR'S SHOP DRAWING ELEVATION", (10, 400, 300, 420)),
        _stamp(VENDOR_RECT, 10),
    ]
    if not headings:
        annotations[0] = _note("GENERAL NOTES", (10, 760, 300, 780))
        annotations[2] = _note("SEE SPECIFICATION", (10, 400, 300, 420))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800]"
            b" /Annots [5 0 R 6 0 R 7 0 R 8 0 R] /Contents 4 0 R >>"
        ),
        b"<< /Length 0 >>\nstream\n\nendstream",
        *annotations,
        _appearance(architect_stream(centre_mark=centre_mark, extras=extras), ARCH_RECT, 11),
        _appearance(vendor_stream(), VENDOR_RECT, 11),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
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
