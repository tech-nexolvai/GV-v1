"""Signing a package off, and taking the review away with you (#532).

Two routes that existed as everything except a route. `app/review/approval.py` has been complete and
tested for months — it refuses while any `REVIEW_REQUIRED` finding is unaddressed, writes the
approval, records every finding it covers and moves the package to `APPROVED` — and nothing reachable
called it. The UI's sign-off button closed the review *sitting* instead, which ends a meeting and
approves nothing.

That mattered beyond a missing row. `reports/publication.py:sign_off` refuses to release anything
without an approval, so the deliverable could not leave the building; and a reviewer pressing a button
labelled "Sign off this package" was told they had done something they had not.

**The download is gated on the approval, not on the workbook existing.** `generate_outputs` writes the
file as soon as the checks have run, which is well before anybody has looked at it. Serving it then
would let a package leave in a state nobody signed for — the exact thing ADR-0010 forbids.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models.package import Package, PackageRevision, PackageState
from app.models.verdicts import OutputArtifact
from app.review.approval import (
    ApprovalNotAuthorised,
    ApprovalRefused,
    approve_package,
)
from storage.store import ArtifactStore

router = APIRouter(tags=["approvals"])

NOT_FOUND_DETAIL: Final = "Not found"

#: The workbook's media type, so a browser hands it to a spreadsheet rather than to a text viewer.
WORKBOOK_MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ApprovalOut(BaseModel):
    """What a sign-off produced."""

    approval_id: UUID
    package_revision_id: UUID
    approved_by: str
    findings_approved: int
    state: str


def _revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    """This package's current revision, or 404 in the same words for absent and forbidden."""
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


@router.post(
    "/projects/{project_id}/review-sessions/{review_session_id}/approve",
    response_model=ApprovalOut,
    status_code=status.HTTP_201_CREATED,
    summary="Sign off a package, so its review can leave the building",
)
def approve(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.APPROVE_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    review_session_id: UUID,
) -> ApprovalOut:
    """Approve every finding of the revision this sitting is reviewing.

    The finding set is chosen by the server, not sent by the caller. A client naming the findings it
    approves is a client that can approve a subset and leave the rest looking reviewed — and the
    approval is the record GV stands behind when a vendor disputes a dimension.

    Refuses while any `REVIEW_REQUIRED` finding is unaddressed. That is not a formality: an abstention
    nobody acted on is a check that did not happen, and approving around it would put a package's name
    to a question nobody answered.
    """
    del project_id  # Scope is established by the dependency; the session id is globally unique.

    try:
        decision = approve_package(
            session, principal=principal, review_session_id=review_session_id
        )
        session.commit()
    except ApprovalNotAuthorised as refusal:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(refusal)) from refusal
    except ApprovalRefused as refusal:
        # 409 rather than 400: the request is well-formed and the package is not ready. A client that
        # retries after the reviewer addresses the abstentions will succeed unchanged.
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(refusal)) from refusal
    except Exception:
        session.rollback()
        raise

    return ApprovalOut(
        approval_id=decision.approval.id,
        package_revision_id=decision.approval.package_revision_id,
        approved_by=decision.approval.approved_by,
        findings_approved=len(decision.finding_ids),
        state=decision.state_event.to_state,
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/report",
    response_class=Response,
    summary="Download the signed-off review as a workbook",
)
def download_report(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
) -> Response:
    """The findings workbook for this revision, once somebody has signed for it.

    **Approval is the gate, not the file's existence.** `generate_outputs` writes the workbook as soon
    as the checks have run, which is before anybody has read a finding. Serving it then would let a
    review leave in a state nobody signed for, which is what ADR-0010 forbids — no computed dimension
    reaches a vendor without reviewer sign-off.

    The bytes are streamed from the artifact store rather than rebuilt. Regenerating on download would
    produce a file that could differ from the one the approval covers, and the approval is the record
    of what was agreed.
    """
    revision = _revision(session, project_id, package_id)

    if PackageState(revision.state) is not PackageState.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this package has not been signed off, so its review cannot be downloaded. "
                "Approve it first: a review that left the building unsigned is one nobody stands "
                "behind."
            ),
        )

    artifact = session.execute(
        select(OutputArtifact)
        .where(OutputArtifact.package_revision_id == revision.id)
        .order_by(OutputArtifact.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "no report has been generated for this package. The checks produce it, so a package "
                "with none has not finished running them."
            ),
        )

    content = store.get(artifact.storage_key).read()
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        # The digest is recorded precisely so this can be asked. A reviewer downloading a report that
        # is not the one the approval covers would have no way to know.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the stored report does not match the digest recorded when it was produced, so it "
                "is not the report this package was signed off on"
            ),
        )

    return Response(
        content=content,
        media_type=WORKBOOK_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="gv-review-{package_id}-r{revision.revision_number}.xlsx"'
            )
        },
    )
