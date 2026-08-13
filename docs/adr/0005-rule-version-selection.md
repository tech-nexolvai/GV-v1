# ADR-0005 — A review pins the rule snapshots it used, rather than a rule-set version

**Status:** Accepted
**Date:** 2026-08-13
**Decides:** D9 (#75)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Accepted by the admin on 2026-08-13.**

## Context

`docs/DESIGN.md` had no answer to a question the backend proposal assumes: when a review runs,
**which published version of a rule does it use?**

Proposal §8.1 lists the resolver's inputs as *"product category, item type,
material/configuration, semantic type, project scope and **effective version**"*. "Effective
version" appeared nowhere in the design and no issue implemented it. ADR-0002 and #56 made
snapshots identifiable and immutable, but identifying a version is not the same as choosing
one.

### Three axes, and only one of them is this decision

The gap surfaced while two of these were being conflated in conversation, so the design must
now name all three:

| Axis | Decided by | Example |
|---|---|---|
| **Which rule and variant applies** | the **drawing** | a three-wall countertop selects the `back_left_right` variant, a two-wall selects `back_left` — different tolerances, one rule |
| **Which rulebook version applies** | the **run** | a tolerance was edited last month; re-running a six-month-old review must not silently apply the new one |
| **Per-vendor rules** | nobody — this does not exist | rules are GV's own standards |

The first is the applicability resolver (#55, ADR-0004). The second is this ADR. The third is
explicitly not a thing.

**Tolerance values differing between layouts (1/8″ vs 1/16″) is the first axis, not the
second.** They are variants of one rule, selected by `wall_config` from the drawing. Reading
that as a version difference is the specific mistake this ADR exists to prevent.

## Options considered

1. **Latest published wins, with no record.** Simple, and unauditable: re-running an old
   package flips its verdicts because the rules moved underneath it, with nothing to show what
   the original run actually applied.
2. **Pin per review run.** The resolver takes the latest published snapshot per applicable rule
   at run time, and every finding records the snapshot IDs it used. An old review is reproduced
   by replaying those recorded snapshots.
3. **Pin per project.** Each project is bound to a rule-set version at the start. Reproducible,
   but it introduces a rule-set version entity, a project binding, and a migration path every
   time a rule changes — before there is any evidence that projects need to diverge.

## Decision

**Option 2 — pin per review run.**

- The resolver selects the **latest published snapshot per applicable rule at run time**.
- **Every finding stores the snapshot IDs it used.** Already supported: `Rule` carries `id` and
  `version` (#53), and `RuleSnapshot.snapshot_id` is a content hash (#56).
- **An old review is reproduced by replaying its recorded snapshots**, never by re-resolving.
- **Per-project pinning is not built in V1.** Deferred behind a measured need, the same way
  `AGENTS.md` §4 defers Temporal, Qdrant and the rest.
- **Per-vendor rule sets do not exist.** Every vendor is held to the same rule for the same
  layout. Vendor identity is recorded for error-pattern reporting only, never to select a
  rulebook. A one-off project exception is a reviewer-approved note on a finding.

## Consequences

Reproducibility comes from what each finding recorded rather than from a pinning mechanism,
which keeps V1 markedly simpler: no rule-set version entity, no project binding table, and no
migration when a rule changes.

The trade-off is real and accepted: a re-run that does *not* replay uses current rules and may
differ from the original. That is tolerable because the original findings remain intact and
reproducible from the snapshot IDs they carry — the audit question ("what judged this drawing
on the day?") is always answerable, even when the rules have since moved.

Deferring per-project pinning means a rule change reaches every subsequent review immediately.
If a project ever needs to be held on an older rulebook, that is the measured trigger to
revisit this.

## Safety impact

Neutral on false-PASS, positive on auditability. No verdict changes as a result of this
decision; what changes is that every verdict can be traced to the exact rule text that produced
it, which is what `AGENTS.md` §2.7 requires.

The one risk worth naming: a finding that fails to record its snapshot IDs would be
unreproducible and would look identical to one that did. That makes recording the IDs a
correctness requirement of the verdict engine, not a reporting nicety.

## Unblocks

- **#55** — the applicability resolver, which now has a stated boundary: it selects rule and
  variant, and takes the latest published snapshot. It does not implement version pinning.
- `docs/DESIGN.md` §3.11 records this and separates the three axes, so the conflation that
  produced this ADR cannot recur silently.
