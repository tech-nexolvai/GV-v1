"""CT-2 proves exact countertop depth and the derived back-clearance guard.

Source: issue #59; client facts Q2, Q5, Q6, Q12 and Q13.
Verification: ``rules/rulebook/ct_depth_001.yaml`` and
``rules/rulebook/ct_back_offset_min_001.yaml``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import cast

import pytest
import yaml

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.publication import is_production_ready, tolerances_of
from rules.schema import Quantity, Rule
from rules.semantic_types import SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"
DEPTH_RULE_PATH = RULEBOOK / "ct_depth_001.yaml"
BACK_OFFSET_RULE_PATH = RULEBOOK / "ct_back_offset_min_001.yaml"
WHEN = datetime(2026, 8, 22, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _load(path: Path) -> Rule:
    return Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _inch(value: int | Fraction, raw_text: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, raw_text)


def _operand(name: str, value: int | Fraction) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=_inch(value, str(value)),
        status=EvidenceStatus.CORROBORATED,
        source="SHOP",
        evidence_ref=f"shop:p1:{name}",
    )


def _parameter(name: str, value: int | Fraction) -> ResolvedParameter:
    return ResolvedParameter(
        name=name,
        value=ParameterValue(
            value=Quantity(value=value, unit=Unit.INCH),
            provenance=Provenance.GC_CLIENT,
            set_by="project reviewer",
            set_at=WHEN,
        ),
        layer=ParameterLayer.PROJECT,
    )


def _trace_derivation(finding: object, name: str) -> dict[str, object]:
    trace = finding.trace  # type: ignore[attr-defined]
    assert trace is not None
    for intermediate_name, fields in trace.intermediates:
        if intermediate_name == name:
            return dict(cast(tuple[tuple[str, object], ...], fields))
    raise AssertionError(f"trace did not contain derivation {name!r}")


def test_depth_rule_is_an_exact_critical_inch_check() -> None:
    """Input: depth-rule YAML. Outcome: one CRITICAL equality check with no tolerance."""
    rule = _load(DEPTH_RULE_PATH)

    assert rule.id == "CT-DEPTH-001"
    assert rule.severity is Severity.CRITICAL
    assert rule.arithmetic_unit is Unit.INCH
    assert rule.operation.type == "equals"
    assert rule.inputs["countertop_depth"].semantic_type is SemanticType.CT010
    assert tolerances_of(rule) == ()
    assert is_production_ready(rule)
    assert "field_cut" not in rule.parameters


def test_depth_passes_only_when_cabinet_and_overhang_sum_exactly() -> None:
    """Input: 24-inch cabinet + 1-inch overhang and CT010=25. Outcome: exact PASS."""
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"countertop_depth": _operand("countertop_depth", 25)},
        {
            "cabinet_depth": _parameter("cabinet_depth", 24),
            "countertop_overhang": _parameter("countertop_overhang", 1),
        },
    )

    assert finding.outcome is Outcome.PASS
    assert finding.trace is not None
    assert finding.trace.tolerance is None
    assert _trace_derivation(finding, "expected_depth")["result"] == _inch(25)


def test_any_depth_difference_fails_without_a_hidden_band() -> None:
    """Input: CT010 is 1/16 inch deeper than the exact sum. Outcome: FAIL, not tolerance."""
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"countertop_depth": _operand("countertop_depth", Fraction(401, 16))},
        {
            "cabinet_depth": _parameter("cabinet_depth", 24),
            "countertop_overhang": _parameter("countertop_overhang", 1),
        },
    )

    assert finding.outcome is Outcome.FAIL
    assert finding.trace is not None
    assert finding.trace.tolerance is None


def test_back_offset_is_derived_as_a_remainder_and_passes_at_the_minimum() -> None:
    """Input: 25 - 4 - 18 = 3 and minimum=3. Outcome: inclusive boundary PASS."""
    finding = execute(
        publish(_load(BACK_OFFSET_RULE_PATH)),
        {
            "countertop_depth": _operand("countertop_depth", 25),
            "front_offset": _operand("front_offset", 4),
            "sink_depth": _operand("sink_depth", 18),
        },
        {"back_offset_minimum": _parameter("back_offset_minimum", 3)},
    )

    assert finding.outcome is Outcome.PASS
    back_offset = _trace_derivation(finding, "back_offset")
    assert back_offset["inputs"] == (
        ("a", _inch(21)),
        ("b", _inch(18, "18")),
    )
    assert back_offset["result"] == _inch(3)


def test_back_offset_below_the_required_minimum_fails() -> None:
    """Input: derived remainder 2 15/16 with minimum=3. Outcome: actionable FAIL."""
    finding = execute(
        publish(_load(BACK_OFFSET_RULE_PATH)),
        {
            "countertop_depth": _operand("countertop_depth", Fraction(399, 16)),
            "front_offset": _operand("front_offset", 4),
            "sink_depth": _operand("sink_depth", 18),
        },
        {"back_offset_minimum": _parameter("back_offset_minimum", 3)},
    )

    assert finding.outcome is Outcome.FAIL


def test_missing_back_offset_minimum_abstains_instead_of_assuming_2375_inches() -> None:
    """Input: no vendor minimum. Outcome: NOT_FOUND; the rejected 2.375-inch guess is unused."""
    rule = _load(BACK_OFFSET_RULE_PATH)
    finding = execute(
        publish(rule),
        {
            "countertop_depth": _operand("countertop_depth", 25),
            "front_offset": _operand("front_offset", 4),
            "sink_depth": _operand("sink_depth", 18),
        },
        {},
    )

    assert rule.parameters["back_offset_minimum"].default is None
    assert finding.outcome is Outcome.NOT_FOUND
    assert "back_offset_minimum" in finding.reason


def test_offset_sum_is_not_authored_as_a_tautological_check() -> None:
    """Input: both CT-2 YAMLs. Outcome: no reconstructed offset-sum terminal comparison."""
    depth_rule = _load(DEPTH_RULE_PATH)
    back_rule = _load(BACK_OFFSET_RULE_PATH)

    assert depth_rule.operation.operands == {
        "actual": "countertop_depth",
        "expected": "expected_depth",
    }
    assert back_rule.operation.operands == {
        "x": "back_offset",
        "bound": "back_offset_minimum",
    }
    assert all(derivation.name != "offset_sum" for derivation in back_rule.derivations)
