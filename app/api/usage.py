"""Project-scoped model usage totals with no prompt or response fields."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.config import Settings
from app.models import (
    AiSpendHistory,
    ExtractionRun,
    ModelInvocation,
    Package,
    PackageRevision,
    PackageState,
    PackageStateEvent,
    TaskRun,
    WorkflowRun,
)
from app.models.runs import made_a_call
from app.provider_usage import OpenRouterUsageCheck, ProviderCheck
from app.schemas.visual_ui import (
    ModelUsageOut,
    PackageReadingTimeOut,
    ProviderCheckOut,
    ProviderChecksOut,
    ReadingTimeOut,
    UsageGroupOut,
    UsageHistoryOut,
    UsageOut,
    UsageTotalsOut,
)
from app.usage_history import PURPOSES, charged_cost, infer_route

router = APIRouter(tags=["visual reviewer"])


@dataclass(frozen=True, slots=True)
class _InvocationUsage:
    created_at: datetime
    extraction_run_id: UUID | None
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    outcome: str


#: Where a reading hands the revision to a person: ready for review, or waiting for a reviewer to
#: confirm or type values (also where an AI budget stop lands). What happens after is not reading.
_READING_HANDOVERS: frozenset[str] = frozenset(
    {PackageState.AWAITING_REVIEW.value, PackageState.NEEDS_INPUT.value}
)

#: Stops nothing resumes from: the reading failed here, whatever happens to the revision later.
_READING_FINAL_STOPS: frozenset[str] = frozenset(
    {
        PackageState.FAILED_PERMANENT.value,
        PackageState.CANCELLED.value,
        PackageState.SUPERSEDED.value,
    }
)


def _reading_times(
    session: Session, project_id: UUID, from_: datetime | None, to: datetime | None
) -> tuple[ReadingTimeOut, ...]:
    """Each revision's reading, from its state events, in one query (#1071).

    Only events from each revision's first `EXTRACTING` onwards are fetched; the walk in Python
    stops at the hand-over or the failure that ended the reading. `ReadingTimeOut` says what the times mean.
    """
    first_extract_query = (
        select(
            PackageStateEvent.package_revision_id.label("revision_id"),
            func.min(PackageStateEvent.sequence).label("sequence"),
        )
        .join(PackageRevision, PackageRevision.id == PackageStateEvent.package_revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(
            Package.project_id == project_id,
            PackageStateEvent.to_state == PackageState.EXTRACTING.value,
        )
        .group_by(PackageStateEvent.package_revision_id)
    )
    # The window selects readings by when they started, the first EXTRACTING event.
    if from_ is not None:
        first_extract_query = first_extract_query.having(
            func.min(PackageStateEvent.created_at) >= from_
        )
    if to is not None:
        first_extract_query = first_extract_query.having(
            func.min(PackageStateEvent.created_at) < to
        )
    first_extract = first_extract_query.subquery()
    rows = session.execute(
        select(
            Package.id.label("package_id"),
            PackageRevision.id.label("revision_id"),
            PackageRevision.revision_number,
            PackageStateEvent.to_state,
            PackageStateEvent.created_at,
        )
        .select_from(PackageStateEvent)
        .join(
            first_extract,
            and_(
                first_extract.c.revision_id == PackageStateEvent.package_revision_id,
                PackageStateEvent.sequence >= first_extract.c.sequence,
            ),
        )
        .join(PackageRevision, PackageRevision.id == PackageStateEvent.package_revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .order_by(PackageRevision.id, PackageStateEvent.sequence)
    ).all()

    readings: list[ReadingTimeOut] = []
    index = 0
    while index < len(rows):
        start = rows[index]
        finished_at: datetime | None = None
        end_state: str | None = None
        outcome: Literal["finished", "failed", "reading"] = "reading"
        done = False
        while index < len(rows) and rows[index].revision_id == start.revision_id:
            row = rows[index]
            index += 1
            if done:
                continue
            if row.to_state in _READING_HANDOVERS:
                finished_at, end_state, outcome = row.created_at, row.to_state, "finished"
                done = True
            elif row.to_state in _READING_FINAL_STOPS:
                # A retryable failure followed by a final stop failed at the first one.
                if outcome != "failed":
                    finished_at, end_state, outcome = row.created_at, row.to_state, "failed"
                done = True
            elif row.to_state == PackageState.FAILED_RETRYABLE.value:
                finished_at, end_state, outcome = row.created_at, row.to_state, "failed"
            else:
                # Still in (or resumed into) the pipeline.
                finished_at, end_state, outcome = None, None, "reading"
        started_at: datetime = start.created_at
        readings.append(
            ReadingTimeOut(
                package_id=start.package_id,
                revision_id=start.revision_id,
                revision_number=start.revision_number,
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome,
                end_state=end_state,
                duration_ms=(
                    None
                    if finished_at is None
                    else (finished_at - started_at) // timedelta(milliseconds=1)
                ),
            )
        )
    return tuple(sorted(readings, key=lambda item: (item.started_at, str(item.revision_id))))


def _invocation_revision() -> Subquery:
    """Each call's package revision: its own, or its extraction run's (chat and assistant calls
    carry their own; reading calls reach it through the run)."""
    return (
        select(
            ModelInvocation.id.label("invocation_id"),
            func.coalesce(
                ModelInvocation.package_revision_id, WorkflowRun.package_revision_id
            ).label("revision_id"),
        )
        .select_from(ModelInvocation)
        .outerjoin(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
        .outerjoin(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .outerjoin(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .subquery()
    )


def _usd(micros: int) -> str:
    return format((Decimal(micros) / Decimal(1_000_000)).quantize(Decimal("0.000001")), "f")


@router.get(
    "/projects/{project_id}/usage", response_model=UsageOut, summary="Read project AI usage"
)
def project_usage(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    group_by: Annotated[Literal["day", "package"], Query()] = "day",
) -> UsageOut:
    del principal
    if from_ is not None and to is not None and from_ > to:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="from must be before to"
        )
    invocation_revision = _invocation_revision()
    base = (
        select(
            ModelInvocation.created_at,
            ModelInvocation.extraction_run_id,
            ModelInvocation.model_id,
            ModelInvocation.input_tokens,
            ModelInvocation.output_tokens,
            ModelInvocation.cost_micros,
            ModelInvocation.outcome,
            invocation_revision.c.revision_id,
            Package.id.label("package_id"),
        )
        .select_from(ModelInvocation)
        .join(invocation_revision, invocation_revision.c.invocation_id == ModelInvocation.id)
        .join(PackageRevision, PackageRevision.id == invocation_revision.c.revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.project_id == project_id)
        # A reused stored answer (#1112) made no call: it is not usage.
        .where(made_a_call())
    )
    if from_ is not None:
        base = base.where(ModelInvocation.created_at >= from_)
    if to is not None:
        base = base.where(ModelInvocation.created_at < to)
    rows = session.execute(base.order_by(ModelInvocation.created_at, ModelInvocation.id)).all()
    grouped: dict[str, dict[str, list[_InvocationUsage]]] = defaultdict(lambda: defaultdict(list))
    per_run: dict[tuple[UUID, UUID], list[datetime]] = defaultdict(list)
    all_calls: list[_InvocationUsage] = []
    for row in rows:
        invocation = _InvocationUsage(
            created_at=row.created_at,
            extraction_run_id=row.extraction_run_id,
            model_id=row.model_id,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            # Counted as Spend so far counts it (#1165): a pre-#754 zero with tokens is priced from
            # the published rates or unpriced, never free.
            cost_micros=charged_cost(
                row.model_id, row.cost_micros, row.outcome, row.input_tokens, row.output_tokens
            ).micros,
            outcome=row.outcome,
        )
        all_calls.append(invocation)
        package_id: UUID = row.package_id
        key = invocation.created_at.date().isoformat() if group_by == "day" else str(package_id)
        grouped[key][invocation.model_id].append(invocation)
        if invocation.extraction_run_id is not None:
            per_run[(package_id, invocation.extraction_run_id)].append(invocation.created_at)

    def totals(calls: list[_InvocationUsage]) -> UsageTotalsOut:
        return UsageTotalsOut(
            calls=len(calls),
            failed_calls=sum(invocation.outcome != "ok" for invocation in calls),
            input_tokens=sum(invocation.input_tokens for invocation in calls),
            output_tokens=sum(invocation.output_tokens for invocation in calls),
            cost_usd=_usd(sum(invocation.cost_micros or 0 for invocation in calls)),
            unpriced_calls=sum(invocation.cost_micros is None for invocation in calls),
        )

    groups: list[UsageGroupOut] = []
    for key in sorted(grouped):
        models = tuple(
            ModelUsageOut(**totals(calls).model_dump(), model=model)
            for model, calls in sorted(grouped[key].items())
        )
        calls = [invocation for values in grouped[key].values() for invocation in values]
        groups.append(
            UsageGroupOut(
                **totals(calls).model_dump(),
                day=date.fromisoformat(key) if group_by == "day" else None,
                package_id=UUID(key) if group_by == "package" else None,
                models=models,
            )
        )
    durations: list[PackageReadingTimeOut] = []
    run_ids = [run_id for _, run_id in per_run]
    run_to_revision: dict[UUID, UUID] = {}
    if run_ids:
        run_rows = session.execute(
            select(ExtractionRun.id, WorkflowRun.package_revision_id)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .where(ExtractionRun.id.in_(run_ids))
        ).all()
        run_to_revision = {run_id: revision_id for run_id, revision_id in run_rows}
    for (package_id, run_id), timestamps in sorted(
        per_run.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
    ):
        first, last = min(timestamps), max(timestamps)
        durations.append(
            PackageReadingTimeOut(
                package_id=package_id,
                revision_id=run_to_revision[run_id],
                run_id=run_id,
                first_call_at=first,
                last_call_at=last,
                duration_ms=(last - first).days * 86_400_000
                + (last - first).seconds * 1000
                + (last - first).microseconds // 1000,
            )
        )
    return UsageOut.model_validate(
        {
            "from": from_,
            "to": to,
            "group_by": group_by,
            "totals": totals(all_calls),
            "groups": tuple(groups),
            "package_reading_times": tuple(durations),
            "reading_times": _reading_times(session, project_id, from_, to),
        }
    )


@dataclass(slots=True)
class _Tally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    unpriced_calls: int = 0
    priced_later_calls: int = 0
    priced_later_micros: int = 0

    def add(self, other: _Tally) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_micros += other.cost_micros
        self.unpriced_calls += other.unpriced_calls
        self.priced_later_calls += other.priced_later_calls
        self.priced_later_micros += other.priced_later_micros

    def out(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": _usd(self.cost_micros),
            "unpriced_calls": self.unpriced_calls,
            "priced_later_calls": self.priced_later_calls,
            "priced_later_cost_usd": _usd(self.priced_later_micros),
        }


def _call_tally(
    input_tokens: int, output_tokens: int, cost: int | None, priced_later: bool
) -> _Tally:
    return _Tally(
        calls=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_micros=cost or 0,
        unpriced_calls=int(cost is None),
        priced_later_calls=int(priced_later),
        priced_later_micros=(cost or 0) if priced_later else 0,
    )


@router.get(
    "/projects/{project_id}/usage/history",
    response_model=UsageHistoryOut,
    summary="Read all AI spending so far: this project's calls and earlier runs",
)
def project_usage_history(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
) -> UsageHistoryOut:
    del principal
    invocation_revision = _invocation_revision()
    calls = session.execute(
        select(
            ModelInvocation.model_id,
            ModelInvocation.prompt_id,
            ModelInvocation.input_tokens,
            ModelInvocation.output_tokens,
            ModelInvocation.cost_micros,
            ModelInvocation.outcome,
        )
        .select_from(ModelInvocation)
        .join(invocation_revision, invocation_revision.c.invocation_id == ModelInvocation.id)
        .join(PackageRevision, PackageRevision.id == invocation_revision.c.revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.project_id == project_id)
        .where(made_a_call())
    ).all()
    by_model: dict[tuple[str, str], _Tally] = defaultdict(_Tally)
    this_project = _Tally()
    for call in calls:
        cost = charged_cost(
            call.model_id, call.cost_micros, call.outcome, call.input_tokens, call.output_tokens
        )
        tally = _call_tally(call.input_tokens, call.output_tokens, cost.micros, cost.priced_later)
        this_project.add(tally)
        by_model[(call.model_id, infer_route(call.model_id, call.prompt_id))].add(tally)

    # The history is one row per call; it is added up here, by group, in the database.
    history = session.execute(
        select(
            AiSpendHistory.occurred_on,
            AiSpendHistory.purpose,
            AiSpendHistory.source_label,
            AiSpendHistory.model_id,
            AiSpendHistory.route,
            func.count().label("calls"),
            func.coalesce(func.sum(AiSpendHistory.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(AiSpendHistory.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(AiSpendHistory.cost_micros), 0).label("cost_micros"),
            func.count().filter(AiSpendHistory.cost_micros.is_(None)).label("unpriced"),
            func.count().filter(AiSpendHistory.priced_later).label("priced_later"),
            func.coalesce(
                func.sum(AiSpendHistory.cost_micros).filter(AiSpendHistory.priced_later), 0
            ).label("priced_later_micros"),
        )
        .where(AiSpendHistory.project_id == project_id)
        .group_by(
            AiSpendHistory.occurred_on,
            AiSpendHistory.purpose,
            AiSpendHistory.source_label,
            AiSpendHistory.model_id,
            AiSpendHistory.route,
        )
    ).all()
    earlier = _Tally()
    runs: dict[tuple[date, str, str], tuple[_Tally, set[str]]] = {}
    for row in history:
        tally = _Tally(
            calls=int(row.calls),
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
            cost_micros=int(row.cost_micros),
            unpriced_calls=int(row.unpriced),
            priced_later_calls=int(row.priced_later),
            priced_later_micros=int(row.priced_later_micros),
        )
        earlier.add(tally)
        by_model[(row.model_id, row.route)].add(tally)
        run_tally, models = runs.setdefault(
            (row.occurred_on, row.purpose, row.source_label), (_Tally(), set())
        )
        run_tally.add(tally)
        models.add(row.model_id)

    totals = _Tally()
    totals.add(this_project)
    totals.add(earlier)
    purpose_order: dict[str, int] = {purpose: index for index, purpose in enumerate(PURPOSES)}
    return UsageHistoryOut.model_validate(
        {
            "totals": totals.out(),
            "this_project": this_project.out(),
            "earlier": earlier.out(),
            "by_model": tuple(
                {**tally.out(), "model": model, "route": route}
                for (model, route), tally in sorted(
                    by_model.items(),
                    key=lambda item: (-item[1].cost_micros, -item[1].calls, item[0]),
                )
            ),
            "earlier_runs": tuple(
                {
                    **tally.out(),
                    "day": day,
                    "purpose": purpose,
                    "source_label": label,
                    "models": tuple(sorted(models)),
                }
                for (day, purpose, label), (tally, models) in sorted(
                    runs.items(),
                    key=lambda item: (
                        -item[0][0].toordinal(),
                        purpose_order.get(item[0][1], len(purpose_order)),
                        item[0][2],
                    ),
                )
            ),
        }
    )


def provider_usage_check(request: Request) -> OpenRouterUsageCheck | None:
    """OpenRouter's report of this key's usage, only when switched on and a key is set.

    One checker per app, so its five-minute memory is shared by every request. A dependency, so a
    test replaces it with a fake transport (no request ever leaves a test).
    """
    settings: Settings = request.app.state.settings
    key = settings.openrouter_api_key
    value = None if key is None else key.get_secret_value()
    if not settings.usage_provider_check or not value or not value.strip():
        return None
    existing = getattr(request.app.state, "openrouter_usage_check", None)
    if isinstance(existing, OpenRouterUsageCheck):
        return existing
    check = OpenRouterUsageCheck(value.strip())
    request.app.state.openrouter_usage_check = check
    return check


@router.get(
    "/projects/{project_id}/usage/provider-check",
    response_model=ProviderChecksOut,
    summary="Read what the AI provider reports this deployment's key has used (all projects)",
)
def project_usage_provider_check(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    provider_check: Annotated[OpenRouterUsageCheck | None, Depends(provider_usage_check)],
    project_id: UUID,
) -> ProviderChecksOut:
    """Its own endpoint, so the recorded totals never wait for a provider or fail with it."""
    del principal, project_id
    if provider_check is None:
        return ProviderChecksOut(checks=())
    check: ProviderCheck = provider_check.check()
    return ProviderChecksOut(
        checks=(
            ProviderCheckOut(
                provider="openrouter",
                status=check.status,
                used_usd=check.used_usd,
                checked_at=check.checked_at,
            ),
        )
    )


__all__ = [
    "project_usage",
    "project_usage_history",
    "project_usage_provider_check",
    "provider_usage_check",
    "router",
]
