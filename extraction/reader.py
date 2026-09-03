"""Opening a PDF and reporting what is on its pages (B2.1, #123).

The seam `extraction/manifest.py` has been waiting for. Its docstring names this module's absence
outright — *"The reader is not this module. Opening the PDF, repairing it and rendering its pages is
B2.1 (#123)… The seam between the two is `RawPage`"* — and until now nothing in the repository opened
a PDF at all, so several thousand lines of page classification, geometry and assembly sat downstream
of an input that did not exist.

**Everything here is measured; nothing here is a conclusion.** A page's size comes from its page
dictionary, its characters from its content stream, its lines from its graphics operators. What any
of it *means* — which page is a plan, which text is a dimension, which line it annotates — belongs to
the modules that already exist for those questions. This one reports.

Four things it has to get right, and three of them are things pdfplumber does that would otherwise be
wrong quietly.

**Rotated text reads backwards by default.** A dimension written bottom-to-top comes back as `489`
where the drawing says `984`. That is the worst failure available to a reader: a real number,
correctly parsed, and wrong — no downstream check can catch it, because 489 is a perfectly plausible
dimension. `char_dir_rotated="btt"` is what makes it right, and `tests/extraction/test_reader.py`
holds a regression on exactly that pair of numbers.

**Rotation is read, never inferred.** `DimensionText` requires it reported rather than guessed from
the box, because a box around `984` is wider than it is tall and a box around `8` is not, so guessing
would read single-digit dimensions as rotated. It comes from the character's own transformation
matrix through `pdfplumber.ctm.CTM`.

**A page with no text objects is reported as unreadable, not as empty.** Some CAD plot
configurations convert text to vector outlines, and scanned sheets never had text objects at all. In
both cases pdfplumber returns nothing, and an empty result would travel downstream as "this page has
no dimensions" — a false pass by omission. `RawPage.unreadable_reason` exists for this and says so in
plain English.

**No float reaches a `Decimal`.** pdfplumber returns floats; `Decimal(str(value))` is the only
conversion used, because `Decimal(0.1)` carries binary rounding into a coordinate that a reviewer will
later be shown as evidence.

What this module deliberately does not do: rasterise a page, run OCR, merge fragmented dimensions, or
associate text with lines. The last two need thresholds, and thresholds need real drawings (#274) —
`AGENTS.md` §9, *"a fixture invented today encodes today's guess as ground truth"*.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Any, Final
from uuid import UUID

import pdfplumber
from pdfplumber.ctm import CTM

from evidence.coordinates import (
    SUPPORTED_ROTATIONS,
    PageBox,
    PageTransform,
    PdfPoint,
    StoredPoint,
)
from evidence.polygon import Polygon
from extraction.geometry.containment import DimensionExtent
from extraction.manifest import RawPage

__all__ = [
    "PageContents",
    "TextItem",
    "UnreadablePdf",
    "read_page_contents",
    "read_pages",
]

#: Read bottom-to-top for rotated runs. Without it pdfplumber returns the characters of a rotated
#: dimension in reverse — `984` as `489` — which is a plausible number and therefore undetectable
#: downstream. Measured, not assumed; the regression is in the test module.
_ROTATED_CHAR_DIRECTION: Final = "btt"

#: Plain English, for a reviewer rather than a log. Named causes, because "no text" tells somebody
#: nothing and "plotted as outlines or scanned" tells them what to go and check.
_NO_TEXT_REASON: Final = (
    "no text objects on this page: it was plotted with text converted to outlines, or it is a "
    "scanned image. Its dimensions cannot be read without OCR."
)


class UnreadablePdf(ValueError):
    """Raised when the file cannot be opened as a PDF at all.

    Distinct from a page that cannot be read. A page with no text is a page whose dimensions need
    another route; a document that will not parse has no pages to report, and `RawPage` refuses to
    invent a page size to fill its required fields. The two must not arrive as the same thing.
    """


@dataclass(frozen=True, slots=True)
class TextItem:
    """One run of text as read, with where it sits and which way it reads.

    Carries the text itself, unlike `DimensionText` — this is the reader's observation, and the
    number becomes an observation with an identity later. The geometry is already in stored space so
    that whatever mints that identity does not have to know about page transforms.
    """

    text: str
    extent: Polygon
    rotation_degrees: int
    upright: bool


@dataclass(frozen=True, slots=True)
class PageContents:
    """What one page holds: its text runs and its straight line segments.

    `unreadable_reason` mirrors `RawPage`'s, and for the same reason: empty tuples with no explanation
    would read as a page with nothing on it.
    """

    page_index: int
    texts: tuple[TextItem, ...]
    segments: tuple[DimensionExtent, ...]
    unreadable_reason: str | None = None

    @property
    def readable(self) -> bool:
        """Whether text was found. `False` sends the page to OCR rather than to the vector lane."""
        return self.unreadable_reason is None


def _decimal(value: object) -> Decimal:
    """A pdfplumber number as an exact Decimal.

    Through `str`, always. `Decimal(0.1)` is `0.1000000000000000055511151231257827`, and a coordinate
    carrying that is a coordinate that will not round-trip — which matters because these become the
    polygon a reviewer is shown as the evidence for a verdict.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise TypeError(f"cannot read {value!r} as a coordinate")


