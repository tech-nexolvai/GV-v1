"""The parts of each vendor drawing, and a person confirming them one at a time (#882).

The computer suggests the parts of each vendor drawing (#868): cabinets, with any filler among
them, and countertops. These endpoints list those suggestions, left to right, and record a person's
answer to one of them: confirm it as a part of a given kind (with the code as printed, corrected if
need be), or withdraw it. A person may also add a part the suggestions missed. **Only a confirmation makes a part**, through
`workflow/parts.py:confirm_part`; listing never does, and there is deliberately no endpoint that
decides more than one suggestion.

The rules about which drawing may have parts, and what is kept, are `app/evidence/parts.py`'s.

Source: issue #882; #748 plan, step 4. Verification: tests/api/test_drawing_parts.py.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.confirmations import _candidate_crop_artifact, _verified_crop_content
from app.api.dependencies import get_artifact_store, get_session
from app.api.drawing_views import NOT_FOUND_DETAIL, _revision
from app.auth import Principal, require_action, require_project_access
from app.auth.roles import Action
from app.db.session import is_unique_violation
from app.evidence.parts import (
    ADDED_BY_A_PERSON,
    DrawingParts,
    ListedPart,
    PartRefusalReason,
    PartRefused,
    add_part,
    confirm_listed_part,
    defining_ends,
    listed_proposal,
    revision_parts,
    withdraw_listed_part,
)
from app.models import PartConfirmation
from storage.store import ArtifactStore
from vocabulary.part_kinds import PartKind

router = APIRouter(tags=["drawing parts"])

#: The refusals that mean "no such thing here", in the same words as every other absence.
_ABSENT: Final = {PartRefusalReason.NO_SUCH_PART, PartRefusalReason.NO_SUCH_DRAWING}

#: The refusals about the drawing as it stands, which no change to the request will fix.
_CONFLICT: Final = {
    PartRefusalReason.DRAWING_NOT_CONFIRMED,
    PartRefusalReason.ARCHITECTS_DRAWING,
}

#: What a person is told when someone else decided on the same part at the same moment. The
#: database keeps one current decision per suggestion, so the second to arrive is refused.
_DECIDED_AT_ONCE: Final = (
    "Someone else decided on this part at the same moment. Reload the page to see their decision."
)


class PointOut(BaseModel):
    """A point in stored page space, as exact text."""

    x: str
    y: str


class DecisionOut(BaseModel):
    """What a person last said about one suggestion."""

    decision: str
    """`confirmed` or `withdrawn`."""
    kind: PartKind | None
    """The kind the person confirmed; null on a withdrawal."""
    code: str | None
    """The code the person kept, exactly as they sent it, or null for none."""
    decided_by: str
    decided_at: datetime


class PartOut(BaseModel):
    """One suggested part, as a person needs it to decide what it is."""

    proposal_id: UUID
    view_id: UUID
    page_index: int
    position: int = Field(description="Its place on its drawing, 1 for the leftmost.")
    suggested_kind: PartKind
    suggested_code: str | None = Field(
        description="The code read on the part, as printed, or null when none was read."
    )
    reason: str = Field(description="Why it was suggested, in plain English.")
    added_by_a_person: bool = Field(
        description="True when a person added it because the suggestions missed it."
    )
    left_end: PointOut | None = Field(
        description="The left end of the line that defines it, or null when no line did."
    )
    right_end: PointOut | None
    has_crop: bool = Field(
        description=(
            "Whether a picture is stored for it. Today that is the crop of the reading its code "
            "came from, so a suggestion with no code has none."
        )
    )
    decision: DecisionOut | None = Field(
        description="The decision nothing has replaced, or null while nobody has decided."
    )


class DrawingOut(BaseModel):
    """One drawing and its suggested parts, left to right."""

    view_id: UUID
    page_index: int
    tag: str
    role: str | None
    """`arch`, `shop`, or null until a person confirms whose drawing it is."""
    can_confirm: bool
    """Whether a part may be confirmed or added here: only on a drawing confirmed as the vendor's."""
    why_not: str | None
    """Why not, in plain English, when `can_confirm` is false."""
    parts: list[PartOut]


class PartsOut(BaseModel):
    drawings: list[DrawingOut]


class ConfirmPartIn(BaseModel):
    kind: PartKind
    code: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "The code printed on the part, exactly as it is printed; null when none is printed. "
            "Stored as sent: never trimmed, never read as a width."
        ),
    )


class EndIn(BaseModel):
    """A point in stored page space, sent as exact text, such as one `left_end` from the list."""

    x: str = Field(max_length=64)
    y: str = Field(max_length=64)


class AddPartIn(BaseModel):
    kind: PartKind
    code: str | None = Field(default=None, max_length=200)
    ends: tuple[EndIn, EndIn] = Field(
        description="The part's two ends on the drawing, in either order."
    )


def _decision(row: PartConfirmation | None) -> DecisionOut | None:
    if row is None:
        return None
    return DecisionOut(
        decision=row.decision,
        kind=None if row.kind is None else PartKind(row.kind),
        code=row.code_as_printed,
        decided_by=row.confirmed_by,
        decided_at=row.created_at,
    )


def _part_out(drawing: DrawingParts, listed: ListedPart) -> PartOut:
    proposal = listed.proposal
    ends = defining_ends(proposal)
    return PartOut(
        proposal_id=proposal.id,
        view_id=drawing.view.id,
        page_index=drawing.page_index,
        position=listed.position,
        suggested_kind=PartKind(proposal.kind),
        suggested_code=proposal.code_as_printed,
        reason=proposal.reason,
        added_by_a_person=proposal.source == ADDED_BY_A_PERSON,
        left_end=None if ends is None else PointOut(x=ends[0][0], y=ends[0][1]),
        right_end=None if ends is None else PointOut(x=ends[1][0], y=ends[1][1]),
        has_crop=listed.has_crop,
        decision=_decision(listed.decision),
    )


