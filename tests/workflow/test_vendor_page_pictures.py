from decimal import Decimal

import pytest

from evidence.coordinates import StoredPoint
from workflow.vendor_page_pictures import SnapPoint, nearest_snap


def _point(x: str, y: str, source: str = "dimension") -> SnapPoint:
    return SnapPoint(StoredPoint(Decimal(x), Decimal(y)), source)


def test_nearest_detected_endpoint_inside_stated_tolerance_is_suggested() -> None:
    expected = _point("0.5", "0.5", "witness")
    result = nearest_snap(
        StoredPoint(Decimal("0.503"), Decimal("0.5")),
        [expected, _point("0.51", "0.5")],
        tolerance=Decimal("0.004"),
    )
    assert result == (expected, Decimal("0.003"))


def test_endpoint_outside_tolerance_is_not_snapped() -> None:
    result = nearest_snap(
        StoredPoint(Decimal("0.505"), Decimal("0.5")),
        [_point("0.5", "0.5")],
        tolerance=Decimal("0.004"),
    )
    assert result is None


def test_equal_nearest_endpoints_are_not_arbitrarily_chosen() -> None:
    result = nearest_snap(
        StoredPoint(Decimal("0.5"), Decimal("0.5")),
        [_point("0.499", "0.5", "dimension"), _point("0.501", "0.5", "extension")],
        tolerance=Decimal("0.004"),
    )
    assert result is None


@pytest.mark.parametrize("tolerance", [Decimal("-0.1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_snap_tolerance_is_refused(tolerance: Decimal) -> None:
    with pytest.raises(ValueError, match="tolerance must be finite"):
        nearest_snap(StoredPoint(Decimal(0), Decimal(0)), [], tolerance=tolerance)
