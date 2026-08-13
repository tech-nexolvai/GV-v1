# ADR-0010 — Derived expectations may be shown, never issued as instructions

**Status:** Accepted
**Date:** 2026-08-13
**Decides:** D8 (#8)
**Deciders:** admin (AnantBisht07)

## Context

`README.md` states the product *"validates drawings; it never designs them"*. The client's own
material pulls in the other direction.

In `Countertop_Checks_Updated.xlsx`, most variables are marked *"Calculated / System Calc"* —
left filler width, cabinet widths, sink back offset, countertop depth. And `Cabinet_Checks.xlsx`
asks that when the wall-to-wall difference exceeds twice the maximum filler,
*"the extra width need to be distributed along the cabinets"*.

Distributing surplus width across cabinets is not validation. It is a design decision about how
a run should be built.

## The distinction that matters

Computing a derived value and **issuing** one are different acts, and the difference is not
technical.

A check that says *"the countertop is 6012 mm; cabinets and fillers sum to 6010 mm; the
difference is 2 mm"* has computed a derived expectation. It has to — that is what a tolerance
comparison is, and a reviewer cannot understand a FAIL without seeing the arithmetic.

A check that says *"make the left filler 47 mm"* has issued an instruction. If a vendor builds
to it and it is wrong, the question of who decided has a different answer.

## Options considered

1. **Never compute derived values.** Impossible: `sum_within_tolerance` is arithmetic on derived
   sums, and a finding without its calculation trace is unreviewable.
2. **Compute and emit them to the vendor.** What the client's *"System Calc"* framing implies,
   and it makes GV the designer of record for numbers the tool produced.
3. **Compute and display, with a reviewer between the calculation and the vendor.**

## Decision

**Option 3.**

- The engine **may** compute derived expectations, and **must** show them in the finding with
  the full calculation trace. That is how a reviewer understands a verdict.
- A derived value is presented as a **derived expected value** — visibly the output of a
  calculation, not a specification.
- **No computed dimension reaches a vendor without reviewer sign-off.** The redline carries what
  a reviewer approved, never what the engine inferred.
- Filler distribution (#61) is therefore reported as a finding a reviewer acts on, not as a
  produced instruction.

## Consequences

Reviewer-minutes go up relative to a tool that simply emits numbers. That is the intended
trade: the reviewer is the control, and `AGENTS.md` §1 places them there deliberately.

The client may expect more automation than this allows, since their checklist describes the
system calculating these values. Worth stating plainly to Raj rather than discovering at
demonstration: the tool will compute and show the expected filler widths; a person still signs
them off.

This is a contractual and liability boundary as much as a technical one, which is why it is
recorded as a decision rather than left to whoever implements #61.

## Safety impact

Indirect but real. A computed dimension that reaches a factory unreviewed makes the tool the
designer of record, and the failure mode is not a wrong verdict a reviewer can catch — it is a
correct-looking number nobody checked.

## Unblocks

- **#61** — cabinet filler distribution, which is where this bites first
- The redline and report work in Track C (#32)
