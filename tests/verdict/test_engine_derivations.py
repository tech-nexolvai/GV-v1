"""Issue #115: execute named derivations before the final verdict operation."""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from typing import cast

import pytest

from rules.derivations import Derivation
from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Parameter,
    Quantity,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations.aggregate import AGGREGATE_SPECS
from verdict.operations.scalar import SCALAR_SPECS
from verdict.outcomes import Outcome, Severity
from verdict.registry import (
    REGISTRY,
    Arity,
    OperationKind,
    OperationResult,
    OperationSpec,
    register,
)


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    for spec in (*SCALAR_SPECS, *AGGREGATE_SPECS):
        register(spec)
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _measurement(value: int | Fraction, raw_text: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, raw_text)


def _operand(name: str, value: Measurement | Fraction | str | None) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,
        status=EvidenceStatus.CORROBORATED,
        source="SHOP",
        evidence_ref=f"p1:{name}",
    )


def _selector() -> InputSelector:
    return InputSelector(
        source=OperandSource.SHOP,
        semantic_type=SemanticType.CABINET_WIDTH,
    )


def _rule(
    *,
    inputs: tuple[str, ...],
    derivations: tuple[Derivation, ...],
    operation: OperationRef,
    parameters: tuple[str, ...] = (),
    rule_id: str = "DERIVATION-EXECUTION",
) -> Rule:
    return Rule(
        id=rule_id,
        version="1.0.0",
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs={name: _selector() for name in inputs},
        parameters={name: Parameter() for name in parameters},
        derivations=derivations,
        applicability=GlobalApplicability(scope="global"),
        operation=operation,
    )


def _parameter(name: str, value: int) -> ResolvedParameter:
    return ResolvedParameter(
        name=name,
        value=ParameterValue(
            value=Quantity(value=Fraction(value), unit=Unit.MM),
            provenance=Provenance.COMPANY_STANDARD,
            set_by="GV",
            set_at=datetime(2026, 8, 14, tzinfo=UTC),
        ),
        layer=ParameterLayer.GLOBAL,
    )


def _trace_record(finding: Finding, name: str) -> dict[str, object]:
    trace = finding.trace
    assert trace is not None
    for intermediate_name, value in trace.intermediates:
        if intermediate_name == name:
            fields = cast(tuple[tuple[str, object], ...], value)
            return dict(fields)
    raise AssertionError(f"trace did not contain derivation {name!r}")


def test_derived_and_equivalent_direct_values_produce_the_same_verdict() -> None:
    derived_rule = _rule(
        inputs=("left", "right", "expected"),
        derivations=(
            Derivation(name="total", operation="sum", operands={"values": ("left", "right")}),
        ),
        operation=OperationRef(type="equals", operands={"actual": "total", "expected": "expected"}),
    )
    direct_rule = _rule(
        inputs=("total", "expected"),
        derivations=(),
        operation=OperationRef(type="equals", operands={"actual": "total", "expected": "expected"}),
        rule_id="DIRECT-EXECUTION",
    )
    derived = execute(
        publish(derived_rule),
        {
            "left": _operand("left", _measurement(40, "40")),
            "right": _operand("right", _measurement(60, "60")),
            "expected": _operand("expected", _measurement(100)),
        },
    )
    direct = execute(
        publish(direct_rule),
        {
            "total": _operand("total", _measurement(100)),
            "expected": _operand("expected", _measurement(100)),
        },
    )

    assert derived.outcome is direct.outcome is Outcome.PASS
    assert derived.reason == direct.reason


def test_a_second_derivation_consumes_the_first_by_named_operands() -> None:
    rule = _rule(
        inputs=("a", "b", "deduction", "expected"),
        derivations=(
            Derivation(name="gross", operation="sum", operands={"values": ("a", "b")}),
            Derivation(
                name="net",
                operation="difference_between",
                operands={"a": "gross", "b": "deduction"},
            ),
        ),
        operation=OperationRef(type="equals", operands={"actual": "net", "expected": "expected"}),
    )

    finding = execute(
        publish(rule),
        {
            "a": _operand("a", _measurement(70)),
            "b": _operand("b", _measurement(50)),
            "deduction": _operand("deduction", _measurement(20)),
            "expected": _operand("expected", _measurement(100)),
        },
    )

    assert finding.outcome is Outcome.PASS
    assert _trace_record(finding, "gross")["result"] == _measurement(120)
    net = _trace_record(finding, "net")
    assert net["inputs"] == (("a", _measurement(120)), ("b", _measurement(20)))
    assert net["result"] == _measurement(100)


def test_trace_records_name_operation_version_inputs_exact_result_and_rendering() -> None:
    rule = _rule(
        inputs=("one_third", "two_thirds", "expected"),
        derivations=(
            Derivation(
                name="whole",
                operation="sum",
                operands={"values": ("one_third", "two_thirds")},
            ),
        ),
        operation=OperationRef(type="equals", operands={"actual": "whole", "expected": "expected"}),
    )
    finding = execute(
        publish(rule),
        {
            "one_third": _operand("one_third", _measurement(Fraction(1, 3))),
            "two_thirds": _operand("two_thirds", _measurement(Fraction(2, 3))),
            "expected": _operand("expected", _measurement(1)),
        },
    )

    record = _trace_record(finding, "whole")
    assert record["operation"] == "sum"
    assert record["operation_version"] == "1.0.0"
    assert record["inputs"] == (
        ("values", (_measurement(Fraction(1, 3)), _measurement(Fraction(2, 3)))),
    )
    assert record["result"] == _measurement(1)
    assert record["rendering"] == "1/3 + 2/3 = 1 mm"


