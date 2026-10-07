"""Which operations care what order their lists arrive in, proved by reordering them (#794, #833).

Verification for: `verdict.operations.POSITION_SENSITIVE_OPERATIONS`.

The evidence path hands a many-valued input over in the order its readings were labelled, and that
order says nothing about which cabinet is which. `workflow/evidence_operands.py` therefore gives an
operation in the declaration nothing from evidence. The declaration is only as good as its
membership: `cabinet_run_distribution` was missing from it, and a vendor who swapped two cabinets
passed the filler check (#833).

So every operation with a list operand is placed here on one side or the other, with a case that
shows it. A position-sensitive one changes its answer when one of its lists is reversed; every other
one gives the same answer with all of its lists reversed. **The drift guard** is the first test: an
operation with a list operand on neither side fails it.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction

import pytest

from units.measurement import Measurement, Unit
from verdict.operations import POSITION_SENSITIVE_OPERATIONS, declared_specs
from verdict.outcomes import Outcome
from verdict.registry import (
    Arity,
    DerivationResult,
    OperationResult,
    OperationSpec,
    validate_operands,
)

SPECS: Mapping[str, OperationSpec] = {spec.name: spec for spec in declared_specs()}


def _inch(value: int) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, None)


#: The #833 run, as the cabinet check sees it: a 63" site that matches the drawing, a 1" and a 2"
#: filler, A = 24" (single door) and B = 36" (double door), and a shop drawing that agrees.
_RUN = {
    "field_width": _inch(63),
    "design_width": _inch(63),
    "design_fillers": (_inch(1), _inch(2)),
    "proposed_fillers": (_inch(1), _inch(2)),
    "design_cabinets": (_inch(24), _inch(36)),
    "proposed_cabinets": (_inch(24), _inch(36)),
    "cabinet_type": ("single_door", "double_door"),
    "single_door_cab_width_min": _inch(9),
    "single_door_cab_width_max": _inch(36),
    "double_door_cab_width_min": _inch(24),
    "double_door_cab_width_max": _inch(48),
    "drawer_cab_width_min": _inch(12),
    "drawer_cab_width_max": _inch(36),
    "filler_min": _inch(1),
    "filler_max": _inch(2),
}

#: Each position-sensitive operation, a case it passes, and the list whose reversal must turn the
#: PASS into a FAIL: the vendor's cabinets, B before A.
POSITION_SENSITIVE: dict[str, tuple[dict[str, object], str]] = {
    "pairwise_within_tolerance": (
        {"left": (_inch(24), _inch(36)), "right": (_inch(24), _inch(36)), "tolerance": _inch(0)},
        "right",
    ),
    "cabinet_run_distribution": (_RUN, "proposed_cabinets"),
}

#: Each other operation with a list operand, and a case whose answer must survive reversing every
#: list in it. No list is a palindrome, so every reversal really reorders it.
ORDER_INDIFFERENT: dict[str, dict[str, object]] = {
    "sum": {"values": (_inch(24), _inch(30), _inch(36))},
    "count": {"values": (_inch(24), _inch(30), _inch(36))},
    "count_equals": {"values": (_inch(24), _inch(30), _inch(36)), "n": 3},
    "sum_within_tolerance": {
        "target": _inch(90),
        "addends": (_inch(24), _inch(30), _inch(36)),
        "tolerance": _inch(0),
    },
    "all_within_tolerance": {
        "values": (_inch(24), _inch(25)),
        "expected": _inch(24),
        "tolerance": _inch(0),
    },
    "alignment": {
        "positions": (_inch(10), _inch(11), _inch(13)),
        "tolerance": _inch(2),
        "axis": "x",
    },
    "one_of": {"x": "drawer", "set": ("single_door", "drawer")},
    # A row is a sum, however it is stated: the pieces and the cabinets-plus-fillers reach the same
    # total in any order, so reading them in labelled order cannot change the width check (#991).
    "row_total": {
        "pieces": (_inch(2), _inch(30), _inch(36)),
        "cabinets": (_inch(30), _inch(36)),
        "fillers": (_inch(2), _inch(0)),
    },
    # Each filler is held to the same bounds and the pair to one total, so which side is which never
    # reaches the answer: the vendor's swapped fillers get the same one either way round.
    "filler_distribution": {
        "field_width": _inch(63),
        "design_width": _inch(63),
        "design_fillers": (_inch(1), _inch(2)),
        "proposed_fillers": (_inch(2), _inch(1)),
        "filler_min": _inch(1),
        "filler_max": _inch(2),
        "allow_asymmetric": 1,
    },
}


def _answer(result: OperationResult | DerivationResult) -> object:
    """What a check would act on: a verdict's outcome and delta, or a derivation's value."""
    if isinstance(result, DerivationResult):
        return result.value
    return result.outcome, result.delta


def _reversed(operands: Mapping[str, object], names: set[str]) -> dict[str, object]:
    reordered = dict(operands)
    for name in names:
        value = operands[name]
        assert isinstance(value, tuple) and value != value[::-1], name
        reordered[name] = value[::-1]
    return reordered


def _lists(spec: OperationSpec) -> set[str]:
    return {name for name, arity in spec.operands.items() if arity is Arity.LIST}


def test_every_operation_with_a_list_is_on_one_side_or_the_other() -> None:
    """**The drift guard.** An operation added with a list operand and placed on neither side fails
    here, so it cannot quietly take a run in the order its readings were labelled."""
    with_lists = {name for name, spec in SPECS.items() if _lists(spec)}

    assert with_lists, "no operation takes a list, so this test would pass vacuously"
    assert POSITION_SENSITIVE_OPERATIONS == set(POSITION_SENSITIVE)
    assert not POSITION_SENSITIVE.keys() & ORDER_INDIFFERENT.keys()
    assert with_lists == POSITION_SENSITIVE.keys() | ORDER_INDIFFERENT.keys()


@pytest.mark.parametrize("name", sorted(POSITION_SENSITIVE))
def test_a_position_sensitive_operation_fails_the_same_cabinets_in_another_order(
    name: str,
) -> None:
    """Outcome: PASS as given, FAIL with the vendor's two cabinets swapped. The same numbers, in
    another order, are another answer — which is why labelling order must never reach these."""
    spec = SPECS[name]
    operands, swapped = POSITION_SENSITIVE[name]
    reordered = _reversed(operands, {swapped})
    validate_operands(spec, operands)

    as_given = spec.fn(**operands)
    in_another_order = spec.fn(**reordered)

    assert isinstance(as_given, OperationResult) and isinstance(in_another_order, OperationResult)
    assert (as_given.outcome, in_another_order.outcome) == (Outcome.PASS, Outcome.FAIL)


@pytest.mark.parametrize("name", sorted(ORDER_INDIFFERENT))
def test_any_other_operation_gives_the_same_answer_with_its_lists_reversed(name: str) -> None:
    """Outcome: the same outcome and delta, or the same derived value, whichever way round its
    lists arrive — so the evidence path may hand these a run in labelling order."""
    spec = SPECS[name]
    operands = ORDER_INDIFFERENT[name]
    validate_operands(spec, operands)

    as_given = spec.fn(**operands)
    reversed_lists = spec.fn(**_reversed(operands, _lists(spec)))

    assert _answer(reversed_lists) == _answer(as_given)
