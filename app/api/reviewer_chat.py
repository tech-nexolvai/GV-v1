"""Run-scoped, fact-bounded reviewer chat.

This route reads the same live deterministic findings the reviewer sees in the list.  It does not
run checks, retrieve a drawing, or accept any value/verdict from a caller.  The optional provider
may only rewrite the selected stored facts; ``app.review.chat`` rejects prose that adds, removes, or
rejudges them and supplies the plain structured fallback on every provider failure.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Principal, require_project_access
from app.config import Settings
from app.models import CheckRun, Finding, Package, PackageRevision, RuleDefinition, RuleSnapshot
from app.review.chat import ChatReply, answer_question, narrate_selection, select_for_question
from app.review.chat_bedrock import configured_reviewer_chat
from app.review.chat_models import (
    ChatModelChoice,
    ChatModelNotAllowed,
    allowed_chat_models,
    default_chat_model,
    resolve_requested_model,
)
from app.runs.invocations import BedrockConverseInvocationRecorder
from workflow.findings_composer import ComposerFinding, ComposerOperand, reviewer_reason

router = APIRouter(tags=["reviewer chat"])
_log = logging.getLogger(__name__)
NOT_FOUND_DETAIL = "Not found"
MAX_QUESTION_LENGTH = 1000


class ReviewerChatRequest(BaseModel):
    """A reviewer question; it cannot carry findings, values, rules, or verdicts.

    ``model_id`` is an optional presentation choice — which allow-listed model narrates. It changes
    no fact: the answer is still composed from stored findings and the narration guard is unchanged.
    An id the deployment did not allow-list is refused (422), so a reviewer cannot select an
    unapproved model. ``None`` uses the deployment default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    model_id: str | None = Field(default=None, min_length=1, max_length=300)


class ChatModelsOut(BaseModel):
    """The models a reviewer may choose from, and which one answers by default."""

    model_config = ConfigDict(frozen=True)

    models: tuple[ChatModelChoice, ...]
    default: str | None = None


class ReviewerChatNarrative(BaseModel):
    """One answer fragment and the deterministic finding that backs it."""

    model_config = ConfigDict(frozen=True)

    finding_id: UUID
    text: str


class ReviewerChatOut(BaseModel):
    """Bounded chat response; every narrative has a finding id from this exact run."""

    model_config = ConfigDict(frozen=True)

    answer: str
    mode: str
    model_id: str | None = None
    fallback_reason: str | None = None
    summary: str | None = None
    findings: tuple[ReviewerChatNarrative, ...]


@dataclass(frozen=True, slots=True)
class _LiveRunFacts:
    revision_id: UUID
    findings: tuple[ComposerFinding, ...]
    checks_have_run: bool


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _evidence_page(reference: object) -> str | None:
    """Use the recorded page label only when the stored reference is structurally complete."""
    if not isinstance(reference, str) or not reference:
        return None
    try:
        content = json.loads(reference)
        page = content["page"]
        document = content["document_version_id"]
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        return None
    if not isinstance(document, str) or not document.strip():
        return None
    # `pages.index` is zero-based, the way the reader addresses a document; a reviewer counts sheets
    # from one, and so does every other surface in the product (`EnterValuesPage`,
    # `ConfirmReadingsPage`). This endpoint was the exception, which put "Sheet 0 has the failure"
    # into an AI answer — a sheet that exists on no drawing.
    #
    # The comment here previously called the raw value intentional because this endpoint "does not
    # calculate a new page number or infer a sheet title". Not inferring a *title* is right and
    # unchanged. Converting an index to the page number it denotes is not an inference; it is the
    # same page, written the way the person reading it counts.
    return str(page + 1)


def _operands(trace: Mapping[str, object]) -> tuple[ComposerOperand, ...]:
    raw = trace.get("operands")
    if not isinstance(raw, list):
        return ()
    return tuple(
        ComposerOperand(
            name=_text(item.get("name")),
            value=_text(item.get("value")),
            source=_text(item.get("source")),
            evidence_page=_evidence_page(item.get("evidence_ref")),
        )
        for item in raw
        if isinstance(item, Mapping)
    )


def _facts(
    finding: Finding,
    definition: RuleDefinition,
    check_name: str,
    rule_description: str | None = None,
) -> ComposerFinding:
    """Project exactly the stored deterministic record into the language-only schema."""
    trace = finding.trace
    operands = _operands(trace)
    pages = tuple(dict.fromkeys(item.evidence_page for item in operands if item.evidence_page))
    return ComposerFinding(
        key=str(finding.id),
        check=definition.rule_id,
        check_name=check_name,
        outcome=finding.outcome,
        severity=finding.severity,
        reason=reviewer_reason(
            finding.reason or _text(trace.get("reason")) or "No reason was recorded.",
            finding.outcome,
        ),
        comparison=_text(trace.get("comparison")) or None,
        # The stored comparison and operands already carry exact rendered values.  This endpoint does
        # not rebuild a delta from rational columns or do arithmetic just to make chat more verbose.
        difference=None,
        tolerance=_text(trace.get("tolerance")) or None,
        arithmetic_unit=_text(trace.get("arithmetic_unit")) or None,
        operands=operands,
        evidence_pages=pages,
        notes=() if finding.notes is None else tuple(finding.notes),
        # The published rule's own account of what it checks. Context, so an answer can say what a
        # check is *for* rather than only what it concluded — the snapshot was already being parsed
        # here for its name, and the description was sitting unused beside it.
        rule_description=rule_description,
    )


