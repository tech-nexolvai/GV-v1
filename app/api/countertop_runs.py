"""The run beneath each countertop: suggested by the computer, decided by a person (#893).

Once a person has confirmed a vendor drawing's parts (#882), these endpoints list each confirmed
countertop with the run of confirmed cabinets and fillers the computer suggests beneath it, and
record a person's answer: confirm the run as suggested, confirm it corrected (a different set of
parts), or say it is wrong. **Only a confirmation writes a run**, through
`workflow/countertop_runs.py:confirm_countertop_run`; listing never does, and there is deliberately
no endpoint that decides more than one countertop's run.

The rules about which runs may be confirmed, and how a run is ordered, are
`app/evidence/countertop_runs.py`'s and `workflow/countertop_runs.py`'s.

Source: issue #893; #748 plan, step 5. Verification: tests/api/test_countertop_runs.py.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.drawing_views import NOT_FOUND_DETAIL, _revision
from app.auth import Principal, require_action, require_project_access
from app.auth.roles import Action
from app.db.session import is_unique_violation
from app.evidence.countertop_runs import (
    NO_TOLERANCE,
    DrawingRuns,
    ListedCountertop,
    PickablePart,
    RunRefusalReason,
    RunRefused,
    confirm_listed_run,
    revision_runs,
    withdraw_listed_run,
)
from app.models import CountertopRunDecision
from vocabulary.part_kinds import PartKind
from vocabulary.semantic_types import SemanticType
from workflow.countertop_runs import published_wall_layouts

router = APIRouter(tags=["countertop runs"])

#: The refusals about the drawing or the system as it stands, which no change to the request fixes.
_CONFLICT: Final = {
    RunRefusalReason.DRAWING_NOT_VENDORS,
    RunRefusalReason.TOLERANCE_NOT_STATED,
}

#: What a person is told when someone else decided on the same run at the same moment. The database
#: keeps one current decision per countertop, so the second to arrive is refused.
_DECIDED_AT_ONCE: Final = (
    "Someone else decided on this run at the same moment. Reload the page to see their decision."
)

#: What a person is told about a confirmed run that is no longer read.
_NOT_READ: Final = (
    "A part in this run was taken back or corrected after the run was confirmed, so the run is no "
    "longer read. Confirm the run again."
)


class RunPartOut(BaseModel):
    """A confirmed part, named as "Parts of each drawing" names it."""

    item_id: UUID
    number: int | None = Field(
        description="Its number in that list, 1 for the leftmost; null if it is not listed there."
    )
    kind: PartKind
    code: str | None


class SuggestedMemberOut(BaseModel):
    item_id: UUID
    number: int | None
    kind: PartKind
    position: int = Field(description="Its place in the run, 1 for the leftmost.")
    signal: str = Field(description="Why it was suggested, in plain English.")


class LeftOutOut(BaseModel):
    item_id: UUID
    number: int | None
    kind: PartKind
    reason: str


class SuggestionOut(BaseModel):
    """The run the computer suggests. Nothing is written until a person confirms it."""

    members: list[SuggestedMemberOut]
    left_out: list[LeftOutOut] = Field(
        description="Parts along the countertop that the below-the-top filter left out, and why."
    )
    warnings: list[str] = Field(
        description="Where the run does not cover the countertop as one unbroken row."
    )
    edge_tolerance: str


class ConfirmedMemberOut(BaseModel):
    item_id: UUID
    number: int | None
    kind: PartKind | None
    position: int = Field(description="Its place in the run, 1 for the leftmost.")
    signal: str
    stands: bool = Field(description="False once a person has taken the part back or corrected it.")


class RunDecisionOut(BaseModel):
    """What a person last said about the run."""

    decision: str
    """`confirmed` or `withdrawn`."""
    decided_by: str
    decided_at: datetime
    members: list[ConfirmedMemberOut]
    """The confirmed run, in order; empty on a withdrawal."""
    read: bool
    """Whether the confirmed run is the one read: every part in it still stands."""
    why_not_read: str | None
    edge_tolerance: str | None
    """The tolerance recorded with the confirmed run."""
    wall_config: str | None
    """The layout this person chose for this run; null for withdrawals or legacy decisions."""


class CountertopOut(BaseModel):
    """One confirmed countertop and its run."""

    countertop_item_id: UUID
    number: int | None
    code: str | None
    suggestion: SuggestionOut | None = Field(
        description="Null when no tolerance is stated, so nothing can be suggested."
    )
    decision: RunDecisionOut | None


class RunDrawingOut(BaseModel):
    view_id: UUID
    page_index: int
    tag: str
    can_confirm: bool
    """Whether a run may be confirmed here: only on a drawing confirmed as the vendor's."""
    why_not: str | None
    parts: list[RunPartOut] = Field(
        description="The confirmed cabinets and fillers on the drawing, which a run may hold."
    )
    countertops: list[CountertopOut]


