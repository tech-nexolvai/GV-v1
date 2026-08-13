"""The execution order that produces a verdict.

Seven steps, in one fixed sequence (`docs/DESIGN.md` §3.10). **Steps 1 to 4 all run before any
arithmetic**, so every route to PASS passes four gates first:

1. Resolve the applicability variant — cannot establish it → REVIEW_REQUIRED
2. Every operand CORROBORATED or HUMAN_CONFIRMED → else REVIEW_REQUIRED
3. Required operand missing → NOT_FOUND
4. Operands share their authored unit → mixed → REVIEW_REQUIRED (ADR-0001)
5. Evaluate derivations in topological order (ADR-0003)
6. Execute the operation from the typed registry
7. Emit a Finding carrying the trace, the snapshot id and every evidence reference

**The ordering is the safety property, not an implementation detail.** Checking qualification
after computing would still produce the right outcome, but it would mean the engine had already
done arithmetic on evidence it was not entitled to use — and the next person to refactor would
have no reason not to keep the answer. The tests assert the order itself: that an unqualified
operand, a missing one and a mixed-unit pair each abstain *before* any operation function is
called.

This module imports `units/`, `rules/` data types and its own package. Never `evidence/`,
`extraction/`, `retrieval/`, a database or a network — `tests/test_verdict_isolation.py` fails
the build if that ever changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rules.parameters import ResolvedParameter
from rules.schema import Applicability, Rule
from rules.snapshot import RuleSnapshot
from units.measurement import Measurement, MixedUnitError, Unit
from verdict.finding import Finding
from verdict.operands import VerdictOperand
from verdict.outcomes import Outcome
from verdict.registry import (
    OperationKind,
    OperationResult,
    RuleAuthoringError,
    validate_operands,
)
from verdict.registry import resolve as resolve_operation
from verdict.trace import CalculationTrace, TracedOperand

ENGINE_VERSION = "0.1.0"


class EngineError(Exception):
    """Raised when the engine was called in a way that cannot produce an honest verdict."""


def _traced(operands: Mapping[str, VerdictOperand]) -> tuple[TracedOperand, ...]:
    return tuple(
        TracedOperand(name=o.name, value=o.value, source=o.source, evidence_ref=o.evidence_ref)
        for o in operands.values()
    )


def _abstain(
    rule: Rule,
    snapshot_id: str,
    outcome: Outcome,
    reason: str,
    operands: Mapping[str, VerdictOperand],
    *,
    variant: str | None = None,
    notes: Sequence[str] = (),
) -> Finding:
    """Build a finding for a check that declined to decide.

    Deliberately carries no calculation trace: nothing was calculated, and an empty trace would
    imply otherwise. The evidence references are kept, because the reviewer still needs to know
    which values were involved in the abstention.
    """
    return Finding(
        rule_id=rule.id,
        outcome=outcome,
        severity=rule.severity,
        reason=reason,
        snapshot_id=snapshot_id,
        engine_version=ENGINE_VERSION,
        trace=None,
        evidence_refs=tuple(o.evidence_ref for o in operands.values() if o.evidence_ref),
        variant=variant,
        notes=tuple(notes),
    )


def _measurements(operands: Mapping[str, VerdictOperand]) -> list[Measurement]:
    return [o.value for o in operands.values() if isinstance(o.value, Measurement)]


def _authored_units(operands: Mapping[str, VerdictOperand]) -> set[Unit]:
    return {m.unit for m in _measurements(operands)}


def execute(
    snapshot: RuleSnapshot,
    operands: Mapping[str, VerdictOperand],
    parameters: Mapping[str, ResolvedParameter] | None = None,
    *,
    discriminators: Mapping[str, str] | None = None,
) -> Finding:
    """Run one check and return its finding.

    ``snapshot`` pins the exact rule text; ``operands`` are values that already cleared the
    evidence gate; ``parameters`` are the resolved project settings behind the check.
    ``discriminators`` carry what the drawing said about the item — ``wall_config`` and the
    like — used to select the applicability variant.
    """
    rule: Rule = snapshot.rule
    parameters = parameters or {}
    discriminators = discriminators or {}
    notes: list[str] = []

    # ---- step 1: applicability -------------------------------------------------
    # `applicability` is always present: a rule with no layout discriminator declares
    # GlobalApplicability explicitly rather than omitting the field, so absence can never be
    # read as "applies to everything" (ADR-0007).
    variant = None
    tolerance = rule.operation.tolerance
    if isinstance(rule.applicability, Applicability):
        stated = discriminators.get(rule.applicability.discriminator)
        if stated is None:
            return _abstain(
                rule,
                snapshot.snapshot_id,
                Outcome.REVIEW_REQUIRED,
                f"{rule.applicability.discriminator!r} could not be established from the "
                "drawing, so the applicable variant is unknown. The layout is not guessed.",
                operands,
            )
        variant = rule.applicability.variant_for(stated)
        if variant is None:
            return _abstain(
                rule,
                snapshot.snapshot_id,
                Outcome.NO_APPLICABLE_RULE,
                f"no variant of this rule covers "
                f"{rule.applicability.discriminator}={stated!r}, so nothing was checked "
                "(ADR-0004). This is not a pass.",
                operands,
            )
        tolerance = variant.tolerance

    variant_name = variant.when if variant is not None else None

    # An unconfirmed tolerance is publishable for development and can never decide anything.
    if tolerance is not None and not tolerance.is_confirmed:
        return _abstain(
            rule,
            snapshot.snapshot_id,
            Outcome.REVIEW_REQUIRED,
            "the tolerance for this check has not been supplied by the client, so no PASS or "
            "FAIL can be honest. An unset tolerance is not zero.",
            operands,
            variant=variant_name,
        )

    # ---- step 2: every operand qualified ---------------------------------------
    unqualified = [o for o in operands.values() if not o.is_qualified]
    if unqualified:
        detail = ", ".join(f"{o.name} ({o.status.value})" for o in unqualified)
        return _abstain(
            rule,
            snapshot.snapshot_id,
            Outcome.REVIEW_REQUIRED,
            f"evidence is not qualified for: {detail}. Only corroborated or human-confirmed "
            "values may enter a verdict, and a disagreement is never resolved by preferring "
            "one reader.",
            operands,
            variant=variant_name,
        )

    # ---- step 3: required operands present -------------------------------------
    missing = [o.name for o in operands.values() if not o.is_present]
    if missing:
        return _abstain(
            rule,
            snapshot.snapshot_id,
            rule.on_missing,
            f"required input(s) absent: {', '.join(sorted(missing))}. A missing value is not a "
            "passing value.",
            operands,
            variant=variant_name,
        )

    # ---- step 4: one authored unit system --------------------------------------
    units = _authored_units(operands)
    if len(units) > 1:
        allowance = rule.cross_unit_allowance
        if allowance is None:
            return _abstain(
                rule,
                snapshot.snapshot_id,
                Outcome.REVIEW_REQUIRED,
                "operands were authored in different unit systems "
                f"({', '.join(sorted(u.value for u in units))}) and this rule declares no "
                "cross-unit allowance. Converting silently can consume the whole tolerance "
                "budget (ADR-0001), so the check abstains.",
                operands,
                variant=variant_name,
            )
        notes.append(
            f"cross-unit comparison permitted by a declared allowance of "
            f"{allowance.exact_value} {allowance.unit.value}"
        )

    arithmetic_unit = rule.arithmetic_unit

    # ---- steps 5 and 6: derive, then decide ------------------------------------
    try:
        spec = resolve_operation(rule.operation.type)
        if spec.kind is OperationKind.DERIVATION:
            # A derivation states no expectation, so no honest outcome exists for it. ADR-0012
            # rejects this at publish; reaching here means a rule got past that validation, and
            # a broken rule must be loud rather than quietly producing an unjustifiable verdict.
            raise RuleAuthoringError(
                f"rule {rule.id!r} terminates in {rule.operation.type!r}, which derives a value "
                "rather than deciding. It states no expectation, so there is no honest outcome "
                "for it. Use it inside a derivations block and finish with an operation that "
                "has a criterion (ADR-0012)."
            )
        call_args = {
            key: operands[ref].value
            for key, ref in rule.operation.operands.items()
            if ref in operands
        }
        validate_operands(spec, call_args)
        if tolerance is not None:
            call_args["tolerance"] = tolerance.as_measurement()
        result = spec.fn(**call_args)
    except MixedUnitError as error:
        # Defence in depth: step 4 should have caught this. If an operation still raises it,
        # abstaining is the only honest answer — never convert and continue.
        return _abstain(
            rule,
            snapshot.snapshot_id,
            Outcome.REVIEW_REQUIRED,
            f"units could not be combined during the calculation: {error}",
            operands,
            variant=variant_name,
            notes=notes,
        )
    except RuleAuthoringError:
        raise
    except Exception as error:  # noqa: BLE001 - see below
        # An unexpected failure means we do not know the answer. AGENTS.md §2.4 — never turn
        # uncertainty into a pass. Re-raising would also be defensible; abstaining keeps one
        # broken rule from failing a whole package.
        return _abstain(
            rule,
            snapshot.snapshot_id,
            Outcome.REVIEW_REQUIRED,
            f"the check could not be completed: {type(error).__name__}: {error}",
            operands,
            variant=variant_name,
            notes=notes,
        )

    assert isinstance(result, OperationResult)  # guaranteed by the kind check above

    # ---- step 7: the finding ---------------------------------------------------
    # The operation returns calculation facts; only the engine has seen the operand provenance,
    # so only the engine can truthfully assemble the trace.
    trace = CalculationTrace(
        operation=rule.operation.type,
        operands=_traced(operands),
        intermediates=result.intermediates,
        comparison=result.comparison,
        tolerance=result.tolerance,
        arithmetic_unit=arithmetic_unit,
        outcome=result.outcome,
        engine_version=ENGINE_VERSION,
        operation_version=spec.version,
    )
    for resolved in parameters.values():
        if resolved.overrides_a_company_standard:
            notes.append(f"{resolved.name} overrides a company standard")

    return Finding(
        rule_id=rule.id,
        outcome=result.outcome,
        severity=rule.severity,
        reason=result.comparison,
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
        trace=trace,
        delta=result.delta,
        parameter_set_ids=(),
        evidence_refs=tuple(o.evidence_ref for o in operands.values() if o.evidence_ref),
        variant=variant_name,
        notes=tuple(notes),
    )
