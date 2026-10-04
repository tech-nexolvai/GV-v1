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

**Where GV's marks are, for the agreement gate and the part pictures (#901, #929).** The same rule
finds what a reader shown the vendor's drawing may still see of somebody's markup: text set in colour
(`coloured_text`), every line, rectangle, curve and fill drawn in colour, of any size
(`coloured_paths`), and a stamp pasted onto one of the page's pasted drawings (`pasted_stamps`). None
of these is ever read as a value; they are only places.

Source: formats phase 1 · Verification: `tests/extraction/test_stamp_text.py`
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

import pdfplumber
import pikepdf

from evidence.coordinates import ImagePoint, PdfPoint
from extraction.reader import (
    MissingSpace,
    PageContents,
    UnreadablePdf,
    page_frame,
    pixel_placement,
    read_page_contents,
)

__all__ = [
    "ColouredPath",
    "PixelBox",
    "StampCharacters",
    "StampText",
    "coloured_paths",
    "coloured_text",
    "drawing_ink",
    "pasted_stamps",
    "path_ink",
    "read_stamp_text",
    "stamp_character_counts",
    "stamps_only",
]

#: A box in a page's pixels: `(left, top, right, bottom)`, edges included.
type PixelBox = tuple[int, int, int, int]

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
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
    missing_space: MissingSpace,
) -> StampText:
    """The readable text runs inside this page's pasted drawings, exactly as the file holds them.

    `missing_space` is the reader's, and has no default there either (#912)."""
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
        missing_space=missing_space,
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


def coloured_text(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
    missing_space: MissingSpace,
) -> tuple[tuple[int, int, int, int], ...]:
    """Where the page's pasted drawings set text in colour: `(left, top, right, bottom)` of each run,
    in the page's pixels at `dpi`.

    The characters `read_stamp_text` refuses, read by the same reader on the same copy of the page.
    This module never reads them as values, because coloured text inside a snapshot is somebody's
    markup; a vision reader is still shown it, so the agreement gate asks where it is (#901). Runs
    set aside unread (#738) are places too, and are included.
    """
    contents = read_page_contents(
        stamps_only(data, page_index),
        page_index,
        document_version_id=document_version_id,
        dpi=dpi,
        missing_space=missing_space,
        keep_char=lambda char: not drawing_ink(char),
    )
    extents = [item.image_extent for item in contents.texts] + [
        label.image_extent for label in contents.set_aside
    ]
    return tuple(
        (
            min(point.x for point in extent),
            min(point.y for point in extent),
            max(point.x for point in extent),
            max(point.y for point in extent),
        )
        for extent in extents
        if extent
    )


def stamp_character_counts(data: bytes, page_index: int) -> StampCharacters:
    """The characters inside the page's pasted drawings, counted for the survey."""
    return _character_counts(stamps_only(data, page_index), page_index)


def _boxes_meet(first: PixelBox, second: PixelBox) -> bool:
    """Whether two boxes share any point, edges included."""
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _line_meets(line: tuple[int, int, int, int], box: PixelBox) -> bool:
    """Whether a straight line from `(x0, y0)` to `(x1, y1)` has any point in `box`, edges included.

    The line is clipped to the box one edge at a time (Liang and Barsky's method), in whole pixels
    and exact fractions, so a line that only passes near a corner is not counted and one that grazes
    an edge is.
    """
    x0, y0, x1, y1 = line
    left, top, right, bottom = box
    across, down = x1 - x0, y1 - y0
    enters, leaves = Fraction(0), Fraction(1)
    for step, room in (
        (-across, x0 - left),
        (across, right - x0),
        (-down, y0 - top),
        (down, bottom - y0),
    ):
        if step == 0:
            if room < 0:
                return False
            continue
        at = Fraction(room, step)
        if step < 0:
            enters = max(enters, at)
        else:
            leaves = min(leaves, at)
        if enters > leaves:
            return False
    return True


def _box_of(points: Sequence[ImagePoint]) -> PixelBox:
    return (
        min(point.x for point in points),
        min(point.y for point in points),
        max(point.x for point in points),
        max(point.y for point in points),
    )


#: A sub-path's corners in a page's pixels, in drawing order; it closes back to its first.
type Ring = tuple[tuple[int, int], ...]


def _covers(point: tuple[int, int], rings: tuple[Ring, ...], even_odd: bool) -> bool:
    """Whether a fill of `rings` covers `point`, by the fill's own rule: the non-zero winding rule,
    or the even-odd rule where the file fills by it (PDF 32000-1 §8.5.3.3). Whole pixels only.

    A ray runs from the point along the row; each side it crosses upwards or downwards turns the
    count, by which side of the edge the point lies.
    """
    x, y = point
    winding = crossings = 0
    for ring in rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
            side = (x1 - x0) * (y - y0) - (x - x0) * (y1 - y0)
            if y0 <= y < y1 and side > 0:
                winding += 1
                crossings += 1
            elif y1 <= y < y0 and side < 0:
                winding -= 1
                crossings += 1
    return crossings % 2 == 1 if even_odd else winding != 0


