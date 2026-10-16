"""The review assistant's two routes (#1128): what it offers, and one streamed answer.

Same access rule as the reviewer chat (`require_project_access`, and the package must belong to
the project), and the same refusal shape: a missing package is a real 404 from a dependency, never a
broken stream. The chat endpoints in `reviewer_chat.py` are unchanged.

The records are read with the screens' own functions (`_countertop_results_for_revision`,
`readiness_and_decisions`, the chat's live findings), so an answer can only cite what the
reviewer's screen shows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import ARTIFACT_STORE_STATE, get_session
from app.api.reviewer_chat import _live_run_facts
from app.api.visual_countertops import _countertop_results_for_revision
from app.auth import Principal, require_project_access
from app.config import Settings
from app.models import Package, PackageRevision
from app.models.document import Document, DocumentVersion, PackageRevisionDocument
from app.review.approval import readiness_and_decisions
from app.review.assistant.agent import AgentFiles, AgentTools
from app.review.assistant.contract import AssistantInfoOut, AssistantRequest
from app.review.assistant.model import OpenRouterAssistantModel
from app.review.assistant.page_pictures import RevisionPagePictures
from app.review.assistant.records import ReviewSnapshot, build_snapshot
from app.review.assistant.sandbox import Workspace, open_sandbox
from app.review.assistant.service import (
    AssistantBudget,
    AssistantRuntime,
    SessionLedger,
    assistant_spent_micros,
    stream_answer,
)
from app.review.assistant.starters import starters
from app.review.review_items import review_items_for_revision
from app.runs.rates import rates_from_environment

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
    """This review's records, from the functions the countertop screen, sign-off and the simple
    review list (#1352) use."""
    countertops = _countertop_results_for_revision(session, package_id, revision)
    readiness, records = readiness_and_decisions(session, revision.id)
    live = _live_run_facts(session, project_id, package_id)
    return build_snapshot(
        countertops,
        readiness,
        () if live is None else live.findings,
        records.decisions,
        checks_have_run=live is not None and live.checks_have_run,
        review_items=review_items_for_revision(
            session, project_id=project_id, package_id=package_id, revision=revision
        ),
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
        page_pictures=settings.review_assistant_page_pictures,
        budget_usd=settings.review_assistant_budget_usd,
        question_cap_usd=settings.review_assistant_question_cap_usd,
        max_tool_turns=settings.review_assistant_max_tool_turns,
        agent_model=OpenRouterAssistantModel(
            key,
            model_id=settings.review_assistant_lead_model,
            timeout_seconds=settings.review_assistant_agent_timeout_seconds,
        ),
        delegate_cap_usd=settings.review_assistant_delegate_question_cap_usd,
        sandbox=settings.review_assistant_sandbox,
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
    store: object | None


def _assistant_context(
    request: Request,
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
        store=getattr(request.app.state, ARTIFACT_STORE_STATE, None),
    )


_MICROS: Final = Decimal(1_000_000)


def _pictures(context: _AssistantContext) -> RevisionPagePictures | None:
    """The asked page's pictures, when switched on and the drawings' store is configured."""
    if not context.runtime.page_pictures or context.store is None:
        return None
    return RevisionPagePictures(context.session, context.store, context.revision.id)


def _budget(context: _AssistantContext) -> AssistantBudget | None:
    """The package's assistant budget, from its recorded calls and the deployment's prices."""
    runtime = context.runtime
    if runtime.budget_usd is None or runtime.question_cap_usd is None:
        return None
    return AssistantBudget(
        package_cap_micros=int(runtime.budget_usd * _MICROS),
        question_cap_micros=int(runtime.question_cap_usd * _MICROS),
        delegate_cap_micros=(
            None if runtime.delegate_cap_usd is None else int(runtime.delegate_cap_usd * _MICROS)
        ),
        spent_micros=lambda: assistant_spent_micros(context.session, context.revision.id),
        rates=rates_from_environment(),
    )


def _revision_pdfs(context: _AssistantContext) -> list[tuple[str, bytes, str, int]]:
    """This revision's PDFs, from the store, checked against their hashes: (uploaded name, bytes,
    kind words, pages). Only this package revision's documents; nothing else is ever copied."""
    from app.api.documents import storage_key

    rows = context.session.execute(
        select(Document, DocumentVersion)
        .join(PackageRevisionDocument, PackageRevisionDocument.document_id == Document.id)
        .join(DocumentVersion, DocumentVersion.id == PackageRevisionDocument.document_version_id)
        .where(
            PackageRevisionDocument.package_revision_id == context.revision.id,
            PackageRevisionDocument.package_id == context.package_id,
        )
        .order_by(Document.kind.desc(), Document.id)
    ).all()
    store: Any = context.store
    files: list[tuple[str, bytes, str, int]] = []
    for document, version in rows:
        with store.get(storage_key(version.document_id, version.sha256)) as stored:
            data: bytes = stored.read()
        if hashlib.sha256(data).hexdigest() != version.sha256:
            continue
        files.append(
            (
                version.file_name or f"{document.kind}.pdf",
                data,
                _KIND_WORDS.get(document.kind, document.kind),
                version.page_count,
            )
        )
    return files


_KIND_WORDS: Final = {
    "shop": "the vendor's shop drawings (a sheet may carry the architect's drawing beside the "
    "vendor's, with GV's markup on top)",
    "architectural": "the architect's drawings",
}


def _ai_review_and_job_values(context: _AssistantContext, page: int | None) -> dict[str, Any]:
    """The AI page review rows (one page) or its per-page summary (whole package), and the job's
    values: the ones typed for the project and what the job values reader found."""
    from app.api.company_settings import _project_set, _text
    from app.review.ai_review import ai_review_for_revision
    from app.review.review_items import _job_value_suggestions

    review = ai_review_for_revision(context.session, context.revision.id)
    fields = {
        "key",
        "measures",
        "architect_text",
        "vendor_text",
        "result",
        "why",
        "status",
        "clouded",
        "verified_by",
        "evaluator_reason",
    }
    pages: list[dict[str, Any]] = []
    for sheet in review.pages:
        if page is not None and sheet.page != page:
            continue
        rows = [row.model_dump(mode="json", include=fields) for row in sheet.rows]
        if page is None:
            counts: dict[str, int] = {}
            for row in sheet.rows:
                counts[row.result] = counts.get(row.result, 0) + 1
            rows = [row for row in rows if row["result"] == "different"]
            pages.append({"page": sheet.page, "status": sheet.status, **counts, "different": rows})
        else:
            pages.append({"page": sheet.page, "status": sheet.status, "rows": rows})
    _version, values, _rows = _project_set(context.session, context.project_id)
    typed = {name: _text(value) for name, value in sorted(values.items())}
    found = [
        item.model_dump(mode="json", exclude={"location"})
        for item in _job_value_suggestions(context.session, context.revision.id)
        if page is None or item.page == page
    ]
    return {
        "ai_page_review": pages,
        "job_values_typed_for_this_project": typed,
        "job_values_found_on_the_drawings": found,
    }


def _agent_tools(
    context: _AssistantContext,
) -> Callable[[ReviewSnapshot], AgentTools | None] | None:
    """The tool-using assistant's sandbox over this package's PDFs, when switched on."""
    runtime = context.runtime
    if runtime.sandbox == "off" or context.store is None or runtime.model is None:
        return None

    def opened(snapshot: ReviewSnapshot) -> AgentTools | None:
        pdfs = _revision_pdfs(context)
        if not pdfs:
            return None
        workspace = Workspace.create([(name, data) for name, data, _kind, _pages in pdfs])
        sandbox = open_sandbox(workspace, prefer=runtime.sandbox)
        if sandbox is None:
            workspace.close()
            return None
        files = tuple(
            AgentFiles(name=name, kind=kind, pages=pages)
            for name, (_n, _d, kind, pages) in zip(workspace.file_names, pdfs, strict=True)
        )
        return AgentTools(
            sandbox=sandbox,
            snapshot=snapshot,
            files=files,
            extra_results=lambda page: _ai_review_and_job_values(context, page),
        )

    return opened


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
        pictures=_pictures(context),
        budget=_budget(context),
        agent_tools=_agent_tools(context),
    ):
        yield ServerSentEvent(event=name, data=data)


__all__ = ["load_snapshot", "router", "runtime_for"]
