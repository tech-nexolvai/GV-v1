# ADR-0008 — Severity on a rule, and what "critical false-PASS" means

**Status:** Accepted
**Date:** 2026-08-13
**Decides:** D3 (#3)
**Deciders:** admin (AnantBisht07)

> **Recorded after the fact.** `Severity` shipped in #72 and `Rule.severity` in #53 before this
> decision was written down. The code is right; the record was missing, and an unrecorded
> decision is one nobody can question later. This ADR closes that gap rather than proposing
> anything new.

## Context

`AGENTS.md` §1 names the **critical false-PASS rate** as the primary safety metric, and §9 makes
it a release gate. Neither the rule schema nor anything else recorded **which checks are
critical**, so the metric was not merely unimplemented — it was undefined. Any number computed
for it would have been arbitrary.

Separately, the client asked for something the four outcomes could not express. The `CT009`
back-offset constraint in `Countertop_Checks_Updated.xlsx` says the program *"should throw
warning"*. With only PASS / FAIL / NOT FOUND / REVIEW REQUIRED, a warning had to become a FAIL,
which would fail packages that are fine.

## Options considered

1. **Infer criticality from the check type.** No stable basis: a countertop width check and a
   sink offset check are the same shape and carry very different manufacturing consequences.
2. **A free-text severity field.** A typo publishes cleanly and then mis-sorts the metric.
3. **A closed `Severity` vocabulary on the rule.** Explicit, authored by whoever writes the
   rule, and impossible to misspell.

## Decision

**Option 3.** `Severity` is a closed enum, and every rule declares one:

| Value | Meaning |
|---|---|
| `CRITICAL` | A wrong PASS could be manufactured and cost money. Blocks release. |
| `MAJOR` | A real defect, normally caught downstream. |
| `MINOR` | Cosmetic, or trivially corrected on site. |
| `ADVISORY` | Reported as a warning, never as a failure. |

**The primary metric is defined as false-PASS on `CRITICAL` rules.** That is what makes it
computable, and it is why the metric could not have been implemented before this existed.

`ADVISORY` is what the client asked for: the `CT009` constraint reports rather than fails.

## Consequences

The metric in #69 becomes implementable. Reporting must distinguish an advisory warning from a
failure, or `ADVISORY` collapses back into FAIL in the reviewer's eyes.

**Which checks are actually critical is still unanswered.** That is Q4 (#12), a client question:
the severity of a check is a manufacturing judgement, not an engineering one. Until Raj answers,
rules can carry a severity but the classification is provisional, and #69 stays blocked on the
answer rather than on the mechanism.

## Safety impact

Directly enables the primary release gate. Before this, "critical false-PASS rate" was a phrase
in the documents with no way to compute it — the gate could be neither passed nor failed, which
is worse than failing it, because it looks like a control while enforcing nothing.

## Unblocks

- **#69** — metric implementations, once Q4 supplies the classification
- The release gate in `AGENTS.md` §9
