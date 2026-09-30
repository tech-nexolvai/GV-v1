"""Which drawing on a combined sheet is the architect's and which the vendor's (#710).

Two endpoints: one lists the drawings found on a package's pages with what the sheet's labels suggest
each one is; the other is a reviewer confirming it. **Only the confirmation sets the role.** The
suggestion is shown so a reviewer can accept it with one click, and never takes effect on its own.

A package with two files — one architect's, one vendor's — does not need this: the upload already
says which is which. This is for the one-sheet case, where both drawings share a page.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Principal, require_action, require_project_access
from app.auth.roles import Action
from app.models import DrawingView, Package, PackageRevision, ViewRole
from workflow.view_roles import RevisionView, confirm_view_role, revision_views

router = APIRouter(tags=["drawing views"])

#: The same words for a package that does not exist and one the caller may not see.
NOT_FOUND_DETAIL: Final = "Not found"


class ViewOut(BaseModel):
    """One drawing on a page, as a reviewer needs it to say which drawing it is."""

    view_id: UUID
    page_index: int
    tag: str
    role: str | None
    """`arch`, `shop`, or null until a reviewer confirms. Set only by a reviewer's confirmation."""
    suggested_role: str | None
    """`arch`, `shop`, or null when the sheet's labels decide nothing."""
    suggested_from: str | None
    """The label the sheet prints above this drawing, when one was used."""
    reason: str | None


class ViewsOut(BaseModel):
    views: list[ViewOut]


class ConfirmRoleIn(BaseModel):
    role: Literal["arch", "shop"]


def _revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return revision


def _out(entry: RevisionView) -> ViewOut:
    view, page_index, proposal = entry.view, entry.page_index, entry.proposal
    return ViewOut(
        view_id=view.id,
        page_index=page_index,
        tag=view.tag,
        role=view.role,
        suggested_role=None if proposal is None else proposal.proposed_role,
        suggested_from=None if proposal is None else proposal.heading,
        reason=None if proposal is None else proposal.reason,
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/views",
    response_model=ViewsOut,
    summary="The drawings found on this package's pages, and what each one is suggested to be",
)
def list_views(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ViewsOut:
    revision = _revision(session, project_id, package_id)
    return ViewsOut(views=[_out(entry) for entry in revision_views(session, revision.id)])


@router.post(
    "/projects/{project_id}/packages/{package_id}/views/{view_id}/role",
    response_model=ViewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Say which drawing this is: the architect's or the vendor's",
)
def confirm_role(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    view_id: UUID,
    body: ConfirmRoleIn,
) -> ViewOut:
    """Record a reviewer's answer and set the drawing's role. Recorded, audited, and correctable by
    confirming again — the latest answer is the one that counts, and every earlier one is kept."""
    revision = _revision(session, project_id, package_id)
    entries = {entry.view.id: entry for entry in revision_views(session, revision.id)}
    entry = entries.get(view_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    view = session.get(DrawingView, view_id, with_for_update=True)
    assert view is not None
    confirm_view_role(session, view=view, role=ViewRole(body.role), actor=principal.id)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    refreshed = {e.view.id: e for e in revision_views(session, revision.id)}[view_id]
    return _out(refreshed)
