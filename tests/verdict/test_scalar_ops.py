"""Verification for issue #48: exact scalar verdict operations."""

from __future__ import annotations

from enum import StrEnum
from fractions import Fraction

import pytest

from units.measurement import Measurement, MixedUnitError, Unit
from verdict.operations.scalar import (
    SCALAR_SPECS,
    between,
    conditional_required,
    contains,
    difference_between,
    equals,
    exists,
    maximum,
    minimum,
    one_of,
    within_tolerance,
)
from verdict.outcomes import Outcome
from verdict.registry import DerivationResult, OperationKind, RuleAuthoringError


class Finish(StrEnum):
    OAK = "oak"
    WALNUT = "walnut"


def mm(value: int | Fraction, raw: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, raw)


def inch(value: int | Fraction, raw: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, raw)


@pytest.mark.parametrize("value", [0, Fraction(0), mm(0), "oak", (0,), [0]])
def test_exists_accepts_zero_and_nonempty_values(value: object) -> None:
    assert exists(value=value).outcome is Outcome.PASS


@pytest.mark.parametrize("value", [None, "", (), [], {}])
def test_exists_rejects_absent_values(value: object | None) -> None:
    result = exists(value=value)
    assert result.outcome is Outcome.FAIL
    assert result.comparison == "value is absent"


def test_exists_rejects_float() -> None:
    with pytest.raises(RuleAuthoringError, match="float"):
        exists(value=0.0)


@pytest.mark.parametrize(
    ("actual", "expected", "outcome"),
    [
        ("oak", "oak", Outcome.PASS),
        ("oak", "Oak", Outcome.FAIL),
        (Finish.OAK, Finish.OAK, Outcome.PASS),
        (1, 1, Outcome.PASS),
        (Fraction(1, 3), Fraction(1, 3), Outcome.PASS),
        (mm(25), mm(25), Outcome.PASS),
        (mm(25), mm(26), Outcome.FAIL),
    ],
)
def test_equals_is_exact_and_same_typed(actual: object, expected: object, outcome: Outcome) -> None:
    assert equals(actual=actual, expected=expected).outcome is outcome  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("actual", "expected"),
    [("1", 1), (1, Fraction(1)), (1.0, 1.0), (True, True), (Finish.OAK, "oak")],
)
def test_equals_rejects_cross_type_or_unsupported_values(actual: object, expected: object) -> None:
    with pytest.raises(RuleAuthoringError):
        equals(actual=actual, expected=expected)  # type: ignore[arg-type]


def test_equals_rejects_mixed_authored_units() -> None:
    with pytest.raises(MixedUnitError):
        equals(actual=mm(25), expected=inch(1))


@pytest.mark.parametrize(
    ("actual", "outcome"),
    [(mm(98), Outcome.PASS), (mm(102), Outcome.PASS), (mm(103), Outcome.FAIL)],
)
def test_within_tolerance_is_inclusive(actual: Measurement, outcome: Outcome) -> None:
    result = within_tolerance(actual=actual, expected=mm(100), tolerance=mm(2))
    assert result.outcome is outcome
    assert result.delta == mm(abs(actual.exact - 100))
    assert result.tolerance == mm(2)


def test_within_tolerance_rejects_negative_tolerance() -> None:
    with pytest.raises(RuleAuthoringError, match="negative"):
        within_tolerance(actual=mm(1), expected=mm(1), tolerance=mm(-1))


@pytest.mark.parametrize("function", [within_tolerance])
def test_numeric_comparison_rejects_mixed_units(function: object) -> None:
    with pytest.raises(MixedUnitError):
        within_tolerance(actual=mm(25), expected=inch(1), tolerance=mm(1))


@pytest.mark.parametrize(
    ("value", "outcome"),
    [(mm(9), Outcome.FAIL), (mm(10), Outcome.PASS), (mm(11), Outcome.PASS)],
)
def test_minimum_is_inclusive(value: Measurement, outcome: Outcome) -> None:
    assert minimum(x=value, bound=mm(10)).outcome is outcome


@pytest.mark.parametrize(
    ("value", "outcome"),
    [(mm(9), Outcome.PASS), (mm(10), Outcome.PASS), (mm(11), Outcome.FAIL)],
)
def test_maximum_is_inclusive(value: Measurement, outcome: Outcome) -> None:
    assert maximum(x=value, bound=mm(10)).outcome is outcome


