"""Review sessions and the actions a reviewer takes (#229 over HTTP).

`app/review/session.py` has done this work since D4.1; nothing exposed it, so the reviewer workspace
had no way to open a sitting or record a decision. This is the HTTP surface over that module and
holds no review logic of its own — every refusal below comes from the service, and the mapping to a
status code is all this layer adds.

**The reviewer's name is never taken from the request.** `reviewer` and `actor` are the authenticated
principal's id. A body-supplied name would let a caller record a decision as somebody else, and an
audit trail whose author is client-supplied answers "who says so?" with "whoever was asked" — which
is the only question it exists to answer.

**A session names a package revision, not a package.** A package moves on; a review that silently
followed it would record decisions against drawings the reviewer never saw. `open_session` refuses a
superseded revision for the same reason.

Source: `docs/DESIGN_PRODUCT.md` §4 · Verification: `tests/api/test_review_api.py`
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models.package import Package, PackageRevision
from app.models.review import ReviewSession
from app.review.session import (
    ActionOutsideTheSession,
    ActorNotNamed,
    NoSuchFinding,
    NoSuchPackageRevision,
    NoSuchReviewSession,
    RevisionSuperseded,
    SessionAlreadyComplete,
    complete_session,
    open_session,
    record_action,
)
from app.schemas.review import (
    OpenReviewSession,
    RecordAction,
    ReviewActionOut,
    ReviewSessionOut,
    ReviewSessionPage,
)

router = APIRouter(tags=["review"])

#: What every refusal says. Nothing about the project, the package or the reason.
NOT_FOUND_DETAIL = "Not found"

#: Which service refusals mean "this does not exist, as far as you are concerned", and which mean
#: "it exists and the request conflicts with its state". Mapped as data rather than as a chain of
#: `except` clauses so that a new refusal has to be classified rather than falling through to a 500.
_CONFLICT = (RevisionSuperseded, SessionAlreadyComplete, ActionOutsideTheSession)
_NOT_FOUND = (NoSuchPackageRevision, NoSuchReviewSession, NoSuchFinding)


def _session_is_in_project(db: Session, project_id: UUID, review_session_id: UUID) -> bool:
    """Whether this session belongs to the caller's project.

    Checked in SQL rather than trusted from the path. The dependency establishes that the caller may
    see *this project*; it says nothing about whether the session they named is in it.
    """
    statement = (
        select(ReviewSession.id)
        .join(PackageRevision, PackageRevision.id == ReviewSession.package_revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(ReviewSession.id == review_session_id, Package.project_id == project_id)
    )
    return db.execute(statement).first() is not None


def _revision_is_in_project(db: Session, project_id: UUID, revision_id: UUID) -> bool:
    statement = (
        select(PackageRevision.id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(PackageRevision.id == revision_id, Package.project_id == project_id)
    )
    return db.execute(statement).first() is not None


def _refuse(error: Exception) -> HTTPException:
    """Turn a service refusal into a status code, keeping the service's own reason.

    The reasons are worth surfacing: `open_session` explains that a revision was superseded, and a
    reviewer told only "409" would go looking for a bug rather than for the newer drawing.
    """
    if isinstance(error, _NOT_FOUND):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if isinstance(error, _CONFLICT):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, ActorNotNamed):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    raise error


@router.post(
    "/projects/{project_id}/packages/{package_id}/review-sessions",
    response_model=ReviewSessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a review session over one package revision",
)
def open_review_session(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    db: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    body: OpenReviewSession,
) -> ReviewSessionOut:
    """Start a sitting. The reviewer is the caller, not a name in the body.

    A revision outside this project is `404` rather than `403`, like everything else here: a 403
    would confirm it exists, and project scope is an isolation boundary rather than a filter.
    """
    del package_id
    if not _revision_is_in_project(db, project_id, body.package_revision_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    try:
        opened = open_session(
            db, package_revision_id=body.package_revision_id, reviewer=principal.id
        )
        db.commit()
    except Exception as error:
        db.rollback()
        raise _refuse(error) from error

    return ReviewSessionOut.model_validate(opened)


@router.get(
    "/projects/{project_id}/review-sessions",
    response_model=ReviewSessionPage,
    summary="Review sessions in this project, newest first",
)
def list_review_sessions(
    principal: Annotated[Principal, Depends(require_project_access)],
    db: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    mine: bool = True,
) -> ReviewSessionPage:
    """The sessions a reviewer picks up again — what the workspace sidebar lists.

    `mine` defaults to true. A reviewer's own sittings are what they came back for, and a list
    defaulting to everyone's would bury them on any project with more than one reviewer.
    """
    statement = (
        select(ReviewSession)
        .join(PackageRevision, PackageRevision.id == ReviewSession.package_revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.project_id == project_id)
        .order_by(ReviewSession.created_at.desc(), ReviewSession.id.desc())
    )
    if mine:
        statement = statement.where(ReviewSession.reviewer == principal.id)

    rows = db.execute(statement).scalars().all()
    return ReviewSessionPage(items=[ReviewSessionOut.model_validate(row) for row in rows])


@router.post(
    "/projects/{project_id}/review-sessions/{review_session_id}/actions",
    response_model=ReviewActionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record what the reviewer did to one finding",
)
def record_review_action(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    db: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    review_session_id: UUID,
    body: RecordAction,
) -> ReviewActionOut:
    """Append one action. Never an edit — a changed mind is a second row and the first one stays.

    The actor is the caller and the revision is read off the finding the service loads, so neither
    can be stated by the client. That is what "an action references a server-side finding revision"
    means in code rather than in a comment.
    """
    if not _session_is_in_project(db, project_id, review_session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    try:
        action = record_action(
            db,
            review_session_id=review_session_id,
            finding_id=body.finding_id,
            action=body.action,
            actor=principal.id,
            note=body.note,
        )
        db.commit()
    except Exception as error:
        db.rollback()
        raise _refuse(error) from error

    return ReviewActionOut.model_validate(action)


@router.post(
    "/projects/{project_id}/review-sessions/{review_session_id}/complete",
    response_model=ReviewSessionOut,
    summary="Finish a review session",
)
def complete_review_session(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    db: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    review_session_id: UUID,
) -> ReviewSessionOut:
    """Close the sitting. Completing twice is refused rather than treated as a no-op — the second
    attempt means somebody believes they are finishing work that was already finished."""
    del principal
    if not _session_is_in_project(db, project_id, review_session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    try:
        completed = complete_session(db, review_session_id=review_session_id)
        db.commit()
    except Exception as error:
        db.rollback()
        raise _refuse(error) from error

    return ReviewSessionOut.model_validate(completed)
