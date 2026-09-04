"""Rendering a PDF page to pixels.

Verification for: `extraction/rasterise.py` (the other half of B2.1).

Two of these tests are about things that would be wrong invisibly: a channel-swapped image looks like
a colour bug rather than a byte-order one, and an unbounded render works on every test sheet and takes
the machine down on the first E-size drawing.

The last test is the one that matters most for what this unlocks — it takes a polygon from the reader
and pixels from the rasteriser and produces the crop a model would actually be shown, which is the
first time that path has run.
"""

from __future__ import annotations

import hashlib
import tempfile
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from evidence.crop import CropSpec, RenderedPage, generate_crop
from extraction.rasterise import PageTooLarge, render_page
from extraction.reader import UnreadablePdf, read_page_contents, read_pages
from storage.local import LocalStore
from tests.extraction.test_reader import _pdf

DOCUMENT = uuid4()
DPI = 150
BUDGET = 10_000_000

#: `1 0 0 rg` is pure red in PDF's RGB colour space, filling the whole page.
RED_PAGE = _pdf(b"1 0 0 rg 0 0 200 100 re f\n")

DRAWING = _pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (38 3/4) Tj ET\n1 w 20 40 m 120 40 l S\n")


def _digest(data: bytes) -> str:
    """The page content digest the manifest would have recorded.

    Taken from the reader, because that is where the real one comes from — a test that invented its
    own digest would not notice the two drifting apart.
    """
    return hashlib.sha256(read_pages(data)[0].content).hexdigest()


def _render(data: bytes = DRAWING, **overrides: object) -> RenderedPage:
    arguments: dict[str, object] = {
        "document_version_id": DOCUMENT,
        "page_content_hash": _digest(data),
        "dpi": DPI,
        "maximum_pixels": BUDGET,
    }
    arguments.update(overrides)
    return render_page(data, 0, **arguments)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The two silent failures
# ---------------------------------------------------------------------------


def test_the_pixels_are_rgb_and_not_bgr() -> None:
    """**pypdfium2 renders BGR by default, and a swapped image does not look swapped.**

    It looks like a colour-management problem — which would be investigated in the renderer, the
    encoder, the browser, anywhere but the byte order. `rev_byteorder=True` is what makes it RGB.

    Tested on a pure red page, because white and black are identical either way: only a colour with
    unequal channels can tell the two apart.
    """
    rendered = _render(RED_PAGE, dpi=36)
    red, green, blue = rendered.rgb_bytes[0], rendered.rgb_bytes[1], rendered.rgb_bytes[2]

    assert (red, green, blue) == (255, 0, 0), (
        f"a pure red page rendered as {(red, green, blue)}; if that is (0, 0, 255) the channels "
        "are reversed and every crop a reviewer sees will have its colours swapped"
    )


def test_a_page_over_the_budget_is_refused_before_it_is_allocated() -> None:
    """**An E-size sheet at 300 dpi is 404 MB of RGB in one allocation.**

    Backend §4.1 puts everything on one 8 GB VM with PostgreSQL and the workers as the other tenants,
    so an unbounded renderer works on every letter-size test page and takes the machine down on the
    first real drawing. The refusal names both numbers, because "too large" without them tells a
    caller nothing about what to change.
    """
    with pytest.raises(PageTooLarge, match="over the budget"):
        _render(maximum_pixels=100)


def test_the_budget_is_checked_against_the_real_rendered_size() -> None:
    """The bound has to be the size that would actually be allocated, not the page's nominal size.

    A page exactly at the budget renders; one pixel more does not. Asserted as a pair, because a
    check that refused everything would pass the test above and be useless.
    """
    rendered = _render()
    exact = rendered.width_px * rendered.height_px

    assert _render(maximum_pixels=exact) is not None
    with pytest.raises(PageTooLarge):
        _render(maximum_pixels=exact - 1)


# ---------------------------------------------------------------------------
# The contract RenderedPage demands
# ---------------------------------------------------------------------------


