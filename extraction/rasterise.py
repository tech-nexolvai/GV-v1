"""Rendering a PDF page to pixels, so a region of it can be shown to a model or a reviewer.

The other half of B2.1. `extraction/reader.py` reads what a page *says*; this produces what it *looks
like*, which is what `evidence/crop.py` needs before it can cut an evidence crop — and a crop is what
`extraction/models/nova.py` needs before it can ask a model to read an ambiguous region.

`RenderedPage` was written renderer-neutral on purpose: *"Keeping this boundary renderer-neutral
avoids a hidden PDF dependency and makes the exact crop input part of the caller contract."* This
module is the renderer that boundary was waiting for, and it stays on the extraction side of it.

**A pixel budget is required, not optional, and it is the main thing this module is about.** Shop
drawings are D and E size. An ANSI E sheet at 300 dpi is 10200×13200 pixels — **404 MB** of RGB in one
allocation — and backend §4.1 puts the whole system on one 8 GB VM where PostgreSQL and the workers
are the other tenants. A renderer with no bound is a renderer that works on every test sheet and takes
the machine down on the first real one. So `maximum_pixels` has no default: the caller says what it
can afford, and a page that would exceed it is refused with both numbers named rather than rendered.

Three details pypdfium2 gets right in a way that would be wrong if copied naively:

**It renders BGR by default.** `rev_byteorder=True` asks for RGB. Without it every crop shown to a
reviewer has its red and blue channels swapped — which looks like a colour-management bug rather than
a byte-order one, and would be diagnosed in the wrong place.

**A bitmap's stride may exceed its row width.** `RenderedPage` requires exactly `width × height × 3`
bytes with no padding, so rows are copied out individually when the buffer is padded rather than
handed over whole.

**Scale is dpi over 72.** PDF user space is 72 units to the inch, so that ratio is the definition
rather than a convention, and the resulting pixel dimensions are what `PageTransform` must be built
with for a crop's coordinates to land where the reviewer is looking.
"""

from __future__ import annotations

import io
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final
from uuid import UUID

import pdfplumber
import pypdfium2 as pdfium  # type: ignore[import-untyped]

from evidence.coordinates import SUPPORTED_ROTATIONS
from evidence.crop import RenderedPage
from extraction.reader import UnreadablePdf, page_boxes_in_pdf_space

__all__ = ["VISION_CROP_DPI", "PageTooLarge", "render_page"]

#: PDF user space is 72 units to the inch. Not a tunable.
_POINTS_PER_INCH: Final = 72

#: The resolution to render at when a crop is going to a vision model. **Measured, not chosen.**
#:
#: On real crops from a client sheet, `minicpm-v` read a rotated `8' - 6"` as `10.8` from a 150 dpi
#: render upscaled four times, and as `8'-6''` from the same region rendered natively at 600 — and
#: added the inch marks it had been dropping on `33` and `120`. Interpolating a 40-pixel-tall label
#: invents no detail; rendering the page again does, because these sheets are **vector** (`/Stamp`
#: annotations of path geometry, no image XObject anywhere in the file), so there is no scan
#: resolution to be limited by. The only cost is time and memory.
#:
#: Not a default anywhere. `render_page` still requires `dpi` and `maximum_pixels` from its caller,
#: because a whole D-size sheet at 600 dpi is far past any budget this system has — this is the
#: resolution for a **crop-sized region**, which is what the vector-first path renders. A caller
#: rendering a full page still has to say what it can afford.
VISION_CROP_DPI: Final = 600


class PageTooLarge(ValueError):
    """Raised when rendering a page would exceed the caller's pixel budget.

    A refusal and not a silent downscale. Rendering at a resolution the caller did not ask for would
    produce pixel coordinates that do not match the `PageTransform` built alongside them, and every
    crop taken from it would be offset — evidence pointing at the wrong part of the drawing, which is
    worse than no evidence. The caller lowers the dpi or raises the budget, deliberately.
    """


