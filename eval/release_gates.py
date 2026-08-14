"""Executable release gates for the deterministic review pipeline.

The four invariant gates probe contracts that exist today.  The three measured gates consume
results produced by the evaluation harness; they do not recompute metrics here.  A gate whose
required input is absent is ``NOT_EVALUATED`` and blocks release just as a failed gate does.

Source: issue #70, ``AGENTS.md`` section 9, and ADR-0014.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import RuleSnapshot, publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.outcomes import Outcome, Severity

OCR_GATE = "ocr_disagreement_never_auto_resolved"
UNKNOWN_UNIT_GATE = "unknown_unit_cannot_enter_verdict"
MISSING_SOURCE_GATE = "missing_approved_source_is_not_found"
ADVISORY_GATE = "advisory_retrieval_never_an_operand"
LOCALISATION_GATE = "evidence_localisation_meets_threshold"
GOLD_REGRESSION_GATE = "full_gold_set_regression"
SAFETY_METRICS_GATE = "safety_metrics_meet_thresholds"

GATE_IDS = (
    OCR_GATE,
    UNKNOWN_UNIT_GATE,
    MISSING_SOURCE_GATE,
    ADVISORY_GATE,
    LOCALISATION_GATE,
    GOLD_REGRESSION_GATE,
    SAFETY_METRICS_GATE,
)


class GateStatus(StrEnum):
    """Whether a safety gate held, failed, or could not be checked."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True, slots=True)
class GateResult:
    """One named gate result with plain-English supporting evidence."""

    gate_id: str
    status: GateStatus
    reason: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseGateInputs:
    """Existing findings and externally computed measurements supplied to the runner."""

    findings: tuple[Finding, ...] = ()
    metrics: Mapping[str, object] | None = None
    thresholds: Mapping[str, object] | None = None
    gold_set_version: str | None = None


@dataclass(frozen=True, slots=True)
class GateReport:
    """Complete seven-gate report. Only an all-PASS report permits release or optimisation."""

    results: tuple[GateResult, ...]

    @property
    def ships(self) -> bool:
        """Return true only when every listed gate explicitly passed."""

        return tuple(result.gate_id for result in self.results) == GATE_IDS and all(
            result.status is GateStatus.PASS for result in self.results
        )

    @property
    def optimisation_allowed(self) -> bool:
        """Reviewer-time and coverage optimisation starts only after every safety gate passes."""

        return self.ships


def _probe_snapshot() -> RuleSnapshot:
    rule = Rule(
        id="RELEASE-GATE-PROBE",
        version="1.0.0",
        product_type=ProductType.CABINET,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs={
            "value": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.CABINET_WIDTH,
            )
        },
        applicability=GlobalApplicability(scope="global"),
        # Both executable probes abstain before operation resolution.  The deliberately unknown
        # name makes an accidental fall-through loud instead of performing arithmetic.
        operation=OperationRef(type="release_gate_must_not_execute", operands={"value": "value"}),
    )
    return publish(rule)


def _probe_operand(value: Measurement | None, status: EvidenceStatus) -> VerdictOperand:
    return VerdictOperand(
        name="value",
        value=value,
        status=status,
        source="SHOP",
        evidence_ref="release-gate:constructed-input",
    )


def _ocr_gate() -> GateResult:
    finding = execute(
        _probe_snapshot(),
        {
            "value": _probe_operand(
                Measurement(Fraction(984), Unit.MM, "984"), EvidenceStatus.CONFLICTING
            )
        },
    )
    passed = finding.outcome is Outcome.REVIEW_REQUIRED and finding.trace is None
    return GateResult(
        OCR_GATE,
        GateStatus.PASS if passed else GateStatus.FAIL,
        (
            "Conflicting OCR evidence produced REVIEW_REQUIRED before arithmetic."
            if passed
            else f"Conflicting OCR evidence produced {finding.outcome.value}; expected REVIEW_REQUIRED."
        ),
        (finding.rule_id,),
    )


def _unknown_unit_gate() -> GateResult:
    try:
        Measurement(Fraction(1), "unknown", "1")  # type: ignore[arg-type]
    except TypeError:
        return GateResult(
            UNKNOWN_UNIT_GATE,
            GateStatus.PASS,
            "Measurement rejected an unknown unit before a verdict operand could be built.",
        )
    return GateResult(
        UNKNOWN_UNIT_GATE,
        GateStatus.FAIL,
        "Measurement accepted an unknown unit, so it could enter the verdict boundary.",
    )


def _missing_source_gate() -> GateResult:
    finding = execute(
        _probe_snapshot(),
        {"value": _probe_operand(None, EvidenceStatus.CORROBORATED)},
    )
    passed = finding.outcome is Outcome.NOT_FOUND and finding.trace is None
    return GateResult(
        MISSING_SOURCE_GATE,
        GateStatus.PASS if passed else GateStatus.FAIL,
        (
            "A missing approved value produced NOT_FOUND before arithmetic."
            if passed
            else f"A missing approved value produced {finding.outcome.value}; expected NOT_FOUND."
        ),
        (finding.rule_id,),
    )


