"""Confirm or correct evidence without rewriting what extraction originally recorded.

A review action, the original canonical observation and the resulting HUMAN_CONFIRMED
observation are joined by foreign keys.  Corrections additionally write the append-only
correction ledger before the caller's transaction commits.  Nothing here commits: the
caller owns one transaction containing the entire decision.

Source: ``docs/DESIGN_PRODUCT.md`` section 4 and issue #230.
Verification: ``tests/review/test_evidence_actions.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.auth import Action, Principal
from app.models.evidence import (
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
)
from app.models.review import ReviewAction, ReviewActionKind, ReviewSession
from app.models.verdicts import Finding, FindingEvidence
from app.review.ledger import record_correction
from evidence.canonical import CorroborationLane, EvidenceStatus


class EvidenceActionRefused(Exception):
    """A review request that cannot safely create trusted evidence."""


class EvidenceConfirmationNotAuthorised(EvidenceActionRefused):
    """The principal does not hold the evidence-confirmation permission."""


@dataclass(frozen=True, slots=True)
class EvidenceDecision:
    """The append-only records produced by one human evidence decision."""

    action: ReviewAction
    original: CanonicalObservation
    resulting: CanonicalObservation


def _authorise(principal: Principal) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
    if not principal.id.strip():
        raise EvidenceActionRefused("an evidence decision must name the human who made it")
    if not principal.may(Action.CONFIRM_EVIDENCE):
        raise EvidenceConfirmationNotAuthorised(
            "this principal may not confirm evidence; reviewer or admin authority is required"
        )


def _context(
    db: Session,
    *,
    review_session_id: UUID,
    finding_id: UUID,
    observation_id: UUID,
) -> tuple[ReviewSession, Finding, CanonicalObservation]:
    review_session = db.get(ReviewSession, review_session_id)
    finding = db.get(Finding, finding_id)
    original = db.get(CanonicalObservation, observation_id)
    if review_session is None or finding is None or original is None:
        raise EvidenceActionRefused("the review session, finding or observation does not exist")
    if review_session.completed_at is not None:
        raise EvidenceActionRefused("the review session has already ended")
    if finding.package_revision_id != review_session.package_revision_id:
        raise EvidenceActionRefused("the finding is outside this review session")
    linked = db.scalar(
        select(FindingEvidence.id).where(
            FindingEvidence.finding_id == finding.id,
            FindingEvidence.canonical_observation_id == original.id,
        )
    )
    if linked is None:
        raise EvidenceActionRefused("the observation is not evidence for this finding")
    return review_session, finding, original


def _canonical_value(observation: CanonicalObservation) -> str:
    """Render the complete corrected fact deterministically for the text ledger."""

    return json.dumps(
        {
            "semantic_type": observation.semantic_type,
            "unit": observation.unit,
            "value": f"{observation.value_numerator}/{observation.value_denominator}",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _new_observation(
    db: Session,
    original: CanonicalObservation,
    *,
    value: Fraction | None,
) -> CanonicalObservation:
    if value is None:
        numerator = original.value_numerator
        denominator = original.value_denominator
        unit = original.unit
    else:
        numerator = value.numerator
        denominator = value.denominator
        unit = original.unit

    resulting = CanonicalObservation(
        document_version_id=original.document_version_id,
        page_id=original.page_id,
        document_role=original.document_role,
        polygon=original.polygon,
        coordinate_space=original.coordinate_space,
        semantic_type=original.semantic_type,
        value_numerator=numerator,
        value_denominator=denominator,
        unit=unit,
        status=EvidenceStatus.HUMAN_CONFIRMED.value,
        authority=original.authority,
        evidence_crop_uri=original.evidence_crop_uri,
    )
    db.add(resulting)
    db.flush()

    support = db.scalars(
        select(EvidenceSupportingCandidate).where(
            EvidenceSupportingCandidate.canonical_observation_id == original.id
        )
    ).all()
    for link in support:
        db.add(
            EvidenceSupportingCandidate(
                canonical_observation_id=resulting.id,
                candidate_id=link.candidate_id,
                role=link.role,
            )
        )
    lanes = {
        lane
        for lane in db.scalars(
            select(EvidenceCorroborationLane.lane).where(
                EvidenceCorroborationLane.canonical_observation_id == original.id
            )
        )
    }
    lanes.add(CorroborationLane.HUMAN.value)
    for lane in sorted(lanes):
        db.add(EvidenceCorroborationLane(canonical_observation_id=resulting.id, lane=lane))
    db.flush()
    return resulting


def _decide(
    db: Session,
    *,
    principal: Principal,
    review_session_id: UUID,
    finding_id: UUID,
    observation_id: UUID,
    kind: ReviewActionKind,
    corrected_value: Fraction | None,
) -> EvidenceDecision:
    # Authorization precedes every lookup so an unauthorized caller learns nothing about ids.
    _authorise(principal)
    review_session, finding, original = _context(
        db,
        review_session_id=review_session_id,
        finding_id=finding_id,
        observation_id=observation_id,
    )
    if corrected_value is not None and not isinstance(corrected_value, Fraction):
        raise TypeError("corrected_value must be an exact Fraction")

    resulting = _new_observation(db, original, value=corrected_value)
    action = ReviewAction(
        review_session_id=review_session.id,
        finding_id=finding.id,
        package_revision_id=finding.package_revision_id,
        action=kind.value,
        actor=principal.id,
        note=None,
        original_observation_id=original.id,
        resulting_observation_id=resulting.id,
    )
    db.add(action)
    db.flush()

    if kind is ReviewActionKind.CORRECT:
        record_correction(
            db,
            review_action_id=action.id,
            canonical_observation_id=original.id,
            original=_canonical_value(original),
            corrected=_canonical_value(resulting),
        )
    return EvidenceDecision(action, original, resulting)


def confirm_evidence(
    db: Session,
    *,
    principal: Principal,
    review_session_id: UUID,
    finding_id: UUID,
    observation_id: UUID,
) -> EvidenceDecision:
    """Create a named HUMAN_CONFIRMED copy of one finding's evidence."""

    return _decide(
        db,
        principal=principal,
        review_session_id=review_session_id,
        finding_id=finding_id,
        observation_id=observation_id,
        kind=ReviewActionKind.CONFIRM,
        corrected_value=None,
    )


