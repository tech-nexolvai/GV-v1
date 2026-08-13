# ADR-0012 — Scalar operation semantics, and where missing input is handled

**Status:** Proposed
**Date:** 2026-08-14
**Decides:** D13 (#100)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Not in force until the admin sets `Status: Accepted`.**
>
> Five of the seven items below follow directly from decisions already ratified, and are marked
> **derived** — they are recorded here for one place to look, not because they are new choices.
> Two are genuinely new and marked **new**. Only the new ones need weighing.

## Context

Raised by the dev on #48 after auditing `docs/RULE_ENGINE_SPEC.md` §2. The spec settles the
obvious semantics — `between`/`minimum`/`maximum` are inclusive, `contains` is substring
presence, `difference_between` reports `a − b` — and leaves seven edge cases undefined. Each one
changes what a check decides, so none can be inferred.

Two of the gaps are worse than under-specification:

- **`conditional_required` is defined nowhere.** It appears in the operations list in
  `AGENTS.md` §7 and in no spec table. There is no signature and no stated outcome.
- **`docs/DESIGN.md` contradicts itself.** §3.7 lists *"a missing required operand → NOT_FOUND"*
  as a rule for every operation, while §3.10 places that check at step 3 of the engine, before
  any arithmetic runs. Both cannot be true.

## Decisions

### 1. `conditional_required` — signature and false-condition outcome — **new**

```python
def conditional_required(*, when: bool, value: object | None) -> OperationResult
```

| Condition | Value | Outcome |
|---|---|---|
| `when=True` | present | `PASS` |
| `when=True` | absent | `NOT_FOUND` |
| `when=False` | either | `PASS`, trace records *requirement not exercised* |

`PASS` on a false condition rather than an abstention, because the rule **did** apply and its
logic determined that nothing was required. That is a decision, not a failure to decide — unlike
`NO_APPLICABLE_RULE` (ADR-0004), where no rule covered the item at all.

**The risk this creates, and the mitigation.** A rule whose condition is always false always
passes, and looks identical to a rule that checked something. So the trace must record that the
requirement was not exercised, and coverage metrics (#69) count those separately. A requirement
that never fires is then visible rather than silently reassuring.

### 2. `exists` — what counts as absent — **derived**

`None`, an empty string and an empty collection are **absent**. **Zero is present.**

Zero is the one that matters: `0 mm` is a legitimate measurement — a flush edge, no gap — and
treating it as missing would turn a real value into `NOT_FOUND`. An empty string, by contrast,
is almost always a failed extraction: absence wearing a value's clothes.

Follows from `AGENTS.md` §2.4 — a missing value is NOT FOUND, and a real zero is not missing.

### 3. `contains` — literal, case-sensitive, no normalization — **derived**

No case folding, no whitespace trimming, no identifier normalization. `"PL-02"` does not contain
`"pl-02"`.

Normalisation is a judgement about whether two spellings mean the same thing, and the verdict
engine makes no judgements. Where normalisation is needed — `X-223` versus `X223` — it belongs
upstream in evidence, where it is recorded, versioned and auditable. A silent transformation
inside the decision path is precisely what the trust boundary in `AGENTS.md` §2.1 forbids.

### 4. `one_of` with an empty allowed set — **derived**

A rule-authoring error, rejected at publish. Not evaluated to `FAIL`.

An empty allowed set means nothing is acceptable, which is almost always a list somebody forgot
to fill in. Evaluating it would produce a confident `FAIL` from a broken rule — the same class of
mistake as an arity mismatch, which §3.6 already treats as an authoring error raised before any
arithmetic.

### 5. `equals` — supported types — **derived**

`Measurement`, `str`, `StrEnum` members, `int` and `Fraction`. Never `float` (ADR-0001).
Comparing across types — a `str` against a `Measurement` — is a rule-authoring error, not
`False`.

Two `Measurement`s must share their **authored** unit. Mixed units raise `MixedUnitError`, which
the engine turns into `REVIEW_REQUIRED` (ADR-0001), rather than being silently converted and
compared.

### 6. `difference_between` is a derivation, not a terminal operation — **new**

`RULE_ENGINE_SPEC.md` §2 marks it *"advisory/derived"*: it reports `a − b` and asks nothing.

A rule whose **terminal** operation is `difference_between` has no pass/fail criterion — it
computes a number and states no expectation, so no honest outcome exists for it. That is a
rule-authoring error, rejected at publish.

It remains available inside a `derivations:` block (ADR-0003), which is what it was always for:
compute a delta, then compare it with an operation that does have a criterion.

### 7. Missing and ambiguous input is the engine's boundary, not each operation's — **derived**

`docs/DESIGN.md` §3.10 steps 1–4 run before any arithmetic. Scalar operations receive **resolved,
qualified values only** and never decide `NOT_FOUND` or `REVIEW_REQUIRED` for themselves.

`docs/DESIGN.md` §3.7 is corrected accordingly: those lines described the engine's behaviour, not
each operation's, and stating them as operation rules invited every operation to reimplement the
policy slightly differently.

**Defence in depth, not duplicated policy.** An operation handed `None` raises a programming
error rather than producing an outcome. It must never be the thing that decides a missing value's
verdict, and it must never quietly proceed on one either.

## Consequences

`#48` becomes implementable. `#96` (the engine) owns steps 1–4 exclusively, so the boundary lives
in one place and can be tested in one place.

Two operations lose a use that looked available: `difference_between` can no longer terminate a
rule, and `one_of` can no longer be published with an empty set. Both were paths to a confident
verdict from a rule that asked nothing coherent.

## Safety impact

Item 1 carries a real risk and states its mitigation. Items 2 and 5 close two routes by which a
non-value becomes a value — an empty extraction reading as present, and a cross-unit comparison
reading as equal. Item 3 keeps a normalisation decision out of the decision path. Items 4 and 6
convert two silently-wrong verdicts into loud authoring errors.

## Unblocks

- **#48** — scalar operations
- Confirms the boundary for **#49**–**#52**
