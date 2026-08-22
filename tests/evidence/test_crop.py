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
from evidence.crop import CropSpec, CropStatus, RenderedPage, generate_crop
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
