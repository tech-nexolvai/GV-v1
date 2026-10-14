"""Bala: suggest each countertop the readers found, and its pieces, for a person to confirm (#1236).

**What it does.** The slot reader reads one countertop row per vendor page: the line of dimensions
under a countertop, piece by piece, and the countertop's overall length when one is printed. Until
now that row was the only "per countertop" thing the system knew, and no part was ever suggested
from it. Bala reads the newest slot-reader run of a revision (`slot_rows_and_unchosen_pages`, read
only) and, for each row, files:

* one countertop suggestion (`PartProposal`, kind `countertop`), and
* one suggestion per piece of the row, a cabinet unless printed words name its kind (the reader's
  `kind:` flag: a printed `FILLER`, a cabinet tag),

each linked to its row and piece in `part_proposal_rows`, with `source = "bala.slot-row"`. A person
confirms each with one click on the Measure page, through the existing `confirm_part` route.

**Suggestions only: no item.** Nothing here writes `drawing_items`; only a person's confirmation
does (`workflow/parts.py:confirm_part`, guarded by `tests/db/test_drawing_models.py`). Whether two
judgments may ever make an item without a click is PR 2's question (decided 2026-10-14), and this
module only *reports* what would qualify (`WouldBeAutomatic`), so the $0 dry run can measure it.

**The countertop's outline is its own, never the union of its pieces** (`app/models/drawing.py`,
`PartProposal.extent`): the box code drew round the row's printed overall length (`slot:overall`'s
`slot-box:`). Where no overall length is printed, the box code drew round the chosen row's own line,
end tick to end tick, and the reason says so, because then "the pieces reach both ends" is true by
construction and proves nothing.

**Every row gets its suggestions, with its reason.** A held row (field cut, counter break, the
readers' doubt), a row code chose without the readers, a row on a drawing nobody confirmed as the
vendor's: each is still suggested, and its reason says why a person should look. A row that cannot
be outlined (no box, no transform, no drawing holding it) is not suggested, and the result names it
and why (never silent). A page whose row the readers did not agree on has no row to outline at all:
it is listed with the readers' reason and stays the reviewer's question on the results.

**Idempotent, append-only.** A row and piece already linked is found again, not filed twice. A
re-read is a new slot-reader run with new anchor readings, so it files new suggestions and links;
the older run's undecided suggestions stop being listed (`app/evidence/parts.py`).

**Code's own part finder as a second judgment** (decision 3, 2026-10-14). `code_parts` runs code's
geometry-only finder on a page in memory (`DatabaseStages._code_parts_on`, $0) and writes nothing.
A piece it finds a cabinet of the same span for counts as judgment 2 in `WouldBeAutomatic`.

**No extraction imports**, so the read-only dry run (`scripts/bala_dry_run.py`) can use the plan.

Source: issue #1236; phase 4 design (§2, §5 PR 1); Anant's decisions of 2026-10-14. Verification:
tests/workflow/test_bala_items.py, tests/db/test_drawing_models.py.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final, cast
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    DrawingView,
    ObservationCandidate,
    Page,
    PartConfirmation,
    PartProposal,
    PartProposalRow,
)
from app.models.runs import ExtractionRun
from app.review.row_location import slot_location
from vocabulary.part_kinds import PartKind
from workflow.parts import outline_box, record_part_proposal
from workflow.slot_row_scope import (
    SlotRow,
    UnchosenRowPage,
    candidate_is_sealed,
    latest_slot_reader_run,
    slot_rows_and_unchosen_pages,
)

__all__ = [
    "BALA_SOURCE",
    "BALA_VERSION",
    "FROM_THE_COUNTERTOP_LINE",
    "BalaOutcome",
    "CodeFinding",
    "CodePart",
    "CodeParts",
    "PlannedPart",
    "PlannedRow",
    "WouldBeAutomatic",
    "plan_items",
    "propose_items",
]

#: What filed the suggestion, on `part_proposals.source`, so the Measure page can say so.
BALA_SOURCE: Final = "bala.slot-row"
BALA_VERSION: Final = "1"

#: How every one of Bala's reasons begins: the words the Measure page shows (#1236).
FROM_THE_COUNTERTOP_LINE: Final = "Suggested by code from the countertop line"

#: `part_proposals.reason` holds at most this many characters.
_REASON_LIMIT: Final = 500

#: Printed kinds (`extraction/slot_reader/kinds.py:PieceKind`) that name a part kind outright.
_NAMED_KINDS: Final[Mapping[str, PartKind]] = {
    "cabinet": PartKind.CABINET,
    "filler": PartKind.FILLER,
}

#: Printed kinds that are evidence but not one of the three part kinds: suggested as a cabinet, with
#: the words saying what the drawing prints, for a person to decide.
_OTHER_KINDS: Final[Mapping[str, str]] = {
    "appliance_space": "the drawing names it an appliance space, which is not a cabinet or a "
    "filler; say it is not a part if so",
    "equal_cabinets": "the drawing prints it as equal cabinets, so it may be more than one",
}


@dataclass(frozen=True, slots=True)
class CodePart:
    """One part code's geometry-only finder suggests on a page: which drawing, what kind, and its
    left and right ends in stored page space."""

    view_id: UUID
    kind: PartKind
    left: Decimal
    right: Decimal


@dataclass(frozen=True, slots=True)
class CodeFinding:
    """What code's part finder found on one page, and the tolerance its ends are compared within
    (its own witness tolerance, in stored units)."""

    parts: tuple[CodePart, ...]
    tolerance: Decimal


#: Runs code's part finder on one page in memory: what it found, or why it did not run.
type CodeParts = Callable[[Page], CodeFinding | str]


@dataclass(frozen=True, slots=True)
class _Box:
    left: Decimal
    top: Decimal
    right: Decimal
    bottom: Decimal

    @property
    def middle(self) -> Decimal:
        return (self.top + self.bottom) / 2

    def extent(self) -> list[tuple[Decimal, Decimal]]:
        return [
            (self.left, self.top),
            (self.right, self.top),
            (self.right, self.bottom),
            (self.left, self.bottom),
        ]

    def line(self) -> tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]:
        return (self.left, self.middle), (self.right, self.middle)


@dataclass(frozen=True, slots=True)
class PlannedPart:
    """One suggestion Bala would file: the countertop (`slot_index` `None`) or one piece."""

    slot_index: int | None
    kind: PartKind
    kind_named: bool
    """Whether printed evidence named the kind; a countertop is named by the row itself."""
    box: _Box
    reason: str


@dataclass(frozen=True, slots=True)
class WouldBeAutomatic:
    """What PR 2 would make without a click, if both judgments agree (reported, never acted on).

    The countertop: judgment 1 is both row readers naming this row, read in full, with no row hold;
    judgment 2 is code placing the row wholly inside exactly one drawing confirmed as the vendor's,
    its piece boxes running end to end, as many as the row says. A piece: its countertop qualifies,
    its own reading is sealed, printed words name its kind, and code's own part finder finds a
    cabinet with the same ends.
    """

    countertop: bool
    countertop_why_not: tuple[str, ...]
    pieces: tuple[bool, ...]
    pieces_why_not: tuple[tuple[str, ...], ...]
    pieces_code_agrees: tuple[bool | None, ...]
    """Per piece: whether code's part finder found a cabinet with the same ends; `None` where it
    did not run."""


@dataclass(frozen=True, slots=True)
class PlannedRow:
    """One countertop row as Bala reads it, and the suggestions it would file."""

    page_number: int
    label: str
    row_candidate_id: UUID
    extraction_run_id: UUID
    view_id: UUID | None
    parts: tuple[PlannedPart, ...]
    not_proposed: str | None
    """Why nothing is suggested for this row, or `None` when its suggestions are filed."""
    states: tuple[str, ...]
    """Why a person should look at this row (held, chosen by code), in plain words."""
    automatic: WouldBeAutomatic
    code_finder: str | None
    """Why code's part finder gave no second judgment on this page, or `None` when it ran."""


