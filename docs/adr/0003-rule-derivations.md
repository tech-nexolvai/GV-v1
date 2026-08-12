# ADR-0003 — Typed `derivations:` DAG in the rule schema

**Status:** Accepted
**Date:** 2026-08-13
**Decides:** D2 (#2)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Accepted by the admin on 2026-08-13.**

## Context

The rule schema in `docs/RULE_ENGINE_SPEC.md` gives each rule a single `operation:` block:
resolve operands, apply one typed operation, emit an outcome.

The client's real rules do not have that shape. From
`Countertop_Checks_Updated.xlsx`, sheet `Countertop_Checks`:

```
CT004 = CAB_SIDE_THK + CT011 + CT012 + CT013 + CAB_SIDE_THK
CT010 = C.T_OH + CT007 + CT008 + CT009 + B.S_THK
CT009 warning if (CT010 − C.T_OH − CT007 − CT008 − B.S_THK) < global minimum
```

Two things break the single-operation model. First, `CT004` and `CT010` are each built from
five sub-values, so the operand of the comparison is itself the result of a computation.
Second, `CT009` is obtained by *rearranging* `CT010`'s formula and then compared against a
minimum — a two-step chain where the intermediate has a name the client uses.

Roughly the client's whole countertop rule set is inexpressible today. This is a larger gap
than the aggregate operations `RULE_ENGINE_SPEC.md` §2 already identified; those let us sum a
list, but not feed one computed value into another.

## Options considered

1. **Status quo — one operation per rule.** Cannot express any chained formula. The client's
   headline depth check simply cannot be written.
2. **A `derivations:` block — named intermediates, each computed by a typed registry
   operation from inputs, parameters or earlier derivations, validated acyclic at publish
   time.** Composes without introducing an interpreter.
3. **Allow formula strings in rule YAML** (`"C.T_OH + CT007 + CT008"`). Directly violates
   `AGENTS.md` §2.2 — no `eval`, no executable rule text. A rule file would become code, and
   rule files are authored by non-engineers. Rejected outright.
4. **Hard-code a bespoke Python operation per composite rule** (e.g. a `countertop_depth`
   operation). Keeps the typed registry, but every new client rule needs an engineer and a
   release. It also moves rule logic out of a reviewable YAML file and into code, which is
   exactly where a domain expert cannot check it.

## Decision

**Option 2.** Add a `derivations:` block to the rule schema:

```yaml
derivations:
  - name: sink_zone_total
    operation: sum
    inputs: [CT007, CT008, CT009]
```

Rules:

- A derivation may reference an input, a parameter, or an **earlier** derivation.
- Every derivation step is a call into the **existing typed operation registry**. No new
  execution mechanism, no expression parser, no `eval`.
- The dependency graph is validated **acyclic at publish time**, not at execution time. A
  cycle is a rule-authoring error and must be caught before the rule can ever run.
- Every intermediate appears in the calculation trace by name, in evaluation order.

## Consequences

The engine gains one step: evaluate derivations in topological order before the final
operation (`docs/DESIGN.md` §3.10, step 5). `rules/derivations.py` is new.

Traces get longer — deliberately. A reviewer looking at a failed depth check should see
`sink_zone_total = 254.0 mm` as a named line, matching the client's own vocabulary, rather
than a single opaque comparison.

Publishing gets stricter: a rule with a cyclic derivation is rejected with the cycle path in
the message.

## Safety impact

Neutral-to-positive, and the "neutral" part is the point. The composition happens between
typed operations that are already individually tested; nothing new is executed. Option 3
would have been a severe regression — an injection surface in a file authored by
non-engineers — which is why it is rejected rather than deferred.

The positive: without this, the client's chained rules would be implemented as one-off Python
functions (Option 4), where the arithmetic is invisible to the domain expert who actually
knows whether it is right.

## Unblocks

- **#54** — `rules/derivations.py`
- **#59** — CT-2 countertop depth (also needs Q2 and Q13)
- Confirms the `Derivation` model sketched in `docs/DESIGN.md` §3.8
