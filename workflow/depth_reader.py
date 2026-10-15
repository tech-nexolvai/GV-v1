"""The depth and sink reader in the worker: read each countertop's plan and section views (#1290).

**When.** Once per `DatabaseStages.match`, right after Bala files the countertops, and only when
`GV_DEPTH_READER` is on (off by default). Off, nothing here runs and nothing is written, so every
finding is exactly as before.

**What it reads.** The pages that hold one of Bala's countertops (a suggestion of the newest
slot-reader run that nobody withdrew, or the suggestion behind a live countertop item). Each page is
prepared by code at no cost (`extraction.depth.read.prepare_pdf_page`), then read by the decided
Claude pair and code, value by value (`extraction.depth.read.read_depth`, #1265).

**The calls.** Every question goes through the shared route and is recorded in `model_invocations`
through the shared `CallLedger`, under this reader's own extraction run. A stored answer to the
identical question is reused, never paid for twice (`workflow/reader_reuse.py`). The reader has its
**own** `ReadingStop`, so a depth question refused or not sent never marks a page's own reading as
unread; and `stop.question_kind` names it `depth`, never `dimension`.

**What it writes**, all append-only:

* one `item_view_links` row for each plan or section view whose kind both AI readers and code agree
  on, when the vendor drawing holding the view holds exactly one countertop;
* one `observation_candidates` row for each value read on a view (semantic guess CT010, CT012,
  CT008 or CT007), with flags `depth-reader`, `view-kind:*`, `item:*`, `link:*`, `confirmed-by:*`,
  and the reader's plain reason (`review_reason`). **Lane `SECOND_READER` only when both judgments
  agree AND code confirmed the number by the exact printed text or a chain sum** (`state:qualified`).
  A value confirmed by its drawn length alone is written for a person to confirm
  (`state:confirm-only`, no lane), and a value code held is written with no number
  (`state:held`). A view on a drawing with two countertops, or not inside one drawing, ties to none:
  its rows say `unplaced-among:` each countertop there, so the hold shows on each;
* one `state:view-note` row for a view that read no value, with why, so the countertop's check can
  say why nothing was read.

Nothing here decides a check: `workflow/depth_evidence.py` turns these rows into one countertop's
operands, and the gate and the engine decide.

Source: issue #1290; phase 5 readings design §7 PR 4, §8, §9. Verification:
`tests/workflow/test_depth_reader.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.evidence.sides import page_transform_at
from app.models import (
    DrawingItem,
    DrawingView,
    ItemViewLink,
    PartConfirmation,
    PartDecision,
    PartProposal,
    PartProposalRow,
)
from app.models.document import Page
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.canonical import CorroborationLane, EvidenceStatus
from evidence.coordinates import ImagePoint, StoredPoint
from extraction.depth.read import (
    Answers,
    Asked,
    DepthReaderSettings,
    DepthReading,
    NoAnswer,
    PreparedPage,
    PreparedView,
    ValueReading,
    ViewReading,
)
from extraction.depth.view_kind import ViewKind
from extraction.form_reader.bedrock import AttemptUsage
from extraction.slot_reader.bedrock import CropJob, read_crops_parallel
from extraction.slot_reader.stop import ReadingStop
from vocabulary.part_kinds import PartKind
from workflow.bala_items import BALA_SOURCE
from workflow.depth_evidence import (
    CONFIRM_ONLY,
    DEPTH_READER_FLAG,
    FIELD_PREFIX,
    HELD,
    ITEM_PREFIX,
    LINK_PREFIX,
    QUALIFIED,
    QUESTION_PREFIX,
    SUPPORT_FLAG,
    UNPLACED_PREFIX,
    VIEW_KIND_PREFIX,
    VIEW_NOTE,
)
from workflow.parts import outline_box
from workflow.reader_reuse import StoredAnswerSource, reuse_stored_answers
from workflow.slot_row_scope import latest_slot_reader_run

__all__ = [
    "LINKED_KINDS",
    "QUALIFYING_CHECKS",
    "DepthPlacement",
    "countertops_by_drawing",
    "depth_asker",
    "persist_depth_reading",
    "placement_of",
]

#: Only these code checks qualify a value for a check (#1290): the printed text read exactly by
#: code, or a chain of the readers' numbers that sums to it exactly. Drawn length alone does not.
QUALIFYING_CHECKS: Final = frozenset({"vector-text", "chain"})
#: The kinds a link is written for: the views values are read on.
LINKED_KINDS: Final = {ViewKind.PLAN: "plan", ViewKind.SECTION: "section"}
#: The longest reason kept on a row (`observation_candidates.review_reason`).
_REASON_LIMIT: Final = 300


def countertops_by_drawing(session: Session, revision_id: UUID) -> dict[UUID, tuple[UUID, ...]]:
    """Each drawing view's countertops, by Bala's countertop suggestion.

    A suggestion counts when it is Bala's for the newest slot-reader run and nobody withdrew it, or
    when it is behind a live countertop item (an older reading's, after a re-read). Each counts once.
    """
    later = aliased(PartConfirmation)
    current = (
        select(PartConfirmation.decision)
        .where(
            PartConfirmation.part_proposal_id == PartProposal.id,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
        .order_by(PartConfirmation.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    held: dict[UUID, set[UUID]] = {}
    newest = latest_slot_reader_run(session, revision_id)
    if newest is not None:
        for proposal_id, view_id, decision in session.execute(
            select(PartProposal.id, PartProposal.drawing_view_id, current)
            .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
            .where(
                PartProposalRow.extraction_run_id == newest,
                PartProposalRow.slot_index.is_(None),
                PartProposal.kind == PartKind.COUNTERTOP.value,
                PartProposal.source == BALA_SOURCE,
            )
        ):
            if decision != PartDecision.WITHDRAWN.value:
                held.setdefault(view_id, set()).add(proposal_id)
    from workflow.item_plan import live_countertops

    live = live_countertops(session, revision_id, follow_rows=False)
    if live:
        rows = session.execute(
            select(PartConfirmation.part_proposal_id, DrawingItem.drawing_view_id)
            .join(DrawingItem, DrawingItem.id == PartConfirmation.drawing_item_id)
            .where(
                PartConfirmation.drawing_item_id.in_([item.item_id for item in live]),
                PartConfirmation.decision == PartDecision.CONFIRMED.value,
                ~exists().where(later.supersedes_id == PartConfirmation.id),
            )
        ).all()
        for proposal_id, view_id in rows:
            held.setdefault(view_id, set()).add(proposal_id)
    return {view: tuple(sorted(found, key=str)) for view, found in held.items()}


@dataclass(frozen=True, slots=True)
class DepthPlacement:
    """Which countertop a view shows, or among which it could not be placed, and why."""

    proposal_id: UUID | None
    among: tuple[UUID, ...] = ()
    reason: str = ""


#: How much of a view must lie in one drawing for it to be that drawing's (#1290). A view's own
#: dimensions and caption can run a little past the pasted drawing's recorded box (measured on AI
#: Set 3: up to a hundredth of the page), so whole containment would place none; half is still one.
VIEW_IN_DRAWING: Final = Decimal("0.5")


def drawings_holding(
    drawings: Sequence[tuple[UUID, tuple[Decimal, Decimal, Decimal, Decimal]]],
    view: Sequence[StoredPoint],
) -> list[UUID] | None:
    """The drawings the view lies in: its centre inside, and at least half of it overlapping.

    `None` when the page records no drawing at all (the page is the drawing). The caller places the
    view only when exactly one drawing holds it.
    """
    if not drawings:
        return None
    left, right = min(p.x for p in view), max(p.x for p in view)
    top, bottom = min(p.y for p in view), max(p.y for p in view)
    area = (right - left) * (bottom - top)
    middle_x, middle_y = (left + right) / 2, (top + bottom) / 2
    found: list[UUID] = []
    for drawing_id, (x0, y0, x1, y1) in drawings:
        if not (x0 <= middle_x <= x1 and y0 <= middle_y <= y1) or area <= 0:
            continue
        overlap = max(Decimal(0), min(right, x1) - max(left, x0)) * max(
            Decimal(0), min(bottom, y1) - max(top, y0)
        )
        if overlap / area >= VIEW_IN_DRAWING:
            found.append(drawing_id)
    return found


def placement_of(
    holding: Sequence[UUID] | None,
    countertops: Mapping[UUID, tuple[UUID, ...]],
    on_page: Sequence[UUID],
) -> DepthPlacement:
    """The view's countertop: the drawing holding it must be one, holding exactly one countertop.

    `holding` is the drawing views whose region holds the view (`None`: the page has none);
    `on_page` every countertop on the page, which share the hold when the view cannot be placed.
    """
    if holding is None or len(holding) != 1:
        return DepthPlacement(
            None,
            tuple(on_page),
            "the view does not lie inside exactly one vendor drawing, so which countertop it "
            "shows is not known.",
        )
    found = countertops.get(holding[0], ())
    if len(found) == 1:
        return DepthPlacement(found[0])
    if not found:
        return DepthPlacement(None, (), "the drawing holding the view has no countertop.")
    return DepthPlacement(
        None,
        tuple(found),
        f"the drawing holding the view holds {len(found)} countertops, so which one the view "
        "shows is not known.",
    )


def depth_asker(
    *,
    clients: object,
    rates: object,
    calls_per_minute: Mapping[str, int],
    max_concurrent_calls: int,
    max_throttle_retries: int,
    retry_backoff_seconds: float,
    settings: DepthReaderSettings,
    record_attempt: Callable[[AttemptUsage], None],
    stored: StoredAnswerSource | None,
    stop: ReadingStop,
    spend_cap_usd: Decimal | None,
) -> Callable[[Sequence[Asked]], Answers]:
    """The reader's `ask`: a stored answer to the identical question first, then the shared route.

    Every attempt, reused or paid, goes to `record_attempt` (the shared `CallLedger`). `stop` is the
    depth reader's own: once tripped, nothing more is sent, and the questions not answered are
    `NoAnswer` with the reason, which holds their values.
    """

    def ask(questions: Sequence[Asked]) -> Answers:
        jobs = [
            CropJob(
                asked.key,
                asked.model_id,
                asked.page_index,
                asked.question.pictures[0],
                question_packet=asked.packet,
                depth=asked.question,
            )
            for asked in questions
        ]
        answers: dict[tuple[str, str], object] = {}
        remaining: list[CropJob] = jobs
        if stored is not None:
            reused, remaining = reuse_stored_answers(
                jobs,
                stored=stored,
                record_attempt=record_attempt,
                max_tokens=settings.max_tokens,
                claude_effort=settings.effort,
            )
            answers.update(reused)
        if remaining:
            got = read_crops_parallel(
                remaining,
                clients=clients,  # type: ignore[arg-type]
                rates=rates,  # type: ignore[arg-type]
                calls_per_minute=calls_per_minute,
                max_concurrent_calls=max_concurrent_calls,
                max_tokens=settings.max_tokens,
                max_throttle_retries=max_throttle_retries,
                retry_backoff_seconds=retry_backoff_seconds,
                record_attempt=record_attempt,
                spend_cap_usd=spend_cap_usd,
                claude_effort=settings.effort,
                stop=stop,
            )
            for job in remaining:
                answer = got.get((job.key, job.model_id))
                answers[(job.key, job.model_id)] = (
                    answer
                    if answer is not None
                    else NoAnswer(
                        "no accepted answer: "
                        + (
                            f"the depth reading stopped ({stop.cause}) before or while asking"
                            if stop.stopped
                            else "the reader abstained (malformed twice, or the call failed)"
                        )
                    )
                )
        return answers  # type: ignore[return-value]

    return ask


@dataclass
class PersistReport:
    links: int = 0
    links_reused: int = 0
    qualified: int = 0
    confirm_only: int = 0
    held: int = 0
    view_notes: int = 0
    unplaced_views: int = 0
    notes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {
            "links_written": self.links,
            "links_found_again": self.links_reused,
            "values_qualified": self.qualified,
            "values_to_confirm": self.confirm_only,
            "values_held": self.held,
            "view_notes": self.view_notes,
            "views_unplaced": self.unplaced_views,
            "notes": list(self.notes),
        }


def _reason(text: str) -> str:
    text = " ".join(text.split()) or "no reason was given"
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 1] + "…"


def _box_polygon(box: tuple[int, int, int, int]) -> list[list[int]]:
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _value_state(value: ValueReading) -> str:
    if not value.usable or value.inches is None:
        return HELD
    if value.confirmed_by in QUALIFYING_CHECKS:
        return QUALIFIED
    return CONFIRM_ONLY


def persist_depth_reading(
    session: Session,
    revision_id: UUID,
    run: ExtractionRun,
    reading: DepthReading,
    prepared: Sequence[PreparedPage],
    pages: Mapping[tuple[str, int], Page],
    *,
    dpi: int,
) -> PersistReport:
    """Write the links and the readings of one depth reading (see the module's docstring)."""
    report = PersistReport()
    countertops = countertops_by_drawing(session, revision_id)
    page_ids = {page.id for page in pages.values()}
    drawings_on: dict[UUID, list[tuple[UUID, tuple[Decimal, Decimal, Decimal, Decimal]]]] = {}
    view_pages: dict[UUID, UUID] = {}
    for drawing in session.scalars(select(DrawingView).where(DrawingView.page_id.in_(page_ids))):
        view_pages[drawing.id] = drawing.page_id
        try:
            drawings_on.setdefault(drawing.page_id, []).append(
                (drawing.id, outline_box(drawing.region))
            )
        except (TypeError, ValueError):
            continue
    views: dict[tuple[str, int, int, int], tuple[PreparedPage, PreparedView]] = {
        (page.page_sha256, page.page_index, view.panel, view.view.number): (page, view)
        for page in prepared
        for view in page.views
    }
    for found in reading.views:
        page_view = views.get((found.page_sha256, found.page_index, found.panel, found.view_number))
        if page_view is None:
            report.notes.append(f"p{found.page_index + 1}: a reading names no prepared view")
            continue
        prepared_page, prepared_view = page_view
        page = pages.get((prepared_page.document_version_id, prepared_page.page_index))
        transform = None if page is None else page_transform_at(page, dpi)
        if page is None or transform is None:
            report.notes.append(f"p{found.page_index + 1}: the page cannot be placed")
            continue
        extent_px = prepared_page.pixels(prepared_view.view.extent)
        polygon_px = _box_polygon(extent_px)
        stored = [transform.to_stored(ImagePoint(x, y)) for x, y in polygon_px]
        region: dict[str, object] = {
            "space": "stored",
            "points": [[str(p.x), str(p.y)] for p in stored],
        }
        holding = drawings_holding(drawings_on.get(page.id, ()), stored)
        on_page = sorted(
            {
                proposal
                for view_id, proposals in countertops.items()
                if view_pages.get(view_id) == page.id
                for proposal in proposals
            },
            key=str,
        )
        placement = placement_of(holding, countertops, on_page)
        kind_word = LINKED_KINDS.get(found.kind) if found.kind is not None else None
        place_flags: list[str] = []
        unplaced_reason = placement.reason
        if placement.proposal_id is not None:
            place_flags.append(f"{ITEM_PREFIX}{placement.proposal_id}")
            if kind_word is not None:
                link, reused, why = _link(
                    session, placement.proposal_id, page, region, kind_word, found, run
                )
                if link is None:
                    place_flags = [f"{UNPLACED_PREFIX}{placement.proposal_id}"]
                    unplaced_reason = why
                else:
                    place_flags.append(f"{LINK_PREFIX}{link.id}")
                    report.links += not reused
                    report.links_reused += reused
        else:
            place_flags.extend(f"{UNPLACED_PREFIX}{proposal}" for proposal in placement.among)
        if any(flag.startswith(UNPLACED_PREFIX) for flag in place_flags):
            report.unplaced_views += 1
        view_flags = [
            DEPTH_READER_FLAG,
            f"view:{prepared_view.key}",
            f"{VIEW_KIND_PREFIX}{found.kind.value if found.kind is not None else 'unsettled'}",
            *place_flags,
        ]
        if not found.values:
            note = found.values_reason or found.kind_reason
            session.add(
                ObservationCandidate(
                    document_version_id=page.document_version_id,
                    page_id=page.id,
                    extraction_run_id=run.id,
                    raw_text="",
                    polygon=polygon_px,
                    coordinate_space="image",
                    ambiguity_flags=[*view_flags, VIEW_NOTE],
                    review_reason=_reason(
                        f"on the view headed {found.heading!r}: {note}" if found.heading else note
                    ),
                )
            )
            report.view_notes += 1
            continue
        for value in found.values:
            state = _value_state(value)
            if state == QUALIFIED and any(f.startswith(UNPLACED_PREFIX) for f in place_flags):
                # Never a lane on a reading no countertop can take: it stays a person's question.
                state = CONFIRM_ONLY
            box = (
                prepared_page.pixels(
                    prepared_view.label_areas.get(value.span_number, prepared_view.view.extent)
                )
                if value.span_number is not None
                else extent_px
            )
            flags = [
                *view_flags,
                state,
                f"{FIELD_PREFIX}{value.field}",
                *((f"span:{value.span_number}",) if value.span_number is not None else ()),
                *(
                    (f"{QUESTION_PREFIX}{prepared_view.key}:n{value.span_number}",)
                    if value.span_number is not None
                    else ()
                ),
                *((f"confirmed-by:{value.confirmed_by}",) if value.confirmed_by else ()),
                *((f"scale-source:{value.scale_source}",) if value.scale_source else ()),
            ]
            valued = state in (QUALIFIED, CONFIRM_ONLY) and value.inches is not None
            reason = (
                unplaced_reason
                if any(f.startswith(UNPLACED_PREFIX) for f in place_flags)
                else value.reason
            )
            readers = [says for says in value.readers if says.says == "value" and says.text]
            if state == QUALIFIED and len({says.model_id for says in readers}) != 2:
                # Never a lane without both readers' own rows to stand on.
                state = CONFIRM_ONLY
                flags[flags.index(QUALIFIED)] = CONFIRM_ONLY
            parent = ObservationCandidate(
                document_version_id=page.document_version_id,
                page_id=page.id,
                extraction_run_id=run.id,
                raw_text=value.text or "",
                value_numerator=value.inches.numerator if valued and value.inches else None,
                value_denominator=(value.inches.denominator if valued and value.inches else None),
                unit="in" if valued else None,
                unit_guess="in" if valued else None,
                semantic_guess=value.field,
                polygon=_box_polygon(box),
                coordinate_space="image",
                ambiguity_flags=flags,
                review_reason=_reason(reason),
                corroboration_status=(
                    EvidenceStatus.CORROBORATED.value if state == QUALIFIED else None
                ),
                corroboration_lane=(
                    CorroborationLane.SECOND_READER.value if state == QUALIFIED else None
                ),
            )
            session.add(parent)
            if state == QUALIFIED:
                session.flush()
                # Each reader's own row: what the qualified observation stands on (two candidates).
                session.add_all(
                    ObservationCandidate(
                        document_version_id=page.document_version_id,
                        page_id=page.id,
                        extraction_run_id=run.id,
                        raw_text=says.text,
                        value_numerator=parent.value_numerator,
                        value_denominator=parent.value_denominator,
                        unit=parent.unit,
                        polygon=parent.polygon,
                        coordinate_space="image",
                        ambiguity_flags=[
                            SUPPORT_FLAG,
                            f"supports:{parent.id}",
                            f"reader-id:{says.model_id}",
                        ],
                    )
                    for says in readers
                )
            report.qualified += state == QUALIFIED
            report.confirm_only += state == CONFIRM_ONLY
            report.held += state == HELD
    session.flush()
    return report


def _link(
    session: Session,
    proposal_id: UUID,
    page: Page,
    region: dict[str, object],
    kind: str,
    found: ViewReading,
    run: ExtractionRun,
) -> tuple[ItemViewLink | None, bool, str]:
    """The view's link to its countertop: found again when it is already there, else written.

    An earlier link of the same countertop, page and region that names another kind is not
    overwritten (append-only, unique): the view is then tied to no countertop, with why.
    """
    existing = [
        link
        for link in session.scalars(
            select(ItemViewLink).where(
                ItemViewLink.part_proposal_id == proposal_id, ItemViewLink.page_id == page.id
            )
        )
        if link.region == region
    ]
    if existing:
        link = existing[0]
        if link.view_kind != kind:
            return (
                None,
                False,
                (
                    f"the view was tied to this countertop earlier as a {link.view_kind} view, "
                    f"and is read as a {kind} view now: a person must say which."
                ),
            )
        return link, True, ""
    heading = (found.heading or "").strip()[:300] or None
    link = ItemViewLink(
        part_proposal_id=proposal_id,
        page_id=page.id,
        region=region,
        view_kind=kind,
        heading_text=heading,
        judged_by="code+ais",
        extraction_run_id=run.id,
    )
    session.add(link)
    session.flush()
    return link, False, ""
