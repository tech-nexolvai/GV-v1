"""Persisting a decision the engine made.

The first writer of `check_runs`, `verdict_inputs` and `findings`. Everything in `app/` that touches
the verdict plane until now has read it — the list endpoint, the chain, the export — and the rows they
read had to be put there by hand.

Four things this has to get right, each of which the schema half-enforces and half-trusts.

**A finding belongs to exactly one run.** `findings.check_run_id` is unique, so a rule is a run. A
re-run writes a new run and a new finding and never edits the old ones: `findings` carries the
append-only trigger, so an `UPDATE` is refused by the database rather than quietly applied. Which set
is current is answered by `check_runs.superseded_at`, not by deleting anything.

**The revision on the finding must be the run's own.** A composite foreign key says so
(`fk_findings_run_revision`), and it exists because the alternative — a finding claiming a revision
its run does not have — would misstate which drawings were reviewed, and an approval built on it
would misstate what was signed off.

**Only qualified operands are sealed.** `verdict_inputs` has a check constraint admitting
`CORROBORATED` and `HUMAN_CONFIRMED` and nothing else, so an unqualified value is refused by the
database. That is the same gate `verdict/operands.py` applies before the arithmetic; this writes it
down so the finding can be recomputed from its own stored inputs years later.

**A decision must carry its evidence; an abstention need not.** `app/models/verdicts.py` says a
finding with no evidence cannot exist, and notes that no `CHECK` can express it — the deferred trigger
belongs to a later story, so today the writer is the enforcement. The line drawn here is narrower than
that sentence and truer to it: a `PASS` or `FAIL` rests on evidence and is refused without it, while an
abstention has no evidence *because there was none*, and manufacturing an empty link for it would be
inventing provenance for a check that never ran.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.rules import RuleSnapshot as RuleSnapshotRow
from app.models.verdicts import CheckRun, VerdictInput
from app.models.verdicts import Finding as FindingRow
from app.verdicts.trace import abstention_trace, calculation_trace, missing_operand_reason
from units.measurement import Measurement
from verdict.finding import Finding
from verdict.operands import QUALIFIED_STATUSES, VerdictOperand
from verdict.outcomes import DECISIVE_OUTCOMES, Outcome

__all__ = ["EvidenceMissing", "record_finding", "supersede_runs"]


class EvidenceMissing(ValueError):
    """Raised when a decided finding would be written with nothing behind it.

    A `ValueError` and not a refusal object: there is no sensible way for a caller to continue. A
    `PASS` with no operands is not a lenient finding, it is a claim about a drawing nobody read.
    """


def supersede_runs(session: Session, package_revision_id: UUID) -> int:
    """Mark every live run for this revision replaced, returning how many.

    Called before writing a new set, so the window in which both are live is inside one transaction
    and no reader ever sees two. `check_runs` is not append-only precisely so this can happen — the
    findings themselves are untouched and every one ever written is still there.

    Returns the count because a caller reporting "checks re-run, 8 previous results superseded" is
    saying something a reviewer needs, and a silent replacement is how somebody comes to believe a
    verdict they are looking at is the only one there has ever been.
    """
    live = list(
        session.execute(
            select(CheckRun.id).where(
                CheckRun.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
            )
        ).scalars()
    )
    if not live:
        return 0

    session.execute(update(CheckRun).where(CheckRun.id.in_(live)).values(superseded_at=utc_now()))
    return len(live)


def record_finding(
    session: Session,
    *,
    package_revision_id: UUID,
    finding: Finding,
    operands: Mapping[str, VerdictOperand],
    parameter_set_ids: Mapping[str, str],
    missing: Mapping[str, str] | None = None,
) -> FindingRow:
    """Write one decision: its run, the operands it was computed from, and the finding itself.

    `parameter_set_ids` is supplied by the caller rather than read off the finding, because
    `verdict/engine.py` returns it empty — the engine is handed resolved parameters and never learns
    which sets they came from. Mapping layer to content hash keeps the column able to answer "which
    numbers judged this?" for each layer separately, which a flat list of hashes cannot.

    `missing` names the operands that were never read, and only reaches the stored trace when the
    check abstained. It is what turns "NOT_FOUND" into a sentence somebody can act on.
    """
    snapshot_row = session.execute(
        select(RuleSnapshotRow).where(RuleSnapshotRow.snapshot_id == finding.snapshot_id)
    ).scalar_one_or_none()
    if snapshot_row is None:
        raise EvidenceMissing(
            f"no published snapshot {finding.snapshot_id!r} for rule {finding.rule_id!r}. A finding "
            "citing a snapshot the database does not hold could never be reproduced."
        )

    sealed = {
        name: operand
        for name, operand in operands.items()
        if operand.status in QUALIFIED_STATUSES and operand.value is not None
    }

    # The invariant the schema cannot hold. Checked before anything is inserted, so a refusal leaves
    # no half-written run behind.
    if finding.outcome in DECISIVE_OUTCOMES and not sealed:
        raise EvidenceMissing(
            f"{finding.rule_id} decided {finding.outcome.value} with no qualified operand. A verdict "
            "with nothing behind it cannot be defended to the vendor it is sent to."
        )

    run = CheckRun(
        package_revision_id=package_revision_id,
        rule_snapshot_id=snapshot_row.id,
        engine_version=finding.engine_version,
    )
    session.add(run)
    session.flush()

    for name, operand in sealed.items():
        exact = _exact(operand.value)
        if exact is None:
            # A qualified operand whose value is not a single exact number — a `many` selector's
            # tuple, or text. The trace records it; `verdict_inputs` holds one rational per slot and
            # has nowhere to put it. Skipped rather than coerced, because a tuple flattened into one
            # number would be a different calculation wearing the same name.
            continue
        session.add(
            VerdictInput(
                check_run_id=run.id,
                operand_name=name,
                value_numerator=exact.numerator,
                value_denominator=exact.denominator,
                unit=_unit_of(operand.value),
                evidence_status=operand.status.value,
                canonical_observation_id=None,
            )
        )

    row = FindingRow(
        check_run_id=run.id,
        # Explicit, and the composite foreign key checks it against the run's own.
        package_revision_id=package_revision_id,
        outcome=finding.outcome.value,
        severity=finding.severity.value,
        trace=(
            calculation_trace(finding.trace, outcome=finding.outcome)
            if finding.trace is not None
            else abstention_trace(
                finding.outcome,
                cause=_cause_for(finding.outcome),
                reason=finding.reason or missing_operand_reason(missing or {}),
            )
        ),
        parameter_set_versions=dict(parameter_set_ids),
    )
    session.add(row)
    session.flush()
    return row


def _cause_for(outcome: Outcome) -> str:
    """A machine-readable cause for an abstention, from the outcome that produced it.

    Kept narrow: three outcomes can abstain and each abstains for one reason, so a free-text cause
    would be three sentences that drift. The reviewer-facing explanation is `reason`.
    """
    return {
        Outcome.NOT_FOUND: "operand_missing",
        Outcome.REVIEW_REQUIRED: "needs_review",
        Outcome.NO_APPLICABLE_RULE: "no_applicable_rule",
    }.get(outcome, "unknown")


def _exact(value: object) -> Fraction | None:
    """The single exact rational behind an operand value, or `None` when there is not one."""
    if isinstance(value, Measurement):
        return value.exact
    if isinstance(value, Fraction):
        return value
    return None


def _unit_of(value: object) -> str:
    """The stored unit for a sealed operand.

    A bare `Fraction` operand — a literal or a count — carries no unit of its own and is stored as
    inches, which is the arithmetic unit every V1 rule declares (Q12). Stated here rather than
    defaulted silently, because the column has a check constraint and a wrong answer would be a
    dimension recorded in the wrong system.
    """
    if isinstance(value, Measurement):
        return value.unit.value
    return "in"


def sealed_operand_names(operands: Mapping[str, VerdictOperand]) -> Sequence[str]:
    """Which operands would be written, for a caller that wants to report before committing."""
    return sorted(
        name for name, operand in operands.items() if operand.status in QUALIFIED_STATUSES
    )
