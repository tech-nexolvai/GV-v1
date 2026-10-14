"""Each live countertop's architect comparison, looked up by the countertop: Milan by item (#1273).

**What it answers.** For one live countertop item (`workflow/item_plan.py`), where its countertop
row stands with the architect's drawing, **exactly as the countertop results show that row**
(`app/api/visual_countertops.py`, `_architect_block`), so the line under a countertop can never
contradict the row under it (review of #1279):

* the row has a live CT-ARCH-WIDTH-001 result: that **recorded** result. PASS or FAIL is
  `compared`, with the pairs the check compared (its recorded operands); any other outcome is
  `for_reviewer`, with the check's reason (a pairing resting on one judgment, an architect value
  the check may not use, a pairing still to make). Never the pairing as it stands now: a pairing
  changed after the run counts only once the checks run again;
* no result: the row's effective pairing and match are planned with `plan_architect_row`, the very
  function the check plans each row with. Nothing comparable is `not_compared`, with the stored
  reason; anything else is `not_checked_yet` (`NOT_CHECKED_YET`, the results' own words), because
  only a check run compares.

**Piece items.** Bala files one suggestion per piece of a row and links it to the row and the
piece's place (`part_proposal_rows.slot_index`, `0` from the left); a recorded pair names the same
place (`vendor_piece[<i>]`). A pair's piece item is the live item confirmed from the suggestion
linked to **that same row** and place, and nothing else: after a re-read the newest row has no
piece links of its own (Bala does not suggest a decided countertop's row again), so its pairs name
no piece item rather than one read from another reading, whose pieces may lie differently.

**Read-only, and it feeds no check** (design phase 5, option A2, decided by Anant 2026-10-14).
Pairings stay stored by row (#1210's carry-over is untouched); nothing here writes a row, runs a
check or asks a model, and no check, rule or evidence step imports this module
(`tests/workflow/test_milan_items.py` holds that). CAB-ARCH-VS-SHOP-001 still reads its cabinets
from Measurements only.

**A fixed number of statements** for any number of countertops, as the countertop results ask
them, except where a row with no result has a pairing whose status is `ambiguous` or `no_fit`:
there `plan_architect_row` asks once per such row whether the architect prints a usable dimension
on its page, exactly as the countertop results do. No stored set has such a pairing.

Source: issue #1273; phase 5 cabinets design §1, §5 (A2, P5-M1); review of #1279. Verification:
`tests/workflow/test_milan_items.py`, `tests/api/test_item_architect.py`.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.evidence.sides import ReadingSides, has_separate_architect_file
from app.models import (
    CheckRun,
    DrawingItem,
    DrawingView,
    Finding,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartDecision,
    PartProposalRow,
    RuleDefinition,
    RuleSnapshot,
    VerdictInput,
)
from verdict.outcomes import Outcome
from workflow.architect_match_records import effective_architect_matches
from workflow.architect_pairing_contract import EffectivePairing

# The batch form of `architect_views` (#1167), private to its module; the countertop results ask
# it the same way, so a row with no match costs no statement of its own.
from workflow.architect_pairing_records import _architect_views_by_page
from workflow.architect_row_plan import (
    ARCHITECT_CHECK_RULE_ID,
    Disposition,
    PairingJudgments,
    PairingLookup,
    architect_file_indexed,
    architect_matches_waiting_for_run,
    effective_architect_pairings,
    pair_label,
    pairing_judgments,
    pairing_source_from_notes,
    plan_architect_row,
)
from workflow.item_plan import LiveCountertop, live_countertops
from workflow.slot_row_scope import SlotRow, slot_rows_and_unchosen_pages

__all__ = [
    "NOT_CHECKED_YET",
    "NO_ROW_FOR_ARCHITECT",
    "ROW_NOT_IN_READING",
    "ArchitectState",
    "ItemArchitectStatus",
    "ItemPair",
    "architect_status_for_item",
    "architect_statuses_for_revision",
]

type ArchitectState = Literal["compared", "for_reviewer", "not_checked_yet", "not_compared"]
"""As the countertop results show the row: `compared` (a recorded PASS or FAIL), `for_reviewer` (a
recorded result the check could not decide; whether it still waits is the review's readiness),
`not_checked_yet` (no result, and the row has something a check run would compare or ask about),
`not_compared` (no result, and nothing comparable)."""

#: The countertop results' own words for a row with no recorded result that a check run would
#: compare or ask about (`app/api/visual_countertops.NOT_CHECKED_YET`; a test keeps them equal).
NOT_CHECKED_YET: Final = (
    "Not checked yet: run the checks to compare this row with the architect's drawing."
)
#: A recorded operand of the architect check: `vendor_overall`, `architect_piece[2]`, …
_OPERAND: Final = re.compile(r"^(architect|vendor)_(overall|piece\[(\d+)\])$")

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


@dataclass(frozen=True, slots=True)
class ItemPair:
    """One width the check compared with the architect: the overall, or one vendor piece."""

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


@dataclass(frozen=True, slots=True)
class ItemArchitectStatus:
    """Where one live countertop stands with the architect's drawing."""

    countertop: LiveCountertop
    state: ArchitectState
    reason: str | None
    """The recorded result's reason; with no result, why it is not compared, or `NOT_CHECKED_YET`."""
    pairs: tuple[ItemPair, ...]
    """The pairs the recorded result compared, overall first; empty with no result."""
    pairing_source: str | None
    """Who paired its row (`code+ais`, `code`, `both-ais`, `reviewer`, `none`): as the recorded
    result noted it, else the effective pairing's; `None` when neither says."""
    pairing_judgments: PairingJudgments | None
    finding_id: UUID | None = None
    """The row's live CT-ARCH-WIDTH-001 result; `None` when the check recorded none."""
    outcome: Outcome | None = None
    """That result's outcome."""


def architect_statuses_for_revision(
    session: Session,
    package_revision_id: UUID,
    *,
    pairing_lookup: PairingLookup | None = None,
) -> tuple[ItemArchitectStatus, ...]:
    """Every live countertop of the revision with its architect comparison, in page order.

    `pairing_lookup` answers the effective pairing of a row with no recorded result; by default
    every such row's is read in one batch (`effective_architect_pairings`), as the countertop
    results read it.
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
    # The row's recorded result first: a row the check recorded a result for is said as recorded,
    # so only the others need their pairing (as the countertop results read them).
    recorded = _recorded(session, package_revision_id, found)
    if pairing_lookup is None:
        unrecorded = [anchor for anchor in found if anchor not in recorded]
        pairings = effective_architect_pairings(session, unrecorded) if unrecorded else {}

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
        result = recorded.get(row.anchor.id)
        if result is not None:
            finding, slots = result
            source = pairing_source_from_notes(finding.notes)
            outcome = Outcome(finding.outcome)
            statuses.append(
                ItemArchitectStatus(
                    countertop=countertop,
                    state=(
                        "compared" if outcome in (Outcome.PASS, Outcome.FAIL) else "for_reviewer"
                    ),
                    reason=finding.reason,
                    pairs=tuple(_item_pair(slot, pieces, row.anchor.id) for slot in slots),
                    pairing_source=source,
                    pairing_judgments=pairing_judgments(source),
                    finding_id=finding.id,
                    outcome=outcome,
                )
            )
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
        source = None if pairing is None else pairing.source
        statuses.append(
            ItemArchitectStatus(
                countertop=countertop,
                state=(
                    "not_compared"
                    if plan.disposition is Disposition.NOT_COMPARED
                    else "not_checked_yet"
                ),
                reason=(
                    plan.reason if plan.disposition is Disposition.NOT_COMPARED else NOT_CHECKED_YET
                ),
                pairs=(),
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


def _item_pair(slot: int | None, pieces: _Pieces, row_anchor_id: UUID) -> ItemPair:
    """One recorded pair, its vendor piece named by the piece item of the same row and place."""
    piece = None if slot is None else pieces.get((row_anchor_id, slot))
    return ItemPair(
        kind="overall" if slot is None else "piece",
        vendor_slot=slot,
        label=pair_label("overall" if slot is None else "piece", slot),
        piece_item_id=None if piece is None else piece[0],
        piece_item_type=None if piece is None else piece[1],
    )


def _recorded(
    session: Session, package_revision_id: UUID, row_anchor_ids: Collection[UUID]
) -> dict[UUID, tuple[Finding, tuple[int | None, ...]]]:
    """Each listed row's live CT-ARCH-WIDTH-001 result, with the places it compared (`None` for
    the overall, then the pieces from the left), read from its recorded operands as the countertop
    results read them. Two statements, whatever the number of rows."""
    if not row_anchor_ids:
        return {}
    findings = {
        finding.scope_row_candidate_id: finding
        for finding in session.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(
                Finding.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
                Finding.scope_row_candidate_id.in_(tuple(row_anchor_ids)),
                RuleDefinition.rule_id == ARCHITECT_CHECK_RULE_ID,
            )
        )
        if finding.scope_row_candidate_id is not None
    }
    if not findings:
        return {}
    places: dict[UUID, set[int | None]] = {}
    for check_run_id, name in session.execute(
        select(VerdictInput.check_run_id, VerdictInput.operand_name).where(
            VerdictInput.check_run_id.in_({f.check_run_id for f in findings.values()})
        )
    ):
        operand = _OPERAND.match(name)
        if operand is not None:
            slot = operand.group(3)
            places.setdefault(check_run_id, set()).add(None if slot is None else int(slot))
    return {
        row_id: (
            finding,
            tuple(
                sorted(
                    places.get(finding.check_run_id, set()),
                    key=lambda slot: -1 if slot is None else slot,
                )
            ),
        )
        for row_id, finding in findings.items()
    }


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
