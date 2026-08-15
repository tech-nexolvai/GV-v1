"""A proposed rule change, and what has to be true before anyone can approve it.

`rules/` can already author, validate and snapshot. What has not existed is the governance around
*publishing* — and until it does, a rule can be written and used with no approval and no regression,
which is the single easiest way to change what the system decides without anyone noticing.

This module is the entry point: the object a human raises, and the validation that has to pass
before approval is even possible. Approval and publication are `D6.3`; the regression gate is
`D6.2`. Keeping them separate is deliberate — validation asks *"is this a coherent rule?"*, approval
asks *"may this person publish it?"*, and regression asks *"does it break anything?"*. Three
different questions, and a proposal that conflates them would let a well-formed rule from an
authorised person ship without anyone checking the third.

**A proposal is a suggestion, not a pending change.** There is no state in which it is "waiting to
apply". Nothing here writes a snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from rules.publication import unconfirmed_tolerance_count
from rules.schema import Applicability, Rule
from rules.snapshot import RuleSnapshot, compute_snapshot_id
from verdict.registry import REGISTRY, UnknownOperationError, resolve


class RegistryNotLoadedError(RuntimeError):
    """Raised when validation is attempted against an empty operation registry.

    Distinct from a rule naming an unknown operation. Reporting every rule invalid because
    nobody registered the specs would send an author looking for a fault in their rule.
    """


class ValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Whether a proposed rule is coherent, and every reason it is not.

    Every problem is collected rather than raising on the first, because an author fixing a rule
    one error at a time will make three round trips where one would do — and each round trip is a
    chance to lose interest and route around the process.
    """

    status: ValidationStatus
    problems: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.status is ValidationStatus.VALID

    def __str__(self) -> str:
        if self.is_valid:
            return "valid"
        return f"invalid: {'; '.join(self.problems)}"


def validate(rule: Rule) -> ValidationResult:
    """Check a proposed rule against the schema and the typed operation registry.

    Pydantic has already enforced the schema by the time a `Rule` exists, so what is left is what
    the type system cannot express: that the named operation is registered, that its operands match
    its signature, and that every operand refers to something the rule actually declares.

    A rule naming an unregistered operation is the case ADR-0003 exists to prevent. It would author
    cleanly, publish cleanly, and then fail at the moment a real drawing was being checked.
    """
    problems: list[str] = []

    # An empty registry would report every rule as naming an unknown operation — which reads as
    # "your rule is wrong" when the truth is that nobody wired the operations up. Distinguish the
    # two loudly, for the same reason the risk-control guard raises when it parses no rule ids.
    if not REGISTRY:
        raise RegistryNotLoadedError(
            "the operation registry is empty, so every rule would be reported invalid. Register "
            "the operation specs before validating — see verdict/operations/*_SPECS."
        )

    try:
        spec = resolve(rule.operation.type)
    except UnknownOperationError:
        problems.append(
            f"operation {rule.operation.type!r} is not in the typed registry. A rule cannot name an "
            "operation that does not exist — it would publish cleanly and fail on a real drawing."
        )
    else:
        expected = set(spec.operands)
        supplied = set(rule.operation.operands)
        if missing := sorted(expected - supplied):
            problems.append(f"operation {spec.name!r} is missing operand(s): {missing}")
        if unexpected := sorted(supplied - expected):
            problems.append(f"operation {spec.name!r} does not take operand(s): {unexpected}")

    # NOT checked here: whether each operand's *source* names something the rule declares.
    # `rules/schema.py` already rejects that at construction — a `Rule` carrying a dangling operand
    # cannot be built at all. Re-checking it would be unreachable code that looks like a safeguard,
    # which is worse than no code: the next reader would trust it and not look for the real one.
    #
    # What the schema does not check is the operand *names* against the registry signature, because
    # the registry is a runtime concern. That is the gap this function closes.

    if isinstance(rule.applicability, Applicability) and not rule.applicability.variants:
        problems.append("applicability declares no variants")

    return ValidationResult(
        status=ValidationStatus.INVALID if problems else ValidationStatus.VALID,
        problems=tuple(problems),
    )


