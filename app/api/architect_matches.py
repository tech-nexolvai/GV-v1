"""The reviewer's one-click choice of the architect's view for a countertop row (#1166).

When the architect's drawings are a separate PDF, each vendor countertop row is matched with one of
its views (`workflow/architect_matching.py`). Automatic only when code and both AIs agree (decision
D1); otherwise the reviewer chooses here, from every view of the file ranked by code, with nothing
pre-selected:

* `GET .../slot-rows/{row_id}/architect-view-match`: the row's current match and every candidate
  view, with code's evidence, which AI picked it, and whether a reviewer chose it for the same item
  on an earlier revision (`remembered`, never pre-selected);
* `POST` the same path: a view, or "none of these", as a new record superseding the latest; refused
  with 409 when the row's match moved on since the reviewer looked (`expected_record_id`), and with
  422 for a view not offered. Sign-off then waits for the checks to run again
  (`app/review/approval.py`);
* `GET .../architect-views/{view_id}/picture`: the stored picture of one view, digest-checked.

Reads the records only (`workflow/architect_match_records.py`): no drawing is read in a request.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.api.drawing_views import _revision
from app.api.slot_rows import _current_row
from app.audit.events import AuditCategory, emit
from app.auth import Principal, require_action, require_project_access
from app.auth.roles import Action
from app.models import PackageRevisionDocument, Page
from app.models.evidence import ArchitectViewIndexEntry, ArchitectViewMatchRecord
from app.review.architect_views_out import API_PREFIX, PICTURE_PATH
from app.review.architect_views_out import view_out as _view_out
from app.review.architect_views_out import views_by_id as _views
from app.review.row_location import row_location
from app.schemas.architect_matches import (
    ArchitectCandidateCodeOut,
    ArchitectMatchCurrentOut,
    ArchitectMatchVendorOut,
    ArchitectViewCandidateOut,
    ArchitectViewMatchOut,
    ArchitectViewPickIn,
)
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
from storage.store import ArtifactStore
from workflow.architect_match_records import (
    ReviewerMatchRefused,
    ReviewerMatchStale,
    latest_match_record,
    record_reviewer_match,
)
from workflow.architect_pairing_records import record_code_pairing_for_view
from workflow.slot_row_scope import SlotRow

router = APIRouter(tags=["architect view matches"])

_NOT_FOUND: Final = "Not found"


def candidate_order(
    record: ArchitectViewMatchRecord,
) -> Literal["code", "ais_then_code", "code_ais_not_asked"]:
    """How the record's candidates were ordered (`ArchitectMatcher._candidates`, #1166): when the
    AIs were asked (the record names their questions) and code had no pick, the views both AIs
    called the same come first; with a code pick, code's order; with no question, code's order.
    A reviewer's record copies these from the record it answered."""
    questions = (record.details or {}).get("questions")
    if not isinstance(questions, list) or not questions:
        return "code_ais_not_asked"
    return "code" if record.code_pick_view_id is not None else "ais_then_code"


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _summary(score: dict[str, object]) -> str:
    parts: list[str] = []
    if score.get("reference_match") is True:
        parts.append("The vendor's sheet prints a reference to this view.")
    error = score.get("run_length_error_display")
    if isinstance(error, str):
        parts.append(
            f"Its run is {error} in off the vendor's"
            + (" (fits)." if score.get("fits") is True else ".")
        )
    else:
        parts.append("Its run could not be measured against the vendor's.")
    bays_vendor, bays_architect = _int(score.get("bays_vendor")), _int(score.get("bays_architect"))
    if bays_vendor is not None and bays_architect is not None:
        parts.append(f"{bays_architect} bays against the vendor's {bays_vendor}.")
    return " ".join(parts)


