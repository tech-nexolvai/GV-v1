# ADR-0011 — A declared cross-unit allowance, distinct from tolerance

**Status:** Proposed
**Date:** 2026-08-13
**Decides:** D12 (#97)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Not in force until the admin sets `Status: Accepted`.**
> `scripts/ratify.py` refuses to unblock anything until then.

## Context

Raised by the dev on #47.

ADR-0001 says that when operands were authored in different unit systems, the engine either
applies an **"explicitly declared rounding allowance"** or returns REVIEW REQUIRED. No such
field exists, nothing says what type it has, and step 4 of the engine's execution order
(`docs/DESIGN.md` §3.10) cannot be implemented without it.

The dev also set two constraints, both correct: it must not be inferred from `tolerance`, and it
must not be derived from the authored values.

### Why it is not the tolerance

They measure different things.

| | What it is | Who sets it |
|---|---|---|
| **Tolerance** | how much the drawing may be **wrong** | the client, as a manufacturing allowance |
| **Cross-unit allowance** | how much two renderings of the **same number** may differ | us, as an artefact of dual-dimension drawings |

Letting a tolerance absorb conversion noise is exactly the false PASS ADR-0001 exists to
prevent. On the real GV drawing, mm and inch renderings of one dimension differ by up to
**1.600 mm** against a 1/16″ tolerance of **1.5875 mm** — the noise alone exceeds the whole
tolerance budget. A rule that quietly added them would pass a drawing that is out of tolerance.

### Why it cannot be derived from the values

A band computed from the operands is a function of the data it is meant to police. It would
widen exactly when the values disagree most, which is precisely when it should not.

## Options considered

1. **Reuse `Tolerance`.** Convenient and wrong: it carries the `UNCONFIRMED` sentinel, means
   something different, and invites the two to be swapped by accident.
2. **Derive it from the authored values' rounding quanta.** Removes the need to declare
   anything, at the cost of the guarantee — see above.
3. **A separate rule-level field, defaulting to refusal.**

## Decision

**Option 3.** On `Rule`, beside the existing `arithmetic_unit`:

```python
cross_unit_allowance: Quantity | None = None
```

- **`Quantity`, not `Tolerance`.** A distinct type so the two cannot be interchanged by
  accident, and so an allowance can never be `UNCONFIRMED` — an unstated allowance means
  refusal, not "we do not know yet".
- **Default `None` means mixing is refused.** The safe default is abstention: a rule that says
  nothing gets REVIEW REQUIRED rather than a silent conversion.
- **Rule-level**, because it is a statement about how *this check's* arithmetic treats
  cross-unit operands, not a property of a value.
- **Never read from `tolerance`, never derived from operand values.**

### Open sub-question for the admin

Should a rule that declares an allowance be **flagged in the finding**, the way an overridden
company standard is (#65)? A check that accepted cross-unit noise is arguably something a
reviewer should see, and the machinery for surfacing it already exists.

## Consequences

The rule schema gains one optional field, and `docs/DESIGN.md` §3.8 gains a line. Existing rules
are unaffected: absent means refuse, which is what the engine would have done anyway.

Rule authors gain a way to say "this check tolerates conversion noise up to X" — and have to say
it deliberately, in a field that is visible in review, rather than by widening a tolerance and
hoping nobody asks why.

## Safety impact

Positive, and specific. Without a distinct field the only way to permit a mixed-unit comparison
would be to widen the tolerance — which silently spends manufacturing budget on conversion
noise and leaves no trace that it happened. Separating them keeps the two allowances countable
and keeps the tolerance meaning what the client thinks it means.

## Unblocks

- **#96** — the verdict engine, step 4 of its execution order
