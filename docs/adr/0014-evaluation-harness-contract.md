# ADR-0014 — Evaluation harness: an unevaluated gate never ships, and a synthetic case is a distinct type

**Status:** Accepted
**Date:** 2026-08-14
**Decides:** D15 (#158)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Accepted by the admin on 2026-08-14.**

## Context

#70 and #71 are both `status: ready` and both unbuildable. `AGENTS.md` §9 lists seven release
gates as a paragraph of prose, and `docs/DESIGN.md` §4 is a testing convention that defines no
harness type. Neither issue can be built to the design document, because on this the design
document says nothing.

The two gaps are separate, but they fail the same way if answered carelessly, which is why they
are decided together.

### The gates are not one kind of thing

Read §9 closely and the seven gates split in two:

| Kind | Gates | Needs |
|---|---|---|
| **Invariant** | OCR disagreement never auto-resolved · unknown unit cannot enter verdict · missing approved source → `NOT_FOUND` · advisory retrieval never a verdict operand | Nothing. Constructed inputs and the engine |
| **Measured** | evidence page+polygon meet threshold · rule change → full gold-set regression · numeric/unit accuracy, match precision, false-PASS | A corpus, and metrics over it (#69) |

The invariant gates are executable **today**. The measured ones cannot run until a gold set and
#69 exist, which may be weeks away. A design that treats all seven identically either blocks the
whole runner on the gold set, or — far worse — lets the four that can run stand in for all seven.

### A synthetic case is not a gold case

`GoldCase` (#68) is the answer key for a real, reviewed client package: PDF paths, observations
carrying page and polygon, matches, expected findings. A synthetic case has no drawing, no page
and no polygon. It also cannot drive the engine as-is, because `execute()` consumes sealed
`VerdictOperand`s, not observations.

## Options considered — gate status

1. **Boolean pass/fail.** A gate that could not run has to be recorded as one or the other.
   Recording it as pass is a lie; recording it as fail makes the runner useless until the gold
   set lands, so it would be switched off, and a switched-off gate is a pass by another name.
2. **Pass/fail, with unevaluable gates omitted from the report.** Worse: the report looks
   complete and the reader has no way to see what is missing. This is the silence failure of
   ADR-0004, one layer up.
3. **Three states, where the third blocks.** `NOT_EVALUATED` is visible, countable, and does not
   ship.

## Options considered — synthetic case identity

1. **Reuse `GoldCase` with optional page and polygon.** One loader, one type. But then a missing
   polygon means either "synthetic, never had one" or "real, and we failed to localise it", and
   **the evidence-localisation gate cannot tell the difference** — it would measure the wrong
   population and report a threshold met that was never tested.
2. **A `synthetic: bool` flag on `GoldCase`.** Same problem, plus the flag defaults to something.
3. **A distinct `SyntheticCase` type.** The two can never be confused by a consumer, because they
   are not the same type.

## Decision

### A. Gates report three states, and only `PASS` ships

```python
class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"     # could not be checked — never counts as held

@dataclass(frozen=True, slots=True)
class GateResult:
    gate_id: str                    # "unknown_unit_cannot_enter_verdict"
    status: GateStatus
    reason: str                     # plain English: what was checked, what happened
    evidence: tuple[str, ...] = ()  # case ids, finding ids, whatever supports the verdict

@dataclass(frozen=True, slots=True)
class ReleaseGateInputs:
    findings: tuple[Finding, ...] = ()
    metrics: Mapping[str, object] | None = None      # from #69; None until it exists
    thresholds: Mapping[str, object] | None = None
    gold_set_version: str | None = None

@dataclass(frozen=True, slots=True)
class GateReport:
    results: tuple[GateResult, ...]

    @property
    def ships(self) -> bool:
        return all(r.status is GateStatus.PASS for r in self.results)

def run_gates(inputs: ReleaseGateInputs) -> GateReport: ...
```

**`ships` is true only when every gate passed.** Not "no gate failed" — that phrasing is how
`NOT_EVALUATED` slips through, and the difference between the two is the entire decision.

Four consequences follow:

- **The runner never computes a metric.** It consumes what #69 produces. A metric defined in two
  places is two definitions that will disagree, and the one inside the gate runner would be the
  one nobody audits.
- **A threshold is a declared input, never a default.** No number for evidence localisation
  exists in any client material or in `AGENTS.md`. An absent threshold yields `NOT_EVALUATED`,
  exactly as an unset tolerance yields `REVIEW_REQUIRED` rather than zero. **A real value is
  still owed** — see *Open* below.
- **"A complete gold-set regression"** means every case in the manifest executed, none skipped,
  with the gold-set version and every rule snapshot id recorded on the run. A partial run is
  `NOT_EVALUATED`, never a pass on the subset that happened to run.
- **Invariant gates are wired into CI immediately.** They need no corpus, and four gates enforced
  today beats seven enforced eventually.

### B. `SyntheticCase` is its own type

```python
@dataclass(frozen=True, slots=True)
class SyntheticCase:
    case_id: str                  # must start with "SYNTH-"
    synthetic: Literal[True]      # not a bool: there is no False to set
    rule_snapshot: RuleSnapshot
    operands: Mapping[str, VerdictOperand]
    parameters: Mapping[str, ResolvedParameter]
    discriminators: Mapping[str, str]
    expected: ExpectedFinding
    seeded_error: SeededError | None    # None = a case that should pass
```

Three independent markers, because one is a thing someone can forget to check: the **type**, the
`Literal[True]` field, and the `SYNTH-` id prefix. The loader refuses to read a `SyntheticCase`
from the real gold-set directory and refuses to read a `GoldCase` from the synthetic one.

**Generation is deterministic and explicit, not random.** One builder per seeded error class —
off-by-tolerance, count mismatch, missing operand, unit mismatch — each stating the error it seeds
and the outcome it expects. A generator that randomises the input *and* derives the expected
answer from the same code will agree with a bug in that code; the expectation has to be authored,
not computed.

**The F1 dual-unit case carries the authored token**, e.g. `"984 [38 3/4]"`, not a pre-parsed
number. A case that hands the engine a clean value has not exercised the cross-unit consistency
check at all, which is the one thing that scenario exists to test.

**#71 runs the engine and asserts per case; it does not compute rates.** Comparing an actual
finding to an expected one is an assertion. Aggregating across cases into a false-PASS rate is a
metric, and belongs to #69.

## Consequences

The gate report gains a state every consumer must handle, and CI gains four enforced gates now
rather than seven later. A green report on four gates will read as "shipped" to a human skimming
it, so the report must always list all seven with their status — a `NOT_EVALUATED` line is the
point, not noise to be filtered out.

`eval/` gains two case types and two loaders. That is more code than one type with optional
fields, and it is the price of the localisation metric never measuring a synthetic case.

## Safety impact

Directly on the primary metric, in the place furthest from the arithmetic. Every other safeguard
in this system is enforced by the release gates; if the gates can report success without having
run, then nothing below them is enforced either. `NOT_EVALUATED` blocking is the single decision
that keeps §9 meaningful before the gold set exists.

The synthetic/gold separation protects the evidence-localisation number specifically. A metric
computed over a population that silently includes cases which never had a polygon is not a
conservative error — it reports better localisation than was achieved.

## Open

**The evidence-localisation threshold has no value.** It is not in `AGENTS.md` §9, the client
material, or the plan. Until an admin or the client sets one, that gate returns `NOT_EVALUATED`
and blocks — which is correct, and is also a reason to set it before the gold set lands rather
than after.

## Unblocks

#70, #71.
