"""A reviewer says what an extracted reading *is*, and it becomes evidence (#530).

This is the link between the half of the system that reads a drawing and the half that decides. Both
have worked for months and have never been joined: extraction stopped at untyped candidates, and the
reviewer path typed values into a form from scratch without ever looking at what was read. So
`evidence/normalize.py` and `evidence/gate.py` — the two modules that turn a reading into something a
rule may use — had no production caller at all.

**The ordinary type comes from a person.** The separate automatic lane may qualify only an exact
vector tag attached to the same resolved dimension line as the reading; it never writes
`semantic_guess`, guesses from position, or admits an agent suggestion. Everything else remains a
reviewer confirmation here. See `docs/decisions/SEMANTIC_TYPING_GATE.md`.

**What a confirmation asserts.** `HUMAN_CONFIRMED` is a claim about the whole reading, not only its
type: that the value shown is what the drawing says, *and* that it is this quantity. That is only
honest if the reviewer is shown both, so the endpoint that reaches this hands back the value and the
crop together and the interface shows them side by side. A confirmation flow that displayed a type
picker alone would be collecting a signature for something nobody looked at.

**The candidate is never edited.** `observation_candidates` is append-only, and a confirmation mints a
canonical observation that cites the candidate instead — the same shape `app/review/evidence_actions.py`
uses to record a correction. What the extractor said stays exactly as it said it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditCategory, emit
from app.evidence.sides import ReadingSides, SideRefusal, SideRefusalReason, reading_transform
from app.models.document import Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.runs import ExtractionRun
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.canonical import Authority, EvidenceStatus
from evidence.coordinates import ImagePoint
from evidence.normalize import NormalizationRefusal, normalize
from rules.semantic_types import SemanticType
from units.measurement import Measurement, Unit

__all__ = ["ConfirmationRefused", "RefusalReason", "confirm_candidate_type"]


class RefusalReason(StrEnum):
    """Why a confirmation could not become evidence.

    Every member is a fact about the stored reading rather than about the reviewer's answer. A
    refusal is returned rather than raised because each of these is something a person can be shown
    and act on — "this page was read before the transform was recorded" is actionable; a traceback is
    not.
    """

    NO_SUCH_CANDIDATE = "no_such_candidate"
    UNKNOWN_TYPE = "unknown_type"
    NO_TRANSFORM = "no_transform"
    NOT_NORMALISABLE = "not_normalisable"
    ALREADY_CONFIRMED = "already_confirmed"
    #: The reading's page holds drawings, and the one holding it has no confirmed role yet (#795).
    VIEW_ROLE_UNCONFIRMED = "view_role_unconfirmed"
    #: The reading's page holds drawings, and no single one holds it (#795).
    NOT_IN_ONE_VIEW = "not_in_one_view"


#: How a reading with no side is refused (`app/evidence/sides.py`, #795).
_SIDE_REFUSAL = {
    SideRefusalReason.NOT_COMPARED: RefusalReason.NOT_NORMALISABLE,
    SideRefusalReason.NO_TRANSFORM: RefusalReason.NO_TRANSFORM,
    SideRefusalReason.NOT_IN_ONE_VIEW: RefusalReason.NOT_IN_ONE_VIEW,
    SideRefusalReason.VIEW_ROLE_UNCONFIRMED: RefusalReason.VIEW_ROLE_UNCONFIRMED,
}


@dataclass(frozen=True, slots=True)
class ConfirmationRefused:
    """A confirmation that produced no evidence, and the reason in plain English."""

    reason: RefusalReason
    detail: str


def confirm_candidate_type(
    session: Session,
    *,
    candidate_id: UUID,
    semantic_type: str,
    confirmed_by: str,
) -> CanonicalObservation | ConfirmationRefused:
    """Record that a reviewer has identified what an extracted reading is.

    Runs the chain that was already built and never called: the candidate, now carrying the type a
    person gave it, goes through `normalize` to become a canonical observation. That observation is
    `HUMAN_CONFIRMED`, which is what makes it eligible to be sealed as a verdict operand — see
    `evidence/gate.py`, whose `QUALIFIED_STATUSES` this is one of exactly two members of.

    The value is never re-entered. It is the number the extractor read, carried through unchanged,
    which is the whole point: a reviewer confirming a reading should not have to retype it, and a
    system that made them would be collecting the reviewer's transcription rather than the drawing's
    dimension.
    """
    if not isinstance(semantic_type, str) or semantic_type not in {
        member.value for member in SemanticType
    }:
        return ConfirmationRefused(
            RefusalReason.UNKNOWN_TYPE,
            f"{semantic_type!r} is not a semantic type this rulebook knows",
        )

    row = session.get(ObservationCandidate, candidate_id)
    if row is None:
        return ConfirmationRefused(
            RefusalReason.NO_SUCH_CANDIDATE, f"no observation candidate {candidate_id}"
        )

    existing = session.execute(
        select(CanonicalObservation.id)
        .join(
            EvidenceSupportingCandidate,
            EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
        )
        .where(EvidenceSupportingCandidate.candidate_id == row.id)
    ).first()
    if existing is not None:
        # Confirming twice would mint a second observation of one reading, and a rule asking for that
        # quantity would then find two — which reads as a drawing stating it twice.
        return ConfirmationRefused(
            RefusalReason.ALREADY_CONFIRMED,
            "this reading has already been confirmed; correct the observation instead",
        )

    page = session.get(Page, row.page_id)
    run = session.get(ExtractionRun, row.extraction_run_id)
    if page is None or run is None:
        return ConfirmationRefused(
            RefusalReason.NO_SUCH_CANDIDATE, "the candidate's page or extraction run is missing"
        )

    transform = reading_transform(page, run)
    if transform is None:
        return ConfirmationRefused(
            RefusalReason.NO_TRANSFORM,
            "this page was read before its transform was recorded, so the reading cannot be placed "
            "on the drawing; re-run extraction for this document",
        )

    # **Which drawing the reading is on, from the confirmed panel holding it** (#795, ADR-0020 §2).
    # The upload's kind decides only for a page with no drawing views; on a combined sheet it would
    # put the architect's number on the vendor's side of every check.
    role = ReadingSides(session).of(row)
    if isinstance(role, SideRefusal):
        return ConfirmationRefused(_SIDE_REFUSAL[role.reason], role.detail)

    if row.value_numerator is None or row.value_denominator is None or row.unit is None:
        return ConfirmationRefused(
            RefusalReason.NOT_NORMALISABLE,
            "this reading has no value; there is nothing to confirm the type of",
        )

    measurement = Measurement(
        exact=Fraction(row.value_numerator, row.value_denominator),
        unit=Unit(row.unit),
        raw_text=row.raw_text,
    )
    normalised = normalize(
        DomainCandidate(
            candidate_id=str(row.id),
            extractor=run.extractor,
            extractor_version=run.extractor_version,
            raw_text=row.raw_text,
            parsed_value=measurement,
            unit_guess=Unit(row.unit),
            # **The reviewer's answer, and the only place it enters.** Nothing above computed this.
            semantic_guess=SemanticType(semantic_type),
            page=page.index,
            polygon=tuple(ImagePoint(x=int(x), y=int(y)) for x, y in row.polygon),
            confidence=row.confidence,
            ambiguity_flags=tuple(row.ambiguity_flags),
        ),
        transform=transform,
        document_version_id=row.document_version_id,
        document_role=role,
    )
    if isinstance(normalised, NormalizationRefusal):
        return ConfirmationRefused(RefusalReason.NOT_NORMALISABLE, normalised.detail)

    observation = CanonicalObservation(
        document_version_id=row.document_version_id,
        page_id=page.id,
        document_role=role.value,
        polygon=[[str(point.x), str(point.y)] for point in normalised.polygon.points],
        coordinate_space="stored",
        semantic_type=semantic_type,
        value_numerator=measurement.exact.numerator,
        value_denominator=measurement.exact.denominator,
        unit=measurement.unit.value,
        # **The status `normalize` could not give it.** Normalisation produces a `RAW_CANDIDATE`,
        # because normalising is not qualifying. What qualifies this is the person who looked at the
        # crop and the value and said what it is, and their name is on the review action beside it.
        status=EvidenceStatus.HUMAN_CONFIRMED.value,
        authority=Authority.AUTHORITATIVE.value,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()

    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=observation.id,
            candidate_id=row.id,
            role="primary",
        )
    )

    # **Who said so, in the same transaction.** A confirmation is a person taking responsibility for
    # a reading entering the verdict path, and `REVIEW_ACTION` is the category for exactly that.
    # `review_actions` is the wrong table for it: that one requires a finding, and this happens
    # before any finding exists — the confirmation is what makes a finding possible.
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=confirmed_by,
        target_id=observation.id,
        target_type="canonical_observation",
    )
    session.flush()
    return observation
