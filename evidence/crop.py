"""Generate immutable evidence crops from an already-rendered page.

This module deliberately does not open or render a PDF.  The page reader owns that
operation; this boundary receives rotation-applied RGB pixels and checks that they are
pinned to the same document version and page as the evidence polygon.  Stored polygon
coordinates are already rotation-applied, so rotating them again here would move evidence
away from the pixels it identifies.

Crop bytes are encoded deterministically and stored below a document-version namespace.
The content hash makes reruns idempotent, while the namespace prevents identical pixels
from two document versions from claiming the same provenance.

Source: ``docs/DESIGN_EXTRACTION.md`` section 5 and issue #171.
Verification: ``tests/evidence/test_crop.py``.
"""

from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from io import BytesIO
from typing import Final
from uuid import UUID

from evidence.coordinates import SUPPORTED_ROTATIONS
from evidence.polygon import Polygon
from storage.hashing import content_key, sha256_stream
from storage.store import ArtifactStore, StoredArtifact

POINTS_PER_INCH: Final = Decimal(72)
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


class CropStatus(StrEnum):
    """Whether the evidence image is available or needs reviewer attention."""

    AVAILABLE = "AVAILABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """Rotation-applied RGB pixels pinned to one immutable document page.

    ``rgb_bytes`` is row-major, eight-bit RGB with no padding.  Keeping this boundary
    renderer-neutral avoids a hidden PDF dependency and makes the exact crop input part
    of the caller contract.
    """

    document_version_id: UUID
    page_index: int
    page_content_hash: str
    rotation: int
    render_failed: bool
    width_px: int
    height_px: int
    dpi: int
    rgb_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        if isinstance(self.page_index, bool) or not isinstance(self.page_index, int):
            raise TypeError("page_index must be an integer")
        if self.page_index < 0:
            raise ValueError("page_index must be zero or greater")
        if (
            not isinstance(self.page_content_hash, str)
            or len(self.page_content_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.page_content_hash)
        ):
            raise ValueError("page_content_hash must be a lowercase SHA-256 digest")
        if isinstance(self.rotation, bool) or not isinstance(self.rotation, int):
            raise TypeError("rotation must be an integer")
        if self.rotation not in SUPPORTED_ROTATIONS:
            raise ValueError("rotation must be one of 0, 90, 180 or 270")
        if not isinstance(self.render_failed, bool):
            raise TypeError("render_failed must be True or False")
        for name, value in (
            ("width_px", self.width_px),
            ("height_px", self.height_px),
            ("dpi", self.dpi),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if not isinstance(self.rgb_bytes, bytes):
            raise TypeError("rgb_bytes must be bytes")
        expected = self.width_px * self.height_px * 3
        if len(self.rgb_bytes) != expected:
            raise ValueError(
                f"rgb_bytes has {len(self.rgb_bytes)} bytes; "
                f"{self.width_px}x{self.height_px} RGB requires {expected}"
            )


@dataclass(frozen=True, slots=True)
class CropSpec:
    """The evidence polygon and explicit amount of surrounding page context."""

    polygon: Polygon
    context_margin_pt: Decimal
    dpi: int

    def __post_init__(self) -> None:
        if not isinstance(self.polygon, Polygon):
            raise TypeError("polygon must be a Polygon")
        if isinstance(self.context_margin_pt, float):
            raise TypeError("context_margin_pt must be a Decimal, never a float")
        if not isinstance(self.context_margin_pt, Decimal):
            raise TypeError("context_margin_pt must be a Decimal")
        if not self.context_margin_pt.is_finite():
            raise ValueError("context_margin_pt must be finite")
        if self.context_margin_pt <= 0:
            raise ValueError("context_margin_pt must be greater than zero")
        if isinstance(self.dpi, bool) or not isinstance(self.dpi, int):
            raise TypeError("dpi must be an integer")
        if self.dpi <= 0:
            raise ValueError("dpi must be greater than zero")


@dataclass(frozen=True, slots=True)
class CropResult:
    """A stored crop, or an explicit abstention safe for downstream mapping.

    ``REVIEW_REQUIRED`` is intentionally not represented as a successful artifact with
    missing metadata.  Callers cannot mistake an operational crop failure for complete
    evidence.
    """

    status: CropStatus
    artifact: StoredArtifact | None
    uri: str | None
    reason: str | None

    def __post_init__(self) -> None:
        available = self.status is CropStatus.AVAILABLE
        if available and (self.artifact is None or self.uri is None or self.reason is not None):
            raise ValueError("an available crop needs an artifact and URI, and no failure reason")
        if not available and (self.artifact is not None or self.uri is not None or not self.reason):
            raise ValueError("a crop requiring review needs only a non-empty reason")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body))


def _encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode deterministic eight-bit RGB PNG bytes using only the standard library."""

    stride = width * 3
    scanlines = b"".join(b"\x00" + rgb[row * stride : (row + 1) * stride] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(scanlines))
        + _chunk(b"IEND", b"")
    )


