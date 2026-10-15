"""The client lead's slides 5 and 9, reproduced from the arithmetic rather than transcribed.

**Asserted against the numbers, not against his prose.** Matching his wording exactly would pin a
style, and the first sensible edit to a sentence would fail a test that was meant to protect the
figures. What must hold is that every figure he states is stated, with the same meaning.
"""

from __future__ import annotations

import re
from fractions import Fraction

import pytest

from units.measurement import Measurement, Unit
from verdict.operations.distribution import (
    CabinetType,
    DistributionCondition,
    cabinet_run_distribution,
)
from workflow.distribution_narrative import (
    distribution_table,
    explain_distribution,
    explain_distribution_with_table,
)

#: Wide enough that no case is decided by a cabinet bound unless it sets one. Not defaults: the real
#: values are unsettled (CLIENT_FACTS Q21) and nothing in this system may carry one.
WIDE_BOUNDS = {
    f"{kind.value}_cab_width_{edge}": Measurement(
        Fraction(9 if edge == "min" else 48), Unit.INCH, None
    )
    for kind in CabinetType
    if not kind.is_equipment
    for edge in ("min", "max")
}


def _inches(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, None)


def _explain(
    *,
    field: int,
    design: int,
    fillers: tuple[int, ...],
    cabinets: tuple[int, ...],
    types: tuple[str, ...],
    filler_min: int,
    filler_max: int,
    **bounds: Measurement,
) -> str:
    """The drawing as it stands, explained. The shop run mirrors the design run: the reviewer is
    asking what it *should* say, not being told their drawing disagrees with itself."""
    operands = dict(WIDE_BOUNDS)
    operands.update(bounds)
    result = cabinet_run_distribution(
        field_width=_inches(field),
        design_width=_inches(design),
        design_fillers=[_inches(value) for value in fillers],
        proposed_fillers=[_inches(value) for value in fillers],
        design_cabinets=[_inches(value) for value in cabinets],
        proposed_cabinets=[_inches(value) for value in cabinets],
        cabinet_type=list(types),
        filler_min=_inches(filler_min),
        filler_max=_inches(filler_max),
        **operands,  # type: ignore[arg-type]
    )
    return explain_distribution(dict(result.intermediates))


CLIENT_LEAD_LAYOUT = {
    "cabinets": (24, 36, 24),
    "types": ("double_door", "equipment", "double_door"),
    "filler_min": 2,
    "filler_max": 3,
}


def test_slide_five_states_every_figure_the_client_lead_states() -> None:
    """*"...8" needs to be reduced... fillers... from 3" to 2"... absorbs 2"... 6"... 24" to 21""*"""
    said = _explain(field=82, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT)

    for figure in ('90"', '82"', '8"', '3"', '2"', '6"', '36"', '24"', '21"'):
        assert figure in said, f"{figure} is missing from: {said}"
    assert "reduced" in said
    assert "RFI" not in said, "a case that resolves must not read like one that did not"


def test_slide_nine_is_the_same_paragraph_in_the_other_direction() -> None:
    """*"...8" needs be increased... maximum width of the filler is 3"... 24" to 27""*"""
    said = _explain(field=96, design=88, fillers=(2, 2), **CLIENT_LEAD_LAYOUT)

    for figure in ('88"', '96"', '8"', '2"', '3"', '6"', '36"', '24"', '27"'):
        assert figure in said, f"{figure} is missing from: {said}"
    assert "increased" in said
    assert "maximum" in said, "the growing case is bounded by the filler maximum, not the minimum"


def test_the_equipment_cabinet_is_named_as_the_reason_the_cabinets_take_it_all() -> None:
    """Slide 5 gives the reason, not just the result: the equipment cabinet cannot be reduced."""
    said = _explain(field=82, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT)

    assert "equipment cabinet" in said
    assert re.search(r"cannot be less than 36\"", said), said


@pytest.mark.parametrize(
    "kind", ["sink_cabinet", "microwave_cabinet", "under_counter_refrigeration_cabinet"]
)
def test_a_named_equipment_cabinet_is_the_reason_too(kind: str) -> None:
    """#818: the reviewer said *which* equipment cabinet it is. Outcome: the paragraph still gives
    its 36" as the reason the other cabinets take the whole 6", word for word as for `equipment`."""
    named = {**CLIENT_LEAD_LAYOUT, "types": ("double_door", kind, "double_door")}

    said = _explain(field=82, design=90, fillers=(3, 3), **named)

    assert said == _explain(field=82, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT)
    assert re.search(r"cannot be less than 36\"", said), said


