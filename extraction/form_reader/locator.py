"""Map Qwen prompt-v5 boxes to stored page geometry and snap only on a unique overlap."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

from evidence.coordinates import ImagePoint, PageTransform
from evidence.polygon import Polygon
from extraction.form_reader.schema import NormalizedBox


@dataclass(frozen=True, slots=True)
class LocatedBox:
    polygon: Polygon
    quality: Literal["snapped", "approximate"]
    snapped_source: str | None = None


def _pixel(value: int, extent: int) -> int:
    return int(
        (Decimal(value) * Decimal(extent) / Decimal(1000)).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )


def _stored_polygon(
    points: tuple[ImagePoint, ...], *, transform: PageTransform, version_id: UUID, page_index: int
) -> Polygon:
    return Polygon(
        points=tuple(transform.to_stored(point) for point in points),
        space="stored",
        document_version_id=version_id,
        page=page_index,
    )


def box_polygon(
    box: NormalizedBox,
    *,
    width_px: int,
    height_px: int,
    transform: PageTransform,
    document_version_id: UUID,
    page_index: int,
) -> Polygon:
    """Convert the model's page-normalized rectangle through the measured page transform."""
    if width_px <= 0 or height_px <= 0:
        raise ValueError("render dimensions must be positive")
    left, right = _pixel(box.x0, width_px), _pixel(box.x1, width_px)
    top, bottom = _pixel(box.y0, height_px), _pixel(box.y1, height_px)
    if right <= left or bottom <= top:
        raise ValueError("normalized box collapses at the rendered page resolution")
    return _stored_polygon(
        (
            ImagePoint(left, top),
            ImagePoint(right, top),
            ImagePoint(right, bottom),
            ImagePoint(left, bottom),
        ),
        transform=transform,
        version_id=document_version_id,
        page_index=page_index,
    )


def _overlap_area(left: Polygon, right: Polygon) -> float:
    left._require_same_space(right)
    return float(left._shape.intersection(right._shape).area)


def locate_box(
    box: NormalizedBox,
    *,
    width_px: int,
    height_px: int,
    transform: PageTransform,
    document_version_id: UUID,
    page_index: int,
    regions: Sequence[tuple[str, Polygon]],
) -> LocatedBox:
    """Snap to one distinct overlapping same-page region; otherwise keep the Qwen box approximate."""
    proposed = box_polygon(
        box,
        width_px=width_px,
        height_px=height_px,
        transform=transform,
        document_version_id=document_version_id,
        page_index=page_index,
    )
    matches: dict[tuple[tuple[Decimal, Decimal], ...], tuple[str, Polygon]] = {}
    for source, polygon in regions:
        proposed._require_same_space(polygon)
        if _overlap_area(proposed, polygon) > 0:
            identity = tuple((point.x, point.y) for point in polygon.points)
            matches.setdefault(identity, (source, polygon))
    if len(matches) == 1:
        source, polygon = next(iter(matches.values()))
        return LocatedBox(polygon=polygon, quality="snapped", snapped_source=source)
    return LocatedBox(polygon=proposed, quality="approximate")
