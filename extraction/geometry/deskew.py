"""Detect and correct small page skew before OCR without losing page coordinates.

The caller supplies the maximum angle it is willing to correct; this module has no empirical
default. A larger angle is retained as a review condition instead of being silently transformed.
The recorded forward and inverse affine matrices let downstream OCR points return to the original
rendered page.

Source: issue #178 and ``docs/DESIGN_EXTRACTION.md`` section 6.
Verification: ``tests/extraction/geometry/test_deskew.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple, cast

import cv2
import numpy as np
from numpy.typing import NDArray

type PageImage = NDArray[np.uint8]
type OpenCvMatrix = NDArray[np.float64]

type AffineMatrix = tuple[
    tuple[Decimal, Decimal, Decimal],
    tuple[Decimal, Decimal, Decimal],
]

IDENTITY_AFFINE: AffineMatrix = (
    (Decimal(1), Decimal(0), Decimal(0)),
    (Decimal(0), Decimal(1), Decimal(0)),
)


class DeskewPoint(NamedTuple):
    """A point in rendered-image pixels, retained as decimals across affine transforms."""

    x: Decimal
    y: Decimal


class DeskewStatus(StrEnum):
    """The three honest outcomes of examining a rendered page for skew."""

    UNCHANGED = "unchanged"
    CORRECTED = "corrected"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True)
class RotationCorrection:
    """The recorded, reversible transform between original and corrected image pixels."""

    detected_skew_degrees: Decimal
    applied_rotation_degrees: Decimal
    forward: AffineMatrix
    inverse: AffineMatrix

    def to_corrected(self, point: DeskewPoint) -> DeskewPoint:
        """Map an original rendered-page point into the corrected image."""

        return _apply(self.forward, point)

    def to_original(self, point: DeskewPoint) -> DeskewPoint:
        """Map a corrected-image point back into the original rendered page."""

        return _apply(self.inverse, point)


@dataclass(frozen=True, slots=True)
class DeskewResult:
    """A corrected page or an explicit refusal that must not proceed to OCR."""

    image: PageImage
    status: DeskewStatus
    correction: RotationCorrection
    max_correctable_degrees: Decimal
    reason: str

    @property
    def image_for_ocr(self) -> PageImage:
        """Return the prepared OCR input, refusing an over-bound page.

        Rendering owns creation of the input image. OCR must consume this property rather than the
        original rendered page, which places this correction between those two stages. A page whose
        skew exceeds the caller's stated bound cannot quietly continue with plausible OCR output.
        """

        if self.status is DeskewStatus.REVIEW_REQUIRED:
            raise DeskewRefusedError(self.reason)
        return self.image


class DeskewRefusedError(ValueError):
    """Raised when OCR is requested for a page whose skew was not safely corrected."""


def _decimal(value: float) -> Decimal:
    """Record an OpenCV result without adding another binary-float conversion."""

    return Decimal(str(float(value)))


def _matrix(values: OpenCvMatrix) -> AffineMatrix:
    """Copy an OpenCV 2x3 affine matrix into an immutable, serialisable value."""

    return (
        (_decimal(values[0][0]), _decimal(values[0][1]), _decimal(values[0][2])),
        (_decimal(values[1][0]), _decimal(values[1][1]), _decimal(values[1][2])),
    )


def _apply(matrix: AffineMatrix, point: DeskewPoint) -> DeskewPoint:
    if not isinstance(point, DeskewPoint):
        raise TypeError("point must be a DeskewPoint")
    if not isinstance(point.x, Decimal) or not isinstance(point.y, Decimal):
        raise TypeError("DeskewPoint coordinates must be Decimal values")
    return DeskewPoint(
        matrix[0][0] * point.x + matrix[0][1] * point.y + matrix[0][2],
        matrix[1][0] * point.x + matrix[1][1] * point.y + matrix[1][2],
    )


def _validate_bound(max_correctable_degrees: Decimal) -> None:
    if not isinstance(max_correctable_degrees, Decimal):
        raise TypeError("max_correctable_degrees must be a Decimal")
    if not max_correctable_degrees.is_finite():
        raise ValueError("max_correctable_degrees must be finite")
    if max_correctable_degrees < 0:
        raise ValueError("max_correctable_degrees must not be negative")


def _require_image(image: object) -> PageImage:
    if not isinstance(image, np.ndarray):
        raise TypeError("image must be an OpenCV-compatible rendered page")
    if image.dtype != np.uint8:
        raise TypeError("rendered page image must contain unsigned 8-bit pixels")
    if image.size == 0:
        raise ValueError("rendered page image must not be empty")
    shape = image.shape
    if len(shape) not in (2, 3):
        raise ValueError("rendered page image must be grayscale or colour")
    if len(shape) == 3 and shape[2] not in (3, 4):
        raise ValueError("colour rendered page must have three or four channels")
    return image


def _grayscale(image: PageImage) -> PageImage:
    shape = image.shape
    if len(shape) == 2:
        return image
    conversion = cv2.COLOR_BGRA2GRAY if shape[2] == 4 else cv2.COLOR_BGR2GRAY
    return cast(PageImage, cv2.cvtColor(image, conversion))


def detect_skew(image: PageImage) -> Decimal:
    """Estimate the page's signed skew from the minimum rectangle around foreground ink.

    Positive means the rendered content is counter-clockwise from horizontal. A page with no
    foreground ink has no observable skew and is returned as zero; no resampling is performed.
    """

    image = _require_image(image)
    gray = _grayscale(image)
    _, foreground = cv2.threshold(
        gray,
        0,
        255,
        cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
    )
    points = cv2.findNonZero(foreground)
    if points is None:
        return Decimal(0)

    _, (width, height), raw_angle = cv2.minAreaRect(points)
    if width == 0 or height == 0:
        return Decimal(0)
    detected = 90.0 - raw_angle if width < height else -raw_angle
    if detected == 0.0:
        return Decimal(0)
    return _decimal(detected)


def _expanded_rotation(
    image: PageImage, angle: Decimal
) -> tuple[PageImage, AffineMatrix, AffineMatrix]:
    height, width = image.shape[:2]
    centre = (width / 2.0, height / 2.0)
    rotation = cv2.getRotationMatrix2D(centre, float(angle), 1.0)

    cosine = abs(rotation[0, 0])
    sine = abs(rotation[0, 1])
    corrected_width = max(1, round((height * sine) + (width * cosine)))
    corrected_height = max(1, round((height * cosine) + (width * sine)))
    rotation[0, 2] += (corrected_width / 2.0) - centre[0]
    rotation[1, 2] += (corrected_height / 2.0) - centre[1]

    corrected = cast(
        PageImage,
        cv2.warpAffine(
            image,
            rotation,
            (corrected_width, corrected_height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255, 255),
        ),
    )
    inverse = cast(OpenCvMatrix, cv2.invertAffineTransform(rotation))
    return corrected, _matrix(cast(OpenCvMatrix, rotation)), _matrix(inverse)


def deskew(image: PageImage, *, max_correctable_degrees: Decimal) -> DeskewResult:
    """Correct a rendered page's skew when it is within the caller's stated bound.

    The bound is required and recorded because it is empirical; this module must not invent one.
    Exactly straight pages return the original image object, avoiding a lossy encode/rotate cycle.
    Pages beyond the bound remain unmodified and refuse access through ``image_for_ocr``.
    """

    _validate_bound(max_correctable_degrees)
    image = _require_image(image)
    detected = detect_skew(image)
    identity = RotationCorrection(detected, Decimal(0), IDENTITY_AFFINE, IDENTITY_AFFINE)

    if detected == 0:
        return DeskewResult(
            image=image,
            status=DeskewStatus.UNCHANGED,
            correction=identity,
            max_correctable_degrees=max_correctable_degrees,
            reason="page is already straight; no resampling was applied",
        )
    if abs(detected) > max_correctable_degrees:
        return DeskewResult(
            image=image,
            status=DeskewStatus.REVIEW_REQUIRED,
            correction=identity,
            max_correctable_degrees=max_correctable_degrees,
            reason=(
                f"detected skew {detected} degrees exceeds the stated correction bound "
                f"of {max_correctable_degrees} degrees"
            ),
        )

    applied = -detected
    corrected, forward, inverse = _expanded_rotation(image, applied)
    return DeskewResult(
        image=corrected,
        status=DeskewStatus.CORRECTED,
        correction=RotationCorrection(detected, applied, forward, inverse),
        max_correctable_degrees=max_correctable_degrees,
        reason=f"applied {applied} degrees before OCR",
    )


__all__ = [
    "AffineMatrix",
    "DeskewPoint",
    "DeskewRefusedError",
    "DeskewResult",
    "DeskewStatus",
    "RotationCorrection",
    "deskew",
    "detect_skew",
]
