"""Confirm or correct evidence without rewriting what extraction originally recorded.

A review action, the original canonical observation and the resulting HUMAN_CONFIRMED
observation are joined by foreign keys.  Corrections additionally write the append-only
correction ledger before the caller's transaction commits.  Nothing here commits: the
caller owns one transaction containing the entire decision.

A held AI reading (one the checks did not use because the two AI readers did not seal it) is
confirmed or fixed from the reading itself, for the finding's own countertop row or countertop only
(`confirm_candidate_reading`, `correct_candidate_reading`, #1339).

Source: ``docs/DESIGN_PRODUCT.md`` section 4 and issues #230, #1339.
Verification: ``tests/review/test_evidence_actions.py``, ``tests/review/test_held_reading_actions.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.auth import Action, Principal
from app.evidence.confirm import ConfirmationRefused, observation_from_candidate
from app.evidence.sides import ReadingSides, SideRefusal
from app.lifecycle.closed import CLOSED_STATES, closed_reason
from app.models.evidence import (
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.package import PackageRevision
from app.models.review import ReviewAction, ReviewActionKind, ReviewSession
from app.models.verdicts import CheckRun, Finding, FindingEvidence
from app.review.ledger import record_correction
from app.review.session import refuse_if_superseded
from evidence.canonical import CorroborationLane, EvidenceStatus
from units.measurement import Unit
from vocabulary.semantic_types import DocumentRole
from workflow.depth_evidence import (
    DEPTH_FIELDS,
    DEPTH_READER_FLAG,
    FIELD_PREFIX,
    QUALIFIED,
    _item_proposals,
    _linked,
    _Reading,
    depth_runs,
)
from workflow.row_answers import own_slot_labels, width_semantic
from workflow.slot_row_scope import candidate_is_sealed, slot_rows


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
    # A revision a newer one replaced is read-only (#1180), whatever the request names.
    sitting = db.get(ReviewSession, review_session_id)
    if sitting is not None:
        refuse_if_superseded(db, sitting.package_revision_id)
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


# --- A held AI reading: confirm it or fix it (#1339, simple review W2) ----------------------------
#
# The checks hold back a reading the two AIs did not seal (they differed, one could not read it, ...),
# and a held result has no evidence linked to its finding, so `confirm_evidence` and
# `correct_evidence` above cannot reach it. These two answer it from the reading itself, the AI's own
# label (an `observation_candidates` row), and only for the finding's own countertop row or
# countertop. A label with no AI reading is checked by hand, never typed (confirm-only, 2026-10-15).


class HeldReadingRefusal(StrEnum):
    """Why an answer about a held reading was refused. Each one is something a person can act on."""

    #: The label is not on the finding's own countertop row, or not tied to its own countertop.
    NOT_THIS_ITEM = "not_this_item"
    #: The finding is not one countertop row's or one countertop's, or a newer check replaced it.
    NOT_A_HELD_ITEM = "not_a_held_item"
    #: Both AI readers sealed it (or code qualified it): it already counts, nothing is held.
    ALREADY_COUNTS = "already_counts"
    #: The AI read no number here, or GV's markup covers it: check it by hand; nothing is typed.
    NO_AI_READING = "no_ai_reading"
    #: Not an inch reading: inches decide, and mm is never converted into a decision.
    NOT_INCHES = "not_inches"
    #: The reading is not on the vendor's drawing (or which drawing it is on is not confirmed).
    NOT_THE_VENDORS_DRAWING = "not_the_vendors_drawing"
    #: A person already gave this same answer about this label.
    ALREADY_ANSWERED = "already_answered"
    #: The fix is the AI's own reading: that is a confirmation.
    SAME_AS_READ = "same_as_read"
    #: The fixed value could not be read as an exact width in inches.
    BAD_VALUE = "bad_value"
    #: The revision is signed off, sent back, replaced or cancelled: nothing more is recorded on it.
    REVISION_CLOSED = "revision_closed"


class HeldReadingRefused(EvidenceActionRefused):
    """A confirm or fix of a held reading that wrote nothing, with the reason in plain words."""

    def __init__(self, reason: HeldReadingRefusal, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReadingDecision:
    """What one person's answer about a held reading wrote.

    `resulting` is the person's `HUMAN_CONFIRMED` observation citing the label. A fix also wrote
    `original` (the AI's reading, `REJECTED`) and `action` (a `correct` review action on the
    finding, with its correction-ledger row); a confirmation writes neither, so it decides nothing
    on the finding: the next check run uses it.
    """

    candidate: ObservationCandidate
    resulting: CanonicalObservation
    original: CanonicalObservation | None = None
    action: ReviewAction | None = None


@dataclass(frozen=True, slots=True)
class _HeldLabel:
    candidate: ObservationCandidate
    semantic_type: str
    value: Fraction


def _sitting(
    db: Session, principal: Principal, review_session_id: UUID, finding_id: UUID
) -> tuple[ReviewSession, Finding]:
    """The open review session and the live finding it is about, or a refusal."""
    _authorise(principal)
    sitting = db.get(ReviewSession, review_session_id)
    if sitting is not None:
        refuse_if_superseded(db, sitting.package_revision_id)
    finding = db.get(Finding, finding_id)
    if sitting is None or finding is None:
        raise EvidenceActionRefused("the review session or finding does not exist")
    if sitting.completed_at is not None:
        raise EvidenceActionRefused("the review session has already ended")
    if finding.package_revision_id != sitting.package_revision_id:
        raise EvidenceActionRefused("the finding is outside this review session")
    revision = db.get(PackageRevision, finding.package_revision_id)
    if revision is None or revision.state in CLOSED_STATES:
        raise HeldReadingRefused(
            HeldReadingRefusal.REVISION_CLOSED,
            (None if revision is None else closed_reason(revision.state))
            or "this revision is closed; nothing more is recorded on it",
        )
    replaced = db.scalar(select(CheckRun.superseded_at).where(CheckRun.id == finding.check_run_id))
    if replaced is not None:
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_A_HELD_ITEM,
            "a newer check run replaced this result; reload and answer the current one",
        )
    return sitting, finding


def _row_label(db: Session, finding: Finding, candidate: ObservationCandidate) -> str:
    """The width type of `candidate` when it is a held label of the finding's own row."""
    rows = {row.anchor.id: row for row in slot_rows(db, finding.package_revision_id)}
    row = rows.get(finding.scope_row_candidate_id)  # type: ignore[arg-type]
    if row is None:
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_A_HELD_ITEM,
            "this result's countertop row is not in the newest reading; reload and answer again",
        )
    if row.held_reason == "This is not the vendor drawing.":
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_THE_VENDORS_DRAWING, "this row is not on the vendor's drawing"
        )
    labels = own_slot_labels(row)
    position = next(
        (position for position, label in labels.items() if label.id == candidate.id), False
    )
    if position is False:
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_THIS_ITEM,
            "this reading is not one of the labels on this result's own countertop row",
        )
    assert position is None or isinstance(position, int)
    if candidate_is_sealed(candidate):
        raise HeldReadingRefused(
            HeldReadingRefusal.ALREADY_COUNTS,
            "both AI readers sealed this reading, so it already counts; nothing is held",
        )
    return width_semantic(position).value


