"""Compared with the architect: every architect dimension, the vendor's number for the same span, and what code decided (#1418).

The worker stores, once per reading, where each dimension the architect printed was paired with
the vendor's dimension line by geometry only (`workflow/architect_compare.py`). This reads those
records and decides, for each, in this order:

1. **unpaired** ("?"): no single vendor line draws the same span; the stored reason says why.
2. **unread** ("?"): the vendor's number on that line was not read as a value code may use. It
   comes only from a reading already stored: the slot reader's label at that spot, sealed by both
   AIs agreeing, and positively the vendor's ink (`vendor_ink_only`; GV's markup is never a
   source). No new AI question is asked.
3. **rule**: a published rule's current result already used that reading (its finding's evidence,
   or the row it was scoped to). That result is shown instead, so nothing is flagged twice.
4. **same** (✓) when the vendor's value equals the architect's exactly, **different** (✗, REVIEW)
   otherwise. Exact `Fraction`s; never a tolerance, never a rounding.

**Every architect dimension exactly once**, in page and reading order. Read-only: nothing is
written here, and the decision is the same every time it is read from the same records.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.evidence.sides import page_transform_at, reading_transform
from app.models import (
    CanonicalObservation,
    CheckRun,
    Finding,
    ObservationCandidate,
    Page,
    VerdictInput,
)
from app.models.architect_compare import ArchitectCompareAnswer, ArchitectCompareRecord
from app.models.evidence import EvidenceSupportingCandidate
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.review.row_location import RowLocation
from evidence.coordinates import ImagePoint, PageTransform, PdfPoint
from workflow.slot_row_scope import candidate_is_sealed, candidate_value

__all__ = [
    "ANSWERS_FOR",
    "ANSWER_NOTES",
    "KEY_PREFIX",
    "NOT_COMPARED_GROUP",
    "SENT_AS",
    "ArchitectCompareCountsOut",
    "ArchitectCompareOut",
    "CompareRefused",
    "CompareResult",
    "architect_compare_for_revision",
    "architect_compare_many",
    "open_architect_compare",
    "open_architect_compare_many",
    "record_compare_answers",
]

_Box = tuple[Decimal, Decimal, Decimal, Decimal]

CompareResult = Literal["same", "different", "rule", "unpaired", "unread"]

NOT_READ = "? couldn't read the vendor's number: the AIs have not read the label on this line"
NOT_AGREED = "? couldn't read the vendor's number: the two AIs did not agree on it"
NOT_VENDOR_INK = (
    "? couldn't read the vendor's number: it is not positively the vendor's own ink "
    "(GV's markup, a second label or unchecked ink): check it by hand"
)
TWO_READINGS = "? couldn't read the vendor's number: two readings sit on this label"


class ArchitectCompareOut(BaseModel):
    """One dimension the architect printed, and what the vendor's drawing shows for the same span."""

    key: str = Field(description="`arch:<record id>`: stable for this reading.")
    page: int = Field(description="The sheet, from 1.")
    view: str | None = Field(description="The architect view's printed title, when it has one.")
    architect_text: str
    architect_inches: str | None = Field(
        description="Exact inches as `n/d` (or `n`); null when the architect's label is held."
    )
    vendor_text: str | None = Field(description="The vendor's number as the AIs read it.")
    vendor_inches: str | None = Field(description="Exact inches, `n/d`; null when not read.")
    result: CompareResult
    rule_id: str | None = Field(description="When `rule`: the published rule shown instead.")
    rule_outcome: str | None = Field(description="When `rule`: that rule's current outcome.")
    why: str = Field(description="In plain words.")
    finding_id: UUID | None = Field(description="When `rule`: that rule's finding.")
    architect_ref: RowLocation | None
    vendor_ref: RowLocation | None
    answer: str | None = Field(
        default=None,
        description=(
            "A person's newest answer, as stored: `vendor_wrong` | `not_a_difference` (on "
            "`different`), `checked_right` | `checked_wrong` | `not_compared` (on `unpaired` or "
            "`unread`; `not_compared` is the group's 'Clear all as not compared')."
        ),
    )
    answered_by: str | None = None
    paired_by: str | None = Field(
        default=None, description="`geometry` (code alone) or `two AIs`; null when unpaired."
    )
    needs_person: bool = Field(
        default=False,
        description="True while a `different`, `unpaired` or `unread` item has no answer: sign-off waits.",
    )
    clouded: bool = Field(
        default=False,
        description=(
            "True when the cloud PDF marks it at the vendor's dimension: `different` unless a person "
            "answered `not_a_difference`, or a person answered `checked_wrong`."
        ),
    )
    actions: list[str] = Field(
        default_factory=list,
        description=(
            "The answers this item takes now: `agree` (the vendor is wrong; URL action `agree`) and "
            "`not_this` (not a difference) on `different`; `checked_right` and `checked_wrong` on "
            "`unpaired`/`unread` (URL action `answer`, the name in the body)."
        ),
    )
    status: Literal["open", "done"] = Field(
        default="done", description="`open` while it needs a person (`needs_person`)."
    )


