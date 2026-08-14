# ADR-0013 — Derivations bind operands by name, and cycles stay unrepresentable

**Status:** Accepted
**Date:** 2026-08-14
**Decides:** D14 (#109)
**Deciders:** admin (AnantBisht07)
**Amends:** [ADR-0003](0003-rule-derivations.md)

> Drafted by a coding agent. **Accepted by the admin on 2026-08-14.**

## Context

ADR-0003 established the typed `derivations:` DAG. Reviewing #54 before implementation surfaced
two defects in it — one that would let a wrong number through, and one that promises an error
message the design makes impossible.

### A positional list cannot bind to a keyword-only operation

`docs/DESIGN.md` §3.8 gives `Derivation.inputs: list[str]`. §3.7 makes every operation
keyword-only, explicitly so there is *"no positional ambiguity"*. The two cannot both hold: a
list has to be matched to parameters by position, which is the ambiguity §3.7 exists to remove.

ADR-0003's own example hides this, because `sum` is variadic and takes its operands as one
collection:

```yaml
- name: sink_zone_total
  operation: sum
  inputs: [CT007, CT008, CT009]
```

The case #54 exists for does not hide it. Plan §F6 records that CT009 is *"derived by rearranging
that same formula"* — a subtraction, where order decides the sign. Written positionally, swapping
two entries produces a rule that **validates cleanly and computes the wrong number**, and the
error is invisible in review because both orderings look equally plausible on the page.

The same file already solved this problem for the rule's final operation: `OperationRef.operands`
is a `dict[str, str]`, binding each operand by the name the operation declares. Derivations were
simply not brought into line.

### The promised cycle path is unreachable

ADR-0003 states both:

- *"A derivation may reference an input, a parameter, or an **earlier** derivation."*
- *"a rule with a cyclic derivation is rejected with the cycle path in the message"*

Backward-only referencing makes a cycle unrepresentable — there is no way to author one, so there
is no cycle path to print. `Rule._derivations_are_acyclic` implements the first rule today, which
means the second describes code that can never run, and #54's acceptance criteria still require
it.

## Options considered — binding

1. **Keep the positional list.** No change, and no way to express `difference(minuend=…,
   subtrahend=…)` without relying on declaration order. Rejected: the failure is silent and
   arithmetic.
2. **Positional list plus per-operation adapters** mapping position to parameter name. Rejected:
   the mapping lives away from the rule, so a reader of the rule still cannot tell which operand
   is which — and an author edits the rule, not the adapter.
3. **A named mapping, as `OperationRef` already uses.** The rule states the binding where the
   author can see it, and swapping two operands becomes a different, visible edit.

## Options considered — acyclicity

1. **Allow derivations in any order, then topologically sort at publish and report a cycle
   path.** More convenient for authors, and it makes the promised error message real. But it
   means cycles are representable and correctness depends on a check running.
2. **Keep backward-only referencing.** A cycle cannot be written down. The cost is that authors
   must declare a derivation before using it, which is one line of ordering discipline in a file
   that is reviewed anyway.

## Decision

**Option 3 for binding, Option 2 for acyclicity.**

```python
class Derivation(BaseModel):
    name: str
    operation: str
    operands: Mapping[str, str]     # operand name -> input, parameter or earlier derivation
```

```yaml
- name: sink_zone_total
  operation: sum
  operands: {values: sink_dimensions}

- name: back_offset
  operation: difference
  operands: {minuend: countertop_depth, subtrahend: sink_zone_total}
```

Backward-only referencing is kept, and is now stated as *the mechanism* by which the graph is
acyclic rather than as a restriction alongside a separate check. The publish-time error names the
offending derivation and the reference it could not resolve:

> `derivation 'back_offset' references 'sink_zone_total', which is not an input, a parameter, or
> an earlier derivation. Derivations may only look backwards, which is what keeps the graph
> acyclic.`

There is no cycle path, because there is no cycle to walk. ADR-0003's promise of one is withdrawn.

**A trace `expression` is a rendering, never an input.** #54 will record a readable form of each
derivation in the calculation trace. That string is generated *from* the typed structure for a
human to read, and is never parsed, evaluated, or accepted back as authored content. ADR-0003
rejected an expression language precisely to keep an evaluator out of a file written by
non-engineers; a round-trippable expression in the trace would reintroduce that surface behind
the reviewer's back.

## Consequences

`docs/DESIGN.md` §3.8 and ADR-0003's example are updated. No rule has been published against the
old shape — the rulebook is still empty — so there is no migration and no snapshot to re-hash.

Authors must declare derivations in dependency order. This is a real constraint and worth
restating in the authoring guide when one exists: the payment for it is that "is this graph
acyclic?" stops being a question anyone has to answer.

`Derivation.operands` and `OperationRef.operands` now have the same shape, so the rule file reads
consistently and a future validator can check both with one code path.

## Safety impact

Positive, on the primary metric. The binding change removes a way to author a rule that computes
the wrong number while passing every validation we have — a wrong operand order in a subtraction
produces a plausible figure, and a plausible wrong figure inside a tolerance comparison is a false
PASS with no trace of how it happened.

The acyclicity change is neutral in effect and positive in kind: it replaces a guarantee that
depends on a check running with one that depends on nothing.

## Unblocks

#54 — the `derivations:` DAG.
