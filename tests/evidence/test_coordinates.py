"""Verification for issue #169: typed, reversible coordinate transforms."""

from __future__ import annotations

from decimal import Decimal

import pytest

from evidence.coordinates import ImagePoint, PageTransform, PdfPoint, StoredPoint


def transform(*, rotation: int = 0, dpi: int = 72) -> PageTransform:
    """Return a transform with non-zero sheet and crop origins."""

    return PageTransform(
        dpi=dpi,
        rotation=rotation,
        media_box=(Decimal(5), Decimal(10), Decimal(205), Decimal(310)),
        crop_box=(Decimal(10), Decimal(20), Decimal(110), Decimal(220)),
    )


@pytest.mark.parametrize(
    ("rotation", "expected_bottom_left", "expected_top_right"),
    [
        (0, ImagePoint(0, 200), ImagePoint(100, 0)),
        (90, ImagePoint(0, 0), ImagePoint(200, 100)),
        (180, ImagePoint(100, 0), ImagePoint(0, 200)),
        (270, ImagePoint(200, 100), ImagePoint(0, 0)),
    ],
)
def test_crop_corners_map_with_the_correct_origin_and_rotation(
    rotation: int,
    expected_bottom_left: ImagePoint,
    expected_top_right: ImagePoint,
) -> None:
    page = transform(rotation=rotation)

    assert page.to_image(PdfPoint(Decimal(10), Decimal(20))) == expected_bottom_left
    assert page.to_image(PdfPoint(Decimal(110), Decimal(220))) == expected_top_right


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_image_round_trip_is_within_one_pixel(rotation: int) -> None:
    page = transform(rotation=rotation, dpi=144)
    original = PdfPoint(Decimal("43.2"), Decimal("151.7"))
    restored = page.to_pdf(page.to_image(original))
    one_pixel_in_points = Decimal(72) / Decimal(page.dpi)

    assert abs(restored.x - original.x) <= one_pixel_in_points
    assert abs(restored.y - original.y) <= one_pixel_in_points


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_image_stored_round_trip_is_within_one_pixel(rotation: int) -> None:
    page = transform(rotation=rotation, dpi=137)
    original = ImagePoint(71, 123)
    restored = page.from_stored(page.to_stored(original))

    assert abs(restored.x - original.x) <= 1
    assert abs(restored.y - original.y) <= 1


def test_stored_coordinates_use_the_visible_crop_not_the_media_box() -> None:
    page = transform()

    assert page.to_stored(ImagePoint(50, 100)) == StoredPoint(Decimal("0.5"), Decimal("0.5"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("10.49"), ImagePoint(10, 190)),
        (Decimal("10.5"), ImagePoint(11, 190)),
        (Decimal("-0.5"), ImagePoint(-1, 190)),
    ],
)
def test_single_points_round_half_away_from_zero(value: Decimal, expected: ImagePoint) -> None:
    page = PageTransform(
        dpi=72,
        rotation=0,
        media_box=(Decimal(0), Decimal(0), Decimal(100), Decimal(200)),
        crop_box=(Decimal(0), Decimal(0), Decimal(100), Decimal(200)),
    )

    assert page.to_image(PdfPoint(value, Decimal(10))) == expected


def test_coordinate_spaces_are_not_interchangeable_at_runtime() -> None:
    page = transform()

    with pytest.raises(TypeError, match="PdfPoint"):
        page.to_image(ImagePoint(1, 2))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ImagePoint"):
        page.to_stored(PdfPoint(Decimal(1), Decimal(2)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="StoredPoint"):
        page.from_stored(ImagePoint(1, 2))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("method", "point", "message"),
    [
        ("to_image", PdfPoint(1, 2), "Decimal"),  # type: ignore[arg-type]
        ("to_pdf", ImagePoint(True, 2), "integer"),
        ("to_stored", ImagePoint(1, False), "integer"),
        ("from_stored", StoredPoint(1, 2), "Decimal"),  # type: ignore[arg-type]
    ],
)
def test_coordinate_values_cannot_smuggle_in_inexact_or_boolean_values(
    method: str, point: object, message: str
) -> None:
    page = transform()

    with pytest.raises(TypeError, match=message):
        getattr(page, method)(point)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"dpi": 0}, ValueError, "greater than zero"),
        ({"rotation": 45}, ValueError, "0, 90, 180 or 270"),
        (
            {"crop_box": (Decimal(0), Decimal(0), Decimal(210), Decimal(200))},
            ValueError,
            "within media_box",
        ),
        (
            {"crop_box": (Decimal(20), Decimal(20), Decimal(20), Decimal(100))},
            ValueError,
            "positive width",
        ),
    ],
)
def test_invalid_transform_metadata_is_rejected(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    values: dict[str, object] = {
        "dpi": 72,
        "rotation": 0,
        "media_box": (Decimal(0), Decimal(0), Decimal(200), Decimal(300)),
        "crop_box": (Decimal(10), Decimal(10), Decimal(100), Decimal(200)),
    }
    values.update(kwargs)

    with pytest.raises(error, match=message):
        PageTransform(**values)  # type: ignore[arg-type]
