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
* `ticks="big"` / `"arrow"` — 7 pt slashes, or arrowheads in place of slashes;
* `notes=True` — general notes printed beside the view; `title_block_note=True` — a title over a
  scale note inside the title block; `joined_border=True` — the drawing's ink joined to the border;
  `stacked=True` — a second view lower down whose drawing stands level with the first's title;
* `approval_stamp=True` — an "approved" stamp pasted over the drawing (no dimension in it);
* `pasted_with_heading=True` — a combined sheet: the architect's drawing pasted beside the page's
  own drawing under an "ID SET ELEVATION" heading;
* `printed_at=0.5` — the whole sheet printed at half size, its scale note unchanged;
* `crop=(l, b, r, t)` — a crop box inside the media box (`pasted_sheet` takes it too);
* `architect_sheet(origin=(dx, dy))` — the same page with a media box that starts at `(dx, dy)`;
* `pasted_sheet(origin=...)` — the architect's drawing pasted as a `/Stamp` on such a page, no
  heading (the crop crash).
"""

from __future__ import annotations

import re
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


def _label_block(
    x: float, number: str, title: str | None, scale: str, *, base: float = 315
) -> bytes:
    """A bubble at `x` (its middle at height `base`), the title to its right and the scale note
    under the title."""
    return (
        _circle(x + 18, base, 16)
        + f"0 0 0 RG 0.3 w {x + 2} {base} m {x + 34} {base} l S\n".encode()
        + _text(x + 15, base + 5, number, size=7)
        + _text(x + 8, base - 10, "ID 9.9", size=5)
        + (b"" if title is None else _text(x + 44, base + 3, title, size=14))
        + _text(x + 44, base - 15, scale, size=8)
    )


def _casework() -> bytes:
    """A row of plain cabinet boxes (line-work only), so the vendor-style drawing is a drawing."""
    return (
        b"0 0 0 RG 0.5 w "
        + b" ".join(
            f"{x} 100 m {x} 160 l S {x} 160 m {x + 40} 160 l S".encode() for x in range(40, 300, 40)
        )
        + b"\n"
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
WALL_X, WALL_BOTTOM = 318, 285
#: With `notes=True`, a column of general notes printed beside the view, from here rightwards.
NOTES_X = 420


def _ticks(stream: bytes, ticks: str) -> bytes:
    """The drawing's tick slashes redrawn: `big` (7 pt across, still a slash) or `arrow`
    (arrowheads pointing at the tick from both sides, 7 by 3 pt: never a slash)."""
    if ticks == "slash":
        return stream

    def redraw(match: re.Match[bytes]) -> bytes:
        x0, y0, x1, y1 = (float(value) for value in match.groups())
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if ticks == "big":
            return f"0.6 w {cx - 3.5} {cy - 3.5} m {cx + 3.5} {cy + 3.5} l S".encode()
        return (
            f"{cx} {cy} m {cx - 7} {cy + 1.5} l {cx - 7} {cy - 1.5} l h f "
            f"{cx} {cy} m {cx + 7} {cy + 1.5} l {cx + 7} {cy - 1.5} l h f"
        ).encode()

    return re.sub(rb"0\.6 w (\S+) (\S+) m (\S+) (\S+) l S", redraw, stream)


def _drawing(ticks: str) -> bytes:
    return _ticks(architect_stream(scale_note=False), ticks)


def _page_content(
    *,
    views: int,
    crowded: bool,
    vendor: bool,
    titled: bool,
    wall: bool,
    ticks: str,
    notes: bool,
    title_block_note: bool,
    joined_border: bool,
    stacked: bool,
    wide: bool = False,
) -> tuple[bytes, int]:
    width = 1200 if views == 2 or stacked or wide else 600
    if vendor:
        body = _placed(vendor_stream() + _casework(), (50, 400)) + _label_block(
            60, "7", "SYNTHETIC VENDOR STYLE ELEVATION", "1:10"
        )
        return _frame(width) + body, width
    body = _placed(_drawing(ticks), DRAWING_ORIGIN)
    body += _label_block(60, "3", "SYNTHETIC ELEVATION" if titled else None, '1/4" = 1\'-0"')
    if wall:
        body += f"0 0 0 RG 0.8 w {WALL_X} {WALL_BOTTOM} m {WALL_X} 600 l S\n".encode()
    if views == 2:
        origin = CROWDED_ORIGIN if crowded else SECOND_ORIGIN
        body += _placed(_drawing(ticks), origin)
        body += _label_block(origin[0] + 10, "4", "SECOND SYNTHETIC ELEVATION", '1/4" = 1\'-0"')
    if stacked:
        # A second view lower down at the right: its drawing reaches up level with the first
        # view's title, and stands right above its own title.
        body += _placed(_drawing(ticks), (650, 275))
        body += _label_block(660, "5", "LOWER SYNTHETIC ELEVATION", '1/4" = 1\'-0"', base=250)
    if notes:
        lines = ["GENERAL NOTES", "1. VERIFY ALL SIZES ON SITE", "2. 2' - 0\" CLEAR TO WALL"]
        body += b"".join(
            _text(NOTES_X, 600 - 14 * row, line, size=6) for row, line in enumerate(lines)
        )
    if title_block_note:
        body += _text(300, 60, "DRAWING TITLE", size=8) + _text(
            300, 48, 'SCALE: 1/4" = 1\'-0"', size=6
        )
    if joined_border:
        # The floor line drawn on to the sheet's border: the drawing's ink joins the border's.
        body += b"0 0 0 RG 0.5 w 20 410 m 100 410 l S\n"
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


#: With `pasted_with_heading=True`: the architect's drawing pasted at the right of a 1200 pt page
#: under an "ID SET ELEVATION" heading, beside the page's own drawing (a combined sheet).
PASTED_BESIDE_RECT = (650, 350, 1150, 650)


def _annotations(approval_stamp: bool, pasted_with_heading: bool) -> tuple[str, list[bytes]]:
    """The page's annotations from object 6 on: an approval stamp pasted over the drawing (a box
    and the words, no dimension), and/or the architect's drawing pasted with its printed heading."""
    objects: list[bytes] = []
    annots: list[int] = []
    if approval_stamp:
        number = 6 + len(objects)
        body = (
            b"1 0 0 RG 1 w 2 2 176 36 re S 1 0 0 rg BT /F1 12 Tf 10 14 Td (APPROVED AS NOTED) Tj ET"
        )
        objects += [
            (
                b"<< /Type /Annot /Subtype /Stamp /Rect [380 600 560 640] /T (OFFICE) "
                + f"/AP << /N {number + 1} 0 R >> >>".encode()
            ),
            (
                b"<< /Type /XObject /Subtype /Form /BBox [0 0 180 40] /Resources << /Font << "
                + f"/F1 5 0 R >> >> /Length {len(body)} >>\nstream\n".encode()
                + body
                + b"\nendstream"
            ),
        ]
        annots.append(number)
    if pasted_with_heading:
        number = 6 + len(objects)
        x0, y0, x1, y1 = PASTED_BESIDE_RECT
        compressed = zlib.compress(architect_stream())
        objects += [
            (
                f"<< /Type /Annot /Subtype /FreeText /Rect [{x0} {y1 + 10} {x0 + 290} {y1 + 30}] "
                "/Contents (ID SET ELEVATION) /T (REVIEWER) >>"
            ).encode(),
            (
                f"<< /Type /Annot /Subtype /Stamp /Rect [{x0} {y0} {x1} {y1}] /T (DESIGNER) "
                f"/AP << /N {number + 2} 0 R >> >>"
            ).encode(),
            (
                f"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [0 0 {x1 - x0} {y1 - y0}] "
                f"/Matrix [1 0 0 1 0 0] /Resources << /Font << /F1 5 0 R >> >> "
                f"/Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode()
                + compressed
                + b"\nendstream"
            ),
        ]
        annots += [number, number + 1]
    if not annots:
        return "", []
    return " /Annots [" + " ".join(f"{number} 0 R" for number in annots) + "]", objects


