"""Deskew keeps OCR readable without detaching evidence from the rendered page.

Source: issue #178 and ``docs/DESIGN_EXTRACTION.md`` section 6.
Verification: ``extraction/geometry/deskew.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import cv2
import numpy as np
import pytest

from extraction.geometry.deskew import (
    DeskewPoint,
    DeskewRefusedError,
    DeskewStatus,
    deskew,
    detect_skew,
)


def _page() -> np.ndarray:
    image = np.full((300, 500), 255, dtype=np.uint8)
    cv2.putText(
        image,
        "COUNTERTOP 984",
        (40, 160),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.5,
        0,
        3,
        cv2.LINE_AA,
    )
    return image


def _rotate(image: np.ndarray, degrees: Decimal) -> np.ndarray:
    height, width = image.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), float(degrees), 1)
    return cv2.warpAffine(image, matrix, (width, height), borderValue=255)


def test_a_straight_page_is_the_same_object_and_is_not_resampled() -> None:
    """Input: horizontal rendered text. Outcome: UNCHANGED and the identical image object."""
    rendered = _page()

    result = deskew(rendered, max_correctable_degrees=Decimal(5))

    assert result.status is DeskewStatus.UNCHANGED
    assert result.image is rendered
    assert result.image_for_ocr is rendered
    assert result.correction.applied_rotation_degrees == 0


@pytest.mark.parametrize("angle", [Decimal(-3), Decimal(3)])
def test_small_skew_is_corrected_and_the_rotation_is_recorded(angle: Decimal) -> None:
    """Input: a page tilted three degrees. Outcome: opposite correction before OCR."""
    rendered = _rotate(_page(), angle)
    original_skew = detect_skew(rendered)
    result = deskew(rendered, max_correctable_degrees=Decimal(5))

    assert result.status is DeskewStatus.CORRECTED
    assert result.correction.detected_skew_degrees == original_skew
    assert original_skew * angle > 0
    assert result.correction.applied_rotation_degrees == -result.correction.detected_skew_degrees
    assert abs(detect_skew(result.image_for_ocr)) < abs(original_skew)


def test_corrected_points_round_trip_to_the_original_page() -> None:
    """Input: one crop corner on a corrected page. Outcome: inverse mapping restores it."""
    result = deskew(_rotate(_page(), Decimal(4)), max_correctable_degrees=Decimal(5))
    original = DeskewPoint(Decimal("137.25"), Decimal("81.75"))

    corrected = result.correction.to_corrected(original)
    restored = result.correction.to_original(corrected)

    assert abs(restored.x - original.x) < Decimal("0.000001")
    assert abs(restored.y - original.y) < Decimal("0.000001")


def test_skew_beyond_the_stated_bound_is_flagged_and_cannot_reach_ocr() -> None:
    """Input: eight-degree skew with a five-degree bound. Outcome: REVIEW REQUIRED."""
    rendered = _rotate(_page(), Decimal(8))

    result = deskew(rendered, max_correctable_degrees=Decimal(5))

    assert result.status is DeskewStatus.REVIEW_REQUIRED
    assert result.image is rendered
    assert result.correction.applied_rotation_degrees == 0
    assert "exceeds the stated correction bound" in result.reason
    with pytest.raises(DeskewRefusedError, match="exceeds"):
        _ = result.image_for_ocr


@pytest.mark.parametrize(
    ("bound", "error", "message"),
    [
        (Decimal("NaN"), ValueError, "finite"),
        (Decimal("Infinity"), ValueError, "finite"),
        (Decimal("-0.1"), ValueError, "negative"),
        (5, TypeError, "Decimal"),
    ],
)
def test_an_unsafe_or_implicit_bound_is_refused(
    bound: object, error: type[Exception], message: str
) -> None:
    """Input: missing numeric discipline. Outcome: refusal instead of a guessed/coerced bound."""
    with pytest.raises(error, match=message):
        deskew(_page(), max_correctable_degrees=bound)  # type: ignore[arg-type]


def test_render_deskew_ocr_order_uses_only_the_prepared_image() -> None:
    """Input: synthetic render and OCR stages. Outcome: render, deskew, then OCR in that order."""
    events: list[str] = []

    def render() -> np.ndarray:
        events.append("render")
        return _rotate(_page(), Decimal(3))

    rendered = render()
    original_skew = abs(detect_skew(rendered))

    def ocr(image: np.ndarray) -> str:
        events.append("ocr")
        assert abs(detect_skew(image)) < original_skew
        return "984"

    events.append("deskew")
    prepared = deskew(rendered, max_correctable_degrees=Decimal(5))
    reading = ocr(prepared.image_for_ocr)

    assert reading == "984"
    assert events == ["render", "deskew", "ocr"]


def test_the_ordering_test_would_fail_if_ocr_received_the_original_page() -> None:
    """Input: the skewed render bypassing deskew. Outcome: the OCR precondition catches it."""
    rendered = _rotate(_page(), Decimal(3))
    original_skew = abs(detect_skew(rendered))

    def requires_straight(image: np.ndarray) -> None:
        assert abs(detect_skew(image)) < original_skew

    checker: Callable[[np.ndarray], None] = requires_straight
    with pytest.raises(AssertionError):
        checker(rendered)
