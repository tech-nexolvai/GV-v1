"""Each live countertop item, and what is checked for it: the seed of Kautilya (#1245).

**What a countertop item is here.** A `drawing_items` row of type `countertop_overall_width` whose
current decision confirms it (`workflow/parts.py`), on one drawing view. Whoever made that decision
counts: a person's click, or, once #1248 lets code confirm, code's two-judgment confirmation
(`confirmed_by` starting `code:`). Nothing here reads who decided, and nothing here can turn a
result toward PASS, so code's items are read exactly as a person's are.

Bala suggests one countertop for every countertop row the slot reader read
(`workflow/bala_items.py`) and links the suggestion to the row (`part_proposal_rows`), so an item
confirmed from one of Bala's suggestions knows its row. An item confirmed from any other suggestion
has no row.

**Three readers use it, and none of them decides anything:**

* `workflow/evidence_operands.py` asks how many countertops each drawing holds
  (`countertops_by_view`): a reading on a drawing with one countertop belongs to that countertop, and
  on a drawing with two or more it belongs to none, so two countertops' cabinets are never summed.
  That only ever withholds, whoever confirmed the countertops.
* the results screen asks which countertop each row belongs to (`countertops_by_row`), to group the
  row's results under it. The results stay keyed by the row (`scope_row_candidate_id`), so a
  reviewer's decision still carries over to an unchanged re-run (decided by Anant, 2026-10-14).
* `check_plan_for` lists, for each countertop, the published rules for the package's product
  (`rules/applicability.resolve`), the results its row (or the item itself) has, and the values each
  rule's whole-package result is still waiting on (`app/review/waiting_values.py`).

**Read-only.** Nothing here writes a row, runs a check or asks a model. No extraction import, so the
API may read it (`tests/api/test_no_heavy_work.py`).

Source: issue #1245; phase 4 design §3 and §5 PR 3; review of #1247. Verification:
`tests/workflow/test_item_plan.py`, `tests/workflow/test_per_item_assembly.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    CheckRun,
    DrawingItem,
    DrawingView,
    ExtractionRun,
    Finding,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartDecision,
    PartProposal,
    PartProposalRow,
    RuleDefinition,
    RuleSnapshot,
)
from app.models.package import package_product
from app.review.waiting_values import WAITING_RULES, missing_inputs_for
from app.schemas.findings import MissingInputOut
from app.verdicts.rulebook import snapshot_store
from rules.applicability import CheckContext, resolve
from rules.parameters import ParameterLayer, ParameterSet
from rules.project import ProjectScope
from rules.semantic_types import ProductType
from verdict.outcomes import Outcome
from vocabulary.part_kinds import PartKind
from workflow.bala_items import _Box, _region_box, _slot_candidates, _stored_box
from workflow.parts import outline_box
from workflow.slot_row_scope import SlotRow, latest_slot_reader_run, slot_rows_and_unchosen_pages

__all__ = [
    "NO_ROW_LINKED",
    "CheckPlan",
    "ItemPlan",
    "LiveCountertop",
    "PlannedFinding",
    "PlannedRule",
    "check_plan_for",
    "countertops_by_row",
    "countertops_by_view",
    "live_countertops",
]

#: The item type of a countertop (`vocabulary/part_kinds.py`).
COUNTERTOP_ITEM_TYPE: Final = PartKind.COUNTERTOP.item_type.value

#: What a countertop with no countertop row says in its plan: it was confirmed from a suggestion
#: that is not one of Bala's, so no row's results belong to it.
NO_ROW_LINKED: Final = (
    "No countertop line is linked to this countertop: it was not confirmed from a suggestion made "
    "from a countertop line, so no row's results are listed under it."
)

#: Where an outline that will not read sorts: after every readable one, never in front of it.
_UNREADABLE: Final = (Decimal("Infinity"), Decimal("Infinity"))


@dataclass(frozen=True, slots=True)
class LiveCountertop:
    """One live countertop item, where it is, and the row it was confirmed from."""

    item_id: UUID
    view_id: UUID
    page_id: UUID
    page_index: int
    number: int
    """Its place among the live countertops on its page, `1` from the left."""
    row_candidate_id: UUID | None
    """The anchor of its countertop row in the newest reading: the row Bala suggested it from
    (`part_proposal_rows`), or, after a re-read, the newest row Bala skipped because of this
    countertop's decision and no other (`row_followed`, `_follow_to_newest_rows`); `None` when it
    was confirmed from another suggestion, or when no newest row is provably its own."""
    row_followed: bool = False
    """Whether `row_candidate_id` was found by following an older reading's link (a re-read)."""

    @property
    def page_number(self) -> int:
        return self.page_index + 1

    @property
    def label(self) -> str:
        """ "Countertop 1 on page 3": the same on the plan, the results screen and the run check."""
        return f"Countertop {self.number} on page {self.page_number}"


