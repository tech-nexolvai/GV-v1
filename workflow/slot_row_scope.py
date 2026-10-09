"""Fail-closed qualification for an automatic countertop-row check scope."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Final, Literal
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
    also: tuple[tuple[str, int], ...] = ()
    """`(short model name, box number)` for each other numbered box a row reader named as a
    second countertop's piece row on this vendor page (#1108). V1 reads one row per page, so these
    are listed as not checked; empty on any other page and on a record from before #1108."""

    @property
    def label(self) -> str:
        if self.row_number is not None:
            return f"Countertop row {self.page_number}.{self.row_number} on page {self.page_number}"
        return f"Countertop row on page {self.page_number}"


#: The hold code the countertop results give a page whose row the readers did not agree on (#1093).
ROW_CHOICE_SPLIT: Final = "row-choice-split"

#: How the reader ends a reason it wrote itself for a page whose row it did not choose
#: (`workflow/slot_reader.py:_agreed_row`), and its default when no reader answer was kept.
_CODE_AUTHORED_ENDING: Final = "; the reviewer chooses"
_CODE_AUTHORED_DEFAULT: Final = (
    "No candidate row was selected; the reviewer must choose the countertop row."
)


@dataclass(frozen=True, slots=True)
class UnchosenRowPage:
    """A vendor page the newest slot-reader run read no countertop row on (#1093).

    `kind` is "split" when the readers did not name the same existing row (or one gave no answer):
    nothing was read, and the page must reach the reviewer as a blocking item. It is "none" only
    when every reader said the page has no countertop row; such a page is listed, not blocking.
    Since #1108 a reader also says what kind of answer it gave (`row-kind:` flags): "none" then
    needs every reader to have said `no_countertop`; `not_among_boxes` (a countertop no numbered
    box measures) or `unsure` from any reader is "split" too, with its own reason.
    `picks` are each reader's own pick (short model name, row number; 0 for "no row", `None` for no
    answer), empty on a record written before #1093. `kinds` are each reader's kind of answer
    (short model name, kind), empty on a record written before #1108. `reason` is the plain
    sentence for the reviewer; `stored_reason` is the record's own text.
    """

    record: ObservationCandidate
    page_number: int
    kind: Literal["split", "none"]
    picks: tuple[tuple[str, int | None], ...]
    reason: str
    stored_reason: str | None
    kinds: tuple[tuple[str, str], ...] = ()

    @property
    def split(self) -> bool:
        return self.kind == "split"

    @property
    def label(self) -> str:
        return f"Countertop row on page {self.page_number}"


def _row_picks(flags: frozenset[str]) -> tuple[tuple[str, int | None], ...] | None:
    """The `row-pick:<model>:<n|none>` flags, sorted by model; `None` when a flag is malformed."""
    picks: list[tuple[str, int | None]] = []
    for flag in sorted(flag for flag in flags if flag.startswith("row-pick:")):
        model, separator, pick = flag.removeprefix("row-pick:").rpartition(":")
        if not separator or not model:
            return None
        if pick == "none":
            picks.append((model, None))
        elif pick.isdigit():
            picks.append((model, int(pick)))
        else:
            return None
    return tuple(picks)


#: The kinds of row answer a reader may give (`slot-row-choice-v3`, #1108).
_ROW_KINDS: Final = frozenset({"row", "no_countertop", "not_among_boxes", "unsure"})


def _row_kinds(flags: frozenset[str]) -> tuple[tuple[str, str], ...] | None:
    """The `row-kind:<model>:<kind>` flags, sorted by model; `None` when a flag is malformed or a
    reader has two."""
    kinds: list[tuple[str, str]] = []
    for flag in sorted(flag for flag in flags if flag.startswith("row-kind:")):
        model, separator, kind = flag.removeprefix("row-kind:").rpartition(":")
        if not separator or not model or kind not in _ROW_KINDS:
            return None
        kinds.append((model, kind))
    if len({model for model, _ in kinds}) != len(kinds):
        return None
    return tuple(kinds)


def _row_also(flags: frozenset[str]) -> tuple[tuple[str, int], ...]:
    """The well-formed `row-also:<model>:<n>` flags, sorted by model then box number."""
    also: list[tuple[str, int]] = []
    for flag in flags:
        if not flag.startswith("row-also:"):
            continue
        model, separator, number = flag.removeprefix("row-also:").rpartition(":")
        if separator and model and number.isdigit() and int(number) > 0:
            also.append((model, int(number)))
    return tuple(sorted(also))


