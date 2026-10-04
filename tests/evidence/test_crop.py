"""Issue #171: evidence crops stay contextual, pinned and safe on failure."""

from __future__ import annotations

import struct
import zlib
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

import pytest

from evidence.coordinates import StoredPoint
from evidence.crop import (
    BoxCropSpec,
    CropSpec,
    CropStatus,
    RenderedPage,
    crop_pixel_box,
    decode_rgb_png,
    encode_png,
    generate_crop,
)
from evidence.polygon import Polygon
from storage.local import LocalStore
from storage.store import StoredArtifact, UploadTicket

DOCUMENT_A = UUID("11111111-1111-1111-1111-111111111111")
DOCUMENT_B = UUID("22222222-2222-2222-2222-222222222222")


def polygon(document_id: UUID = DOCUMENT_A) -> Polygon:
    return Polygon(
        points=(
            StoredPoint(Decimal("0.4"), Decimal("0.4")),
            StoredPoint(Decimal("0.6"), Decimal("0.4")),
            StoredPoint(Decimal("0.6"), Decimal("0.6")),
            StoredPoint(Decimal("0.4"), Decimal("0.6")),
        ),
        space="stored",
        document_version_id=document_id,
        page=0,
    )


def pixels() -> bytes:
    """A 10x10 blue page whose evidence polygon covers a red 2x2 centre."""

    values = bytearray()
    for y in range(10):
        for x in range(10):
            values.extend((255, 0, 0) if 4 <= x < 6 and 4 <= y < 6 else (0, 0, 255))
    return bytes(values)


def rendered(
    document_id: UUID = DOCUMENT_A,
    *,
    rotation: int = 0,
    failed: bool = False,
) -> RenderedPage:
    return RenderedPage(
        document_version_id=document_id,
        page_index=0,
        page_content_hash="a" * 64,
        rotation=rotation,
        render_failed=failed,
        width_px=10,
        height_px=10,
        dpi=72,
        rgb_bytes=pixels(),
    )


def spec(document_id: UUID = DOCUMENT_A) -> CropSpec:
    # Input: centre 2x2 evidence plus one PDF point at 72 dpi. Expected: a 4x4 crop.
    return CropSpec(polygon(document_id), Decimal(1), 72)


def png_rgb(data: bytes) -> tuple[int, int, bytes]:
    """Read the deliberately small filter-0 RGB PNG emitted by the crop module."""

    width, height = struct.unpack(">II", data[16:24])
    offset = 8
    compressed = bytearray()
    while offset < len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + size]
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += 12 + size
    rows = zlib.decompress(bytes(compressed))
    stride = width * 3
    assert all(rows[row * (stride + 1)] == 0 for row in range(height))
    rgb = b"".join(rows[row * (stride + 1) + 1 : (row + 1) * (stride + 1)] for row in range(height))
    return width, height, rgb


def test_the_localized_ocr_png_decoder_round_trips_crop_pixels() -> None:
    """Localized OCR must receive the exact RGB pixels the crop renderer produced."""
    rgb = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255))

    assert decode_rgb_png(encode_png(2, 2, rgb)) == (2, 2, rgb)


def test_the_localized_ocr_png_decoder_refuses_an_unknown_filter() -> None:
    """A decoder that silently misreads a different PNG encoding would invent OCR pixels."""
    png = bytearray(encode_png(1, 1, bytes((1, 2, 3))))
    # The compressed filter byte lives inside IDAT, so generate a deliberately valid PNG rather
    # than mutating bytes behind a CRC. `png_rgb` documents this repository's filter-zero format.
    altered = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + png[16:29]
    import binascii

    altered += binascii.crc32(altered[12:29]).to_bytes(4, "big")
    body = zlib.compress(b"\x01\x01\x02\x03")
    altered += len(body).to_bytes(4, "big") + b"IDAT" + body
    altered += binascii.crc32(b"IDAT" + body).to_bytes(4, "big")
    altered += b"\x00\x00\x00\x00IEND\xaeB`\x82"

    with pytest.raises(ValueError, match="unsupported PNG filter"):
        decode_rgb_png(altered)