class ArchitectCompareCountsOut(BaseModel):
    same: int = 0
    different: int = 0
    rule: int = 0
    unpaired: int = 0
    unread: int = 0
    needs_person: int = 0


#: The answers each result takes, and the fixed note each records.
ANSWER_NOTES: Final[Mapping[str, str]] = {
    "vendor_wrong": "Agreed: the vendor's number is not the architect's. It stays clouded.",
    "not_a_difference": "Not a difference: the person checked these two numbers measure differently.",
    "checked_right": "Checked by hand: the vendor's drawing is right for this architect dimension.",
    "checked_wrong": "Checked by hand: the vendor's drawing is wrong for this architect dimension.",
    "not_compared": "Cleared as not compared: no vendor dimension was paired with this one.",
}
ANSWERS_FOR: Final[Mapping[str, tuple[str, ...]]] = {
    "different": ("vendor_wrong", "not_a_difference"),
    "unpaired": ("checked_right", "checked_wrong"),
    "unread": ("checked_right", "checked_wrong"),
    "same": (),
    "rule": (),
}
#: The names the screen sends (`frontend/main/src/api/review-items.ts`), as the answers stored.
SENT_AS: Final[Mapping[str, str]] = {
    "agree": "vendor_wrong",
    "agree_vendor_wrong": "vendor_wrong",
    "vendor_wrong": "vendor_wrong",
    "not_this": "not_a_difference",
    "not_a_difference": "not_a_difference",
    "checked_right": "checked_right",
    "checked_wrong": "checked_wrong",
    "clear_not_compared": "not_compared",
}
#: Each stored answer as the screen names it in an item's `actions`.
_OFFERED_AS: Final[Mapping[str, str]] = {"vendor_wrong": "agree", "not_a_difference": "not_this"}
#: The group key for "Clear all as not compared": every open unpaired or unread item, by
#: `expected_keys` (the screen's `NOT_COMPARED_GROUP`).
NOT_COMPARED_GROUP: Final = "architect-not-compared"
KEY_PREFIX: Final = "arch:"


@dataclass(frozen=True, slots=True)
class _Rule:
    rule_id: str
    outcome: str
    finding_id: UUID


def _fraction_text(value: Fraction) -> str:
    return str(value)


def _box(stored: object) -> _Box | None:
    if not isinstance(stored, list) or len(stored) != 4:
        return None
    try:
        x0, top, x1, bottom = (Decimal(str(value)) for value in stored)
    except InvalidOperation:
        return None
    if not all(v.is_finite() for v in (x0, top, x1, bottom)) or x1 < x0 or bottom < top:
        return None
    return x0, top, x1, bottom


def _union(
    boxes: Iterable[_Box | None],
) -> _Box | None:
    found = [box for box in boxes if box is not None]
    if not found:
        return None
    return (
        min(b[0] for b in found),
        min(b[1] for b in found),
        max(b[2] for b in found),
        max(b[3] for b in found),
    )


