"""Which published rules apply to one item, and what went unchecked.

This is the first step of every review (`docs/DESIGN.md` §3.10), and the step where the
project's worst failure mode is either caught or created. That failure is not an arithmetic
error — it is an item nobody checked, rendering as clean. A reviewer who sees no findings for a
countertop will reasonably conclude it was checked and was fine.

So this resolver never answers with silence. It returns the rules that apply **and** the scopes
it could not cover, in one value (ADR-0007). A caller has to actively discard an abstention;
there is no way to drop one by forgetting it exists.

Everything here is decided from explicit fields. No model, no retrieval, no inference about
which layout a drawing probably shows. If the layout is not stated, we say so and stop.

See ADR-0004 (`NO_APPLICABLE_RULE`), ADR-0005 (findings pin their snapshots), ADR-0006
(effective version, project scope) and ADR-0007 (this interface).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rules.project import ProjectScope
from rules.schema import Applicability, ApplicabilityVariant, GlobalApplicability
from rules.semantic_types import ProductType
from rules.snapshot import RuleSnapshot, SnapshotStore
from verdict.outcomes import Outcome


@dataclass(frozen=True, slots=True)
class CheckContext:
    """Everything the resolver may key on, stated explicitly.

    ``discriminators`` is keyed by the discriminator name a rule declares — ``wall_config``
    today, possibly a material or mount type later. Looking values up by the rule's own
    declared name is what lets a new discriminator arrive without changing this signature, and
    without any caller matching on a name itself.

    A discriminator that is simply absent from the mapping is not the same as one whose value
    is unknown to every variant. The first means we do not know which rule *would* apply; the
    second means we know exactly what applies — nothing.
    """

    product_type: ProductType
    project: ProjectScope
    discriminators: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ApplicableRule:
    """One rule that applies, at the version and variant resolved for this check."""

    snapshot: RuleSnapshot
    variant: ApplicabilityVariant | None
    """The resolved branch, or ``None`` for a rule declared ``{scope: global}``.

    ``None`` here means *this rule declared that it has no discriminator*, never *we could not
    work out which branch applies*. A rule that omits its applicability entirely does not
    validate (ADR-0007), so the ambiguous case cannot reach this far.
    """

    @property
    def rule_id(self) -> str:
        return self.snapshot.rule_id


@dataclass(frozen=True, slots=True)
class Abstention:
    """A scope that went unchecked, and why.

    ``reason`` is written for a reviewer rather than a log: it names the thing that was not
    checked, so the message answers "what did this miss?" without needing the code open.
    """

    outcome: Outcome
    reason: str
    rule_id: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in (Outcome.NO_APPLICABLE_RULE, Outcome.REVIEW_REQUIRED):
            raise ValueError(
                f"an abstention is NO_APPLICABLE_RULE or REVIEW_REQUIRED, got {self.outcome}. "
                "Resolution decides nothing — a decisive outcome here would be a verdict "
                "reached without arithmetic."
            )


@dataclass(frozen=True, slots=True)
class Resolution:
    """What applies to one item, and what could not be covered.

    Both halves matter. ``applicable`` alone would let an uncovered item look identical to one
    with nothing wrong with it.
    """

    applicable: tuple[ApplicableRule, ...]
    abstentions: tuple[Abstention, ...]

    project: ProjectScope
    """The project this resolution was made for.

    Carried, never used to filter. Rules are GV's own standards and every vendor is held to
    the same rule for the same layout (ADR-0006), so no rule is selected by project. What the
    project supplies is the parameter layer `rules/parameters.py` reads, and attaching it to
    the result is what stops one project's overrides reaching a check resolved for another.
    """

    @property
    def is_fully_covered(self) -> bool:
        """True when every candidate rule resolved. False means a reviewer has work."""
        return not self.abstentions

    @property
    def checked_count(self) -> int:
        """Rules that will actually run — the numerator of automation coverage."""
        return len(self.applicable)

    @property
    def considered_count(self) -> int:
        """Rules that applied or should have — the denominator of automation coverage.

        Counting abstentions here is what makes "we checked 6 of 9 applicable items"
        reportable rather than implied (ADR-0004).
        """
        return len(self.applicable) + len(self.abstentions)


def resolve(store: SnapshotStore, context: CheckContext) -> Resolution:
    """Return the rules that apply to this item, and the scopes that went unchecked.

    Deterministic and reproducible from ``context`` alone: the same store and the same context
    always give the same answer, and rebuilding the store in a different order cannot change
    it.

    The result carries **no priority and no firing order**. Width, depth and sink checks all
    apply to the same countertop, so rules do not compete. ``applicable`` is sorted by rule id
    for stable reports only, and no caller may read meaning into that order — relying on firing
    order is what turns independent rules into implicitly dependent ones.
    """
    applicable: list[ApplicableRule] = []
    abstentions: list[Abstention] = []

    candidates = _candidates_for(store, context.product_type)
    if not candidates:
        return Resolution(
            applicable=(),
            abstentions=(
                Abstention(
                    outcome=Outcome.NO_APPLICABLE_RULE,
                    reason=(
                        f"no rule is published for product type "
                        f"{context.product_type.value!r}, so nothing about this item was "
                        f"checked"
                    ),
                ),
            ),
            project=context.project,
        )

    for snapshot in candidates:
        outcome = _resolve_one(snapshot, context)
        if isinstance(outcome, ApplicableRule):
            applicable.append(outcome)
        else:
            abstentions.append(outcome)

    return Resolution(
        applicable=tuple(applicable),
        abstentions=tuple(abstentions),
        project=context.project,
    )


def _candidates_for(store: SnapshotStore, product_type: ProductType) -> tuple[RuleSnapshot, ...]:
    """Return the effective snapshot of every rule covering this product type.

    "Effective" is the highest version, delegated to :meth:`SnapshotStore.latest` rather than
    recomputed here — that definition is ADR-0006's, and it is only safe because ``publish``
    enforces one content hash per ``(rule_id, version)``.

    Product type matches exactly and case-sensitively. It is an enum validated at publish
    (ADR-0007), so there is no near-miss to be lenient about.
    """
    snapshots = []
    for rule_id in store.rule_ids():
        latest = store.latest(rule_id)
        if latest is not None and latest.rule.product_type == product_type:
            snapshots.append(latest)
    return tuple(sorted(snapshots, key=lambda s: s.rule_id))


def _resolve_one(snapshot: RuleSnapshot, context: CheckContext) -> ApplicableRule | Abstention:
    """Resolve one candidate rule to its variant, or explain why it could not be."""
    applicability = snapshot.rule.applicability

    if isinstance(applicability, GlobalApplicability):
        # The rule declared that it has no discriminator. That declaration is required, so
        # this is a statement by the author rather than an omission we interpreted.
        return ApplicableRule(snapshot=snapshot, variant=None)

    assert isinstance(applicability, Applicability)  # the union has no third member
    discriminator = applicability.discriminator
    value = context.discriminators.get(discriminator)

    if value is None:
        # We do not know which rule *would* apply, so we cannot say a rule is missing.
        # Guessing the layout here is the one thing that would let a wrong tolerance through.
        return Abstention(
            outcome=Outcome.REVIEW_REQUIRED,
            reason=(
                f"{snapshot.rule_id} needs {discriminator!r} to choose its variant, and the "
                f"drawing did not establish it — the layout is a question for the reviewer, "
                f"never a guess"
            ),
            rule_id=snapshot.rule_id,
        )

    variant = applicability.variant_for(value)
    if variant is None:
        # We know exactly what applies: nothing. That sends a reviewer to the rulebook,
        # whereas the branch above sends them to the drawing.
        return Abstention(
            outcome=Outcome.NO_APPLICABLE_RULE,
            reason=(
                f"no variant of {snapshot.rule_id} covers {discriminator}={value!r}, so this "
                f"item was not checked against it"
            ),
            rule_id=snapshot.rule_id,
        )

    return ApplicableRule(snapshot=snapshot, variant=variant)
