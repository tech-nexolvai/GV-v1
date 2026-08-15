"""Validated evidence polygons in the normalised stored coordinate space.

Shapely remains an internal implementation detail for topology and spatial predicates.
The safety-critical zero-area decision is exact and uses ``Fraction`` instead.

Source: backend proposal section 10.1, ADR-0016 and issue #170.
Verification: ``tests/evidence/test_polygon.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Literal
from uuid import UUID

from shapely.geometry import (  # type: ignore[import-untyped]
    Polygon as ShapelyPolygon,
)

from evidence.coordinates import StoredPoint

STORED_MINIMUM = Decimal(0)
STORED_MAXIMUM = Decimal(1)


class PolygonSpaceMismatchError(ValueError):
    """Raised when polygons from unrelated coordinate planes are compared."""


def _twice_signed_area(points: tuple[StoredPoint, ...]) -> Fraction:
    """Return exact twice-signed area using the shoelace formula."""

    total = Fraction(0)
    for point, following in zip(points, points[1:] + points[:1], strict=True):
        total += Fraction(point.x) * Fraction(following.y)
        total -= Fraction(point.y) * Fraction(following.x)
    return total


@dataclass(frozen=True, slots=True)
class Polygon:
    """A valid polygon tied to one stored page coordinate space.

    Stored coordinates are rotation-applied ``Decimal`` values normalised to ``0..1``.
    Comparisons are meaningful only within the same document version, page and named
    space; attempting a cross-plane comparison raises instead of returning a misleading
    geometric answer.
    """

    points: tuple[StoredPoint, ...]
    space: Literal["stored"]
    document_version_id: UUID
    page: int

    def __post_init__(self) -> None:
        """Reject malformed, degenerate, self-intersecting or out-of-page geometry."""

        if not isinstance(self.points, tuple):
            raise TypeError("points must be a tuple of StoredPoint values")
        if len(self.points) < 3:
            raise ValueError("a polygon requires at least three points")
        for point in self.points:
            if not isinstance(point, StoredPoint):
                raise TypeError("points must contain only StoredPoint values")
            if not isinstance(point.x, Decimal) or not isinstance(point.y, Decimal):
                raise TypeError("StoredPoint coordinates must be Decimal values")
            if not (
                STORED_MINIMUM <= point.x <= STORED_MAXIMUM
                and STORED_MINIMUM <= point.y <= STORED_MAXIMUM
            ):
                raise ValueError("polygon coordinates must stay within stored page bounds 0..1")

        if self.space != "stored":
            raise ValueError("space must be 'stored'")
        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        if isinstance(self.page, bool) or not isinstance(self.page, int):
            raise TypeError("page must be an integer")
        if self.page < 0:
            raise ValueError("page must be zero or greater")

        if _twice_signed_area(self.points) == 0:
            raise ValueError("polygon has exactly zero area")
        if not self._shape.is_valid:
            raise ValueError("polygon must not self-intersect")

    @property
    def _shape(self) -> ShapelyPolygon:
        """Build the private float geometry used only for spatial relationships."""

        return ShapelyPolygon([(float(point.x), float(point.y)) for point in self.points])

    def _require_same_space(self, other: Polygon) -> None:
        if not isinstance(other, Polygon):
            raise TypeError("other must be a Polygon")
        if (
            self.document_version_id != other.document_version_id
            or self.page != other.page
            or self.space != other.space
        ):
            raise PolygonSpaceMismatchError(
                "polygons must share document version, page and coordinate space"
            )

    def contains(self, other: Polygon) -> bool:
        """Return whether ``other`` lies inside, permitting contact from within.

        Boundary contact is allowed only when no part of ``other`` lies outside this
        polygon. Two polygons that merely touch from opposite sides are not containment.
        """

        self._require_same_space(other)
        return bool(self._shape.covers(other._shape))

    def overlaps(self, other: Polygon) -> bool:
        """Return whether the polygons share interior area.

        Unlike Shapely's predicate with the same name, this includes containment and
        identical polygons. A shared edge or corner alone has zero shared area and is
        therefore not overlap.
        """

        self._require_same_space(other)
        return bool(self._shape.intersection(other._shape).area > 0)
