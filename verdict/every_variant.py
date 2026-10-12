"""One rule, evaluated once for every variant it publishes, and one outcome only when all agree.

**Why (#1202).** The countertop width check's variant is the wall layout, and the layout is often
not established. Yet when the vendor's overall is wrong for *every* layout the rule allows, the walls
do not matter: the answer is FAIL whichever they are. This module runs the ordinary engine
(`execute`) once per published variant, with nothing else changed, and combines the results:

- every variant decided FAIL → FAIL;
- every variant decided PASS → PASS;
- anything else (a mix, or any variant that abstained) → REVIEW_REQUIRED: the variant is needed.

**No new arithmetic.** Every number comes from `execute`, step for step as it would for one stated
variant: the same four gates before any arithmetic, the same derivations, the same operation. This
module only reads the engine's own findings and compares their outcomes.

**Auditable.** The combined finding carries every variant's calculation in its trace: each variant's
own intermediates, prefixed `<variant>.`, with `<variant>.outcome` and `<variant>.comparison`. A
REVIEW_REQUIRED here keeps that trace too, so the record shows what each variant would have given;
its outcome says plainly that nothing was decided.

Source: issue #1202 · Verification: `tests/verdict/test_every_variant.py`
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rules.parameters import ResolvedParameter
from rules.schema import Applicability
from rules.snapshot import RuleSnapshot
from verdict.engine import ENGINE_VERSION, EngineError, execute
from verdict.finding import Finding
from verdict.operands import VerdictOperand
from verdict.outcomes import Outcome
from verdict.trace import CalculationTrace

__all__ = [
    "VariantOutcome",
    "combined_finding",
    "every_variant_operation",
    "execute_every_variant",
    "trace_result",
    "unanimous_outcome",
]


@dataclass(frozen=True, slots=True)
class VariantOutcome:
    """The engine's finding for one variant, exactly as `execute` returned it."""

    variant: str
    finding: Finding


def every_variant_operation(snapshot: RuleSnapshot) -> str:
    """The name the combined trace gives its operation: the rule's own, for every variant."""
    rule = snapshot.rule
    if not isinstance(rule.applicability, Applicability):
        raise EngineError(f"{rule.id} has no variants to evaluate one by one")
    return f"{rule.operation.type} for every {rule.applicability.discriminator}"


def execute_every_variant(
    snapshot: RuleSnapshot,
    operands: Mapping[str, VerdictOperand],
    parameters: Mapping[str, ResolvedParameter] | None = None,
    *,
    discriminators: Mapping[str, str] | None = None,
) -> tuple[VariantOutcome, ...]:
    """`execute` once per published variant, in the rule's own order, with nothing else changed.

    `discriminators` are the item's other stated discriminators; the rule's own discriminator is
    set to each variant in turn, whatever was stated for it.
    """
    rule = snapshot.rule
    if not isinstance(rule.applicability, Applicability):
        raise EngineError(f"{rule.id} has no variants to evaluate one by one")
    name = rule.applicability.discriminator
    stated = dict(discriminators or {})
    return tuple(
        VariantOutcome(
            variant.when,
            execute(snapshot, operands, parameters, discriminators={**stated, name: variant.when}),
        )
        for variant in rule.applicability.variants
    )


def unanimous_outcome(outcomes: Sequence[VariantOutcome]) -> Outcome:
    """PASS or FAIL only when every variant decided it; otherwise REVIEW_REQUIRED."""
    seen = {item.finding.outcome for item in outcomes}
    if seen == {Outcome.PASS}:
        return Outcome.PASS
    if seen == {Outcome.FAIL}:
        return Outcome.FAIL
    return Outcome.REVIEW_REQUIRED


def trace_result(finding: Finding, name: str) -> object | None:
    """The exact result the engine recorded for derivation `name` in this finding's trace."""
    if finding.trace is None:
        return None
    for entry_name, value in finding.trace.intermediates:
        if entry_name != name or not isinstance(value, tuple):
            continue
        for pair in value:
            if isinstance(pair, tuple) and len(pair) == 2 and pair[0] == "result":
                result: object = pair[1]
                return result
    return None


def combined_finding(
    snapshot: RuleSnapshot,
    outcomes: Sequence[VariantOutcome],
    *,
    reason: str,
    notes: Sequence[str] = (),
) -> Finding:
    """One finding for every variant, its outcome `unanimous_outcome`, its trace every variant's.

    `reason` is the caller's plain sentence for the combined result. The first variant's own notes
    (where each setting came from) are kept, then `notes`. `variant` is left empty: no one variant
    applied. With no variant computed at all, the finding abstains with no trace, as the engine's
    own abstentions do.
    """
    if not outcomes:
        raise EngineError("there are no variant outcomes to combine")
    rule = snapshot.rule
    outcome = unanimous_outcome(outcomes)
    computed = [item for item in outcomes if item.finding.trace is not None]
    first = outcomes[0].finding
    kept_notes = (*first.notes, *notes)
    if not computed:
        return Finding(
            rule_id=rule.id,
            outcome=Outcome.REVIEW_REQUIRED,
            severity=rule.severity,
            reason=reason,
            snapshot_id=snapshot.snapshot_id,
            engine_version=ENGINE_VERSION,
            evidence_refs=first.evidence_refs,
            notes=kept_notes,
        )
    intermediates: list[tuple[str, object]] = []
    for item in outcomes:
        trace = item.finding.trace
        if trace is not None:
            intermediates.extend(
                (f"{item.variant}.{name}", value) for name, value in trace.intermediates
            )
        intermediates.append((f"{item.variant}.outcome", item.finding.outcome.value))
        intermediates.append(
            (
                f"{item.variant}.comparison",
                trace.comparison if trace is not None else item.finding.reason,
            )
        )
    template = computed[0].finding.trace
    assert template is not None
    return Finding(
        rule_id=rule.id,
        outcome=outcome,
        severity=rule.severity,
        reason=reason,
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
        trace=CalculationTrace(
            operation=every_variant_operation(snapshot),
            operands=template.operands,
            intermediates=tuple(intermediates),
            comparison=reason,
            tolerance=None,
            arithmetic_unit=template.arithmetic_unit,
            outcome=outcome,
            engine_version=ENGINE_VERSION,
            operation_version=template.operation_version,
        ),
        evidence_refs=first.evidence_refs,
        notes=kept_notes,
    )