def test_the_bytes_are_exactly_the_pixels_with_no_padding() -> None:
    """`RenderedPage` requires `width × height × 3` and refuses anything else.

    A bitmap's stride may exceed its row width, and the surplus is padding. A consumer indexing by
    `y * width * 3` would read that padding as the start of the next row, shearing the image
    progressively down the page — which looks like a skewed drawing rather than a layout bug.
    """
    rendered = _render()

    assert len(rendered.rgb_bytes) == rendered.width_px * rendered.height_px * 3


def test_the_rendered_size_follows_the_dpi() -> None:
    """72 points to the inch is the definition of PDF user space, so 150 dpi on a 200pt page is
    about 417 pixels. Asserted as a ratio rather than an exact count, because pdfium's rounding is
    its business and pinning it would make this a test of pdfium."""
    at_72 = _render(dpi=72)
    at_144 = _render(dpi=144)

    assert at_72.dpi == 72
    assert at_144.width_px == pytest.approx(at_72.width_px * 2, abs=2)
    assert at_144.height_px == pytest.approx(at_72.height_px * 2, abs=2)


def test_the_content_hash_is_the_one_the_manifest_recorded() -> None:
    """**Supplied, not recomputed, and this is why.**

    The digest ties a crop back to the exact page bytes it was cut from. Computing it here would be a
    second implementation of one identifier, reached through a different PDF library — two answers
    waiting to disagree, after which a crop appears to come from a page no manifest mentions.
    """
    digest = _digest(DRAWING)

    assert _render().page_content_hash == digest


@pytest.mark.parametrize("bad", ["", "not a digest", "A" * 64, "0" * 63])
def test_a_malformed_content_hash_is_refused_before_rendering(bad: str) -> None:
    """Checked early on purpose. Rendering and then discarding the pixels because the identifier was
    malformed would spend the allocation this module exists to bound."""
    with pytest.raises(ValueError, match="page_content_hash"):
        _render(page_content_hash=bad)


def test_a_rendered_page_is_not_marked_failed() -> None:
    """`generate_crop` refuses a page marked failed, so a successful render must not claim to be
    one — the crop would be refused for a reason that had not happened."""
    assert _render().render_failed is False


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dpi", [0, -150, True])
def test_dpi_must_be_a_positive_integer(dpi: object) -> None:
    with pytest.raises(ValueError, match="dpi"):
        _render(dpi=dpi)


def test_a_budget_of_zero_is_refused_rather_than_read_as_unlimited() -> None:
    """Zero is the value somebody passes meaning "no limit". It would refuse every page instead, so
    it is refused with an explanation rather than obeyed."""
    with pytest.raises(ValueError, match="maximum_pixels must be positive"):
        _render(maximum_pixels=0)


def test_a_page_beyond_the_document_is_refused_by_name() -> None:
    with pytest.raises(UnreadablePdf, match="beyond the 1 pages"):
        render_page(
            DRAWING,
            9,
            document_version_id=DOCUMENT,
            page_content_hash=_digest(DRAWING),
            dpi=DPI,
            maximum_pixels=BUDGET,
        )


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.4\nbroken"])
def test_something_that_is_not_a_pdf_is_refused(data: bytes) -> None:
    with pytest.raises((UnreadablePdf, ValueError)):
        render_page(
            data,
            0,
            document_version_id=DOCUMENT,
            page_content_hash="0" * 64,
            dpi=DPI,
            maximum_pixels=BUDGET,
        )


# ---------------------------------------------------------------------------
# What this unlocks
# ---------------------------------------------------------------------------