def _crop_box(rendered: RenderedPage, spec: CropSpec) -> tuple[int, int, int, int]:
    xs = tuple(point.x * Decimal(rendered.width_px) for point in spec.polygon.points)
    ys = tuple(point.y * Decimal(rendered.height_px) for point in spec.polygon.points)
    margin = spec.context_margin_pt * Decimal(spec.dpi) / POINTS_PER_INCH
    left = max(0, int((min(xs) - margin).to_integral_value(rounding=ROUND_FLOOR)))
    top = max(0, int((min(ys) - margin).to_integral_value(rounding=ROUND_FLOOR)))
    right = min(
        rendered.width_px,
        int((max(xs) + margin).to_integral_value(rounding=ROUND_CEILING)),
    )
    bottom = min(
        rendered.height_px,
        int((max(ys) + margin).to_integral_value(rounding=ROUND_CEILING)),
    )
    if right <= left or bottom <= top:
        raise ValueError("the evidence polygon does not produce a non-empty pixel crop")
    return left, top, right, bottom


def _crop_rgb(rendered: RenderedPage, box: tuple[int, int, int, int]) -> bytes:
    left, top, right, bottom = box
    page_stride = rendered.width_px * 3
    start = left * 3
    stop = right * 3
    return b"".join(
        rendered.rgb_bytes[row * page_stride + start : row * page_stride + stop]
        for row in range(top, bottom)
    )


def generate_crop(
    rendered: RenderedPage,
    spec: CropSpec,
    store: ArtifactStore,
) -> CropResult:
    """Crop, encode and immutably store evidence with surrounding context.

    Operational failures abstain with ``REVIEW_REQUIRED``.  No partial artifact is
    returned, so a finding whose localisation failed cannot look complete.  Input types
    still validate loudly when constructed; this function handles failures encountered
    while associating, generating or storing a particular crop.
    """

    if not isinstance(rendered, RenderedPage):
        raise TypeError("rendered must be a RenderedPage")
    if not isinstance(spec, CropSpec):
        raise TypeError("spec must be a CropSpec")
    if not isinstance(store, ArtifactStore):
        raise TypeError("store must implement ArtifactStore")

    try:
        if rendered.render_failed:
            raise ValueError("the source page did not render")
        if rendered.document_version_id != spec.polygon.document_version_id:
            raise ValueError("crop and polygon belong to different document versions")
        if rendered.page_index != spec.polygon.page:
            raise ValueError("crop and polygon belong to different pages")
        if rendered.dpi != spec.dpi:
            raise ValueError("crop specification DPI does not match the rendered pixels")

        left, top, right, bottom = _crop_box(rendered, spec)
        rgb = _crop_rgb(rendered, (left, top, right, bottom))
        png = _encode_png(right - left, bottom - top, rgb)
        stream = BytesIO(png)
        digest, _ = sha256_stream(stream)
        key = content_key(
            f"evidence-crops/{rendered.document_version_id}/pages/{rendered.page_index}",
            digest,
            suffix=".png",
        )
        stream.seek(0)
        artifact = store.put(key, stream, content_type="image/png")
        return CropResult(CropStatus.AVAILABLE, artifact, store.uri(key), None)
    # ArtifactStore is backend-neutral and therefore cannot name every backend exception.  This is
    # an operational safety boundary: any ordinary generation/write failure must abstain rather
    # than escape and leave a finding looking complete.  BaseException is deliberately untouched.
    except Exception as error:  # noqa: BLE001
        reason = str(error).strip() or type(error).__name__
        return CropResult(CropStatus.REVIEW_REQUIRED, None, None, reason)