def test_same_evidence_rerun_has_the_same_content_address(tmp_path: Path) -> None:
    """Input: identical pinned pixels/spec twice. Outcome: one stable artifact URI."""

    store = LocalStore(tmp_path)

    first = generate_crop(rendered(), spec(), store)
    second = generate_crop(rendered(), spec(), store)

    assert first.status is CropStatus.AVAILABLE
    assert second.status is CropStatus.AVAILABLE
    assert first.artifact == second.artifact
    assert first.uri == second.uri


def test_crop_contains_context_beyond_the_evidence_polygon(tmp_path: Path) -> None:
    """Input: red 2x2 evidence on blue page. Outcome: 4x4 crop includes both colours."""

    store = LocalStore(tmp_path)
    result = generate_crop(rendered(), spec(), store)

    assert result.artifact is not None
    width, height, rgb = png_rgb(store.get(result.artifact.key).read())
    assert (width, height) == (4, 4)
    colours = {tuple(rgb[index : index + 3]) for index in range(0, len(rgb), 3)}
    assert colours == {(255, 0, 0), (0, 0, 255)}


def test_identical_pixels_from_a_new_document_version_get_a_new_key(tmp_path: Path) -> None:
    """Input: same pixels, different immutable version. Outcome: provenance-distinct keys."""

    store = LocalStore(tmp_path)
    first = generate_crop(rendered(DOCUMENT_A), spec(DOCUMENT_A), store)
    second = generate_crop(rendered(DOCUMENT_B), spec(DOCUMENT_B), store)

    assert first.artifact is not None
    assert second.artifact is not None
    assert first.artifact.sha256 == second.artifact.sha256
    assert first.artifact.key != second.artifact.key


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotation_applied_stored_coordinates_are_not_rotated_twice(
    tmp_path: Path, rotation: int
) -> None:
    """Input: rendered page in any supported rotation. Outcome: same stored centre is cropped."""

    store = LocalStore(tmp_path / str(rotation))
    result = generate_crop(rendered(rotation=rotation), spec(), store)

    assert result.status is CropStatus.AVAILABLE
    assert result.artifact is not None
    _, _, rgb = png_rgb(store.get(result.artifact.key).read())
    assert (255, 0, 0) in {tuple(rgb[index : index + 3]) for index in range(0, len(rgb), 3)}


class FailingStore:
    """ArtifactStore whose write simulates an unavailable backend."""

    def put(self, key: str, data: BinaryIO, *, content_type: str) -> StoredArtifact:
        raise OSError("artifact backend unavailable")

    def get(self, key: str) -> BinaryIO:
        return BytesIO()

    def exists(self, key: str) -> bool:
        return False

    def uri(self, key: str) -> str:
        return f"test://{key}"

    def upload_ticket(self, key: str, *, content_type: str, expires_in: timedelta) -> UploadTicket:
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        """Nothing was ever written, so there is nothing to remove."""
        return False


def test_storage_failure_is_an_explicit_review_not_partial_evidence() -> None:
    """Input: failed artifact write. Outcome: REVIEW_REQUIRED and no artifact or URI."""

    result = generate_crop(rendered(), spec(), FailingStore())

    assert result.status is CropStatus.REVIEW_REQUIRED
    assert result.artifact is None
    assert result.uri is None
    assert result.reason == "artifact backend unavailable"


def test_unrendered_page_is_an_explicit_review() -> None:
    """Input: manifest records render failure. Outcome: REVIEW_REQUIRED, never a gap."""

    result = generate_crop(rendered(failed=True), spec(), FailingStore())

    assert result.status is CropStatus.REVIEW_REQUIRED
    assert result.reason == "the source page did not render"