def _box(values: object) -> PageBox:
    """A pdfplumber media or crop box as the four Decimals `PageTransform` demands."""
    if not isinstance(values, (tuple, list)) or len(values) != 4:
        raise UnreadablePdf(f"page box {values!r} is not four coordinates")
    left, bottom, right, top = (_decimal(value) for value in values)
    return (left, bottom, right, top)


def _rotation(value: object) -> int:
    """A page's `/Rotate`, normalised to the four values everything downstream accepts.

    PDF permits any multiple of 90, including negatives and values past 360; `RawPage` and
    `PageTransform` both accept only `0, 90, 180, 270`. Normalising here rather than refusing keeps a
    `/Rotate -90` page readable, which is a real thing plotters emit.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnreadablePdf(f"page rotation {value!r} is not an integer")
    normalised = value % 360
    if normalised not in SUPPORTED_ROTATIONS:
        raise UnreadablePdf(
            f"page rotation {value!r} is not a quarter turn. Every later stage assumes one of "
            "0, 90, 180 or 270, and a page rotated by anything else would have its geometry "
            "silently misplaced rather than refused."
        )
    return normalised


def _content_bytes(page: Any) -> bytes:
    """The page's content stream, which `build_manifest` hashes to identify the page.

    Best-effort by design: the hash exists so a re-read of the same page is recognisable, and a page
    whose stream cannot be reached still has a size, a rotation and a character count worth
    reporting. `b""` is explicitly allowed by `RawPage`.
    """
    try:
        contents = page.page_obj.get_data()
    except Exception:  # noqa: BLE001 - any failure to reach the stream is the same answer
        # Deliberately broad. The hash exists so a re-read of the same page is recognisable, and a
        # page whose stream cannot be reached still has a size, a rotation and a character count
        # worth reporting. Enumerating the ways a malformed stream can fail would be a list that a
        # new pdfminer release lengthens.
        return b""
    return contents if isinstance(contents, bytes) else b""


def read_pages(data: bytes) -> tuple[RawPage, ...]:
    """Every page of a PDF, as the observations `build_manifest` turns into a manifest.

    Reports rather than judges: `vector_character_count` is a count of characters found, and whether
    that is *enough* to call the page text-bearing is `build_manifest`'s decision, made against a
    `minimum_vector_characters` its caller has to supply.

    A page with no characters carries `unreadable_reason` and a count of zero — `RawPage` cross-checks
    that pair, and the pairing is the point: the page is not empty, it is unread.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be the PDF's bytes")
    if not data:
        raise UnreadablePdf("the file is empty")

    pages: list[RawPage] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            for index, page in enumerate(document.pages):
                characters = len(page.chars)
                pages.append(
                    RawPage(
                        index=index,
                        content=_content_bytes(page),
                        width_pt=_decimal(page.width),
                        height_pt=_decimal(page.height),
                        rotation=_rotation(page.rotation),
                        vector_character_count=characters,
                        unreadable_reason=None if characters else _NO_TEXT_REASON,
                    )
                )
    except UnreadablePdf:
        raise
    except Exception as error:
        # Any parse failure is one answer: this is not a document we can read. Deliberately not a
        # partial list of the pages reached before it failed — a truncated page list is a document
        # that looks shorter than it is, and every later stage would trust the count.
        raise UnreadablePdf(f"the file could not be read as a PDF: {error}") from error

    if not pages:
        raise UnreadablePdf("the file parsed as a PDF but contains no pages")
    return tuple(pages)


def read_page_contents(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
) -> PageContents:
    """The text runs and straight segments on one page, in stored coordinates.

    `dpi` has no default. Stored coordinates are normalised against the visible crop box and reached
    through integer image space, so the resolution decides how much precision survives the trip — a
    default here would silently pick that for every caller. `evidence/coordinates.py` documents the
    round trip as lossy by up to one pixel per axis.

    Both tuples come back in page order, and the segments are every straight line the page draws:
    line objects, rectangle edges, and the point pairs of each path. A dimension line may be any of
    the three depending on what plotted the sheet, so filtering them by what looks like a dimension
    is the association step's job, not this one's — a reader that dropped candidate geometry would
    make a missing dimension look like a drawing that never showed it.
    """
    if not isinstance(dpi, bool) and isinstance(dpi, int) and dpi > 0:
        pass
    else:
        raise ValueError("dpi must be a positive integer; stored coordinates depend on it")

    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            try:
                page = document.pages[page_index]
            except IndexError as error:
                raise UnreadablePdf(
                    f"page {page_index} is beyond the {len(document.pages)} pages in this document"
                ) from error

            transform = PageTransform(
                dpi=dpi,
                rotation=_rotation(page.rotation),
                media_box=_box(page.mediabox),
                crop_box=_box(page.cropbox),
            )
            height = _decimal(page.height)

            texts = tuple(
                item
                for word in page.extract_words(
                    return_chars=True, char_dir_rotated=_ROTATED_CHAR_DIRECTION
                )
                if (item := _text_item(word, transform, height, document_version_id, page_index))
                is not None
            )
            segments = _segments(page, transform, height, document_version_id, page_index)
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(f"page {page_index} could not be read: {error}") from error

    return PageContents(
        page_index=page_index,
        texts=texts,
        segments=segments,
        unreadable_reason=None if texts else _NO_TEXT_REASON,
    )


