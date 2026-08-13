# ADR-0006 — Effective version by semver, and project scope as a resolver key

**Status:** Accepted
**Date:** 2026-08-13
**Decides:** D10 (#79)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Accepted by the admin on 2026-08-13.**

## Context

Two gaps, both found while planning the applicability resolver (#55), and both blocking it.

### "Effective version" had a policy but no mechanism

ADR-0005 decided that a review takes the *latest published snapshot per applicable rule*. But
`SnapshotStore` (#56) is a dictionary keyed by content hash, with no publication time and no
sequence. #56 deliberately kept timestamps out of the hash so that byte-identical input yields
an identical identifier — which is right, and left "latest" undefined.

### Project scope did not exist at all

Backend proposal §8.1 keys the resolver on *"product category, item type,
material/configuration, semantic type, project scope and effective version"*. Category, config
and version are covered by #53, #55 and ADR-0005. **Project scope appears nowhere in
`docs/DESIGN.md` and no issue implements it** — despite each project being one finalized vendor
and one brand, with reference sets that must stay isolated so one project's references never
leak into another's.

## Options considered — effective version

1. **Highest `rule.version`.** Deterministic, needs no clock, and the version is already part of
   the hashed content so it cannot drift from the snapshot it labels. Its weakness is real: two
   snapshots could share `1.0.0` with different content if someone edits without bumping.
2. **Insertion order into the store.** No new fields, but "latest" would depend on load order
   rather than on anything recorded — the same store rebuilt in a different order would resolve
   differently. Rejected: a resolver whose answer depends on load order is not deterministic in
   any sense worth having.
3. **Publication timestamp stored alongside.** Explicit and total, but introduces a clock into
   rule publication and something extra to persist and trust.

## Decision

### Effective version — highest semver, with uniqueness enforced at publish

The resolver selects the **highest `rule.version`** among published snapshots for a given rule
id. `publish` additionally enforces that **`(rule_id, version)` maps to exactly one content
hash**: publishing different content under a version that already exists is a hard error.

That second half is what makes the first half safe. Option 1's weakness was the ambiguity of two
snapshots sharing a version; enforcing uniqueness converts that ambiguity into a loud failure at
publish time, and states a rule authors can follow: **to change a published rule, bump its
version.**

This composes with ADR-0005 rather than replacing it. The resolver still records the chosen
snapshot hashes on every finding, so an old review is reproduced by replaying those hashes, not
by re-resolving against whatever is now highest.

### Project scope — a resolver key and an isolation boundary

A project carries brand, vendor, parameter overrides and its own reference set. It acts in two
distinct ways, and conflating them would be a mistake:

- **As a resolver key** — it supplies the parameter overrides for a check (filler min/max, field
  cut size, tolerances), layering over global defaults exactly as Raj's checklist describes with
  *"Global / Project Based Input"*.
- **As an isolation boundary** — retrieval and matching filter by project, so one project's
  references can never be offered as evidence in another's review. Backend proposal §7.3 already
  lists project among the retrieval hard filters; this records it as a design requirement rather
  than an implementation detail.

### Vendor is metadata, never a rule key

Vendor identity identifies the project and feeds error-pattern reporting. **Every vendor is held
to the same rule for the same layout.** Rules are GV's own standards, so selecting a rule set by
vendor would mean holding one vendor to a different standard than another — which is not what
the client asked for and would be difficult to defend. A one-off project exception is a
reviewer-approved note on a finding, not a vendor rule set.

### Brand-prototype standards are deferred

A later layer, behind a measured need, consistent with how `AGENTS.md` §4 defers other
capability.

## The full picture

```
category (cabinet/countertop)     -> which checklist
+ layout/config (wall_config)     -> which variant (1/8" vs 1/16")
+ project scope                   -> parameter overrides + reference isolation
+ effective version (pin per run) -> which snapshot, recorded on the finding
= the rule and parameters for this check
```

## Consequences

`SnapshotStore` gains a uniqueness constraint on `(rule_id, version)` and a "highest version"
lookup. Both are small; the constraint is the significant one, because it changes publishing
from permissive to strict and will reject a workflow that previously succeeded.

`rules/` gains a minimal project projection carrying only what the resolver needs — a project
identifier and its parameter overrides. Brand and vendor are business metadata and belong on the
full project record in the control plane (Track C), because `rules/` must not depend on `app/`
(see §2 of the design). Splitting it this way keeps the import boundary intact and avoids
pulling the control plane into the deterministic core.

Reference-set isolation becomes a stated requirement of retrieval and matching rather than an
assumed one, which means it can be tested.

## Safety impact

Positive on both counts.

The uniqueness constraint removes a silent ambiguity: previously two snapshots could share a
version and the resolver would have had no defined way to choose, which is exactly the shape of
a wrong-rule-applied error that would be very hard to spot after the fact.

Reference-set isolation prevents a subtler failure — evidence from one project being matched
into another's review. That would produce a finding that is internally consistent and completely
wrong, and no tolerance check would catch it.

## Unblocks

- **#55** — the applicability resolver, which now has all four keys defined
- **#80** — the project scope record
- Confirms the `SnapshotStore` changes needed for a defined "latest"
