"""Durable, package-scoped model-call and token ceilings.

Usage is calculated from append-only ``model_invocations`` rows rather than an in-memory
counter. A process restart therefore cannot erase spend. This module reports whether a proposed
call fits; issue #265 owns the downstream REVIEW REQUIRED behavior when it does not.

The pre-call check is deliberately honest about its boundary: it reads committed invocation
records. It does not reserve unknown provider tokens or make a later model call atomic with this
query. Callers must record every attempt, including failures, through ``app.runs.invocations``.

Source: ``docs/DESIGN_CONTROLS.md`` section 5 and issue #264.
Verification: ``tests/budget/test_ceiling.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun


class CeilingLayer(StrEnum):
    """The same precedence order used by A7 parameters."""

    GLOBAL = "global"
    PROJECT = "project"
    RUN = "run"


class CeilingState(StrEnum):
    """Observable state of package usage relative to its configured ceiling."""

    AVAILABLE = "available"
    APPROACHING = "approaching"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class Ceiling:
    """Maximum recorded model calls and tokens permitted for one package revision."""

    max_model_calls: int
    max_tokens: int

    def __post_init__(self) -> None:
        _positive_integer("max_model_calls", self.max_model_calls)
        _positive_integer("max_tokens", self.max_tokens)


@dataclass(frozen=True, slots=True)
class ResolvedCeiling:
    """A ceiling plus the highest-precedence layer that supplied it."""

    ceiling: Ceiling
    layer: CeilingLayer


@dataclass(frozen=True, slots=True)
class Usage:
    """Committed provider usage recorded for one package revision."""

    model_calls: int
    tokens: int

    def __post_init__(self) -> None:
        _non_negative_integer("model_calls", self.model_calls)
        _non_negative_integer("tokens", self.tokens)


@dataclass(frozen=True, slots=True)
class Remaining:
    """Result of checking proposed spend against a resolved ceiling."""

    usage: Usage
    calls_remaining: int
    tokens_remaining: int
    permitted: bool
    state: CeilingState


def _positive_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def resolve_ceiling(
    project_id: UUID,
    run_id: UUID,
    *,
    global_ceiling: Ceiling,
    project_ceilings: Mapping[UUID, Ceiling],
    run_ceilings: Mapping[UUID, Ceiling],
) -> ResolvedCeiling:
    """Resolve GLOBAL -> PROJECT -> RUN, with the last matching layer winning.

    All inputs are required. In particular, there is no hidden global number: operational
    defaults belong in configuration and must be passed here visibly.
    """

    if run_id in run_ceilings:
        return ResolvedCeiling(run_ceilings[run_id], CeilingLayer.RUN)
    if project_id in project_ceilings:
        return ResolvedCeiling(project_ceilings[project_id], CeilingLayer.PROJECT)
    return ResolvedCeiling(global_ceiling, CeilingLayer.GLOBAL)


def recorded_usage(session: Session, package_revision_id: UUID) -> Usage:
    """Read durable usage for every region and workflow run of a package revision."""

    statement = (
        select(
            func.count(ModelInvocation.id),
            func.coalesce(
                func.sum(ModelInvocation.input_tokens + ModelInvocation.output_tokens), 0
            ),
        )
        .select_from(ModelInvocation)
        .join(ExtractionRun, ModelInvocation.extraction_run_id == ExtractionRun.id)
        .join(TaskRun, ExtractionRun.task_run_id == TaskRun.id)
        .join(WorkflowRun, TaskRun.workflow_run_id == WorkflowRun.id)
        .where(WorkflowRun.package_revision_id == package_revision_id)
    )
    calls, tokens = session.execute(statement).one()
    return Usage(model_calls=int(calls), tokens=int(tokens))


def consume(
    session: Session,
    package_revision_id: UUID,
    *,
    ceiling: Ceiling,
    calls: int,
    tokens: int,
    approaching_at: Decimal,
) -> Remaining:
    """Assess proposed spend before a call, without mutating the durable ledger.

    ``approaching_at`` is required because it is an empirical alert threshold. It must be a
    finite ratio greater than zero and no greater than one; this module never invents one.
    ``tokens`` is the amount known before the call (normally the assembled input). Actual provider
    usage is counted after the invocation is recorded.
    """

    _non_negative_integer("calls", calls)
    _non_negative_integer("tokens", tokens)
    if not isinstance(approaching_at, Decimal):
        raise TypeError("approaching_at must be a Decimal")
    if not approaching_at.is_finite() or approaching_at <= 0 or approaching_at > 1:
        raise ValueError("approaching_at must be a finite Decimal in (0, 1]")

    usage = recorded_usage(session, package_revision_id)
    projected_calls = usage.model_calls + calls
    projected_tokens = usage.tokens + tokens
    calls_remaining = max(0, ceiling.max_model_calls - projected_calls)
    tokens_remaining = max(0, ceiling.max_tokens - projected_tokens)
    permitted = (
        projected_calls <= ceiling.max_model_calls and projected_tokens <= ceiling.max_tokens
    )

    if not permitted or calls_remaining == 0 or tokens_remaining == 0:
        state = CeilingState.EXHAUSTED
    elif (
        Decimal(projected_calls) / Decimal(ceiling.max_model_calls) >= approaching_at
        or Decimal(projected_tokens) / Decimal(ceiling.max_tokens) >= approaching_at
    ):
        state = CeilingState.APPROACHING
    else:
        state = CeilingState.AVAILABLE

    return Remaining(
        usage=usage,
        calls_remaining=calls_remaining,
        tokens_remaining=tokens_remaining,
        permitted=permitted,
        state=state,
    )