def correct_evidence(
    db: Session,
    *,
    principal: Principal,
    review_session_id: UUID,
    finding_id: UUID,
    observation_id: UUID,
    corrected_value: Fraction,
) -> EvidenceDecision:
    """Correct the exact value in its authored unit and write the ledger atomically."""

    return _decide(
        db,
        principal=principal,
        review_session_id=review_session_id,
        finding_id=finding_id,
        observation_id=observation_id,
        kind=ReviewActionKind.CORRECT,
        corrected_value=corrected_value,
    )


def confirmed_for_same_evidence(
    db: Session, *, observation_id: UUID
) -> CanonicalObservation | None:
    """Return the latest human result for an identical immutable evidence reading.

    Identity includes document version, page, polygon, semantic association, exact value,
    unit and authority.  A different document version is never carried forward merely
    because it contains the same number at the same coordinates.
    """

    observed = db.get(CanonicalObservation, observation_id)
    if observed is None:
        raise EvidenceActionRefused("the observation does not exist")
    if observed.status == EvidenceStatus.HUMAN_CONFIRMED.value:
        return observed

    original = aliased(CanonicalObservation)
    resulting = aliased(CanonicalObservation)
    statement = (
        select(resulting)
        .join(ReviewAction, ReviewAction.resulting_observation_id == resulting.id)
        .join(original, ReviewAction.original_observation_id == original.id)
        .where(
            original.document_version_id == observed.document_version_id,
            original.page_id == observed.page_id,
            original.polygon == observed.polygon,
            original.coordinate_space == observed.coordinate_space,
            original.semantic_type == observed.semantic_type,
            original.value_numerator == observed.value_numerator,
            original.value_denominator == observed.value_denominator,
            original.unit == observed.unit,
            original.authority == observed.authority,
        )
        .order_by(ReviewAction.created_at.desc(), ReviewAction.id.desc())
        .limit(1)
    )
    return db.scalars(statement).first()
