"""Each live countertop's architect comparison, looked up by the countertop: Milan by item (#1273).

**What it answers.** For one live countertop item (`workflow/item_plan.py`), whether its countertop
row is compared with the architect's drawing, and how: "not compared" with the stored reason,
"waits for the reviewer" with the reason, or the pairs that are compared, each vendor piece named
by the piece item a person confirmed for it.

**How it finds them.** The item's row is the one `item_plan.live_countertops` resolves (#1247,
following a re-read to the newest row only where that row is provably the item's own). The row's
architect pairing and its match with a view of the architect's own file are the effective ones
(`workflow/architect_row_plan.py`: `effective_architect_pairings`, `effective_architect_matches`),
and what the check does with them is `plan_architect_row`, the very function the vendor-vs-architect
check (CT-ARCH-WIDTH-001) plans each row with. Nothing here judges a pairing a second time.

**Piece items.** Bala files one suggestion per piece of a row and links it to the row and the
piece's place (`part_proposal_rows.slot_index`, `0` from the left); a compared pair names the same
place (`ComparedPair.vendor_slot`). A pair's piece item is the live item confirmed from the
suggestion linked to **that same row** and place, and nothing else: after a re-read the newest row
has no piece links of its own (Bala does not suggest a decided countertop's row again), so its
pairs name no piece item rather than one read from another reading, whose pieces may lie
differently.

**Read-only, and it feeds no check** (design phase 5, option A2, decided by Anant 2026-10-14).
Pairings stay stored by row (#1210's carry-over is untouched); nothing here writes a row, runs a
check or asks a model, and no check, rule or evidence step imports this module
(`tests/workflow/test_milan_items.py` holds that). CAB-ARCH-VS-SHOP-001 still reads its cabinets
from Measurements only.

**A fixed number of statements** for any number of countertops, as the countertop results ask
them, except where a pairing's status is `ambiguous` or `no_fit`: there `plan_architect_row` asks
once per such row whether the architect prints a usable dimension on its page, exactly as the
countertop results do. No stored set has such a pairing on a countertop's row.

Source: issue #1273; phase 5 cabinets design §1, §5 (A2, P5-M1). Verification:
`tests/workflow/test_milan_items.py`, `tests/api/test_item_architect.py`.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.evidence.sides import ReadingSides, has_separate_architect_file
from app.models import (
    DrawingItem,
    DrawingView,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartDecision,
    PartProposalRow,
)
from workflow.architect_match_records import effective_architect_matches
from workflow.architect_pairing_contract import EffectivePairing

# The batch form of `architect_views` (#1167), private to its module; the countertop results ask
# it the same way, so a row with no match costs no statement of its own.
from workflow.architect_pairing_records import _architect_views_by_page
from workflow.architect_row_plan import (
    ComparedPair,
    Disposition,
    PairingJudgments,
    PairingLookup,
    architect_file_indexed,
    architect_matches_waiting_for_run,
    effective_architect_pairings,
    pair_label,
    pairing_judgments,
    plan_architect_row,
)
from workflow.item_plan import LiveCountertop, live_countertops
from workflow.slot_row_scope import SlotRow, slot_rows_and_unchosen_pages

__all__ = [
    "NO_ROW_FOR_ARCHITECT",
    "ROW_NOT_IN_READING",
    "ArchitectState",
    "ItemArchitectStatus",
    "ItemPair",
    "architect_status_for_item",
    "architect_statuses_for_revision",
]

type ArchitectState = Literal["compared", "not_compared", "waits_for_reviewer"]
"""`compared`: at least one pair is compared; `not_compared`: nothing comparable, with the reason;
`waits_for_reviewer`: something may be comparable and a person pairs it, or chooses the view."""

#: A countertop confirmed from a suggestion that is not one of Bala's, or not provably followed to
#: the newest reading's row (`item_plan.NO_ROW_LINKED`): no row, so no pairing belongs to it.
NO_ROW_FOR_ARCHITECT: Final = (
    "No countertop line is linked to this countertop, so no architect pairing belongs to it and "
    "nothing was compared."
)
#: A countertop whose row is not among the newest reading's countertop rows.
ROW_NOT_IN_READING: Final = (
    "This countertop's line is not among the newest reading's countertop lines, so its architect "
    "pairing could not be looked up and nothing was compared."
)

_STATES: Final[dict[Disposition, ArchitectState]] = {
    Disposition.COMPARE: "compared",
    Disposition.NOT_COMPARED: "not_compared",
    Disposition.UNRESOLVED: "waits_for_reviewer",
}


@dataclass(frozen=True, slots=True)
class ItemPair:
    """One width compared with the architect: the overall, or one vendor piece."""

    kind: Literal["piece", "overall"]
    vendor_slot: int | None
    """The piece's place along the row, `0` from the left; `None` for the overall."""
    label: str
    """`overall` or `piece 2`, as the countertop results say it."""
    piece_item_id: UUID | None
    """The live piece item confirmed from Bala's suggestion for this row and place; `None` for the
    overall, for a piece nobody confirmed, and for a row Bala linked no pieces to."""
    piece_item_type: str | None
    """That item's type (`cabinet_width`, `filler_width`); `None` when there is no item."""
    architect_candidate_id: UUID