def render_page(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    page_content_hash: str,
    dpi: int,
    maximum_pixels: int,
) -> RenderedPage:
    """One page as rotation-applied RGB pixels.

    `dpi` and `maximum_pixels` are both required. The first decides the coordinate frame every crop
    from this page will be expressed in; the second is what stops an E-size sheet at 300 dpi
    allocating 404 MB on a machine that also runs the database. Neither is the sort of thing to
    inherit from a default nobody chose.

    `page_content_hash` is supplied rather than computed, and that is deliberate. It is the digest
    `extraction/manifest.py` already recorded for this page, and it is what ties a crop back to the
    exact bytes it was cut from. Recomputing it here would be a second implementation of the same
    hash, reached through a different PDF library — and two implementations of one identifier are two
    answers waiting to disagree, after which a crop would appear to come from a page that no manifest
    mentions. The caller has the digest; it passes it in.

    Rotation is applied by the renderer, so `width_px` and `height_px` describe the image as a
    reviewer sees it. A landscape sheet stored with `/Rotate 90` renders landscape.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be the PDF's bytes")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    if isinstance(maximum_pixels, bool) or not isinstance(maximum_pixels, int):
        raise TypeError("maximum_pixels must be an integer")
    if not _is_digest(page_content_hash):
        raise ValueError(
            "page_content_hash must be the lowercase SHA-256 the manifest recorded for this page"
        )
    if maximum_pixels <= 0:
        raise ValueError(
            "maximum_pixels must be positive. It is the caller's memory budget, and a budget of "
            "zero would refuse every page rather than expressing no limit."
        )

    document = None
    try:
        document = pdfium.PdfDocument(data)
        # Counted rather than caught. pdfium raises its own "Failed to load page" for an index past
        # the end, which reads as a corrupt page rather than a request for one that does not exist —
        # and a caller replaying an old message needs to be told which of the two it is.
        page_count = len(document)
        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
            raise ValueError("page_index must be a non-negative integer")
        if page_index >= page_count:
            raise UnreadablePdf(
                f"page {page_index} is beyond the {page_count} pages in this document"
            )
        page = document[page_index]

        rotation = _rotation_of(page)
        width_px, height_px = _pixel_size(page, dpi=dpi, rotation=rotation)

        # Checked before rendering, not after. Refusing an allocation that already happened is not a
        # budget, and the number that matters is the one nobody has spent yet.
        if width_px * height_px > maximum_pixels:
            raise PageTooLarge(
                f"page {page_index} at {dpi} dpi is {width_px}x{height_px} = "
                f"{width_px * height_px} pixels, over the budget of {maximum_pixels}. "
                f"That is {width_px * height_px * 3 / 1_000_000:.0f} MB of RGB in one allocation. "
                "Lower the dpi or raise the budget deliberately — rendering smaller than asked "
                "would put every crop's coordinates out of step with the transform beside them."
            )

        # `rev_byteorder=True` is what makes this RGB. The default is BGR, and a reviewer shown a
        # channel-swapped crop would see a colour bug rather than a byte-order one.
        bitmap = page.render(scale=dpi / _POINTS_PER_INCH, rev_byteorder=True)
        rgb = _reframe_visible_page(
            bitmap,
            page,
            declared_crop_box=_declared_crop_box(data, page_index),
            dpi=dpi,
        )
    except (UnreadablePdf, PageTooLarge):
        raise
    except Exception as error:
        raise UnreadablePdf(f"page {page_index} could not be rendered: {error}") from error
    finally:
        if document is not None:
            document.close()

    return RenderedPage(
        document_version_id=document_version_id,
        page_index=page_index,
        page_content_hash=page_content_hash,
        rotation=rotation,
        render_failed=False,
        width_px=bitmap.width,
        height_px=bitmap.height,
        dpi=dpi,
        rgb_bytes=rgb,
    )


def _declared_crop_box(data: bytes, page_index: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Read the page's declared PDF-space CropBox before PDFium translates it.

    ``PageTransform`` and localized OCR use the PDF-declared visible frame.  PDFium can expose the
    same visible page through a translated rendering-space box, especially when a PDF has been
    tightly cropped without translating its annotations.  Treating those origins as interchangeable
    stores a real crop of the wrong drawing region.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            # Not `.cropbox`, which is pdfplumber's inverted box rather than the PDF-declared
            # one this docstring promises. `reader.page_boxes_in_pdf_space` has the measurement.
            values = page_boxes_in_pdf_space(document.pages[page_index])[1]
    except (IndexError, TypeError, ValueError) as error:
        raise UnreadablePdf(
            f"page {page_index} has no readable declared crop box: {error}"
        ) from error
    if len(values) != 4 or values[2] <= values[0] or values[3] <= values[1]:
        raise UnreadablePdf("page crop box has no area for rendering")
    return values


def _reframe_visible_page(
    bitmap: Any,
    page: Any,
    *,
    declared_crop_box: tuple[Decimal, Decimal, Decimal, Decimal],
    dpi: int,
) -> bytes:
    """Return PDFium pixels in the declared visible-page frame used by stored polygons.

    This normally returns the packed bitmap unchanged.  For a non-zero CropBox, PDFium's full-page
    bitmap can be shifted relative to the declared origin.  Reframe the existing bounded bitmap,
    never rerender or infer a location, so a candidate polygon continues to identify the exact
    pixels OCR saw.  Missing edge pixels are white page background; no drawing pixels are invented.
    """
    raw = _packed_rgb(bitmap)
    crop_left, crop_bottom, _, crop_top = declared_crop_box
    if crop_left == 0 and crop_bottom == 0:
        return raw

    bbox_left, _, _, bbox_top = (Decimal(str(value)) for value in page.get_bbox())
    scale = Decimal(dpi) / _POINTS_PER_INCH
    x_offset = int(((crop_left - bbox_left) * scale).to_integral_value(ROUND_HALF_UP))
    y_offset = int(((bbox_top - crop_top) * scale).to_integral_value(ROUND_HALF_UP))
    if x_offset == 0 and y_offset == 0:
        return raw

    width = int(bitmap.width)
    height = int(bitmap.height)
    destination_left = max(0, -x_offset)
    destination_right = min(width, width - x_offset)
    destination_top = max(0, -y_offset)
    destination_bottom = min(height, height - y_offset)
    reframed = bytearray(b"\xff" * (width * height * 3))
    if destination_right <= destination_left or destination_bottom <= destination_top:
        return bytes(reframed)

    row_bytes = (destination_right - destination_left) * 3
    for destination_y in range(destination_top, destination_bottom):
        source_y = destination_y + y_offset
        source = (source_y * width + destination_left + x_offset) * 3
        destination = (destination_y * width + destination_left) * 3
        reframed[destination : destination + row_bytes] = raw[source : source + row_bytes]
    return bytes(reframed)


def _is_digest(value: object) -> bool:
    """Whether this is a lowercase SHA-256, checked here rather than left to `RenderedPage`.

    The same rule, applied earlier. Rendering a page and then discarding the pixels because the
    identifier was malformed would spend the allocation this module exists to bound.
    """
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _rotation_of(page: object) -> int:
    """The page's `/Rotate`, normalised to a quarter turn.

    Same normalisation the reader applies, and for the same reason: `/Rotate -90` is a real thing
    plotters emit and it means 270, while everything downstream accepts only the four values.
    """
    raw = page.get_rotation()  # type: ignore[attr-defined]
    normalised = int(raw) % 360
    if normalised not in SUPPORTED_ROTATIONS:
        raise UnreadablePdf(
            f"page rotation {raw!r} is not a quarter turn, so its pixels could not be placed"
        )
    return normalised


def _pixel_size(page: object, *, dpi: int, rotation: int) -> tuple[int, int]:
    """The rendered size in pixels, before anything is allocated.

    Computed from the page size rather than measured from a bitmap, because the budget has to be
    checked *before* the allocation it is meant to prevent. pdfium rounds up, so `ceil` matches what
    the renderer will actually produce — guessing low here would let a page a pixel over the budget
    through, which is pedantic on its own and wrong as a bound.
    """
    width_pt, height_pt = page.get_size()  # type: ignore[attr-defined]
    scale = dpi / _POINTS_PER_INCH
    width = -(-int(width_pt * scale * 1000) // 1000)
    height = -(-int(height_pt * scale * 1000) // 1000)
    if rotation in (90, 270):
        # A quarter-turned page renders with its axes swapped, and the budget is about the pixels
        # that get allocated rather than the page's nominal orientation.
        width, height = height, width
    return max(width, 1), max(height, 1)


def _packed_rgb(bitmap: object) -> bytes:
    """The bitmap's pixels as exactly `width × height × 3` bytes.

    A bitmap's stride is its row length in memory and may be larger than the row's pixels — the
    surplus is padding, and `RenderedPage` refuses it, since a consumer indexing by
    `y * width * 3` would read the padding as the start of the next row and shear the image
    progressively down the page.

    The fast path is the common one: when stride already equals the row width, the buffer is handed
    over whole rather than copied row by row.
    """
    width: int = bitmap.width  # type: ignore[attr-defined]
    height: int = bitmap.height  # type: ignore[attr-defined]
    stride: int = bitmap.stride  # type: ignore[attr-defined]
    raw = bytes(bitmap.buffer)  # type: ignore[attr-defined]
    row = width * 3

    if stride == row:
        return raw[: row * height]

    packed = bytearray(row * height)
    for y in range(height):
        start = y * stride
        packed[y * row : (y + 1) * row] = raw[start : start + row]
    return bytes(packed)
