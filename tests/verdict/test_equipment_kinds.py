"""The reviewer names the equipment cabinet: sink, microwave, under-counter refrigeration, or other (#818).

Verification for: `vocabulary/cabinet_categories.py`, and its readers — `cabinet_run_distribution`,
the form's choices (`rules/required_inputs.CATEGORICAL_VALUES`) and the narrative.

The one that matters most is `test_a_named_equipment_cabinet_distributes_exactly_as_other_equipment`:
slide 11's kinds are equipment cabinets, and a run with one must come out exactly as a run whose
cabinet is marked `equipment` — the width it holds is the appliance's, whatever the appliance is.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from rules.required_inputs import CATEGORICAL_VALUES
from units.measurement import Measurement, Unit
from verdict.operations.distribution import CabinetType, cabinet_run_distribution
from vocabulary.cabinet_categories import CABINET_CATEGORY_LABELS, CabinetCategory
from vocabulary.semantic_types import SemanticType

NAMED = (
    CabinetCategory.SINK_CABINET,
    CabinetCategory.MICROWAVE_CABINET,
    CabinetCategory.UNDER_COUNTER_REFRIGERATION_CABINET,
)
REGULAR = (CabinetCategory.SINGLE_DOOR, CabinetCategory.DOUBLE_DOOR, CabinetCategory.DRAWER)


def _inches(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, None)


@pytest.mark.parametrize("kind", [*NAMED, CabinetCategory.EQUIPMENT])
def test_every_equipment_kind_is_fixed_width_and_has_no_bound(kind: CabinetCategory) -> None:
    assert kind.is_equipment
    with pytest.raises(ValueError, match="no width bound"):
        _ = kind.bound_prefix


@pytest.mark.parametrize("kind", REGULAR)
def test_the_regular_cabinets_still_move(kind: CabinetCategory) -> None:
    assert not kind.is_equipment
    assert kind.bound_prefix == f"{kind.value}_cab_width"


def test_the_stored_equipment_value_is_unchanged() -> None:
    """Classifications already stored say `equipment`; renaming it would orphan them."""
    assert CabinetCategory("equipment") is CabinetCategory.EQUIPMENT


def test_the_form_offers_seven_categories_regular_first_then_equipment() -> None:
    expected = (
        "single_door",
        "double_door",
        "drawer",
        "sink_cabinet",
        "microwave_cabinet",
        "under_counter_refrigeration_cabinet",
        "equipment",
    )
    assert CATEGORICAL_VALUES[SemanticType.CABINET_CATEGORY.value] == expected
    assert tuple(kind.value for kind, _ in CABINET_CATEGORY_LABELS) == expected
    assert dict(CABINET_CATEGORY_LABELS)[CabinetCategory.EQUIPMENT].startswith("Other equipment")


def _run(middle: CabinetCategory):  # type: ignore[no-untyped-def]
    """The client lead's scenario 2 shape: the run grows 4", the fillers take 2", the regular cabinets the rest."""
    bounds = {
        f"{kind.value}_cab_width_{edge}": _inches(1 if edge == "min" else 96)
        for kind in CabinetType
        if not kind.is_equipment
        for edge in ("min", "max")
    }
    return cabinet_run_distribution(
        field_width=_inches(94),
        design_width=_inches(90),
        design_fillers=[_inches(3), _inches(3)],
        proposed_fillers=[_inches(4), _inches(4)],
        design_cabinets=[_inches(24), _inches(36), _inches(24)],
        proposed_cabinets=[_inches(25), _inches(36), _inches(25)],
        cabinet_type=["double_door", middle.value, "double_door"],
        **bounds,
        filler_min=_inches(1),
        filler_max=_inches(4),
    )


@pytest.mark.parametrize("kind", NAMED)
def test_a_named_equipment_cabinet_distributes_exactly_as_other_equipment(
    kind: CabinetCategory,
) -> None:
    """**The point.** Outcome: the same verdict, the same expected cabinets and fillers, and the
    named cabinet listed among the fixed ones — only the recorded name differs."""
    named, other = _run(kind), _run(CabinetCategory.EQUIPMENT)

    assert named.outcome is other.outcome
    named_facts, other_facts = dict(named.intermediates), dict(other.intermediates)
    assert named_facts["equipment_cabinets"] == other_facts["equipment_cabinets"] == (1,)
    for key, value in other_facts.items():
        if key != "cabinet_types":
            assert named_facts[key] == value, key
    assert named_facts["cabinet_types"] == ("double_door", kind.value, "double_door")
