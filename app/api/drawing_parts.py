"""The parts of each vendor drawing, and a person confirming them one at a time (#882).

The computer suggests the parts of each vendor drawing (#868): cabinets, with any filler among
them, and countertops. These endpoints list those suggestions, left to right, and record a person's
answer to one of them: confirm it as a part of a given kind (with the code as printed, corrected if
need be), or withdraw it. A person may also add a part the suggestions missed. **Only a confirmation makes a part**, through
`workflow/parts.py:confirm_part`; listing never does, and there is deliberately no endpoint that
decides more than one suggestion.

The rules about which drawing may have parts, and what is kept, are `app/evidence/parts.py`'s.

**Each part's picture (#897)** is served from where the worker stored it, checked against its
recorded digest first. Adding a part asks the worker to cut the new part's picture, in the same
transaction as the part, through the outbox: this module may not render a page itself
(`tests/api/test_no_heavy_work.py`). Each part says what its picture was found to show of GV's own
coloured marks when it was cut (#921), so the page can warn under one that shows them.

Source: issues #882, #897 and #921; #748 plan, step 4. Verification:
tests/api/test_drawing_parts.py.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
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
from app.models import OutboxEntry, Page, PartConfirmation, PartPicture, VendorPagePicture
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
from storage.store import ArtifactStore
from vocabulary.part_kinds import PartKind
from workflow.outbox import enqueue
from workflow.part_pictures import CUT_PART_PICTURES_WORKFLOW, GvMarks
from workflow.part_pictures import part_picture as recorded_picture
from workflow.vendor_page_pictures import (
    RENDER_VENDOR_PAGE_PICTURES_WORKFLOW,
)
from workflow.vendor_page_pictures import (
    page_picture as recorded_page_picture,
)

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
    has_picture: bool = Field(
        description=(
            "Whether its own picture is stored: the vendor's drawing around its outline (#897). "
            "False until the worker has cut it, or where the page could not be rendered."
        )
    )
    picture_gv_marks: GvMarks | None = Field(
        description=(
            "What its picture was found to show of GV's own coloured marks when it was cut (#921): "
            "`shown` where markup drawn in colour lies in it, so the page warns under it; "
            "`not_shown` where it was checked and none does; `not_checked` for a picture cut "
            "before the check existed or whose page's coloured markup could not be read. Null "
            "when no picture is stored."
        )
    )
    has_crop: bool = Field(
        description=(
            "Whether the crop of the reading its code came from is stored, so a suggestion with no "
            "code has none."
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
    page_picture: VendorPagePictureOut | None = None


class SnapPointOut(BaseModel):
    x: str
    y: str
    source: str


class VendorPagePictureOut(BaseModel):
    url: str
    dpi: int
    width_px: int
    height_px: int
    rotation: int
    media_box: list[str]
    crop_box: list[str]
    snap_tolerance: str | None
    snap_points: list[SnapPointOut]


class PreparePagePicturesOut(BaseModel):
    queued: bool
    reason: str | None = None


DrawingOut.model_rebuild()


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
        has_picture=listed.has_picture,
        picture_gv_marks=listed.picture_gv_marks,
        has_crop=listed.has_crop,
        decision=_decision(listed.decision),
    )


def _drawing_out(
    drawing: DrawingParts, session: Session, request: Request, project_id: UUID, package_id: UUID
) -> DrawingOut:
    return DrawingOut(
        view_id=drawing.view.id,
        page_index=drawing.page_index,
        tag=drawing.view.tag,
        role=drawing.view.role,
        can_confirm=drawing.refusal is None,
        why_not=None if drawing.refusal is None else drawing.refusal.detail,
        parts=[_part_out(drawing, listed) for listed in drawing.parts],
        page_picture=_vendor_page_picture_out(
            session, request, drawing.view.id, drawing.view.page_id, project_id, package_id
        ),
    )


def _vendor_page_picture_out(
    session: Session,
    request: Request,
    view_id: UUID,
    page_id: UUID,
    project_id: UUID,
    package_id: UUID,
) -> VendorPagePictureOut | None:
    picture = recorded_page_picture(session, page_id)
    page = session.get(Page, page_id)
    if picture is None or page is None or page.media_box is None or page.crop_box is None:
        return None
    return VendorPagePictureOut(
        url=request.url_for(
            "vendor_page_picture",
            project_id=str(project_id),
            package_id=str(package_id),
            view_id=str(view_id),
        ).path,
        dpi=picture.dpi,
        width_px=picture.width_px,
        height_px=picture.height_px,
        rotation=page.rotation,
        media_box=page.media_box,
        crop_box=page.crop_box,
        snap_tolerance=picture.snap_tolerance,
        snap_points=[SnapPointOut(**point) for point in picture.snap_points],
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
    request: Request,
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> PartsOut:
    """Read only: listing never makes a part, however many times it is called."""
    revision = _revision(session, project_id, package_id)
    return PartsOut(
        drawings=[
            _drawing_out(d, session, request, project_id, package_id)
            for d in revision_parts(session, revision.id)
        ]
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/parts/page-pictures",
    response_model=PreparePagePicturesOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Prepare missing vendor-only page pictures for placement",
)
def prepare_page_pictures(
    _principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> PreparePagePicturesOut:
    """Ask the worker to fill missing full-page pictures; rendering never happens in the API."""
    revision = _revision(session, project_id, package_id)
    vendor_drawings = [
        drawing for drawing in revision_parts(session, revision.id) if drawing.view.role == "shop"
    ]
    missing = [
        drawing
        for drawing in vendor_drawings
        if recorded_page_picture(session, drawing.view.page_id) is None
    ]
    if not missing:
        return PreparePagePicturesOut(
            queued=False,
            reason=(
                "confirm a vendor drawing first" if not vendor_drawings else "pictures are ready"
            ),
        )
    pending = session.scalar(
        select(OutboxEntry.id)
        .where(
            OutboxEntry.workflow == RENDER_VENDOR_PAGE_PICTURES_WORKFLOW,
            OutboxEntry.dispatched_at.is_(None),
            OutboxEntry.payload["package_revision_id"].as_string() == str(revision.id),
        )
        .limit(1)
    )
    if pending is None:
        enqueue(
            session,
            workflow=RENDER_VENDOR_PAGE_PICTURES_WORKFLOW,
            payload={"package_revision_id": str(revision.id)},
        )
        session.commit()
    return PreparePagePicturesOut(queued=True)


@router.get(
    "/projects/{project_id}/packages/{package_id}/views/{view_id}/picture",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}
        }
    },
    summary="View a vendor-only drawing page",
)
def vendor_page_picture(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    view_id: UUID,
) -> Response:
    revision = _revision(session, project_id, package_id)
    drawing = next(
        (entry for entry in revision_parts(session, revision.id) if entry.view.id == view_id), None
    )
    if drawing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if drawing.view.role != "shop":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="confirm this as the vendor drawing first"
        )
    picture = recorded_page_picture(session, drawing.view.page_id)
    if picture is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="the vendor page picture is not ready yet"
        )
    content = _verified_page_picture(store, picture)
    return Response(
        content=content, media_type=picture.media_type, headers={"Cache-Control": "no-store"}
    )


def _verified_page_picture(store: ArtifactStore, picture: VendorPagePicture) -> bytes:
    try:
        with store.get(picture.storage_key) as stored:
            content = stored.read()
    except (ArtifactCorrupt, FileNotFoundError, IntegrityRecordMissing) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored vendor page picture is unavailable",
        ) from error
    if hashlib.sha256(content).hexdigest() != picture.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored vendor page picture failed its integrity check",
        )
    return content


def _verified_picture(store: ArtifactStore, picture: PartPicture) -> bytes:
    """A part's picture, only while its stored bytes still match the digest recorded for them."""
    try:
        with store.get(picture.storage_key) as stored:
            content = stored.read()
    except (ArtifactCorrupt, FileNotFoundError, IntegrityRecordMissing) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored picture of this part is unavailable, so it cannot be shown",
        ) from error
    if hashlib.sha256(content).hexdigest() != picture.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored picture of this part does not match its recorded digest, so it "
            "cannot be shown",
        )
    return content


