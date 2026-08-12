# ADR-0001 — Unit policy: canonical mm for storage, authored-unit arithmetic for verdicts

**Status:** Proposed
**Date:** 2026-08-13
**Decides:** D1 (#1)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Not in force until the admin sets `Status: Accepted`.**
> `scripts/ratify.py` refuses to unblock anything until then.

## Context

`AGENTS.md` §6 currently states: *"canonical unit = mm"*, and requires all values to be
converted to it. Every dimension on the real GV shop drawing is written twice — millimetres
with the inch fraction in brackets, e.g. `984 [38 3/4]`.

Measured on that drawing:

| mm as drawn | inch as drawn | inch → mm | delta |
|---:|---:|---:|---:|
| 6012 | 236 3/4 | 6013.450 | **1.450** |
| 2025 | 79 3/4 | 2025.650 | 0.650 |
| 1968 | 77 1/2 | 1968.500 | 0.500 |
| 984 | 38 3/4 | 984.250 | 0.250 |
| 864 | 34 | 863.600 | 0.400 |
| 100 | 4 | 101.600 | **1.600** |

**Worst delta: 1.600 mm. A 1/16″ tolerance is 1.5875 mm.** The rounding noise between the
two renderings of the *same* dimension is larger than the tolerance we would judge it by.

Critically, each unit system is internally exact. Verified on the same drawing:

```
2025 + 1968 + 2019 = 6012          79 3/4 + 77 1/2 + 79 1/2 = 236 3/4
  51 + 533 + 457 + 984 = 2025            2 + 21 + 18 + 38 3/4 = 79 3/4
```

Both close exactly. Neither number is wrong; each is a correct rounding of the same design
intent. The error is introduced only by *converting between them*.

## Options considered

1. **Convert everything to mm (the status quo).** Simple and uniform. But a check whose
   operands were authored in different unit systems inherits up to 1.6 mm of pure rounding
   noise. At a 1/16″ tolerance that consumes the entire budget: a perfect drawing can FAIL,
   and — far worse — a drawing that is genuinely 1/16″ out can PASS.
2. **Canonical mm for storage, arithmetic in the authored unit.** Keeps one comparable unit
   for cross-document work and display, while ensuring a single arithmetic operation never
   mixes unit systems. Requires preserving the original token on every observation.
3. **Make inches canonical.** Inverts the problem rather than solving it; the drawings are
   authored mm-primary, and the client's rules are written in inches. Something must convert.
4. **Reject drawings that carry dual dimensions.** Not viable — this is GV's house style on
   every drawing we have seen.

## Decision

**Option 2.**

1. Canonical **mm** remains the storage, cross-document comparison and display unit.
2. Every observation additionally preserves the **exact original token** as a `Fraction`
   plus its authored unit. What the drawing said is never discarded.
3. Each rule declares an **`arithmetic_unit`**. Before any arithmetic, the engine verifies
   that all operands entering a single operation share the authored unit system. If they do
   not, it either applies an **explicitly declared** rounding allowance or returns
   **REVIEW REQUIRED**. It never silently converts.
4. The bracketed alternate reading becomes a **corroboration lane** with a rounding-aware
   band, never an independent operand. A consistent pair is independent evidence for the
   *reading* of a number — not for its *semantic association*.

## Consequences

**This amends a golden rule.** `AGENTS.md` §6 must be updated: canonical mm still holds for
storage, but "convert everything to mm" no longer holds for verdict arithmetic.

Easier: tight tolerances become trustworthy; the dual-dimension convention becomes a free
corroboration source that reduces how often we need PaddleOCR and docTR to agree.

Harder: `Measurement` must carry more than a number, and rules must declare their arithmetic
unit. Mixed-unit checks now abstain rather than answer, which will produce some REVIEW
REQUIRED outcomes that Option 1 would have answered — wrongly.

Forbidden: implicit unit conversion anywhere in the verdict path.

## Safety impact

Directly protects the **primary safety metric**. Under Option 1, a drawing 1/16″ out of
tolerance can be rendered PASS purely by conversion noise — a false PASS that reaches
manufacturing. This is the highest-severity failure mode identified in
`docs/V1_RESEARCH_AND_PLAN.md`, and it was found by measurement, not by inspection.

## Unblocks

- **#43** — `arithmetic_unit` policy enforcement (`units/policy.py`)
- Confirms the shape of `Measurement` (`docs/DESIGN.md` §3.1), which #39 builds and #40–#42
  depend on
