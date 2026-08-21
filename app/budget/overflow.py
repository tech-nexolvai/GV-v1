"""Fail closed when a package exhausts its model budget.

Overflow is not an infrastructure error and it is never a partial success. This module returns a
REVIEW REQUIRED disposition for every affected finding, keeps already-produced evidence intact,
halts further agent work, and moves the package into the reviewer-actionable ``NEEDS_INPUT`` state.

Stored findings are immutable, so this function does not rewrite an earlier finding. Call it at the
pre-call ceiling boundary, before affected checks are finalized, and persist the returned
dispositions as their outcomes. That boundary prevents a partially read package from acquiring a
PASS while preserving all evidence gathered before exhaustion.

Source: ``docs/DESIGN_CONTROLS.md`` section 5 and issue #265.
Verification: ``tests/budget/test_overflow.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from sqlalchemy.orm import Session

from app.lifecycle.side_states import enter_needs_input
from app.models.package import PackageStateEvent
from verdict.outcomes import Outcome

BUDGET_EXHAUSTED_REASON = "budget exhausted after {regions_done} regions"


@dataclass(frozen=True, slots=True)
class FindingDisposition:
    """The only permitted outcome for one check affected by budget exhaustion."""

    finding_id: UUID
    outcome: Outcome
    trace: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class OverflowResult:
    """Instructions produced when package-level model work must stop."""

    outcome: Outcome
    reason: str
    affected_findings: tuple[FindingDisposition, ...]
    partial_evidence_retained: bool
    review_complete: bool
    agent_halted: bool
    state_event: PackageStateEvent


def _region_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("regions_done must be a non-negative integer")
    return value


def on_overflow(
    session: Session,
    package_revision_id: UUID,
    *,
    regions_done: int,
    affected_finding_ids: Sequence[UUID],
    actor: str,
) -> OverflowResult:
    """Stop agent work and abstain every affected finding.

    The trace distinguishes budget exhaustion from drawing ambiguity. Existing candidate and
    canonical evidence is deliberately left untouched; only the completeness claim is refused.
    Duplicate finding identifiers are rejected because one affected check must have one addressable
    disposition, not two conflicting records.
    """

    completed = _region_count(regions_done)
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must name who or what stopped the package")

    identifiers = tuple(affected_finding_ids)
    if any(not isinstance(identifier, UUID) for identifier in identifiers):
        raise TypeError("affected_finding_ids must contain UUID values")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("affected_finding_ids must not contain duplicates")

    reason = BUDGET_EXHAUSTED_REASON.format(regions_done=completed)
    trace: Mapping[str, object] = MappingProxyType(
        {
            "cause": "package_model_budget_exhausted",
            "reason": reason,
            "regions_done": completed,
            "review_complete": False,
        }
    )
    dispositions = tuple(
        FindingDisposition(
            finding_id=identifier,
            outcome=Outcome.REVIEW_REQUIRED,
            trace=trace,
        )
        for identifier in identifiers
    )

    event = enter_needs_input(
        session,
        package_revision_id,
        actor=actor.strip(),
        needed=(
            f"{reason}; review the retained partial evidence or authorize additional model budget"
        ),
    )
    return OverflowResult(
        outcome=Outcome.REVIEW_REQUIRED,
        reason=reason,
        affected_findings=dispositions,
        partial_evidence_retained=True,
        review_complete=False,
        agent_halted=True,
        state_event=event,
    )
