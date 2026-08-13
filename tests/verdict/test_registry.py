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
    OperationKind,
    OperationResult,
    OperationSpec,
    RuleAuthoringError,
    UnknownOperationError,
    register,
    resolve,
    validate_operands,
)


@pytest.fixture(autouse=True)
def _empty_registry() -> None:
    """Keep the module-level registry deterministic between tests."""

    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _result() -> OperationResult:
    tolerance = Measurement(Fraction(1), Unit.MM, "1")
    delta = Measurement(Fraction(0), Unit.MM, None)
    return OperationResult(
        outcome=Outcome.PASS,
        delta=delta,
        intermediates=(("delta", Measurement(Fraction(0), Unit.MM, None)),),
        comparison="|6012 - 6012| = 0 <= 1",
        tolerance=tolerance,
    )


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


def test_result_contains_calculation_facts_for_engine_trace() -> None:
    """An operation reports arithmetic facts without inventing evidence provenance."""

    result = _result()

    assert result.outcome is Outcome.PASS
    assert result.delta == Measurement(Fraction(0), Unit.MM, None)
    assert result.intermediates == (("delta", Measurement(Fraction(0), Unit.MM, None)),)
    assert result.comparison == "|6012 - 6012| = 0 <= 1"
    assert result.tolerance == Measurement(Fraction(1), Unit.MM, "1")
    assert not hasattr(result, "trace")


def test_trace_and_operation_spec_are_immutable() -> None:
    """Reviewed metadata and emitted audit records cannot change afterward."""

    spec = _spec()
    result = _result()

    with pytest.raises(FrozenInstanceError):
        result.outcome = Outcome.FAIL  # type: ignore[misc]
    with pytest.raises(TypeError):
        spec.operands["actual"] = Arity.LIST  # type: ignore[index]


def test_operation_kind_defaults_to_verdict() -> None:
    assert _spec().kind is OperationKind.VERDICT


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
