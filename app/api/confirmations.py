"""What was read, and a reviewer saying what it is (#530).

Two endpoints, and between them the join that had never existed. Extraction produced untyped readings
and stopped; the reviewer form asked for numbers and never showed what had been read. So a person
could look at a package that had been fully extracted and see nothing of it, and the checks were
judged on what they retyped.

**The list shows the value and the crop together, and that is not decoration.** Confirming a reading
records `HUMAN_CONFIRMED`, which is a claim about the whole reading — that the value is what the
drawing says *and* that it is this quantity. A picker that showed only a type would be collecting a
signature for something nobody looked at.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Principal, require_project_access
from app.evidence.confirm import ConfirmationRefused, RefusalReason, confirm_candidate_type
from app.models.document import DocumentVersion, PackageRevisionDocument, Page
from app.models.evidence import EvidenceArtifact, ObservationCandidate
from app.models.package import Package, PackageRevision
from rules.semantic_types import SemanticType
from units.imperial import format_inches

router = APIRouter(tags=["confirmations"])

#: The same words for a package that does not exist and one the caller may not see.
NOT_FOUND_DETAIL: Final = "Not found"

#: How many readings one response carries.
#:
#: A drawing page yields hundreds of text runs and most are not dimensions. The cap keeps one request
#: bounded; a package with more than this needs the filtering that #531 would add rather than a
#: response nobody can read.
MAX_CANDIDATES = 500

#: The refusals a caller caused, and the ones that describe the stored data.
#:
#: Split because they mean different things to a client: the first is a bad request it can correct,
#: the second is a fact about the package that no retry will change.
_CALLER_ERROR = {RefusalReason.UNKNOWN_TYPE, RefusalReason.NO_SUCH_CANDIDATE}


class CandidateOut(BaseModel):
    """One extracted reading, as a reviewer needs to see it before naming it."""

    candidate_id: UUID
    page_index: int
    raw_text: str
    value: str | None = Field(
        default=None,
        description=(
            "The reading as exact text, `25 1/2 in`. Null when the token carried no unit and was "
            "recorded without a value, which is most text on a drawing."
        ),
    )
    crop_key: str | None = Field(
        default=None,
        description="Storage key of the crop of this reading's region, when one was cut.",
    )
    corroboration_status: str | None = None
    corroboration_lane: str | None = None


class CandidatesOut(BaseModel):
    """Everything read for this package that nobody has yet said the meaning of."""

    candidates: tuple[CandidateOut, ...]
    total: int


class ConfirmIn(BaseModel):
    """A reviewer naming what a reading is."""

    semantic_type: str = Field(
        description="A member of the rulebook's vocabulary, e.g. `CT010`. Free text is refused."
    )


class ConfirmedOut(BaseModel):
    """The evidence a confirmation produced."""

    canonical_observation_id: UUID
    semantic_type: str
    status: str


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


@router.get(
    "/projects/{project_id}/packages/{package_id}/candidates",
    response_model=CandidatesOut,
    summary="What the extractor read, waiting for somebody to say what it is",
)
def list_candidates(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> CandidatesOut:
    """Every reading of this package that carries a value, with its crop.

    **Only readings that carry a value.** A token with no unit was recorded without one — deliberately,
    because a bare `38` is a dimension whose unit is unknown — and there is nothing for a reviewer to
    confirm about it. They are still in the database, and a reviewer who wants to see what was thrown
    away is asking a different question than this one.

    Ordered by page and then by when it was read, so the list follows the drawing rather than the
    database's insertion order, and two loads put the same reading in the same place.
    """
    revision = _revision(session, project_id, package_id)

    rows = session.execute(
        select(ObservationCandidate, Page.index, EvidenceArtifact.storage_key)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .outerjoin(EvidenceArtifact, EvidenceArtifact.candidate_id == ObservationCandidate.id)
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            ObservationCandidate.value_numerator.is_not(None),
        )
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
        .limit(MAX_CANDIDATES + 1)
    ).all()

    if len(rows) > MAX_CANDIDATES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"this package has more than {MAX_CANDIDATES} readings with values. Nothing is "
                "returned rather than a partial list, because a truncated one cannot be told from a "
                "complete one."
            ),
        )

    return CandidatesOut(
        candidates=tuple(
            CandidateOut(
                candidate_id=row.id,
                page_index=page_index,
                raw_text=row.raw_text,
                value=(
                    None
                    if row.value_numerator is None or row.value_denominator is None
                    else f"{format_inches(Fraction(row.value_numerator, row.value_denominator))} "
                    f"{row.unit}"
                ),
                crop_key=crop_key,
                corroboration_status=row.corroboration_status,
                corroboration_lane=row.corroboration_lane,
            )
            for row, page_index, crop_key in rows
        ),
        total=len(rows),
    )


@router.get(
    "/semantic-types",
    response_model=tuple[str, ...],
    summary="The vocabulary a reviewer may choose from",
)
def list_semantic_types(
    _access: Annotated[Principal, Depends(require_project_access)],
) -> tuple[str, ...]:
    """Read from `rules/semantic_types.py` rather than listed here.

    A hand-kept copy is right the day it is written and wrong the first time the vocabulary changes —
    and the reviewer would then be offered a type no rule reads, or denied one every rule needs.
    """
    return tuple(member.value for member in SemanticType)


@router.post(
    "/projects/{project_id}/packages/{package_id}/candidates/{candidate_id}/confirm",
    response_model=ConfirmedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Say what an extracted reading is, so the rules can use it",
)
def confirm_candidate(
    principal: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    candidate_id: UUID,
    body: ConfirmIn,
) -> ConfirmedOut:
    """Turn one reading into evidence a rule may be run against.

    The value is not in the request, and that is the point of the whole story: the reviewer is
    confirming the number the extractor read, not supplying one. A body carrying a value would be the
    old form path wearing a new name.

    Runs in the request rather than a background task because it is one row's worth of work and a
    reviewer is waiting on the answer — and because the audit event naming them has to commit with it.
    """
    _revision(session, project_id, package_id)

    result = confirm_candidate_type(
        session,
        candidate_id=candidate_id,
        semantic_type=body.semantic_type,
        confirmed_by=principal.id,
    )
    if isinstance(result, ConfirmationRefused):
        raise HTTPException(
            status_code=(
                status.HTTP_400_BAD_REQUEST
                if result.reason in _CALLER_ERROR
                else status.HTTP_409_CONFLICT
            ),
            detail=result.detail,
        )

    return ConfirmedOut(
        canonical_observation_id=result.id,
        semantic_type=result.semantic_type,
        status=result.status,
    )
