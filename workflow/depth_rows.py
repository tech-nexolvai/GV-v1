"""Depth and sink checks per countertop row, from code's own countertop suggestion (#1390).

**The gap.** The five depth and sink checks (`workflow/depth_evidence.DEPTH_RULE_IDS`) run for one
countertop only once a person confirms the countertop **and** decides its cabinet run on the
advanced Measurements view (`workflow/part_operands.countertop_scopes`). The simple review screen
never makes a run, so every depth and sink result stayed a placeholder held on its countertop row
(#1201) that nobody could answer there.

**The rule (Anant, 2026-10-16, behind `GV_DEPTH_PER_ROW`, off by default).** A countertop row gets
its own five depth and sink checks, with no cabinet run, only where two independent judgments agree
on which countertop the row is:

1. **The suggestion itself** (the 2026-10-14 rule, `workflow/bala_items.WouldBeAutomatic`): both AI
   row readers named this very row, nothing holds it, its pieces tile its printed overall, every
   width is sealed by both readers, and code finds the whole row inside exactly one drawing that a
   confirmation says is the vendor's. Bala filed exactly one countertop suggestion from the row on
   that drawing, and no person took it back or confirmed it as something else.
2. **Code's geometry**: that drawing holds exactly one countertop now (every live suggestion and
   every live countertop item on it, `workflow/depth_again.countertops_by_drawing`), no reader named
   a second countertop on the page, and every plan or section view code tied to the suggestion lies
   clearly inside that drawing (nine tenths in it, under a tenth in any other: the depth reader's
   own placement rule, `VIEW_IN_DRAWING`).

Each such row's checks read only the readings tied to its suggestion
(`workflow/depth_evidence.suggestion_depth_operands`): a value still needs its own two judgments
(both readers plus code's printed-text or chain check) or a person's Confirm, exactly as for a
person's countertop. **Anything less is held, with a plain reason** (`DepthRows.held`), and the row
keeps today's placeholder; a split page has no row and no suggestion, so it is never checked here.

**A person always wins.** A run a person decided anywhere on the revision keeps the whole revision
on the advanced path (the caller asks this only when `countertop_scopes` is `None`); a person's
withdrawal of the suggestion holds the row; a person's correction of a view's link is followed; a
person's confirmed value comes first (`depth_evidence._decide`).

**Never silent.** A row result says which suggestion and drawing it is for (`ROW_CHECK_NOTE`), and
the screens offer Confirm on its readings only where it still holds now (`row_check_proposal`, the
same judgments re-read).

Read-only and model-free: no extraction import, no write.

Source: issue #1390; the depth research of 2026-10-16. Verification:
`tests/workflow/test_depth_per_row.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    DrawingView,
    ItemViewLink,
    PartDecision,
    PartProposal,
    PartProposalRow,
)
from vocabulary.part_kinds import PartKind
from workflow.bala_items import BALA_SOURCE, plan_items
from workflow.depth_again import countertops_by_drawing
from workflow.evidence_operands import ONE_COUNTERTOPS_READING
from workflow.item_plan import live_countertops
from workflow.parts import current_decision, outline_box
from workflow.slot_row_scope import slot_rows_and_unchosen_pages

__all__ = [
    "NOT_PER_ROW",
    "ONE_COUNTERTOPS_OPENING",
    "ROW_CHECK_NOTE",
    "VIEW_IN_ANOTHER",
    "VIEW_IN_DRAWING",
    "WHOLE_PACKAGE_VALUE",
    "DepthRow",
    "DepthRows",
    "depth_rows",
    "row_check_proposal",
    "view_in_drawing",
]

#: The note on each row's own result: which suggestion and drawing the row's checks read. The
#: screens find it by its opening words (`row_check_proposal`); a decision's carry-over ignores notes.
ROW_CHECK_NOTE: Final = (
    "Checked for this countertop row on its own, because two judgments agree on which countertop "
    "it is: countertop suggestion {proposal}, vendor drawing {view}."
)
_ROW_CHECK_OPENING: Final = ROW_CHECK_NOTE.split("{proposal}")[0]
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: How a whole-package hold opens when a person confirmed a depth reader's reading
#: (`workflow/evidence_operands.ONE_COUNTERTOPS_READING`): that is one countertop's value, which its
#: row's own check uses, never a value given for the whole package.
ONE_COUNTERTOPS_OPENING: Final = ONE_COUNTERTOPS_READING.split("{countertops}")[0]

#: Why a row is not checked on its own, added to its held result's reason (`DepthRows.held`).
NOT_PER_ROW: Final = "It is not checked on its countertop row on its own: {why}."

#: The depth reader's placement rule (`workflow/depth_reader.VIEW_IN_DRAWING`, held the same by a
#: test, since this module may not import the reader).
VIEW_IN_DRAWING: Final = Decimal("0.9")
VIEW_IN_ANOTHER: Final = Decimal("0.1")

# Why a row is held, in plain words (each said after `NOT_PER_ROW`'s colon).
NOT_SUGGESTED: Final = "code made no countertop suggestion from this row ({why})"
NOT_TWO_JUDGMENTS: Final = (
    "the AI readers and code do not both say which countertop this row is ({why})"
)
NO_ONE_SUGGESTION: Final = "this row does not have exactly one countertop suggestion on its drawing"
TAKEN_BACK: Final = "a person took back the countertop suggested from this row"
NOT_A_COUNTERTOP: Final = (
    "a person decided the part suggested from this row is not a live countertop"
)
ANOTHER_ROW: Final = "a reader named another countertop row on this page"
WHOLE_PACKAGE_VALUE: Final = (
    "a person gave a value for this check for the whole package, which stands over any reading"
)
NO_ROW_READ: Final = (
    "no countertop row was read on this page, so code made no countertop suggestion from it"
)
SHARED_DRAWING: Final = (
    "its drawing holds {count} countertops, so a plan or section on it cannot be tied to one"
)
VIEW_OUTSIDE: Final = (
    "a plan or section view tied to it does not lie clearly inside its drawing (at least nine "
    "tenths in it and under a tenth in any other)"
)


@dataclass(frozen=True, slots=True)
class DepthRow:
    """One countertop row whose two judgments agree: its own five checks read its suggestion's."""

    row_id: UUID
    """The row's anchor reading: what its results are scoped to (as #1201's held results are)."""
    proposal_id: UUID
    """Bala's countertop suggestion from the row."""
    view_id: UUID
    """The vendor drawing that holds the row, and only this countertop."""
    label: str
    page_number: int


