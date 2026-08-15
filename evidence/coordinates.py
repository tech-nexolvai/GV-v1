"""Typed coordinate spaces and reversible page transforms.

PDF coordinates use points measured from the bottom-left of the page. Rendered image
coordinates use integer pixels measured from the top-left. Stored coordinates use the
same top-left orientation as the rendered image, normalised to the visible crop box.

Source: ``docs/DESIGN_EXTRACTION.md`` section 5 and issue #169.
Verification: ``tests/evidence/test_coordinates.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

POINTS_PER_INCH = Decimal(72)
SUPPORTED_ROTATIONS = frozenset({0, 90, 180, 270})

type PageBox = tuple[Decimal, Decimal, Decimal, Decimal]


class PdfPoint(NamedTuple):
    """A point in PDF user space: points from the bottom-left origin."""

    x: Decimal
    y: Decimal


class ImagePoint(NamedTuple):
    """A point in rendered image space: pixels from the top-left origin."""

    x: int
    y: int


class StoredPoint(NamedTuple):
    """A top-left, rotation-applied point normalised to the visible crop box."""

    x: Decimal
    y: Decimal


def _round_pixel(value: Decimal) -> int:
    """Round a single point to the nearest pixel, with ties away from zero."""

    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _validate_box(name: str, box: PageBox) -> None:
    if not isinstance(box, tuple) or len(box) != 4:
        raise TypeError(f"{name} must contain four Decimal coordinates")
    if any(not isinstance(value, Decimal) for value in box):
        raise TypeError(f"{name} must contain four Decimal coordinates")
    left, bottom, right, top = box
    if right <= left or top <= bottom:
        raise ValueError(f"{name} must have positive width and height")


@dataclass(frozen=True, slots=True)
class PageTransform:
    """Convert points among PDF, rendered-image and stored coordinate spaces.

    ``crop_box`` defines the visible page and therefore the normalisation frame.
    ``media_box`` is retained because the crop box is expressed in the full sheet's
    PDF user space. PDF ``/Rotate`` values are clockwise.

    Converting through integer image space is necessarily lossy. A round trip is
    bounded by one rendered pixel on each axis; no binary floating point is used.
    """

    dpi: int
    rotation: int
    media_box: PageBox
    crop_box: PageBox

    def __post_init__(self) -> None:
        """Reject transform metadata that cannot describe a real rendered page."""

        if isinstance(self.dpi, bool) or not isinstance(self.dpi, int):
            raise TypeError("dpi must be an integer")
        if self.dpi <= 0:
            raise ValueError("dpi must be greater than zero")
        if isinstance(self.rotation, bool) or not isinstance(self.rotation, int):
            raise TypeError("rotation must be an integer")
        if self.rotation not in SUPPORTED_ROTATIONS:
            raise ValueError("rotation must be one of 0, 90, 180 or 270")

        _validate_box("media_box", self.media_box)
        _validate_box("crop_box", self.crop_box)
        media_left, media_bottom, media_right, media_top = self.media_box
        crop_left, crop_bottom, crop_right, crop_top = self.crop_box
        if (
            crop_left < media_left
            or crop_bottom < media_bottom
            or crop_right > media_right
            or crop_top > media_top
        ):
            raise ValueError("crop_box must lie within media_box")
        if any(size <= 0 for size in self._image_size):
            raise ValueError("crop_box is too small to render at the configured dpi")

    @property
    def _scale(self) -> Decimal:
        return Decimal(self.dpi) / POINTS_PER_INCH

    @property
    def _crop_size(self) -> tuple[Decimal, Decimal]:
        left, bottom, right, top = self.crop_box
        return right - left, top - bottom

    @property
    def _rotated_size(self) -> tuple[Decimal, Decimal]:
        width, height = self._crop_size
        if self.rotation in (90, 270):
            return height, width
        return width, height

    @property
    def _image_size(self) -> tuple[int, int]:
        width, height = self._rotated_size
        return _round_pixel(width * self._scale), _round_pixel(height * self._scale)

    def to_image(self, point: PdfPoint) -> ImagePoint:
        """Map a bottom-left PDF point to a top-left integer image point."""

        if not isinstance(point, PdfPoint):
            raise TypeError("point must be a PdfPoint")
        if not isinstance(point.x, Decimal) or not isinstance(point.y, Decimal):
            raise TypeError("PdfPoint coordinates must be Decimal values")
        left, bottom, _, _ = self.crop_box
        width, height = self._crop_size
        local_x = point.x - left
        local_y = point.y - bottom

        if self.rotation == 0:
            rendered_x, rendered_y = local_x, height - local_y
        elif self.rotation == 90:
            rendered_x, rendered_y = local_y, local_x
        elif self.rotation == 180:
            rendered_x, rendered_y = width - local_x, local_y
        else:
            rendered_x, rendered_y = height - local_y, width - local_x

        return ImagePoint(
            _round_pixel(rendered_x * self._scale),
            _round_pixel(rendered_y * self._scale),
        )

    def to_pdf(self, point: ImagePoint) -> PdfPoint:
        """Map a top-left integer image point back into PDF user space."""

        if not isinstance(point, ImagePoint):
            raise TypeError("point must be an ImagePoint")
        if (
            isinstance(point.x, bool)
            or not isinstance(point.x, int)
            or isinstance(point.y, bool)
            or not isinstance(point.y, int)
        ):
            raise TypeError("ImagePoint coordinates must be integer values")
        rendered_x = Decimal(point.x) / self._scale
        rendered_y = Decimal(point.y) / self._scale
        width, height = self._crop_size

        if self.rotation == 0:
            local_x, local_y = rendered_x, height - rendered_y
        elif self.rotation == 90:
            local_x, local_y = rendered_y, rendered_x
        elif self.rotation == 180:
            local_x, local_y = width - rendered_x, rendered_y
        else:
            local_x, local_y = width - rendered_y, height - rendered_x

        left, bottom, _, _ = self.crop_box
        return PdfPoint(left + local_x, bottom + local_y)

    def to_stored(self, point: ImagePoint) -> StoredPoint:
        """Normalise an image point against the rotation-applied visible page."""

        if not isinstance(point, ImagePoint):
            raise TypeError("point must be an ImagePoint")
        if (
            isinstance(point.x, bool)
            or not isinstance(point.x, int)
            or isinstance(point.y, bool)
            or not isinstance(point.y, int)
        ):
            raise TypeError("ImagePoint coordinates must be integer values")
        width, height = self._image_size
        return StoredPoint(Decimal(point.x) / Decimal(width), Decimal(point.y) / Decimal(height))

    def from_stored(self, point: StoredPoint) -> ImagePoint:
        """Restore a normalised stored point to the rendered integer image grid."""

        if not isinstance(point, StoredPoint):
            raise TypeError("point must be a StoredPoint")
        if not isinstance(point.x, Decimal) or not isinstance(point.y, Decimal):
            raise TypeError("StoredPoint coordinates must be Decimal values")
        width, height = self._image_size
        return ImagePoint(
            _round_pixel(point.x * Decimal(width)),
            _round_pixel(point.y * Decimal(height)),
        )
