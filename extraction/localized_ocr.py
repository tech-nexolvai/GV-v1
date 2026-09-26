"""Read vendor outlined-text regions as small, high-resolution OCR crops.

The vendor drawing in a reviewed set lives in ``/Stamp`` annotations while reviewer notes are
``/FreeText`` annotations.  A full page at a safe shared DPI makes the vendor's dual-notation
dimension glyphs too small for OCR.  This route retains the existing deterministic geometry: it
uses each outlined-text candidate region, renders *only* the stamp layer for that region at the
measured crop resolution, then maps a uniquely recognised dual-unit reading back to that original
region.  Production association still decides whether the reading belongs to a dimension line.

This module never assigns a semantic type, resolves a verdict, or converts an unclear crop into a
number.  A crop that contains zero or more than one recognised dual-unit dimension remains
unlocated, so downstream association and the gold-set grader abstain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from evidence.coordinates import ImagePoint, PageTransform
from evidence.crop import decode_rgb_png
from evidence.polygon import Polygon
from extraction.annotations import OutlinedTextRegion
from extraction.ocr import OcrEngine, OcrItem, combine_dual_notation
from extraction.rasterise import VISION_CROP_DPI
from extraction.vector_first import region_crop

__all__ = ["read_localized_vendor_regions"]


def read_localized_vendor_regions(
    data: bytes,
    *,
    page_index: int,
    regions: Sequence[OutlinedTextRegion],
    engine: OcrEngine,
    page_transform: PageTransform,
    margin_pt: Decimal,
) -> tuple[OcrItem, ...]:
    """OCR vendor-only crops and locate only an unambiguous dual-unit reading per region.

    ``region_crop(..., include_markup=False)`` removes every non-``/Stamp`` annotation in memory.
    The original PDF bytes are not changed, and no reviewer's annotation can reach ``engine.read``.
    ``page_transform`` describes the stored full-page candidate coordinate frame, not the crop's
    local pixels, which keeps persisted polygons usable by evidence rendering and association.
    """
    if isinstance(margin_pt, float) or not isinstance(margin_pt, Decimal):
        raise TypeError("margin_pt must be a Decimal")
    if not margin_pt.is_finite() or margin_pt < 0:
        raise ValueError("margin_pt must be finite and non-negative")

    readings: list[OcrItem] = []
    for region in regions:
        crop = region_crop(
            data,
            page_index,
            region,
            dpi=VISION_CROP_DPI,
            margin_pt=margin_pt,
            include_markup=False,
            return_metadata=True,
        )
        width, height, rgb = decode_rgb_png(crop.png)
        combined = combine_dual_notation(engine.read(rgb, width=width, height=height))
        located = [item for item in combined if item.rotation_degrees is not None]

        # The reader's rectangle is the source location. A single recognised stacked mm[inch] pair
        # has exactly the layout guarantee the ordinary OCR route needs; mapping it through the
        # crop's inverse transform preserves that source location in the full-page coordinate frame.
        # Multiple such pairs would leave the question "which one labels this line?" unresolved, so
        # none is associated.
        if len(located) == 1:
            item = located[0]
            image_extent = crop.map_reader_polygon(item.image_extent, target_dpi=page_transform.dpi)
            assert item.rotation_degrees is not None
            readings.append(
                OcrItem(
                    text=item.text,
                    confidence=item.confidence,
                    image_extent=image_extent,
                    rotation_degrees=(
                        (item.rotation_degrees + crop.applied_rotation_degrees) % 360
                    ),
                    extent=_stored_polygon(
                        image_extent,
                        page_transform=page_transform,
                        page_index=page_index,
                        document_version_id=region.extent.document_version_id,
                    ),
                    crop_rotation_degrees=crop.applied_rotation_degrees,
                )
            )
            continue

        # Preserve raw OCR evidence for a reviewer, but deliberately with no orientation or stored
        # extent.  It cannot enter production association or become a gold-set reading.
        readings.extend(
            replace(
                item,
                image_extent=crop.map_reader_polygon(
                    item.image_extent, target_dpi=page_transform.dpi
                ),
                rotation_degrees=None,
                extent=None,
                crop_rotation_degrees=crop.applied_rotation_degrees,
            )
            for item in combined
        )
    return tuple(readings)


def _stored_polygon(
    points: tuple[ImagePoint, ...],
    *,
    page_transform: PageTransform,
    page_index: int,
    document_version_id: UUID,
) -> Polygon:
    return Polygon(
        points=tuple(page_transform.to_stored(point) for point in points),
        space="stored",
        document_version_id=document_version_id,
        page=page_index,
    )