def _crop_box(crop: tuple[int, int, int, int] | None, dx: int, dy: int, width: int) -> str:
    if crop is None:
        return ""
    left, bottom, right, top = crop
    return f" /CropBox [{dx + left} {dy + bottom} {dx + width - right} {dy + PAGE_HEIGHT - top}]"


def architect_sheet(
    *,
    views: int = 1,
    crowded: bool = False,
    vendor: bool = False,
    titled: bool = True,
    wall: bool = False,
    ticks: str = "slash",
    notes: bool = False,
    title_block_note: bool = False,
    joined_border: bool = False,
    stacked: bool = False,
    approval_stamp: bool = False,
    pasted_with_heading: bool = False,
    printed_at: float = 1.0,
    origin: tuple[int, int] = (0, 0),
    crop: tuple[int, int, int, int] | None = None,
) -> bytes:
    """One page drawn as an architect issues it. `origin` moves the media box's corner (and the
    content with it), as a sheet cut out of a larger set; `crop` sets a crop box that many points
    inside the media box (left, bottom, right, top); `printed_at` shrinks the whole sheet, as a
    reduced print does, its scale note unchanged."""
    body, width = _page_content(
        views=views,
        crowded=crowded,
        vendor=vendor,
        titled=titled,
        wall=wall,
        ticks=ticks,
        notes=notes,
        title_block_note=title_block_note,
        joined_border=joined_border,
        stacked=stacked,
        wide=pasted_with_heading,
    )
    if printed_at != 1.0:
        body = f"q {printed_at} 0 0 {printed_at} 0 0 cm\n".encode() + body + b"Q\n"
    dx, dy = origin
    if dx or dy:
        body = _placed(body, (dx, dy))
    media = f"[{dx} {dy} {dx + width} {dy + PAGE_HEIGHT}]"
    annots, extra = _annotations(approval_stamp, pasted_with_heading)
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox {media}{_crop_box(crop, dx, dy, width)}"
                f"{annots} /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
            ).encode(),
            _stream(body),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            *extra,
        ]
    )


#: The pasted drawing's `/Rect` before the page's corner is moved.
PASTED_RECT = (50, 350, 550, 650)


def pasted_sheet(
    *, origin: tuple[int, int] = (0, 0), crop: tuple[int, int, int, int] | None = None
) -> bytes:
    """The architect's drawing pasted as a `/Stamp` (at 1:1) on a page whose media box starts at
    `origin` (and, with `crop`, whose crop box sits inside it), nothing else on it: no heading, no
    vendor's drawing."""
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
                f"<< /Type /Page /Parent 2 0 R /MediaBox [{dx} {dy} {dx + 600} {dy + PAGE_HEIGHT}]"
                f"{_crop_box(crop, dx, dy, 600)} /Annots [5 0 R] /Contents 4 0 R >>"
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