@pytest.mark.parametrize("margin", [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_context_margin_refuses_empty_negative_and_non_finite_values(margin: Decimal) -> None:
    """Input: unusable context bound. Outcome: construction fails before cropping."""

    with pytest.raises(ValueError):
        CropSpec(polygon(), margin, 72)


def test_rendered_pixels_have_an_exact_declared_shape() -> None:
    """Input: truncated RGB raster. Outcome: refusal instead of reading incomplete pixels."""

    with pytest.raises(ValueError, match="requires 300"):
        RenderedPage(DOCUMENT_A, 0, "a" * 64, 0, False, 10, 10, 72, b"short")


def test_polygon_must_be_pinned_to_the_same_document_version() -> None:
    """Input: crop and polygon from different versions. Outcome: REVIEW_REQUIRED."""

    result = generate_crop(rendered(DOCUMENT_A), spec(DOCUMENT_B), FailingStore())

    assert result.status is CropStatus.REVIEW_REQUIRED
    assert result.reason == "crop and polygon belong to different document versions"


# -- a box a polygon cannot describe (#897) -------------------------------------------------------


def box(
    document_id: UUID = DOCUMENT_A,
    *,
    left: Decimal = Decimal("0.4"),
    top: Decimal = Decimal("0.4"),
    right: Decimal = Decimal("0.6"),
    bottom: Decimal = Decimal("0.6"),
    page: int = 0,
) -> BoxCropSpec:
    return BoxCropSpec(document_id, page, left, top, right, bottom, Decimal(1), 72)


def test_a_box_cuts_exactly_what_the_same_polygon_cuts(tmp_path: Path) -> None:
    """Input: the red centre as a box, and as the polygon round it. Outcome: the same pixel box and
    the same stored bytes, so a box is a second way to name a region, not a second crop."""
    store = LocalStore(tmp_path)

    by_box = generate_crop(rendered(), box(), store)
    by_polygon = generate_crop(rendered(), spec(), store)

    assert crop_pixel_box(rendered(), box()) == crop_pixel_box(rendered(), spec()) == (3, 3, 7, 7)
    assert by_box.artifact is not None
    assert by_box.artifact == by_polygon.artifact


def test_a_box_with_no_height_is_its_line_and_the_margin_round_it(tmp_path: Path) -> None:
    """Input: a line across the page, which no polygon can describe (a part a person added by its
    two ends, #882). Outcome: a crop the margin above and below it, and nothing invented beyond."""
    store = LocalStore(tmp_path)
    line = box(top=Decimal("0.5"), bottom=Decimal("0.5"))

    result = generate_crop(rendered(), line, store)

    assert result.status is CropStatus.AVAILABLE and result.artifact is not None
    assert crop_pixel_box(rendered(), line) == (3, 4, 7, 6)
    width, height, _ = png_rgb(store.get(result.artifact.key).read())
    assert (width, height) == (4, 2)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"left": 0.4}, TypeError),
        ({"bottom": Decimal("1.1")}, ValueError),
        ({"top": Decimal(-1)}, ValueError),
        ({"right": Decimal("NaN")}, ValueError),
        ({"left": Decimal("0.7")}, ValueError),
        ({"top": Decimal("0.7")}, ValueError),
        ({"page": -1}, ValueError),
        ({"page": True}, TypeError),
    ],
)
def test_a_box_off_the_page_or_inside_out_is_refused(
    changes: dict[str, object], error: type[Exception]
) -> None:
    """Input: a float, a side outside 0..1, NaN, a left past its right or a top below its bottom,
    or a page that is not one. Outcome: construction fails before anything is cut."""
    with pytest.raises(error):
        box(**changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("margin", "dpi", "error"),
    [
        (Decimal(0), 72, ValueError),
        (Decimal("Infinity"), 72, ValueError),
        (1.0, 72, TypeError),
        (Decimal(1), 0, ValueError),
        (Decimal(1), True, TypeError),
    ],
)
def test_a_box_states_its_margin_and_resolution_like_a_polygon(
    margin: object, dpi: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        BoxCropSpec(
            DOCUMENT_A, 0, Decimal("0.4"), Decimal("0.4"), Decimal("0.6"), Decimal("0.6"), margin, dpi  # type: ignore[arg-type]
        )


def test_a_box_must_be_pinned_to_the_same_document_and_page() -> None:
    """Input: a box from another version, or another page. Outcome: REVIEW_REQUIRED, saying so."""

    other_version = generate_crop(rendered(DOCUMENT_A), box(DOCUMENT_B), FailingStore())
    other_page = generate_crop(rendered(DOCUMENT_A), box(page=1), FailingStore())

    assert other_version.status is CropStatus.REVIEW_REQUIRED
    assert other_version.reason == "crop and box belong to different document versions"
    assert other_page.reason == "crop and box belong to different pages"
