"""Preparation harness for comparing the bounded agent with fixed extraction routing.

The real experiment cannot run until a reviewed gold set exists. This module builds the
reproducible comparison boundary now and refuses an empty set rather than reporting invented
success. Both arms receive the same immutable case identifiers and code version. Cost is summed
only from recorded model invocations, and critical false-PASS is always the first reported metric.

``DO_NOT_SHIP`` is a successful experimental conclusion: it means the optional agent did not earn
its place. It is not an execution error and must remain recordable.

Source: ``AGENTS.md`` section 8 Phase 4, system design sections 7 and 14, issue #248.
Verification: ``tests/eval/test_agent_experiment.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from extraction.models.invocations import InvocationRecord


class ExperimentVerdict(StrEnum):
    """Whether the bounded agent earned shipment; either value is a valid result."""

    SHIP = "SHIP"
    DO_NOT_SHIP = "DO_NOT_SHIP"


class EmptyGoldSetError(ValueError):
    """The experiment was asked to measure against no reviewed cases."""


def _require_text(value: object, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _rate(value: object, *, field: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be a Decimal")
    if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise ValueError(f"{field} must be finite and between 0 and 1 inclusive")


@dataclass(frozen=True, slots=True)
class ExperimentGoldSet:
    """Immutable identity and case selection shared by both experiment arms."""

    gold_set_id: str
    version: str
    case_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.gold_set_id, field="gold_set_id")
        _require_text(self.version, field="version")
        if not isinstance(self.case_ids, tuple):
            raise TypeError("case_ids must be a tuple")
        if any(not isinstance(case_id, str) or not case_id.strip() for case_id in self.case_ids):
            raise ValueError("case_ids must contain only non-empty strings")
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("case_ids must be unique")


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    """Exact non-cost measurements produced by one experiment arm."""

    critical_false_pass: Decimal
    accuracy: Decimal
    reviewer_minutes: Decimal

    def __post_init__(self) -> None:
        _rate(self.critical_false_pass, field="critical_false_pass")
        _rate(self.accuracy, field="accuracy")
        if not isinstance(self.reviewer_minutes, Decimal):
            raise TypeError("reviewer_minutes must be a Decimal")
        if not self.reviewer_minutes.is_finite() or self.reviewer_minutes < 0:
            raise ValueError("reviewer_minutes must be finite and zero or greater")


@dataclass(frozen=True, slots=True)
class ArmExecution:
    """One arm's metrics and the real invocation records from which cost is computed."""

    metrics: ArmMetrics
    invocations: tuple[InvocationRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.metrics, ArmMetrics):
            raise TypeError("metrics must be ArmMetrics")
        if not isinstance(self.invocations, tuple) or any(
            not isinstance(invocation, InvocationRecord) for invocation in self.invocations
        ):
            raise TypeError("invocations must be a tuple of InvocationRecord values")


@dataclass(frozen=True, slots=True)
class ArmResult:
    """Reported measurements for one arm, with critical false-PASS first."""

    critical_false_pass: Decimal
    accuracy: Decimal
    cost_micros: int
    reviewer_minutes: Decimal

    def __post_init__(self) -> None:
        _rate(self.critical_false_pass, field="critical_false_pass")
        _rate(self.accuracy, field="accuracy")
        if isinstance(self.cost_micros, bool) or not isinstance(self.cost_micros, int):
            raise TypeError("cost_micros must be a plain integer")
        if self.cost_micros < 0:
            raise ValueError("cost_micros must be zero or greater")
        if not isinstance(self.reviewer_minutes, Decimal):
            raise TypeError("reviewer_minutes must be a Decimal")
        if not self.reviewer_minutes.is_finite() or self.reviewer_minutes < 0:
            raise ValueError("reviewer_minutes must be finite and zero or greater")


class ExperimentArm(Protocol):
    """A fixed or agent route that can run the same pinned case selection."""

    def run(self, gold: ExperimentGoldSet, *, code_version: str) -> ArmExecution:
        """Execute exactly the supplied cases at the supplied code version."""