def test_no_float_reaches_the_page() -> None:
    """A half inch is `1 1/2"`, the way a drawing writes it — never `1.5`."""
    said = _explain(
        field=89,
        design=90,
        fillers=(2, 2),
        cabinets=(24, 36, 24),
        types=("double_door", "equipment", "double_door"),
        filler_min=1,
        filler_max=2,
    )

    assert '1 1/2"' in said
    assert not re.search(r"\d\.\d", said), f"a decimal reached the reviewer: {said}"


def test_an_unresolvable_run_says_what_blocked_it_and_by_how_much() -> None:
    """An RFI that does not say how far short the drawing falls costs a second round trip."""
    said = _explain(
        field=82,
        design=90,
        fillers=(3, 3),
        cabinets=(24, 36, 24),
        types=("double_door", "equipment", "double_door"),
        filler_min=2,
        filler_max=3,
        double_door_cab_width_min=_inches(23),
    )

    assert "RFI" in said
    assert '23"' in said, "the bound that blocked it"
    assert '6"' in said, "how much is left over"
    assert "will not force a fix" in said


def test_an_uneven_split_says_the_rule_does_not_settle_it() -> None:
    """Ours, not his. The paragraph must not imply the deck answered a question it did not."""
    said = _explain(
        field=108,
        design=114,
        fillers=(3, 3),
        cabinets=(24, 24, 24, 36),
        types=("drawer", "drawer", "drawer", "equipment"),
        filler_min=2,
        filler_max=3,
    )

    assert "does not say" in said
    assert '1 1/3"' in said, "the exact share the rule produced, so a reviewer can settle it"


def test_a_site_that_matches_says_so_in_one_line() -> None:
    said = _explain(field=90, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT)

    assert "already shows the architectural widths" in said
    assert (
        "needs to be" not in said
    ), "nothing has to move, so nothing should be described as moving"


def test_fillers_that_absorb_it_all_say_no_cabinet_changes() -> None:
    """Slide 12, outcome 3: the cabinets get green checks."""
    said = _explain(field=88, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT)

    assert "no cabinet changes" in said
    assert '21"' not in said, "no cabinet moved, so no new cabinet width may appear"


def test_unequal_fillers_ask_for_the_split_rather_than_choosing_one() -> None:
    said = _explain(
        field=86,
        design=90,
        fillers=(2, 4),
        cabinets=(30, 36, 18),
        types=("double_door", "equipment", "double_door"),
        filler_min=1,
        filler_max=4,
    )

    assert "not equal to begin with" in said
    assert "Confirm the split" in said


def test_a_shape_the_check_cannot_compare_says_so_plainly() -> None:
    said = _explain(
        field=82,
        design=90,
        fillers=(3, 3),
        cabinets=(24, 36, 24),
        types=("double_door", "equipment", "appliance"),
        filler_min=2,
        filler_max=3,
    )

    assert "cannot be checked automatically" in said
    assert "by hand" in said


@pytest.mark.parametrize(
    "condition",
    [c for c in DistributionCondition if c is not DistributionCondition.CABINET_SELECTION_REQUIRED],
)
def test_every_condition_the_operation_can_return_produces_a_paragraph(
    condition: DistributionCondition,
) -> None:
    """No condition may reach a reviewer as a bare enum value.

    Walks the enum rather than waiting for one to slip through: the cost of a missing branch is a
    reviewer reading `share_does_not_divide` on the screen where the explanation should be.
    `CABINET_SELECTION_REQUIRED` belongs to `filler_distribution`, which no rule calls any more.
    """
    cases: dict[str, str] = {
        DistributionCondition.NO_CHANGE_REQUIRED.value: _explain(
            field=90, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT
        ),
        DistributionCondition.FILLERS_ABSORB.value: _explain(
            field=88, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT
        ),
        DistributionCondition.CABINETS_ABSORB_REMAINDER.value: _explain(
            field=82, design=90, fillers=(3, 3), **CLIENT_LEAD_LAYOUT
        ),
        DistributionCondition.CANNOT_BE_RESOLVED.value: _explain(
            field=82,
            design=90,
            fillers=(3, 3),
            cabinets=(24, 36, 24),
            types=("double_door", "equipment", "double_door"),
            filler_min=2,
            filler_max=3,
            double_door_cab_width_min=_inches(23),
        ),
        DistributionCondition.SHARE_DOES_NOT_DIVIDE.value: _explain(
            field=108,
            design=114,
            fillers=(3, 3),
            cabinets=(24, 24, 24, 36),
            types=("drawer", "drawer", "drawer", "equipment"),
            filler_min=2,
            filler_max=3,
        ),
        DistributionCondition.FILLER_APPORTIONMENT_NOT_DETERMINED.value: _explain(
            field=86,
            design=90,
            fillers=(2, 4),
            cabinets=(30, 36, 18),
            types=("double_door", "equipment", "double_door"),
            filler_min=1,
            filler_max=4,
        ),
        DistributionCondition.RUN_SHAPE_UNSUPPORTED.value: _explain(
            field=82,
            design=90,
            fillers=(3, 3),
            cabinets=(24, 36, 24),
            types=("double_door", "equipment", "appliance"),
            filler_min=2,
            filler_max=3,
        ),
    }

    said = cases[condition.value]
    assert said.strip(), f"{condition.value} produced nothing"
    assert condition.value not in said, "the machine name reached the reviewer"