@pytest.mark.parametrize(
    ("value", "outcome"),
    [
        (mm(9), Outcome.FAIL),
        (mm(10), Outcome.PASS),
        (mm(15), Outcome.PASS),
        (mm(20), Outcome.PASS),
        (mm(21), Outcome.FAIL),
    ],
)
def test_between_is_inclusive_at_both_bounds(value: Measurement, outcome: Outcome) -> None:
    assert between(x=value, lo=mm(10), hi=mm(20)).outcome is outcome


def test_between_rejects_reversed_bounds() -> None:
    with pytest.raises(RuleAuthoringError, match="lower bound"):
        between(x=mm(15), lo=mm(20), hi=mm(10))


def test_one_of_uses_exact_membership() -> None:
    assert one_of(x="oak", set=("oak", "walnut")).outcome is Outcome.PASS
    assert one_of(x="OAK", set=("oak", "walnut")).outcome is Outcome.FAIL


def test_one_of_rejects_empty_or_cross_type_allowed_values() -> None:
    with pytest.raises(RuleAuthoringError, match="empty"):
        one_of(x="oak", set=())
    with pytest.raises(RuleAuthoringError, match="same type"):
        one_of(x="1", set=("2", 1))  # type: ignore[arg-type]


def test_contains_is_literal_case_sensitive_and_does_not_trim() -> None:
    assert contains(text="finish PL-02 oak", substr="PL-02").outcome is Outcome.PASS
    assert contains(text="finish PL-02 oak", substr="pl-02").outcome is Outcome.FAIL
    assert contains(text="PL-02", substr=" PL-02 ").outcome is Outcome.FAIL


def test_contains_rejects_none_instead_of_deciding_missing_policy() -> None:
    with pytest.raises(TypeError, match="resolved"):
        contains(text=None, substr="oak")  # type: ignore[arg-type]


def test_difference_between_returns_exact_derived_measurement() -> None:
    result = difference_between(a=mm(Fraction(7, 2)), b=mm(Fraction(5, 4)))
    assert isinstance(result, DerivationResult)
    assert result.value == mm(Fraction(9, 4))
    assert not hasattr(result, "outcome")


def test_difference_between_supports_exact_nonmeasurement_numbers() -> None:
    assert difference_between(a=5, b=2).value == 3
    assert difference_between(a=Fraction(5, 2), b=Fraction(1, 2)).value == Fraction(2)


def test_difference_between_rejects_float_cross_type_and_mixed_units() -> None:
    with pytest.raises(RuleAuthoringError):
        difference_between(a=1.0, b=0.5)  # type: ignore[arg-type]
    with pytest.raises(RuleAuthoringError, match="same type"):
        difference_between(a=1, b=Fraction(1))
    with pytest.raises(MixedUnitError):
        difference_between(a=mm(25), b=inch(1))


@pytest.mark.parametrize("value", ["oak", 0, mm(0)])
def test_conditional_required_true_and_present_passes(value: object) -> None:
    result = conditional_required(when=True, value=value)
    assert result.outcome is Outcome.PASS
    assert result.intermediates == (("requirement_exercised", True),)


@pytest.mark.parametrize("value", [None, "", (), []])
def test_conditional_required_true_and_absent_is_not_found(value: object | None) -> None:
    result = conditional_required(when=True, value=value)
    assert result.outcome is Outcome.NOT_FOUND
    assert result.intermediates == (("requirement_exercised", True),)


@pytest.mark.parametrize("value", [None, "", "oak"])
def test_conditional_required_false_passes_and_records_not_exercised(
    value: object | None,
) -> None:
    result = conditional_required(when=False, value=value)
    assert result.outcome is Outcome.PASS
    assert result.intermediates == (("requirement_exercised", False),)
    assert "not exercised" in result.comparison


def test_conditional_required_rejects_non_boolean_condition() -> None:
    with pytest.raises(RuleAuthoringError, match="bool"):
        conditional_required(when=1, value="oak")  # type: ignore[arg-type]


def test_all_eleven_operations_have_versioned_registry_specs() -> None:
    assert {spec.name for spec in SCALAR_SPECS} == {
        "exists",
        "equals",
        "within_tolerance",
        "minimum",
        "maximum",
        "between",
        "one_of",
        "contains",
        "difference_between",
        "scale",
        "conditional_required",
    }
    assert all(spec.version == "1.0.0" for spec in SCALAR_SPECS)
    difference = next(spec for spec in SCALAR_SPECS if spec.name == "difference_between")
    scale_spec = next(spec for spec in SCALAR_SPECS if spec.name == "scale")
    assert difference.kind is OperationKind.DERIVATION
    assert scale_spec.kind is OperationKind.DERIVATION
