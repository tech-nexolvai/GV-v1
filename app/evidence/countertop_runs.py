"""The run beneath each countertop, as a person decides it on the Measure page (#893).

Once a person has confirmed a vendor drawing's parts (#882), this module is what the Measure page
asks for next to them: for each confirmed countertop, the run of cabinets and fillers the computer
suggests beneath it (`workflow/countertop_runs.py:propose_run`), and a person's answer to it.

- **Confirming** says which parts make up the run: the suggested ones, or the person's correction.
  The person picks the parts; the order is taken from the drawing, left to right, never from the
  order they were picked in.
- **Withdrawing** says the suggested or confirmed run is not the one beneath this countertop.

**Listing writes nothing.** A suggested run is worked out each time the page asks, from the parts a
person stands by at that moment, and is never stored: only a person's confirmation writes a
`countertop_runs` row, through `workflow/countertop_runs.py:confirm_countertop_run`.

**A run is confirmed only on a drawing a person has confirmed as the vendor's**, as a part is.
Withdrawing is allowed on any drawing of the revision, because it makes nothing.

**Without a stated tolerance nothing is suggested and nothing can be confirmed.** The tolerance is
recorded on every run, and there is no honest number to record when none was stated
(`app/config.py`, `GV_RUN_EDGE_TOLERANCE`).

**There is no "confirm all".** Each call decides one countertop's run.

**No extraction imports**, as in `app/evidence/parts.py`: the API reaches this, and
`tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that reads a PDF.

Source: issue #893; #748 plan, step 5. Verification: tests/api/test_countertop_runs.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.parts import revision_parts
from app.models import (
    CountertopRun,
    CountertopRunDecision,
    DrawingView,
    PartConfirmation,
    PartDecision,
    ViewRole,
)
from vocabulary.part_kinds import PartKind
from workflow.countertop_runs import (
    MEMBER_KINDS,
    PlacedPart,
    RunProposal,
    confirm_countertop_run,
    current_run_decision,
    live_parts_on,
    live_run_rows,
    propose_run,
    published_wall_layouts,
    withdraw_countertop_run,
)

__all__ = [
    "DrawingRuns",
    "ListedCountertop",
    "PickablePart",
    "RunMember",
    "RunRefusalReason",
    "RunRefused",
    "confirm_listed_run",
    "revision_runs",
    "withdraw_listed_run",
]

#: What a person is told when no tolerance is stated, so neither a suggestion nor a run can be made.
NO_TOLERANCE: Final = (
    "No run can be suggested or confirmed yet: the tolerance for whether a part lies along a "
    "countertop (GV_RUN_EDGE_TOLERANCE) has not been set for this system."
)


class RunRefusalReason(StrEnum):
    """Why a decision on a run was not recorded. Each is something a person can be told."""

    NO_SUCH_COUNTERTOP = "no_such_countertop"
    #: The countertop's drawing is not one a person confirmed as the vendor's.
    DRAWING_NOT_VENDORS = "drawing_not_vendors"
    #: `GV_RUN_EDGE_TOLERANCE` is not set.
    TOLERANCE_NOT_STATED = "tolerance_not_stated"
    NO_LAYOUT = "no_layout"
    NO_PARTS = "no_parts"
    PART_TWICE = "part_twice"
    #: A part named is not a confirmed cabinet or filler on the countertop's drawing.
    NOT_A_RUN_PART = "not_a_run_part"


@dataclass(frozen=True, slots=True)
class RunRefused:
    """A decision that was not recorded, and why, in plain English."""

    reason: RunRefusalReason
    detail: str


@dataclass(frozen=True, slots=True)
class PickablePart:
    """A confirmed part on a drawing, with the number it has in "Parts of each drawing"."""

    part: PlacedPart
    number: int | None
    """Its place in that list, `1` for the leftmost; `None` if it is not listed there."""
    code: str | None


@dataclass(frozen=True, slots=True)
class RunMember:
    """One member of a confirmed run, as it was confirmed."""

    row: CountertopRun
    kind: PartKind | None
    """The kind the member was confirmed as when it joined the run."""
    number: int | None
    stands: bool
    """Whether a person still stands by the part; a run with a part taken back is not read."""


@dataclass(frozen=True, slots=True)
class ListedCountertop:
    """One confirmed countertop: the run suggested beneath it, and what a person decided."""

    countertop: PickablePart
    proposal: RunProposal | None
    """`None` when no tolerance is stated."""
    decision: CountertopRunDecision | None
    """The decision nothing has replaced, or `None` while nobody has decided."""
    members: tuple[RunMember, ...]
    """The current decision's run, in order; empty unless that decision confirmed one."""
    read: bool
    """Whether that run is the one a reader reads: confirmed, and every part still stood by."""


