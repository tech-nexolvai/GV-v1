"""Adversarial properties for the typed verdict boundary.

The finite matrices below are deliberately exhaustive over the safety states that
matter rather than randomly sampled: presence, evidence qualification, authored
unit, and exact comparison boundaries. This keeps failures reproducible without a
new test dependency.

Source: AGENTS.md section 6; docs/DESIGN.md sections 3.7 and 4.
Verification: tests/verdict/test_properties.py.
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

import pytest

from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
    Tolerance,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, MixedUnitError, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations.aggregate import (
    AGGREGATE_SPECS,
    all_within_tolerance,
    sum_within_tolerance,
)
from verdict.operations.aggregate import (
    sum as sum_measurements,
)
from verdict.operations.alignment import ALIGNMENT_SPECS
from verdict.operations.pairwise import PAIRWISE_SPECS, pairwise_within_tolerance
from verdict.operations.scalar import (
    SCALAR_SPECS,
    between,
    maximum,
    minimum,
    within_tolerance,
)
from verdict.outcomes import Outcome, Severity
from verdict.registry import (
    REGISTRY,
    OperationResult,
    OperationSpec,
    RuleAuthoringError,
    register,
    validate_operands,
)

ALL_SPECS = (*SCALAR_SPECS, *AGGREGATE_SPECS, *PAIRWISE_SPECS, *ALIGNMENT_SPECS)
UNQUALIFIED = (
    EvidenceStatus.RAW_CANDIDATE,
    EvidenceStatus.CONFLICTING,
    EvidenceStatus.REJECTED,
)
QUALIFIED = (EvidenceStatus.CORROBORATED, EvidenceStatus.HUMAN_CONFIRMED)


def mm(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, str(value))


def inch(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, str(value))


def _operand(
    name: str,
    *,
    value: Measurement | str | None = None,
    status: EvidenceStatus,
) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,
        status=status,
        source="SHOP",
        evidence_ref=f"p1:{name}",
    )


def _rule_for(spec: OperationSpec) -> Rule:
    """Build a rule that reaches any registered signature after the safety gates."""

    argument_names = tuple(name for name in spec.operands if name != "tolerance")
    inputs = {
        name: InputSelector(
            source=OperandSource.SHOP,
            semantic_type=SemanticType.CABINET_WIDTH,
        )
        for name in argument_names
    }
    tolerance = Tolerance(value="1", unit=Unit.MM) if "tolerance" in spec.operands else None
    return Rule(
        id=f"PROPERTY-{spec.name.upper()}",
        version="1.0.0",
        product_type=ProductType.CABINET,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs=inputs,
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(
            type=spec.name,
            operands={name: name for name in argument_names},
            tolerance=tolerance,
        ),
    )


@pytest.fixture(autouse=True)
def _restore_registry() -> object:
    """Keep these adversarial registrations isolated from the rest of the suite."""

    previous = dict(REGISTRY)
    REGISTRY.clear()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.name)
@pytest.mark.parametrize("status", UNQUALIFIED, ids=lambda status: status.value)
@pytest.mark.parametrize("missing", [False, True], ids=["present", "missing"])
def test_no_operation_can_pass_an_unqualified_input(
    spec: OperationSpec,
    status: EvidenceStatus,
    missing: bool,
) -> None:
    """Every unqualified/presence combination abstains before operation code runs."""

    called = False

    def would_pass(**_: object) -> OperationResult:
        nonlocal called
        called = True
        return OperationResult(Outcome.PASS, None, (), "unsafe pass", None)

    guarded_spec = OperationSpec(
        spec.name,
        spec.version,
        spec.operands,
        would_pass,
        spec.kind,
    )
    register(guarded_spec)
    rule = _rule_for(guarded_spec)
    for unsafe_name in rule.inputs:
        operands = {
            name: _operand(
                name,
                value=None if missing and name == unsafe_name else mm(1),
                status=status if name == unsafe_name else EvidenceStatus.CORROBORATED,
            )
            for name in rule.inputs
        }

        finding = execute(publish(rule), operands)

        assert finding.outcome is Outcome.REVIEW_REQUIRED
        assert finding.outcome is not Outcome.PASS
        assert called is False


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.name)
@pytest.mark.parametrize("status", QUALIFIED, ids=lambda status: status.value)
def test_every_operation_has_an_engine_owned_not_found_path(
    spec: OperationSpec,
    status: EvidenceStatus,
) -> None:
    """A missing qualified operand becomes NOT_FOUND before any operation executes."""

    called = False

    def would_pass(**_: object) -> OperationResult:
        nonlocal called
        called = True
        return OperationResult(Outcome.PASS, None, (), "unsafe pass", None)

    guarded_spec = OperationSpec(
        spec.name,
        spec.version,
        spec.operands,
        would_pass,
        spec.kind,
    )
    register(guarded_spec)
    rule = _rule_for(guarded_spec)
    for missing_name in rule.inputs:
        operands = {
            name: _operand(
                name,
                value=None if name == missing_name else mm(1),
                status=status,
            )
            for name in rule.inputs
        }

        finding = execute(publish(rule), operands)

        assert finding.outcome is Outcome.NOT_FOUND
        assert finding.outcome is not Outcome.PASS
        assert called is False


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.name)
def test_registry_rejects_each_operation_when_one_declared_operand_is_missing(
    spec: OperationSpec,
) -> None:
    """Removing any one argument is always a loud authoring error."""

    valid_shapes: dict[str, object] = {
        name: [mm(1)] if arity.value == "list" else mm(1) for name, arity in spec.operands.items()
    }
    for missing_name in spec.operands:
        supplied = {name: value for name, value in valid_shapes.items() if name != missing_name}
        with pytest.raises(RuleAuthoringError, match="missing"):
            validate_operands(spec, supplied)


@pytest.mark.parametrize(
    ("actual", "expected_outcome"),
    [
        (Fraction(98), Outcome.FAIL),
        (Fraction(99), Outcome.PASS),
        (Fraction(100), Outcome.PASS),
        (Fraction(101), Outcome.PASS),
        (Fraction(102), Outcome.FAIL),
    ],
)
def test_within_tolerance_is_exact_on_and_beyond_both_boundaries(
    actual: Fraction,
    expected_outcome: Outcome,
) -> None:
    assert within_tolerance(actual=mm(actual), expected=mm(100), tolerance=mm(1)).outcome is (
        expected_outcome
    )


@pytest.mark.parametrize(
    ("operation", "value", "expected_outcome"),
    [
        (minimum, Fraction(99), Outcome.FAIL),
        (minimum, Fraction(100), Outcome.PASS),
        (minimum, Fraction(101), Outcome.PASS),
        (maximum, Fraction(99), Outcome.PASS),
        (maximum, Fraction(100), Outcome.PASS),
        (maximum, Fraction(101), Outcome.FAIL),
    ],
)
def test_minimum_and_maximum_are_exact_at_each_boundary(
    operation: Callable[..., OperationResult],
    value: Fraction,
    expected_outcome: Outcome,
) -> None:
    assert operation(x=mm(value), bound=mm(100)).outcome is expected_outcome


@pytest.mark.parametrize(
    ("value", "expected_outcome"),
    [
        (Fraction(9), Outcome.FAIL),
        (Fraction(10), Outcome.PASS),
        (Fraction(15), Outcome.PASS),
        (Fraction(20), Outcome.PASS),
        (Fraction(21), Outcome.FAIL),
    ],
)
def test_between_is_exact_on_and_outside_both_boundaries(
    value: Fraction,
    expected_outcome: Outcome,
) -> None:
    assert between(x=mm(value), lo=mm(10), hi=mm(20)).outcome is expected_outcome


@pytest.mark.parametrize("direction", [Fraction(-1), Fraction(1)], ids=["below", "above"])
@pytest.mark.parametrize("outside", [False, True], ids=["boundary", "beyond"])
def test_aggregate_comparisons_cover_both_exact_tolerance_sides(
    direction: Fraction,
    outside: bool,
) -> None:
    tolerance = Fraction(1)
    offset = direction * (tolerance + (Fraction(1, 16) if outside else 0))
    expected_outcome = Outcome.FAIL if outside else Outcome.PASS

    summed = sum_within_tolerance(
        target=mm(Fraction(20) + offset),
        addends=[mm(8), mm(12)],
        tolerance=mm(tolerance),
    )
    all_values = all_within_tolerance(
        values=[mm(Fraction(20) + offset)],
        expected=mm(20),
        tolerance=mm(tolerance),
    )
    paired = pairwise_within_tolerance(
        left={"CAB-1": mm(20)},
        right={"CAB-1": mm(Fraction(20) + offset)},
        tolerance=mm(tolerance),
    )

    assert {summed.outcome, all_values.outcome, paired.outcome} == {expected_outcome}


@pytest.mark.parametrize(
    "operation",
    [
        lambda: sum_measurements(values=[]),
        lambda: sum_within_tolerance(target=mm(0), addends=[], tolerance=mm(0)),
        lambda: all_within_tolerance(values=[], expected=mm(0), tolerance=mm(0)),
        lambda: pairwise_within_tolerance(left={}, right={}, tolerance=mm(0)),
    ],
    ids=["sum", "sum_within_tolerance", "all_within_tolerance", "pairwise"],
)
def test_required_empty_collections_never_pass(operation: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match="at least one"):
        operation()


@pytest.mark.parametrize(
    "operation",
    [
        lambda: within_tolerance(actual=mm(1), expected=inch(1), tolerance=mm(1)),
        lambda: sum_within_tolerance(target=mm(1), addends=[inch(1)], tolerance=mm(1)),
        lambda: all_within_tolerance(values=[mm(1)], expected=inch(1), tolerance=mm(1)),
        lambda: pairwise_within_tolerance(
            left={"CAB-1": mm(1)}, right={"CAB-1": inch(1)}, tolerance=mm(1)
        ),
    ],
    ids=["within_tolerance", "sum_within_tolerance", "all_within_tolerance", "pairwise"],
)
def test_mixed_authored_units_never_produce_pass(operation: Callable[[], object]) -> None:
    with pytest.raises(MixedUnitError):
        operation()


def test_pairwise_rejects_duplicate_prone_sequence_input() -> None:
    """Only mappings cross the pairing boundary, so duplicate keys cannot be paired silently."""

    duplicate_prone = [("CAB-1", mm(24)), ("CAB-1", mm(25))]
    with pytest.raises(RuleAuthoringError, match="mapping keyed by identifier"):
        pairwise_within_tolerance(  # type: ignore[arg-type]
            left=duplicate_prone,
            right={"CAB-1": mm(24)},
            tolerance=mm(1),
        )
