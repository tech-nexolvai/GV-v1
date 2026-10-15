"""Confirm or correct evidence without rewriting what extraction originally recorded.

A review action, the original canonical observation and the resulting HUMAN_CONFIRMED
observation are joined by foreign keys.  Corrections additionally write the append-only
correction ledger before the caller's transaction commits.  Nothing here commits: the
caller owns one transaction containing the entire decision.

A held AI reading (one the checks did not use because the two AI readers did not seal it) is
confirmed or fixed from the reading itself, for the finding's own countertop row or countertop only,
while its result is live (`confirm_candidate_reading`, `correct_candidate_reading`, #1339). A depth
or sink reading is confirmed once and never fixed there. A confirmation is recorded as a `confirm`
tagged `held-reading-answer`, which is never a decision on its finding (#1367).

Source: ``docs/DESIGN_PRODUCT.md`` section 4 and issues #230, #1339, #1367.
Verification: ``tests/review/test_evidence_actions.py``, ``tests/review/test_held_reading_actions.py``,
``tests/review/test_review_action_tags.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.auth import Action, Principal
from app.evidence.confirm import ConfirmationRefused, RefusalReason, observation_from_candidate
from app.evidence.sides import ReadingSides, SideRefusal
from app.lifecycle.closed import CLOSED_STATES, closed_reason
from app.models.evidence import (
    CanonicalObservation,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.package import PackageRevision
from app.models.review import ReviewAction, ReviewActionKind, ReviewActionTag, ReviewSession
from app.models.verdicts import CheckRun, Finding, FindingEvidence
from app.review.ledger import record_correction
from app.review.review_items import under_gv_markup, vendor_ink_only
from app.review.session import refuse_if_superseded
from evidence.canonical import CorroborationLane, EvidenceStatus
from units.imperial import format_inches
from units.measurement import Unit
from units.normalise import plain_marks
from vocabulary.drawn_length import DRAWN_LENGTH_DISAGREED
from vocabulary.semantic_types import DocumentRole
from workflow.depth_evidence import (
    DEPTH_FIELDS,
    DEPTH_READER_FLAG,
    FIELD_PREFIX,
    QUALIFIED,
    DepthReading,
    depth_runs,
    item_proposals,
    person_types,
    reading_linked,
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
#
# **An answer is a review action on the finding**, from "the AI's reading" (one canonical
# observation per label, minted by the first answer and reused by every later one) to the person's
# `HUMAN_CONFIRMED` value, so the correction rate counts confirmations as well as fixes and the
# answers about one label form one chain. Neither verb decides the finding; the next check run uses
# the value and code decides PASS or FAIL:
#
# - a fix writes a `correct`, which blocks its finding until the re-run
#   (`app/review/carry_over.decision_records`);
# - a confirmation writes a `confirm` with no note on any live result, waiting, PASS or FAIL (e.g.
#   after a fix the person now takes back), tagged `held-reading-answer` (#1367; plus
#   `drawn-length-disagreed` where the label's drawn length disagreed). The tag keeps every reader of
#   review actions as decisions from taking it as one (sign-off, carried decisions, Smriti's
#   confirmed FAILs: `ReviewAction.is_decision`), so it never stands behind a PASS or FAIL nor
#   settles a waiting result, while the correction rate counts it. The confirmation is a new input,
#   and sign-off waits for the re-run that uses it (`app/review/approval`: its original is not the
#   finding's evidence).


class HeldReadingRefusal(StrEnum):
    """Why an answer about a held reading was refused. Each one is something a person can act on."""

    #: The label is not on the finding's own countertop row, or not tied to its own countertop.
    NOT_THIS_ITEM = "not_this_item"
    #: The finding is not one countertop row's or one countertop's, or a newer check replaced it.
    NOT_A_HELD_ITEM = "not_a_held_item"
    #: Both AI readers sealed it (or code qualified it): it already counts, nothing is held.
    ALREADY_COUNTS = "already_counts"
    #: The AI read no number here, or the number is not positively the vendor's one label (GV's
    #: markup, a second label, ink never checked): check it by hand; nothing is typed.
    NO_AI_READING = "no_ai_reading"
    #: Not an inch reading, or a fix typed in millimetres: inches decide, and mm is never converted
    #: into a decision.
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
    #: The value the person was shown is not the AI's stored reading this would record.
    SHOWN_VALUE_DIFFERS = "shown_value_differs"
    #: A depth or sink reading: confirmed once here, never fixed here (newest-wins is #1360).
    DEPTH_CONFIRM_ONCE = "depth_confirm_once"
    #: The page was read before its transform was recorded: the reading cannot be placed.
    CANNOT_PLACE = "cannot_place"
    #: The reading could not be normalised into an observation.
    NOT_NORMALISABLE = "not_normalisable"


#: Why a depth or sink reading takes one confirmation here and no fix (#1339 review). The depth
#: check holds whenever two people's answers differ, or a person's differs from the AI's value on
#: the same reading, and answers are append-only: a fix here, or a second answer, would hold the
#: countertop's depth for good. Newest-wins for depth is issue #1360.
DEPTH_CONFIRM_ONCE_DETAIL = (
    "Depth and sink readings can only be confirmed once in this screen, and are not fixed here; "
    "to fix one or change an answer, use the advanced depth card"
)

#: A fix typed in millimetres (`520 mm`, `520mm`, `520 millimetres`): refused, never converted.
_MILLIMETRES = re.compile(r"mm|millim", re.IGNORECASE)

#: How a refusal to mint an observation is told (`app/evidence/confirm.RefusalReason`).
_MINT_REFUSALS: dict[RefusalReason, HeldReadingRefusal] = {
    RefusalReason.NO_SUCH_CANDIDATE: HeldReadingRefusal.NOT_THIS_ITEM,
    RefusalReason.UNKNOWN_TYPE: HeldReadingRefusal.NOT_NORMALISABLE,
    RefusalReason.NO_TRANSFORM: HeldReadingRefusal.CANNOT_PLACE,
    RefusalReason.NOT_NORMALISABLE: HeldReadingRefusal.NOT_NORMALISABLE,
    RefusalReason.ALREADY_CONFIRMED: HeldReadingRefusal.ALREADY_ANSWERED,
    RefusalReason.VIEW_ROLE_UNCONFIRMED: HeldReadingRefusal.NOT_THE_VENDORS_DRAWING,
    RefusalReason.NOT_IN_ONE_VIEW: HeldReadingRefusal.NOT_THE_VENDORS_DRAWING,
    RefusalReason.REVIEWER_MARKUP: HeldReadingRefusal.NO_AI_READING,
}


class HeldReadingRefused(EvidenceActionRefused):
    """A confirm or fix of a held reading that wrote nothing, with the reason in plain words."""

    def __init__(self, reason: HeldReadingRefusal, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReadingDecision:
    """What one person's answer about a held reading wrote.

    `original` is the AI's reading of the label as a canonical observation (`RAW_CANDIDATE` when
    the label's first answer was a confirmation, `REJECTED` when it was a fix; reused by every later
    answer about the label). `resulting` is the person's `HUMAN_CONFIRMED` observation citing the
    label. `action` is the `confirm` or `correct` review action on the finding (original →
    resulting); a fix also wrote its correction-ledger row. A confirmation's action is tagged
    `held-reading-answer` (and `drawn-length-disagreed` where the label's drawn length disagreed),
    so it is counted by the correction rate and never stands as a decision on the finding (#1367).
    """

    candidate: ObservationCandidate
    resulting: CanonicalObservation
    original: CanonicalObservation
    action: ReviewAction


@dataclass(frozen=True, slots=True)
class _HeldLabel:
    candidate: ObservationCandidate
    semantic_type: str
    value: Fraction
    #: A countertop's depth or sink reading (else a width label of a countertop row).
    depth: bool


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
    proposal_id, _view = item_proposals(db, finding.scope_item_id)  # type: ignore[arg-type]
    if (
        DEPTH_READER_FLAG not in flags
        or candidate.extraction_run_id not in runs
        or proposal_id is None
        or not reading_linked(db, DepthReading(candidate, flags), proposal_id)
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
    if person_types(db, candidate):
        # Any person's answer on this reading (or either reader's own row of it), from this screen
        # or the advanced depth card: a second one could only hold the depth for good.
        raise HeldReadingRefused(HeldReadingRefusal.DEPTH_CONFIRM_ONCE, DEPTH_CONFIRM_ONCE_DETAIL)
    assert field is not None
    return field


def _vendors_own_number(flags: frozenset[str], *, depth: bool) -> bool:
    """Whether the label's value is positively the vendor's own number, by W1's rule.

    A width label: exactly `app/review/review_items.vendor_ink_only` (checked as vendor ink and no
    other ink, no second label, no unchecked ink, no GV mark), the rule the review list uses to show
    a guess at all. A depth or sink reading carries no ink flag: the depth reader checks the ink
    itself and writes a value it saw GV's ink on with no number (`state:held`), so for it the rule
    is `under_gv_markup` (any GV mark or any ink that is not the vendor's), which every flag the
    reader may add later still meets.
    """
    return not under_gv_markup(flags) if depth else vendor_ink_only(flags)


def _held_label(db: Session, finding: Finding, candidate_id: UUID) -> _HeldLabel:
    """The finding's own held label, its type and the AI's value, or why it cannot be answered."""
    candidate = db.get(ObservationCandidate, candidate_id)
    if candidate is None:
        raise HeldReadingRefused(HeldReadingRefusal.NOT_THIS_ITEM, "no such reading")
    if finding.scope_row_candidate_id is not None:
        semantic_type, depth = _row_label(db, finding, candidate), False
    elif finding.scope_item_id is not None:
        semantic_type, depth = _countertop_label(db, finding, candidate), True
    else:
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_A_HELD_ITEM,
            "this result is not about one countertop row or one countertop",
        )
    flags = frozenset(candidate.ambiguity_flags or ())
    if not _vendors_own_number(flags, depth=depth):
        # GV's markup is the answer key, never a reading: the vendor's own number is checked by hand.
        raise HeldReadingRefused(
            HeldReadingRefusal.NO_AI_READING,
            "this number is not positively the vendor's own label (GV's markup, a second label, or "
            "ink that was never checked is on it), so there is no AI reading of it to confirm or "
            "fix; check it by hand",
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
    return _HeldLabel(candidate, semantic_type, value, depth)


def _answers_about(db: Session, candidate: ObservationCandidate) -> list[CanonicalObservation]:
    """Every observation an answer about this label wrote, newest first."""
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


def _ai_reading(
    db: Session,
    label: _HeldLabel,
    answers: list[CanonicalObservation],
    status: EvidenceStatus,
    actor: str,
) -> CanonicalObservation:
    """The AI's reading of the label as one canonical observation: the one an earlier answer about
    it minted, else a new one with `status` (`RAW_CANDIDATE` for a confirmation, `REJECTED` for a
    fix; the evidence gate seals neither)."""
    earlier = [
        answer
        for answer in answers
        if answer.semantic_type == label.semantic_type
        and answer.status in (EvidenceStatus.RAW_CANDIDATE.value, EvidenceStatus.REJECTED.value)
    ]
    if earlier:
        return earlier[-1]  # the first minted (answers are newest first)
    made = observation_from_candidate(
        db, label.candidate, semantic_type=label.semantic_type, status=status, actor=actor
    )
    if isinstance(made, ConfirmationRefused):
        raise HeldReadingRefused(
            _MINT_REFUSALS.get(made.reason, HeldReadingRefusal.NOT_NORMALISABLE), made.detail
        )
    return made


def _act(
    db: Session,
    sitting: ReviewSession,
    finding: Finding,
    principal: Principal,
    kind: ReviewActionKind,
    original: CanonicalObservation,
    resulting: CanonicalObservation,
    note: str | None = None,
    tags: tuple[ReviewActionTag, ...] = (),
) -> ReviewAction:
    action = ReviewAction(
        review_session_id=sitting.id,
        finding_id=finding.id,
        package_revision_id=finding.package_revision_id,
        action=kind.value,
        actor=principal.id,
        note=note,
        original_observation_id=original.id,
        resulting_observation_id=resulting.id,
        tags=[tag.value for tag in tags],
    )
    db.add(action)
    db.flush()
    return action


def answer_tags(candidate: ObservationCandidate) -> tuple[ReviewActionTag, ...]:
    """The tags a confirmation of this held label is recorded with (#1367).

    Always `held-reading-answer`: the action is an answer about one reading, never a decision on
    its finding. Plus `drawn-length-disagreed` where the reader held the label because its drawn
    length did not fit the number both AI readers agreed on (`DRAWN_LENGTH_DISAGREED`). That fact
    goes in a tag, never in the note: a note on a confirm is what sign-off reads as the person
    deciding the result (`app/review/requirements.needs_note`).
    """
    flags = frozenset(candidate.ambiguity_flags or ())
    return (
        ReviewActionTag.HELD_READING_ANSWER,
        *((ReviewActionTag.DRAWN_LENGTH_DISAGREED,) if DRAWN_LENGTH_DISAGREED in flags else ()),
    )


def confirm_candidate_reading(
    db: Session,
    *,
    principal: Principal,
    review_session_id: UUID,
    finding_id: UUID,
    candidate_id: UUID,
    expected_value: Fraction,
) -> ReadingDecision:
    """A person confirms the AI's own reading the checks held back (#1339).

    `expected_value` (required, keyword) is the exact value the screen showed the person, in
    inches. The confirmation records the label's own stored value, never one re-entered; when the
    two differ (the screen's guess came from the PDF text or the other reader), nothing is written
    and the person is told to use Fix.

    Writes, in the caller's transaction: the AI's reading as a canonical observation (once per
    label; `RAW_CANDIDATE`), the person's `HUMAN_CONFIRMED` copy of it citing the label (its side
    from `ReadingSides`), and a `confirm` review action on the finding with no note, tagged
    `held-reading-answer` (`answer_tags`), whatever the finding's result: the correction rate counts
    it, and no reader takes it as a decision on the finding, so it never stands behind a PASS or
    FAIL nor settles a result that waits (#1367). The next check run uses the value and code
    decides PASS or FAIL.

    Refused unless the label is on the finding's own countertop row
    (or tied to its own countertop), is held, has an AI reading in inches that is positively the
    vendor's own number (W1's rule), and lies on the vendor's drawing. A second confirmation of the
    same answer is refused; after a fix it records the AI's value as the newest answer. A depth or
    sink reading takes one answer only. Nothing is committed; on a refusal nothing is written.
    """
    if not isinstance(expected_value, Fraction):
        raise TypeError("expected_value must be the exact Fraction the screen showed")
    sitting, finding = _sitting(db, principal, review_session_id, finding_id)
    label = _held_label(db, finding, candidate_id)
    if expected_value != label.value:
        raise HeldReadingRefused(
            HeldReadingRefusal.SHOWN_VALUE_DIFFERS,
            f"you were shown {format_inches(expected_value)}\", but the AI's stored reading of "
            f'this label is {format_inches(label.value)}"; confirming would record '
            f'{format_inches(label.value)}", so nothing was saved. If the drawing says '
            f'{format_inches(expected_value)}", use Fix',
        )
    answers = _answers_about(db, label.candidate)
    if _newest_answer(answers, label.semantic_type) == label.value:
        raise HeldReadingRefused(
            HeldReadingRefusal.ALREADY_ANSWERED, "a person already confirmed this reading"
        )
    with db.begin_nested():
        original = _ai_reading(db, label, answers, EvidenceStatus.RAW_CANDIDATE, principal.id)
        resulting = _new_observation(db, original, value=None)
        action = _act(
            db,
            sitting,
            finding,
            principal,
            ReviewActionKind.CONFIRM,
            original,
            resulting,
            tags=answer_tags(label.candidate),
        )
    return ReadingDecision(label.candidate, resulting, original, action)


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
    """A person fixes the AI's own held reading of a countertop row's width (#1339): it says X, the
    drawing says Y.

    Writes, in the caller's transaction: the AI's reading as a canonical observation (once per
    label, reused by every later answer; `REJECTED` when a fix is its first answer), the person's
    value as a `HUMAN_CONFIRMED` one citing the same label, a `correct` review action on the finding
    (original → resulting, with `note`, e.g. "AI misread" or "the vendor drew it wrong"), and its
    correction-ledger row. `corrected_value` is typed with its unit, inches or feet and inches only
    (millimetres are refused, never converted), and read by the review screen's own parser
    (`app/api/review._corrected_inches`): exact, above zero. Only an AI reading can be fixed; with
    none, the label is checked by hand. A depth or sink reading is never fixed here (use the
    advanced depth card). The same refusals as `confirm_candidate_reading`, plus a fix to the AI's
    own value (that is a confirmation) or to the answer already standing. Nothing is committed; on
    a refusal nothing is written.

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
    if label.depth:
        raise HeldReadingRefused(HeldReadingRefusal.DEPTH_CONFIRM_ONCE, DEPTH_CONFIRM_ONCE_DETAIL)
    if _MILLIMETRES.search(plain_marks(corrected_value or "")):
        raise HeldReadingRefused(
            HeldReadingRefusal.NOT_INCHES,
            'the fix is in millimetres; give it in inches (25 1/2") or feet and inches '
            "(2'-1 1/2\"): inches decide, and a value converted from mm is never exact",
        )
    answers = _answers_about(db, label.candidate)
    # A savepoint: a refusal below leaves nothing behind, whatever the caller does next.
    with db.begin_nested():
        original = _ai_reading(db, label, answers, EvidenceStatus.REJECTED, principal.id)
        try:
            corrected = _corrected_inches(db, original.id, corrected_value)
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
        resulting = _new_observation(db, original, value=corrected)
        action = _act(
            db,
            sitting,
            finding,
            principal,
            ReviewActionKind.CORRECT,
            original,
            resulting,
            note=(note or "").strip()[:1000] or None,
        )
        record_correction(
            db,
            review_action_id=action.id,
            canonical_observation_id=original.id,
            original=_canonical_value(original),
            corrected=_canonical_value(resulting),
        )
    return ReadingDecision(label.candidate, resulting, original, action)