def _advisory_gate() -> GateResult:
    try:
        VerdictOperand(
            name="retrieved_value",
            value=Measurement(Fraction(1), Unit.MM, "1"),
            status=EvidenceStatus.CORROBORATED,
            source="RETRIEVAL",
            evidence_ref="release-gate:advisory-probe",
        )
    except (TypeError, ValueError):
        return GateResult(
            ADVISORY_GATE,
            GateStatus.PASS,
            "The verdict operand boundary rejected advisory retrieval as a source.",
        )
    return GateResult(
        ADVISORY_GATE,
        GateStatus.FAIL,
        "The verdict operand boundary accepted source='RETRIEVAL'. Advisory retrieval can still "
        "be presented as an operand, so release is blocked.",
        ("source=RETRIEVAL",),
    )


def _exact(mapping: Mapping[str, object] | None, key: str) -> Fraction | Decimal | int | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (Fraction, Decimal, int)):
        return None
    return value


def _localisation_gate(inputs: ReleaseGateInputs) -> GateResult:
    metric = _exact(inputs.metrics, "evidence_localisation_rate")
    threshold = _exact(inputs.thresholds, "evidence_localisation_rate")
    if metric is None or threshold is None:
        return GateResult(
            LOCALISATION_GATE,
            GateStatus.NOT_EVALUATED,
            "Evidence localisation needs both the #69 metric and an explicitly declared "
            "threshold; at least one is absent.",
        )
    passed = metric >= threshold
    return GateResult(
        LOCALISATION_GATE,
        GateStatus.PASS if passed else GateStatus.FAIL,
        f"Evidence localisation was {metric}; the declared minimum is {threshold}.",
    )


def _string_items(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    return tuple(value)


def _gold_regression_gate(inputs: ReleaseGateInputs) -> GateResult:
    metrics = inputs.metrics
    if metrics is None or not inputs.gold_set_version:
        return GateResult(
            GOLD_REGRESSION_GATE,
            GateStatus.NOT_EVALUATED,
            "A complete regression needs a gold-set version, manifest case ids, executed case "
            "ids, skipped case ids, and rule snapshot ids.",
        )
    manifest = _string_items(metrics.get("gold_manifest_case_ids"))
    executed = _string_items(metrics.get("gold_executed_case_ids"))
    skipped = _string_items(metrics.get("gold_skipped_case_ids"))
    snapshots = _string_items(metrics.get("rule_snapshot_ids"))
    if manifest is None or executed is None or skipped is None or not snapshots:
        return GateResult(
            GOLD_REGRESSION_GATE,
            GateStatus.NOT_EVALUATED,
            "The gold regression record is incomplete; a subset cannot count as a full run.",
        )
    complete = not skipped and set(executed) == set(manifest)
    return GateResult(
        GOLD_REGRESSION_GATE,
        GateStatus.PASS if complete else GateStatus.NOT_EVALUATED,
        f"Gold set {inputs.gold_set_version}: executed {len(executed)} of {len(manifest)} cases, "
        f"skipped {len(skipped)}, with {len(snapshots)} rule snapshot id(s).",
        tuple(executed) + tuple(snapshots),
    )


def _safety_metrics_gate(inputs: ReleaseGateInputs) -> GateResult:
    directions = {
        "critical_false_pass_rate": "maximum",
        "numeric_exact_match_accuracy": "minimum",
        "unit_exact_match_accuracy": "minimum",
        "identifier_match_precision": "minimum",
    }
    values = {key: _exact(inputs.metrics, key) for key in directions}
    limits = {key: _exact(inputs.thresholds, key) for key in directions}
    if any(value is None for value in (*values.values(), *limits.values())):
        return GateResult(
            SAFETY_METRICS_GATE,
            GateStatus.NOT_EVALUATED,
            "False-PASS, numeric accuracy, unit accuracy, and match precision each need a #69 "
            "metric and an explicitly declared threshold.",
        )
    failures = []
    for key, direction in directions.items():
        value = values[key]
        limit = limits[key]
        assert value is not None and limit is not None
        held = value <= limit if direction == "maximum" else value >= limit
        if not held:
            failures.append(key)
    status = GateStatus.FAIL if failures else GateStatus.PASS
    reason = "All four safety metrics met their declared thresholds."
    if failures:
        reason = "Safety threshold missed by: " + ", ".join(failures) + "."
    return GateResult(SAFETY_METRICS_GATE, status, reason)


def run_gates(inputs: ReleaseGateInputs) -> GateReport:
    """Execute every release gate and return all results in stable order."""

    return GateReport(
        (
            _ocr_gate(),
            _unknown_unit_gate(),
            _missing_source_gate(),
            _advisory_gate(),
            _localisation_gate(inputs),
            _gold_regression_gate(inputs),
            _safety_metrics_gate(inputs),
        )
    )
