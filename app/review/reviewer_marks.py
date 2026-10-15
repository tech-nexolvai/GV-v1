"""What a person confirmed, fixed or answered on a revision, as marks for the cloud PDF (#1363).

The cloud PDF used to show only the checks' results. A person's work did not appear on it at all: a
reading they confirmed, a reading they fixed, the walls they chose, a page they answered, a result
they checked by hand. This module reads those stored records and writes each one as a plain,
frozen mark (`FrozenReviewerMark`): its kind, the person, the words with the person's initials, what
it is about, and the place on the drawing code recorded for it, or why there is none.

**Frozen at sign-off.** `app/review/signed_exports.py` calls `reviewer_marks_for` in the approval's
own transaction and stores the result in the sign-off snapshot (format 2), so the signed cloud PDF
draws the marks as they were when it was signed, never from the live database. The draft cloud PDF
(`workflow/draft_cloud_render.py`) calls it on the live records, so both show the same marks.

**Where each record comes from** (all append-only; nothing here writes):

* *Fixed*: a `correct` review action naming the AI's observation and the person's (`original` →
  `resulting`), as W2 (#1339) and the older evidence correction both record it: "✎ Fixed by AB: AI
  read X → Y", at the reading's stored polygon.
* *Confirmed*: a `HUMAN_CONFIRMED` canonical observation that is not a fix, with the person from
  its `confirm` review action or its audit event: "✓ Confirmed by AB", at its stored polygon. Only
  the newest answer about one reading is shown.
* *Walls*: the wall choice in force on each countertop row of the newest reading, at the row's
  first number (its candidate polygon, through the transform recorded when it was read).
* *Pages*: the newest answer on each page listed with no countertop, at the page's reading record.
* *Checked by hand*: the standing decision on each result shown (the snapshot's own review record),
  at the result's countertop row when it has one; else listed.

**Never a PASS mark for a confirmation.** A confirmation is a person agreeing with a reading; the
check's own result is separate and is never changed or restated as PASS here.

**NOT CHECKED** (decision 1, 2026-10-15): the results whose check could not run are named here too
(`not_checked_finding_ids`), so the signed PDF prints them NOT CHECKED from the snapshot.

Source: issue #1363. Verification: `tests/review/test_reviewer_marks.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.events import AuditEvent
from app.evidence.sides import reading_transform
from app.models.document import PackageRevisionDocument, Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceSupportingCandidate,
    NoCountertopAnswer,
    ObservationCandidate,
)
from app.models.review import ReviewAction, ReviewActionKind
from app.models.runs import ExtractionRun
from app.models.verdicts import Finding
from app.review.signed_record import ReviewDisposition, SignedReviewFinding
from evidence.coordinates import ImagePoint
from units.imperial import format_inches
from units.measurement import Unit
from verdict.operands import EvidenceStatus
from verdict.outcomes import Outcome, is_abstention
from vocabulary.wall_layouts import wall_layout_words

__all__ = [
    "FrozenReviewerMark",
    "FrozenReviewerMarks",
    "initials",
    "is_checked_wrong",
    "is_not_checked",
    "live_decisions",
    "reviewer_marks_for",
]

MarkKind = Literal["confirmed", "fixed", "answered", "checked_wrong"]

#: The reason every check waiting for unread values starts with (`several_countertops.WAITING`).
WAITING: Final = "Waiting for "

#: Why a result about a whole countertop (or the package) is listed rather than drawn.
NO_ONE_PLACE: Final = (
    "this result is about a whole countertop or the whole package, so there is no one place on "
    "the drawing for it"
)


class FrozenReviewerMark(BaseModel):
    """One person's confirm, fix or answer, exactly as the cloud PDF prints it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MarkKind
    reviewer: str
    at: AwareDatetime
    words: str
    """With the glyph added when drawn: "Confirmed by AB", "Fixed by AB: AI read X → Y", ..."""
    subject: str
    details: tuple[str, ...] = ()
    place: str | None = None
    """A code-recorded page and polygon, in the JSON shape a finding's evidence carries."""
    unplaced_reason: str | None = None

    @model_validator(mode="after")
    def placed_or_explained(self) -> FrozenReviewerMark:
        if (self.place is None) == (self.unplaced_reason is None):
            raise ValueError("a mark has either a recorded place or the reason it has none")
        if not self.words.strip() or not self.subject.strip() or not self.reviewer.strip():
            raise ValueError("a mark names the person, what they did and what it is about")
        return self


