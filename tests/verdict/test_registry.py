"""Verification for issue #47: typed operation registry and calculation traces."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from fractions import Fraction

import pytest

from units.measurement import Measurement, Unit
from verdict.outcomes import Outcome
from verdict.registry import (
    REGISTRY,
    Arity,
    OperationResult,
    OperationSpec,
    RuleAuthoringError,
    UnknownOperationError,
    register,
    resolve,
    validate_operands,
)
from verdict.trace import CalculationTrace, TracedOperand


@pytest.fixture(autouse=True)
def _empty_registry() -> None:
    """Keep the module-level registry deterministic between tests."""

    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _result() -> OperationResult:
    actual = Measurement(Fraction(6012), Unit.MM, "6012")
    expected = Measurement(Fraction(6012), Unit.MM, "6012")
    tolerance = Measurement(Fraction(1), Unit.MM, "1")
    trace = CalculationTrace(
        operation="example",
        operands=(
            TracedOperand("actual", actual, "SHOP", "shop-v1/page-4/region-2"),
            TracedOperand("expected", expected, "ARCH", "arch-v3/page-7/region-8"),
        ),
        intermediates=(("delta", Measurement(Fraction(0), Unit.MM, None)),),
        comparison="|6012 - 6012| = 0 <= 1",
        tolerance=tolerance,
        arithmetic_unit=Unit.MM,
        outcome=Outcome.PASS,
        engine_version="1.0.0",
        operation_version="1.0.0",
    )
    return OperationResult(Outcome.PASS, Measurement(Fraction(0), Unit.MM, None), trace)


def _operation(**_: object) -> OperationResult:
    return _result()


def _spec(**overrides: object) -> OperationSpec:
    values: dict[str, object] = {
        "name": "example",
        "version": "1.0.0",
        "operands": {"actual": Arity.SCALAR, "addends": Arity.LIST},
        "fn": _operation,
    }
    values.update(overrides)
    return OperationSpec(**values)  # type: ignore[arg-type]


def test_register_and_resolve_by_exact_name() -> None:
    """A rule-facing name resolves only to its reviewed operation specification."""

    spec = _spec()
    register(spec)

    assert resolve("example") is spec


def test_unknown_operation_is_a_loud_error() -> None:
    """An unknown name never falls back to another operation."""

    with pytest.raises(UnknownOperationError, match="unknown operation"):
        resolve("not_registered")


def test_duplicate_registration_cannot_replace_reviewed_code() -> None:
    """Import order cannot silently choose which implementation wins."""

    original = _spec()
    register(original)

    with pytest.raises(RuleAuthoringError, match="already registered"):
        register(_spec(version="2.0.0"))

    assert resolve("example") is original


@pytest.mark.parametrize(
    "operands",
    [
        {"actual": Measurement(Fraction(1), Unit.MM, "1")},
        {
            "actual": Measurement(Fraction(1), Unit.MM, "1"),
            "addends": [Measurement(Fraction(2), Unit.MM, "2")],
            "extra": Measurement(Fraction(3), Unit.MM, "3"),
        },
    ],
)
def test_missing_or_extra_operands_are_rule_authoring_errors(
    operands: dict[str, object],
) -> None:
    """A malformed invocation cannot discard or invent an operation input."""

    with pytest.raises(RuleAuthoringError, match="signature"):
        validate_operands(_spec(), operands)


@pytest.mark.parametrize(
    "operands",
    [
        {
            "actual": [Measurement(Fraction(1), Unit.MM, "1")],
            "addends": [Measurement(Fraction(2), Unit.MM, "2")],
        },
        {
            "actual": Measurement(Fraction(1), Unit.MM, "1"),
            "addends": Measurement(Fraction(2), Unit.MM, "2"),
        },
    ],
)
def test_wrong_arity_is_rejected_before_operation_is_called(
    operands: dict[str, object],
) -> None:
    """Structural errors fail validation without running registered arithmetic."""

    called = False

    def operation(**_: object) -> OperationResult:
        nonlocal called
        called = True
        return _result()

    spec = _spec(fn=operation)

    with pytest.raises(RuleAuthoringError, match="arity"):
        validate_operands(spec, operands)

    assert called is False


def test_list_arity_accepts_sequences_and_identifier_keyed_mappings() -> None:
    """Aggregate lists and pairwise mappings share the designed multi-value arity."""

    actual = Measurement(Fraction(1), Unit.MM, "1")
    spec = _spec()

    validate_operands(spec, {"actual": actual, "addends": [actual]})
    validate_operands(spec, {"actual": actual, "addends": {"CAB-1": actual}})


def test_trace_contains_every_value_needed_for_manual_reconstruction() -> None:
    """The result records provenance, arithmetic, outcome, and both versions."""

    result = _result()
    trace = result.trace

    assert result.outcome is Outcome.PASS
    assert result.delta == Measurement(Fraction(0), Unit.MM, None)
    assert [operand.name for operand in trace.operands] == ["actual", "expected"]
    assert [operand.source for operand in trace.operands] == ["SHOP", "ARCH"]
    assert all(operand.evidence_ref is not None for operand in trace.operands)
    assert trace.intermediates == (("delta", Measurement(Fraction(0), Unit.MM, None)),)
    assert trace.comparison == "|6012 - 6012| = 0 <= 1"
    assert trace.tolerance == Measurement(Fraction(1), Unit.MM, "1")
    assert trace.arithmetic_unit is Unit.MM
    assert trace.outcome is Outcome.PASS
    assert trace.engine_version == "1.0.0"
    assert trace.operation_version == "1.0.0"


def test_trace_and_operation_spec_are_immutable() -> None:
    """Reviewed metadata and emitted audit records cannot change afterward."""

    spec = _spec()
    trace = _result().trace

    with pytest.raises(FrozenInstanceError):
        trace.outcome = Outcome.FAIL  # type: ignore[misc]
    with pytest.raises(TypeError):
        spec.operands["actual"] = Arity.LIST  # type: ignore[index]


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"version": ""},
        {"operands": {}},
        {"operands": {"actual": "scalar"}},
        {"fn": None},
    ],
)
def test_invalid_operation_specs_are_rejected(overrides: dict[str, object]) -> None:
    """Invalid registry metadata is rejected before it can be published."""

    with pytest.raises(RuleAuthoringError):
        _spec(**overrides)
