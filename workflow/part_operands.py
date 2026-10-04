"""Complete human-confirmed runs, never partial sums or form substitutes (#932).

This is the only operand resolver that reads runs and reading links. It selects evidence; the
evidence gate qualifies each reading and the unchanged engine decides the published check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    CanonicalObservation,
    CountertopRun,
    CountertopRunDecision,
    DrawingItem,
    DrawingView,
    PackageRevisionDocument,
    Page,
    PartDecision,
    ReadingPart,
    ViewRole,
)
from evidence.canonical import CanonicalObservation as DomainObservation
from evidence.gate import GateRefusal, seal
from rules.schema import Rule
from units.measurement import Measurement
from verdict.operands import VerdictOperand
from vocabulary.part_kinds import PartKind
from workflow.countertop_runs import live_run_rows
from workflow.parts import live_part
from workflow.reading_parts import current_links_to, live_reading_parts


@dataclass(frozen=True)
class PartOperands:
    """Run-owned inputs stay owned even when no usable operand can be returned."""

    owned: dict[str, frozenset[str]] = field(default_factory=dict)
    operands: dict[str, dict[str, VerdictOperand]] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)
    ambiguous: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: dict[str, tuple[str, ...]] = field(default_factory=dict)


_INPUTS = {
    "CT-WIDTH-001": {
        "countertop_width": PartKind.COUNTERTOP,
        "cabinet_widths": PartKind.CABINET,
        "filler_widths": PartKind.FILLER,
    },
    "CAB-FILLER-001": {"shop_cabinets": PartKind.CABINET, "shop_fillers": PartKind.FILLER},
}


def part_operands(
    session: Session,
    revision_id: UUID,
    rules: Sequence[Rule],
    observations: Mapping[UUID, tuple[DomainObservation, tuple[str, UUID]]],
) -> PartOperands:
    """Select one complete run for this revision, or explicitly withhold its owned inputs.

    Current decisions include withdrawals and invalidated parts. Filtering those out at discovery
    would mistake a revoked decision for no decision and restore stale form/label widths.
    Multiple countertops have no per-top check scope yet, so they are ambiguous, never concatenated.
    """
    result = PartOperands()
    relevant = [rule for rule in rules if rule.id in _INPUTS]
    if not relevant:
        return result
    later = aliased(CountertopRunDecision)
    decisions = list(
        session.scalars(
            select(CountertopRunDecision)
            .join(DrawingItem, DrawingItem.id == CountertopRunDecision.countertop_item_id)
            .join(DrawingView, DrawingView.id == DrawingItem.drawing_view_id)
            .join(Page, Page.id == DrawingView.page_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .where(
                PackageRevisionDocument.package_revision_id == revision_id,
                ~exists().where(later.supersedes_id == CountertopRunDecision.id),
            )
            .order_by(CountertopRunDecision.id)
        )
    )
    if not decisions:
        return result
    for rule in relevant:
        result.owned[rule.id] = frozenset(_INPUTS[rule.id])

    def refuse(reason: str, *, ambiguous: bool = False) -> PartOperands:
        result.operands.clear()
        result.notes.clear()
        for rule in relevant:
            if ambiguous:
                result.ambiguous[rule.id] = dict.fromkeys(result.owned[rule.id], reason)
            else:
                result.missing[rule.id] = reason
        return result

    if len(decisions) != 1:
        return refuse(
            "More than one countertop run has no single check scope; choose the "
            "countertop before checking its widths.",
            ambiguous=True,
        )
    decision = decisions[0]
    prefix = f"Countertop run decision {decision.id}: "
    if decision.decision != PartDecision.CONFIRMED.value or decision.run_id is None:
        return refuse(prefix + "the run was withdrawn; confirm a current run before checking.")
    top = live_part(session, decision.countertop_item_id)
    if top is None or top.kind is not PartKind.COUNTERTOP:
        return refuse(prefix + "the countertop was withdrawn or corrected; confirm a current run.")
    view = session.get_one(DrawingView, top.view_id)
    if view.role != ViewRole.SHOP.value:
        return refuse(
            prefix + "the countertop drawing is not confirmed as the vendor's.", ambiguous=True
        )
    page = session.get_one(Page, view.page_id)
    # Deliberately no SQL order: the stored positions, not insertion or click order, govern.
    rows = list(session.scalars(live_run_rows().where(CountertopRun.run_id == decision.run_id)))
    if not rows:
        return refuse(
            prefix + "the run has no live complete membership; a part was taken back "
            "or corrected. Confirm the whole run again."
        )
    rows = sorted(rows, key=lambda row: row.position)
    if (
        [row.position for row in rows] != list(range(len(rows)))
        or len({row.member_item_id for row in rows}) != len(rows)
        or any(row.countertop_item_id != top.item_id for row in rows)
    ):
        return refuse(
            prefix + "the run's membership or drawing order is conflicting.", ambiguous=True
        )

    parts = [top]
    for row in rows:
        member = live_part(session, row.member_item_id)
        if member is None:
            return refuse(prefix + f"member {row.position + 1} is no longer confirmed.")
        if member.view_id != top.view_id or member.kind not in {PartKind.CABINET, PartKind.FILLER}:
            return refuse(
                prefix + f"member {row.position + 1} is not a cabinet or filler on "
                "the countertop's vendor drawing.",
                ambiguous=True,
            )
        parts.append(member)

    live_links = {
        link.id
        for link in session.scalars(
            live_reading_parts().where(
                ReadingPart.drawing_item_id.in_([part.item_id for part in parts])
            )
        )
    }
    sealed_by_kind: dict[PartKind, list[VerdictOperand]] = {}
    notes = [
        (
            f"Widths from confirmed run {decision.run_id}, decision {decision.id}, "
            f"countertop {top.item_id}, vendor drawing {top.view_id}."
        )
    ]
    for position, part in enumerate(parts):
        label = "countertop" if position == 0 else f"{part.kind.value} at run position {position}"
        links = current_links_to(session, part.item_id)
        if len(links) > 1:
            return refuse(prefix + f"{label} has conflicting width links.", ambiguous=True)
        if not links or links[0].id not in live_links:
            return refuse(
                prefix + f"{label} has no live confirmed width; its link or reading "
                "may have been withdrawn or replaced in review."
            )
        link = links[0]
        reading = session.get_one(CanonicalObservation, link.canonical_observation_id)
        found = observations.get(reading.id)
        if (
            reading.document_version_id != page.document_version_id
            or reading.page_id != page.id
            or reading.document_role != "SHOP"
            or (found is not None and found[1] != ("view", top.view_id))
        ):
            return refuse(
                prefix + f"{label}'s reading is not on the same vendor drawing and "
                "document revision as its countertop.",
                ambiguous=True,
            )
        if found is None:
            return refuse(
                prefix + f"{label}'s reading cannot be rebuilt as qualified evidence.",
                ambiguous=reading.status == "CONFLICTING",
            )
        qualified = seal(found[0], label)
        if isinstance(qualified, GateRefusal):
            return refuse(
                prefix + f"{label}'s width was withheld by the evidence gate: "
                f"{qualified.detail}.",
                ambiguous=reading.status == "CONFLICTING",
            )
        qualified = replace(qualified, evidence_observation_id=str(reading.id))
        sealed_by_kind.setdefault(part.kind, []).append(qualified)
        notes.append(
            f"{label.capitalize()}: part {part.item_id}, width reading {reading.id}, "
            f"confirmed link {link.id}."
        )

    # All-or-nothing before publishing even a single operand; no partial sums.
    if sum(len(group) for group in sealed_by_kind.values()) != len(parts):
        return refuse(prefix + "not every run member has a sealed width.")
    for rule in relevant:
        built: dict[str, VerdictOperand] = {}
        for name, kind in _INPUTS[rule.id].items():
            group = sealed_by_kind.get(kind, [])
            if not group:
                return refuse(
                    prefix + f"the run has no confirmed {kind.value} width input; "
                    "no zero width was substituted."
                )
            first = group[0]
            if kind is PartKind.COUNTERTOP:
                built[name] = replace(first, name=name)
            else:
                values = tuple(op.value for op in group if isinstance(op.value, Measurement))
                if len(values) != len(group):
                    return refuse(prefix + "a linked width is not an exact measurement.")
                built[name] = replace(first, name=name, value=values, evidence_observation_id=None)
        result.operands[rule.id] = built
        result.notes[rule.id] = tuple(notes)
    return result