def _countertop_label(db: Session, finding: Finding, candidate: ObservationCandidate) -> str:
    """The value type of `candidate` when it is a depth or sink reading of the finding's own
    countertop: written by a depth reading of this revision, and tied to this countertop by its
    view's link as it stands now (the depth checks' own rule, `workflow/depth_evidence.py`)."""
    flags = frozenset(candidate.ambiguity_flags or ())
    runs = {run.id for run in depth_runs(db, finding.package_revision_id)}
    proposal_id, _view = _item_proposals(db, finding.scope_item_id)  # type: ignore[arg-type]
    if (
        DEPTH_READER_FLAG not in flags
        or candidate.extraction_run_id not in runs
        or proposal_id is None
        or not _linked(db, _Reading(candidate, flags), proposal_id)
    ):
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_THIS_ITEM,
            "this reading is not tied to this result's own countertop",
        )
    field = next(
        (flag.removeprefix(FIELD_PREFIX) for flag in flags if flag.startswith(FIELD_PREFIX)), None
    )
    if field not in DEPTH_FIELDS:
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_THIS_ITEM, "this reading is not a depth or sink value"
        )
    if QUALIFIED in flags:
        raise HeldReadingRefused(
            HeldReadingRefusal.ALREADY_COUNTS,
            "both AI readers and code already qualified this reading, so it already counts",
        )
    assert field is not None
    return field