def _drawing_out(drawing: DrawingParts) -> DrawingOut:
    return DrawingOut(
        view_id=drawing.view.id,
        page_index=drawing.page_index,
        tag=drawing.view.tag,
        role=drawing.view.role,
        can_confirm=drawing.refusal is None,
        why_not=None if drawing.refusal is None else drawing.refusal.detail,
        parts=[_part_out(drawing, listed) for listed in drawing.parts],
    )


def _listed(session: Session, revision_id: UUID, proposal_id: UUID) -> PartOut:
    """The part as the list now shows it, read back after a decision was committed."""
    for drawing in revision_parts(session, revision_id):
        for listed in drawing.parts:
            if listed.proposal.id == proposal_id:
                return _part_out(drawing, listed)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


def _refuse(refused: PartRefused) -> HTTPException:
    if refused.reason in _ABSENT:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if refused.reason in _CONFLICT:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refused.detail)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refused.detail)


def _commit(session: Session, outcome: PartConfirmation | PartRefused) -> PartConfirmation:
    """Commit a recorded decision, or roll back and refuse one that was not recorded."""
    if isinstance(outcome, PartRefused):
        session.rollback()
        raise _refuse(outcome)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return outcome


def _decide(
    session: Session, decide: Callable[[], PartConfirmation | PartRefused]
) -> PartConfirmation:
    """Run one decision; a second decision on the same part at the same moment is a conflict.

    The decision writes inside `decide`, so a unique violation arrives there rather than at commit:
    the database keeps at most one current decision per suggestion, and the loser is told so.
    """
    try:
        outcome = decide()
    except IntegrityError as error:
        session.rollback()
        if is_unique_violation(error, "supersedes_id", "first_decision"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=_DECIDED_AT_ONCE
            ) from error
        raise
    return _commit(session, outcome)


@router.get(
    "/projects/{project_id}/packages/{package_id}/parts",
    response_model=PartsOut,
    summary="The parts suggested in each drawing, left to right, and what a person said of each",
)
def list_parts(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> PartsOut:
    """Read only: listing never makes a part, however many times it is called."""
    revision = _revision(session, project_id, package_id)
    return PartsOut(drawings=[_drawing_out(d) for d in revision_parts(session, revision.id)])


@router.get(
    "/projects/{project_id}/packages/{package_id}/parts/{proposal_id}/crop",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
            "description": "The integrity-checked picture stored for this suggestion.",
        }
    },
    summary="View the picture stored for one suggested part",
)
def part_crop(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    proposal_id: UUID,
) -> Response:
    """The crop of the reading the part's code came from, checked against its recorded digest.

    The same picture the Measure page shows for that reading, through the same project boundary:
    the suggestion must be on this package's current revision, and so must the reading.
    """
    revision = _revision(session, project_id, package_id)
    proposal = listed_proposal(session, revision.id, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    artifact = (
        None
        if proposal.code_candidate_id is None
        else _candidate_crop_artifact(session, revision, proposal.code_candidate_id)
    )
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no picture is stored for this part",
        )
    content = _verified_crop_content(store, artifact)
    return Response(
        content=content, media_type=artifact.media_type, headers={"Cache-Control": "no-store"}
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/parts/{proposal_id}/confirm",
    response_model=PartOut,
    status_code=status.HTTP_201_CREATED,
    summary="Say what one suggested part is: a cabinet, a filler or a countertop",
)
def confirm_part_endpoint(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    proposal_id: UUID,
    body: ConfirmPartIn,
) -> PartOut:
    """Record a person's decision, which makes the part. Audited, and correctable by deciding again:
    the latest decision is the one that counts, and every earlier one is kept."""
    revision = _revision(session, project_id, package_id)
    _decide(
        session,
        lambda: confirm_listed_part(
            session,
            package_revision_id=revision.id,
            proposal_id=proposal_id,
            kind=body.kind,
            code=body.code,
            actor=principal.id,
        ),
    )
    return _listed(session, revision.id, proposal_id)


@router.post(
    "/projects/{project_id}/packages/{package_id}/parts/{proposal_id}/withdraw",
    response_model=PartOut,
    status_code=status.HTTP_201_CREATED,
    summary="Say one suggestion is not a part",
)
def withdraw_part_endpoint(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    proposal_id: UUID,
) -> PartOut:
    """Record that it is not a part. Makes nothing; a part it had made stops being read."""
    revision = _revision(session, project_id, package_id)
    _decide(
        session,
        lambda: withdraw_listed_part(
            session,
            package_revision_id=revision.id,
            proposal_id=proposal_id,
            actor=principal.id,
        ),
    )
    return _listed(session, revision.id, proposal_id)


@router.post(
    "/projects/{project_id}/packages/{package_id}/views/{view_id}/parts",
    response_model=PartOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a part the suggestions missed, by its two ends on the drawing",
)
def add_part_endpoint(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    view_id: UUID,
    body: AddPartIn,
) -> PartOut:
    """File the person's own suggestion and confirm it, in one transaction."""
    revision = _revision(session, project_id, package_id)
    first, second = body.ends
    confirmation = _decide(
        session,
        lambda: add_part(
            session,
            package_revision_id=revision.id,
            view_id=view_id,
            kind=body.kind,
            code=body.code,
            ends=((first.x, first.y), (second.x, second.y)),
            actor=principal.id,
        ),
    )
    return _listed(session, revision.id, confirmation.part_proposal_id)