@dataclass(frozen=True, slots=True)
class ColouredPath:
    """One path the page's pasted drawings draw in colour, in the page's pixels at the dpi it was
    read at (#929): what of the page it can show.

    `lines` are its straight pieces, `(x0, y0, x1, y1)`: a stroked path's, and a filled path's
    outline with each sub-path closed, as a fill closes it. `areas` are boxes it may cover anywhere
    inside: the box of a curve's ends and control points, which holds the curve, or, where the
    path's pieces cannot be told apart, the whole path's box. `rings` are a filled path's sub-paths,
    corner by corner, for whether the fill covers a place none of its outline reaches; `even_odd`
    is the rule it is filled by. A path only stroked has no rings.

    What the file draws, rounded to whole pixels as the text boxes are; a curve's box is wider than
    the curve, never narrower. The width of a line is not counted: a line running just outside a
    crop, closer than half its own width, is not in it.
    """

    lines: tuple[tuple[int, int, int, int], ...]
    areas: tuple[PixelBox, ...]
    rings: tuple[Ring, ...]
    even_odd: bool

    def meets(self, box: PixelBox) -> bool:
        """Whether any part of the path lies in `box`, edges included.

        Its outline is asked first: a line or a curve's box in the box. A box no part of the
        outline reaches lies wholly inside the fill or wholly outside it, so one of its corners
        answers for all of it; a curve has been ruled out there, so its chord stands in for it.
        """
        if any(_boxes_meet(area, box) for area in self.areas) or any(
            _line_meets(line, box) for line in self.lines
        ):
            return True
        return bool(self.rings) and _covers((box[0], box[1]), self.rings, self.even_odd)


def _pieces(
    commands: Sequence[Any], place: Callable[[object, object], ImagePoint]
) -> tuple[list[tuple[int, int, int, int]], list[PixelBox], list[list[tuple[int, int]]]] | None:
    """A path's straight lines, its curves' boxes and its sub-paths' corners, from the commands
    pdfplumber keeps (`m`, `l`, `c`, `v`, `y`, `h`); `None` where a command is not one of those, or
    comes before the path has started."""
    lines: list[tuple[int, int, int, int]] = []
    areas: list[PixelBox] = []
    rings: list[list[tuple[int, int]]] = []
    start: ImagePoint | None = None
    here: ImagePoint | None = None
    for command in commands:
        operator, points = command[0], [place(x, top) for x, top in command[1:]]
        if operator == "m" and len(points) == 1:
            start = here = points[0]
            rings.append([(here.x, here.y)])
        elif here is None or start is None:
            return None
        elif operator == "l" and len(points) == 1:
            lines.append((here.x, here.y, points[0].x, points[0].y))
            here = points[0]
            rings[-1].append((here.x, here.y))
        elif operator in ("c", "v", "y") and points:
            areas.append(_box_of([here, *points]))
            here = points[-1]
            rings[-1].append((here.x, here.y))
        elif operator == "h" and not points:
            lines.append((here.x, here.y, start.x, start.y))
            here = start
            rings[-1].append((here.x, here.y))
            rings.append([(here.x, here.y)])
        else:
            return None
    return lines, areas, [ring for ring in rings if len(ring) > 1]


def _coloured_path(
    path: dict[str, Any], place: Callable[[object, object], ImagePoint]
) -> ColouredPath:
    """What one line, rectangle or curve drawn in colour can show, in the page's pixels."""
    commands = path.get("path") or ()
    points = [place(x, top) for command in commands for x, top in command[1:]]
    if not points:
        points = [
            place(path["x0"], path["top"]),
            place(path["x1"], path["bottom"]),
        ]
    whole = ColouredPath(lines=(), areas=(_box_of(points),), rings=(), even_odd=False)
    pieces = _pieces(commands, place) if commands else None
    if pieces is None:
        return whole
    lines, areas, rings = pieces
    if not path.get("fill"):
        return (
            ColouredPath(lines=tuple(lines), areas=tuple(areas), rings=(), even_odd=False)
            if lines or areas
            else whole
        )
    # Filled, every sub-path is closed and its inside shows; a stroke round it lies on its outline.
    for ring in rings:
        if ring[-1] != ring[0]:
            lines.append((*ring[-1], *ring[0]))
    if not rings:
        return whole
    return ColouredPath(
        lines=tuple(lines),
        areas=tuple(areas),
        rings=tuple(tuple(ring) for ring in rings),
        even_odd=bool(path.get("evenodd")),
    )