class RunsOut(BaseModel):
    can_suggest: bool
    why_not: str | None
    wall_layout_choices: list[str]
    drawings: list[RunDrawingOut]


class ConfirmRunIn(BaseModel):
    part_ids: list[UUID] = Field(
        max_length=200,
        description=(
            "The drawing items the run holds, in any order: the server orders them across the "
            "drawing, never by the order they were picked in."
        ),
    )
    wall_config: str | None = Field(
        description="Required: one of the published CT-WIDTH-001 wall-layout choices."
    )

    @model_validator(mode="before")
    @classmethod
    def _missing_layout_gets_the_plain_refusal(cls, data: object) -> object:
        # Keep the field required in the API schema while the evidence boundary gives an older
        # caller that omits it the exact, actionable refusal rather than a generic shape error.
        layout_key = SemanticType.WALL_CONFIG.value
        if isinstance(data, dict) and layout_key not in data:
            return {**data, layout_key: None}
        return data


def _tolerance(request: Request) -> Decimal | None:
    tolerance: Decimal | None = request.app.state.settings.run_edge_tolerance
    return tolerance


def _part_out(entry: PickablePart) -> RunPartOut:
    return RunPartOut(
        item_id=entry.part.item_id, number=entry.number, kind=entry.part.kind, code=entry.code
    )


def _countertop_out(drawing: DrawingRuns, listed: ListedCountertop) -> CountertopOut:
    numbers = {entry.part.item_id: entry.number for entry in drawing.parts}
    proposal = listed.proposal
    decision = listed.decision
    return CountertopOut(
        countertop_item_id=listed.countertop.part.item_id,
        number=listed.countertop.number,
        code=listed.countertop.code,
        suggestion=(
            None
            if proposal is None
            else SuggestionOut(
                members=[
                    SuggestedMemberOut(
                        item_id=member.part.item_id,
                        number=numbers.get(member.part.item_id),
                        kind=member.part.kind,
                        position=member.position + 1,
                        signal=member.signal,
                    )
                    for member in proposal.members
                ],
                left_out=[
                    LeftOutOut(
                        item_id=entry.part.item_id,
                        number=numbers.get(entry.part.item_id),
                        kind=entry.part.kind,
                        reason=entry.reason,
                    )
                    for entry in proposal.left_out
                ],
                warnings=list(proposal.warnings),
                edge_tolerance=str(proposal.edge_tolerance),
            )
        ),
        decision=None if decision is None else _decision_out(decision, listed),
    )


def _decision_out(decision: CountertopRunDecision, listed: ListedCountertop) -> RunDecisionOut:
    confirmed = decision.decision == "confirmed"
    return RunDecisionOut(
        decision=decision.decision,
        decided_by=decision.confirmed_by,
        decided_at=decision.created_at,
        members=[
            ConfirmedMemberOut(
                item_id=member.row.member_item_id,
                number=member.number,
                kind=member.kind,
                position=member.row.position + 1,
                signal=member.row.signal,
                stands=member.stands,
            )
            for member in listed.members
        ],
        read=listed.read,
        why_not_read=None if listed.read or not confirmed else _NOT_READ,
        edge_tolerance=str(listed.members[0].row.edge_tolerance) if listed.members else None,
        wall_config=decision.wall_config,
    )


