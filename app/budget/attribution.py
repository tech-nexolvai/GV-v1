"""Attribute recorded Bedrock usage to packages, rules and findings.

Every monetary value comes from ``model_invocations.cost_micros``. There is no model rate card in
this module. Package attribution follows the durable run chain; finding and rule attribution follows
the candidate-to-canonical-to-finding evidence chain. Shared invocations are included in every
finding they supported and deduplicated within each result, so finding totals are intentionally
non-additive rather than divided by an invented heuristic.

Amazon Bedrock does not expose CPU or GPU consumption for managed Nova inference. Tokens, calls and
latency are therefore reported as usage, never mislabeled as compute cost. USD is converted for the
documented INR planning comparison only with a required, externally supplied exact exchange rate
whose source and effective instant are retained.

Source: system design section 12, backend proposal section 6.3, and issue #266.
Verification: ``tests/budget/test_attribution.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import EvidenceSupportingCandidate
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, Finding, FindingEvidence

PLANNING_ESTIMATE_INR_MONTHLY = (6_000, 9_000)
USD_MICROS = Decimal(1_000_000)


class UnknownFindingError(LookupError):
    """The requested finding does not exist, so zero would be a false attribution."""


class PlanningStatus(StrEnum):
    """Position of measured monthly spend relative to the planning range."""

    BELOW = "below"
    WITHIN = "within"
    ABOVE = "above"


@dataclass(frozen=True, slots=True)
class AttributedUsage:
    """Exact recorded usage for one attribution target."""

    invocation_count: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd_micros: int

    def __post_init__(self) -> None:
        for name in (
            "invocation_count",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_usd_micros",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PlanningComparison:
    """A reproducible conversion of measured monthly USD spend to the INR plan."""

    measured_cost_usd_micros: int
    measured_cost_inr: Decimal
    estimate_inr_low: int
    estimate_inr_high: int
    status: PlanningStatus
    usd_to_inr: Decimal
    rate_source: str
    rate_as_of: datetime


def _aware(value: datetime, *, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _window(window: timedelta, as_of: datetime) -> datetime:
    if not isinstance(window, timedelta):
        raise TypeError("window must be a timedelta")
    if window <= timedelta(0):
        raise ValueError("window must be positive")
    _aware(as_of, name="as_of")
    return as_of - window


def _usage(rows: Iterable[tuple[UUID, int, int, int, int]]) -> AttributedUsage:
    """Aggregate rows already deduplicated by invocation identity."""

    seen: set[UUID] = set()
    input_tokens = output_tokens = latency_ms = cost = 0
    for invocation_id, row_input, row_output, row_latency, row_cost in rows:
        if invocation_id in seen:
            continue
        seen.add(invocation_id)
        input_tokens += int(row_input)
        output_tokens += int(row_output)
        latency_ms += int(row_latency)
        cost += int(row_cost)
    return AttributedUsage(len(seen), input_tokens, output_tokens, latency_ms, cost)


def usage_by_package(
    session: Session,
    window: timedelta,
    *,
    as_of: datetime,
) -> Mapping[UUID, AttributedUsage]:
    """Return recorded usage per package revision for the requested time window."""

    since = _window(window, as_of)
    statement = (
        select(
            WorkflowRun.package_revision_id,
            ModelInvocation.id,
            ModelInvocation.input_tokens,
            ModelInvocation.output_tokens,
            ModelInvocation.latency_ms,
            ModelInvocation.cost_micros,
        )
        .select_from(ModelInvocation)
        .join(ExtractionRun, ModelInvocation.extraction_run_id == ExtractionRun.id)
        .join(TaskRun, ExtractionRun.task_run_id == TaskRun.id)
        .join(WorkflowRun, TaskRun.workflow_run_id == WorkflowRun.id)
        .where(ModelInvocation.created_at >= since, ModelInvocation.created_at <= as_of)
        .order_by(WorkflowRun.package_revision_id, ModelInvocation.id)
    )
    grouped: dict[UUID, list[tuple[UUID, int, int, int, int]]] = {}
    for package_id, invocation_id, inputs, outputs, latency, cost in session.execute(
        statement
    ).tuples():
        grouped.setdefault(package_id, []).append((invocation_id, inputs, outputs, latency, cost))
    return MappingProxyType({package_id: _usage(rows) for package_id, rows in grouped.items()})


def cost_by_package(
    session: Session,
    window: timedelta,
    *,
    as_of: datetime,
) -> Mapping[UUID, int]:
    """Return exact recorded USD micros per package revision."""

    return MappingProxyType(
        {
            package_id: usage.cost_usd_micros
            for package_id, usage in usage_by_package(session, window, as_of=as_of).items()
        }
    )


def _finding_statement(finding_id: UUID):  # type: ignore[no-untyped-def]
    return (
        select(
            ModelInvocation.id,
            ModelInvocation.input_tokens,
            ModelInvocation.output_tokens,
            ModelInvocation.latency_ms,
            ModelInvocation.cost_micros,
        )
        .select_from(ModelInvocation)
        .join(
            EvidenceSupportingCandidate,
            ModelInvocation.candidate_id == EvidenceSupportingCandidate.candidate_id,
        )
        .join(
            FindingEvidence,
            EvidenceSupportingCandidate.canonical_observation_id
            == FindingEvidence.canonical_observation_id,
        )
        .where(FindingEvidence.finding_id == finding_id)
        .order_by(ModelInvocation.id)
    )


def usage_by_finding(session: Session, finding_id: UUID) -> AttributedUsage:
    """Return inclusive recorded model usage for one finding's evidence."""

    if session.get(Finding, finding_id) is None:
        raise UnknownFindingError(f"no finding {finding_id}")
    return _usage(session.execute(_finding_statement(finding_id)).tuples().all())