@router.get(
    "/projects/{project_id}/packages/{package_id}/parts/{proposal_id}/picture",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
            "description": "The integrity-checked picture of this part (#897).",
        }
    },
    summary="View the picture of one suggested part, cut from the vendor's drawing",
)
def part_picture(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    proposal_id: UUID,
) -> Response:
    """The part's own picture: the vendor's drawing around its outline, checked against its
    recorded digest. For a person's eyes only; nothing reads a value from it.

    The suggestion must be on this package's current revision, so another project's part looks
    absent (404), as every other absence does.
    """
    revision = _revision(session, project_id, package_id)
    proposal = listed_proposal(session, revision.id, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    picture = recorded_picture(session, proposal.id)
    if picture is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no picture of this part is stored yet",
        )
    return Response(
        content=_verified_picture(store, picture),
        media_type=picture.media_type,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/parts/{proposal_id}/crop",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
            "description": "The integrity-checked crop of the reading the code came from.",
        }
    },
    summary="View the crop of the reading one suggested part's code came from",
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
            detail="no crop of this part's code is stored",
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
    """File the person's own suggestion and confirm it, in one transaction, and ask the worker to
    cut its picture (#897) in the same one: the part and the request commit together or not at all.
    The list shows the part at once and its picture once the worker has cut it."""
    revision = _revision(session, project_id, package_id)
    first, second = body.ends

    def add_and_ask_for_its_picture() -> PartConfirmation | PartRefused:
        outcome = add_part(
            session,
            package_revision_id=revision.id,
            view_id=view_id,
            kind=body.kind,
            code=body.code,
            ends=((first.x, first.y), (second.x, second.y)),
            actor=principal.id,
        )
        if isinstance(outcome, PartConfirmation):
            enqueue(
                session,
                workflow=CUT_PART_PICTURES_WORKFLOW,
                payload={"package_revision_id": str(revision.id)},
            )
        return outcome

    confirmation = _decide(session, add_and_ask_for_its_picture)
    return _listed(session, revision.id, confirmation.part_proposal_id)