def _held_label(db: Session, finding: Finding, candidate_id: UUID) -> _HeldLabel:
    """The finding's own held label, its type and the AI's value, or why it cannot be answered."""
    candidate = db.get(ObservationCandidate, candidate_id)
    if candidate is None:
        raise HeldReadingRefused(HeldReadingRefusal.NOT_THIS_ITEM, "no such reading")
    if finding.scope_row_candidate_id is not None:
        semantic_type = _row_label(db, finding, candidate)
    elif finding.scope_item_id is not None:
        semantic_type = _countertop_label(db, finding, candidate)
    else:
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_A_HELD_ITEM,
            "this result is not about one countertop row or one countertop",
        )
    flags = set(candidate.ambiguity_flags or ())
    if "reviewer-markup" in flags or "ink:gv" in flags:
        # GV's markup is the answer key, never a reading: the vendor's own number is checked by hand.
        raise HeldReadingRefused(
            HeldReadingRefusal.NO_AI_READING,
            "GV's markup covers this number, so there is no AI reading of the vendor's own number "
            "to confirm; check it by hand",
        )
    if candidate.value_numerator is None or candidate.value_denominator is None:
        raise HeldReadingRefused(
            HeldReadingRefusal.NO_AI_READING,
            "the AI read no number here; check it by hand (numbers are not typed)",
        )
    if candidate.unit != Unit.INCH.value:
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_INCHES,
            f"this reading is in {candidate.unit}; inches decide, and a conversion would round",
        )
    value = Fraction(candidate.value_numerator, candidate.value_denominator)
    if value <= 0:
        raise HeldReadingRefused(
            HeldReadingRefusal.NO_AI_READING,
            "the AI's reading is zero or less, which is no width; check it by hand",
        )
    side = ReadingSides(db).of(candidate)
    if side is not DocumentRole.SHOP:
        detail = (
            side.detail if isinstance(side, SideRefusal) else "it is on the architect's drawing"
        )
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_THE_VENDORS_DRAWING,
            f"this reading is not on the vendor's drawing: {detail}",
        )
    return _HeldLabel(candidate, semantic_type, value)


def _answers_about(db: Session, candidate: ObservationCandidate) -> list[CanonicalObservation]:
    """Every observation a person's answer about this label wrote, newest first."""
    return list(
        db.scalars(
            select(CanonicalObservation)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
            )
            .where(EvidenceSupportingCandidate.candidate_id == candidate.id)
            .order_by(CanonicalObservation.created_at.desc(), CanonicalObservation.id.desc())
        )
    )


def _newest_answer(answers: list[CanonicalObservation], semantic_type: str) -> Fraction | None:
    return next(
        (
            Fraction(answer.value_numerator, answer.value_denominator)
            for answer in answers
            if answer.status == EvidenceStatus.HUMAN_CONFIRMED.value
            and answer.semantic_type == semantic_type
        ),
        None,
    )


def _observe(
    db: Session, label: _HeldLabel, status: EvidenceStatus, actor: str
) -> CanonicalObservation:
    made = observation_from_candidate(
        db, label.candidate, semantic_type=label.semantic_type, status=status, actor=actor
    )
    if isinstance(made, ConfirmationRefused):
        raise HeldReadingRefused(HeldReadingRefusal.NOT_THE_VENDORS_DRAWING, made.detail)
    return made


def confirm_candidate_reading(
    db: Session,
    *,
    principal: Principal,
    review_session_id: UUID,
    finding_id: UUID,
    candidate_id: UUID,
) -> ReadingDecision:
    """A person confirms the AI's own reading the checks held back (#1339).

    Writes one `HUMAN_CONFIRMED` canonical observation citing the held label, the shape
    `app/evidence/confirm.py` mints (the reading's own value, never re-entered; its side from
    `ReadingSides`), with the person on its audit event. No review action: the finding is not
    decided here; the next check run uses the confirmed value and code decides PASS or FAIL.
    Refused unless the label is on the finding's own countertop row (or tied to its own
    countertop), is held, has an AI reading in inches, and lies on the vendor's drawing. A second
    confirmation of the same answer is refused; after a fix it records the AI's value as the newest
    answer. Nothing is committed; on a refusal nothing is written.
    """
    _sitting(db, principal, review_session_id, finding_id)
    finding = db.get_one(Finding, finding_id)
    label = _held_label(db, finding, candidate_id)
    if _newest_answer(_answers_about(db, label.candidate), label.semantic_type) == label.value:
        raise HeldReadingRefused(
            HeldReadingRefusal.ALREADY_ANSWERED, "a person already confirmed this reading"
        )
    with db.begin_nested():
        resulting = _observe(db, label, EvidenceStatus.HUMAN_CONFIRMED, principal.id)
    return ReadingDecision(label.candidate, resulting)