def _match_out(
    session: Session, row: SlotRow, *, project_id: UUID, package_id: UUID
) -> ArchitectViewMatchOut:
    current = latest_match_record(session, row.anchor.id)
    raw = [] if current is None else [item for item in current.candidates if isinstance(item, dict)]
    ids: set[UUID] = set()
    for item in raw:
        try:
            ids.add(UUID(str(item.get("view_id"))))
        except ValueError:
            continue
    views = _views(session, ids)
    candidates: list[ArchitectViewCandidateOut] = []
    for item in sorted(raw, key=lambda item: _int(item.get("rank")) or 0):
        try:
            view_id = UUID(str(item.get("view_id")))
        except ValueError:
            continue
        found = views.get(view_id)
        if found is None:
            continue
        entry, page_number, document_id = found
        score = item.get("score") if isinstance(item.get("score"), dict) else {}
        assert isinstance(score, dict)
        evidence = item.get("evidence")
        picked = item.get("ai_picked_by")
        error_display = score.get("run_length_error_display")
        candidates.append(
            ArchitectViewCandidateOut(
                rank=_int(item.get("rank")) or 0,
                view=_view_out(
                    entry, page_number, document_id, project_id=project_id, package_id=package_id
                ),
                shown_to_ais=item.get("shown_number") is not None,
                code=ArchitectCandidateCodeOut(
                    fits=score.get("fits") is True,
                    reference_match=score.get("reference_match") is True,
                    run_length_error_display=(
                        error_display if isinstance(error_display, str) else None
                    ),
                    bays_vendor=_int(score.get("bays_vendor")),
                    bays_architect=_int(score.get("bays_architect")),
                    pair_support=_int(score.get("pair_support")),
                ),
                score_summary=_summary(score),
                evidence=[str(text) for text in evidence] if isinstance(evidence, list) else [],
                ai_picked_by=[str(model) for model in picked] if isinstance(picked, list) else [],
                remembered=item.get("remembered") is True,
                can_pick=True,
                refusal=None,
            )
        )
    vendor_page = session.get(Page, row.anchor.page_id)
    return ArchitectViewMatchOut(
        row_id=row.anchor.id,
        vendor=ArchitectMatchVendorOut(
            page_number=row.page_number if vendor_page is None else vendor_page.index + 1,
            document_version_id=row.anchor.document_version_id,
            title=None if current is None else current.vendor_title,
            references=[] if current is None else [str(ref) for ref in current.vendor_references],
            region=row_location(session, row.anchor.id),
        ),
        current=(
            None
            if current is None
            else ArchitectMatchCurrentOut(
                record_id=current.id,
                status=current.status,
                source=current.source,
                decided_by=current.decided_by,
                decided_at=current.created_at,
                supersedes_id=current.supersedes_id,
                note=current.note,
                reasons=[str(reason) for reason in current.reasons],
            )
        ),
        candidates=candidates,
        can_choose_none=True,
        order=None if current is None else candidate_order(current),
    )


_MATCH_PATH: Final = (
    "/projects/{project_id}/packages/{package_id}/slot-rows/{row_id}/architect-view-match"
)


@router.get(
    _MATCH_PATH,
    response_model=ArchitectViewMatchOut,
    summary="Show which of the architect's views this countertop row is matched with, and the "
    "candidates",
)
def get_architect_view_match(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    row_id: UUID,
) -> ArchitectViewMatchOut:
    revision = _revision(session, project_id, package_id)
    row = _current_row(session, revision.id, row_id)
    return _match_out(session, row, project_id=project_id, package_id=package_id)


@router.post(
    _MATCH_PATH,
    response_model=ArchitectViewMatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="Choose which of the architect's views shows this countertop (or none of them)",
)
def pick_architect_view(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    row_id: UUID,
    body: ArchitectViewPickIn,
) -> ArchitectViewMatchOut:
    revision = _revision(session, project_id, package_id)
    row = _current_row(session, revision.id, row_id)
    try:
        record = record_reviewer_match(
            session,
            anchor=row.anchor,
            package_revision_id=revision.id,
            view_id=body.view_id,
            none_of_these=body.none_of_these,
            note=body.note,
            actor=principal.id,
            expected_record_id=body.expected_record_id,
        )
    except ReviewerMatchStale as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ReviewerMatchRefused as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    try:
        # Flushed first: two reviewers superseding the same record collide on its unique index here.
        session.flush()
        # The picked view's stored code pairing becomes the row's pairing (#1167): one judgment,
        # so the reviewer confirms the pairing as today.
        record_code_pairing_for_view(
            session, anchor=row.anchor, package_revision_id=revision.id, match_record=record
        )
        emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor=principal.id,
            target_id=record.id,
            target_type="architect_view_match",
        )
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This row's architect view match was just updated. Reload it before choosing "
            "again.",
        ) from error
    return _match_out(
        session,
        _current_row(session, revision.id, row_id),
        project_id=project_id,
        package_id=package_id,
    )


@router.get(
    PICTURE_PATH,
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}
        }
    },
    summary="View the stored picture of one of the architect's views",
)
def architect_view_picture(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    project_id: UUID,
    package_id: UUID,
    view_id: UUID,
) -> Response:
    """The picture of a view of this package's architect file, only while its bytes still match the
    digest recorded for them; 404 for a view of another package or one with no picture."""
    revision = _revision(session, project_id, package_id)
    entry = session.execute(
        select(ArchitectViewIndexEntry)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id
            == ArchitectViewIndexEntry.document_version_id,
        )
        .where(
            ArchitectViewIndexEntry.id == view_id,
            PackageRevisionDocument.package_revision_id == revision.id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if entry is None or entry.picture_storage_key is None or entry.picture_sha256 is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    try:
        with store.get(entry.picture_storage_key) as stored:
            content = stored.read()
    except (ArtifactCorrupt, FileNotFoundError, IntegrityRecordMissing) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored picture of this architect view is unavailable",
        ) from error
    if hashlib.sha256(content).hexdigest() != entry.picture_sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the stored picture of this architect view failed its integrity check",
        )
    return Response(content=content, media_type="image/png", headers={"Cache-Control": "no-store"})


__all__ = [
    "API_PREFIX",
    "PICTURE_PATH",
    "architect_view_picture",
    "get_architect_view_match",
    "pick_architect_view",
    "router",
]
