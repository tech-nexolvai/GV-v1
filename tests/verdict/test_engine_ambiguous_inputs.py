"""An input found but not shown to belong together makes a check return the rule's `on_ambiguous` (#826).

Verification for: `verdict/engine.py:execute`, step 1b. The caller says *which* inputs and *why*; the
rule says what ambiguity means, and the schema allows it only to abstain — so this step can turn a
verdict into an abstention and never the other way.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations.scalar import SCALAR_SPECS
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY, register

WHY = "The labelled readings for this check are on 2 different drawings."


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    for spec in SCALAR_SPECS:
        register(spec)
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _rule(on_ambiguous: Outcome = Outcome.REVIEW_REQUIRED) -> Rule:
    selector = InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CABINET_WIDTH)
    return Rule(
        id="AMBIGUITY",
        version="1.0.0",
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.INCH,
        inputs={"actual": selector, "expected": selector},
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(
            type="equals", operands={"actual": "actual", "expected": "expected"}
        ),
        on_ambiguous=on_ambiguous,
    )


def _operand(name: str, inches: int) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=Measurement(Fraction(inches), Unit.INCH, None),
        status=EvidenceStatus.HUMAN_CONFIRMED,
        source="SHOP",
        evidence_ref=f"p1:{name}",
    )


def test_an_ambiguous_input_returns_the_rules_on_ambiguous_with_why() -> None:
    """Outcome: REVIEW_REQUIRED naming the reason and the input — not NOT_FOUND, which would tell the
    reviewer to go and read a number they already labelled."""
    finding = execute(
        publish(_rule()), {"actual": _operand("actual", 45)}, ambiguous={"expected": WHY}
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert WHY in finding.reason and "expected" in finding.reason
    assert finding.trace is None, "nothing was calculated, so nothing may look calculated"


def test_the_rule_decides_what_ambiguity_means() -> None:
    """A rule authored `on_ambiguous: NOT_FOUND` gets NOT_FOUND. The engine reads the rule."""
    finding = execute(
        publish(_rule(on_ambiguous=Outcome.NOT_FOUND)),
        {"actual": _operand("actual", 45)},
        ambiguous={"expected": WHY},
    )

    assert finding.outcome is Outcome.NOT_FOUND


def test_a_value_supplied_for_the_input_is_the_answer_not_an_ambiguity() -> None:
    """The reviewer typed it for this check, so the check runs on it. Outcome: an ordinary FAIL."""
    finding = execute(
        publish(_rule()),
        {"actual": _operand("actual", 45), "expected": _operand("expected", 60)},
        ambiguous={"expected": WHY},
    )

    assert finding.outcome is Outcome.FAIL


def test_a_name_that_is_not_one_of_the_rules_inputs_is_ignored() -> None:
    """Another rule's ambiguity is not this one's."""
    finding = execute(
        publish(_rule()),
        {"actual": _operand("actual", 45), "expected": _operand("expected", 45)},
        ambiguous={"countertop_width": WHY},
    )

    assert finding.outcome is Outcome.PASS


@pytest.mark.parametrize("supplied", [45, 60])
def test_ambiguity_never_turns_into_a_verdict(supplied: int) -> None:
    """Whatever the other operand says, a withheld input abstains: never PASS, never FAIL."""
    finding = execute(
        publish(_rule()), {"actual": _operand("actual", supplied)}, ambiguous={"expected": WHY}
    )

    assert finding.outcome not in (Outcome.PASS, Outcome.FAIL)
