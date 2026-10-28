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

from fractions import Fraction
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.signed_off import ensure_revision_writable, open_revision
from app.auth import Action, Principal, require_action, require_project_access
from app.models.evidence import CanonicalObservation
from app.models.package import Package, PackageRevision
from app.review.evidence_actions import (
    EvidenceActionRefused,
    EvidenceConfirmationNotAuthorised,
)
from app.review.session import (
    ActionOutsideTheSession,
    ActorNotNamed,
    ExceptionAlreadyOver,
    ExceptionNeedsAReason,
    ExceptionScopeMismatch,
    NoSuchFinding,
    NoSuchPackageRevision,
    NoSuchReviewSession,
    ReviewNeedsANote,
    RevisionSuperseded,
    SessionAlreadyComplete,
    open_session,
)
from app.review.sizes import NOT_ABOVE_ZERO, is_positive_size, typed_negative
from app.schemas.review import (
    OpenReviewSession,
    ReviewSessionOut,
)
from units.measurement import Unit
from units.normalise import UnitNormalisationError, normalise_to_inches, typed_fraction_refusal

router = APIRouter(tags=["review"])

#: What every refusal says. Nothing about the project, the package or the reason.
NOT_FOUND_DETAIL = "Not found"

#: Which service refusals mean "this does not exist, as far as you are concerned", and which mean
#: "it exists and the request conflicts with its state". Mapped as data rather than as a chain of
#: `except` clauses so that a new refusal has to be classified rather than falling through to a 500.
_CONFLICT = (RevisionSuperseded, SessionAlreadyComplete, ActionOutsideTheSession)

#: Refusals about the *terms* a reviewer stated, rather than about what they may reach. A missing
#: reason, an expiry already past and a finding-scoped exception naming another finding are all
#: things the caller can fix by sending something else, which is what 422 means.
_UNPROCESSABLE = (
    ExceptionNeedsAReason,
    ExceptionAlreadyOver,
    ExceptionScopeMismatch,
    ReviewNeedsANote,
)
_NOT_FOUND = (NoSuchPackageRevision, NoSuchReviewSession, NoSuchFinding)


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
    if isinstance(error, _UNPROCESSABLE):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))
    if isinstance(error, ActorNotNamed):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    if isinstance(error, EvidenceConfirmationNotAuthorised):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    if isinstance(error, EvidenceActionRefused):
        # The evidence family says only that the session, finding or observation could not be used
        # together — deliberately without saying which, for the reason `NOT_FOUND_DETAIL` gives.
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    raise error


def _corrected_inches(db: Session, observation_id: UUID, typed: str) -> Fraction:
    """A reviewer's typed correction as an exact `Fraction` in the observation's own unit.

    **Inches only, and that is Q12 rather than a shortcut.** `correct_evidence` keeps the original
    observation's unit and replaces only the number, so a value typed in one unit for an
    observation stored in another would have to be converted — and `AGENTS.md` is explicit that mm
    on these drawings is the vendor's machine reference and never a verdict operand. Converting
    `25 1/2"` into millimetres to correct a millimetre observation would put a rounded number where
    an exact one is required. So a non-inch observation is refused with the reason, rather than
    corrected approximately.

    The parse itself is `normalise_to_inches`, which refuses a token with no unit. Inherited on
    purpose: it is the refusal that stops a bare `984` becoming 984 inches (#483).
    """
    observation = db.get(CanonicalObservation, observation_id)
    if observation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if observation.unit != Unit.INCH.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"this reading is stored in {observation.unit}, and a correction keeps the unit it "
                "was authored in. Inches are what decide (Q12), so correcting a non-inch reading "
                "would need a conversion that rounds — which is exactly what must not happen to the "
                "number that gets cut."
            ),
        )
    if is_positive_size(observation.semantic_type) and typed_negative(typed):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"corrected_value: {NOT_ABOVE_ZERO}",
        )
    # `163/8"` typed would be stored as 20 3/8 (#1199): refused, with the split as a suggestion.
    odd = typed_fraction_refusal(typed)
    if odd is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"corrected_value: {odd}"
        )
    try:
        measured = normalise_to_inches(typed)
    except UnitNormalisationError as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f'corrected_value: {refused}. Give the value with its unit — 25 1/2" — because a '
                "number with no unit cannot be read and must not be guessed at."
            ),
        ) from refused
    if is_positive_size(observation.semantic_type) and measured.exact <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"corrected_value: {NOT_ABOVE_ZERO}",
        )
    return measured.exact


@router.post(
    "/projects/{project_id}/packages/{package_id}/review-sessions",
    response_model=ReviewSessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a review session over one package revision",
    dependencies=[Depends(open_revision)],
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
    # The dependency checked the package's newest revision; the body may name an older one (#1182).
    ensure_revision_writable(db, body.package_revision_id)

    try:
        opened = open_session(
            db, package_revision_id=body.package_revision_id, reviewer=principal.id
        )
        db.commit()
    except Exception as error:
        db.rollback()
        raise _refuse(error) from error

    return ReviewSessionOut.model_validate(opened)


