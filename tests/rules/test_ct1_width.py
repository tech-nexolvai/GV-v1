"""CT-1 proves the real countertop-width rule through the complete verdict path.

Source: issue #58; Countertop_Checks_Updated.xlsx D23/D24/D27; client facts Q1-Q3,
Q12 and Q14. Verification: ``rules/rulebook/ct_width_001.yaml``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import cast

import pytest
import yaml

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.publication import is_production_ready, tolerances_of
from rules.schema import Cardinality, Quantity, Rule
from rules.semantic_types import SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, OperandValue, VerdictOperand
from verdict.operations import register_all
from verdict.operations.aggregate import sum as exact_sum
from verdict.operations.scalar import scale
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY, RuleAuthoringError

RULE_PATH = Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "ct_width_001.yaml"

# The real drawing states the same component chain twice. The rule decides in inches; the
# millimetre tokens remain a fixture proving that neither authored representation was discarded.
REAL_MM_CHAIN = (51, 533, 457, 984)
REAL_INCH_CABINETS = (Fraction(21), Fraction(18), Fraction(155, 4))
REAL_INCH_FILLERS = (Fraction(2),)
WALL_TO_WALL_INCHES = Fraction(319, 4)
THREE_WALL_OVERALL_INCHES = Fraction(327, 4)


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _load_rule() -> Rule:
    authored = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return Rule.model_validate(authored)


def _inch(value: int | Fraction, raw_text: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, raw_text)


def _mm(value: int) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, str(value))


def _operand(
    name: str,
    value: OperandValue,
    *,
    status: EvidenceStatus = EvidenceStatus.CORROBORATED,
) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,
        status=status,
        source="SHOP",
        evidence_ref=f"shop:p1:{name}",
    )


def _field_cut(value: int | Fraction = 1) -> ResolvedParameter:
    return ResolvedParameter(
        name="field_cut",
        value=ParameterValue(
            value=Quantity(value=value, unit=Unit.INCH),
            provenance=Provenance.COMPANY_STANDARD,
            set_by="GV",
            set_at=datetime(2026, 8, 22, tzinfo=UTC),
        ),
        layer=ParameterLayer.GLOBAL,
    )


def _operands(overall: Fraction = THREE_WALL_OVERALL_INCHES) -> dict[str, VerdictOperand]:
    return {
        "countertop_width": _operand("countertop_width", _inch(overall, str(overall))),
        "cabinet_widths": _operand(
            "cabinet_widths", tuple(_inch(value, str(value)) for value in REAL_INCH_CABINETS)
        ),
        "filler_widths": _operand(
            "filler_widths", tuple(_inch(value, str(value)) for value in REAL_INCH_FILLERS)
        ),
    }


def _trace_derivation(finding: object, name: str) -> dict[str, object]:
    trace = finding.trace  # type: ignore[attr-defined]
    assert trace is not None
    for intermediate_name, fields in trace.intermediates:
        if intermediate_name == name:
            return dict(cast(tuple[tuple[str, object], ...], fields))
    raise AssertionError(f"trace did not contain derivation {name!r}")


def test_rule_authors_exact_inch_equality_without_a_tolerance() -> None:
    """Input: CT-1 YAML. Output: a CRITICAL exact-equality rule ready for production."""
    rule = _load_rule()

    assert rule.severity is Severity.CRITICAL
    assert rule.arithmetic_unit is Unit.INCH
    assert rule.operation.type == "equals"
    assert tolerances_of(rule) == ()
    assert is_production_ready(rule)
    assert rule.inputs["cabinet_widths"].cardinality is Cardinality.MANY
    assert rule.inputs["filler_widths"].cardinality is Cardinality.MANY
    assert rule.inputs["countertop_width"].semantic_type is SemanticType.COUNTERTOP_OVERALL_WIDTH


def test_real_drawing_component_tokens_close_exactly_in_both_authored_units() -> None:
    """Input: 51+533+457+984 and 2+21+18+38 3/4. Output: exact matching totals."""
    mm_total = exact_sum(values=tuple(_mm(value) for value in REAL_MM_CHAIN))
    inch_total = exact_sum(
        values=tuple(_inch(value) for value in (*REAL_INCH_FILLERS, *REAL_INCH_CABINETS))
    )

    assert mm_total.value == Measurement(Fraction(2025), Unit.MM, None)
    assert inch_total.value == Measurement(WALL_TO_WALL_INCHES, Unit.INCH, None)


def test_three_wall_rule_adds_two_distinct_field_cuts_and_passes() -> None:
    """Input: 79 3/4-inch run plus two 1-inch cuts. Output: exact PASS at 81 3/4."""
    finding = execute(
        publish(_load_rule()),
        _operands(),
        {"field_cut": _field_cut()},
        discriminators={"wall_config": "back_left_right"},
    )

    assert finding.outcome is Outcome.PASS
    assert finding.variant == "back_left_right"
    assert finding.trace is not None
    assert finding.trace.arithmetic_unit is Unit.INCH
    assert finding.trace.tolerance is None
    cut = _trace_derivation(finding, "field_cut_total")
    assert cut["inputs"] == (("value", _inch(1)), ("multiplier", 2))
    assert cut["result"] == Measurement(Fraction(2), Unit.INCH, None)
    assert _trace_derivation(finding, "expected_width")["result"] == Measurement(
        THREE_WALL_OVERALL_INCHES, Unit.INCH, None
    )


def test_any_exact_mismatch_fails_instead_of_using_a_hidden_band() -> None:
    """Input: overall width 1/16 inch high. Output: FAIL because V1 has no tolerance."""
    finding = execute(
        publish(_load_rule()),
        _operands(THREE_WALL_OVERALL_INCHES + Fraction(1, 16)),
        {"field_cut": _field_cut()},
        discriminators={"wall_config": "back_left_right"},
    )

    assert finding.outcome is Outcome.FAIL
    assert finding.trace is not None
    assert finding.trace.tolerance is None


@pytest.mark.parametrize("wall_config", ["back_only", "island"])
def test_layouts_without_side_walls_apply_zero_field_cuts(wall_config: str) -> None:
    """Input: an in-scope no-side-wall layout. Output: PASS with a traced zero cut total."""
    finding = execute(
        publish(_load_rule()),
        _operands(WALL_TO_WALL_INCHES),
        {"field_cut": _field_cut()},
        discriminators={"wall_config": wall_config},
    )

    assert finding.outcome is Outcome.PASS
    cut = _trace_derivation(finding, "field_cut_total")
    assert cut["inputs"] == (("value", _inch(1)), ("multiplier", 0))
    assert cut["result"] == Measurement(Fraction(0), Unit.INCH, None)


def test_missing_or_unqualified_many_operand_abstains_before_arithmetic() -> None:
    """Input: no cabinets, then ambiguous cabinets. Output: NOT_FOUND, then REVIEW_REQUIRED."""
    missing = _operands()
    missing["cabinet_widths"] = _operand("cabinet_widths", ())
    missing_finding = execute(
        publish(_load_rule()),
        missing,
        {"field_cut": _field_cut()},
        discriminators={"wall_config": "back_left_right"},
    )

    ambiguous = _operands()
    ambiguous["cabinet_widths"] = _operand(
        "cabinet_widths",
        tuple(_inch(value) for value in REAL_INCH_CABINETS),
        status=EvidenceStatus.RAW_CANDIDATE,
    )
    ambiguous_finding = execute(
        publish(_load_rule()),
        ambiguous,
        {"field_cut": _field_cut()},
        discriminators={"wall_config": "back_left_right"},
    )

    assert missing_finding.outcome is Outcome.NOT_FOUND
    assert missing_finding.trace is None
    assert ambiguous_finding.outcome is Outcome.REVIEW_REQUIRED
    assert ambiguous_finding.trace is None


def test_mixed_units_inside_a_many_operand_produce_review_required() -> None:
    """Input: one millimetre cabinet among inch operands. Output: REVIEW_REQUIRED, no conversion."""
    operands = _operands()
    operands["cabinet_widths"] = _operand(
        "cabinet_widths", (_inch(21), _mm(457), _inch(Fraction(155, 4)))
    )

    finding = execute(
        publish(_load_rule()),
        operands,
        {"field_cut": _field_cut()},
        discriminators={"wall_config": "back_left_right"},
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.trace is None
    assert "different unit systems" in finding.reason


def test_all_millimetre_operands_cannot_bypass_the_rules_inch_policy() -> None:
    """Input: internally consistent mm values. Output: REVIEW because inches govern CT-1."""
    operands = {
        "countertop_width": _operand(
            "countertop_width", Measurement(Fraction(10379, 5), Unit.MM, "2075.8")
        ),
        "cabinet_widths": _operand(
            "cabinet_widths", tuple(_mm(value) for value in (533, 457, 984))
        ),
        "filler_widths": _operand("filler_widths", (_mm(51),)),
    }

    finding = execute(
        publish(_load_rule()),
        operands,
        {
            "field_cut": ResolvedParameter(
                name="field_cut",
                value=ParameterValue(
                    value=Quantity(value=Fraction(254, 10), unit=Unit.MM),
                    provenance=Provenance.COMPANY_STANDARD,
                    set_by="GV",
                    set_at=datetime(2026, 8, 22, tzinfo=UTC),
                ),
                layer=ParameterLayer.GLOBAL,
            )
        },
        discriminators={"wall_config": "back_left_right"},
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.trace is None
    assert "rule requires 'in' arithmetic" in finding.reason


@pytest.mark.parametrize("bad", [True, -1, 1.0, Decimal("NaN"), Decimal("Infinity")])
def test_scale_refuses_non_integer_or_negative_multipliers(bad: object) -> None:
    """Input: unsafe scale multipliers. Output: loud authoring error, never coercion."""
    with pytest.raises(RuleAuthoringError, match="non-negative real integer"):
        scale(value=_inch(1), multiplier=bad)  # type: ignore[arg-type]


def test_many_operand_rejects_a_nested_float_at_the_sealed_boundary() -> None:
    """Input: a float hidden in a tuple. Output: rejection before the engine sees it."""
    unsafe = cast(OperandValue, (_inch(1), 2.0))
    with pytest.raises(TypeError, match="cannot be a float"):
        _operand("cabinet_widths", unsafe)


def test_many_operand_rejects_a_mutable_list() -> None:
    """Input: mutable measurement list. Output: refusal so sealed evidence cannot change later."""
    unsafe = cast(OperandValue, [_inch(21), _inch(18)])
    with pytest.raises(TypeError, match="immutable tuple"):
        _operand("cabinet_widths", unsafe)
