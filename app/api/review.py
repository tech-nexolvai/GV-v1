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
from app.auth import Action, Principal, require_action, require_project_access
from app.models.evidence import CanonicalObservation
from app.models.package import Package, PackageRevision
from app.models.review import ReviewActionKind, ReviewSession
from app.review.evidence_actions import (
    EvidenceActionRefused,
    EvidenceConfirmationNotAuthorised,
    confirm_evidence,
    correct_evidence,
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
    RevisionSuperseded,
    SessionAlreadyComplete,
    complete_session,
    grant_exception,
    open_session,
    record_action,
)
from app.schemas.review import (
    DecidedEvidenceOut,
    DecideEvidence,
    GrantException,
    OpenReviewSession,
    RecordAction,
    ReviewActionOut,
    ReviewExceptionOut,
    ReviewSessionOut,
    ReviewSessionPage,
)
from units.measurement import Unit
from units.normalise import UnitNormalisationError, normalise_to_inches

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
_UNPROCESSABLE = (ExceptionNeedsAReason, ExceptionAlreadyOver, ExceptionScopeMismatch)
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


def _rendered(observation: CanonicalObservation) -> str:
    """One observation as the ledger stores it: exact, with its unit and type, never a float."""
    return (
        f"{observation.value_numerator}/{observation.value_denominator} "
        f"{observation.unit} ({observation.semantic_type})"
    )


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
    return measured.exact


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
    "/projects/{project_id}/review-sessions/{review_session_id}/evidence",
    response_model=DecidedEvidenceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm or correct one reading behind a finding",
)
def decide_evidence(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    db: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    review_session_id: UUID,
    body: DecideEvidence,
) -> DecidedEvidenceOut:
    """**The way into the correction ledger, which had none.**

    `correct_evidence` writes a `correction_ledger` entry in the same transaction as its action row,
    exactly as `AGENTS.md` §2.6 requires — and it had no route, so no reviewer could ever create
    one and `D5.4`'s correction rate measured an empty table. That reads as "no corrections were
    needed", which is the most flattering possible account of a system nobody can correct.

    A correction never edits the reading the system made. The original observation stays and a new
    `HUMAN_CONFIRMED` one is written beside it, which is what keeps "what did we get wrong?"
    answerable.

    **The corrected value is parsed, not trusted.** It arrives as typed, with its unit, and
    `normalise_to_inches` refuses a bare number — the same refusal `app/api/measurements.py`
    inherits deliberately, because a `984` whose `mm` was lost once became 984 inches.
    """
    if not _session_is_in_project(db, project_id, review_session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    corrected: Fraction | None = None
    if body.corrected_value is not None:
        corrected = _corrected_inches(db, body.observation_id, body.corrected_value)

    try:
        if body.action is ReviewActionKind.CORRECT:
            assert corrected is not None
            decision = correct_evidence(
                db,
                principal=principal,
                review_session_id=review_session_id,
                finding_id=body.finding_id,
                observation_id=body.observation_id,
                corrected_value=corrected,
            )
        else:
            decision = confirm_evidence(
                db,
                principal=principal,
                review_session_id=review_session_id,
                finding_id=body.finding_id,
                observation_id=body.observation_id,
            )
        db.commit()
    except Exception as error:
        db.rollback()
        raise _refuse(error) from error

    return DecidedEvidenceOut(
        action=ReviewActionOut.model_validate(decision.action),
        original_observation_id=decision.original.id,
        resulting_observation_id=decision.resulting.id,
        original_value=_rendered(decision.original),
        resulting_value=_rendered(decision.resulting),
    )


@router.post(
    "/projects/{project_id}/review-sessions/{review_session_id}/exceptions",
    response_model=ReviewExceptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Accept one specific deviation, until a date",
)
def grant_review_exception(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    db: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    review_session_id: UUID,
    body: GrantException,
) -> ReviewExceptionOut:
    """**The way into the exceptions table, which had none.**

    `ReviewException` appeared only in the models and the tests: nothing granted one and
    `apply_exceptions` had no caller, so the whole control was inert in both directions.

    The expiry is required at every layer and this is the last of them — the column is `NOT NULL`,
    `ExceptionGrant` has no default for it, the schema refuses its absence, and `grant_exception`
    refuses one already past. A permanent silent exception is not representable, which is the
    control: somebody has to look again.

    The approver is the caller. Never a field.
    """
    if not _session_is_in_project(db, project_id, review_session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    try:
        # Not `_`: that name is already the authorisation dependency in this signature, and binding
        # the action to it would make the two the same variable.
        _action, granted = grant_exception(
            db,
            review_session_id=review_session_id,
            finding_id=body.finding_id,
            actor=principal.id,
            scope=body.scope,
            scope_id=body.scope_id,
            reason=body.reason,
            expires_at=body.expires_at,
        )
        db.commit()
    except Exception as error:
        db.rollback()
        raise _refuse(error) from error

    return ReviewExceptionOut.model_validate(granted)


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
