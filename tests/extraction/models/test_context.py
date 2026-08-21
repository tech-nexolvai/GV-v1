"""Crop-bounded context tests for issue #252."""

from decimal import Decimal

import pytest

from extraction.models.context import (
    AssemblyInput,
    NearbyGeometry,
    NearbyText,
    Segment,
    assemble,
)


def _source() -> AssemblyInput:
    return AssemblyInput(
        nearby_text=(
            NearbyText("984", Decimal("4.25")),
            NearbyText("outside", Decimal("12.01")),
        ),
        nearby_geometry=(
            NearbyGeometry(
                Segment((Decimal("1.1"), Decimal("2.2")), (Decimal("3.3"), Decimal("4.4"))),
                Decimal(12),
            ),
            NearbyGeometry(
                Segment((Decimal(20), Decimal(20)), (Decimal(30), Decimal(30))),
                Decimal(13),
            ),
        ),
    )


def test_assembly_keeps_only_items_inside_the_explicit_bound() -> None:
    """Input: items inside/outside 12 pt. Output: inside only. Why: no context leakage."""

    context = assemble(_source(), bound_pt=Decimal(12))

    assert [item.text for item in context.nearby_text] == ["984"]
    assert len(context.nearby_geometry) == 1


def test_an_item_exactly_at_the_bound_is_included() -> None:
    """Input: geometry at exactly 12 pt. Output: included. Why: boundary is explicit."""

    context = assemble(_source(), bound_pt=Decimal(12))

    assert context.nearby_geometry[0].distance_pt == Decimal(12)


@pytest.mark.parametrize("bound", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_bounds_are_rejected(bound: Decimal) -> None:
    """Input: unsafe bound. Output: ValueError. Why: NaN must not bypass comparisons."""

    with pytest.raises(ValueError, match="bound_pt"):
        assemble(_source(), bound_pt=bound)


def test_recorded_context_contains_strings_not_floats() -> None:
    """Input: decimal geometry. Output: decimal strings. Why: JSON must remain exact."""

    record = assemble(_source(), bound_pt=Decimal(12)).as_record()

    assert record == {
        "nearby_text": [{"text": "984", "distance_pt": "4.25"}],
        "nearby_geometry": [
            {
                "start": ["1.1", "2.2"],
                "end": ["3.3", "4.4"],
                "distance_pt": "12",
            }
        ],
    }


def test_the_input_has_no_full_package_or_page_surface() -> None:
    """Input: AssemblyInput fields. Output: neighbourhood only. Why: package access is impossible."""

    assert set(AssemblyInput.__dataclass_fields__) == {"nearby_text", "nearby_geometry"}
