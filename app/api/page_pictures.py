"""The picture of any drawing page, for the reviewer's left-hand pane (#968).

The Measure screen shows the drawing on the left and the form on the right (admin, 2026-10-06).
Rendering never happens in the API: the worker's page-picture job (`RENDER_VENDOR_PAGE_PICTURES`)
renders every page of the revision, vendor layer only, and this serves the stored, digest-checked
picture or says it is not ready and lets the screen ask for it.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import case, select, true
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.api.drawing_parts import _verified_page_picture
from app.api.drawing_views import NOT_FOUND_DETAIL, _revision
from app.auth import Principal, require_project_access
from app.models import OutboxEntry, Page
from app.models.document import Document, DocumentKind, PackageRevisionDocument
from storage.store import ArtifactStore
from workflow.outbox import enqueue
from workflow.vendor_page_pictures import (
    RENDER_VENDOR_PAGE_PICTURES_WORKFLOW,
    pages_without_pictures,
)
from workflow.vendor_page_pictures import page_picture as recorded_page_picture

router = APIRouter(tags=["drawings"])


class PagePicturesQueuedOut(BaseModel):
    queued: bool


def _page(
    session: Session, revision_id: UUID, page_number: int, document_version_id: UUID | None = None
) -> Page | None:
    """Page `page_number` (1-based); the shop drawing's when two documents have that page."""
    return session.scalars(
        select(Page)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .join(Document, Document.id == PackageRevisionDocument.document_id)
        .where(
            PackageRevisionDocument.package_revision_id == revision_id,
            Page.index == page_number - 1,
            (
                Page.document_version_id == document_version_id
                if document_version_id is not None
                else true()
            ),
        )
        .order_by(case((Document.kind == DocumentKind.SHOP.value, 0), else_=1), Document.id)
        .limit(1)
    ).first()


@router.get(
    "/projects/{project_id}/packages/{package_id}/pages/{page_number}/picture",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}
        }
    },
    summary="View a drawing page (vendor layer only)",
)
def page_picture(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    page_number: int,
    document_version_id: UUID | None = None,
) -> Response:
    """The stored picture of page `page_number`, or 404 while the worker has not rendered it."""
    revision = _revision(session, project_id, package_id)
    page = _page(session, revision.id, page_number, document_version_id)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    picture = recorded_page_picture(session, page.id)
    if picture is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="the page picture is not ready yet"
        )
    content = _verified_page_picture(store, picture)
    return Response(
        content=content, media_type=picture.media_type, headers={"Cache-Control": "no-store"}
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/pages/pictures",
    response_model=PagePicturesQueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask the worker to render missing page pictures",
)
def prepare_page_pictures(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> PagePicturesQueuedOut:
    """Queue the worker's picture job when any page lacks a picture and none is already queued."""
    revision = _revision(session, project_id, package_id)
    page_ids = list(
        session.scalars(
            select(Page.id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .where(PackageRevisionDocument.package_revision_id == revision.id)
        )
    )
    if not pages_without_pictures(session, page_ids):
        return PagePicturesQueuedOut(queued=False)
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
    return PagePicturesQueuedOut(queued=True)