@pytest.mark.parametrize("outcome", [Outcome.NOT_FOUND, Outcome.REVIEW_REQUIRED])
def test_an_abstaining_derivation_stops_before_the_final_operation(outcome: Outcome) -> None:
    final_calls: list[object] = []

    def abstain(*, value: object) -> OperationResult:
        return OperationResult(outcome, None, (), f"could not derive from {value!r}", None)

    def final(*, actual: object, expected: object) -> OperationResult:
        final_calls.extend((actual, expected))
        return OperationResult(Outcome.PASS, None, (), "should not run", None)

    register(
        OperationSpec(
            "abstaining_derivation",
            "1.0.0",
            {"value": Arity.SCALAR},
            abstain,
            OperationKind.DERIVATION,
        )
    )
    register(
        OperationSpec(
            "final_spy",
            "1.0.0",
            {"actual": Arity.SCALAR, "expected": Arity.SCALAR},
            final,
        )
    )
    rule = _rule(
        inputs=("source", "expected"),
        derivations=(
            Derivation(
                name="unavailable",
                operation="abstaining_derivation",
                operands={"value": "source"},
            ),
        ),
        operation=OperationRef(
            type="final_spy", operands={"actual": "unavailable", "expected": "expected"}
        ),
    )

    finding = execute(
        publish(rule),
        {
            "source": _operand("source", _measurement(10)),
            "expected": _operand("expected", _measurement(10)),
        },
    )

    assert finding.outcome is outcome
    assert finding.trace is None
    assert "abstained" in finding.reason
    assert not final_calls


def test_a_missing_derivation_parameter_is_not_found_not_zero() -> None:
    rule = _rule(
        inputs=("width", "expected"),
        parameters=("offset",),
        derivations=(
            Derivation(
                name="adjusted",
                operation="difference_between",
                operands={"a": "width", "b": "offset"},
            ),
        ),
        operation=OperationRef(
            type="equals", operands={"actual": "adjusted", "expected": "expected"}
        ),
    )

    finding = execute(
        publish(rule),
        {
            "width": _operand("width", _measurement(100)),
            "expected": _operand("expected", _measurement(100)),
        },
        parameters={},
    )

    assert finding.outcome is Outcome.NOT_FOUND
    assert "not zero" in finding.reason
    assert finding.trace is None


def test_a_verdict_operation_cannot_be_used_as_a_derivation_or_run_arithmetic() -> None:
    calls: list[object] = []

    def verdict_only(*, value: object) -> OperationResult:
        calls.append(value)
        return OperationResult(Outcome.PASS, None, (), "passed", None)

    register(OperationSpec("verdict_only", "1.0.0", {"value": Arity.SCALAR}, verdict_only))
    rule = _rule(
        inputs=("source", "expected"),
        derivations=(
            Derivation(
                name="wrong_kind",
                operation="verdict_only",
                operands={"value": "source"},
            ),
        ),
        operation=OperationRef(
            type="equals", operands={"actual": "wrong_kind", "expected": "expected"}
        ),
    )

    with pytest.raises(ValueError, match="not registered as a derivation"):
        execute(
            publish(rule),
            {
                "source": _operand("source", _measurement(10)),
                "expected": _operand("expected", _measurement(10)),
            },
        )
    assert not calls, "operation kind must be rejected before arithmetic"


@pytest.mark.parametrize(
    ("rule_id", "input_values", "parameter_values", "terms", "expected"),
    [
        (
            "CT004",
            {"ct011": 500, "ct012": 400, "ct013": 50},
            {"cab_side_thk": 25},
            ("cab_side_thk", "ct011", "ct012", "ct013", "cab_side_thk"),
            1000,
        ),
        (
            "CT010",
            {"ct007": 500, "ct008": 400, "ct009": 50},
            {"countertop_overhang": 25, "backsplash_thickness": 25},
            (
                "countertop_overhang",
                "ct007",
                "ct008",
                "ct009",
                "backsplash_thickness",
            ),
            1000,
        ),
    ],
)
def test_real_ct_formulas_execute_end_to_end(
    rule_id: str,
    input_values: dict[str, int],
    parameter_values: dict[str, int],
    terms: tuple[str, ...],
    expected: int,
) -> None:
    derived_name = rule_id.lower()
    rule = _rule(
        inputs=(*input_values, "expected"),
        parameters=tuple(parameter_values),
        derivations=(Derivation(name=derived_name, operation="sum", operands={"values": terms}),),
        operation=OperationRef(
            type="equals", operands={"actual": derived_name, "expected": "expected"}
        ),
        rule_id=rule_id,
    )
    operands = {name: _operand(name, _measurement(value)) for name, value in input_values.items()}
    operands["expected"] = _operand("expected", _measurement(expected))
    parameters = {name: _parameter(name, value) for name, value in parameter_values.items()}

    finding = execute(publish(rule), operands, parameters)

    assert finding.outcome is Outcome.PASS
    assert _trace_record(finding, derived_name)["result"] == _measurement(expected)