@dataclass(frozen=True, slots=True)
class BalaPlan:
    run_id: UUID | None
    rows: tuple[PlannedRow, ...]
    unchosen: tuple[UnchosenRowPage, ...]


@dataclass
class BalaOutcome:
    """What `propose_items` filed, and what it left out and why."""

    plan: BalaPlan
    written: int = 0
    found: int = 0
    already_decided: int = 0
    """Re-read parts not suggested again: a person's decision stands on the same outline."""
    proposals: dict[tuple[UUID, int | None], UUID] = field(default_factory=dict)

    def as_payload(self) -> dict[str, object]:
        rows = self.plan.rows
        filed = [row for row in rows if row.not_proposed is None]
        return {
            "ran": True,
            "slot_reader_run": None if self.plan.run_id is None else str(self.plan.run_id),
            "rows": len(rows),
            "countertops": sum(1 for row in filed for part in row.parts if part.slot_index is None),
            "pieces": sum(1 for row in filed for part in row.parts if part.slot_index is not None),
            "written": self.written,
            "found_again": self.found,
            "already_decided": self.already_decided,
            "items_written": 0,
            "not_proposed": [
                f"{row.label}: {row.not_proposed}" for row in rows if row.not_proposed is not None
            ],
            "held_or_code_chosen": [
                f"{row.label}: {'; '.join(row.states)}" for row in filed if row.states
            ],
            "pages_without_a_row": [
                f"{page.label}: {page.reason}" for page in self.plan.unchosen if page.split
            ],
            "would_be_automatic": {
                "countertops": sum(1 for row in filed if row.automatic.countertop),
                "pieces": sum(sum(row.automatic.pieces) for row in filed),
            },
            "code_part_finder": sorted(
                {row.code_finder for row in rows if row.code_finder is not None}
            ),
        }


