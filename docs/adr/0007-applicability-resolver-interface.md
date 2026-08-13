# ADR-0007 — The applicability resolver returns abstentions in band, not an empty list

**Status:** Proposed
**Date:** 2026-08-13
**Decides:** D11 (#89)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. Only the admin may set `Status: Accepted`.

## Context

`docs/DESIGN.md` §1 lists `rules/applicability.py` as the *"deterministic variant resolver"*
and §3.10 step 1 has the engine *"resolve the applicability variant"* before anything else.
Neither states the resolver's signature. §3.8 defines the rule model — `Applicability`,
`ApplicabilityVariant`, and `Applicability.variant_for()` — but nothing describes what the
resolver takes in or hands back.

So #55 cannot be built to the design document, because on this point the design document does
not exist. Two types have to be settled before a line is written:

**What describes the item under check.** The resolver keys on four things (ADR-0006): product
category, layout/config discriminator, project scope, and effective version. There is no
canonical item model in the repository to carry the first two — `evidence/` and `app/models/`
are both empty — so the input type is unspecified.

**What the resolver hands back.** ADR-0004 requires `NO_APPLICABLE_RULE` whenever no published
rule covers an item's scope, and requires it to be *counted* so automation coverage is
reportable. ADR-0005 requires the chosen snapshot identifiers to be recorded on every finding.
Neither is expressible as a bare list of rules, and `Finding` itself is not yet designed.

This is the same class of gap as #43 and #45: an interface that would otherwise be shaped by
whichever caller happened to be written first.

## Options considered — what the resolver returns

1. **A tuple of matching snapshots, empty when nothing matches.** Simplest, and wrong in the
   one way that matters. ADR-0004 exists because an unchecked item that produces silence is
   indistinguishable from a passing one. An empty tuple *is* silence: it puts the burden on
   every caller to remember that empty means `NO_APPLICABLE_RULE` and to synthesise the
   abstention itself. A caller that forgets emits nothing, no finding exists to be wrong, and
   the gap is invisible to every metric we have.
2. **`Finding` objects directly.** Rejected on two counts. `Finding` is the engine's output
   type and does not exist yet, and the resolver could not fill one honestly — it has no
   operands, no arithmetic and no calculation trace, so every such field would be a
   placeholder. It would also point `rules/` at `verdict/` internals, which the §2 import
   table forbids.
3. **Applicable rules and abstentions, both in band.** The resolver states what it found *and*
   what it could not cover, in one value. Nothing has to be remembered by a caller, and the
   unchecked scopes are countable at the point they are discovered.

## Options considered — what the resolver takes

1. **The canonical item model.** The natural answer, unavailable: no such model exists, and
   `evidence/` may import `rules/` but not the reverse, so the dependency would run the wrong
   way.
2. **Loose keyword arguments — `product_type: str, wall_config: str`.** Hard-codes one
   discriminator into the signature. `Applicability.discriminator` is a free string, so a rule
   keyed on material or mount type would need the signature changed — and until it was, the
   only way to pass the value would be by matching on its name at the call site.
3. **A `CheckContext` carrying a discriminator mapping.** Values are looked up by the name the
   rule itself declares, so a second discriminator needs no change to the signature. Same
   principle as #45: a name resolves through a table inside the module that owns it, never by
   string matching at the call site.

## Decision

Option 3 in both cases.

```python
@dataclass(frozen=True, slots=True)
class CheckContext:
    """Everything the resolver may key on. Explicit fields only — nothing inferred."""
    product_type: str
    project: ProjectScope
    discriminators: Mapping[str, str]      # {"wall_config": "back_left_right"}

@dataclass(frozen=True, slots=True)
class ApplicableRule:
    snapshot: RuleSnapshot                 # the effective version, ADR-0006
    variant: ApplicabilityVariant | None   # None when the rule declares no applicability

@dataclass(frozen=True, slots=True)
class Abstention:
    outcome: Outcome                       # NO_APPLICABLE_RULE | REVIEW_REQUIRED
    reason: str                            # names the scope that went unchecked
    rule_id: str | None                    # None when nothing was published for the category

@dataclass(frozen=True, slots=True)
class Resolution:
    applicable: tuple[ApplicableRule, ...]
    abstentions: tuple[Abstention, ...]

def resolve(store: SnapshotStore, context: CheckContext) -> Resolution: ...
```

Four semantics come with it.

**A missing discriminator and an uncovered one are different abstentions.** If the context
carries no value for the discriminator a rule declares, the resolver cannot know which rule
*would* apply: `REVIEW_REQUIRED`. If it carries a value and no variant covers it, we know
exactly what applies — nothing: `NO_APPLICABLE_RULE`. ADR-0004 already draws this line; this
records where it is enforced. Both abstain, and they send a reviewer to different places: one
to the drawing, one to the rulebook.

**Project scope is carried, never used to filter.** ADR-0006 settles that rules are GV's own
standards and every vendor is held to the same rule for the same layout, so no rule is
selected by project. `ProjectScope` rides through the resolution to supply the parameter layer
`rules/parameters.py` consumes. A resolver that filtered rules by project would quietly
recreate per-project rule sets, which ADR-0005 and ADR-0006 both refuse.

**No priority and no firing order.** Width, depth and sink checks all apply to one countertop;
rules do not compete. `applicable` is sorted by rule id for stable reporting only, and no
consumer may attribute meaning to the order. Relying on firing order is what makes
independent rules implicitly dependent — the failure mode conflict-resolution strategies in
conventional rule engines are known for.

**Effective version is delegated, not reimplemented.** The resolver calls
`SnapshotStore.latest()`, which ADR-0006 defines as the highest `rule.version` and which is
safe only because `publish` enforces one content hash per `(rule_id, version)`.

`SnapshotStore` gains one public accessor, `rule_ids()`, because candidate selection cannot
reach into `_by_id` from another module.

## Consequences

The resolver's output is a small type every consumer must handle, rather than a list that can
be iterated and forgotten. That is the point: `NO_APPLICABLE_RULE` becomes something a caller
must actively discard rather than something it must remember to produce.

Reporting and the reviewer UI must render two abstention reasons distinctly, and automation
coverage is computed as applicable over applicable-plus-abstained. Expect a visible abstention
rate early, since only the three-wall layout has an authored rule — that number is the honest
picture of coverage, and it names the next rule to write.

`Finding` remains undesigned. This ADR deliberately does not pre-empt it; the resolver's output
is an input to whatever the engine eventually emits, and ADR-0005's requirement to record
snapshot identifiers is satisfied because `ApplicableRule` carries the snapshot itself.

This amends no golden rule in `AGENTS.md`.

## Safety impact

Directly on the primary metric. The false PASS this project most fears is not an arithmetic
error — it is an item nobody checked, rendering as clean. Returning abstentions in band is what
makes that failure structurally hard: the resolver cannot report a covered item and an
uncovered one the same way, so a reviewer is never shown silence where a rule was missing.

The distinction between the two abstention kinds also keeps review load honest. Folding "no
rule exists" into "cannot establish the layout" would send reviewers hunting through drawings
for a dimension when the real gap is in the rulebook.

## Unblocks

#55 — the applicability resolver and explicit `NO_APPLICABLE_RULE`.