def _drawing_out(drawing: DrawingRuns) -> RunDrawingOut:
    return RunDrawingOut(
        view_id=drawing.view.id,
        page_index=drawing.page_index,
        tag=drawing.view.tag,
        can_confirm=drawing.refusal is None,
        why_not=None if drawing.refusal is None else drawing.refusal.detail,
        parts=[_part_out(entry) for entry in drawing.parts],
        countertops=[_countertop_out(drawing, listed) for listed in drawing.countertops],
    )


def _listed(
    session: Session, revision_id: UUID, countertop_item_id: UUID, tolerance: Decimal | None
) -> CountertopOut:
    """The countertop as the list now shows it, read back after a decision was committed."""
    for drawing in revision_runs(session, revision_id, edge_tolerance=tolerance):
        for listed in drawing.countertops:
            if listed.countertop.part.item_id == countertop_item_id:
                return _countertop_out(drawing, listed)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


def _refuse(refused: RunRefused) -> HTTPException:
    if refused.reason is RunRefusalReason.NO_SUCH_COUNTERTOP:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if refused.reason in _CONFLICT:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refused.detail)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refused.detail)


def _decide(
    session: Session, decide: Callable[[], CountertopRunDecision | RunRefused]
) -> CountertopRunDecision:
    """Run one decision and commit it; refuse one that was not recorded. A second decision on the
    same run at the same moment is a conflict: the database keeps one current decision per
    countertop, and the loser is told so."""
    try:
        outcome = decide()
    except IntegrityError as error:
        session.rollback()
        if is_unique_violation(error, "supersedes_id", "first_decision"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=_DECIDED_AT_ONCE
            ) from error
        raise
    if isinstance(outcome, RunRefused):
        session.rollback()
        raise _refuse(outcome)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return outcome


@router.get(
    "/projects/{project_id}/packages/{package_id}/countertop-runs",
    response_model=RunsOut,
    summary="Each confirmed countertop, the run suggested beneath it, and what a person decided",
)
def list_runs(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    request: Request,
    project_id: UUID,
    package_id: UUID,
) -> RunsOut:
    """Read only: a suggested run is worked out on each call and never stored."""
    revision = _revision(session, project_id, package_id)
    tolerance = _tolerance(request)
    return RunsOut(
        can_suggest=tolerance is not None,
        why_not=None if tolerance is not None else NO_TOLERANCE,
        wall_layout_choices=list(published_wall_layouts(session)),
        drawings=[
            _drawing_out(drawing)
            for drawing in revision_runs(session, revision.id, edge_tolerance=tolerance)
        ],
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/countertop-runs/{countertop_item_id}/confirm",
    response_model=CountertopOut,
    status_code=status.HTTP_201_CREATED,
    summary="Say which parts make up the run beneath one countertop",
)
def confirm_run_endpoint(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    request: Request,
    project_id: UUID,
    package_id: UUID,
    countertop_item_id: UUID,
    body: ConfirmRunIn,
) -> CountertopOut:
    """Record a person's run, as suggested or corrected. Audited, and correctable by confirming
    again: the latest decision is the one that counts, and every earlier one is kept."""
    revision = _revision(session, project_id, package_id)
    tolerance = _tolerance(request)
    _decide(
        session,
        lambda: confirm_listed_run(
            session,
            package_revision_id=revision.id,
            countertop_item_id=countertop_item_id,
            member_item_ids=body.part_ids,
            edge_tolerance=tolerance,
            actor=principal.id,
            wall_config=body.wall_config,
        ),
    )
    return _listed(session, revision.id, countertop_item_id, tolerance)


@router.post(
    "/projects/{project_id}/packages/{package_id}/countertop-runs/{countertop_item_id}/withdraw",
    response_model=CountertopOut,
    status_code=status.HTTP_201_CREATED,
    summary="Say the run suggested or confirmed beneath one countertop is wrong",
)
def withdraw_run_endpoint(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    request: Request,
    project_id: UUID,
    package_id: UUID,
    countertop_item_id: UUID,
) -> CountertopOut:
    """Record that it is not the run. Writes no run; a run confirmed before stops being read."""
    revision = _revision(session, project_id, package_id)
    _decide(
        session,
        lambda: withdraw_listed_run(
            session,
            package_revision_id=revision.id,
            countertop_item_id=countertop_item_id,
            actor=principal.id,
        ),
    )
    return _listed(session, revision.id, countertop_item_id, _tolerance(request))
