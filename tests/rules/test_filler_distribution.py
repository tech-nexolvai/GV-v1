"""The filler-first client rule executes exactly and abstains before choosing cabinets.

Source: issue #61; Cabinet_Checks.xlsx H18-H25 and N18-N22; client facts Q8, Q9 and Q21.
Verification: ``rules/rulebook/cab_filler_001.yaml``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.schema import Cardinality, Quantity, Rule
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, OperandValue, VerdictOperand
from verdict.operations import register_all
from verdict.operations.distribution import DistributionCondition, filler_distribution
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY, RuleAuthoringError

RULE_PATH = Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "cab_filler_001.yaml"


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _load_rule() -> Rule:
    return Rule.model_validate(yaml.safe_load(RULE_PATH.read_text(encoding="utf-8")))


def _inch(value: int | Fraction, raw_text: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, raw_text)


def _operand(
    name: str,
    value: OperandValue,
    *,
    source: str,
    status: EvidenceStatus = EvidenceStatus.CORROBORATED,
) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,
        status=status,
        source=source,
        evidence_ref=f"{source.lower()}:assembly-1:{name}",
    )


def _parameter(name: str, value: int | Fraction) -> ResolvedParameter:
    return ResolvedParameter(
        name=name,
        value=ParameterValue(
            value=Quantity(value=value, unit=Unit.INCH),
            provenance=Provenance.COMPANY_STANDARD,
            set_by="GV",
            set_at=datetime(2026, 8, 22, tzinfo=UTC),
        ),
        layer=ParameterLayer.GLOBAL,
    )


def _parameters() -> dict[str, ResolvedParameter]:
    return {"filler_min": _parameter("filler_min", 1), "filler_max": _parameter("filler_max", 2)}


def _operands(
    *,
    field: int | Fraction = 90,
    design: int | Fraction = 88,
    design_fillers: tuple[int | Fraction, int | Fraction] = (1, 1),
    proposed_fillers: tuple[int | Fraction, int | Fraction] = (2, 2),
) -> dict[str, VerdictOperand]:
    return {
        "field_width": _operand(
            "field_width",
            _inch(field),
            source="USER_INPUT",
            status=EvidenceStatus.HUMAN_CONFIRMED,
        ),
        "design_width": _operand("design_width", _inch(design), source="ARCH"),
        "design_fillers": _operand(
            "design_fillers",
            tuple(_inch(value) for value in design_fillers),
            source="ARCH",
        ),
        "proposed_fillers": _operand(
            "proposed_fillers",
            tuple(_inch(value) for value in proposed_fillers),
            source="SHOP",
        ),
    }


def _intermediate(finding: object, name: str) -> object:
    trace = finding.trace  # type: ignore[attr-defined]
    assert trace is not None
    return dict(trace.intermediates)[name]


def test_rule_declares_the_client_sources_bounds_and_exact_operation() -> None:
    """Input: authored YAML. Output: exact CRITICAL rule with USER_INPUT field width and two fillers."""
    rule = _load_rule()

    assert rule.severity is Severity.CRITICAL
    assert rule.arithmetic_unit is Unit.INCH
    assert rule.operation.type == "filler_distribution"
    assert rule.inputs["field_width"].source.value == "USER_INPUT"
    assert rule.inputs["design_fillers"].cardinality is Cardinality.MANY
    assert rule.inputs["proposed_fillers"].cardinality is Cardinality.MANY
    assert rule.parameters["filler_min"].default == Quantity(value=1, unit=Unit.INCH)
    assert rule.parameters["filler_max"].default == Quantity(value=2, unit=Unit.INCH)


def test_larger_site_is_absorbed_by_equal_fillers_before_any_cabinet() -> None:
    """Input: 88-inch design, 90-inch site, two 1-inch design fillers. Output: 2+2 exact PASS."""
    finding = execute(
        publish(_load_rule()),
        _operands(),
        _parameters(),
        discriminators={"filler_symmetry": "equal_unless_noted"},
    )

    assert finding.outcome is Outcome.PASS
    assert _intermediate(finding, "expected_fillers") == (_inch(2), _inch(2))
    assert _intermediate(finding, "condition") == DistributionCondition.FILLERS_ABSORB.value
    assert _intermediate(finding, "cabinet_adjustment") == "not_required; no cabinet selected"


def test_smaller_site_shrinks_fillers_exactly_without_a_false_pass() -> None:
    """Input: 88-inch design with 2+2 fillers and 87-inch site. Output: exact 1.5+1.5 PASS."""
    finding = execute(
        publish(_load_rule()),
        _operands(
            field=87,
            design_fillers=(2, 2),
            proposed_fillers=(Fraction(3, 2), Fraction(3, 2)),
        ),
        _parameters(),
        discriminators={"filler_symmetry": "equal_unless_noted"},
    )

    assert finding.outcome is Outcome.PASS
    assert _intermediate(finding, "site_difference") == _inch(-1)
    assert _intermediate(finding, "expected_fillers") == (
        _inch(Fraction(3, 2)),
        _inch(Fraction(3, 2)),
    )


def test_reviewer_noted_asymmetry_may_differ_but_must_close_exactly() -> None:
    """Input: reviewer-noted 1+2 split for an exact 3-inch total. Output: PASS with note traced."""
    finding = execute(
        publish(_load_rule()),
        _operands(field=89, proposed_fillers=(1, 2)),
        _parameters(),
        discriminators={"filler_symmetry": "reviewer_noted_asymmetric"},
    )

    assert finding.outcome is Outcome.PASS
    assert _intermediate(finding, "asymmetric_note_applied") is True
    assert _intermediate(finding, "expected_fillers") is None


def test_asymmetry_without_the_reviewer_variant_fails() -> None:
    """Input: 1+2 split under equal-U.N.O. mode. Output: FAIL despite the correct total."""
    finding = execute(
        publish(_load_rule()),
        _operands(field=89, proposed_fillers=(1, 2)),
        _parameters(),
        discriminators={"filler_symmetry": "equal_unless_noted"},
    )

    assert finding.outcome is Outcome.FAIL
    assert _intermediate(finding, "each_filler_within_bounds") is True


@pytest.mark.parametrize(
    ("field", "expected_remaining"),
    [(91, Fraction(1)), (87, Fraction(-1))],
)
def test_overflow_in_either_direction_requires_reviewer_cabinet_selection(
    field: int, expected_remaining: Fraction
) -> None:
    """Input: difference beyond filler max or min. Output: explicit REVIEW; no cabinet selected."""
    finding = execute(
        publish(_load_rule()),
        _operands(field=field),
        _parameters(),
        discriminators={"filler_symmetry": "equal_unless_noted"},
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert _intermediate(finding, "condition") == (
        DistributionCondition.CABINET_SELECTION_REQUIRED.value
    )
    assert _intermediate(finding, "remaining_difference") == _inch(expected_remaining)
    assert _intermediate(finding, "cabinet_adjustment") == (
        "reviewer_selection_required; no cabinet selected"
    )
    assert "reviewer must select an adjustable cabinet" in finding.reason


def test_each_asymmetric_filler_must_remain_inside_both_bounds() -> None:
    """Input: exact 3-inch total as 3/4+2 1/4. Output: FAIL because each bound is enforced."""
    finding = execute(
        publish(_load_rule()),
        _operands(field=89, proposed_fillers=(Fraction(3, 4), Fraction(9, 4))),
        _parameters(),
        discriminators={"filler_symmetry": "reviewer_noted_asymmetric"},
    )

    assert finding.outcome is Outcome.FAIL
    assert _intermediate(finding, "each_filler_within_bounds") is False


def test_missing_and_unqualified_fillers_abstain_before_distribution() -> None:
    """Input: no proposed fillers, then ambiguous fillers. Output: NOT_FOUND then REVIEW_REQUIRED."""
    missing = _operands()
    missing["proposed_fillers"] = _operand("proposed_fillers", (), source="SHOP")
    missing_finding = execute(
        publish(_load_rule()),
        missing,
        _parameters(),
        discriminators={"filler_symmetry": "equal_unless_noted"},
    )

    ambiguous = _operands()
    ambiguous["proposed_fillers"] = _operand(
        "proposed_fillers",
        (_inch(2), _inch(2)),
        source="SHOP",
        status=EvidenceStatus.RAW_CANDIDATE,
    )
    ambiguous_finding = execute(
        publish(_load_rule()),
        ambiguous,
        _parameters(),
        discriminators={"filler_symmetry": "equal_unless_noted"},
    )

    assert missing_finding.outcome is Outcome.NOT_FOUND
    assert missing_finding.trace is None
    assert ambiguous_finding.outcome is Outcome.REVIEW_REQUIRED
    assert ambiguous_finding.trace is None


def test_operation_refuses_malformed_pairs_modes_and_bounds() -> None:
    """Input: unsafe authoring values. Output: loud errors rather than guessed distribution."""
    with pytest.raises(RuleAuthoringError, match="exactly two ordered values"):
        filler_distribution(
            field_width=_inch(90),
            design_width=_inch(88),
            design_fillers=(_inch(1),),
            proposed_fillers=(_inch(2), _inch(2)),
            filler_min=_inch(1),
            filler_max=_inch(2),
            allow_asymmetric=0,
        )
    with pytest.raises(RuleAuthoringError, match="reviewed integer 0 or 1"):
        filler_distribution(
            field_width=_inch(90),
            design_width=_inch(88),
            design_fillers=(_inch(1), _inch(1)),
            proposed_fillers=(_inch(2), _inch(2)),
            filler_min=_inch(1),
            filler_max=_inch(2),
            allow_asymmetric=True,
        )
    with pytest.raises(RuleAuthoringError, match="must not exceed"):
        filler_distribution(
            field_width=_inch(90),
            design_width=_inch(88),
            design_fillers=(_inch(1), _inch(1)),
            proposed_fillers=(_inch(2), _inch(2)),
            filler_min=_inch(2),
            filler_max=_inch(1),
            allow_asymmetric=0,
        )
