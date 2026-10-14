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

**Suggestions, and one narrow automatic case (#1244).** `propose_items` writes no item. Only a
confirmation makes one (`workflow/parts.py:confirm_part`, guarded by
`tests/db/test_drawing_models.py`): a person's, or, behind `GV_BALA_AUTO_ITEMS` (off by default),
code's own for a **countertop** whose two independent judgments agree (`WouldBeAutomatic`,
`confirm_automatic_countertops`; Anant's decision 1 of 2026-10-14, the ADR-0021 amendment):

* **the AI readers**: both row readers named this very row (`row-choice:<n>`, n > 0, on every
  reading of it), so code did not pick it; its page is not one they split on; it carries no hold of
  any kind (`row-hold:`, `row-ambiguous`, missing widths, a check hold, GV's markup over it); no
  reader said no stone is drawn over it; Rekha's gate did not stop it; and no reviewer row decision
  typed widths into it;
* **code**: the row's printed overall exists and the row's pieces tile it (no `pieces do not tile
  the overall`), one reading per piece, as many as `row-slot-count`, their boxes meeting end to end,
  every one and the overall sealed by both readers; and the overall and every piece lie wholly
  inside exactly one drawing (`ReadingSides.drawings_holding`), whose role a confirmation (a
  person's, or code's two-judgment one) says is the vendor's.

Either judgment missing leaves a suggestion for a person's one click, with the reason recorded. A
later read that no longer agrees, a newer reading, or a person's later decision on a countertop at
the same place takes back only code's own item (`release_contradicted_code_part`), and only one
this revision's reading made: never another revision's, on a drawing the two share (review of
#1248), and never one a person confirmed or withdrew a run beneath (re-review of #1248). Where a
live countertop's outline overlaps, code makes none. **Pieces are never automatic**
(decision 2): nothing printed names 130 of every 131 of them, and even a named one stays a person's
click in this version. Automatic items cannot create a PASS or change a finding: the countertop
checks are scoped only to a countertop a person confirmed or confirmed a run beneath
(`workflow/part_operands.py:countertop_scopes`).

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

**Code pre-fills "cabinet" from one printed tag** (#1266, Anant's decision 5 of 2026-10-14),
behind `GV_BALA_CABINET_PREFILL` (off by default). Where code's part finder found a cabinet with a
piece's two ends and attached exactly one cabinet tag to it, and that tag stands among the lowest
cabinet tags printed over the row (so a wall cabinet's tag never counts), the piece is suggested as
a named cabinet: the Measure page pre-selects it and a person still confirms or changes it. A
pre-fill is a suggestion's kind and nothing more: no confirmation, no item, never automatic. A
label naming another kind and a tag name the piece two ways, so it stays unknown, naming both.

**No extraction imports**, so the read-only dry run (`scripts/bala_dry_run.py`) can use the plan.

Source: issue #1236; phase 4 design (§2, §5 PR 1); Anant's decisions of 2026-10-14. Verification:
tests/workflow/test_bala_items.py, tests/db/test_drawing_models.py.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final, cast
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.evidence.sides import ReadingSides
from app.models import (
    DrawingView,
    ObservationCandidate,
    Page,
    PartConfirmation,
    PartDecision,
    PartProposal,
    PartProposalRow,
    ViewRole,
    ViewRoleConfirmation,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.review.row_location import slot_location
from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon
from vocabulary.check_holds import NO_STONE_FLAG
from vocabulary.part_kinds import PartKind
from workflow.pariksha_gates import stopped_by_rekha
from workflow.parts import (
    A_PERSON_DECIDED_HERE,
    A_PERSONS_RUN_BENEATH,
    ALREADY_DECIDED,
    BALA_CONFIRMER,
    CODE_ACTOR_PREFIX,
    confirm_part_by_code,
    outline_box,
    person_decided_countertop_here,
    person_run_beneath,
    record_part_proposal,
    release_contradicted_code_part,
    why_code_may_not_confirm,
)
from workflow.slot_row_scope import (
    SlotRow,
    UnchosenRowPage,
    candidate_is_sealed,
    latest_slot_reader_run,
    slot_rows_and_unchosen_pages,
)

__all__ = [
    "AUTOMATIC_REASON",
    "BALA_SOURCE",
    "BALA_VERSION",
    "CABINET_TAG_PREFIXES",
    "FROM_THE_COUNTERTOP_LINE",
    "PIECES_STAY_ONE_CLICK",
    "PRINTED_ON_IT",
    "TAG_NAMES_IT",
    "AutomaticOutcome",
    "BalaOutcome",
    "CodeFinding",
    "CodePart",
    "CodeParts",
    "PlannedPart",
    "PlannedRow",
    "PrintedCode",
    "WouldBeAutomatic",
    "confirm_automatic_countertops",
    "plan_items",
    "propose_items",
]

#: What filed the suggestion, on `part_proposals.source`, so the Measure page can say so.
BALA_SOURCE: Final = "bala.slot-row"
BALA_VERSION: Final = "1"

#: How every one of Bala's reasons begins: the words the Measure page shows (#1236).
FROM_THE_COUNTERTOP_LINE: Final = "Suggested by code from the countertop line"

#: Why code confirmed a countertop with no click (#1244): both judgments, in words.
AUTOMATIC_REASON: Final = (
    "Both AI readers named this row, and code finds it wholly inside one drawing confirmed as the "
    "vendor's, outlined by its printed overall length, which its pieces tile end to end."
)

#: How a piece pre-filled from its printed cabinet tag says so (#1266): "... because the drawing
#: prints the cabinet tag 'B36' on it, ...". The Measure page pre-selects "cabinet"; a person still
#: confirms or changes it.
TAG_NAMES_IT: Final = "the drawing prints the cabinet tag"
PRINTED_ON_IT: Final = (
    "on it (code pre-filled cabinet from that one tag; confirm it, or change it if it is not)"
)

#: The letter codes a cabinet tag may start with to pre-fill "cabinet" on a countertop row's piece
#: (#1266; Anant's decision of 2026-10-15: an allow-list). A tag's shape alone
#: (`vocabulary/cabinet_codes.py`, which other callers keep as it is) also fits appliances (DW, MW,
#: HD), fillers (BF, WF, F) and panels (EP, FP, RF, RP), so only these name a cabinet here. Each is
#: a base cabinet, the kind a countertop row's piece is; every one but DB is printed on pieces the
#: stored, person-confirmed form keys call cabinets, and DB is the usual spelling of BD.
#:
#: * `B`: base cabinet.
#: * `BC`: base corner cabinet.
#: * `BD`: base drawer cabinet (a drawer stack).
#: * `DB`: drawer base cabinet, the same as BD.
#: * `SB`: sink base cabinet.
#:
#: Left out on purpose: wall cabinets (W, and open shelves), whose tag over a countertop row's
#: piece is never that piece's own, and tall cabinets, which no stored key shows on a countertop
#: row and whose letter codes vary by maker. Anything left out stays unknown, with its reason.
CABINET_TAG_PREFIXES: Final[Mapping[str, str]] = {
    "B": "base cabinet",
    "BC": "base corner cabinet",
    "BD": "base drawer cabinet",
    "DB": "drawer base cabinet",
    "SB": "sink base cabinet",
}

_LEADING_LETTERS: Final = re.compile(r"[A-Z]+")

#: Why no piece is ever automatic (decision 2 of 2026-10-14).
PIECES_STAY_ONE_CLICK: Final = "a piece stays a person's click (decision 2 of 2026-10-14)"

#: The geometry finding `extraction/geometry/rows.py:PIECES_DO_NOT_TILE`, as the reader records it
#: on the row (`row-finding:`): the printed overall reaches past the pieces. Restated, because this
#: module imports nothing from `extraction/`.
_PIECES_DO_NOT_TILE_FLAG: Final = "row-finding:pieces do not tile the overall"

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
class PrintedCode:
    """A cabinet code printed on the vendor's page (`vocabulary/cabinet_codes.py`), as printed, and
    its box in stored page space (#1266)."""

    text: str
    left: Decimal
    top: Decimal
    right: Decimal
    bottom: Decimal


@dataclass(frozen=True, slots=True)
class CodePart:
    """One part code's geometry-only finder suggests on a page: which drawing, what kind, its left
    and right ends in stored page space, and the cabinet code it attached, if any (#1266)."""

    view_id: UUID
    kind: PartKind
    left: Decimal
    right: Decimal
    code: PrintedCode | None = None
    """The cabinet code the finder attached: exactly one sits inside the part's span and no other
    part's (`extraction/model/part_proposals.py`). `None` for none, or for two."""


@dataclass(frozen=True, slots=True)
class CodeFinding:
    """What code's part finder found on one page, and the tolerance its ends are compared within
    (its own witness tolerance, in stored units)."""

    parts: tuple[CodePart, ...]
    tolerance: Decimal
    codes: tuple[PrintedCode, ...] = ()
    """Every cabinet code the finder was given on the page: the vendor's own text layers, one per
    print (#1266). Bala reads which of them stand lowest over a row."""


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
    """Whether code may make the row's countertop item with no click: both judgments agree (#1244).

    `countertop` is True only when the AI readers' judgment and code's both hold (the module
    docstring lists each condition); `countertop_why_not` names every one that does not. Acted on
    only behind `GV_BALA_AUTO_ITEMS` (`confirm_automatic_countertops`). **No piece is ever
    automatic** (`PIECES_STAY_ONE_CLICK`): `pieces` is always False, and its reasons also say what
    a piece's second judgment found, for the dry run.
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
    revision_id: UUID
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
    session: Session,
    revision_id: UUID,
    *,
    code_parts: CodeParts | None = None,
    cabinet_prefill: bool = False,
) -> BalaPlan:
    """What Bala would suggest for the revision's newest slot-reader run. **Reads only.**

    `cabinet_prefill` (`GV_BALA_CABINET_PREFILL`, #1266): a piece with exactly one cabinet tag
    printed on it, as code's part finder reads it, is suggested as a named cabinet (pre-filled, one
    click still needed); see `_printed_tag`. Off, the kinds are exactly what they were."""
    run_id = latest_slot_reader_run(session, revision_id)
    if run_id is None:
        return BalaPlan(revision_id=revision_id, run_id=None, rows=(), unchosen=())
    rows, unchosen = slot_rows_and_unchosen_pages(session, revision_id, run_id=run_id)
    run = session.get(ExtractionRun, run_id)
    if run is None:
        return BalaPlan(revision_id=revision_id, run_id=run_id, rows=(), unchosen=unchosen)
    found: dict[UUID, CodeFinding | str] = {}
    planned: list[PlannedRow] = []
    sides = ReadingSides(session)
    split = frozenset(page.page_number for page in unchosen if page.split)
    for row in rows:
        page = session.get(Page, row.anchor.page_id)
        if page is None:
            continue
        if page.id not in found:
            found[page.id] = (
                "code's part finder was not asked" if code_parts is None else code_parts(page)
            )
        planned.append(
            _plan_row(
                session,
                sides,
                split,
                row,
                page,
                run,
                found[page.id],
                cabinet_prefill=cabinet_prefill,
            )
        )
    return BalaPlan(revision_id=revision_id, run_id=run_id, rows=tuple(planned), unchosen=unchosen)


def propose_items(
    session: Session,
    revision_id: UUID,
    *,
    code_parts: CodeParts | None = None,
    cabinet_prefill: bool = False,
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

    **The cabinet pre-fill (`cabinet_prefill`, #1266) governs new filings only.** A suggestion
    already filed keeps the kind and `kind_named` it was filed with (its link is append-only):
    switching the pre-fill off does not take back pre-fills already filed (they stay one-click
    pre-selections a person must still confirm), and switching it on applies from the next reading.
    """
    plan = plan_items(session, revision_id, code_parts=code_parts, cabinet_prefill=cabinet_prefill)
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


@dataclass
class AutomaticOutcome:
    """What `confirm_automatic_countertops` did: code's countertop items made and taken back."""

    switched_on: bool
    confirmed: list[str] = field(default_factory=list)
    """Each countertop code confirmed, by its row's label."""
    left_to_a_person: list[str] = field(default_factory=list)
    """Rows whose two judgments agree but a decision already stands (a person always wins)."""
    released: list[str] = field(default_factory=list)
    """Code's own items taken back, by row, with why."""
    kept: list[str] = field(default_factory=list)
    """Code's own items it would have taken back but kept, because a person confirmed or withdrew
    a run beneath them (re-review of #1248), by row, with why it would have."""

    def as_payload(self) -> dict[str, object]:
        return {
            "switched_on": self.switched_on,
            "countertops_confirmed": len(self.confirmed),
            "confirmed": self.confirmed,
            "left_to_a_person": self.left_to_a_person,
            "released": self.released,
            "kept": self.kept,
        }


def confirm_automatic_countertops(
    session: Session, outcome: BalaOutcome, *, switched_on: bool
) -> AutomaticOutcome:
    """Code's countertop items for the rows whose two judgments agree, and code's own items taken
    back where they no longer do (#1244). Call after `propose_items`, with its outcome.

    **Taking back runs whatever the switch says**, and only on **this revision's** readings (review
    of #1248): an item of code's whose link names a slot-reader run of this revision is withdrawn as
    `BALA_CONFIRMER` (`release_contradicted_code_part`) when a newer run of this revision replaced
    its row, when the newest read no longer gives both judgments, or when a person has since
    decided a countertop at the same place on the same drawing (a person always wins). **Never one a
    person confirmed or withdrew a run beneath** (re-review of #1248): that run is a person's
    decision about this countertop, so the item stays, and `kept` says why. A person's
    decision is never touched, and an item another revision's reading made is never this
    revision's to take back, even on a drawing the two share: that revision may be signed off.
    **Making items needs `switched_on`** (`GV_BALA_AUTO_ITEMS`, off by default): for each row whose
    `WouldBeAutomatic.countertop` holds, its countertop suggestion is confirmed through
    `confirm_part_by_code`, which writes nothing where any decision already stands, where a person
    decided a countertop at the same place, or where any live countertop stands (another
    revision's), and the reason is recorded. Pieces never.
    """
    plan = outcome.plan
    result = AutomaticOutcome(switched_on=switched_on)
    rows = {row.row_candidate_id: row for row in plan.rows}
    if plan.run_id is None:
        return result
    later = aliased(PartConfirmation)
    held_by_code = session.execute(
        select(PartProposal, PartProposalRow, PartConfirmation)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .join(PartConfirmation, PartConfirmation.part_proposal_id == PartProposal.id)
        .join(ExtractionRun, ExtractionRun.id == PartProposalRow.extraction_run_id)
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(
            WorkflowRun.package_revision_id == plan.revision_id,
            PartProposalRow.slot_index.is_(None),
            PartConfirmation.confirmed_by == BALA_CONFIRMER,
            PartConfirmation.decision == PartDecision.CONFIRMED.value,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
        .order_by(PartProposal.created_at, PartProposal.id)
    ).all()
    for proposal, link, decision in held_by_code:
        row = rows.get(link.row_candidate_id)
        if link.extraction_run_id != plan.run_id:
            why = "a newer reading of the countertop rows replaced it"
        elif row is None or not row.automatic.countertop:
            why = "; ".join(
                row.automatic.countertop_why_not if row is not None else ("its row is gone",)
            )
        elif person_decided_countertop_here(session, proposal):
            why = A_PERSON_DECIDED_HERE
        else:
            continue
        if person_run_beneath(session, decision):
            result.kept.append(f"{_label(session, row, link)}: {A_PERSONS_RUN_BENEATH} ({why})")
        elif release_contradicted_code_part(session, proposal=proposal, judgments_agree=False):
            result.released.append(f"{_label(session, row, link)}: {why}")
    if not switched_on:
        return result
    for row in plan.rows:
        if row.not_proposed is not None or not row.automatic.countertop:
            continue
        proposal_id = outcome.proposals.get((row.row_candidate_id, None))
        if proposal_id is None:
            result.left_to_a_person.append(f"{row.label}: a person already decided it")
            continue
        proposal = session.get_one(PartProposal, proposal_id)
        why_not = why_code_may_not_confirm(session, proposal)
        made = (
            None
            if why_not is not None
            else confirm_part_by_code(
                session, proposal=proposal, kind=PartKind.COUNTERTOP, reason=AUTOMATIC_REASON
            )
        )
        if made is None:
            result.left_to_a_person.append(f"{row.label}: {why_not or ALREADY_DECIDED}")
        else:
            result.confirmed.append(row.label)
    session.flush()
    return result


def _label(session: Session, row: PlannedRow | None, link: PartProposalRow) -> str:
    """The row's label, or for a row of an earlier reading, its page and that it is earlier."""
    if row is not None:
        return row.label
    page_index = session.execute(
        select(Page.index)
        .join(ObservationCandidate, ObservationCandidate.page_id == Page.id)
        .where(ObservationCandidate.id == link.row_candidate_id)
    ).scalar_one()
    return f"Countertop row on page {page_index + 1} (an earlier reading)"


def _decided_outlines(session: Session, plan: BalaPlan) -> dict[UUID, list[tuple[bool, _Box]]]:
    """By drawing: the outline of every Bala suggestion from another reading that a person's
    current decision stands on, and whether it is a countertop.

    A person's only: code's own decision (#1244) is no reason to stop suggesting the part again.
    Its item from an older reading is taken back by `confirm_automatic_countertops`, and the newer
    reading's suggestion is the one that counts."""
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
            ~PartConfirmation.confirmed_by.startswith(CODE_ACTOR_PREFIX),
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
    sides: ReadingSides,
    split: frozenset[int],
    row: SlotRow,
    page: Page,
    run: ExtractionRun,
    code: CodeFinding | str,
    *,
    cabinet_prefill: bool = False,
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
        tag: PrintedCode | str | None = None
        if cabinet_prefill:
            tag = (
                _printed_tag(view, box, countertop_box, code)
                if not isinstance(code, str)
                else "code's part finder did not run, so no printed tag was read"
            )
        kind, named, why = _kind(candidate, tag)
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
        automatic=_would_be_automatic(
            _Judged(
                session=session,
                sides=sides,
                split=split,
                row=row,
                page=page,
                view=view,
                by_slot=by_slot,
                overall=(
                    None if overall is None or overall_box is None else (overall, overall_box)
                ),
            ),
            pieces,
            parts,
            code,
        ),
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
    """Whether the row readers named this row: every reading of it carries the one `row-choice:<n>`,
    n > 0, which the reader writes only when every row reader named the same line
    (`workflow/slot_reader.py:_agreed_row`). Otherwise code chose it."""
    choices: set[str] = set()
    for candidate in row.candidates:
        found = [
            flag.removeprefix("row-choice:")
            for flag in candidate.ambiguity_flags or ()
            if flag.startswith("row-choice:")
        ]
        if len(found) != 1:
            return False
        choices.add(found[0])
    if len(choices) != 1:
        return False
    (number,) = choices
    return number.isdigit() and int(number) > 0


def _row_held(row: SlotRow) -> bool:
    return any(flag == "row-ambiguous" or flag.startswith("row-hold:") for flag in _flags(row))


def _states(row: SlotRow) -> tuple[str, ...]:
    states: list[str] = []
    if row.held_reason is not None:
        states.append(f"Held: {row.held_reason.rstrip('.')}")
    if not _chosen_by_the_readers(row):
        states.append("The AI readers did not choose this row; code did")
    return tuple(states)


def _kind(
    candidate: ObservationCandidate, tag: PrintedCode | str | None = None
) -> tuple[PartKind, bool, str]:
    """The suggested kind, whether printed evidence named it, and why, in plain words.

    `tag` is the cabinet tag printed on the piece (`_printed_tag`), or why none counts, or `None`
    where the pre-fill is off (#1266). A tag names a cabinet only where the label names no other
    kind: a label and a tag that disagree name it two ways, so it stays a person's to say, naming
    both."""
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
    if isinstance(tag, PrintedCode):
        if printed in ("", "unknown"):
            return PartKind.CABINET, True, f"{TAG_NAMES_IT} {tag.text!r} {PRINTED_ON_IT}"
        if printed != "cabinet":
            return (
                PartKind.CABINET,
                False,
                (
                    f"the drawing names it two ways: its label says {printed} "
                    f"({evidence or printed}) and the cabinet tag {tag.text!r} is printed on it; "
                    "say what it is"
                ),
            )
    if printed in _NAMED_KINDS:
        return _NAMED_KINDS[printed], True, f"the drawing names it ({evidence or printed})"
    if printed in _OTHER_KINDS:
        return PartKind.CABINET, False, _OTHER_KINDS[printed]
    if isinstance(tag, str):
        return PartKind.CABINET, False, f"nothing printed names its kind ({tag}); say what it is"
    return PartKind.CABINET, False, "nothing printed names its kind; say what it is"


def _printed_tag(view: DrawingView, box: _Box, row: _Box, code: CodeFinding) -> PrintedCode | str:
    """The one cabinet tag printed on the piece, or why none counts (#1266, P5-K1).

    1. Code's part finder found a cabinet on the piece's drawing with the piece's two ends (within
       its tolerance) and attached exactly one cabinet code to it: one inside its span and no other
       part's (`extraction/model/part_proposals.py`).
    2. The code starts with a base-cabinet letter code (`CABINET_TAG_PREFIXES`). A code's shape
       alone also fits appliances, fillers and panels (a dishwasher, a base filler, an end panel),
       so any other letter code names nothing.
    3. It is printed above the piece's dimension, where the cabinet's face is.
    4. **No cabinet code printed over the row** (`row`, the countertop's outline, left to right,
       inside the drawing, above or below the dimensions) **stands lower than it by more than its
       own height** (the smaller side of its box, so a turned tag is measured across its text). A
       tag with one lower stands higher up the wall: a wall cabinet's.
    5. It is **not the only cabinet code printed above the row's dimensions** on the drawing: with
       nothing to compare it with, a lone tag could be a wall cabinet's over a piece that carries
       none of its own.

    The finder reads only across the page, so without 3 to 5 a wall cabinet's tag above an
    appliance space or a panel would count.
    """
    same = [
        part
        for part in code.parts
        if part.view_id == view.id
        and part.kind is PartKind.CABINET
        and abs(part.left - box.left) <= code.tolerance
        and abs(part.right - box.right) <= code.tolerance
    ]
    if not same:
        return "code's part finder found no cabinet with its ends"
    tags = {part.code for part in same if part.code is not None}
    if not tags:
        return "code's part finder found no one cabinet tag inside its span alone"
    if len(tags) > 1:
        named = ", ".join(sorted(repr(tag.text) for tag in tags))
        return f"code's part finder read more than one tag on it ({named})"
    (tag,) = tags
    prefix = _letters(tag.text)
    if prefix not in CABINET_TAG_PREFIXES:
        return (
            f"the code {tag.text!r} printed on it does not start with a base-cabinet letter code "
            f"({', '.join(CABINET_TAG_PREFIXES)}), so it may name an appliance, a filler, a panel, or a "
            "wall or tall cabinet"
        )
    if tag.bottom > box.top:
        return f"the tag {tag.text!r} is not printed above its dimension"
    region = _region_box(view)
    over_row = [
        other
        for other in code.codes
        if other != tag
        and region is not None
        and row.left <= other.left
        and other.right <= row.right
        and region.top <= other.top
        and other.bottom <= region.bottom
    ]
    height = min(tag.bottom - tag.top, tag.right - tag.left)
    lower = sorted({other.text for other in over_row if other.top > tag.bottom + height})
    if lower:
        return (
            f"the tag {tag.text!r} over it stands higher up the wall than "
            f"{', '.join(repr(text) for text in lower)} printed over the same row, so it is a "
            "wall cabinet's"
        )
    if not any(other.bottom <= box.top for other in over_row):
        return (
            f"the tag {tag.text!r} is the only cabinet code printed over the row, so nothing shows "
            "it is the row's own and not a wall cabinet's above it"
        )
    return tag


def _letters(code: str) -> str:
    """A cabinet code's leading capital letters: `"SB"` for a sink base's code."""
    found = _LEADING_LETTERS.match(code)
    return "" if found is None else found.group(0)


def _reason(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 1] + "…"


@dataclass(frozen=True, slots=True)
class _Judged:
    """What the two judgments on one row read."""

    session: Session
    sides: ReadingSides
    split: frozenset[int]
    row: SlotRow
    page: Page
    view: DrawingView
    by_slot: Mapping[str, ObservationCandidate]
    overall: tuple[ObservationCandidate, _Box] | None


def _would_be_automatic(
    judged: _Judged,
    pieces: Sequence[tuple[int, ObservationCandidate, _Box | None]],
    parts: Sequence[PlannedPart],
    code: CodeFinding | str,
) -> WouldBeAutomatic:
    why_not = [*_readers_judgment(judged), *_codes_judgment(judged, pieces)]
    countertop = not why_not

    piece_parts = {part.slot_index: part for part in parts if part.slot_index is not None}
    automatic: list[bool] = []
    reasons: list[tuple[str, ...]] = []
    agrees: list[bool | None] = []
    for index, candidate, box in pieces:
        part = piece_parts.get(index)
        piece_why: list[str] = [PIECES_STAY_ONE_CLICK]
        if not countertop:
            piece_why.append("its countertop does not qualify")
        if not candidate_is_sealed(candidate):
            piece_why.append("its reading is not sealed")
        if part is None or not part.kind_named:
            piece_why.append("nothing printed names its kind")
        agreed = (
            None if isinstance(code, str) or box is None else _code_agrees(judged.view, box, code)
        )
        agrees.append(agreed)
        if agreed is None:
            piece_why.append("code's part finder did not run")
        elif not agreed:
            piece_why.append("code's part finder found no cabinet with the same ends")
        automatic.append(False)
        reasons.append(tuple(piece_why))
    return WouldBeAutomatic(
        countertop=countertop,
        countertop_why_not=tuple(why_not),
        pieces=tuple(automatic),
        pieces_why_not=tuple(reasons),
        pieces_code_agrees=tuple(agrees),
    )


def _readers_judgment(judged: _Judged) -> list[str]:
    """Judgment 1, the AI readers': why it does not hold, or nothing when it does."""
    row = judged.row
    why: list[str] = []
    if not _chosen_by_the_readers(row):
        why.append("the AI readers did not both name this row")
    if row.page_number in judged.split:
        why.append("the AI readers split on this page")
    flags = _flags(row)
    if _row_held(row):
        why.append("the row is held")
    if row.held_reason is not None:
        # Every hold, including GV's markup over the row, a missing width and a check hold: a person
        # looks at a held row anyway, and code decides nothing a person has been asked about.
        why.append(f"the row waits for the reviewer ({row.held_reason.rstrip('.')})")
    if "reviewer-markup" in flags:
        why.append("GV's markup is drawn over the row, and GV's markup is never a reading")
    if any(flag.startswith(NO_STONE_FLAG) for flag in flags):
        why.append("a reader said no stone top is drawn over this row")
    if stopped_by_rekha(row.anchor.ambiguity_flags):
        why.append("Rekha's gate stopped the row")
    decision = row.decision
    if decision is not None and decision.measurements:
        why.append("a reviewer typed widths into this row")
    return why


def _codes_judgment(
    judged: _Judged, pieces: Sequence[tuple[int, ObservationCandidate, _Box | None]]
) -> list[str]:
    """Judgment 2, code's: why it does not hold, or nothing when it does."""
    row = judged.row
    why: list[str] = []
    if judged.overall is None:
        why.append(
            "no overall length is printed for it (or it could not be placed), so its outline "
            "would be its pieces' own"
        )
    if _PIECES_DO_NOT_TILE_FLAG in _flags(row):
        why.append("its pieces do not tile its printed overall")
    if not _one_reading_per_slot(row):
        why.append(
            f"it does not have exactly one reading for each of its {row.piece_count} pieces and "
            "its overall"
        )
    boxes = [box for _, _, box in pieces]
    if any(box is None for box in boxes) or not _end_to_end(judged.by_slot, row.piece_count):
        why.append("its pieces do not run end to end")
    readings = [candidate for _, candidate, _ in pieces]
    if judged.overall is not None:
        readings.append(judged.overall[0])
    if not readings or not all(candidate_is_sealed(candidate) for candidate in readings):
        why.append("a width in the row is not sealed by both readers")
    placed = [box for box in boxes if box is not None]
    if judged.overall is not None:
        placed.append(judged.overall[1])
    holding = {_holding(judged, box) for box in placed}
    if not placed or holding != {judged.view.id}:
        why.append("code does not find the whole row inside exactly one drawing")
    if not _vendors_by_a_confirmation(judged.session, judged.view):
        why.append("no confirmation says its drawing is the vendor's")
    return why


def _one_reading_per_slot(row: SlotRow) -> bool:
    """Exactly one reading for each piece `0 … row-slot-count - 1` and one for the overall."""
    counts: dict[str, int] = {}
    for candidate in row.candidates:
        for flag in candidate.ambiguity_flags or ():
            if flag.startswith("slot:"):
                slot = flag.removeprefix("slot:")
                counts[slot] = counts.get(slot, 0) + 1
    expected = {str(index) for index in range(row.piece_count)} | {"overall"}
    return row.piece_count > 0 and set(counts) == expected and set(counts.values()) == {1}


def _holding(judged: _Judged, box: _Box) -> UUID | None:
    """The one drawing that holds the box, as every side is decided (`ReadingSides`), or `None`."""
    try:
        polygon = Polygon(
            points=tuple(
                StoredPoint(x=x, y=y)
                for x, y in ((box.left, box.top), (box.right, box.top), (box.right, box.bottom))
            )
            + (StoredPoint(x=box.left, y=box.bottom),),
            space="stored",
            document_version_id=judged.page.document_version_id,
            page=judged.page.index,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None
    holding = judged.sides.drawings_holding(judged.page, polygon)
    if holding is None or len(holding) != 1:
        return None
    return holding[0].id


def _vendors_by_a_confirmation(session: Session, view: DrawingView) -> bool:
    """Whether the drawing is the vendor's by a confirmation: its role is SHOP, and the newest
    confirmation of it (a person's, or code's on two judgments, `workflow/view_roles.py`) says so.
    A role nothing confirmed, or one a later read withdrew, does not count."""
    if view.role != ViewRole.SHOP.value:
        return False
    latest = session.execute(
        select(ViewRoleConfirmation.role)
        .where(ViewRoleConfirmation.drawing_view_id == view.id)
        .order_by(ViewRoleConfirmation.created_at.desc(), ViewRoleConfirmation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return latest == ViewRole.SHOP.value


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
