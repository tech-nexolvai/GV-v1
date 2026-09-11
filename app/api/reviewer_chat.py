"""Run-scoped, fact-bounded reviewer chat.

This route reads the same live deterministic findings the reviewer sees in the list.  It does not
run checks, retrieve a drawing, or accept any value/verdict from a caller.  The optional provider
may only rewrite the selected stored facts; ``app.review.chat`` rejects prose that adds, removes, or
rejudges them and supplies the plain structured fallback on every provider failure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Principal, require_project_access
from app.models import CheckRun, Finding, Package, PackageRevision, RuleDefinition, RuleSnapshot
from app.review.chat import ChatReply, answer_question
from app.review.chat_bedrock import configured_reviewer_chat
from workflow.findings_composer import ComposerFinding, ComposerOperand, reviewer_reason

router = APIRouter(tags=["reviewer chat"])
NOT_FOUND_DETAIL = "Not found"
MAX_QUESTION_LENGTH = 1000


class ReviewerChatRequest(BaseModel):
    """A reviewer question; it cannot carry findings, values, rules, or verdicts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)


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
    # It is intentionally the stored value: this endpoint labels evidence, it does not calculate a
    # new page number or infer a sheet title.
    return str(page)


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


def _facts(finding: Finding, definition: RuleDefinition, check_name: str) -> ComposerFinding:
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
    )


def _live_run_facts(
    session: Session, project_id: UUID, package_id: UUID
) -> tuple[ComposerFinding, ...] | None:
    """Return one package revision's unsuperseded deterministic findings, or no such package."""
    revision = session.execute(
        select(PackageRevision.id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        return None

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
        try:
            payload = json.loads(canonical_json)
            name = payload.get("name") if isinstance(payload, Mapping) else None
        except (TypeError, ValueError):
            name = None
        check_name = name if isinstance(name, str) and name.strip() else definition.rule_id
        result.append(_facts(finding, definition, check_name))
    return tuple(result)


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
    facts = _live_run_facts(session, project_id, package_id)
    if facts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    reply: ChatReply = answer_question(
        body.question,
        facts,
        configured_reviewer_chat(request.app.state.settings),
    )
    keys = {item.key for item in facts}
    # This is redundant with ``compose_findings`` deliberately: an API response with an unbacked id
    # must be impossible even if a future chat adapter bypasses that helper by accident.
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