class FrozenReviewerMarks(BaseModel):
    """The sign-off snapshot's marks section (format 2, #1363)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    marks: tuple[FrozenReviewerMark, ...]
    not_checked_finding_ids: tuple[UUID, ...]


def initials(name: str) -> str:
    """ "AB" for "ana.bell@example.com" or "Ana Bell"; the full name is in the mark's details."""
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", name.split("@", 1)[0]) if part]
    if not parts:
        return name.strip()[:2].upper() or "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return "".join(part[0] for part in parts[:3]).upper()


def is_not_checked(outcome: str, reason: str | None) -> bool:
    """A check that could not run (decision 1): no rule covers it, or it waits for unread values."""
    if outcome == Outcome.NO_APPLICABLE_RULE.value:
        return True
    return is_abstention(Outcome(outcome)) and (reason or "").startswith(WAITING)


def finding_reason(finding: Finding) -> str | None:
    trace_reason = finding.trace.get("reason") if isinstance(finding.trace, Mapping) else None
    return finding.reason or (trace_reason if isinstance(trace_reason, str) else None)


def is_checked_wrong(disposition: ReviewDisposition) -> bool:
    """ "Checked by reviewer: WRONG" (#1358): a person checked a result by hand and found it wrong.

    Read from `signed_record`'s own mark (the shared `CHECKED_WRONG_NOTE`), never from the words.
    """
    return disposition.checked_wrong_by_hand


# ---------------------------------------------------------------------------
# Places: only what code recorded
# ---------------------------------------------------------------------------


