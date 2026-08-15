"""Verification for issue #170: valid, page-bound evidence polygons."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import UUID

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon, PolygonSpaceMismatchError

DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")


def point(x: str, y: str) -> StoredPoint:
    return StoredPoint(Decimal(x), Decimal(y))


def rectangle(
    left: str,
    top: str,
    right: str,
    bottom: str,
    *,
    document_version_id: UUID = DOCUMENT_ID,
    page: int = 0,
) -> Polygon:
    return Polygon(
        points=(
            point(left, top),
            point(right, top),
            point(right, bottom),
            point(left, bottom),
        ),
        space="stored",
        document_version_id=document_version_id,
        page=page,
    )


def test_valid_polygon_is_immutable_and_carries_its_coordinate_identity() -> None:
    polygon = rectangle("0", "0", "1", "1", page=3)

    assert polygon.space == "stored"
    assert polygon.document_version_id == DOCUMENT_ID
    assert polygon.page == 3
    assert hash(polygon)
    with pytest.raises(FrozenInstanceError):
        polygon.page = 4  # type: ignore[misc]


def test_stored_page_boundaries_are_inclusive() -> None:
    polygon = rectangle("0", "0", "1", "1")

    assert polygon.points[0] == point("0", "0")
    assert polygon.points[2] == point("1", "1")


@pytest.mark.parametrize(
    "outside",
    [
        point("-0.0001", "0.5"),
        point("1.0001", "0.5"),
        point("0.5", "-0.0001"),
        point("0.5", "1.0001"),
    ],
)
def test_out_of_page_coordinate_is_rejected_not_clamped(outside: StoredPoint) -> None:
    with pytest.raises(ValueError, match="bounds 0..1"):
        Polygon(
            points=(point("0.1", "0.1"), point("0.9", "0.1"), outside),
            space="stored",
            document_version_id=DOCUMENT_ID,
            page=0,
        )


def test_fewer_than_three_points_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least three"):
        Polygon(
            points=(point("0", "0"), point("1", "1")),
            space="stored",
            document_version_id=DOCUMENT_ID,
            page=0,
        )


def test_exactly_zero_area_is_rejected_without_a_float_tolerance() -> None:
    with pytest.raises(ValueError, match="exactly zero area"):
        Polygon(
            points=(point("0", "0"), point("0.5", "0.5"), point("1", "1")),
            space="stored",
            document_version_id=DOCUMENT_ID,
            page=0,
        )


def test_very_thin_nonzero_polygon_remains_valid() -> None:
    thin = rectangle("0", "0", "1", "0.0000000000000000000000000001")

    assert thin.points[-1].y == Decimal("0.0000000000000000000000000001")


def test_self_intersection_with_nonzero_signed_area_is_rejected() -> None:
    with pytest.raises(ValueError, match="self-intersect"):
        Polygon(
            points=(point("0", "0"), point("1", "1"), point("0", "1"), point("0.8", "0")),
            space="stored",
            document_version_id=DOCUMENT_ID,
            page=0,
        )


def test_construction_accepts_either_winding_and_any_starting_point() -> None:
    """The same valid boundary remains valid when its order or starting vertex changes."""

    points = (point("0.1", "0.1"), point("0.6", "0.1"), point("0.6", "0.6"))
    forward = Polygon(points, "stored", DOCUMENT_ID, 0)
    reverse_winding = Polygon(tuple(reversed(points)), "stored", DOCUMENT_ID, 0)
    rotated_start = Polygon(points[1:] + points[:1], "stored", DOCUMENT_ID, 0)

    assert forward.contains(reverse_winding)
    assert reverse_winding.contains(rotated_start)


def test_contains_permits_inner_polygon_to_touch_boundary_from_inside() -> None:
    outer = rectangle("0.1", "0.1", "0.5", "0.5")
    inner_touching = rectangle("0.2", "0.2", "0.5", "0.4")

    assert outer.contains(inner_touching)


def test_contains_rejects_polygon_with_any_exterior_area() -> None:
    outer = rectangle("0.1", "0.1", "0.5", "0.5")
    partly_outside = rectangle("0.2", "0.2", "0.51", "0.4")

    assert not outer.contains(partly_outside)


def test_overlap_includes_partial_intersection_and_complete_containment() -> None:
    outer = rectangle("0.1", "0.1", "0.5", "0.5")
    partial = rectangle("0.49", "0.2", "0.7", "0.4")
    contained = rectangle("0.2", "0.2", "0.3", "0.3")

    assert outer.overlaps(partial)
    assert outer.overlaps(contained)
    assert contained.overlaps(outer)
    assert outer.overlaps(outer)


@pytest.mark.parametrize(
    "touching",
    [
        rectangle("0.5", "0.2", "0.7", "0.4"),
        rectangle("0.5", "0.5", "0.7", "0.7"),
    ],
)
def test_shared_edge_or_corner_alone_is_not_overlap_or_containment(touching: Polygon) -> None:
    outer = rectangle("0.1", "0.1", "0.5", "0.5")

    assert not outer.overlaps(touching)
    assert not outer.contains(touching)


def test_comparison_across_document_versions_raises() -> None:
    first = rectangle("0.1", "0.1", "0.5", "0.5")
    second = rectangle(
        "0.2",
        "0.2",
        "0.3",
        "0.3",
        document_version_id=UUID("87654321-4321-8765-4321-876543218765"),
    )

    with pytest.raises(PolygonSpaceMismatchError, match="document version, page"):
        first.contains(second)
    with pytest.raises(PolygonSpaceMismatchError, match="document version, page"):
        first.overlaps(second)


def test_comparison_across_pages_raises() -> None:
    first = rectangle("0.1", "0.1", "0.5", "0.5", page=1)
    second = rectangle("0.2", "0.2", "0.3", "0.3", page=2)

    with pytest.raises(PolygonSpaceMismatchError, match="document version, page"):
        first.contains(second)


@pytest.mark.parametrize(
    ("field", "value", "error", "message"),
    [
        ("space", "image", ValueError, "space must be"),
        ("document_version_id", "not-a-uuid", TypeError, "must be a UUID"),
        ("page", -1, ValueError, "zero or greater"),
        ("page", False, TypeError, "must be an integer"),
    ],
)
def test_invalid_identity_is_rejected(
    field: str, value: object, error: type[Exception], message: str
) -> None:
    arguments: dict[str, object] = {
        "points": (point("0", "0"), point("1", "0"), point("0", "1")),
        "space": "stored",
        "document_version_id": DOCUMENT_ID,
        "page": 0,
    }
    arguments[field] = value

    with pytest.raises(error, match=message):
        Polygon(**arguments)  # type: ignore[arg-type]


def test_no_public_area_method_launders_shapely_float_precision() -> None:
    polygon = rectangle("0.1", "0.1", "0.5", "0.5")

    assert not hasattr(polygon, "area")