def _pick_words(pick: int | None) -> str:
    if pick is None:
        return "no answer"
    return "no countertop line" if pick == 0 else f"line {pick}"


def _classify(
    flags: frozenset[str], stored_reason: str | None
) -> tuple[
    Literal["split", "none"], tuple[tuple[str, int | None], ...], tuple[tuple[str, str], ...]
]:
    """Split unless every reader said "no countertop row"; never the other way round on doubt."""
    picks = _row_picks(flags)
    kinds = _row_kinds(flags)
    if picks is None or kinds is None:
        return "split", (), ()
    if kinds:
        # #1108: each reader said what its 0 meant. Only "no countertop on the sheet" from every
        # reader that picked is listed; any other kind, a missing kind or answer, blocks.
        readers = {model for model, _ in picks}
        none = (
            bool(picks)
            and all(pick == 0 for _, pick in picks)
            and {model for model, _ in kinds} == readers
            and all(kind == "no_countertop" for _, kind in kinds)
        )
        return ("none" if none else "split"), picks, kinds
    if picks:
        return ("none" if all(pick == 0 for _, pick in picks) else "split"), picks, ()
    # A record from before #1093 carries no picks. Its reason tells the two apart: the reader wrote
    # its own reason, ending "; the reviewer chooses", for a disagreement or a missing answer; a
    # "both said no row" record keeps the AI's own why. A record that names a row (`row-choice:N`,
    # N > 0) read nothing on it, so it waits for the reviewer too.
    choices = {flag.removeprefix("row-choice:") for flag in flags if flag.startswith("row-choice:")}
    reason = (stored_reason or "").strip()
    if (
        choices != {"0"}
        or not reason
        or reason.endswith(_CODE_AUTHORED_ENDING)
        or reason == _CODE_AUTHORED_DEFAULT
    ):
        return "split", (), ()
    return "none", (), ()


#: How the reason for the reviewer names each kind of row answer (#1108).
_KIND_WORDS: Final = {
    "row": "a numbered line",
    "no_countertop": "no countertop on the sheet",
    "not_among_boxes": "a countertop that none of the numbered lines measures",
    "unsure": "not sure which line",
}


def _split_reason(
    picks: tuple[tuple[str, int | None], ...],
    stored_reason: str | None,
    kinds: tuple[tuple[str, str], ...] = (),
) -> str:
    said_by_kind = dict(kinds)
    if kinds and all(pick == 0 for _, pick in picks):
        # Every reader answered 0, but not every one said "no countertop" (#1108): a countertop
        # may be drawn that none of the numbered lines measures, so nothing on it was read.
        words = (stored_reason or "").removesuffix(_CODE_AUTHORED_ENDING) or "; ".join(
            f"{model}: {_KIND_WORDS.get(said_by_kind.get(model, ''), 'no kind of answer')}"
            for model, _ in picks
        )
        said_kinds = {kind for _, kind in kinds}
        if said_kinds == {"not_among_boxes"} and len(kinds) == len(picks):
            opening = (
                "The AIs found a countertop on this page that none of the numbered lines measures"
            )
        elif "unsure" in said_kinds:
            opening = "An AI was not sure which line on this page is the countertop line"
        else:
            opening = "The AIs did not agree whether this page has a countertop"
        return (
            f"{opening} ({words}), so nothing on it was read or checked. The reviewer sets up "
            "this page's countertop line on the Measurements screen, or decides this page."
        )
    said = (
        "; ".join(
            f"{model} picked "
            + (
                _KIND_WORDS[said_by_kind[model]]
                if pick == 0 and said_by_kind.get(model) in _KIND_WORDS
                else _pick_words(pick)
            )
            for model, pick in picks
        )
        if picks
        else (stored_reason or "no reader answer was kept").removesuffix(_CODE_AUTHORED_ENDING)
    )
    return (
        f"The two AIs did not agree on this page's countertop line ({said}), so nothing on it was "
        "read or checked. The reviewer decides this page."
    )


