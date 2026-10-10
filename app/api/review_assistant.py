"""The review assistant's two routes (#1128): what it offers, and one streamed answer.

Same access rule as the reviewer chat (`require_project_access`, and the package must belong to
the project), and the same refusal shape: a missing package is a real 404 from a dependency, never a
broken stream. The chat endpoints in `reviewer_chat.py` are unchanged.

The records are read with the screens' own functions (`_countertop_results_for_revision`,
`readiness_and_decisions`, the chat's live findings), so an answer can only cite what the
reviewer's screen shows.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.reviewer_chat import _live_run_facts
from app.api.visual_countertops import _countertop_results_for_revision
from app.auth import Principal, require_project_access
from app.config import Settings
from app.models import Package, PackageRevision
from app.review.approval import readiness_and_decisions
from app.review.assistant.contract import AssistantInfoOut, AssistantRequest
from app.review.assistant.model import OpenRouterAssistantModel
from app.review.assistant.records import ReviewSnapshot, build_snapshot
from app.review.assistant.service import AssistantRuntime, SessionLedger, stream_answer
from app.review.assistant.starters import starters

router = APIRouter(tags=["review assistant"])
NOT_FOUND_DETAIL: Final = "Not found"


def _latest_revision(
    session: Session, project_id: UUID, package_id: UUID
) -> PackageRevision | None:
    """The package's newest revision, only when the package belongs to this project."""
    return session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def load_snapshot(
    session: Session, project_id: UUID, package_id: UUID, revision: PackageRevision
) -> ReviewSnapshot:
    """This review's records, from the functions the countertop screen and sign-off use."""
    countertops = _countertop_results_for_revision(session, package_id, revision)
    readiness, records = readiness_and_decisions(session, revision.id)
    live = _live_run_facts(session, project_id, package_id)
    return build_snapshot(
        countertops,
        readiness,
        () if live is None else live.findings,
        records.decisions,
        checks_have_run=live is not None and live.checks_have_run,
    )


def _key(settings: Settings) -> str | None:
    key = settings.openrouter_api_key
    value = None if key is None else key.get_secret_value()
    return value if value and value.strip() else None


def runtime_for(request: Request) -> AssistantRuntime:
    """The deployment's assistant: the OpenRouter model when enabled, else none.

    A dependency, so a test replaces it with a fake model (no paid call is possible from a test).
    """
    settings: Settings = request.app.state.settings
    key = _key(settings)
    if not settings.review_assistant_enabled or key is None:
        return AssistantRuntime(
            model=None, max_history_turns=settings.review_assistant_max_history_turns
        )
    return AssistantRuntime(
        model=OpenRouterAssistantModel(
            key,
            model_id=settings.review_assistant_model,
            timeout_seconds=settings.review_assistant_timeout_seconds,
        ),
        max_history_turns=settings.review_assistant_max_history_turns,
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/assistant",
    response_model=AssistantInfoOut,
    summary="Whether the review assistant answers here, and starter questions for this review",
)
def review_assistant_info(
    _: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AssistantRuntime, Depends(runtime_for)],
    project_id: UUID,
    package_id: UUID,
) -> AssistantInfoOut:
    """Starters come from the records and work with the assistant off.

    `enabled` is read from the same runtime the stream uses, so it is true exactly when a
    question can reach the model."""
    revision = _latest_revision(session, project_id, package_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    snapshot = load_snapshot(session, project_id, package_id, revision)
    return AssistantInfoOut(
        enabled=runtime.model is not None,
        starters=starters(snapshot),
    )


@dataclass(frozen=True, slots=True)
class _AssistantContext:
    body: AssistantRequest
    session: Session
    project_id: UUID
    package_id: UUID
    revision: PackageRevision
    runtime: AssistantRuntime


def _assistant_context(
    body: AssistantRequest,
    _: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AssistantRuntime, Depends(runtime_for)],
    project_id: UUID,
    package_id: UUID,
) -> _AssistantContext:
    """Validated before the stream starts, so a missing package is a 404, not a broken stream."""
    revision = _latest_revision(session, project_id, package_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return _AssistantContext(
        body=body,
        session=session,
        project_id=project_id,
        package_id=package_id,
        revision=revision,
        runtime=runtime,
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/assistant/stream",
    response_class=EventSourceResponse,
    summary="Ask the review assistant about this review, streamed",
)
def review_assistant_stream(
    context: Annotated[_AssistantContext, Depends(_assistant_context)],
) -> Iterator[ServerSentEvent]:
    """`stage` events as each real step starts (records, model, guard), then `answer`, or `error`.

    Runs in a worker thread; the request's session stays open until the stream ends, and the one
    write (the model call's usage row) is committed as soon as it is made.
    """
    for name, data in stream_answer(
        context.body,
        load_snapshot=lambda: load_snapshot(
            context.session, context.project_id, context.package_id, context.revision
        ),
        runtime=context.runtime,
        ledger=SessionLedger(context.session, context.revision.id),
    ):
        yield ServerSentEvent(event=name, data=data)


__all__ = ["load_snapshot", "router", "runtime_for"]
