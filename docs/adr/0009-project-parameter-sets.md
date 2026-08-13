# ADR-0009 — Versioned parameter sets for values that appear on no drawing

**Status:** Accepted
**Date:** 2026-08-13
**Decides:** D5 (#5)
**Deciders:** admin (AnantBisht07)

> **Recorded after the fact.** `ParameterSet`, `ParameterSetStore` and layered resolution
> shipped in #64, #65, #66, #67 and #80 before this decision was written down. The code is
> right; the record was missing. This ADR closes that gap rather than proposing anything new.

## Context

Nine values the rules require appear on **no drawing at all** (`docs/V1_RESEARCH_AND_PLAN.md`
§F5): door thickness, countertop overhang, backsplash thickness, cabinet side panel thickness,
field cut size, filler min and max, the sink front offset minimum, sink depth, and the on-site
field wall-to-wall dimension.

The client sources them from *"G.C / Client"*, *"Company standard"* or on-site measurement, and
their own checklist distinguishes *"Global / Project Based Input"*.

The backend proposal's data model (§10.1) had **no aggregate for any of this**. Without one,
these values would have become code constants — which is how a "typical" figure becomes an
invisible assumption.

## Options considered

1. **Code constants for the typical values.** Simplest, and it destroys the distinction between
   a value somebody chose and a value nobody noticed. Rejected: after the fact you cannot tell
   which is which.
2. **A flat per-project settings mapping.** Records the number, loses who set it and when.
3. **Versioned, immutable, content-addressed parameter sets, layered global → project → run,
   with provenance on every value.**

## Decision

**Option 3.**

- **Versioned and immutable.** A set is frozen at a version and identified by the hash of its
  content, so a finding can name the exact numbers that judged a drawing months later.
  `(project_id, layer, version)` maps to exactly one content hash, matching the rule-snapshot
  rule in ADR-0006.
- **Three layers, last wins:** `GLOBAL` company standards → `PROJECT` overrides → `RUN` inputs
  measured for one review.
- **Provenance on every value** — a closed vocabulary of `G.C / Client`, `Company standard`,
  `Measured` — plus who set it and when. A value nobody is attributed to cannot be questioned.
- **Missing at every layer is `NOT_FOUND`, never a default.** There is no fallback path
  anywhere in resolution, and tests parse the AST to prove it.
- **An override records what it displaced.** Reporting only the winner would satisfy "which
  layer supplied it" while still hiding that a company standard was set aside.

## Consequences

The client's "typical" values become **seeded standards** rather than code defaults: same
numbers, entirely different accountability. A seeded standard carries provenance, names who set
it, and sits in a layer a project can override and a reviewer can see.

It is also a UI requirement — a reviewer must be able to enter and see these values — which
lands in Track C.

The cost is real: three layers and provenance on every value is more machinery than a settings
dictionary. It is justified by the failure it prevents.

## Safety impact

The failure this prevents is quiet. A defaulted parameter does not crash and does not look
wrong; it produces a confident PASS on a check that was never really performed. Making every
value attributable and every absence an explicit `NOT_FOUND` removes the category.

## Unblocks

Delivered by #64, #65, #66, #67 and #80 — all merged. Consumed by the applicability resolver
(#55) and the verdict engine (#96).