@dataclass(frozen=True, slots=True)
class DrawingRuns:
    """One drawing with a confirmed part, its countertops and the parts a run may hold."""

    view: DrawingView
    page_index: int
    parts: tuple[PickablePart, ...]
    """The confirmed cabinets and fillers on the drawing, left to right."""
    countertops: tuple[ListedCountertop, ...]
    refusal: RunRefused | None
    """Why no run can be confirmed on this drawing, or `None` when one can."""


def revision_runs(
    session: Session, package_revision_id: UUID, *, edge_tolerance: Decimal | None
) -> tuple[DrawingRuns, ...]:
    """Every drawing of the revision with a confirmed part a person stands by.

    In page order; each drawing's countertops left to right. A part is confirmed only on a drawing
    confirmed as the vendor's, so these are vendor drawings, unless a person has since said
    otherwise; such a drawing is listed with why no run can be confirmed on it, so a run confirmed
    there before can still be withdrawn. **Reads only.**
    """
    drawings: list[DrawingRuns] = []
    for drawing in revision_parts(session, package_revision_id):
        numbers = {listed.proposal.id: listed.position for listed in drawing.parts}
        decided = {
            listed.decision.drawing_item_id: listed.decision
            for listed in drawing.parts
            if listed.decision is not None and listed.decision.drawing_item_id is not None
        }
        placed = live_parts_on(session, drawing.view.id)
        if not placed:
            continue
        pickable = {
            part.item_id: PickablePart(
                part=part,
                number=(
                    numbers.get(decided[part.item_id].part_proposal_id)
                    if part.item_id in decided
                    else None
                ),
                code=decided[part.item_id].code_as_printed if part.item_id in decided else None,
            )
            for part in placed
        }
        countertops = [
            _listed_countertop(
                session,
                pickable[part.item_id],
                placed,
                numbers,
                edge_tolerance,
            )
            for part in placed
            if part.kind is PartKind.COUNTERTOP
        ]
        drawings.append(
            DrawingRuns(
                view=drawing.view,
                page_index=drawing.page_index,
                parts=tuple(pickable[part.item_id] for part in placed if part.kind in MEMBER_KINDS),
                countertops=tuple(countertops),
                refusal=_cannot_confirm(drawing.view),
            )
        )
    return tuple(drawings)


def confirm_listed_run(
    session: Session,
    *,
    package_revision_id: UUID,
    countertop_item_id: UUID,
    member_item_ids: Sequence[UUID],
    edge_tolerance: Decimal | None,
    actor: str,
    wall_config: str | None,
) -> CountertopRunDecision | RunRefused:
    """A person saying which parts make up the run beneath one countertop of this revision.

    Recorded by `confirm_countertop_run`, which orders the parts across the page, writes the run and
    audits the decision. Confirming again is how a person corrects a run.
    """
    found = _countertop(session, package_revision_id, countertop_item_id)
    if isinstance(found, RunRefused):
        return found
    countertop, view = found
    refused = _cannot_confirm(view)
    if refused is not None:
        return refused
    if edge_tolerance is None:
        return RunRefused(RunRefusalReason.TOLERANCE_NOT_STATED, NO_TOLERANCE)
    if not wall_config:
        return RunRefused(
            RunRefusalReason.NO_LAYOUT,
            "Choose this countertop's wall layout before confirming its run.",
        )
    if wall_config not in published_wall_layouts(session):
        return RunRefused(
            RunRefusalReason.NO_LAYOUT,
            "Choose a wall layout offered by the published countertop width check.",
        )
    if not member_item_ids:
        return RunRefused(
            RunRefusalReason.NO_PARTS,
            "A run needs at least one part. If nothing sits beneath this countertop, say the run is "
            "wrong instead.",
        )
    if len(set(member_item_ids)) != len(member_item_ids):
        return RunRefused(RunRefusalReason.PART_TWICE, "Each part can be in the run once.")
    members = {
        part.item_id for part in live_parts_on(session, view.id) if part.kind in MEMBER_KINDS
    }
    if not set(member_item_ids) <= members:
        return RunRefused(
            RunRefusalReason.NOT_A_RUN_PART,
            "A run holds only cabinets and fillers confirmed on the countertop's own drawing. One "
            "of these is not, or was taken back since the page was loaded. Reload the page.",
        )
    return confirm_countertop_run(
        session,
        countertop_item_id=countertop.item_id,
        member_item_ids=member_item_ids,
        edge_tolerance=edge_tolerance,
        actor=actor,
        wall_config=wall_config,
    )