def _location(page: Page, transform: PageTransform | None, box: _Box) -> RowLocation | None:
    """A page-point box (pdfplumber's frame) in the published stored space, or `None`."""
    height = page.height_pt
    if transform is None or not isinstance(height, Decimal):
        return None
    x0, top, x1, bottom = box
    try:
        corners = [
            transform.to_stored(transform.to_image(PdfPoint(x, height - y)))
            for x, y in ((x0, top), (x1, bottom))
        ]
    except (ArithmeticError, TypeError, ValueError):
        return None
    left, right = min(p.x for p in corners), max(p.x for p in corners)
    upper, lower = min(p.y for p in corners), max(p.y for p in corners)
    if any(not v.is_finite() or v < 0 or v > 1 for v in (left, right, upper, lower)):
        return None
    return RowLocation(
        page_id=page.id,
        document_version_id=page.document_version_id,
        page_number=page.index + 1,
        polygon=[
            [str(x), str(y)]
            for x, y in ((left, upper), (right, upper), (right, lower), (left, lower))
        ],
    )


def _latest_records(
    session: Session, revision_ids: Sequence[UUID]
) -> dict[UUID, list[tuple[ArchitectCompareRecord, ExtractionRun]]]:
    """The records of each revision's newest compare reading, with that reading's run.

    One statement for any number of revisions, and the only one when none of them has a record
    (#1426): sign-off readiness asks this for every revision it judges.
    """
    newest = (
        func.rank()
        .over(
            partition_by=ArchitectCompareRecord.package_revision_id,
            order_by=(ExtractionRun.created_at.desc(), ExtractionRun.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(ArchitectCompareRecord.id.label("record_id"), newest)
        .join(ExtractionRun, ExtractionRun.id == ArchitectCompareRecord.extraction_run_id)
        .where(ArchitectCompareRecord.package_revision_id.in_(revision_ids))
        .subquery()
    )
    found: dict[UUID, list[tuple[ArchitectCompareRecord, ExtractionRun]]] = {}
    for record, run in session.execute(
        select(ArchitectCompareRecord, ExtractionRun)
        .join(ExtractionRun, ExtractionRun.id == ArchitectCompareRecord.extraction_run_id)
        .join(ranked, ranked.c.record_id == ArchitectCompareRecord.id)
        .where(ranked.c.rank == 1)
    ).tuples():
        found.setdefault(record.package_revision_id, []).append((record, run))
    return found


def _slot_box(flags: Sequence[str]) -> tuple[int, int, int, int] | None:
    found = [flag.removeprefix("slot-box:") for flag in flags if flag.startswith("slot-box:")]
    if len(found) != 1:
        return None
    try:
        x0, y0, x1, y1 = (int(part) for part in found[0].split(","))
    except ValueError:
        return None
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def _reading_box(
    candidate: ObservationCandidate, page: Page, transform: PageTransform | None
) -> _Box | None:
    """Where a slot-reader label was read, in page points (pdfplumber's frame)."""
    height = page.height_pt
    if transform is None or not isinstance(height, Decimal):
        return None
    pixels = _slot_box(candidate.ambiguity_flags or ())
    corners: list[tuple[int, int]]
    if pixels is not None:
        corners = [(pixels[0], pixels[1]), (pixels[2], pixels[3])]
    elif candidate.coordinate_space == "image" and isinstance(candidate.polygon, list):
        try:
            corners = [(int(x), int(y)) for x, y in candidate.polygon]
        except (TypeError, ValueError):
            return None
    else:
        return None
    try:
        points = [transform.to_pdf(ImagePoint(x, y)) for x, y in corners]
    except (ArithmeticError, TypeError, ValueError):
        return None
    xs = [p.x for p in points]
    tops = [height - p.y for p in points]
    return min(xs), min(tops), max(xs), max(tops)


def _overlaps(a: _Box, b: _Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _row_rank(flags: Sequence[str]) -> str | None:
    found = [flag for flag in flags if flag.startswith("row-rank:")]
    return found[0] if len(found) == 1 else None


def _supported(flags: Sequence[str]) -> UUID | None:
    if "slot-reader-support" not in flags:
        return None
    found = [flag.removeprefix("supports:") for flag in flags if flag.startswith("supports:")]
    if len(found) != 1:
        return None
    try:
        return UUID(found[0])
    except ValueError:
        return None


#: A live published result: its finding, check run, outcome, scoped row and rule id.
_Live = tuple[UUID, UUID, str, UUID | None, str]


def _live_results(
    session: Session, revision_ids: Sequence[UUID]
) -> tuple[dict[UUID, list[_Live]], dict[UUID, list[tuple[UUID, list[str] | None]]]]:
    """Every revision's current published results, and the readings each check run cited: two
    statements for any number of revisions (one when none has a result)."""
    live: dict[UUID, list[_Live]] = {}
    rows = session.execute(
        select(
            Finding.package_revision_id,
            Finding.id,
            Finding.check_run_id,
            Finding.outcome,
            Finding.scope_row_candidate_id,
        )
        .add_columns(RuleDefinition.rule_id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(Finding.package_revision_id.in_(revision_ids), CheckRun.superseded_at.is_(None))
    ).all()
    if not rows:
        return {}, {}
    for revision_id, fid, run_id, outcome, scope, rule_id in rows:
        live.setdefault(revision_id, []).append((fid, run_id, outcome, scope, rule_id))
    cited: dict[UUID, list[tuple[UUID, list[str] | None]]] = {}
    for run_id, candidate_id, flags in session.execute(
        select(
            VerdictInput.check_run_id, ObservationCandidate.id, ObservationCandidate.ambiguity_flags
        )
        .join(
            CanonicalObservation,
            CanonicalObservation.id == VerdictInput.canonical_observation_id,
        )
        .join(
            EvidenceSupportingCandidate,
            EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
        )
        .join(
            ObservationCandidate,
            ObservationCandidate.id == EvidenceSupportingCandidate.candidate_id,
        )
        .where(VerdictInput.check_run_id.in_({row[2] for row in rows}))
    ).all():
        cited.setdefault(run_id, []).append((candidate_id, flags))
    return live, cited


def _rules_by_reading(
    live: Sequence[_Live],
    cited: Mapping[UUID, Sequence[tuple[UUID, list[str] | None]]],
    labels: Sequence[ObservationCandidate],
) -> dict[UUID, list[_Rule]]:
    """For each slot-reader label, the current published results that used it: through their
    recorded operands' evidence, or because the result is scoped to the label's row."""
    if not live:
        return {}
    by_run = {run_id: _Rule(rule_id, outcome, fid) for fid, run_id, outcome, _s, rule_id in live}
    found: dict[UUID, list[_Rule]] = {}
    label_ids = {label.id for label in labels}
    rows: dict[tuple[UUID, UUID, str], list[UUID]] = {}
    by_id = {label.id: label for label in labels}
    for label in labels:
        rank = _row_rank(label.ambiguity_flags or ())
        if rank is not None:
            rows.setdefault((label.page_id, label.extraction_run_id, rank), []).append(label.id)
    for fid, run_id, outcome, scope, rule_id in live:
        anchor = by_id.get(scope) if scope is not None else None
        if anchor is None:
            continue
        rank = _row_rank(anchor.ambiguity_flags or ())
        members = rows.get((anchor.page_id, anchor.extraction_run_id, rank), []) if rank else []
        for member in members or [anchor.id]:
            found.setdefault(member, []).append(_Rule(rule_id, outcome, fid))
    for run_id, rule in by_run.items():
        for candidate_id, flags in cited.get(run_id, ()):
            target = candidate_id if candidate_id in label_ids else _supported(flags or [])
            if target in label_ids and target is not None and rule not in found.get(target, []):
                found.setdefault(target, []).append(rule)
    return found


def _labels(
    session: Session, pages_by_revision: Mapping[UUID, set[UUID]]
) -> dict[UUID, tuple[ExtractionRun, list[ObservationCandidate]]]:
    """Each revision's newest slot-reader run (as `latest_slot_reader_run` picks it) and its
    slot-reader labels on the compared pages: two statements for any number of revisions."""
    ranked = (
        select(
            WorkflowRun.package_revision_id.label("revision_id"),
            ExtractionRun.id.label("run_id"),
            func.row_number()
            .over(
                partition_by=WorkflowRun.package_revision_id,
                order_by=(ExtractionRun.created_at.desc(), ExtractionRun.id.desc()),
            )
            .label("rank"),
        )
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(
            WorkflowRun.package_revision_id.in_(list(pages_by_revision)),
            ExtractionRun.extractor_version.like("slot-reader%"),
        )
        .subquery()
    )
    runs = {
        revision_id: run
        for revision_id, run in session.execute(
            select(ranked.c.revision_id, ExtractionRun)
            .join(ExtractionRun, ExtractionRun.id == ranked.c.run_id)
            .where(ranked.c.rank == 1)
        ).tuples()
        if pages_by_revision.get(revision_id)
    }
    if not runs:
        return {}
    revision_of = {run.id: revision_id for revision_id, run in runs.items()}
    labels: dict[UUID, list[ObservationCandidate]] = {revision_id: [] for revision_id in runs}
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id.in_(list(revision_of)),
            ObservationCandidate.page_id.in_(set().union(*pages_by_revision.values())),
        )
    ):
        revision_id = revision_of[candidate.extraction_run_id]
        if candidate.page_id in pages_by_revision[revision_id] and "slot-reader" in (
            candidate.ambiguity_flags or ()
        ):
            labels[revision_id].append(candidate)
    return {revision_id: (run, labels[revision_id]) for revision_id, run in runs.items()}


def _vendor_ink_only(flags: Sequence[str]) -> bool:
    from app.review.review_items import vendor_ink_only

    return vendor_ink_only(flags)


def _decide(
    record: ArchitectCompareRecord,
    architect: Fraction | None,
    reading: ObservationCandidate | None,
    several: bool,
    rules: Mapping[UUID, list[_Rule]],
) -> tuple[CompareResult, str, Fraction | None, _Rule | None]:
    if record.pairing != "paired" or architect is None:
        return "unpaired", f"? {record.reason}", None, None
    if several:
        return "unread", TWO_READINGS, None, None
    if reading is None:
        return "unread", NOT_READ, None, None
    if not candidate_is_sealed(reading):
        return "unread", NOT_AGREED, None, None
    if not _vendor_ink_only(reading.ambiguity_flags or ()):
        return "unread", NOT_VENDOR_INK, None, None
    value = candidate_value(reading)
    if value is None:
        return "unread", NOT_AGREED, None, None
    covered = sorted(
        rules.get(reading.id, []), key=lambda rule: (rule.rule_id, str(rule.finding_id))
    )
    if covered:
        rule = covered[0]
        return (
            "rule",
            f"Checked by the rule {rule.rule_id} on this number: its result is shown instead.",
            value,
            rule,
        )
    if value == architect:
        return (
            "same",
            "The vendor prints the same number as the architect for this span.",
            value,
            None,
        )
    return (
        "different",
        "The vendor's number for this span is not the architect's: review it (it is clouded).",
        value,
        None,
    )


def architect_compare_for_revision(
    session: Session, revision_id: UUID
) -> tuple[list[ArchitectCompareOut], ArchitectCompareCountsOut]:
    """Every architect dimension of the revision's newest compare reading, exactly once, with what
    code decided for it, and the counts. Empty when the compare has not run."""
    return architect_compare_many(session, [revision_id]).get(
        revision_id, ([], ArchitectCompareCountsOut())
    )


def architect_compare_many(
    session: Session, revision_ids: Sequence[UUID]
) -> dict[UUID, tuple[list[ArchitectCompareOut], ArchitectCompareCountsOut]]:
    """`architect_compare_for_revision` for a page of revisions, with a bounded query plan (#1426):
    one statement when none of them has a compare record, and a fixed handful otherwise (the
    records, their pages, the slot-reader runs and labels, the live results and the answers),
    whatever the number of revisions or records. Revisions without a record are left out."""
    ids = list(dict.fromkeys(revision_ids))
    if not ids:
        return {}
    by_revision = _latest_records(session, ids)
    if not by_revision:
        return {}
    every = [record for pairs in by_revision.values() for record, _run in pairs]
    pages = {
        page.id: page
        for page in session.scalars(
            select(Page).where(Page.id.in_({record.page_id for record in every}))
        )
    }
    slots = _labels(
        session,
        {
            revision_id: {record.page_id for record, _run in pairs}
            for revision_id, pairs in by_revision.items()
        },
    )
    live, cited = _live_results(session, list(by_revision))
    answers: dict[UUID, ArchitectCompareAnswer] = {}
    for row in session.scalars(
        select(ArchitectCompareAnswer)
        .where(ArchitectCompareAnswer.record_id.in_([record.id for record in every]))
        .order_by(ArchitectCompareAnswer.created_at, ArchitectCompareAnswer.id)
    ):
        answers[row.record_id] = row
    result: dict[UUID, tuple[list[ArchitectCompareOut], ArchitectCompareCountsOut]] = {}
    for revision_id, pairs in by_revision.items():
        slot_run, labels = slots.get(revision_id, (None, []))
        result[revision_id] = _compared(
            [record for record, _run in pairs],
            {run.id: run for _record, run in pairs},
            pages,
            slot_run,
            labels,
            _rules_by_reading(live.get(revision_id, ()), cited, labels),
            answers,
        )
    return result


def _compared(
    records: Sequence[ArchitectCompareRecord],
    runs: Mapping[UUID, ExtractionRun],
    pages: Mapping[UUID, Page],
    slot_run: ExtractionRun | None,
    labels: Sequence[ObservationCandidate],
    rules: Mapping[UUID, list[_Rule]],
    answers: Mapping[UUID, ArchitectCompareAnswer],
) -> tuple[list[ArchitectCompareOut], ArchitectCompareCountsOut]:
    """One revision's compared dimensions and counts, from rows already read (no statement)."""
    placed: dict[UUID, list[tuple[ObservationCandidate, _Box]]] = {}
    for label in labels:
        page = pages.get(label.page_id)
        if page is None or slot_run is None:
            continue
        box = _reading_box(label, page, reading_transform(page, slot_run))
        if box is not None:
            placed.setdefault(label.page_id, []).append((label, box))
    ordered = sorted(
        records,
        key=lambda r: (pages[r.page_id].index, r.architect_view, r.architect_number, str(r.id)),
    )
    out: list[ArchitectCompareOut] = []
    counts = ArchitectCompareCountsOut()
    for record in ordered:
        page = pages[record.page_id]
        run = runs.get(record.extraction_run_id)
        transform = None if run is None or run.dpi is None else page_transform_at(page, run.dpi)
        architect = (
            None
            if record.architect_numerator is None or record.architect_denominator is None
            else Fraction(record.architect_numerator, record.architect_denominator)
        )
        label_box = _box(record.vendor_label_box)
        hits = (
            []
            if label_box is None
            else [c for c, box in placed.get(record.page_id, []) if _overlaps(label_box, box)]
        )
        reading = hits[0] if len(hits) == 1 else None
        result, why, value, rule = _decide(record, architect, reading, len(hits) > 1, rules)
        given = answers.get(record.id)
        allowed = ANSWERS_FOR[result]
        answer = (
            given.answer
            if given is not None
            and given.answer
            in (
                *allowed,
                *(("not_compared",) if result in ("unpaired", "unread") else ()),
            )
            else None
        )
        architect_box = _box(record.architect_box)
        vendor_box = _union((label_box, _box(record.vendor_line_box)))
        out.append(
            ArchitectCompareOut(
                key=f"arch:{record.id}",
                page=page.index + 1,
                view=record.view_title,
                architect_text=record.architect_text,
                architect_inches=None if architect is None else _fraction_text(architect),
                vendor_text=None if value is None or reading is None else reading.raw_text,
                vendor_inches=None if value is None else _fraction_text(value),
                result=result,
                rule_id=None if rule is None else rule.rule_id,
                rule_outcome=None if rule is None else rule.outcome,
                why=why,
                finding_id=None if rule is None else rule.finding_id,
                architect_ref=(
                    None if architect_box is None else _location(page, transform, architect_box)
                ),
                vendor_ref=(
                    None
                    if vendor_box is None or record.pairing != "paired"
                    else _location(page, transform, vendor_box)
                ),
                paired_by=record.paired_by,
                answer=answer,
                answered_by=None if answer is None or given is None else given.answered_by,
                needs_person=bool(allowed) and answer is None,
                clouded=(result == "different" and answer != "not_a_difference")
                or answer == "checked_wrong",
                actions=[] if answer is not None else [_OFFERED_AS.get(n, n) for n in allowed],
                status="open" if bool(allowed) and answer is None else "done",
            )
        )
        setattr(counts, result, getattr(counts, result) + 1)
        counts.needs_person += bool(allowed) and answer is None
    return out, counts


class CompareRefused(ValueError):
    """An answer the item does not take now, or a group that changed since it was shown."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def open_architect_compare(session: Session, revision_id: UUID) -> int:
    """How many compared dimensions still need a person (sign-off waits for them)."""
    return open_architect_compare_many(session, [revision_id]).get(revision_id, 0)


def open_architect_compare_many(session: Session, revision_ids: Sequence[UUID]) -> dict[UUID, int]:
    """For each revision, how many compared dimensions still need a person (sign-off waits for
    them); a revision with none is left out. Bounded as `architect_compare_many` is."""
    return {
        revision_id: counts.needs_person
        for revision_id, (_items, counts) in architect_compare_many(session, revision_ids).items()
        if counts.needs_person
    }


def record_compare_answers(
    session: Session,
    *,
    revision_id: UUID,
    key: str,
    answer: str,
    actor: str,
    expected_keys: Sequence[str] | None = None,
) -> int:
    """Record a person's answer on one compared dimension (`arch:<id>`), or "Clear all as not
    compared" on every open unpaired or unread one (`NOT_COMPARED_GROUP`, `expected_keys`
    required: the group as shown). `answer` is the name the screen sends (`SENT_AS`). Append-only:
    a new row per answer, never an edit of the record. Returns how many rows were written (0: the
    same answer was already the newest). Raises `CompareRefused`."""
    stored = SENT_AS.get(answer)
    if stored is None:
        raise CompareRefused(422, f"A compared dimension does not take '{answer}'.")
    items, _counts = architect_compare_for_revision(session, revision_id)
    if key == NOT_COMPARED_GROUP:
        if stored != "not_compared":
            raise CompareRefused(422, "This group takes 'Clear all as not compared' only.")
        group = [i for i in items if i.result in ("unpaired", "unread") and i.needs_person]
        if expected_keys is None or sorted(expected_keys) != sorted(i.key for i in group):
            raise CompareRefused(
                409, "The numbers with no match changed since they were shown: reload."
            )
        chosen = group
    else:
        item = next((i for i in items if i.key == key), None)
        if item is None:
            raise CompareRefused(404, "No such compared dimension on this revision.")
        if item.answer == stored:
            return 0
        if stored not in ANSWERS_FOR[item.result]:
            raise CompareRefused(422, f"A '{item.result}' dimension does not take '{answer}'.")
        chosen = [item]
    for item in chosen:
        session.add(
            ArchitectCompareAnswer(
                record_id=UUID(item.key.removeprefix(KEY_PREFIX)),
                package_revision_id=revision_id,
                answer=stored,
                note=ANSWER_NOTES[stored],
                answered_by=actor,
            )
        )
    session.flush()
    return len(chosen)
