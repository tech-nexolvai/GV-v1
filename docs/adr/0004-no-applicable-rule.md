# ADR-0004 — An explicit `NO_APPLICABLE_RULE` outcome

**Status:** Proposed
**Date:** 2026-08-13
**Decides:** D7 (#7)
**Deciders:** admin (AnantBisht07)

## Context

Every countertop check the client has supplied is scoped to one configuration: *"Countertop
with walls on left, right and back side"*, confirmed by their own image captioned *"Vanity
with a wall on 3 sides"*.

`docs/RULE_ENGINE_SPEC.md` §4 anticipates four `wall_config` variants — `back_left_right`,
`back_left`, `back_only`, `island` — but only the three-wall case has an authored rule.

So a package containing an island countertop will pass through the applicability resolver and
match nothing. Today the resolver has no way to say that. The check simply does not run, and
the package comes back with no finding for it.

**A reviewer seeing no findings will reasonably conclude the package was checked and was
fine.** That is a false PASS produced by silence rather than by arithmetic — and it is
invisible to every metric we have, because no finding was emitted to be wrong.

## Options considered

1. **Silently skip (status quo).** An unchecked item is indistinguishable from a passing one.
2. **A distinct `NO_APPLICABLE_RULE` outcome, surfaced to the reviewer as REVIEW REQUIRED.**
   Makes the gap explicit and countable.
3. **Reuse `NOT_FOUND`.** Conflates two different failures: `NOT_FOUND` means *the drawing
   did not give us a required value*, and sends a reviewer hunting for a dimension. "No rule
   covers this layout" sends them to the rulebook instead. Same outcome, wrong instruction.
4. **Fail the package.** Too blunt. Partial coverage is the expected state for the whole
   pilot; blocking on it makes the tool unusable while the rulebook is still small.

## Decision

**Option 2.** Add `NO_APPLICABLE_RULE` to the `Outcome` enum
(`docs/DESIGN.md` §3.4) as a first-class value — **not** a flavour of PASS and not an
absence.

- The applicability resolver returns it when no published rule snapshot matches the resolved
  scope.
- It is surfaced to the reviewer as **REVIEW REQUIRED**, with a reason naming the scope that
  went unchecked (e.g. *"no countertop width rule published for wall_config=island"*).
- It is counted separately in **automation coverage**, so "we checked 6 of 9 applicable
  items" is reportable rather than implied.
- It is distinct from the discriminator being unestablished. If `wall_config` itself cannot
  be determined, that is REVIEW REQUIRED for a different reason — we do not know which rule
  *would* apply. Both abstain; they tell the reviewer different things.

## Consequences

The `Outcome` enum grows, so every consumer — reporting, the redline generator, the reviewer
UI, the metrics — must handle a fifth value. Reports must distinguish "checked and passed"
from "not checked", which is a UI requirement as much as an engine one.

Expect a visible rate of `NO_APPLICABLE_RULE` early on, since only one wall configuration is
authored. That is the honest picture, and it is the number that tells us which rule to write
next.

## Safety impact

This is the highest-leverage cheap safety control available. Every other false-PASS defence
assumes a check ran and produced a wrong answer. This one addresses the case where **no check
ran at all** — which no metric can currently detect, because there is no finding to score.

Silence is the most dangerous possible false PASS, precisely because it looks like success
and leaves no trace to audit.

## Unblocks

- **#55** — `rules/applicability.py`, whose acceptance criteria include *"a package with an
  island countertop cannot render as clean"*
- Confirms `Outcome.NO_APPLICABLE_RULE` in `docs/DESIGN.md` §3.4
- Feeds the automation-coverage metric in **#69**