def _reference(
    document_version_id: UUID, page_index: int, polygon: Iterable[Sequence[object]]
) -> str:
    """The same JSON `workflow/redline_outputs._typed_references` writes for a finding."""
    return json.dumps(
        {
            "document_version_id": str(document_version_id),
            "page": page_index,
            "polygon": [[str(x), str(y)] for x, y in polygon],
            "space": "stored",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class _Places:
    """Pages and transforms read once, for every mark."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._pages: dict[UUID, Page | None] = {}

    def page(self, page_id: UUID) -> Page | None:
        if page_id not in self._pages:
            self._pages[page_id] = self.db.get(Page, page_id)
        return self._pages[page_id]

    def of_observation(self, observation: CanonicalObservation) -> tuple[str | None, str | None]:
        page = self.page(observation.page_id)
        if page is None or observation.coordinate_space != "stored":
            return None, "its reading has no recorded place in the stored space"
        return _reference(observation.document_version_id, page.index, observation.polygon), None

    def of_candidate(self, candidate: ObservationCandidate) -> tuple[str | None, str | None]:
        page = self.page(candidate.page_id)
        run = self.db.get(ExtractionRun, candidate.extraction_run_id)
        if page is None or run is None:
            return None, "its reading's page or run is missing"
        if candidate.coordinate_space == "stored":
            return _reference(candidate.document_version_id, page.index, candidate.polygon), None
        transform = reading_transform(page, run)
        if transform is None or candidate.coordinate_space != "image":
            return None, "its page was read before the page's transform was recorded"
        try:
            points = [
                transform.to_stored(ImagePoint(x=int(x), y=int(y))) for x, y in candidate.polygon
            ]
        except (TypeError, ValueError) as error:
            return None, f"its recorded box is not a usable page region ({error})"
        return (
            _reference(candidate.document_version_id, page.index, ((p.x, p.y) for p in points)),
            None,
        )

    def page_number(self, page_id: UUID) -> int | None:
        page = self.page(page_id)
        return None if page is None else page.index + 1


def _until(column: Any, until: datetime | None) -> tuple[Any, ...]:
    return () if until is None else (column <= until,)


def _value(numerator: int, denominator: int, unit: str) -> str:
    value = Fraction(numerator, denominator)
    if unit == Unit.INCH.value:
        return f'{format_inches(value)}"'
    return f"{value} {unit}"


def _what(semantic_type: str) -> str:
    return semantic_type.rsplit(":", 1)[-1].replace("_", " ")


def _by(reviewer: str, at: datetime) -> str:
    return f"By {reviewer} on {at.astimezone(UTC):%Y-%m-%d %H:%M} UTC."


def _mark(
    kind: MarkKind,
    reviewer: str,
    at: datetime,
    words: str,
    subject: str,
    place: tuple[str | None, str | None],
    details: Sequence[str] = (),
) -> FrozenReviewerMark:
    return FrozenReviewerMark(
        kind=kind,
        reviewer=reviewer,
        at=at,
        words=words,
        subject=subject,
        details=(*details, _by(reviewer, at)),
        place=place[0],
        unplaced_reason=place[1],
    )


# ---------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------


def _readings(
    db: Session, revision_id: UUID, until: datetime | None, places: _Places
) -> tuple[list[FrozenReviewerMark], frozenset[UUID]]:
    """Fixed and confirmed readings: the newest answer about each reading only.

    Also returns the review actions these marks drew, so the same action is not drawn twice as a
    result checked by hand."""
    versions = select(PackageRevisionDocument.document_version_id).where(
        PackageRevisionDocument.package_revision_id == revision_id
    )
    actions = {
        action.resulting_observation_id: action
        for action in db.scalars(
            select(ReviewAction)
            .where(
                ReviewAction.package_revision_id == revision_id,
                ReviewAction.resulting_observation_id.is_not(None),
                ReviewAction.action.in_(
                    (ReviewActionKind.CORRECT.value, ReviewActionKind.CONFIRM.value)
                ),
                *_until(ReviewAction.created_at, until),
            )
            .order_by(ReviewAction.created_at, ReviewAction.id)
        )
    }
    answers = list(
        db.scalars(
            select(CanonicalObservation)
            .where(
                CanonicalObservation.document_version_id.in_(versions),
                CanonicalObservation.status == EvidenceStatus.HUMAN_CONFIRMED.value,
                *_until(CanonicalObservation.created_at, until),
            )
            .order_by(CanonicalObservation.created_at, CanonicalObservation.id)
        )
    )
    used = frozenset(action.id for action in actions.values())
    if not answers:
        return [], used
    ids = [answer.id for answer in answers]
    cited: dict[UUID, frozenset[UUID]] = {}
    for observation_id, candidate_id in db.execute(
        select(
            EvidenceSupportingCandidate.canonical_observation_id,
            EvidenceSupportingCandidate.candidate_id,
        ).where(EvidenceSupportingCandidate.canonical_observation_id.in_(ids))
    ):
        cited[observation_id] = cited.get(observation_id, frozenset()) | {candidate_id}
    audited = {
        event.target_id: event.actor
        for event in db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.target_type == "canonical_observation",
                AuditEvent.target_id.in_(ids),
            )
            .order_by(AuditEvent.created_at.desc())
        )
    }
    # Newest answer about one reading wins: a fix then a confirmation shows the confirmation.
    newest: dict[object, CanonicalObservation] = {}
    for answer in answers:
        key = cited.get(answer.id) or (answer.page_id, json.dumps(answer.polygon))
        newest[(key, answer.semantic_type)] = answer
    marks: list[FrozenReviewerMark] = []
    for answer in sorted(newest.values(), key=lambda row: (row.created_at, row.id)):
        page = places.page_number(answer.page_id)
        value = _value(answer.value_numerator, answer.value_denominator, answer.unit)
        subject = f"Page {page}: {_what(answer.semantic_type)}"
        action = actions.get(answer.id)
        place = places.of_observation(answer)
        if action is not None and action.action == ReviewActionKind.CORRECT.value:
            original = (
                None
                if action.original_observation_id is None
                else db.get(CanonicalObservation, action.original_observation_id)
            )
            read = (
                "?"
                if original is None
                else _value(original.value_numerator, original.value_denominator, original.unit)
            )
            who = initials(action.actor)
            marks.append(
                _mark(
                    "fixed",
                    action.actor,
                    action.created_at,
                    f"Fixed by {who}: AI read {read} → {value}",
                    subject,
                    place,
                    () if action.note is None else (f"Note: {action.note}",),
                )
            )
            continue
        reviewer = action.actor if action is not None else audited.get(answer.id)
        at = action.created_at if action is not None else answer.created_at
        if reviewer is None:
            marks.append(
                _mark(
                    "answered",
                    "a person (name not recorded)",
                    at,
                    f"Value recorded by a person: {value}",
                    subject,
                    place,
                )
            )
            continue
        marks.append(
            _mark(
                "confirmed",
                reviewer,
                at,
                f"Confirmed by {initials(reviewer)}",
                f"{subject} {value}",
                place,
                ("A person agreed with the AI's reading. The check's own result is separate.",),
            )
        )
    return marks, used


def _walls(
    db: Session, revision_id: UUID, until: datetime | None, places: _Places
) -> list[FrozenReviewerMark]:
    from workflow.slot_row_scope import slot_rows

    marks: list[FrozenReviewerMark] = []
    for row in slot_rows(db, revision_id):
        decision = row.decision
        if (
            decision is None
            or decision.wall_config is None
            or (until is not None and decision.created_at > until)
        ):
            continue
        details = []
        if decision.carried_from_id is not None:
            details.append("Carried from the same answer on the earlier revision.")
        if decision.measurements:
            details.append(
                "This answer also typed widths (an older screen): "
                + ", ".join(
                    f"{key} {value}" for key, value in sorted(decision.measurements.items())
                )
            )
        marks.append(
            _mark(
                "answered",
                decision.confirmed_by,
                decision.created_at,
                f"Walls chosen by {initials(decision.confirmed_by)}: "
                f"{wall_layout_words(decision.wall_config)}",
                row.label,
                places.of_candidate(row.anchor),
                details,
            )
        )
    return marks


def _pages(
    db: Session, revision_id: UUID, until: datetime | None, places: _Places
) -> list[FrozenReviewerMark]:
    newest: dict[int, NoCountertopAnswer] = {}
    for answer in db.scalars(
        select(NoCountertopAnswer)
        .where(
            NoCountertopAnswer.package_revision_id == revision_id,
            *_until(NoCountertopAnswer.created_at, until),
        )
        .order_by(NoCountertopAnswer.created_at, NoCountertopAnswer.id)
    ):
        newest[answer.page_number] = answer
    marks: list[FrozenReviewerMark] = []
    for page_number in sorted(newest):
        answer = newest[page_number]
        who = initials(answer.answered_by)
        words = (
            f"No countertop on this page: confirmed by {who}"
            if answer.answer == "no_countertop"
            else f"There is a countertop on this page: said by {who}"
        )
        candidate = db.get(ObservationCandidate, answer.row_choice_candidate_id)
        place: tuple[str | None, str | None] = (
            (None, "its page's reading record is missing")
            if candidate is None
            else places.of_candidate(candidate)
        )
        marks.append(
            _mark(
                "answered",
                answer.answered_by,
                answer.created_at,
                words,
                f"Page {page_number}",
                place,
                () if answer.note is None else (f"Note: {answer.note}",),
            )
        )
    return marks


def _by_hand(
    db: Session,
    findings: Sequence[tuple[Finding, str]],
    decisions: Sequence[SignedReviewFinding],
    places: _Places,
    drawn: frozenset[UUID],
) -> list[FrozenReviewerMark]:
    """The standing decision on each result shown, as one mark: a result checked by hand."""
    rows = {finding.id: finding for finding, _ in findings}
    marks: list[FrozenReviewerMark] = []
    for decided in decisions:
        if not decided.actions:
            continue
        last = decided.actions[-1]
        if last.action == ReviewActionKind.CORRECT.value or last.action_id in drawn:
            continue  # a fix or a confirmed reading: drawn from its observations above
        finding = rows.get(decided.finding_id)
        who = initials(last.reviewer)
        outcome = decided.outcome
        kind: MarkKind = "answered"
        if is_checked_wrong(last):
            kind, words = "checked_wrong", f"Checked by reviewer {who}: WRONG"
        elif last.action == ReviewActionKind.CONFIRM.value and outcome == Outcome.FAIL.value:
            kind, words = "confirmed", f"FAIL agreed by {who}: wrong on the drawing"
        elif last.action == ReviewActionKind.CONFIRM.value:
            kind, words = "confirmed", f"Checked by reviewer {who}: OK"
        elif last.action == ReviewActionKind.DISMISS.value and (
            finding is not None and is_not_checked(outcome, finding_reason(finding))
        ):
            words = f"Settled by {who} as not checked"
        elif last.action == ReviewActionKind.DISMISS.value:
            words = f"Dismissed by {who}: not checkable"
        else:
            words = f"Exception recorded by {who}"
        anchor = (
            None
            if finding is None or finding.scope_row_candidate_id is None
            else db.get(ObservationCandidate, finding.scope_row_candidate_id)
        )
        place: tuple[str | None, str | None] = (
            (None, NO_ONE_PLACE) if anchor is None else places.of_candidate(anchor)
        )
        subject = (
            f"{decided.scope_label} — {decided.rule_id}" if decided.scope_label else decided.rule_id
        )
        marks.append(
            _mark(
                kind,
                last.reviewer,
                last.at,
                words,
                subject,
                place,
                (last.wording(outcome), f"The check's own result stays {outcome}."),
            )
        )
    return marks


def live_decisions(
    db: Session, findings: Sequence[tuple[Finding, str]]
) -> tuple[SignedReviewFinding, ...]:
    """The standing decision on each live result, in the snapshot's own shape (for the draft)."""
    from app.review.carry_over import decision_records

    records = decision_records(db, [finding.id for finding, _ in findings])
    result: list[SignedReviewFinding] = []
    for finding, rule_id in findings:
        decision = records.decisions.get(finding.id)
        actions: tuple[ReviewDisposition, ...] = ()
        if decision is not None:
            action = decision.action
            actions = (
                ReviewDisposition(
                    action_id=action.id,
                    action=action.action,  # type: ignore[arg-type]  # Pydantic validates stored verbs.
                    reviewer=action.actor,
                    at=action.created_at,
                    note=action.note,
                    carried_from_finding_id=decision.carried_from_finding_id,
                ),
            )
        result.append(
            SignedReviewFinding(
                finding_id=finding.id,
                rule_id=rule_id,
                outcome=finding.outcome,
                scope_label=finding.scope_label,
                actions=actions,
            )
        )
    return tuple(result)


def reviewer_marks_for(
    db: Session,
    revision_id: UUID,
    *,
    findings: Sequence[tuple[Finding, str]],
    decisions: Sequence[SignedReviewFinding],
    until: datetime | None,
) -> FrozenReviewerMarks:
    """Every person's confirm, fix and answer on the revision up to `until` (the sign-off; `None`
    for everything recorded, as the draft shows), and the results whose check could not run.
    Reads only; the order is fixed (readings, walls, pages, results)."""
    places = _Places(db)
    readings, drawn = _readings(db, revision_id, until, places)
    return FrozenReviewerMarks(
        marks=(
            *readings,
            *_walls(db, revision_id, until, places),
            *_pages(db, revision_id, until, places),
            *_by_hand(db, findings, decisions, places, drawn),
        ),
        not_checked_finding_ids=tuple(
            finding.id
            for finding, _ in findings
            if is_not_checked(finding.outcome, finding_reason(finding))
        ),
    )