def _unchosen_page(page_index: int, record: ObservationCandidate) -> UnchosenRowPage:
    kind, picks, kinds = _classify(frozenset(record.ambiguity_flags or ()), record.review_reason)
    return UnchosenRowPage(
        record=record,
        page_number=page_index + 1,
        kind=kind,
        picks=picks,
        reason=(
            _split_reason(picks, record.review_reason, kinds)
            if kind == "split"
            else (record.review_reason or "Both AIs found no countertop line on this page.")
        ),
        stored_reason=record.review_reason,
        kinds=kinds,
    )


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
    return slot_rows_and_unchosen_pages(session, revision_id)[0]


def unchosen_row_pages(session: Session, revision_id: UUID) -> tuple[UnchosenRowPage, ...]:
    """Vendor pages of the newest slot-reader run whose countertop row was not chosen (#1093).

    Each is the reader's `slot-reader-row-choice` record for that page, in page order. Only the
    newest run counts, as for `slot_rows`; a record on a page that is not the vendor drawing is left
    out, because no countertop is checked there.
    """
    return slot_rows_and_unchosen_pages(session, revision_id)[1]


def slot_rows_and_unchosen_pages(
    session: Session, revision_id: UUID
) -> tuple[tuple[SlotRow, ...], tuple[UnchosenRowPage, ...]]:
    """`slot_rows` and `unchosen_row_pages` from the same one read of the newest run's records."""
    run_id = latest_slot_reader_run(session, revision_id)
    if run_id is None:
        return (), ()
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
    walls: dict[tuple[UUID, str], ObservationCandidate] = {}
    unchosen: list[UnchosenRowPage] = []
    for page_index, candidate, role in records:
        flags = set(candidate.ambiguity_flags or [])
        if "slot-reader-row-choice" in flags:
            if role == "shop":
                unchosen.append(_unchosen_page(page_index, candidate))
            continue
        rank = next(
            (flag.removeprefix("row-rank:") for flag in flags if flag.startswith("row-rank:")), None
        )
        if rank is not None and candidate.raw_text.startswith("walls: "):
            walls[(candidate.page_id, rank)] = candidate
        if "slot-reader" not in flags:
            continue
        if rank is None:
            continue
        key = (page_index, rank)
        grouped.setdefault(key, []).append(candidate)
        roles[key] = role

    # Load the two append-only side tables once for the whole run. The former per-row proposal,
    # wall and decision lookups made a 20-row package issue 60 extra queries.
    candidate_ids = {candidate.id for candidates in grouped.values() for candidate in candidates}
    proposals_by_candidate = (
        {
            proposal.candidate_id: proposal
            for proposal in session.scalars(
                select(MeasurementProposal).where(
                    MeasurementProposal.package_revision_id == revision_id,
                    MeasurementProposal.candidate_id.in_(candidate_ids),
                )
            ).all()
        }
        if candidate_ids
        else {}
    )
    anchors_by_key = {
        key: next(
            candidate for candidate in candidates if "slot:0" in (candidate.ambiguity_flags or [])
        )
        for key, candidates in grouped.items()
        if sum("slot:0" in (candidate.ambiguity_flags or []) for candidate in candidates) == 1
    }
    anchor_ids = [anchor.id for anchor in anchors_by_key.values()]
    later_decision = aliased(SlotRowReviewDecision)
    decisions_by_anchor = (
        {
            decision.row_candidate_id: decision
            for decision in session.scalars(
                select(SlotRowReviewDecision).where(
                    SlotRowReviewDecision.row_candidate_id.in_(anchor_ids),
                    ~exists().where(later_decision.supersedes_id == SlotRowReviewDecision.id),
                )
            ).all()
        }
        if anchor_ids
        else {}
    )

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
        proposals = {
            candidate.id: proposals_by_candidate[candidate.id]
            for candidate in candidates
            if candidate.id in proposals_by_candidate
        }
        wall_candidate = walls.get((anchor.page_id, rank))
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
        decision = decisions_by_anchor.get(anchor.id)
        # Only an explicit wall choice on this exact row can clear this one check hold.
        # A typed value, another row's choice, or another hold cannot release it.
        if not (between_panels and decision is not None and decision.wall_config is not None):
            held = held or next(
                (CHECK_HOLD_REASONS.get(code, code) for code in sorted(check_holds)), None
            )
        is_vendor = roles[(page_index, rank)] == "shop"
        also = (
            tuple(
                sorted(
                    {
                        pair
                        for candidate in candidates
                        for pair in _row_also(frozenset(candidate.ambiguity_flags or ()))
                    }
                )
            )
            if is_vendor
            else ()
        )
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
                also=also,
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
    return tuple(numbered), tuple(unchosen)


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
