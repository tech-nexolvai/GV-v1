"""Nina's gate: a PASS or FAIL that breaks a safety rule waits for a person (#1228).

**Why.** Each safety rule is enforced where it applies (the seal, the one-judgment pairing rule, the
unwitnessed-reading and automatic-walls clicks, the inputs a decision must store). Pariksha's run
check (#1220) confirms afterwards that all of them held for one run, but only reported. This makes
it enforcing at Nina's hand-off: at the end of `run_checks`, after the last result is written and
before reviewer decisions carry over, any live PASS or FAIL the run check's finding-level checks
fault (`workflow.pariksha_run_check.decision_faults`) becomes REVIEW_REQUIRED.

**What it never does.** It only holds. It never makes a hold into PASS or FAIL, never turns a PASS
into a FAIL or back, and never changes a result the checks do not fault. A run whose results break
no rule is left exactly as it was, so on a correct run it does nothing.

**How a hold is written.** Findings are append-only (`app/verdicts/record.py`), so the faulted
result's own check run is superseded (marked replaced, never deleted) and a new run and finding are
written in the same transaction with the same rule snapshot, engine version, defaults, inputs
(`verdict_inputs`, copied exactly), scope, severity and variant. The new finding is REVIEW_REQUIRED
with the check's plain reason. Its notes keep the engine's own result first (`engine_result_note`,
the shape `confirm_unwitnessed_pass` uses), then one `Pariksha gate (Nina, safety-rules)` line per
rule it broke, then the original notes. The decided trace is not kept, so nothing on record reads as
decided. The superseded result stays in the database as history.

**What it cannot hold.** A rule in scope with no result, a countertop row with no width result, a
vendor page that vanished, an unread page with no reason: these are about the run, not one result,
so there is no finding to hold. They stay in the run check's report (`run_check`), which lists them
as before.

Source: issue #1228. Verification: `tests/workflow/test_nina_gate.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.verdicts import CheckRun, Finding, VerdictInput
from app.verdicts.trace import abstention_trace
from verdict.outcomes import DECISIVE_OUTCOMES, Outcome
from vocabulary.drawn_length import engine_result_note
from workflow.pariksha_gates import Agent, GateDecision, GateResult, GateScope
from workflow.pariksha_run_check import RunCheckItem, decision_faults

__all__ = [
    "NINA_GATE",
    "NinaGateOutcome",
    "hold_unsafe_decisions",
    "nina_hold_reason",
]

#: Nina's gate.
NINA_GATE: Final = "safety-rules"

_DECIDED: Final = frozenset(outcome.value for outcome in DECISIVE_OUTCOMES)


@dataclass(frozen=True, slots=True)
class NinaGateOutcome:
    """What Nina's gate did at the end of one `run_checks`."""

    checked: int
    """The live PASS and FAIL results it looked at."""
    held: tuple[GateResult, ...]
    """One per rule broken, by finding (the held result's new id in `scope_id`)."""

    @property
    def held_findings(self) -> int:
        return len({result.scope_id for result in self.held})

    def to_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "held": self.held_findings,
            "passed": self.checked - self.held_findings,
            "stops": [result.to_dict() for result in self.held],
        }


def nina_hold_reason(items: Sequence[RunCheckItem]) -> str:
    """The held result's reason: the first broken rule's sentence, and how many more there are."""
    if not items:
        raise ValueError("a hold needs the rule it broke")
    reason = f"Held for a person by Pariksha: {items[0].found}"
    if len(items) > 1:
        reason += f" ({len(items) - 1} more safety rule(s) broken; see the notes.)"
    return reason


def _gate_results(items: Sequence[RunCheckItem], scope_id: str) -> tuple[GateResult, ...]:
    return tuple(
        GateResult(
            agent=Agent.NINA,
            gate=NINA_GATE,
            scope=GateScope.FINDING,
            decision=GateDecision.STOP,
            code=item.code,
            reason=f"{item.found} (points to {item.agent.value})",
            scope_id=scope_id,
        )
        for item in items
    )


def _hold(session: Session, finding: Finding, items: Sequence[RunCheckItem]) -> Finding:
    """Supersede the faulted result's run and write its held replacement; returns the new row."""
    run = session.get(CheckRun, finding.check_run_id)
    if run is None or run.superseded_at is not None:
        raise ValueError("only a live result can be held")
    if finding.outcome not in _DECIDED:
        raise ValueError("only a PASS or FAIL is ever held")
    inputs = list(
        session.scalars(
            select(VerdictInput)
            .where(VerdictInput.check_run_id == run.id)
            .order_by(VerdictInput.operand_name, VerdictInput.id)
        )
    )
    run.superseded_at = utc_now()
    session.flush()
    held_run = CheckRun(
        package_revision_id=run.package_revision_id,
        rule_snapshot_id=run.rule_snapshot_id,
        engine_version=run.engine_version,
        defaults_set_id=run.defaults_set_id,
        defaults_canonical_json=run.defaults_canonical_json,
    )
    session.add(held_run)
    session.flush()
    for item in inputs:
        session.add(
            VerdictInput(
                check_run_id=held_run.id,
                operand_name=item.operand_name,
                value_numerator=item.value_numerator,
                value_denominator=item.value_denominator,
                unit=item.unit,
                evidence_status=item.evidence_status,
                canonical_observation_id=item.canonical_observation_id,
                slot_row_review_decision_id=item.slot_row_review_decision_id,
            )
        )
    reason = nina_hold_reason(items)
    gate_lines = [result.note for result in _gate_results(items, str(finding.id))]
    held = Finding(
        check_run_id=held_run.id,
        package_revision_id=finding.package_revision_id,
        scope_item_id=finding.scope_item_id,
        scope_row_candidate_id=finding.scope_row_candidate_id,
        scope_label=finding.scope_label,
        outcome=Outcome.REVIEW_REQUIRED.value,
        severity=finding.severity,
        trace=abstention_trace(Outcome.REVIEW_REQUIRED, cause="needs_review", reason=reason),
        parameter_set_versions=dict(finding.parameter_set_versions or {}),
        reason=reason,
        delta_numerator=None,
        delta_denominator=None,
        delta_unit=None,
        variant=finding.variant,
        notes=[
            engine_result_note(finding.outcome, finding.reason or "no reason recorded"),
            *gate_lines,
            *(finding.notes or ()),
        ],
    )
    session.add(held)
    session.flush()
    return held


def hold_unsafe_decisions(session: Session, package_revision_id: UUID) -> NinaGateOutcome:
    """Hold every live PASS or FAIL of the revision that breaks a safety rule. Called at the end
    of `run_checks`, inside its transaction, before reviewer decisions carry over."""
    faults: Mapping[UUID, tuple[RunCheckItem, ...]] = decision_faults(session, package_revision_id)
    checked = len(
        session.scalars(
            select(Finding.id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(
                Finding.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
                Finding.outcome.in_(sorted(_DECIDED)),
            )
        ).all()
    )
    held: list[GateResult] = []
    for finding_id in sorted(faults, key=str):
        finding = session.get(Finding, finding_id)
        if finding is None:
            continue
        replacement = _hold(session, finding, faults[finding_id])
        held.extend(_gate_results(faults[finding_id], str(replacement.id)))
    return NinaGateOutcome(checked=checked, held=tuple(held))
