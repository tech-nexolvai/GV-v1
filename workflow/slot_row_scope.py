"""Fail-closed qualification for an automatic countertop-row check scope."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models.document import Document, DocumentVersion, PackageRevisionDocument, Page
from app.models.evidence import MeasurementProposal, ObservationCandidate, SlotRowReviewDecision
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from vocabulary.check_holds import CHECK_HOLD_REASONS, STONE_SHORT_OF_ENDS
from vocabulary.reviewer_reasons import reviewer_reason


@dataclass(frozen=True, slots=True)
class SlotRowReading:
    candidate_id: UUID
    position: int | None
    value_numerator: int | None
    value_denominator: int | None
    unit: str | None
    status: str | None
    lane: str | None
    flags: frozenset[str]


@dataclass(frozen=True, slots=True)
class SlotRowQualification:
    eligible: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class SlotRow:
    anchor: ObservationCandidate
    page_number: int
    piece_count: int
    candidates: tuple[ObservationCandidate, ...]
    proposals: dict[UUID, MeasurementProposal]
    wall_candidate: ObservationCandidate | None
    decision: SlotRowReviewDecision | None
    held_reason: str | None
    wall_confirmation_allowed: bool
    row_number: int | None = None

    @property
    def label(self) -> str:
        if self.row_number is not None:
            return f"Countertop row {self.page_number}.{self.row_number} on page {self.page_number}"
        return f"Countertop row on page {self.page_number}"


def candidate_is_sealed(candidate: ObservationCandidate) -> bool:
    """Whether this reader candidate was admitted to the second-reader lane.

    The verdict path independently checks the two stored source answers before creating canonical
    evidence. This predicate only controls whether the UI/API may treat a proposal as an already
    supplied value; anything less must remain an input for the reviewer.
    """
    reader_ids = {
        flag.removeprefix("reader-id:")
        for flag in candidate.ambiguity_flags or ()
        if flag.startswith("reader-id:")
    }
    return (
        candidate.corroboration_status == "CORROBORATED"
        and candidate.corroboration_lane == "SECOND_READER"
        and candidate.value_numerator is not None
        and candidate.value_denominator is not None
        and candidate.value_denominator > 0
        and candidate.unit in {"in", "mm"}
        and (not reader_ids or len(reader_ids) == 2)
    )


def latest_slot_reader_run(session: Session, revision_id: UUID) -> UUID | None:
    return session.execute(
        select(ExtractionRun.id)
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(
            WorkflowRun.package_revision_id == revision_id,
            ExtractionRun.extractor_version.like("slot-reader%"),
        )
        .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_row_decision(session: Session, row_candidate_id: UUID) -> SlotRowReviewDecision | None:
    later = aliased(SlotRowReviewDecision)
    return session.execute(
        select(SlotRowReviewDecision)
        .where(
            SlotRowReviewDecision.row_candidate_id == row_candidate_id,
            ~exists().where(later.supersedes_id == SlotRowReviewDecision.id),
        )
        .order_by(SlotRowReviewDecision.created_at.desc(), SlotRowReviewDecision.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def slot_rows(session: Session, revision_id: UUID) -> tuple[SlotRow, ...]:
    """Return rows from only the newest slot-reader run; never combine pages or row ranks."""
    run_id = latest_slot_reader_run(session, revision_id)
    if run_id is None:
        return ()
    records = session.execute(
        select(Page.index, ObservationCandidate, Document.kind)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(
            ObservationCandidate.extraction_run_id == run_id,
            PackageRevisionDocument.package_revision_id == revision_id,
        )
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    grouped: dict[tuple[int, str], list[ObservationCandidate]] = {}
    roles: dict[tuple[int, str], str] = {}
    for page_index, candidate, role in records:
        flags = set(candidate.ambiguity_flags or [])
        if "slot-reader" not in flags:
            continue
        rank = next(
            (flag.removeprefix("row-rank:") for flag in flags if flag.startswith("row-rank:")), None
        )
        if rank is None:
            continue
        key = (page_index, rank)
        grouped.setdefault(key, []).append(candidate)
        roles[key] = role

    output: list[SlotRow] = []
    for (page_index, rank), candidates in sorted(
        grouped.items(),
        key=lambda entry: (
            entry[0][0],
            int(entry[0][1]) if entry[0][1].isdigit() else -1,
            entry[0][1],
        ),
    ):
        anchors = [
            candidate for candidate in candidates if "slot:0" in (candidate.ambiguity_flags or [])
        ]
        if len(anchors) != 1:
            continue
        anchor = anchors[0]
        count_flags = {
            flag.removeprefix("row-slot-count:")
            for candidate in candidates
            for flag in (candidate.ambiguity_flags or [])
            if flag.startswith("row-slot-count:")
        }
        if len(count_flags) != 1 or not next(iter(count_flags)).isdigit():
            continue
        piece_count = int(next(iter(count_flags)))
        latest_proposals = session.scalars(
            select(MeasurementProposal).where(
                MeasurementProposal.package_revision_id == revision_id,
                MeasurementProposal.candidate_id.in_([candidate.id for candidate in candidates]),
            )
        ).all()
        proposals = {proposal.candidate_id: proposal for proposal in latest_proposals}
        wall = (
            session.execute(
                select(ObservationCandidate).where(
                    ObservationCandidate.extraction_run_id == run_id,
                    ObservationCandidate.page_id == anchor.page_id,
                    ObservationCandidate.raw_text.startswith("walls: "),
                )
            )
            .scalars()
            .all()
        )
        wall_candidate = next(
            (item for item in wall if f"row-rank:{rank}" in (item.ambiguity_flags or [])),
            None,
        )
        held = next(
            (
                candidate.review_reason
                or next(
                    (
                        flag.removeprefix("row-hold:")
                        for flag in candidate.ambiguity_flags or ()
                        if flag.startswith("row-hold:")
                    ),
                    None,
                )
                for candidate in candidates
                if any(
                    flag == "row-ambiguous" or flag == "row-partial" or flag.startswith("row-hold:")
                    for flag in (candidate.ambiguity_flags or [])
                )
            ),
            None,
        )
        check_holds = {
            flag.removeprefix("check-hold:")
            for candidate in candidates
            for flag in candidate.ambiguity_flags or ()
            if flag.startswith("check-hold:")
        }
        has_row_hold = any(
            flag in {"row-ambiguous", "row-partial"} or flag.startswith("row-hold:")
            for candidate in candidates
            for flag in candidate.ambiguity_flags or ()
        )
        if has_row_hold:
            held = reviewer_reason(
                (
                    flag
                    for candidate in candidates
                    for flag in candidate.ambiguity_flags or ()
                    if not flag.startswith("check-hold:")
                ),
                held or "This row needs a reviewer decision.",
            )
        between_panels = not has_row_hold and check_holds == {STONE_SHORT_OF_ENDS[0]}
        decision = latest_row_decision(session, anchor.id)
        # Only an explicit wall choice on this exact row can clear this one check hold.
        # A typed value, another row's choice, or another hold cannot release it.
        if not (between_panels and decision is not None and decision.wall_config is not None):
            held = held or next(
                (CHECK_HOLD_REASONS.get(code, code) for code in sorted(check_holds)), None
            )
        is_vendor = roles[(page_index, rank)] == "shop"
        output.append(
            SlotRow(
                anchor=anchor,
                page_number=page_index + 1,
                piece_count=piece_count,
                candidates=tuple(candidates),
                proposals=proposals,
                wall_candidate=wall_candidate,
                decision=decision,
                held_reason=held if is_vendor else "This is not the vendor drawing.",
                wall_confirmation_allowed=is_vendor and (held is None or between_panels),
            )
        )
    counts = {
        number: sum(row.page_number == number for row in output)
        for number in {row.page_number for row in output}
    }
    numbered: list[SlotRow] = []
    for row in output:
        number = 1 + sum(previous.page_number == row.page_number for previous in numbered)
        numbered.append(replace(row, row_number=number if counts[row.page_number] > 1 else None))
    return tuple(numbered)


def candidate_value(candidate: ObservationCandidate) -> Fraction | None:
    if (
        candidate.value_numerator is None
        or candidate.value_denominator is None
        or candidate.value_denominator <= 0
        or candidate.unit not in {"in", "mm"}
    ):
        return None
    value = Fraction(candidate.value_numerator, candidate.value_denominator)
    return value if candidate.unit == "in" else value * Fraction(50, 127)


def qualify_slot_row(
    readings: tuple[SlotRowReading, ...],
    *,
    expected_piece_count: int,
    held_reason: str | None,
    shop_document: bool,
) -> SlotRowQualification:
    """Require every ordered piece and the overall from one unheld vendor row.

    No candidate from another row or page can repair a gap: callers pass candidates that share the
    same row anchor, and this function requires their exact positions to be contiguous.
    """
    if not shop_document:
        return SlotRowQualification(False, "The selected row is not on the vendor drawing.")
    if held_reason:
        return SlotRowQualification(False, held_reason)
    if expected_piece_count <= 0:
        return SlotRowQualification(False, "This row has no identified pieces to check.")
    expected = {None, *range(expected_piece_count)}
    actual = [reading.position for reading in readings]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        return SlotRowQualification(
            False, "This row is incomplete; confirm every piece and its overall."
        )
    for reading in readings:
        reader_sealed = (
            reading.status == "CORROBORATED"
            and reading.lane == "SECOND_READER"
            and (
                not any(flag.startswith("reader-id:") for flag in reading.flags)
                or len(
                    {
                        flag.removeprefix("reader-id:")
                        for flag in reading.flags
                        if flag.startswith("reader-id:")
                    }
                )
                == 2
            )
        )
        human_saved = reading.status == "HUMAN_CONFIRMED" and "human-saved-for-row" in reading.flags
        if (
            not (reader_sealed or human_saved)
            or reading.value_numerator is None
            or reading.value_denominator is None
            or reading.value_denominator <= 0
            or reading.unit not in {"in", "mm"}
        ):
            return SlotRowQualification(False, "A value in this row is not sealed by both readers.")
        if not human_saved and any(
            flag in {"row-partial", "row-ambiguous", "reviewer-markup"}
            or flag.startswith(("row-hold:", "drawn-length"))
            for flag in reading.flags
        ):
            return SlotRowQualification(False, "This row is held for reviewer input.")
    return SlotRowQualification(True, None)


def effective_row_wall(
    *,
    layout: str | None,
    source: str | None,
    held: bool,
    reviewer_confirmed: str | None,
) -> tuple[str | None, str | None]:
    """Drawing clues may decide; reader-only proposals need confirmation on this exact row."""
    if reviewer_confirmed is not None:
        return reviewer_confirmed, None
    if held:
        return None, "The wall layout is held for reviewer input."
    if layout is None:
        return None, "Choose the wall layout for this countertop row."
    if source == "vendor-drawing-clues":
        return layout, None
    if source == "readers":
        return None, "Confirm the proposed wall layout for this countertop row."
    return None, "The wall layout has no approved source for this row."