@dataclass(frozen=True, slots=True)
class ItemArchitectStatus:
    """Where one live countertop stands with the architect's drawing."""

    countertop: LiveCountertop
    state: ArchitectState
    reason: str | None
    """Why it is not compared, or what the reviewer is asked; `None` when it is compared."""
    pairs: tuple[ItemPair, ...]
    pairing_source: str | None
    """Who paired its row (`code+ais`, `code`, `both-ais`, `reviewer`, `none`); `None` with no
    pairing."""
    pairing_judgments: PairingJudgments | None


def architect_statuses_for_revision(
    session: Session,
    package_revision_id: UUID,
    *,
    pairing_lookup: PairingLookup | None = None,
) -> tuple[ItemArchitectStatus, ...]:
    """Every live countertop of the revision with its architect comparison, in page order.

    `pairing_lookup` answers a row's effective pairing; by default every row's is read in one batch
    (`effective_architect_pairings`), as the countertop results read it.
    """
    countertops = live_countertops(session, package_revision_id)
    if not countertops:
        return ()
    anchors = {c.row_candidate_id for c in countertops if c.row_candidate_id is not None}
    rows: dict[UUID, SlotRow] = {}
    if anchors:
        rows = {
            row.anchor.id: row
            for row in slot_rows_and_unchosen_pages(session, package_revision_id)[0]
            if row.anchor.id in anchors
        }
    found = tuple(rows)
    if pairing_lookup is None:
        pairings = effective_architect_pairings(session, found) if found else {}

        def lookup(_session: Session, row_anchor_id: UUID) -> EffectivePairing | None:
            return pairings.get(row_anchor_id)

        pairing_lookup = lookup
    # As the countertop results ask it (#1161, #1168): whether the architect's drawings came as
    # their own file, and when that file was indexed, every row's match with one of its views.
    separate = bool(found) and has_separate_architect_file(session, package_revision_id)
    indexed = separate and architect_file_indexed(session, package_revision_id)
    matches = effective_architect_matches(session, found) if indexed else {}
    waiting = architect_matches_waiting_for_run(
        session,
        package_revision_id,
        {match.record_id for match in matches.values() if match is not None},
    )
    unmatched_pages = {rows[anchor].anchor.page_id for anchor in found if not matches.get(anchor)}
    own_view_pages = (
        frozenset(_architect_views_by_page(session, unmatched_pages))
        if indexed and unmatched_pages
        else frozenset()
    )
    pieces = _piece_items(session, found)
    sides = ReadingSides(session)

    statuses: list[ItemArchitectStatus] = []
    for countertop in countertops:
        if countertop.row_candidate_id is None:
            statuses.append(_not_compared(countertop, NO_ROW_FOR_ARCHITECT))
            continue
        row = rows.get(countertop.row_candidate_id)
        if row is None:
            statuses.append(_not_compared(countertop, ROW_NOT_IN_READING))
            continue
        pairing = pairing_lookup(session, row.anchor.id)
        match = matches.get(row.anchor.id)
        plan = plan_architect_row(
            session,
            row,
            pairing,
            sides=sides,
            separate_architect_file=separate,
            architect_file_indexed=indexed,
            match=match,
            match_waits_for_run=match is not None and match.record_id in waiting,
            own_architect_view=None if not indexed else row.anchor.page_id in own_view_pages,
        )
        source = None if plan.pairing is None else plan.pairing.source
        statuses.append(
            ItemArchitectStatus(
                countertop=countertop,
                state=_STATES[plan.disposition],
                reason=plan.reason,
                pairs=tuple(_item_pair(pair, pieces, row.anchor.id) for pair in plan.pairs),
                pairing_source=source,
                pairing_judgments=pairing_judgments(source),
            )
        )
    return tuple(statuses)


