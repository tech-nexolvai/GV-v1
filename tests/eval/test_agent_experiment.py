"""Tests for the prep-only agent-versus-fixed experiment harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

import pytest

from eval.experiments.agent_vs_fixed import (
    ArmExecution,
    ArmMetrics,
    EmptyGoldSetError,
    ExperimentGoldSet,
    ExperimentReport,
    ExperimentVerdict,
    compare_arms,
)
from extraction.models.invocations import InvocationRecord


def _invocation(cost: int) -> InvocationRecord:
    return InvocationRecord(
        extraction_run_id=UUID("00000000-0000-0000-0000-000000000001"),
        model_id="model-v1",
        prompt_id="prompt-v1",
        template_id="template-v1",
        crop_artifact_id=None,
        input_tokens=10,
        output_tokens=2,
        cost_micros=cost,
        latency_ms=5,
        outcome="ok",
    )


def _execution(
    *,
    false_pass: str = "0",
    accuracy: str = "0.90",
    costs: tuple[int, ...] = (),
    minutes: str = "10",
) -> ArmExecution:
    return ArmExecution(
        ArmMetrics(Decimal(false_pass), Decimal(accuracy), Decimal(minutes)),
        tuple(_invocation(cost) for cost in costs),
    )


@dataclass
class Arm:
    execution: ArmExecution
    calls: list[tuple[ExperimentGoldSet, str]] = field(default_factory=list)

    def run(self, gold: ExperimentGoldSet, *, code_version: str) -> ArmExecution:
        self.calls.append((gold, code_version))
        return self.execution


@dataclass
class Recorder:
    reports: list[ExperimentReport] = field(default_factory=list)

    def record(self, report: ExperimentReport) -> None:
        self.reports.append(report)


def _gold(*case_ids: str) -> ExperimentGoldSet:
    return ExperimentGoldSet("gold-1", "v1", case_ids or ("GC-001",))


def test_both_arms_receive_the_identical_gold_set_and_code_version() -> None:
    """Input: one pinned set/version. Output: both runners receive the same immutable values."""

    gold = _gold("GC-001", "GC-002")
    fixed = Arm(_execution())
    agent = Arm(_execution(accuracy="0.91"))

    compare_arms(
        gold,
        code_version="commit-abc",
        fixed_arm=fixed,
        agent_arm=agent,
        recorder=Recorder(),
    )

    assert fixed.calls == [(gold, "commit-abc")]
    assert agent.calls == [(gold, "commit-abc")]


def test_false_pass_regression_is_do_not_ship_even_when_agent_is_cheaper() -> None:
    """Input: cheaper agent with worse false-PASS. Output: DO_NOT_SHIP for safety."""

    report = compare_arms(
        _gold(),
        code_version="commit-abc",
        fixed_arm=Arm(_execution(false_pass="0", costs=(500,))),
        agent_arm=Arm(_execution(false_pass="0.01", accuracy="1", costs=(1,), minutes="1")),
        recorder=Recorder(),
    )

    assert report.verdict is ExperimentVerdict.DO_NOT_SHIP
    assert report.metrics[0].name == "critical_false_pass"
    assert "cannot override safety" in report.reason


def test_do_not_ship_is_recorded_as_a_successful_experiment_result() -> None:
    """Input: agent improves nothing. Output: recorded DO_NOT_SHIP, not an exception."""

    recorder = Recorder()
    report = compare_arms(
        _gold(),
        code_version="commit-abc",
        fixed_arm=Arm(_execution()),
        agent_arm=Arm(_execution()),
        recorder=recorder,
    )

    assert report.verdict is ExperimentVerdict.DO_NOT_SHIP
    assert recorder.reports == [report]


def test_cost_is_summed_from_real_invocation_records() -> None:
    """Input: two invocation rows. Output: their exact integer cost, never an estimate."""

    report = compare_arms(
        _gold(),
        code_version="commit-abc",
        fixed_arm=Arm(_execution(costs=(101, 202))),
        agent_arm=Arm(_execution(costs=(11, 22))),
        recorder=Recorder(),
    )

    assert report.fixed.cost_micros == 303
    assert report.agent.cost_micros == 33


def test_an_improvement_without_false_pass_regression_can_ship() -> None:
    """Input: equal safety and higher accuracy. Output: SHIP is structurally reachable."""

    report = compare_arms(
        _gold(),
        code_version="commit-abc",
        fixed_arm=Arm(_execution(accuracy="0.90")),
        agent_arm=Arm(_execution(accuracy="0.91")),
        recorder=Recorder(),
    )

    assert report.verdict is ExperimentVerdict.SHIP


def test_empty_gold_set_is_not_misreported_as_a_success() -> None:
    """Input: zero reviewed cases. Output: explicit NOT MEASURED refusal; neither arm runs."""

    fixed = Arm(_execution())
    agent = Arm(_execution())

    with pytest.raises(EmptyGoldSetError, match="not measured"):
        compare_arms(
            ExperimentGoldSet("gold-1", "v0", ()),
            code_version="commit-abc",
            fixed_arm=fixed,
            agent_arm=agent,
            recorder=Recorder(),
        )

    assert fixed.calls == []
    assert agent.calls == []


@pytest.mark.parametrize(
    "value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01"), Decimal("1.01")]
)
def test_invalid_exact_rates_are_refused(value: Decimal) -> None:
    """Input: non-finite/out-of-range rate. Output: refusal before evaluation evidence exists."""

    with pytest.raises(ValueError, match="finite and between"):
        ArmMetrics(value, Decimal("0.9"), Decimal(1))


def test_duplicate_case_identifiers_are_refused() -> None:
    """Input: duplicate case identity. Output: refusal because both arms need one stable set."""

    with pytest.raises(ValueError, match="unique"):
        _gold("GC-001", "GC-001")