def live_countertops(
    session: Session, package_revision_id: UUID, *, follow_rows: bool = True
) -> tuple[LiveCountertop, ...]:
    """Every live countertop item on this revision's drawings, in page order, left to right.

    **Live** means its current decision confirms it as a countertop, whoever decided (a person, or
    code under #1248): a withdrawn or corrected item keeps its row (no role holds `DELETE`) and is
    not here. The row it came from is read through its confirmation's suggestion
    (`part_proposal_rows`, the countertop's own link: `slot_index` is `NULL`).

    **After a re-read** the newest slot-reader run has new rows, and Bala does not suggest again a
    countertop already decided: the new row has no link of its own, and the item's link names the
    older reading's row. With `follow_rows`, such an item is followed to the newest row only where
    that row is provably its own (`_follow_to_newest_rows`), so its results, which are keyed by the
    newest row, are still its own. `follow_rows=False` (the evidence step, which needs only the
    drawings) leaves `row_candidate_id` as the link says, and costs one statement. Following costs
    a fixed number of statements, whatever the number of rows.
    """
    later = aliased(PartConfirmation)
    rows = session.execute(
        select(
            DrawingItem.id,
            DrawingItem.drawing_view_id,
            DrawingItem.extent,
            Page.id,
            Page.index,
            PartProposalRow.row_candidate_id,
            PartProposalRow.extraction_run_id,
            PartConfirmation.part_proposal_id,
        )
        .join(PartConfirmation, PartConfirmation.drawing_item_id == DrawingItem.id)
        .join(DrawingView, DrawingView.id == DrawingItem.drawing_view_id)
        .join(Page, Page.id == DrawingView.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .outerjoin(
            PartProposalRow,
            (PartProposalRow.part_proposal_id == PartConfirmation.part_proposal_id)
            & PartProposalRow.slot_index.is_(None),
        )
        .where(
            PackageRevisionDocument.package_revision_id == package_revision_id,
            DrawingItem.item_type == COUNTERTOP_ITEM_TYPE,
            PartConfirmation.decision == PartDecision.CONFIRMED.value,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
    ).all()
    if not rows:
        return ()
    current: dict[UUID, UUID | None] = {row[0]: row[5] for row in rows}
    followed: set[UUID] = set()
    if follow_rows and any(row[6] is not None for row in rows):
        newest = latest_slot_reader_run(session, package_revision_id)
        stale = [row for row in rows if row[6] is not None and row[6] != newest]
        if stale and newest is not None:
            for item_id in (row[0] for row in stale):
                current[item_id] = None
            found = _follow_to_newest_rows(
                session,
                package_revision_id,
                newest,
                [_Stale(item_id=row[0], page_id=row[3], proposal_id=row[7]) for row in stale],
            )
            current.update(found)
            followed.update(found)

    def place(extent: Mapping[str, object]) -> tuple[Decimal, Decimal]:
        try:
            left, top, _right, _bottom = outline_box(extent)
        except ValueError:
            return _UNREADABLE
        return left, top

    ordered = sorted(rows, key=lambda row: (row[4], *place(row[2]), str(row[0])))
    counts: dict[UUID, int] = {}
    countertops: list[LiveCountertop] = []
    for item_id, view_id, _extent, page_id, page_index, _link, _run, _proposal in ordered:
        counts[page_id] = counts.get(page_id, 0) + 1
        countertops.append(
            LiveCountertop(
                item_id=item_id,
                view_id=view_id,
                page_id=page_id,
                page_index=page_index,
                number=counts[page_id],
                row_candidate_id=current[item_id],
                row_followed=item_id in followed,
            )
        )
    return tuple(countertops)


@dataclass(frozen=True, slots=True)
class _Stale:
    """A live countertop confirmed from an older reading's row."""

    item_id: UUID
    page_id: UUID
    proposal_id: UUID


def _follow_to_newest_rows(
    session: Session,
    package_revision_id: UUID,
    newest: UUID,
    stale: Sequence[_Stale],
) -> dict[UUID, UUID]:
    """Each countertop confirmed from an older reading's row, followed to the newest run's row that
    Bala skipped **because of this countertop's decision and no other**: the item's anchor in the
    newest run, by item (review of #1247, M1).

    **How Bala skips** (`bala_items.propose_items`): a newest row's countertop is not suggested again
    when an older reading's Bala countertop suggestion on the same drawing, with the same outline
    within the part finder's tolerance, has a current decision of any kind: confirmed, or withdrawn.
    So a newest row with no link of its own may have been skipped for a *withdrawn* suggestion, or
    for another countertop's. Following it by nearness alone could hang a rejected row's results
    under this countertop.

    **The rule, all of it required:**

    * this countertop's suggestion is **the only decided Bala countertop suggestion** from another
      reading on any drawing of its page. Decisions are never removed (a correction supersedes a
      decision on the same suggestion, so a decided suggestion stays decided), so the set of decided
      suggestions only grows: if this one is the only one now, it was the only one when Bala
      skipped, and Bala's skip on this page can only have been for it, within Bala's own tolerance,
      whatever that tolerance was. No tolerance needs to be stored or guessed;
    * the newest run has **exactly one** countertop row on the page with no countertop link of its
      own, so one row for this countertop and, by the first point, one countertop for this row;
    * Bala can outline that row exactly as it does (`bala_items`'s own helpers): every piece has a
      reading, its outline is placed, and exactly one drawing holds its line, the countertop's own;
    * its outline overlaps this countertop's suggested outline **along and across**.

    Anything else stays unlinked: the plan says so (`NO_ROW_LINKED`) and the run check names it.
    That only withholds a grouping; nothing here reaches a result.

    **A fixed number of statements**, whatever the number of rows or countertops: the decided
    suggestions with their outlines, the newest run's links, its rows, their pages and drawings,
    and the run.
    """
    pages = {entry.page_id for entry in stale}
    decided: dict[UUID, set[UUID]] = {}
    outlines: dict[UUID, PartProposal] = {}
    for proposal, page_id in session.execute(
        select(PartProposal, DrawingView.page_id)
        .join(DrawingView, DrawingView.id == PartProposal.drawing_view_id)
        .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
        .where(
            DrawingView.page_id.in_(pages),
            PartProposalRow.slot_index.is_(None),
            PartProposalRow.extraction_run_id != newest,
            exists().where(PartConfirmation.part_proposal_id == PartProposal.id),
        )
    ).all():
        decided.setdefault(page_id, set()).add(proposal.id)
        outlines[proposal.id] = proposal
    sole = {
        entry.page_id: entry for entry in stale if decided.get(entry.page_id) == {entry.proposal_id}
    }
    if not sole:
        return {}
    linked = set(
        session.scalars(
            select(PartProposalRow.row_candidate_id).where(
                PartProposalRow.extraction_run_id == newest,
                PartProposalRow.slot_index.is_(None),
            )
        )
    )
    unlinked: dict[UUID, list[SlotRow]] = {}
    for row in slot_rows_and_unchosen_pages(session, package_revision_id, run_id=newest)[0]:
        if row.anchor.page_id in sole and row.anchor.id not in linked:
            unlinked.setdefault(row.anchor.page_id, []).append(row)
    single = {page_id: rows[0] for page_id, rows in unlinked.items() if len(rows) == 1}
    if not single:
        return {}
    run = session.get(ExtractionRun, newest)
    if run is None:
        return {}
    page_by_id = {
        page.id: page for page in session.scalars(select(Page).where(Page.id.in_(single)))
    }
    views_by_page: dict[UUID, list[DrawingView]] = {}
    for view in session.scalars(
        select(DrawingView).where(DrawingView.page_id.in_(single)).order_by(DrawingView.id)
    ):
        views_by_page.setdefault(view.page_id, []).append(view)
    followed: dict[UUID, UUID] = {}
    for page_id, row in single.items():
        page = page_by_id.get(page_id)
        suggested = outlines.get(sole[page_id].proposal_id)
        if page is None or suggested is None:
            continue
        box = _bala_outline(row, page, run)
        if box is None:
            continue
        holding = [view.id for view in views_by_page.get(page_id, ()) if _holds_line(view, box)]
        if holding != [suggested.drawing_view_id]:
            continue
        try:
            left, top, right, bottom = outline_box(suggested.extent)
        except ValueError:
            continue
        if box.right < left or right < box.left or box.bottom < top or bottom < box.top:
            continue
        followed[sole[page_id].item_id] = row.anchor.id
    return followed


def _bala_outline(row: SlotRow, page: Page, run: ExtractionRun) -> _Box | None:
    """The countertop outline Bala draws for `row` (`bala_items._plan_row`), or `None` where Bala
    would not suggest it: a piece with no reading, no pieces, or boxes that cannot be placed."""
    by_slot = _slot_candidates(row)
    if row.piece_count < 1:
        return None
    pieces: list[_Box | None] = []
    for index in range(row.piece_count):
        candidate = by_slot.get(str(index))
        if candidate is None:
            return None
        pieces.append(_stored_box(candidate, page, run))
    overall = by_slot.get("overall")
    overall_box = None if overall is None else _stored_box(overall, page, run)
    if overall_box is not None:
        return overall_box
    placed = [box for box in pieces if box is not None]
    if len(placed) != len(pieces):
        return None
    return _Box(
        left=min(box.left for box in placed),
        top=min(box.top for box in placed),
        right=max(box.right for box in placed),
        bottom=max(box.bottom for box in placed),
    )


def _holds_line(view: DrawingView, box: _Box) -> bool:
    """Whether the drawing holds the row's line, end to end, as Bala places a row
    (`bala_items._views_holding`)."""
    region = _region_box(view)
    return (
        region is not None
        and region.left <= box.left
        and box.right <= region.right
        and region.top <= box.middle <= region.bottom
    )


def countertops_by_view(
    session: Session, package_revision_id: UUID
) -> dict[UUID, tuple[UUID, ...]]:
    """Each drawing view of the revision that holds a live countertop item, with those items."""
    held: dict[UUID, list[UUID]] = {}
    for countertop in live_countertops(session, package_revision_id, follow_rows=False):
        held.setdefault(countertop.view_id, []).append(countertop.item_id)
    return {view_id: tuple(items) for view_id, items in held.items()}


def countertops_by_row(session: Session, package_revision_id: UUID) -> dict[UUID, LiveCountertop]:
    """Each countertop row's live countertop item, by the row's anchor reading.

    A row has at most one: Bala files one countertop suggestion per row (unique in
    `part_proposal_rows`), and a suggestion has at most one current decision.
    """
    return {
        countertop.row_candidate_id: countertop
        for countertop in live_countertops(session, package_revision_id)
        if countertop.row_candidate_id is not None
    }


@dataclass(frozen=True, slots=True)
class PlannedFinding:
    """One live result that belongs to a countertop: its row's, or the item's own."""

    finding_id: UUID
    rule_id: str
    outcome: Outcome
    scope: str
    """`row` for a result keyed by the countertop's row, `item` for one keyed by the item."""


@dataclass(frozen=True, slots=True)
class PlannedRule:
    """One published rule for the package's product, and where it stands for this countertop."""

    rule_id: str
    resolved: bool
    """Whether it is known which version and variant of the rule applies to this countertop: from
    this countertop's own result for it when it has one (the checks chose them from its row's
    walls), else from what is stated alone (the resolver)."""
    waits_for: str | None
    """Only when it is not known: the resolver's own reason (`rules/applicability.Abstention`)."""
    version: str | None
    """The rule's version: the one its own result was checked under, else the resolver's."""
    variant: str | None
    """The variant that applied (the wall layout's branch): its own result's, else the resolver's;
    `None` for a rule with no variants, or when it is not known."""
    findings: tuple[PlannedFinding, ...]
    """This countertop's own live results for the rule."""
    missing: tuple[MissingInputOut, ...]
    """What the rule's whole-package result is waiting on (`missing_inputs_for`), empty otherwise."""


@dataclass(frozen=True, slots=True)
class ItemPlan:
    """What is checked for one countertop."""

    countertop: LiveCountertop
    rules: tuple[PlannedRule, ...]
    findings: tuple[PlannedFinding, ...]
    """Every live result that belongs to this countertop, whatever its rule."""
    note: str | None
    """`NO_ROW_LINKED` when no countertop row is linked to it; `None` otherwise."""


@dataclass(frozen=True, slots=True)
class CheckPlan:
    """The plan of one revision: one entry per live countertop, in page order."""

    package_revision_id: UUID
    items: tuple[ItemPlan, ...]


@dataclass(frozen=True, slots=True)
class _Waiting:
    """A live whole-package result, in the shape `missing_inputs_for` reads."""

    id: UUID
    check_run_id: UUID
    package_revision_id: UUID
    rule_id: str
    rule_snapshot_id: UUID
    outcome: Outcome
    scope_item_id: UUID | None
    scope_row_candidate_id: UUID | None
    parameter_set_versions: Mapping[str, str]


def check_plan_for(session: Session, package_revision_id: UUID) -> CheckPlan:
    """For each live countertop item, its rules, its own results and what its rules wait on.

    **Read-only, and it decides nothing.** The rules are the published ones for **the package's
    product** (`package_product`; a package that never stated one is checked by every product's
    rules, and its countertops are listed with the countertop rules), through the same resolver the
    checks use, with no discriminator stated. Where this countertop has its own result for a rule,
    the rule's version and variant are the result's, because the checks chose them from its row's
    walls; only a rule with no result of its own is listed as waiting, with the resolver's reason.
    The results are the live ones (`check_runs.superseded_at IS NULL`) keyed by the countertop's row
    or by the item itself. The values listed as missing are those of each rule's whole-package
    `NOT_FOUND` result, worked out by `missing_inputs_for` exactly as the results list does.
    """
    countertops = live_countertops(session, package_revision_id)
    if not countertops:
        return CheckPlan(package_revision_id=package_revision_id, items=())
    revision = session.get_one(PackageRevision, package_revision_id)
    package = session.get_one(Package, revision.package_id)
    store = snapshot_store(session)
    resolution = resolve(
        store,
        CheckContext(
            product_type=package_product(package) or ProductType.COUNTERTOP,
            project=ProjectScope(
                project_id=str(package.project_id),
                # Carried by the resolver, never used to choose a rule (`rules/applicability.py`).
                parameter_set=ParameterSet(
                    project_id=str(package.project_id),
                    layer=ParameterLayer.PROJECT,
                    version=1,
                    parameters={},
                ),
            ),
            discriminators={},
        ),
    )
    planned: list[tuple[str, bool, str | None, str | None, str | None]] = sorted(
        [
            (
                entry.rule_id,
                True,
                None,
                entry.snapshot.rule.version,
                None if entry.variant is None else entry.variant.when,
            )
            for entry in resolution.applicable
        ]
        + [
            (abstention.rule_id, False, abstention.reason, None, None)
            for abstention in resolution.abstentions
            if abstention.rule_id is not None
        ]
    )

    live = session.execute(
        select(Finding, RuleDefinition.rule_id, CheckRun.rule_snapshot_id, RuleSnapshot.version)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == package_revision_id,
            CheckRun.superseded_at.is_(None),
        )
        .order_by(Finding.created_at, Finding.id)
    ).all()
    whole_package = [
        _Waiting(
            id=finding.id,
            check_run_id=finding.check_run_id,
            package_revision_id=finding.package_revision_id,
            rule_id=rule_id,
            rule_snapshot_id=snapshot_id,
            outcome=Outcome(finding.outcome),
            scope_item_id=finding.scope_item_id,
            scope_row_candidate_id=finding.scope_row_candidate_id,
            parameter_set_versions=finding.parameter_set_versions or {},
        )
        for finding, rule_id, snapshot_id, _version in live
        if finding.scope_item_id is None
        and finding.scope_row_candidate_id is None
        and rule_id in WAITING_RULES
    ]
    waiting = missing_inputs_for(session, package.project_id, whole_package)
    missing_by_rule: dict[str, tuple[MissingInputOut, ...]] = {}
    for entry in whole_package:
        if waiting.get(entry.id):
            missing_by_rule.setdefault(entry.rule_id, tuple(waiting[entry.id]))

    items: list[ItemPlan] = []
    for countertop in countertops:
        mine = [
            (finding, rule_id, version)
            for finding, rule_id, _snapshot, version in live
            if finding.scope_item_id == countertop.item_id
            or (
                countertop.row_candidate_id is not None
                and finding.scope_row_candidate_id == countertop.row_candidate_id
            )
        ]
        own = tuple(
            PlannedFinding(
                finding_id=finding.id,
                rule_id=rule_id,
                outcome=Outcome(finding.outcome),
                scope="item" if finding.scope_item_id == countertop.item_id else "row",
            )
            for finding, rule_id, _version in mine
        )
        # The newest of its own results per rule: the version and variant the checks chose for it.
        checked = {rule_id: (version, finding.variant) for finding, rule_id, version in mine}
        items.append(
            ItemPlan(
                countertop=countertop,
                rules=tuple(
                    _planned_rule(
                        rule_id,
                        resolved,
                        reason,
                        version,
                        variant,
                        checked.get(rule_id),
                        tuple(entry for entry in own if entry.rule_id == rule_id),
                        missing_by_rule.get(rule_id, ()),
                    )
                    for rule_id, resolved, reason, version, variant in planned
                ),
                findings=own,
                note=NO_ROW_LINKED if countertop.row_candidate_id is None else None,
            )
        )
    return CheckPlan(package_revision_id=package_revision_id, items=tuple(items))


def _planned_rule(
    rule_id: str,
    resolved: bool,
    reason: str | None,
    version: str | None,
    variant: str | None,
    checked: tuple[str, str | None] | None,
    findings: tuple[PlannedFinding, ...],
    missing: tuple[MissingInputOut, ...],
) -> PlannedRule:
    """One rule's entry: from this countertop's own result when it has one, else the resolver's."""
    if checked is not None:
        return PlannedRule(
            rule_id=rule_id,
            resolved=True,
            waits_for=None,
            version=checked[0],
            variant=checked[1],
            findings=findings,
            missing=missing,
        )
    return PlannedRule(
        rule_id=rule_id,
        resolved=resolved,
        waits_for=reason,
        version=version,
        variant=variant,
        findings=findings,
        missing=missing,
    )