def plan_items(
    session: Session, revision_id: UUID, *, code_parts: CodeParts | None = None
) -> BalaPlan:
    """What Bala would suggest for the revision's newest slot-reader run. **Reads only.**"""
    run_id = latest_slot_reader_run(session, revision_id)
    if run_id is None:
        return BalaPlan(run_id=None, rows=(), unchosen=())
    rows, unchosen = slot_rows_and_unchosen_pages(session, revision_id, run_id=run_id)
    run = session.get(ExtractionRun, run_id)
    if run is None:
        return BalaPlan(run_id=run_id, rows=(), unchosen=unchosen)
    found: dict[UUID, CodeFinding | str] = {}
    planned: list[PlannedRow] = []
    for row in rows:
        page = session.get(Page, row.anchor.page_id)
        if page is None:
            continue
        if page.id not in found:
            found[page.id] = (
                "code's part finder was not asked" if code_parts is None else code_parts(page)
            )
        planned.append(_plan_row(session, row, page, run, found[page.id]))
    return BalaPlan(run_id=run_id, rows=tuple(planned), unchosen=unchosen)


def propose_items(
    session: Session,
    revision_id: UUID,
    *,
    code_parts: CodeParts | None = None,
    same_outline_tolerance: Decimal = Decimal(0),
) -> BalaOutcome:
    """File Bala's suggestions and their links for the revision's newest slot-reader run.

    **Writes `part_proposals` and `part_proposal_rows` only**, never an item, a decision or a run.
    A row and piece already linked is found again rather than filed twice.

    **A part a person already decided is not suggested again** (review of #1243). Where an earlier
    reading's Bala suggestion on the same drawing, of the same kind of thing (countertop or piece),
    has the same outline within `same_outline_tolerance` (stored units; the match stage passes the
    part finder's witness tolerance) and a person's decision stands on it, the re-read piece is that
    part: nothing is filed, and `already_decided` counts it. Asking twice would let one cabinet be
    confirmed twice, as two live items.
    """
    plan = plan_items(session, revision_id, code_parts=code_parts)
    outcome = BalaOutcome(plan=plan)
    decided = _decided_outlines(session, plan)
    for row in plan.rows:
        if row.not_proposed is not None or row.view_id is None:
            continue
        linked = {
            link.slot_index: link.part_proposal_id
            for link in session.scalars(
                select(PartProposalRow).where(
                    PartProposalRow.row_candidate_id == row.row_candidate_id
                )
            )
        }
        for part in row.parts:
            existing = linked.get(part.slot_index)
            if existing is not None:
                outcome.found += 1
                outcome.proposals[(row.row_candidate_id, part.slot_index)] = existing
                continue
            if any(
                countertop == (part.slot_index is None)
                and _same_outline(box, part.box, same_outline_tolerance)
                for countertop, box in decided.get(row.view_id, ())
            ):
                outcome.already_decided += 1
                continue
            proposal = record_part_proposal(
                session,
                drawing_view_id=row.view_id,
                kind=part.kind,
                extent=part.box.extent(),
                defining_line=part.box.line(),
                code_as_printed=None,
                code_candidate_id=None,
                reason=part.reason,
                source=BALA_SOURCE,
                source_version=BALA_VERSION,
                # A suggestion of its own for each row and piece, even where an earlier reading
                # suggested the same outline: the link is one suggestion to one row and piece.
                find_existing=False,
            )
            session.add(
                PartProposalRow(
                    part_proposal_id=proposal.id,
                    row_candidate_id=row.row_candidate_id,
                    slot_index=part.slot_index,
                    extraction_run_id=row.extraction_run_id,
                    kind_named=part.kind_named,
                )
            )
            outcome.written += 1
            outcome.proposals[(row.row_candidate_id, part.slot_index)] = proposal.id
    session.flush()
    return outcome


