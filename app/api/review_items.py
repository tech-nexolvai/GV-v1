"""`GET .../review-items`: one read-only list of what a person must check (#1338, simple review W1).

The route only finds the package's newest revision and hands it to
`app/review/review_items.py`, which builds the list from the existing projections. It writes
nothing, queues nothing and calls no model.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Package, PackageRevision
from app.review.review_items import ReviewItemsOut, review_items_for_revision

router = APIRouter(tags=["simple review"])


@router.get(
    "/projects/{project_id}/packages/{package_id}/review-items",
    response_model=ReviewItemsOut,
    summary="List exactly what a person must check on the newest revision, read-only",
)
def review_items(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ReviewItemsOut:
    """Items to answer, FAIL results to agree with, and the checks that could not run.

    Every blocking result is in exactly one of `items`, `failures` and `not_checked`, and every
    page that blocks sign-off is one item; `unaccounted` names anything that is not, and must be
    empty. Nothing here decides or records anything.
    """
    del principal
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return review_items_for_revision(
        session, project_id=project_id, package_id=package_id, revision=revision
    )


__all__ = ["router"]