def _checks_have_run(session: Session, revision_id: UUID) -> bool:
    """Whether any check has actually been run on this revision.

    **Separate from "are there findings", because an empty list means two different things.** A
    package uploaded and never checked and a package checked with nothing wrong both produce no
    findings, and answering them the same way told a reviewer "No FAIL findings in this run" about a
    drawing nothing had looked at — which reads as a clean bill of health. The presence of an
    unsuperseded `CheckRun` is what separates them, and it is a row rather than an inference.
    """
    return (
        session.execute(
            select(CheckRun.id)
            .where(CheckRun.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _live_run_facts(session: Session, project_id: UUID, package_id: UUID) -> _LiveRunFacts | None:
    """One revision's unsuperseded findings and whether anything was checked, or no such package.

    Both, because the findings alone cannot tell the caller which of the two empty cases it is in.
    """
    revision = session.execute(
        select(PackageRevision.id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        return None

    checked = _checks_have_run(session, revision)
    rows = session.execute(
        select(Finding, RuleDefinition, RuleSnapshot.canonical_json)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision,
            CheckRun.superseded_at.is_(None),
        )
        .order_by(Finding.created_at, Finding.id)
    ).all()
    result: list[ComposerFinding] = []
    for finding, definition, canonical_json in rows:
        name: object = None
        description: object = None
        try:
            payload = json.loads(canonical_json)
            if isinstance(payload, Mapping):
                name = payload.get("name")
                description = payload.get("description")
        except (TypeError, ValueError):
            # A snapshot that will not parse still has a finding worth showing. The rule id is a
            # usable heading and the description is simply absent, which is what `None` means.
            pass
        check_name = name if isinstance(name, str) and name.strip() else definition.rule_id
        result.append(
            _facts(
                finding,
                definition,
                check_name,
                (
                    description.strip()
                    if isinstance(description, str) and description.strip()
                    else None
                ),
            )
        )
    return _LiveRunFacts(revision_id=revision, findings=tuple(result), checks_have_run=checked)


@router.get(
    "/projects/{project_id}/packages/{package_id}/chat/models",
    response_model=ChatModelsOut,
    summary="The models a reviewer may pick to narrate this run",
)
def reviewer_chat_models(
    request: Request,
    _: Annotated[Principal, Depends(require_project_access)],
    project_id: UUID,
    package_id: UUID,
) -> ChatModelsOut:
    """List the allow-listed narration models and the default, for the chat model picker.

    An empty list means the deployment configured no chat model; the chat then serves its plain
    deterministic findings view and the picker has nothing to offer.
    """
    settings = request.app.state.settings
    return ChatModelsOut(
        models=allowed_chat_models(settings.bedrock_chat_models, settings.bedrock_model),
        default=default_chat_model(settings.bedrock_chat_models, settings.bedrock_model),
    )


def _resolve_model(settings: Settings, requested: str | None) -> str | None:
    """The allow-listed model to narrate with, or 422. A model id is untrusted input."""
    try:
        return resolve_requested_model(
            settings.bedrock_chat_models, settings.bedrock_model, requested
        )
    except ChatModelNotAllowed:
        # The reviewer asked for a model the deployment did not allow-list. Refuse rather than
        # invoke it — the allow-list is the whole point.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="that model is not available for this deployment",
        ) from None


def _reply_out(reply: ChatReply, facts: tuple[ComposerFinding, ...]) -> ReviewerChatOut:
    """The published form of a reply, refusing any narrative without a stored finding.

    Shared by both endpoints so the streaming one cannot publish what the plain one would refuse.
    This is redundant with ``compose_findings`` deliberately: an API response with an unbacked id
    must be impossible even if a future chat adapter bypasses that helper by accident.
    """
    keys = {item.key for item in facts}
    if any(item.finding_key not in keys for item in reply.narratives):
        raise RuntimeError("reviewer chat attempted to return a narrative without a stored finding")
    return ReviewerChatOut(
        answer=reply.text,
        mode=reply.mode.value,
        model_id=reply.model_id,
        fallback_reason=reply.fallback_reason,
        summary=reply.summary,
        findings=tuple(
            ReviewerChatNarrative(finding_id=UUID(item.finding_key), text=item.text)
            for item in reply.narratives
        ),
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/chat",
    response_model=ReviewerChatOut,
    summary="Ask about one deterministic review run",
)
def reviewer_chat(
    body: ReviewerChatRequest,
    request: Request,
    _: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ReviewerChatOut:
    """Narrate the package's current findings without changing, calculating, or extending them."""
    live = _live_run_facts(session, project_id, package_id)
    if live is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    settings = request.app.state.settings
    chosen_model = _resolve_model(settings, body.model_id)

    reply: ChatReply = answer_question(
        body.question,
        live.findings,
        configured_reviewer_chat(
            settings,
            recorder=BedrockConverseInvocationRecorder(session, live.revision_id),
            model_id=chosen_model,
        ),
        checks_have_run=live.checks_have_run,
    )
    return _reply_out(reply, live.findings)


# ---------------------------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------------------------


class ChatFactsEvent(BaseModel):
    """Sent first: which recorded findings answer the question. Deterministic; no model involved.

    The reviewer sees these findings immediately, while the provider is still writing about them.
    ``narrating`` says whether a ``narration`` event with provider prose is still to come; when it
    is false the ``narration`` event that follows is the plain deterministic reply.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str
    finding_ids: tuple[UUID, ...]
    total: int
    narrating: bool


class ChatStageEvent(BaseModel):
    """Real progress, sent only when a provider is actually about to be called."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    model_id: str | None


class ChatErrorEvent(BaseModel):
    """A generic failure. Never carries exception text, which may quote a prompt or a provider."""

    model_config = ConfigDict(extra="forbid")

    detail: str


@dataclass(frozen=True, slots=True)
class _ChatContext:
    live: _LiveRunFacts
    chosen_model: str | None
    session: Session
    settings: Settings
    question: str


def _chat_context(
    body: ReviewerChatRequest,
    request: Request,
    _: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> _ChatContext:
    """Everything the stream needs, validated before it starts.

    **This has to be a dependency.** A generator endpoint's body does not run until the response
    has already begun with status 200, so a 404 or 422 raised inside it would reach the reviewer as
    a broken stream rather than an error. FastAPI resolves dependencies first, so these refusals
    are real HTTP errors.
    """
    live = _live_run_facts(session, project_id, package_id)
    if live is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    settings = request.app.state.settings
    return _ChatContext(
        live=live,
        chosen_model=_resolve_model(settings, body.model_id),
        session=session,
        settings=settings,
        question=body.question,
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/chat/stream",
    response_class=EventSourceResponse,
    summary="Ask about one deterministic review run, streamed",
)
def reviewer_chat_stream(
    context: Annotated[_ChatContext, Depends(_chat_context)],
) -> Iterator[ServerSentEvent]:
    """The same answer as ``/chat``, sent in the order it becomes known.

    1. ``facts``: the recorded findings the question selects, at once. No model is involved.
    2. ``stage``: only when a provider is about to be called.
    3. ``narration``: the **complete, guarded** reply, identical to ``/chat``'s response body.
    4. ``done``.

    **Why the narration is not streamed token by token.** The provider returns every narrative in
    one structured call, and ``compose_findings`` accepts or rejects that batch as a whole. A token
    stream would show a reviewer sentences the guard might then discard. So the provider's words
    are sent once, after the guard has accepted them; what streams is everything that did not
    need a model.

    Runs in a worker thread (FastAPI iterates a sync generator in its threadpool), and the request's
    session stays open until the stream ends, so the provider call is recorded exactly as ``/chat``
    records it.
    """
    live = context.live
    selection = select_for_question(
        context.question, live.findings, checks_have_run=live.checks_have_run
    )
    model = (
        None
        if selection.final is not None
        else configured_reviewer_chat(
            context.settings,
            recorder=BedrockConverseInvocationRecorder(context.session, live.revision_id),
            model_id=context.chosen_model,
        )
    )
    yield ServerSentEvent(
        event="facts",
        data=ChatFactsEvent(
            answer=selection.intro,
            finding_ids=tuple(UUID(item.key) for item in selection.selected),
            total=selection.total,
            narrating=model is not None,
        ),
    )
    try:
        if model is not None:
            yield ServerSentEvent(
                event="stage",
                data=ChatStageEvent(stage="narrating", model_id=context.chosen_model),
            )
        reply = narrate_selection(selection, context.question, model)
        yield ServerSentEvent(event="narration", data=_reply_out(reply, live.findings))
    except Exception:  # noqa: BLE001 - the stream has started; report and end it cleanly
        _log.exception("reviewer chat stream failed after the facts were sent")
        yield ServerSentEvent(
            event="error", data=ChatErrorEvent(detail="The explanation could not be produced.")
        )
        return
    yield ServerSentEvent(event="done", data={})
