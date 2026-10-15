"""A person asks to read the depths again on a drawing set that was already read (#1308).

**Why.** The depth and sink reader (`GV_DEPTH_READER`, #1290) runs only inside the match stage of
a set's first reading. A set read before the reader existed or was switched on had no way to get
depth readings: a new reading request is skipped because that stage is done, and "Read the unread
pages" never reaches the reader.

**Two routes, one action.** `GET .../read-depths-again` says whether the action is offered (only
with the switch on), why it would be refused, about how many questions it would ask and how many
of them already have a stored answer, and what became of the last request. `POST` queues the work
(`workflow.depth_again.READ_DEPTHS_AGAIN_WORKFLOW`) in the caller's transaction (the outbox), as
"Read the unread pages" does (`app/api/reading_again.py`); the worker runs only Bala's suggestions
and the reader, then the checks again (`DatabaseStages.read_depths_again`).

**A paid action, refused unless it can run as the reader's first run does**: the switch, the
Claude readers and stored answers reused must be on, and the set must be with the reviewer (not
being read or checked, not signed off). It is also refused when a call had no price, or when the
set's AI budget is used up while a question has no stored answer: it could only stop part way.
`GET` says how much of the budget is left (the worker's own cap). One request at a time per set. Audited as a reviewer
action, and only someone who may start a review may ask.

This module reads nothing and imports no reader (`tests/api/test_no_heavy_work.py`).

Verification: `tests/api/test_read_depths_again.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.documents import _package_revision
from app.api.signed_off import open_revision
from app.audit.events import AuditCategory, emit
from app.auth import Action, Principal, require_action, require_project_access
from app.config import Settings
from app.models import PackageRevision
from workflow.ai_budget import ai_budget_from_settings, claude_ceiling
from workflow.depth_again import (
    ALREADY_ASKED,
    READ_DEPTHS_AGAIN_WORKFLOW,
    SpendLimits,
    budget_left_usd,
    estimate,
    last_request,
    refusal,
    request_waiting,
)
from workflow.depth_evidence import DEPTH_RULE_IDS
from workflow.outbox import enqueue

router = APIRouter(tags=["packages"])


class DepthsAgainEstimateOut(BaseModel):
    """About how many questions reading depths again would ask, before a person starts it."""

    pages: int = Field(description="Pages holding a countertop: the pages the reader reads.")
    countertops: int
    questions_low: int
    questions_high: int = Field(
        description="About how many questions (each reader asked is one); equal to "
        "questions_low when an earlier depth reading of the set says exactly."
    )
    stored_answers: int = Field(
        description="Of those, how many already have a stored answer, reused at no cost."
    )
    basis: str = Field(description="How the estimate was made, in plain words.")


class DepthsAgainLastOut(BaseModel):
    """What became of the newest request."""

    request_id: UUID
    asked_at: datetime
    state: Literal["waiting", "not_read", "read", "stopped", "failed"]
    sentence: str


class ReadDepthsAgainStatusOut(BaseModel):
    """Whether "Read depths again" is offered on this set, and what it would do."""

    switched_on: bool = Field(
        description="The depth and sink reader's switch (GV_DEPTH_READER). Off, the action is "
        "not offered at all."
    )
    can_ask: bool
    refused: str | None = Field(default=None, description="Why it cannot be asked now.")
    checks: list[str] = Field(
        default_factory=list,
        description="The checks the depth and sink reader fills: the action is offered on these.",
    )
    estimate: DepthsAgainEstimateOut | None = None
    budget_left_usd: str | None = Field(
        default=None,
        description="What the depth and sink reader may still spend on this set, in US dollars "
        "rounded down to the cent: the worker's own spend cap. Spending stops there.",
    )
    last: DepthsAgainLastOut | None = None


class ReadDepthsAgainOut(BaseModel):
    """The queued request."""

    accepted_id: UUID = Field(description="The enqueued work, not a workflow run.")
    package_revision_id: UUID


def _limits(settings: Settings) -> SpendLimits:
    """The worker's spend settings, as this API process states them.

    The Claude readers' ceiling is built by the worker's own function (`claude_ceiling`, #1317):
    the stated ceiling, or else the admin's maximum for a drawing set. Passing the stated setting
    as it is showed a fixed $40 for a set as $40 left while the worker stops at the $25 maximum."""
    budget = ai_budget_from_settings(settings)
    return SpendLimits(
        budget=budget,
        fixed_usd=settings.ai_budget_per_set_usd,
        claude_ceiling_usd=claude_ceiling(settings, budget),
    )


def _why_not(session: Session, settings: Settings, revision: PackageRevision) -> str | None:
    """Why it cannot be asked now: the worker's own refusal (switches, state, an unpriced call,
    a used-up budget), or a request already waiting."""
    why = refusal(
        session,
        revision,
        switched_on=settings.depth_reader,
        claude_readers=settings.claude_reader_enabled,
        reuse_answers=settings.reader_reuse_answers,
        limits=_limits(settings),
    )
    if why is None and request_waiting(session, revision.id):
        return ALREADY_ASKED
    return why


@router.get(
    "/projects/{project_id}/packages/{package_id}/read-depths-again",
    response_model=ReadDepthsAgainStatusOut,
    summary="Whether the depths can be read again on this drawing set, and about how many questions",
)
def depths_again_status(
    request: Request,
    _: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ReadDepthsAgainStatusOut:
    """The action's status for the package's current revision. Reads only."""
    settings: Settings = request.app.state.settings
    revision = _package_revision(session, project_id, package_id)
    last = last_request(session, revision.id)
    last_out = (
        None
        if last is None
        else DepthsAgainLastOut(
            request_id=last.request_id,
            asked_at=last.asked_at,
            state=last.state,
            sentence=last.sentence,
        )
    )
    if not settings.depth_reader:
        return ReadDepthsAgainStatusOut(
            switched_on=False, can_ask=False, refused=None, last=last_out
        )
    why = _why_not(session, settings, revision)
    shown = estimate(session, revision.id)
    return ReadDepthsAgainStatusOut(
        switched_on=True,
        can_ask=why is None,
        refused=why,
        checks=sorted(DEPTH_RULE_IDS),
        budget_left_usd=str(budget_left_usd(session, revision.id, _limits(settings))),
        estimate=DepthsAgainEstimateOut(
            pages=shown.pages,
            countertops=shown.countertops,
            questions_low=shown.questions_low,
            questions_high=shown.questions_high,
            stored_answers=shown.stored_answers,
            basis=shown.basis,
        ),
        last=last_out,
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/read-depths-again",
    response_model=ReadDepthsAgainOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Read the depths and sinks again on a drawing set that was already read (paid)",
    # A signed-off or closed review is never read again: refused with the plain sentence.
    dependencies=[Depends(open_revision)],
)
def read_depths_again(
    request: Request,
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.START_REVIEW))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ReadDepthsAgainOut:
    """Queue "Read depths again". Refused (409) with the plain reason when it cannot run now."""
    settings: Settings = request.app.state.settings
    revision = _package_revision(session, project_id, package_id, lock=True)
    why = _why_not(session, settings, revision)
    if why is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=why)
    accepted = enqueue(
        session,
        workflow=READ_DEPTHS_AGAIN_WORKFLOW,
        payload={"package_revision_id": str(revision.id), "project_id": str(project_id)},
    )
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=accepted,
        target_type="reading_again:read_depths_again",
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ReadDepthsAgainOut(accepted_id=accepted, package_revision_id=revision.id)
