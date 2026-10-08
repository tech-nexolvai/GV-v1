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
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.api.review import _session_is_in_project
from app.auth import Action, Principal, require_action, require_project_access
from app.models.package import Package, PackageRevision, PackageState
from app.models.signed_exports import (
    ApprovalExportBundle,
    ApprovalExportFailure,
    ApprovalExportSnapshot,
)
from app.models.verdicts import OutputArtifact, OutputArtifactKind
from app.review.approval import (
    ApprovalNotAuthorised,
    ApprovalRefused,
    approval_readiness,
    approve_package,
)
from app.review.publication import UnapprovedContent, sign_off
from app.review.signed_exports import SignedExportRefused, load_snapshot, request_signed_exports
from storage.store import ArtifactStore

router = APIRouter(tags=["approvals"])

NOT_FOUND_DETAIL: Final = "Not found"


class ApprovalOut(BaseModel):
    """What a sign-off produced."""

    approval_id: UUID
    package_revision_id: UUID
    approved_by: str
    findings_approved: int
    state: str


class ApprovalReadinessOut(BaseModel):
    revision_id: UUID
    can_approve: bool
    blocking_findings: int
    blocking_finding_ids: tuple[UUID, ...]
    reason: str | None


@router.get(
    "/projects/{project_id}/packages/{package_id}/approval-readiness",
    response_model=ApprovalReadinessOut,
)
def get_approval_readiness(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ApprovalReadinessOut:
    revision = _revision(session, project_id, package_id)
    result = approval_readiness(session, revision.id)
    return ApprovalReadinessOut(
        revision_id=result.revision_id,
        can_approve=result.can_approve,
        blocking_findings=result.blocking_findings,
        blocking_finding_ids=result.blocking_finding_ids,
        reason=result.reason,
    )


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
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
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
    if not _session_is_in_project(session, project_id, review_session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    try:
        decision = approve_package(
            session, principal=principal, review_session_id=review_session_id
        )
        session.commit()
    except ApprovalNotAuthorised as refusal:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(refusal)) from refusal
    except (ApprovalRefused, SignedExportRefused) as refusal:
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


def _download_artifact(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    *,
    kind: OutputArtifactKind,
    extension: str,
    label: str,
) -> Response:
    """One signed-off output artifact, streamed as the immutable recorded bytes.

    **Approval is the gate, not the artifact's existence.** `generate_outputs` writes both reports as
    soon as the checks have run, which is before anybody has read a finding. Serving either then would let a
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

    try:
        signed = sign_off(session, revision.id)
        snapshot = session.scalar(
            select(ApprovalExportSnapshot).where(
                ApprovalExportSnapshot.approval_id == signed.approval_id
            )
        )
        if snapshot is None:
            raise SignedExportRefused(
                "signed exports have not been requested for this approval; before-review files are not final reports"
            )
        load_snapshot(session, snapshot)
        bundle = session.scalar(
            select(ApprovalExportBundle).where(ApprovalExportBundle.snapshot_id == snapshot.id)
        )
        if bundle is None:
            if (
                session.scalar(
                    select(ApprovalExportFailure.id)
                    .where(ApprovalExportFailure.snapshot_id == snapshot.id)
                    .limit(1)
                )
                is not None
            ):
                raise SignedExportRefused(
                    "signed export generation failed; no final files were published. Check worker logs and retry availability; before-review files are not final reports"
                )
            raise SignedExportRefused(
                "signed exports are being prepared; before-review files cannot be downloaded as final reports"
            )
        artifacts = {}
        for artifact_kind, artifact_id in (
            (OutputArtifactKind.FINDINGS_PDF, bundle.pdf_id),
            (OutputArtifactKind.FINDINGS_WORKBOOK, bundle.workbook_id),
            (OutputArtifactKind.REDLINE, bundle.redline_id),
        ):
            item = session.get(OutputArtifact, artifact_id)
            if (
                item is None
                or item.package_revision_id != revision.id
                or item.kind != artifact_kind.value
                or item.findings != len(signed.finding_ids)
            ):
                raise SignedExportRefused(
                    "signed export bundle is incomplete or belongs to a different revision"
                )
            artifacts[artifact_kind] = item
        artifact = artifacts[kind]
    except (SignedExportRefused, UnapprovedContent) as refusal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(refusal),
        ) from refusal

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
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="gv-review-{package_id}-r{revision.revision_number}.{extension}"'
            )
        },
    )


class SignedExportRequestOut(BaseModel):
    approval_id: UUID
    status: Literal["not_requested", "preparing", "ready", "failed"]


@router.get(
    "/projects/{project_id}/packages/{package_id}/signed-exports",
    response_model=SignedExportRequestOut,
)
def export_status(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> SignedExportRequestOut:
    """Read availability only; downloading or inspecting never generates files."""
    revision = _revision(session, project_id, package_id)
    if revision.state != PackageState.APPROVED.value:
        raise HTTPException(status_code=409, detail="this package has not been signed off")
    try:
        signed = sign_off(session, revision.id)
        snapshot = session.scalar(
            select(ApprovalExportSnapshot).where(
                ApprovalExportSnapshot.approval_id == signed.approval_id
            )
        )
        if snapshot is None:
            return SignedExportRequestOut(approval_id=signed.approval_id, status="not_requested")
        load_snapshot(session, snapshot)
        bundle = session.scalar(
            select(ApprovalExportBundle.id).where(ApprovalExportBundle.snapshot_id == snapshot.id)
        )
        if (
            bundle is None
            and session.scalar(
                select(ApprovalExportFailure.id)
                .where(ApprovalExportFailure.snapshot_id == snapshot.id)
                .limit(1)
            )
            is not None
        ):
            return SignedExportRequestOut(approval_id=signed.approval_id, status="failed")
        return SignedExportRequestOut(
            approval_id=signed.approval_id, status="ready" if bundle is not None else "preparing"
        )
    except (SignedExportRefused, UnapprovedContent) as refusal:
        raise HTTPException(status_code=409, detail=str(refusal)) from refusal


@router.post(
    "/projects/{project_id}/packages/{package_id}/signed-exports",
    response_model=SignedExportRequestOut,
    status_code=202,
)
def request_exports(
    _access: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.APPROVE_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> SignedExportRequestOut:
    """Explicitly prepare the signed files for an existing approval, without signing again."""
    revision = _revision(session, project_id, package_id)
    if revision.state != PackageState.APPROVED.value:
        raise HTTPException(
            status_code=409, detail="sign off this package before requesting signed exports"
        )
    try:
        signed = sign_off(session, revision.id)
        snapshot = request_signed_exports(session, signed.approval_id)
        ready = (
            session.scalar(
                select(ApprovalExportBundle.id).where(
                    ApprovalExportBundle.snapshot_id == snapshot.id
                )
            )
            is not None
        )
        session.commit()
    except (SignedExportRefused, UnapprovedContent) as refusal:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(refusal)) from refusal
    return SignedExportRequestOut(
        approval_id=signed.approval_id, status="ready" if ready else "preparing"
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
    """The signed-off workbook: the tabular audit handoff."""
    return _download_artifact(
        _access,
        session,
        store,
        project_id,
        package_id,
        kind=OutputArtifactKind.FINDINGS_WORKBOOK,
        extension="xlsx",
        label="findings workbook",
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/report.pdf",
    response_class=Response,
    summary="Download the signed-off review as a PDF",
)
def download_pdf_report(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
) -> Response:
    """The branded signed-off PDF: the readable reviewer handoff."""
    return _download_artifact(
        _access,
        session,
        store,
        project_id,
        package_id,
        kind=OutputArtifactKind.FINDINGS_PDF,
        extension="pdf",
        label="findings PDF",
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/redline.pdf",
    response_class=Response,
    summary="Download the signed-off evidence-grounded drawing redline",
)
def download_redline(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
) -> Response:
    """The signed-off redline, available only for typed findings with real stored locations."""
    return _download_artifact(
        _access,
        session,
        store,
        project_id,
        package_id,
        kind=OutputArtifactKind.REDLINE,
        extension="pdf",
        label="evidence-grounded redline",
    )