@dataclass(frozen=True, slots=True)
class DepthRows:
    """The rows checked on their own, and why each other row of the newest reading is not."""

    rows: tuple[DepthRow, ...] = ()
    held: Mapping[UUID, str] = field(default_factory=dict)
    """Each other row, by its anchor reading: why it is not checked on its own, in plain words."""

    def row(self, row_id: UUID | None) -> DepthRow | None:
        return next((row for row in self.rows if row.row_id == row_id), None)


def view_in_drawing(
    drawings: Sequence[tuple[UUID, tuple[Decimal, Decimal, Decimal, Decimal]]],
    box: tuple[Decimal, Decimal, Decimal, Decimal],
) -> list[UUID] | None:
    """The drawing a view's box lies in: at least nine tenths of it inside, and under a tenth inside
    any other (`workflow/depth_reader.drawings_holding`, the same rule on the same numbers; a test
    holds the two the same). `None` when the page records no drawing; `[]` when none clearly holds
    it."""
    if not drawings:
        return None
    left, top, right, bottom = box
    area = (right - left) * (bottom - top)
    if area <= 0:
        return []
    shares: list[tuple[UUID, Decimal]] = []
    for drawing_id, (x0, y0, x1, y1) in drawings:
        overlap = max(Decimal(0), min(right, x1) - max(left, x0)) * max(
            Decimal(0), min(bottom, y1) - max(top, y0)
        )
        shares.append((drawing_id, overlap / area))
    inside = [drawing for drawing, share in shares if share >= VIEW_IN_DRAWING]
    if len(inside) != 1:
        return []
    if any(share >= VIEW_IN_ANOTHER for drawing, share in shares if drawing != inside[0]):
        return []
    return inside


