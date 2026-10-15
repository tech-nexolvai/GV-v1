"""The draft cloud PDF, any time before sign-off (#1340).

`POST .../draft-cloud` queues the worker's draft job (202), `GET .../draft-cloud` says where the
newest request stands, and `GET .../draft-cloud.pdf` serves the draft once it is ready. The draft is
the drawing clouded as the signed cloud PDF would be, with a "DRAFT - not signed off" banner on every
page and a front page listing the open items.

**Rendering never happens here** (`tests/api/test_no_heavy_work.py`): this module imports no
renderer. **A draft is never the signed file**: it is stored as `draft_redline`, and the signed
downloads (`app/api/approvals.py`) serve only the `redline` a signed bundle names.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.approvals import _revision
from app.api.dependencies import get_artifact_store, get_session
from app.auth import Principal, require_project_access
from app.models.verdicts import CheckRun, Finding
from storage.store import ArtifactStore
from workflow.draft_cloud import DraftCloud, draft_cloud_status, request_draft_cloud

router = APIRouter(tags=["approvals"])


class DraftCloudOut(BaseModel):
    """Where the newest draft cloud PDF request of a package stands."""

    status: Literal["not_requested", "preparing", "ready", "failed"]
    request_id: UUID | None = Field(
        default=None, description="The newest request; null when none was made."
    )
    asked_at: datetime | None = None
    reason: str | None = Field(
        default=None, description="Only when `status` is `failed`: why, in plain words."
    )
    bytes: int | None = Field(default=None, description="Only when `status` is `ready`.")
    findings: int | None = Field(
        default=None,
        description="Only when `status` is `ready`: how many results the draft covers.",
    )


def _out(draft: DraftCloud) -> DraftCloudOut:
    return DraftCloudOut(
        status=draft.state,
        request_id=draft.request_id,
        asked_at=draft.asked_at,
        reason=draft.reason,
        bytes=None if draft.artifact is None else draft.artifact.size,
        findings=None if draft.artifact is None else draft.artifact.findings,
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/draft-cloud",
    response_model=DraftCloudOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask the worker for a draft cloud PDF (before sign-off)",
)
def request_draft(
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> DraftCloudOut:
    """Queue a draft of the current revision, or answer with the request already waiting.

    `409` while the revision has no results in force: there is nothing to cloud yet.
    """
    revision = _revision(session, project_id, package_id)
    has_results = session.scalar(
        select(Finding.id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .where(Finding.package_revision_id == revision.id, CheckRun.superseded_at.is_(None))
        .limit(1)
    )
    if has_results is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There are no results to draw yet. Run the checks first.",
        )
    request_draft_cloud(session, revision.id, requested_by=principal.id)
    session.commit()
    return _out(draft_cloud_status(session, revision.id))


@router.get(
    "/projects/{project_id}/packages/{package_id}/draft-cloud",
    response_model=DraftCloudOut,
    summary="Where the newest draft cloud PDF request stands",
)
def draft_status(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> DraftCloudOut:
    """Reads records only; asking never renders anything."""
    revision = _revision(session, project_id, package_id)
    return _out(draft_cloud_status(session, revision.id))


@router.get(
    "/projects/{project_id}/packages/{package_id}/draft-cloud.pdf",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}
        }
    },
    summary="Download the newest draft cloud PDF (not signed off)",
)
def download_draft(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
) -> Response:
    """The newest request's draft, as stored; `409` while it is not ready."""
    revision = _revision(session, project_id, package_id)
    draft = draft_cloud_status(session, revision.id)
    if draft.artifact is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                draft.reason
                if draft.state == "failed" and draft.reason
                else f"The draft cloud PDF is not ready ({draft.state.replace('_', ' ')})."
            ),
        )
    content = store.get(draft.artifact.storage_key).read()
    if hashlib.sha256(content).hexdigest() != draft.artifact.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored draft does not match the digest recorded when it was made.",
        )
    return Response(
        content=content,
        media_type=draft.artifact.media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="gv-draft-cloud-{package_id}-r{revision.revision_number}.pdf"'
            ),
            "Cache-Control": "no-store",
        },
    )