def correct_candidate_reading(
    db: Session,
    *,
    principal: Principal,
    review_session_id: UUID,
    finding_id: UUID,
    candidate_id: UUID,
    corrected_value: str,
    note: str | None = None,
) -> ReadingDecision:
    """A person fixes the AI's own held reading (#1339): it says X, the drawing says Y.

    Writes four records in the caller's transaction: the AI's reading as a `REJECTED` canonical
    observation (one per label, reused by a later fix), the person's value as a `HUMAN_CONFIRMED`
    one citing the same label, a `correct` review action on the finding (original → resulting,
    with `note`, e.g. "AI misread" or "the vendor drew it wrong"), and its correction-ledger row.
    `corrected_value` is typed with its unit and read by the review screen's own parser
    (`app/api/review._corrected_inches`): inches only, exact, above zero. Only an AI reading can be
    fixed; with none, the label is checked by hand. The same refusals as
    `confirm_candidate_reading`, plus a fix to the AI's own value (that is a confirmation) or to the
    answer already standing. Nothing is committed; on a refusal nothing is written.

    **`REJECTED`, not `CONFLICTING`, for the AI's reading.** The database's provenance trigger
    (`alembic/versions/0006_evidence_plane.py`) accepts `CONFLICTING` only with a supporting *and*
    a conflicting extraction candidate, and what contradicts the AI here is a person, not another
    reading; inventing a candidate for the person would fake extraction provenance. `REJECTED` ("the
    reading was found invalid") is what a fix says, it needs no second candidate, and the evidence
    gate never seals it.
    """
    from fastapi import HTTPException

    from app.api.review import _corrected_inches

    sitting, finding = _sitting(db, principal, review_session_id, finding_id)
    label = _held_label(db, finding, candidate_id)
    answers = _answers_about(db, label.candidate)
    # A savepoint: a refusal below leaves nothing behind, whatever the caller does next.
    with db.begin_nested():
        rejected_reading = next(
            (
                answer
                for answer in answers
                if answer.status == EvidenceStatus.REJECTED.value
                and answer.semantic_type == label.semantic_type
            ),
            None,
        ) or _observe(db, label, EvidenceStatus.REJECTED, principal.id)
        try:
            corrected = _corrected_inches(db, rejected_reading.id, corrected_value)
        except HTTPException as refused:
            raise HeldReadingRefused(HeldReadingRefusal.BAD_VALUE, str(refused.detail)) from None
        if corrected == label.value:
            raise HeldReadingRefused(
                HeldReadingRefusal.SAME_AS_READ,
                "that is what the AI read; confirm the reading instead of fixing it",
            )
        if _newest_answer(answers, label.semantic_type) == corrected:
            raise HeldReadingRefused(
                HeldReadingRefusal.ALREADY_ANSWERED, "a person already gave this value"
            )
        resulting = _new_observation(db, rejected_reading, value=corrected)
        action = ReviewAction(
            review_session_id=sitting.id,
            finding_id=finding.id,
            package_revision_id=finding.package_revision_id,
            action=ReviewActionKind.CORRECT.value,
            actor=principal.id,
            note=(note or "").strip()[:1000] or None,
            original_observation_id=rejected_reading.id,
            resulting_observation_id=resulting.id,
        )
        db.add(action)
        db.flush()
        record_correction(
            db,
            review_action_id=action.id,
            canonical_observation_id=rejected_reading.id,
            original=_canonical_value(rejected_reading),
            corrected=_canonical_value(resulting),
        )
    return ReadingDecision(label.candidate, resulting, rejected_reading, action)
