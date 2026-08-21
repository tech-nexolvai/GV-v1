"""Measured model-cost attribution by package, rule and finding.

Source: system design section 12, backend proposal section 6.3, and issue #266.
Verification: this file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.budget.attribution import (
    AttributedUsage,
    PlanningStatus,
    UnknownFindingError,
    compare_monthly_plan,
    cost_by_finding,
    cost_by_package,
    usage_by_finding,
    usage_by_package,
    usage_by_rule,
)

PACKAGE_A = UUID("00000000-0000-0000-0000-000000000601")
PACKAGE_B = UUID("00000000-0000-0000-0000-000000000602")
FINDING = UUID("00000000-0000-0000-0000-000000000701")
INVOCATION_A = UUID("00000000-0000-0000-0000-000000000801")
INVOCATION_B = UUID("00000000-0000-0000-0000-000000000802")
NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)

    def all(self) -> list[tuple[object, ...]]:
        return self._rows

    def tuples(self) -> _Result:
        return self


class _Session:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        *,
        finding_exists: bool = True,
    ) -> None:
        self.rows = rows
        self.finding_exists = finding_exists
        self.statements: list[object] = []

    def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)

    def get(self, model: object, identity: UUID) -> object | None:
        del model, identity
        return object() if self.finding_exists else None


def test_package_cost_comes_only_from_recorded_invocation_rows() -> None:
    """Input: three recorded calls over two packages. Output: exact per-package USD micros."""

    session = _Session(
        [
            (PACKAGE_A, INVOCATION_A, 10, 2, 50, 101),
            (PACKAGE_A, INVOCATION_B, 20, 3, 70, 202),
            (PACKAGE_B, INVOCATION_A, 10, 2, 50, 999),
        ]
    )

    costs = cost_by_package(session, timedelta(days=30), as_of=NOW)  # type: ignore[arg-type]

    assert costs == {PACKAGE_A: 303, PACKAGE_B: 999}
    assert "model_invocations.cost_micros" in str(session.statements[0])


def test_package_usage_reports_calls_tokens_latency_and_cost() -> None:
    """Input: two calls. Output: measured usage fields; no CPU/GPU claim exists."""

    result = usage_by_package(
        _Session(
            [
                (PACKAGE_A, INVOCATION_A, 10, 2, 50, 101),
                (PACKAGE_A, INVOCATION_B, 20, 3, 70, 202),
            ]
        ),  # type: ignore[arg-type]
        timedelta(days=30),
        as_of=NOW,
    )[PACKAGE_A]

    assert result == AttributedUsage(2, 30, 5, 120, 303)
    assert not hasattr(result, "gpu_cost_micros")
    assert not hasattr(result, "cpu_cost_micros")


def test_finding_attribution_follows_candidate_evidence_and_deduplicates_invocations() -> None:
    """Input: one shared invocation joined twice. Output: charged once to this finding."""

    session = _Session(
        [
            (INVOCATION_A, 10, 2, 50, 101),
            (INVOCATION_A, 10, 2, 50, 101),
            (INVOCATION_B, 20, 3, 70, 202),
        ]
    )

    result = usage_by_finding(session, FINDING)  # type: ignore[arg-type]

    assert result == AttributedUsage(2, 30, 5, 120, 303)
    sql = str(session.statements[0])
    assert "evidence_supporting_candidates" in sql
    assert "finding_evidence" in sql
    assert cost_by_finding(_Session([(INVOCATION_A, 1, 1, 1, 7)]), FINDING) == 7  # type: ignore[arg-type]


def test_unknown_finding_is_not_misreported_as_zero_cost() -> None:
    """Input: nonexistent finding. Output: loud error, not plausible zero spend."""

    with pytest.raises(UnknownFindingError, match="no finding"):
        cost_by_finding(_Session([], finding_exists=False), FINDING)  # type: ignore[arg-type]


def test_rule_attribution_reaches_rule_definition_and_is_inclusive() -> None:
    """Input: invocation supporting rule findings. Output: exact distinct usage for that rule."""

    session = _Session([(INVOCATION_A, 10, 2, 50, 101), (INVOCATION_A, 10, 2, 50, 101)])

    result = usage_by_rule(session, "CT-1")  # type: ignore[arg-type]

    assert result.cost_usd_micros == 101
    sql = str(session.statements[0])
    assert "rule_snapshots" in sql
    assert "rule_definitions.rule_id" in sql


@pytest.mark.parametrize(
    ("micros", "expected", "status"),
    [
        (50_000_000, Decimal(4150), PlanningStatus.BELOW),
        (80_000_000, Decimal(6640), PlanningStatus.WITHIN),
        (120_000_000, Decimal(9960), PlanningStatus.ABOVE),
    ],
)
def test_monthly_plan_comparison_uses_explicit_exact_exchange_rate(
    micros: int,
    expected: Decimal,
    status: PlanningStatus,
) -> None:
    """Input: recorded USD micros plus supplied rate. Output: reproducible INR comparison."""

    result = compare_monthly_plan(
        micros,
        usd_to_inr=Decimal(83),
        rate_source="Treasury approved monthly rate",
        rate_as_of=NOW,
    )

    assert result.measured_cost_inr == expected
    assert result.status is status
    assert result.rate_source == "Treasury approved monthly rate"
    assert result.rate_as_of == NOW


@pytest.mark.parametrize("rate", [Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal(-1)])
def test_unsafe_exchange_rates_are_refused(rate: Decimal) -> None:
    """Input: missing mathematical meaning. Output: rejection before planning comparison."""

    with pytest.raises(ValueError, match="finite and positive"):
        compare_monthly_plan(
            1,
            usd_to_inr=rate,
            rate_source="source",
            rate_as_of=NOW,
        )


def test_window_and_rate_instants_must_be_explicit_and_safe() -> None:
    """Input: invalid window/naive rate time. Output: refusal rather than moving-clock report."""

    with pytest.raises(ValueError, match="window must be positive"):
        usage_by_package(_Session([]), timedelta(0), as_of=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        compare_monthly_plan(
            1,
            usd_to_inr=Decimal(83),
            rate_source="source",
            rate_as_of=NOW.replace(tzinfo=None),
        )