def test_a_reader_polygon_and_rendered_pixels_produce_a_crop() -> None:
    """**The first time this path has ever run.**

    `evidence/crop.py` has been able to cut an evidence crop since it was written, and nothing could
    give it pixels — `RenderedPage` was constructed only in its own tests. This takes a polygon from
    the reader and pixels from the rasteriser and produces the PNG a model would be shown, which is
    the input `extraction/models/nova.py` has been waiting for.

    Asserted on the stored artifact rather than on the return value alone: the crop is only useful if
    it reached the store, and a `CropResult` that reported success while storing nothing would look
    identical from the outside.
    """
    contents = read_page_contents(DRAWING, 0, document_version_id=DOCUMENT, dpi=DPI)
    rendered = _render()

    assert contents.texts, "the reader found no text to crop around"

    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")
        result = generate_crop(
            rendered,
            CropSpec(
                polygon=contents.texts[0].extent,
                context_margin_pt=Decimal(12),
                dpi=DPI,
            ),
            store,
        )

        assert result.reason is None, f"the crop was refused: {result.reason}"
        assert result.artifact is not None
        with store.get(result.artifact.key) as stored:
            png = stored.read()

    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "the stored crop is not a PNG"
    assert len(png) > 0


def test_the_crop_dpi_must_match_the_render_dpi() -> None:
    """`generate_crop` refuses a mismatch, and it is right to.

    The crop's pixel coordinates come from the polygon scaled by its own dpi; if the pixels were
    rendered at another, the crop lands on the wrong part of the page. A reviewer would be shown a
    region of the drawing that is not the one the finding is about — evidence for the wrong thing,
    which is worse than none.
    """
    contents = read_page_contents(DRAWING, 0, document_version_id=DOCUMENT, dpi=DPI)
    rendered = _render(dpi=DPI)

    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")
        result = generate_crop(
            rendered,
            CropSpec(
                polygon=contents.texts[0].extent,
                context_margin_pt=Decimal(12),
                dpi=DPI * 2,
            ),
            store,
        )

    assert result.reason is not None


# ---------------------------------------------------------------------------
# Stride padding, tested on the helper because pdfium does not produce it here
# ---------------------------------------------------------------------------


class _PaddedBitmap:
    """A bitmap whose rows are longer in memory than the pixels they hold.

    Written by hand because pypdfium2 does not produce one for this format: every real render in this
    module comes back with `stride == width * 3`, so mutating the padding branch away left the whole
    suite green.

    Stride is a documented property of a bitmap rather than an accident, and it differs by format and
    by version, so the branch is worth keeping and worth testing. Testing it on the helper is the
    honest way to do that — the alternative is a test that claims to cover a path the renderer never
    takes.
    """

    def __init__(self, width: int, height: int, padding: int) -> None:
        self.width = width
        self.height = height
        self.stride = width * 3 + padding
        # Each row is its own byte value, so a mis-strided read shows up as the wrong row rather
        # than as noise.
        self.buffer = b"".join(
            bytes([row + 1]) * (width * 3) + b"\xee" * padding for row in range(height)
        )


def test_stride_padding_is_stripped_rather_than_read_as_pixels() -> None:
    """**The failure this prevents looks like a skewed drawing, not a bug.**

    A consumer indexing by `y * width * 3` over a padded buffer reads each row a little further
    along than the last, shearing the image progressively down the page. On a drawing that reads as
    bad geometry — which is exactly the thing a reviewer is looking for, arriving from the wrong
    place.
    """
    from extraction.rasterise import _packed_rgb

    packed = _packed_rgb(_PaddedBitmap(width=4, height=3, padding=2))

    assert len(packed) == 4 * 3 * 3
    # Row n is all bytes of value n+1. Padding is 0xee and must appear nowhere.
    assert packed[0:12] == bytes([1]) * 12
    assert packed[12:24] == bytes([2]) * 12
    assert packed[24:36] == bytes([3]) * 12
    assert b"\xee" not in packed


def test_an_unpadded_bitmap_is_handed_over_whole() -> None:
    """The fast path, which is the one every real render takes. Asserted so that a change to the
    padded branch cannot quietly break the common case."""
    from extraction.rasterise import _packed_rgb

    packed = _packed_rgb(_PaddedBitmap(width=4, height=3, padding=0))

    assert len(packed) == 36
    assert packed[0:12] == bytes([1]) * 12