def coloured_paths(data: bytes, page_index: int, *, dpi: int) -> tuple[ColouredPath, ...]:
    """Every line, rectangle and curve the page's pasted drawings draw in colour, of any size and
    stroked or filled, in the page's pixels at `dpi` (#929).

    **Read as the coloured text is read**: from the same copy of the page that holds only its
    pasted drawings (`stamps_only`), placed on the page's pixels as every text run is
    (`reader.pixel_placement`). A drawing nested inside the snapshot's own drawings is read too.

    **Coloured by the one rule** (`path_ink`): a path that shows its line, its fill or both, and
    shows any of them in something other than black or grey. A path drawn neither way shows nothing
    and is not a mark. GV's long red lines and outlines, its yellow fills and the coloured stamps it
    pastes are all found; so is anything the vendor drew in colour, which this cannot tell apart.
    """
    flattened = stamps_only(data, page_index)
    try:
        with pdfplumber.open(io.BytesIO(flattened)) as document:
            page = document.pages[page_index]
            place = pixel_placement(page, dpi)
            return tuple(
                _coloured_path(path, place)
                for path in (*page.lines, *page.rects, *page.curves)
                if (path.get("stroke") or path.get("fill")) and not path_ink(path)
            )
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(
            f"page {page_index}'s pasted drawings could not be read for their paths: {error}"
        ) from error


def _stamp_rect(annotation: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    """A `/Stamp` annotation's rectangle in PDF space, `(left, bottom, right, top)`; `None` for any
    other annotation. A stamp whose rectangle cannot be read raises `UnreadablePdf`."""
    data = annotation.get("data") or {}
    if getattr(data.get("Subtype"), "name", None) != "Stamp":
        return None
    values = data.get("Rect")
    try:
        x0, y0, x1, y1 = (Decimal(str(value)) for value in values or ())
    except (TypeError, ValueError, ArithmeticError) as error:
        raise UnreadablePdf(f"a pasted drawing's rectangle {values!r} cannot be read") from error
    if not all(value.is_finite() for value in (x0, y0, x1, y1)):
        raise UnreadablePdf(f"a pasted drawing's rectangle {values!r} cannot be read")
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _within(
    inner: tuple[Decimal, Decimal, Decimal, Decimal],
    outer: tuple[Decimal, Decimal, Decimal, Decimal],
) -> bool:
    return (
        outer[0] <= inner[0]
        and inner[2] <= outer[2]
        and outer[1] <= inner[1]
        and inner[3] <= outer[3]
    )


def pasted_stamps(data: bytes, page_index: int, *, dpi: int) -> tuple[PixelBox, ...]:
    """Where a stamp is pasted onto one of the page's pasted drawings: each such stamp's rectangle,
    in the page's pixels at `dpi` (#929).

    **A pasted drawing is a `/Stamp`, and so is anything pasted onto one.** On the client's sets each
    drawing — the ID set's and the vendor's — is a snapshot stamp, and GV's reviewer pasted small
    stamps of their own onto them: outlet symbols, a yellow fill in a red outline, about 13 by 7
    points. The file marks them no differently (same subject, same author), and the vendor-only
    render keeps every stamp (#742), so a reader is shown them.

    **Told apart by where they lie: a stamp wholly inside another stamp's rectangle**, edges
    included, is pasted onto that drawing and is not a drawing of its own. Measured on both client
    sets, no drawing lies inside another (AI_Set_1's drawings overlap at their margins, which does
    not count), and the five stamps that do, all on AI_Set_2, are outlet symbols pasted onto its
    drawings. Two stamps with the same rectangle each lie inside the other, so both count: nothing
    tells which is the drawing. A stamp pasted across a drawing's edge is not inside it and is not
    found here; drawn in colour, `coloured_paths` still finds it.

    Whatever it holds, black or coloured, it counts.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            try:
                page = document.pages[page_index]
            except IndexError as error:
                raise UnreadablePdf(
                    f"page {page_index} is beyond the {len(document.pages)} pages in this document"
                ) from error
            transform, _ = page_frame(page, dpi)
            rects = [rect for annotation in page.annots if (rect := _stamp_rect(annotation))]
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(
            f"page {page_index}'s pasted drawings could not be read for their places: {error}"
        ) from error
    return tuple(
        _box_of(
            [
                transform.to_image(PdfPoint(x=x, y=y))
                for x, y in ((rect[0], rect[1]), (rect[2], rect[3]))
            ]
        )
        for index, rect in enumerate(rects)
        if any(other != index and _within(rect, rects[other]) for other in range(len(rects)))
    )