def _decided_outlines(session: Session, plan: BalaPlan) -> dict[UUID, list[tuple[bool, _Box]]]:
    """By drawing: the outline of every Bala suggestion from another reading that a person's
    current decision stands on, and whether it is a countertop."""
    views = {row.view_id for row in plan.rows if row.view_id is not None}
    if not views or plan.run_id is None:
        return {}
    later = aliased(PartConfirmation)
    rows = session.execute(
        select(PartProposal, PartProposalRow.slot_index)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .join(PartConfirmation, PartConfirmation.part_proposal_id == PartProposal.id)
        .where(
            PartProposal.drawing_view_id.in_(views),
            PartProposalRow.extraction_run_id != plan.run_id,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
    ).all()
    decided: dict[UUID, list[tuple[bool, _Box]]] = {}
    for proposal, slot_index in rows:
        left, top, right, bottom = outline_box(proposal.extent)
        decided.setdefault(proposal.drawing_view_id, []).append(
            (slot_index is None, _Box(left=left, top=top, right=right, bottom=bottom))
        )
    return decided


def _same_outline(first: _Box, second: _Box, tolerance: Decimal) -> bool:
    return (
        abs(first.left - second.left) <= tolerance
        and abs(first.right - second.right) <= tolerance
        and abs(first.top - second.top) <= tolerance
        and abs(first.bottom - second.bottom) <= tolerance
    )


# ---------------------------------------------------------------------------
# One row
# ---------------------------------------------------------------------------


def _plan_row(
    session: Session,
    row: SlotRow,
    page: Page,
    run: ExtractionRun,
    code: CodeFinding | str,
) -> PlannedRow:
    by_slot = _slot_candidates(row)
    states = _states(row)
    code_finder = code if isinstance(code, str) else None

    def unplanned(why: str) -> PlannedRow:
        return PlannedRow(
            page_number=row.page_number,
            label=row.label,
            row_candidate_id=row.anchor.id,
            extraction_run_id=run.id,
            view_id=None,
            parts=(),
            not_proposed=why,
            states=states,
            automatic=WouldBeAutomatic(False, (why,), (), (), ()),
            code_finder=code_finder,
        )

    pieces: list[tuple[int, ObservationCandidate, _Box | None]] = []
    for index in range(row.piece_count):
        candidate = by_slot.get(str(index))
        if candidate is None:
            return unplanned(f"piece {index + 1} of {row.piece_count} has no reading on record")
        pieces.append((index, candidate, _stored_box(candidate, page, run)))
    if not pieces:
        return unplanned("the row has no pieces")
    overall = by_slot.get("overall")
    overall_box = None if overall is None else _stored_box(overall, page, run)
    placed = [box for _, _, box in pieces if box is not None]
    if overall_box is not None:
        countertop_box = overall_box
        outline = "outlined by its printed overall length"
    elif len(placed) == len(pieces):
        countertop_box = _Box(
            left=min(box.left for box in placed),
            top=min(box.top for box in placed),
            right=max(box.right for box in placed),
            bottom=max(box.bottom for box in placed),
        )
        outline = (
            "outlined by the row's own line, end to end, because no overall length is printed "
            "for it"
        )
    else:
        return unplanned("its boxes could not be placed on the page")

    views = _views_holding(session, page, countertop_box)
    if not views:
        return unplanned(f"no drawing on page {row.page_number} holds the whole row")
    if len(views) > 1:
        return unplanned(f"{len(views)} drawings on page {row.page_number} hold the row")
    view = views[0]

    state_words = "" if not states else " " + " ".join(f"{state}." for state in states)
    parts: list[PlannedPart] = [
        PlannedPart(
            slot_index=None,
            kind=PartKind.COUNTERTOP,
            kind_named=True,
            box=countertop_box,
            reason=_reason(
                f"{FROM_THE_COUNTERTOP_LINE}: the countertop of this row, {outline}.{state_words}"
            ),
        )
    ]
    unplaced: list[int] = []
    for index, candidate, box in pieces:
        if box is None:
            unplaced.append(index)
            continue
        kind, named, why = _kind(candidate)
        parts.append(
            PlannedPart(
                slot_index=index,
                kind=kind,
                kind_named=named,
                box=box,
                reason=_reason(
                    f"{FROM_THE_COUNTERTOP_LINE}: piece {index + 1} of {row.piece_count} of this "
                    f"row, suggested as a {kind.value} because {why}.{state_words}"
                ),
            )
        )
    if unplaced:
        states = (
            *states,
            "piece "
            + ", ".join(str(index + 1) for index in unplaced)
            + " could not be placed on the page, so it is not suggested",
        )

    return PlannedRow(
        page_number=row.page_number,
        label=row.label,
        row_candidate_id=row.anchor.id,
        extraction_run_id=run.id,
        view_id=view.id,
        parts=tuple(parts),
        not_proposed=None,
        states=states,
        automatic=_would_be_automatic(row, view, by_slot, pieces, parts, code),
        code_finder=code_finder,
    )


def _slot_candidates(row: SlotRow) -> dict[str, ObservationCandidate]:
    """The row's readings by slot (`"0"`, `"1"`, … and `"overall"`), the first of each."""
    by_slot: dict[str, ObservationCandidate] = {}
    for candidate in row.candidates:
        slot = next(
            (
                flag.removeprefix("slot:")
                for flag in candidate.ambiguity_flags or ()
                if flag.startswith("slot:")
            ),
            None,
        )
        if slot is not None and slot not in by_slot:
            by_slot[slot] = candidate
    return by_slot


def _stored_box(candidate: ObservationCandidate, page: Page, run: ExtractionRun) -> _Box | None:
    """The reading's own `slot-box:` in stored page space, placed as the results place it."""
    location = slot_location(candidate, page, run)
    if location is None:
        return None
    try:
        points = [(Decimal(x), Decimal(y)) for x, y in location.polygon]
    except (InvalidOperation, TypeError, ValueError):
        return None
    return _Box(
        left=min(x for x, _ in points),
        top=min(y for _, y in points),
        right=max(x for x, _ in points),
        bottom=max(y for _, y in points),
    )


def _region_box(view: DrawingView) -> _Box | None:
    region = view.region
    if not isinstance(region, dict) or region.get("space") != "stored":
        return None
    raw = region.get("points")
    if not isinstance(raw, list) or len(raw) < 3:
        return None
    try:
        points = [(Decimal(str(x)), Decimal(str(y))) for x, y in cast(list[list[object]], raw)]
    except (InvalidOperation, TypeError, ValueError):
        return None
    return _Box(
        left=min(x for x, _ in points),
        top=min(y for _, y in points),
        right=max(x for x, _ in points),
        bottom=max(y for _, y in points),
    )


def _views_holding(session: Session, page: Page, box: _Box) -> list[DrawingView]:
    """The drawings on the page whose region holds the row's line, end to end.

    The line, not the whole box: the box reaches the label's band above and below the line, which
    may cross a drawing's frame while the countertop itself does not.
    """
    views = session.scalars(
        select(DrawingView).where(DrawingView.page_id == page.id).order_by(DrawingView.id)
    ).all()
    holding: list[DrawingView] = []
    for view in views:
        region = _region_box(view)
        if region is None:
            continue
        if (
            region.left <= box.left
            and box.right <= region.right
            and region.top <= box.middle <= region.bottom
        ):
            holding.append(view)
    return holding


def _flags(row: SlotRow) -> frozenset[str]:
    return frozenset(
        flag for candidate in row.candidates for flag in candidate.ambiguity_flags or ()
    )


def _chosen_by_the_readers(row: SlotRow) -> bool:
    """Whether the row readers named this row (`row-choice:<n>`, n > 0); otherwise code chose it."""
    for flag in row.anchor.ambiguity_flags or ():
        if flag.startswith("row-choice:"):
            number = flag.removeprefix("row-choice:")
            return number.isdigit() and int(number) > 0
    return False


def _row_held(row: SlotRow) -> bool:
    return any(flag == "row-ambiguous" or flag.startswith("row-hold:") for flag in _flags(row))


def _states(row: SlotRow) -> tuple[str, ...]:
    states: list[str] = []
    if row.held_reason is not None:
        states.append(f"Held: {row.held_reason.rstrip('.')}")
    if not _chosen_by_the_readers(row):
        states.append("The AI readers did not choose this row; code did")
    return tuple(states)


def _kind(candidate: ObservationCandidate) -> tuple[PartKind, bool, str]:
    """The suggested kind, whether printed evidence named it, and why, in plain words."""
    flags = candidate.ambiguity_flags or ()
    printed = next((flag.removeprefix("kind:") for flag in flags if flag.startswith("kind:")), "")
    evidence = next(
        (
            flag.removeprefix("kind-evidence:")
            for flag in flags
            if flag.startswith("kind-evidence:")
        ),
        None,
    )
    if printed in _NAMED_KINDS:
        return _NAMED_KINDS[printed], True, f"the drawing names it ({evidence or printed})"
    if printed in _OTHER_KINDS:
        return PartKind.CABINET, False, _OTHER_KINDS[printed]
    return PartKind.CABINET, False, "nothing printed names its kind; say what it is"


def _reason(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 1] + "…"


def _would_be_automatic(
    row: SlotRow,
    view: DrawingView,
    by_slot: Mapping[str, ObservationCandidate],
    pieces: Sequence[tuple[int, ObservationCandidate, _Box | None]],
    parts: Sequence[PlannedPart],
    code: CodeFinding | str,
) -> WouldBeAutomatic:
    why_not: list[str] = []
    if not _chosen_by_the_readers(row):
        why_not.append("the AI readers did not choose this row")
    if _row_held(row):
        why_not.append("the row is held")
    if view.role != "shop":
        why_not.append("its drawing is not confirmed as the vendor's")
    boxes = [box for _, _, box in pieces]
    if any(box is None for box in boxes) or not _end_to_end(by_slot, row.piece_count):
        why_not.append("its pieces do not run end to end")
    countertop = not why_not

    piece_parts = {part.slot_index: part for part in parts if part.slot_index is not None}
    automatic: list[bool] = []
    reasons: list[tuple[str, ...]] = []
    agrees: list[bool | None] = []
    for index, candidate, box in pieces:
        part = piece_parts.get(index)
        piece_why: list[str] = [] if countertop else ["its countertop does not qualify"]
        if not candidate_is_sealed(candidate):
            piece_why.append("its reading is not sealed")
        if part is None or not part.kind_named:
            piece_why.append("nothing printed names its kind")
        agreed = None if isinstance(code, str) or box is None else _code_agrees(view, box, code)
        agrees.append(agreed)
        if agreed is None:
            piece_why.append("code's part finder did not run")
        elif not agreed:
            piece_why.append("code's part finder found no cabinet with the same ends")
        automatic.append(not piece_why)
        reasons.append(tuple(piece_why))
    return WouldBeAutomatic(
        countertop=countertop,
        countertop_why_not=tuple(why_not),
        pieces=tuple(automatic),
        pieces_why_not=tuple(reasons),
        pieces_code_agrees=tuple(agrees),
    )


def _end_to_end(by_slot: Mapping[str, ObservationCandidate], count: int) -> bool:
    """Whether the pieces' own boxes, in the reader's pixels, meet end to end, one per piece."""
    boxes: list[tuple[int, int]] = []
    for index in range(count):
        candidate = by_slot.get(str(index))
        if candidate is None:
            return False
        found = [
            flag.removeprefix("slot-box:")
            for flag in candidate.ambiguity_flags or ()
            if flag.startswith("slot-box:")
        ]
        if len(found) != 1:
            return False
        parts = found[0].split(",")
        if len(parts) != 4 or not all(part.isdigit() for part in parts):
            return False
        boxes.append((int(parts[0]), int(parts[2])))
    return bool(boxes) and all(boxes[i][1] == boxes[i + 1][0] for i in range(len(boxes) - 1))


def _code_agrees(view: DrawingView, box: _Box, code: CodeFinding) -> bool:
    return any(
        part.view_id == view.id
        and part.kind is PartKind.CABINET
        and abs(part.left - box.left) <= code.tolerance
        and abs(part.right - box.right) <= code.tolerance
        for part in code.parts
    )