def cost_by_finding(session: Session, finding_id: UUID) -> int:
    """Return exact recorded USD micros for one finding, or raise when it is unknown."""

    return usage_by_finding(session, finding_id).cost_usd_micros


def usage_by_rule(session: Session, rule_id: str) -> AttributedUsage:
    """Return inclusive usage of distinct invocations supporting findings for one rule."""

    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule_id must be a non-empty string")
    statement = (
        select(
            ModelInvocation.id,
            ModelInvocation.input_tokens,
            ModelInvocation.output_tokens,
            ModelInvocation.latency_ms,
            ModelInvocation.cost_micros,
        )
        .select_from(ModelInvocation)
        .join(
            EvidenceSupportingCandidate,
            ModelInvocation.candidate_id == EvidenceSupportingCandidate.candidate_id,
        )
        .join(
            FindingEvidence,
            EvidenceSupportingCandidate.canonical_observation_id
            == FindingEvidence.canonical_observation_id,
        )
        .join(Finding, FindingEvidence.finding_id == Finding.id)
        .join(CheckRun, Finding.check_run_id == CheckRun.id)
        .join(RuleSnapshot, CheckRun.rule_snapshot_id == RuleSnapshot.id)
        .join(RuleDefinition, RuleSnapshot.rule_definition_id == RuleDefinition.id)
        .where(RuleDefinition.rule_id == rule_id.strip())
        .order_by(ModelInvocation.id)
    )
    return _usage(session.execute(statement).tuples().all())


def compare_monthly_plan(
    cost_usd_micros: int,
    *,
    usd_to_inr: Decimal,
    rate_source: str,
    rate_as_of: datetime,
) -> PlanningComparison:
    """Compare measured monthly spend with the documented INR planning range.

    The exchange rate is display/reporting provenance, not a model cost estimate: model spend still
    comes solely from invocation records.
    """

    if isinstance(cost_usd_micros, bool) or not isinstance(cost_usd_micros, int):
        raise TypeError("cost_usd_micros must be a plain integer")
    if cost_usd_micros < 0:
        raise ValueError("cost_usd_micros must be non-negative")
    if not isinstance(usd_to_inr, Decimal):
        raise TypeError("usd_to_inr must be a Decimal")
    if not usd_to_inr.is_finite() or usd_to_inr <= 0:
        raise ValueError("usd_to_inr must be finite and positive")
    if not isinstance(rate_source, str) or not rate_source.strip():
        raise ValueError("rate_source must be a non-empty string")
    _aware(rate_as_of, name="rate_as_of")

    measured_inr = Decimal(cost_usd_micros) / USD_MICROS * usd_to_inr
    low, high = PLANNING_ESTIMATE_INR_MONTHLY
    if measured_inr < low:
        status = PlanningStatus.BELOW
    elif measured_inr > high:
        status = PlanningStatus.ABOVE
    else:
        status = PlanningStatus.WITHIN
    return PlanningComparison(
        measured_cost_usd_micros=cost_usd_micros,
        measured_cost_inr=measured_inr,
        estimate_inr_low=low,
        estimate_inr_high=high,
        status=status,
        usd_to_inr=usd_to_inr,
        rate_source=rate_source.strip(),
        rate_as_of=rate_as_of,
    )