def _stored(x: object, top: object, transform: PageTransform, height: Decimal) -> StoredPoint:
    """A pdfplumber `(x, top)` as a stored point.

    Two conversions, both easy to get wrong. pdfplumber measures `top` downward from the top of the
    page while PDF user space measures upward from the bottom, so the y is flipped against the page
    height. Then stored space is reached through integer image space, because that is the only route
    `PageTransform` offers — and going through it rather than normalising directly keeps this
    agreeing with every other consumer of stored coordinates.
    """
    pdf_point = PdfPoint(x=_decimal(x), y=height - _decimal(top))
    return transform.to_stored(transform.to_image(pdf_point))


def _text_item(
    word: dict[str, Any],
    transform: PageTransform,
    height: Decimal,
    document_version_id: UUID,
    page_index: int,
) -> TextItem | None:
    """One extracted word as a `TextItem`, or `None` when its box cannot be a polygon.

    A zero-area box is dropped rather than nudged into validity. `Polygon` refuses one, and rightly:
    a rectangle with no area names no region of the drawing, so widening it by a pixel would invent
    a location for evidence that has none.
    """
    text = str(word.get("text", ""))
    if not text:
        return None

    chars = word.get("chars") or ()
    rotation = _text_rotation(chars[0]) if chars else 0

    corners = (
        _stored(word["x0"], word["top"], transform, height),
        _stored(word["x1"], word["top"], transform, height),
        _stored(word["x1"], word["bottom"], transform, height),
        _stored(word["x0"], word["bottom"], transform, height),
    )
    try:
        extent = Polygon(
            points=corners,
            space="stored",
            document_version_id=document_version_id,
            page=page_index,
        )
    except (ValueError, TypeError):
        return None

    return TextItem(
        text=text,
        extent=extent,
        rotation_degrees=rotation,
        upright=bool(word.get("upright", True)),
    )


def _text_rotation(char: dict[str, Any]) -> int:
    """A character's rotation, from its own transformation matrix.

    Read, never inferred — `DimensionText` insists on that, because a box around `984` is wider than
    it is tall and a box around `8` is not, so inferring from the shape would report single-digit
    dimensions as rotated.

    Snapped to the nearest quarter turn. `CTM.skew_x` is a rotation in degrees and a plotter may emit
    `89.9999`; the four values are what everything downstream accepts, and refusing a sheet over a
    ten-thousandth of a degree would be pedantry rather than safety.
    """
    matrix = char.get("matrix")
    if not isinstance(matrix, (tuple, list)) or len(matrix) != 6:
        return 0
    degrees = float(CTM(*matrix).skew_x)
    return int(round(degrees / 90.0) * 90) % 360


def _segments(
    page: Any,
    transform: PageTransform,
    height: Decimal,
    document_version_id: UUID,
    page_index: int,
) -> tuple[DimensionExtent, ...]:
    """Every straight segment the page draws, from all three of pdfplumber's geometry lists.

    Line objects are the obvious source and not the only one: a plotter may emit a dimension line as
    a rectangle edge or as a path, and which one it chooses is a property of the software rather than
    of the drawing. Taking all three is what stops a dimension being invisible because of how the
    sheet was exported.

    Degenerate segments are dropped — `DimensionExtent` refuses identical endpoints, since a line
    spanning nothing annotates nothing.
    """
    found: list[DimensionExtent] = []

    def add(x0: object, top0: object, x1: object, top1: object) -> None:
        try:
            extent = DimensionExtent(
                start=_stored(x0, top0, transform, height),
                end=_stored(x1, top1, transform, height),
                document_version_id=document_version_id,
                page=page_index,
            )
        except (ValueError, TypeError):
            return
        found.append(extent)

    for line in page.lines:
        add(line["x0"], line["top"], line["x1"], line["bottom"])

    for rect in page.rects:
        x0, x1, top, bottom = rect["x0"], rect["x1"], rect["top"], rect["bottom"]
        add(x0, top, x1, top)
        add(x1, top, x1, bottom)
        add(x1, bottom, x0, bottom)
        add(x0, bottom, x0, top)

    for curve in page.curves:
        points = curve.get("pts") or ()
        # Consecutive pairs only. A path's points are in drawing order, so pairing them recovers the
        # straight runs; the curved parts come back as short chords, which is a faithful reading of
        # what was drawn rather than an interpolation of what was meant.
        for start, end in pairwise(points):
            add(start[0], height - _decimal(start[1]), end[0], height - _decimal(end[1]))

    return tuple(found)