@dataclass(frozen=True, slots=True)
class RuleProposal:
    """One proposed change to the rulebook, raised by a named human.

    Frozen and append-only: a revised proposal is a **new** proposal, not an edited one. The record
    of what was originally proposed is part of why a rule change is defensible six months later, and
    it is exactly the record someone would be tempted to tidy after a review goes badly.
    """

    rule_id: str
    proposed: Rule
    author: str
    rationale: str
    validation: ValidationResult
    supersedes: str | None = None
    """The snapshot id this would replace, or None for a new rule."""

    raised_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.author.strip():
            raise ValueError(
                "a proposal must name its author. An unattributed rule change cannot be reviewed — "
                "the first question about any change is who wanted it and why."
            )
        if not self.rationale.strip():
            raise ValueError(
                "a proposal must carry a rationale. 'What changed' is visible from the diff; 'why' "
                "is not, and it is what an approver is actually judging."
            )
        if self.rule_id != self.proposed.id:
            raise ValueError(
                f"proposal names rule {self.rule_id!r} but carries rule {self.proposed.id!r}"
            )

    @property
    def approvable(self) -> bool:
        """Whether this may be put to an approver at all.

        An invalid proposal cannot be approved regardless of who is asking. Authority decides
        *whether* a coherent change ships, not whether an incoherent one is coherent.
        """
        return self.validation.is_valid

    @property
    def snapshot_id(self) -> str:
        """The content hash the rulebook would carry if this were published."""
        return compute_snapshot_id(self.proposed)


def propose(
    rule: Rule, *, author: str, rationale: str, current: RuleSnapshot | None = None
) -> RuleProposal:
    """Raise a proposal, validating it on the way in.

    Validation happens here rather than at approval so an author finds out immediately, while they
    still have the change in their head.
    """
    return RuleProposal(
        rule_id=rule.id,
        proposed=rule,
        author=author,
        rationale=rationale,
        validation=validate(rule),
        supersedes=current.snapshot_id if current is not None else None,
    )


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


def _tolerance_text(rule: Rule) -> dict[str, str]:
    """Every tolerance in a rule, keyed by where it sits."""
    found: dict[str, str] = {}
    if isinstance(rule.applicability, Applicability):
        for variant in rule.applicability.variants:
            found[f"when {variant.when}"] = str(variant.tolerance.value)
    if rule.operation.tolerance is not None:
        found["operation"] = str(rule.operation.tolerance.value)
    return found


def describe_change(current: Rule | None, proposed: Rule) -> str:
    """What this change does, in language an approver can act on.

    The approver is the client. A diff of two YAML files tells them a line moved; it does not tell
    them the check just got twice as strict on island layouts. This is the difference between a
    review and a rubber stamp, and `D6` requires a human approval that means something.
    """
    if current is None:
        return (
            f"New rule {proposed.id} ({proposed.version}) — {proposed.name or 'unnamed'}. "
            f"Severity {proposed.severity.value}, checked in {proposed.arithmetic_unit.value}."
        )

    changes: list[str] = []

    if current.version != proposed.version:
        changes.append(f"version {current.version} → {proposed.version}")
    if current.severity != proposed.severity:
        changes.append(
            f"severity {current.severity.value} → {proposed.severity.value} — this changes whether "
            "a failure blocks release"
        )
    if current.operation.type != proposed.operation.type:
        changes.append(f"operation {current.operation.type} → {proposed.operation.type}")
    if current.arithmetic_unit != proposed.arithmetic_unit:
        changes.append(
            f"arithmetic unit {current.arithmetic_unit.value} → {proposed.arithmetic_unit.value}"
        )

    before, after = _tolerance_text(current), _tolerance_text(proposed)
    for where in sorted(set(before) | set(after)):
        was, now = before.get(where), after.get(where)
        if was == now:
            continue
        if was is None:
            changes.append(f"tolerance added for {where}: {now}")
        elif now is None:
            changes.append(f"tolerance removed for {where} (was {was})")
        else:
            changes.append(f"tolerance for {where}: {was} → {now}")

    if set(current.inputs) != set(proposed.inputs):
        added = sorted(set(proposed.inputs) - set(current.inputs))
        removed = sorted(set(current.inputs) - set(proposed.inputs))
        if added:
            changes.append(f"reads new input(s): {added}")
        if removed:
            changes.append(f"no longer reads: {removed}")

    unconfirmed = unconfirmed_tolerance_count(proposed)
    if unconfirmed:
        changes.append(
            f"{unconfirmed} tolerance(s) still unconfirmed — this rule cannot reach production "
            "until the client supplies a number (ADR-0011)"
        )

    if not changes:
        return f"{proposed.id}: no material change."
    return f"{proposed.id}:\n" + "\n".join(f"  - {c}" for c in changes)