@dataclass(frozen=True, slots=True)
class ReportedMetric:
    """One ordered comparison shown in the experiment report."""

    name: str
    fixed: Decimal | int
    agent: Decimal | int


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    """Reproducible head-to-head result ready for evaluation-run persistence."""

    gold_set_id: str
    gold_set_version: str
    code_version: str
    fixed: ArmResult
    agent: ArmResult
    verdict: ExperimentVerdict
    metrics: tuple[ReportedMetric, ...]
    reason: str

    def __post_init__(self) -> None:
        if not self.metrics or self.metrics[0].name != "critical_false_pass":
            raise ValueError("critical_false_pass must be the first reported metric")


class EvaluationRunRecorder(Protocol):
    """Persistence boundary implemented by the F4 evaluation-run layer."""

    def record(self, report: ExperimentReport) -> None:
        """Persist the complete comparison and its reproducibility fields."""


def _result(execution: ArmExecution) -> ArmResult:
    cost = sum(invocation.cost_micros for invocation in execution.invocations)
    return ArmResult(
        critical_false_pass=execution.metrics.critical_false_pass,
        accuracy=execution.metrics.accuracy,
        cost_micros=cost,
        reviewer_minutes=execution.metrics.reviewer_minutes,
    )


def _decision(fixed: ArmResult, agent: ArmResult) -> tuple[ExperimentVerdict, str]:
    if agent.critical_false_pass > fixed.critical_false_pass:
        return (
            ExperimentVerdict.DO_NOT_SHIP,
            "agent critical false-PASS regressed; lower cost cannot override safety",
        )
    improvements = (
        agent.accuracy > fixed.accuracy,
        agent.cost_micros < fixed.cost_micros,
        agent.reviewer_minutes < fixed.reviewer_minutes,
    )
    if any(improvements):
        return (
            ExperimentVerdict.SHIP,
            "agent improved at least one exit-gate measure without a false-PASS regression",
        )
    return (
        ExperimentVerdict.DO_NOT_SHIP,
        "agent did not improve accuracy, recorded cost, or reviewer time",
    )


def compare_arms(
    gold: ExperimentGoldSet,
    *,
    code_version: str,
    fixed_arm: ExperimentArm,
    agent_arm: ExperimentArm,
    recorder: EvaluationRunRecorder,
) -> ExperimentReport:
    """Run and record both arms; refuse an empty set rather than claiming measurement.

    This is preparation for the real run. It supplies the same immutable ``gold`` and
    ``code_version`` to both arms, derives cost only from their invocation records, reports
    critical false-PASS first and records either verdict before returning it.
    """

    if not isinstance(gold, ExperimentGoldSet):
        raise TypeError("gold must be an ExperimentGoldSet")
    _require_text(code_version, field="code_version")
    if not gold.case_ids:
        raise EmptyGoldSetError(
            "the gold set contains no reviewed cases; the experiment is not measured"
        )

    fixed = _result(fixed_arm.run(gold, code_version=code_version))
    agent = _result(agent_arm.run(gold, code_version=code_version))
    verdict, reason = _decision(fixed, agent)
    report = ExperimentReport(
        gold_set_id=gold.gold_set_id,
        gold_set_version=gold.version,
        code_version=code_version,
        fixed=fixed,
        agent=agent,
        verdict=verdict,
        metrics=(
            ReportedMetric(
                "critical_false_pass", fixed.critical_false_pass, agent.critical_false_pass
            ),
            ReportedMetric("accuracy", fixed.accuracy, agent.accuracy),
            ReportedMetric("cost_micros", fixed.cost_micros, agent.cost_micros),
            ReportedMetric("reviewer_minutes", fixed.reviewer_minutes, agent.reviewer_minutes),
        ),
        reason=reason,
    )
    recorder.record(report)
    return report
