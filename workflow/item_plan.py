"""Each live countertop item, and what is checked for it: the seed of Kautilya (#1245).

**What a countertop item is here.** A `drawing_items` row of type `countertop_overall_width` that a
person's current decision confirmed (`workflow/parts.py:live_part_item_ids`), on one drawing view.
Bala suggests one for every countertop row the slot reader read (`workflow/bala_items.py`) and links
the suggestion to the row (`part_proposal_rows`), so an item confirmed from one of Bala's suggestions
knows its row. An item confirmed from any other suggestion has no row.

**Three readers use it, and none of them decides anything:**

* `workflow/evidence_operands.py` asks how many countertops each drawing holds
  (`countertops_by_view`): a reading on a drawing with one countertop belongs to that countertop, and
  on a drawing with two or more it belongs to none, so two countertops' cabinets are never summed.
* the results screen asks which countertop each row belongs to (`countertops_by_row`), to group the
  row's results under it. The results stay keyed by the row (`scope_row_candidate_id`), so a
  reviewer's decision still carries over to an unchanged re-run (decided by Anant, 2026-10-14).
* `check_plan_for` lists, for each countertop, the published rules for its product
  (`rules/applicability.resolve`), the results its row (or the item itself) has, and the values each
  rule's whole-package result is still waiting on (`app/review/waiting_values.py`).

**Read-only.** Nothing here writes a row, runs a check or asks a model. No extraction import, so the
API may read it (`tests/api/test_no_heavy_work.py`).

Source: issue #1245; phase 4 design §3 and §5 PR 3. Verification: `tests/workflow/test_item_plan.py`,
`tests/workflow/test_per_item_assembly.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    Finding,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartDecision,
    PartProposalRow,
    RuleDefinition,
    RuleSnapshot,
)
from app.review.waiting_values import WAITING_RULES, missing_inputs_for
from app.schemas.findings import MissingInputOut
from app.verdicts.rulebook import snapshot_store
from rules.applicability import CheckContext, resolve
from rules.parameters import ParameterLayer, ParameterSet
from rules.project import ProjectScope
from rules.semantic_types import ProductType
from verdict.outcomes import Outcome
from vocabulary.part_kinds import PartKind
from workflow.parts import outline_box

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
    """One countertop a person stands by, where it is, and the row it was confirmed from."""

    item_id: UUID
    view_id: UUID
    page_id: UUID
    page_index: int
    number: int
    """Its place among the live countertops on its page, `1` from the left."""
    row_candidate_id: UUID | None
    """The anchor of the countertop row Bala suggested it from (`part_proposal_rows`); `None` when
    it was confirmed from another suggestion."""

    @property
    def page_number(self) -> int:
        return self.page_index + 1

    @property
    def label(self) -> str:
        """ "Countertop 1 on page 3": the same on the plan, the results screen and the run check."""
        return f"Countertop {self.number} on page {self.page_number}"


def live_countertops(session: Session, package_revision_id: UUID) -> tuple[LiveCountertop, ...]:
    """Every live countertop item on this revision's drawings, in page order, left to right.

    **Live** means a person's current decision confirmed it as a countertop: a withdrawn or corrected
    item keeps its row (no role holds `DELETE`) and is not here. The row it came from is read through
    its confirmation's suggestion (`part_proposal_rows`, the countertop's own link: `slot_index` is
    `NULL`). One statement, whatever the number of items.
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

    def place(extent: Mapping[str, object]) -> tuple[Decimal, Decimal]:
        try:
            left, top, _right, _bottom = outline_box(extent)
        except ValueError:
            return _UNREADABLE
        return left, top

    ordered = sorted(rows, key=lambda row: (row[4], *place(row[2]), str(row[0])))
    counts: dict[UUID, int] = {}
    countertops: list[LiveCountertop] = []
    for item_id, view_id, _extent, page_id, page_index, row_candidate_id in ordered:
        counts[page_id] = counts.get(page_id, 0) + 1
        countertops.append(
            LiveCountertop(
                item_id=item_id,
                view_id=view_id,
                page_id=page_id,
                page_index=page_index,
                number=counts[page_id],
                row_candidate_id=row_candidate_id,
            )
        )
    return tuple(countertops)


def countertops_by_view(
    session: Session, package_revision_id: UUID
) -> dict[UUID, tuple[UUID, ...]]:
    """Each drawing view of the revision that holds a live countertop item, with those items."""
    held: dict[UUID, list[UUID]] = {}
    for countertop in live_countertops(session, package_revision_id):
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
    """One published rule for the countertop's product, and where it stands for this countertop."""

    rule_id: str
    resolved: bool
    """Whether the resolver could say which version of the rule applies from what is stated alone.
    A rule with a discriminator (the wall layout) is resolved per row when the checks run."""
    waits_for: str | None
    """The resolver's own reason when it could not (`rules/applicability.Abstention.reason`)."""
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

    **Read-only, and it decides nothing.** The rules come from the published rulebook through the
    same resolver the checks use, with no discriminator stated: a rule with one (the wall layout) is
    listed as waiting for it, with the resolver's reason, because the checks resolve it per row from
    that row's walls. The results are the live ones (`check_runs.superseded_at IS NULL`) keyed by the
    countertop's row or by the item itself. The values listed as missing are those of each rule's
    whole-package `NOT_FOUND` result, worked out by `missing_inputs_for` exactly as the results list
    does.
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
            product_type=ProductType.COUNTERTOP,
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
    planned: list[tuple[str, bool, str | None]] = sorted(
        [(entry.rule_id, True, None) for entry in resolution.applicable]
        + [
            (abstention.rule_id, False, abstention.reason)
            for abstention in resolution.abstentions
            if abstention.rule_id is not None
        ]
    )

    live = session.execute(
        select(Finding, RuleDefinition.rule_id, CheckRun.rule_snapshot_id)
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
        for finding, rule_id, snapshot_id in live
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
        own = tuple(
            PlannedFinding(
                finding_id=finding.id,
                rule_id=rule_id,
                outcome=Outcome(finding.outcome),
                scope="item" if finding.scope_item_id == countertop.item_id else "row",
            )
            for finding, rule_id, _snapshot in live
            if finding.scope_item_id == countertop.item_id
            or (
                countertop.row_candidate_id is not None
                and finding.scope_row_candidate_id == countertop.row_candidate_id
            )
        )
        items.append(
            ItemPlan(
                countertop=countertop,
                rules=tuple(
                    PlannedRule(
                        rule_id=rule_id,
                        resolved=resolved,
                        waits_for=reason,
                        findings=tuple(entry for entry in own if entry.rule_id == rule_id),
                        missing=missing_by_rule.get(rule_id, ()),
                    )
                    for rule_id, resolved, reason in planned
                ),
                findings=own,
                note=NO_ROW_LINKED if countertop.row_candidate_id is None else None,
            )
        )
    return CheckPlan(package_revision_id=package_revision_id, items=tuple(items))
