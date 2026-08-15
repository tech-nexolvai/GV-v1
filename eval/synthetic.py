"""Deterministic synthetic cases for exercising the verdict engine without client data.

Synthetic expectations are authored explicitly, never calculated from the implementation under
test. They are structurally separate from real gold cases because they have no drawing, page, or
polygon and must never enter evidence-localisation metrics.

Source: issue #71 and ADR-0014.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from eval.gold_set.schema import DEFAULT_CASES_DIRECTORY, ExpectedFinding, GoldCase
from rules.parameters import ResolvedParameter
from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import RuleSnapshot, publish
from units.dual import parse_dual
from units.measurement import Measurement, Unit
from units.policy import Consistency, check_dual
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.outcomes import Outcome, Severity

SYNTHETIC_CASES_DIRECTORY = Path(__file__).with_name("synthetic_cases")
F1_AUTHORED_TOKEN = "984 [38 3/4]"


class SeededError(StrEnum):
    """The deliberate fault a synthetic case is proving the engine handles."""

    OFF_BY_TOLERANCE = "off_by_tolerance"
    COUNT_MISMATCH = "count_mismatch"
    MISSING_OPERAND = "missing_operand"
    UNIT_MISMATCH = "unit_mismatch"
    F1_DUAL_UNIT_ROUNDING = "f1_dual_unit_rounding"


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    """Executable engine inputs that are structurally unmistakable as synthetic."""

    case_id: str
    synthetic: Literal[True]
    rule_snapshot: RuleSnapshot
    operands: Mapping[str, VerdictOperand]
    parameters: Mapping[str, ResolvedParameter]
    discriminators: Mapping[str, str]
    expected: ExpectedFinding
    seeded_error: SeededError | None

    def __post_init__(self) -> None:
        if not self.case_id.startswith("SYNTH-"):
            raise ValueError("a synthetic case id must start with 'SYNTH-'")
        if self.synthetic is not True:
            raise ValueError("a SyntheticCase must carry synthetic=True")
        if self.expected.check != self.rule_snapshot.rule_id:
            raise ValueError(
                f"synthetic case {self.case_id!r} expects check {self.expected.check!r}, "
                f"but carries snapshot {self.rule_snapshot.rule_id!r}"
            )
        object.__setattr__(self, "operands", MappingProxyType(dict(self.operands)))
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "discriminators", MappingProxyType(dict(self.discriminators)))


def _measurement(value: int | Fraction, unit: Unit, raw_text: str) -> Measurement:
    return Measurement(Fraction(value), unit, raw_text)


def _operand(
    name: str,
    value: Measurement | Fraction | str | None,
    *,
    source: str = "SHOP",
) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,
        status=EvidenceStatus.CORROBORATED,
        source=source,
        evidence_ref=f"synthetic:{name}",
    )


def _snapshot(
    case_id: str,
    operation: str,
    operand_names: tuple[str, ...],
    operation_operands: Mapping[str, str],
) -> RuleSnapshot:
    inputs = {
        name: InputSelector(
            source=OperandSource.SHOP,
            semantic_type=SemanticType.CABINET_WIDTH,
        )
        for name in operand_names
    }
    return publish(
        Rule(
            id=f"{case_id}-RULE",
            version="1.0.0",
            product_type=ProductType.CABINET,
            check_type=CheckType.INTERNAL,
            severity=Severity.CRITICAL,
            arithmetic_unit=Unit.MM,
            inputs=inputs,
            applicability=GlobalApplicability(scope="global"),
            operation=OperationRef(type=operation, operands=dict(operation_operands)),
        )
    )


def _case(
    case_id: str,
    *,
    operation: str,
    operation_operands: Mapping[str, str],
    operands: Mapping[str, VerdictOperand],
    expected: Outcome,
    seeded_error: SeededError | None,
) -> SyntheticCase:
    snapshot = _snapshot(case_id, operation, tuple(operands), operation_operands)
    return SyntheticCase(
        case_id=case_id,
        synthetic=True,
        rule_snapshot=snapshot,
        operands=operands,
        parameters={},
        discriminators={},
        expected=ExpectedFinding(
            check=snapshot.rule_id,
            outcome=expected,
            reason=f"Explicit synthetic expectation for {case_id}",
        ),
        seeded_error=seeded_error,
    )


def build_passing_case() -> SyntheticCase:
    """Return an exact comparison whose delta equals its synthetic tolerance."""
    return _case(
        "SYNTH-PASS-BOUNDARY",
        operation="within_tolerance",
        operation_operands={"actual": "actual", "expected": "expected", "tolerance": "tolerance"},
        operands={
            "actual": _operand("actual", _measurement(102, Unit.MM, "102")),
            "expected": _operand("expected", _measurement(100, Unit.MM, "100")),
            "tolerance": _operand("tolerance", _measurement(2, Unit.MM, "2")),
        },
        expected=Outcome.PASS,
        seeded_error=None,
    )


def build_off_by_tolerance_case() -> SyntheticCase:
    """Return a comparison one exact millimetre beyond its synthetic tolerance."""
    return _case(
        "SYNTH-OFF-BY-TOLERANCE",
        operation="within_tolerance",
        operation_operands={"actual": "actual", "expected": "expected", "tolerance": "tolerance"},
        operands={
            "actual": _operand("actual", _measurement(103, Unit.MM, "103")),
            "expected": _operand("expected", _measurement(100, Unit.MM, "100")),
            "tolerance": _operand("tolerance", _measurement(2, Unit.MM, "2")),
        },
        expected=Outcome.FAIL,
        seeded_error=SeededError.OFF_BY_TOLERANCE,
    )


def build_count_mismatch_case() -> SyntheticCase:
    """Return explicitly authored architectural and shop counts that disagree."""
    return _case(
        "SYNTH-COUNT-MISMATCH",
        operation="equals",
        operation_operands={"actual": "shop_count", "expected": "architectural_count"},
        operands={
            "shop_count": _operand("shop_count", Fraction(6)),
            "architectural_count": _operand("architectural_count", Fraction(7), source="ARCH"),
        },
        expected=Outcome.FAIL,
        seeded_error=SeededError.COUNT_MISMATCH,
    )


def build_missing_operand_case() -> SyntheticCase:
    """Return a required operand that is explicitly absent, never defaulted."""
    return _case(
        "SYNTH-MISSING-OPERAND",
        operation="exists",
        operation_operands={"value": "required_width"},
        operands={"required_width": _operand("required_width", None)},
        expected=Outcome.NOT_FOUND,
        seeded_error=SeededError.MISSING_OPERAND,
    )


def build_unit_mismatch_case() -> SyntheticCase:
    """Return equivalent-looking values authored in different unit systems."""
    return _case(
        "SYNTH-UNIT-MISMATCH",
        operation="equals",
        operation_operands={"actual": "shop_width", "expected": "architectural_width"},
        operands={
            "shop_width": _operand("shop_width", _measurement(984, Unit.MM, "984")),
            "architectural_width": _operand(
                "architectural_width",
                _measurement(Fraction(155, 4), Unit.INCH, "38 3/4"),
                source="ARCH",
            ),
        },
        expected=Outcome.REVIEW_REQUIRED,
        seeded_error=SeededError.UNIT_MISMATCH,
    )


def build_f1_rounding_case() -> SyntheticCase:
    """Return the real F1 dual token and an expected mixed-unit abstention."""
    dual = parse_dual(F1_AUTHORED_TOKEN)
    assert dual.alternate is not None
    return _case(
        "SYNTH-F1-DUAL-UNIT-ROUNDING",
        operation="equals",
        operation_operands={"actual": "primary", "expected": "alternate"},
        operands={
            "authored_dual_token": _operand("authored_dual_token", F1_AUTHORED_TOKEN),
            "primary": _operand("primary", dual.primary),
            "alternate": _operand("alternate", dual.alternate),
        },
        expected=Outcome.REVIEW_REQUIRED,
        seeded_error=SeededError.F1_DUAL_UNIT_ROUNDING,
    )


def generate_synthetic_cases() -> tuple[SyntheticCase, ...]:
    """Return the complete deterministic case set in stable order."""
    return (
        build_passing_case(),
        build_off_by_tolerance_case(),
        build_count_mismatch_case(),
        build_missing_operand_case(),
        build_unit_mismatch_case(),
        build_f1_rounding_case(),
    )


def load_synthetic_cases(
    cases: Sequence[object],
    *,
    directory: str | Path = SYNTHETIC_CASES_DIRECTORY,
) -> tuple[SyntheticCase, ...]:
    """Validate synthetic case identity and reject the real gold-set directory.

    Synthetic cases contain executable Python contracts and are not deserialised from untrusted
    rule text. This loader validates an already-built collection and creates its dedicated
    directory only after confirming it is not inside the proprietary real-case directory.
    """
    target = Path(directory).resolve()
    real_cases = DEFAULT_CASES_DIRECTORY.resolve()
    if target == real_cases or real_cases in target.parents:
        raise ValueError("synthetic cases cannot be loaded from the real gold-set directory")
    if any(isinstance(case, GoldCase) for case in cases):
        raise TypeError("a GoldCase cannot be loaded as a SyntheticCase")
    if any(not isinstance(case, SyntheticCase) for case in cases):
        raise TypeError("the synthetic loader accepts SyntheticCase objects only")
    target.mkdir(parents=True, exist_ok=True)
    return tuple(cast(SyntheticCase, case) for case in cases)


def run_synthetic_case(case: SyntheticCase) -> Finding:
    """Run one case through the engine and assert its independently authored expectation."""
    if case.seeded_error is SeededError.F1_DUAL_UNIT_ROUNDING:
        token_operand = case.operands.get("authored_dual_token")
        if token_operand is None or not isinstance(token_operand.value, str):
            raise AssertionError("the F1 case must carry its authored dual token")
        if (
            check_dual(parse_dual(token_operand.value))
            is not Consistency.CONSISTENT_WITHIN_ROUNDING
        ):
            raise AssertionError("the authored F1 token must exercise rounding-aware consistency")

    finding = execute(
        case.rule_snapshot,
        case.operands,
        case.parameters,
        discriminators=case.discriminators,
    )
    if finding.rule_id != case.expected.check or finding.outcome is not case.expected.outcome:
        raise AssertionError(
            f"{case.case_id} expected {case.expected.check}={case.expected.outcome.value}, "
            f"got {finding.rule_id}={finding.outcome.value}"
        )
    return finding