def withdraw_listed_run(
    session: Session, *, package_revision_id: UUID, countertop_item_id: UUID, actor: str
) -> CountertopRunDecision | RunRefused:
    """A person saying the run suggested or confirmed beneath one countertop is not its run."""
    found = _countertop(session, package_revision_id, countertop_item_id)
    if isinstance(found, RunRefused):
        return found
    return withdraw_countertop_run(session, countertop_item_id=found[0].item_id, actor=actor)


def _cannot_confirm(view: DrawingView) -> RunRefused | None:
    if view.role == ViewRole.SHOP.value:
        return None
    return RunRefused(
        RunRefusalReason.DRAWING_NOT_VENDORS,
        "This drawing is no longer confirmed as the vendor's, so no run can be confirmed on it. A "
        "run confirmed here before can still be said to be wrong.",
    )


def _countertop(
    session: Session, package_revision_id: UUID, item_id: UUID
) -> tuple[PlacedPart, DrawingView] | RunRefused:
    """The countertop, if it is a confirmed one a person stands by on a drawing of this revision."""
    for drawing in revision_parts(session, package_revision_id):
        for part in live_parts_on(session, drawing.view.id):
            if part.item_id == item_id and part.kind is PartKind.COUNTERTOP:
                return part, drawing.view
    return RunRefused(
        RunRefusalReason.NO_SUCH_COUNTERTOP, "There is no such countertop on this package."
    )


def _listed_countertop(
    session: Session,
    countertop: PickablePart,
    placed: Sequence[PlacedPart],
    numbers: dict[UUID, int],
    edge_tolerance: Decimal | None,
) -> ListedCountertop:
    decision = current_run_decision(session, countertop.part.item_id)
    rows: list[CountertopRun] = []
    read: set[UUID] = set()
    if decision is not None and decision.decision == PartDecision.CONFIRMED.value:
        read = set(
            session.scalars(
                live_run_rows()
                .where(CountertopRun.run_id == decision.run_id)
                .with_only_columns(CountertopRun.id)
            )
        )
        rows = list(
            session.scalars(
                select(CountertopRun)
                .where(CountertopRun.run_id == decision.run_id)
                .order_by(CountertopRun.position)
            )
        )
    standing = {part.item_id for part in placed}
    joined = {
        row.drawing_item_id: row
        for row in session.scalars(
            select(PartConfirmation).where(
                PartConfirmation.drawing_item_id.in_([row.member_item_id for row in rows])
            )
        )
        if row.drawing_item_id is not None
    }
    members = tuple(
        RunMember(
            row=row,
            kind=_kind(joined.get(row.member_item_id)),
            number=_number(joined.get(row.member_item_id), numbers),
            stands=row.member_item_id in standing,
        )
        for row in rows
    )
    return ListedCountertop(
        countertop=countertop,
        proposal=(
            None
            if edge_tolerance is None
            else propose_run(countertop.part, placed, edge_tolerance=edge_tolerance)
        ),
        decision=decision,
        members=members,
        read=bool(rows) and all(row.id in read for row in rows),
    )


def _kind(confirmation: PartConfirmation | None) -> PartKind | None:
    return (
        None if confirmation is None or confirmation.kind is None else PartKind(confirmation.kind)
    )


def _number(confirmation: PartConfirmation | None, numbers: dict[UUID, int]) -> int | None:
    return None if confirmation is None else numbers.get(confirmation.part_proposal_id)