# ---------------------------------------------------------------------------
# #648: the slide-6 table — element | architectural | shop, corrected or retained
# ---------------------------------------------------------------------------


def _facts(
    *,
    field: int,
    design: int,
    fillers: tuple[int, ...],
    cabinets: tuple[int, ...],
    types: tuple[str, ...],
) -> dict[str, object]:
    result = cabinet_run_distribution(
        field_width=_inches(field),
        design_width=_inches(design),
        design_fillers=[_inches(value) for value in fillers],
        proposed_fillers=[_inches(value) for value in fillers],
        design_cabinets=[_inches(value) for value in cabinets],
        proposed_cabinets=[_inches(value) for value in cabinets],
        cabinet_type=list(types),
        filler_min=_inches(2),
        filler_max=_inches(3),
        **WIDE_BOUNDS,  # type: ignore[arg-type]
    )
    return dict(result.intermediates)


def test_the_table_reproduces_slide_six_for_the_first_worked_example() -> None:
    """3|24|36|24|3 = 90 becomes 2|21|36|21|2 = 82; only the equipment cabinet is retained."""
    facts = _facts(
        field=82,
        design=90,
        fillers=(3, 3),
        cabinets=(24, 36, 24),
        types=("double_door", "equipment", "double_door"),
    )

    assert distribution_table(facts) == (
        ("Left filler", '3"', '2"', "corrected"),
        ("Cabinet 1 (double door)", '24"', '21"', "corrected"),
        ("Cabinet 2 (equipment, fixed width)", '36"', '36"', "retained"),
        ("Cabinet 3 (double door)", '24"', '21"', "corrected"),
        ("Right filler", '3"', '2"', "corrected"),
        ("Wall to wall", '90"', '82"', "corrected"),
    )


def test_the_table_reproduces_the_second_worked_example() -> None:
    facts = _facts(
        field=96,
        design=88,
        fillers=(2, 2),
        cabinets=(24, 36, 24),
        types=("single_door", "sink_cabinet", "drawer"),
    )

    assert [row[2] for row in distribution_table(facts)] == ['3"', '27"', '36"', '27"', '3"', '96"']


def test_cabinets_the_fillers_cover_are_retained() -> None:
    """Slide 12, outcome 3: only the fillers change; every cabinet keeps its green check."""
    facts = _facts(
        field=88,
        design=90,
        fillers=(3, 3),
        cabinets=(24, 36, 24),
        types=("double_door", "equipment", "double_door"),
    )

    statuses = [row[3] for row in distribution_table(facts)]
    assert statuses == ["corrected", "retained", "retained", "retained", "corrected", "corrected"]


def test_no_table_when_the_check_proposes_nothing() -> None:
    """An RFI or a flagged split has no corrected layout, so no table pretends to show one."""
    unresolvable = _facts(
        field=40,
        design=90,
        fillers=(3, 3),
        cabinets=(24, 36, 24),
        types=("double_door", "equipment", "double_door"),
    )

    assert unresolvable["condition"] == DistributionCondition.CANNOT_BE_RESOLVED.value
    assert distribution_table(unresolvable) == ()
    assert explain_distribution_with_table(unresolvable) == explain_distribution(unresolvable)
    assert distribution_table({}) == ()


def test_the_findings_explanation_carries_the_derivation_then_the_table() -> None:
    facts = _facts(
        field=82,
        design=90,
        fillers=(3, 3),
        cabinets=(24, 36, 24),
        types=("double_door", "equipment", "double_door"),
    )

    said = explain_distribution_with_table(facts)

    assert said.startswith(explain_distribution(facts))
    assert "Element | Architectural | Shop drawing | Corrected or retained" in said
    assert 'Cabinet 2 (equipment, fixed width) | 36" | 36" | retained' in said