def depth_rows(session: Session, revision_id: UUID) -> DepthRows:
    """The newest reading's countertop rows checked on their own, on two judgments (module
    docstring), and why each other row is not. Reads only."""
    plan = plan_items(session, revision_id)
    if plan.run_id is None:
        return DepthRows()
    slot_rows, unchosen = slot_rows_and_unchosen_pages(session, revision_id, run_id=plan.run_id)
    rows_by_anchor = {row.anchor.id: row for row in slot_rows}
    rows_on_page: dict[UUID, int] = {}
    for row in slot_rows:
        rows_on_page[row.anchor.page_id] = rows_on_page.get(row.anchor.page_id, 0) + 1
    suggested = _row_suggestions(session, [planned.row_candidate_id for planned in plan.rows])
    on_drawing = countertops_by_drawing(session, revision_id)
    live = {
        countertop.item_id
        for countertop in live_countertops(session, revision_id, follow_rows=False)
    }

    rows: list[DepthRow] = []
    held: dict[UUID, str] = {}
    for planned in plan.rows:
        anchor = planned.row_candidate_id
        if planned.not_proposed is not None or planned.view_id is None:
            held[anchor] = NOT_SUGGESTED.format(
                why=planned.not_proposed or "no drawing holds the row"
            )
            continue
        if not planned.automatic.countertop:
            held[anchor] = NOT_TWO_JUDGMENTS.format(
                why="; ".join(planned.automatic.countertop_why_not) or "no reason was recorded"
            )
            continue
        proposals = suggested.get(anchor, [])
        if len(proposals) != 1 or not _bala_countertop_on(proposals[0], planned.view_id):
            held[anchor] = NO_ONE_SUGGESTION
            continue
        proposal = proposals[0]
        decision = current_decision(session, proposal)
        if decision is not None and decision.decision == PartDecision.WITHDRAWN.value:
            held[anchor] = TAKEN_BACK
            continue
        # A confirmation stands for a countertop only while its item is a live countertop: one a
        # person confirmed as another kind, or corrected, is not (`live_countertops`).
        if decision is not None and decision.drawing_item_id not in live:
            held[anchor] = NOT_A_COUNTERTOP
            continue
        slot_row = rows_by_anchor.get(anchor)
        if slot_row is None or slot_row.also or rows_on_page.get(slot_row.anchor.page_id, 0) != 1:
            held[anchor] = ANOTHER_ROW
            continue
        sharing = set(on_drawing.get(planned.view_id, ())) | {proposal.id}
        if len(sharing) != 1:
            held[anchor] = SHARED_DRAWING.format(count=len(sharing))
            continue
        if not _views_inside(session, proposal.id, planned.view_id):
            held[anchor] = VIEW_OUTSIDE
            continue
        rows.append(
            DepthRow(
                row_id=anchor,
                proposal_id=proposal.id,
                view_id=planned.view_id,
                label=planned.label,
                page_number=planned.page_number,
            )
        )
    # A page with no countertop row read on it (the readers split, or a person said there is one):
    # it has no row and no suggestion, so it is never checked on its own; its held result says so.
    for page in unchosen:
        held.setdefault(page.record.id, NO_ROW_READ)
    return DepthRows(tuple(rows), held)


def row_check_proposal(notes: Iterable[object] | None) -> UUID | None:
    """The countertop suggestion a row's own result was checked for (`ROW_CHECK_NOTE`), or `None`
    for any other result (a #1201 placeholder, a whole-package result, a countertop's own)."""
    for note in notes or ():
        if isinstance(note, str) and note.startswith(_ROW_CHECK_OPENING):
            found = _UUID.search(note[len(_ROW_CHECK_OPENING) :])
            return None if found is None else UUID(found.group(0))
    return None


def _row_suggestions(session: Session, anchors: Sequence[UUID]) -> dict[UUID, list[PartProposal]]:
    """Each row's countertop suggestions (its links with no piece): Bala files one per row."""
    if not anchors:
        return {}
    found: dict[UUID, list[PartProposal]] = {}
    for row_id, proposal in session.execute(
        select(PartProposalRow.row_candidate_id, PartProposal)
        .join(PartProposal, PartProposal.id == PartProposalRow.part_proposal_id)
        .where(
            PartProposalRow.row_candidate_id.in_(list(anchors)),
            PartProposalRow.slot_index.is_(None),
        )
        .order_by(PartProposal.created_at, PartProposal.id)
    ).all():
        found.setdefault(row_id, []).append(proposal)
    return found


def _bala_countertop_on(proposal: PartProposal, view_id: UUID) -> bool:
    return (
        proposal.kind == PartKind.COUNTERTOP.value
        and proposal.source == BALA_SOURCE
        and proposal.drawing_view_id == view_id
    )


def _views_inside(session: Session, proposal_id: UUID, view_id: UUID) -> bool:
    """Whether every plan or section view code tied to the suggestion, as its link stands now, lies
    clearly inside the suggestion's own drawing. A link a person made or corrected is a person's
    word and stands wherever it is (a person always wins)."""
    drawing = session.get(DrawingView, view_id)
    if drawing is None:
        return False
    links = list(
        session.scalars(
            select(ItemViewLink).where(
                or_(
                    ItemViewLink.part_proposal_id == proposal_id,
                    ItemViewLink.page_id == drawing.page_id,
                )
            )
        )
    )
    replaced = {link.supersedes_id: link for link in links if link.supersedes_id is not None}
    current: list[ItemViewLink] = []
    for link in links:
        if link.supersedes_id is not None:
            continue
        now, seen = link, {link.id}
        while (later := replaced.get(now.id)) is not None and later.id not in seen:
            seen.add(later.id)
            now = later
        current.append(now)
    mine = [
        link
        for link in dict.fromkeys(current)
        if link.part_proposal_id == proposal_id and link.judged_by != "person"
    ]
    if not mine:
        return True
    drawings: list[tuple[UUID, tuple[Decimal, Decimal, Decimal, Decimal]]] = []
    for other in session.scalars(
        select(DrawingView).where(DrawingView.page_id == drawing.page_id).order_by(DrawingView.id)
    ):
        try:
            drawings.append((other.id, outline_box(other.region)))
        except (TypeError, ValueError):
            continue
    for link in mine:
        if link.page_id != drawing.page_id:
            return False
        try:
            box = outline_box(link.region)
        except (TypeError, ValueError):
            return False
        if view_in_drawing(drawings, box) != [view_id]:
            return False
    return True
