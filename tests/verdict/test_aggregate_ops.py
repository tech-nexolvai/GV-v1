"""Verification for issue #49: exact aggregate verdict operations."""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.measurement import Measurement, MixedUnitError, Unit
from verdict.operations.aggregate import (
    AGGREGATE_SPECS,
    all_within_tolerance,
    count,
    count_equals,
    sum,
    sum_within_tolerance,
)
from verdict.outcomes import Outcome
from verdict.registry import DerivationResult, OperationKind, RuleAuthoringError


def mm(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, str(value))


def inch(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, str(value))


DRAWING_CHAIN = (mm(51), mm(533), mm(457), mm(984))


def test_sum_closes_the_real_drawing_chain_exactly() -> None:
    result = sum(values=DRAWING_CHAIN)
    assert isinstance(result, DerivationResult)
    assert result.value == Measurement(Fraction(2025), Unit.MM, None)
    assert result.expression == "51 + 533 + 457 + 984 = 2025 mm"


def test_sum_records_every_addend_and_running_total_in_order() -> None:
    result = sum(values=DRAWING_CHAIN)
    assert result.intermediates == (
        ("addend[0]", mm(51)),
        ("running_total[0]", Measurement(Fraction(51), Unit.MM, None)),
        ("addend[1]", mm(533)),
        ("running_total[1]", Measurement(Fraction(584), Unit.MM, None)),
        ("addend[2]", mm(457)),
        ("running_total[2]", Measurement(Fraction(1041), Unit.MM, None)),
        ("addend[3]", mm(984)),
        ("running_total[3]", Measurement(Fraction(2025), Unit.MM, None)),
    )


def test_sum_supports_variable_length_lists() -> None:
    assert sum(values=(mm(1),)).value == Measurement(Fraction(1), Unit.MM, None)
    assert sum(values=(mm(1), mm(2), mm(3))).value == Measurement(Fraction(6), Unit.MM, None)


def test_sum_rejects_empty_list_instead_of_returning_zero() -> None:
    with pytest.raises(ValueError, match="empty sum is not zero"):
        sum(values=())


def test_sum_rejects_mixed_units_and_non_measurements() -> None:
    with pytest.raises(MixedUnitError):
        sum(values=(mm(25), inch(1)))
    with pytest.raises(RuleAuthoringError, match=r"values\[1\]"):
        sum(values=(mm(1), "2"))  # type: ignore[arg-type]


def test_count_returns_exact_length_and_accepts_empty_list() -> None:
    assert count(values=()).value == 0
    result = count(values=("CAB-1", "CAB-2", "CAB-3"))
    assert result.value == 3
    assert result.intermediates == (("count", 3),)


@pytest.mark.parametrize(
    ("values", "expected", "outcome"),
    [
        ((), 0, Outcome.PASS),
        (("CAB-1", "CAB-2"), 2, Outcome.PASS),
        (("CAB-1",), 2, Outcome.FAIL),
    ],
)
def test_count_equals_compares_exact_list_length(
    values: tuple[str, ...], expected: int, outcome: Outcome
) -> None:
    result = count_equals(values=values, n=expected)
    assert result.outcome is outcome
    assert result.intermediates == (("count", len(values)),)


@pytest.mark.parametrize("bad", [-1, True, Fraction(2)])
def test_count_equals_rejects_invalid_expected_count(bad: object) -> None:
    with pytest.raises(RuleAuthoringError, match="non-negative integer"):
        count_equals(values=(), n=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("target", "tolerance", "outcome"),
    [
        (mm(2025), mm(0), Outcome.PASS),
        (mm(2026), mm(1), Outcome.PASS),
        (mm(2027), mm(1), Outcome.FAIL),
    ],
)
def test_sum_within_tolerance_uses_inclusive_exact_boundary(
    target: Measurement, tolerance: Measurement, outcome: Outcome
) -> None:
    result = sum_within_tolerance(
        target=target,
        addends=DRAWING_CHAIN,
        tolerance=tolerance,
    )
    assert result.outcome is outcome
    assert result.tolerance == tolerance


def test_sum_within_tolerance_trace_facts_include_all_addends_and_total() -> None:
    result = sum_within_tolerance(
        target=mm(2025),
        addends=DRAWING_CHAIN,
        tolerance=mm(0),
    )
    assert [name for name, _ in result.intermediates] == [
        "addend[0]",
        "running_total[0]",
        "addend[1]",
        "running_total[1]",
        "addend[2]",
        "running_total[2]",
        "addend[3]",
        "running_total[3]",
        "sum",
        "absolute_difference",
    ]
    assert result.intermediates[-2][1] == Measurement(Fraction(2025), Unit.MM, None)
    assert result.delta == Measurement(Fraction(0), Unit.MM, None)


def test_sum_within_tolerance_rejects_empty_mixed_or_negative_inputs() -> None:
    with pytest.raises(ValueError, match="empty sum is not zero"):
        sum_within_tolerance(target=mm(0), addends=(), tolerance=mm(1))
    with pytest.raises(MixedUnitError):
        sum_within_tolerance(target=mm(25), addends=(inch(1),), tolerance=mm(1))
    with pytest.raises(RuleAuthoringError, match="negative"):
        sum_within_tolerance(target=mm(1), addends=(mm(1),), tolerance=mm(-1))


@pytest.mark.parametrize(
    ("values", "outcome", "maximum_delta"),
    [
        ((mm(98), mm(102), mm(100)), Outcome.PASS, mm(2)),
        ((mm(98), mm(103), mm(100)), Outcome.FAIL, mm(3)),
    ],
)
def test_all_within_tolerance_checks_every_value_inclusively(
    values: tuple[Measurement, ...], outcome: Outcome, maximum_delta: Measurement
) -> None:
    result = all_within_tolerance(values=values, expected=mm(100), tolerance=mm(2))
    assert result.outcome is outcome
    assert result.delta == Measurement(maximum_delta.exact, Unit.MM, None)
    assert len(result.intermediates) == len(values) * 2


def test_all_within_tolerance_rejects_empty_mixed_or_negative_inputs() -> None:
    with pytest.raises(ValueError, match="at least one"):
        all_within_tolerance(values=(), expected=mm(1), tolerance=mm(1))
    with pytest.raises(MixedUnitError):
        all_within_tolerance(values=(inch(1),), expected=mm(25), tolerance=mm(1))
    with pytest.raises(RuleAuthoringError, match="negative"):
        all_within_tolerance(values=(mm(1),), expected=mm(1), tolerance=mm(-1))


def test_all_five_operations_have_versioned_registry_specs() -> None:
    assert {spec.name for spec in AGGREGATE_SPECS} == {
        "sum",
        "count",
        "count_equals",
        "sum_within_tolerance",
        "all_within_tolerance",
    }
    assert all(spec.version == "1.0.0" for spec in AGGREGATE_SPECS)
    kinds = {spec.name: spec.kind for spec in AGGREGATE_SPECS}
    assert kinds["sum"] is OperationKind.DERIVATION
    assert kinds["count"] is OperationKind.DERIVATION
    assert kinds["count_equals"] is OperationKind.VERDICT