def architect_status_for_item(
    session: Session,
    item_id: UUID,
    *,
    package_revision_id: UUID | None = None,
    pairing_lookup: PairingLookup | None = None,
) -> ItemArchitectStatus | None:
    """One countertop item's architect comparison, or `None` when it is not a live countertop.

    `package_revision_id` defaults to the newest revision holding the item's drawing: the same
    drawing can be carried into a later revision, whose reading is the one that counts.
    """
    if package_revision_id is None:
        package_revision_id = session.scalar(
            select(PackageRevision.id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.package_revision_id == PackageRevision.id,
            )
            .join(Page, Page.document_version_id == PackageRevisionDocument.document_version_id)
            .join(DrawingView, DrawingView.page_id == Page.id)
            .join(DrawingItem, DrawingItem.drawing_view_id == DrawingView.id)
            .where(DrawingItem.id == item_id)
            .order_by(PackageRevision.revision_number.desc())
            .limit(1)
        )
        if package_revision_id is None:
            return None
    for status in architect_statuses_for_revision(
        session, package_revision_id, pairing_lookup=pairing_lookup
    ):
        if status.countertop.item_id == item_id:
            return status
    return None


def _not_compared(countertop: LiveCountertop, reason: str) -> ItemArchitectStatus:
    return ItemArchitectStatus(
        countertop=countertop,
        state="not_compared",
        reason=reason,
        pairs=(),
        pairing_source=None,
        pairing_judgments=None,
    )


type _Pieces = dict[tuple[UUID, int], tuple[UUID, str]]


def _item_pair(pair: ComparedPair, pieces: _Pieces, row_anchor_id: UUID) -> ItemPair:
    """One compared pair, its vendor piece named by the piece item of the same row and place."""
    piece = None if pair.vendor_slot is None else pieces.get((row_anchor_id, pair.vendor_slot))
    return ItemPair(
        kind=pair.kind,
        vendor_slot=pair.vendor_slot,
        label=pair_label(pair.kind, pair.vendor_slot),
        piece_item_id=None if piece is None else piece[0],
        piece_item_type=None if piece is None else piece[1],
        architect_candidate_id=pair.architect_candidate_id,
    )


def _piece_items(session: Session, row_anchor_ids: Collection[UUID]) -> _Pieces:
    """Each listed row's live piece items by place: the item whose current decision confirms the
    suggestion Bala linked to that row and place. One statement, whatever the number of rows."""
    if not row_anchor_ids:
        return {}
    later = aliased(PartConfirmation)
    found: _Pieces = {}
    for row_id, slot, item_id, item_type in session.execute(
        select(
            PartProposalRow.row_candidate_id,
            PartProposalRow.slot_index,
            DrawingItem.id,
            DrawingItem.item_type,
        )
        .join(
            PartConfirmation,
            PartConfirmation.part_proposal_id == PartProposalRow.part_proposal_id,
        )
        .join(DrawingItem, DrawingItem.id == PartConfirmation.drawing_item_id)
        .where(
            PartProposalRow.row_candidate_id.in_(tuple(row_anchor_ids)),
            PartProposalRow.slot_index.is_not(None),
            PartConfirmation.decision == PartDecision.CONFIRMED.value,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
    ):
        if slot is not None:
            found[(row_id, slot)] = (item_id, item_type)
    return found
