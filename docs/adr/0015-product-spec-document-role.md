# ADR-0015 — `PRODUCT_SPEC` as a document-backed operand source

**Status:** Proposed        <!-- Proposed | Accepted | Rejected | Superseded by ADR-NNNN -->
**Date:** 2026-08-14
**Decides:** D4 (#4)
**Deciders:** admin

> A coding agent may draft this record. Only the admin may set `Status: Accepted`.
> `scripts/ratify.py` refuses to unblock anything until the status reads Accepted.

## Context

`OperandSource` in `rules/semantic_types.py` has four members: `ARCH`, `SHOP`, `LITERAL` and
`USER_INPUT`. None of them can express *"this value came from a manufacturer's cut sheet."*

Sink interior dimensions do. The client's own checklist says so twice:

- `[CT-upd]` N23/S23/X23 — *"Sink interior dimension ( E ) should comply with the production
  specification"*
- `[Countertop_Checks]` CT008 E42 — the source is **"G.C / Client"**

The worked example Raj gave was a Kohler K-2330-G specification sheet.

Finding **F4** in `docs/V1_RESEARCH_AND_PLAN.md` records the consequence: without a third role the
sink-cutout family — **three of the five countertop checks** — has no authoritative source and can
only ever return `NOT_FOUND`.

### Why this is decidable now, although the brief says otherwise

Issue #4 states that D4 depends on **Q7** (*will GV supply cut sheets per project?*), which is
unanswered — Raj gave the concept and an example but never committed to supplying one per job.

That dependency does not survive being worked through. Both branches of Q7:

| | Role added | Role not added |
|---|---|---|
| **Sheets are supplied** | checks can run | the data exists and **cannot be represented** — checks still fail |
| **Sheets never supplied** | `NOT_FOUND`, correctly | `NOT_FOUND` |

Adding the role is strictly better in one branch and identical in the other. Q7 decides whether the
role is ever *populated*; it does not decide whether it should *exist*. Waiting for Q7 would hold
three checks hostage to an answer that cannot change this outcome.

### The distinction the client material conflates

The checklist points at two different things with one phrase. *"Production specification"* is a
document. *"G.C / Client"* is a person telling us a number over email or on a call.

These are not the same kind of fact, and the difference lands squarely on the evidence gate:

- a value read from a **hashed, versioned document** can be re-read, corroborated by a second route,
  and audited six months later against the exact bytes it came from
- a value someone **typed in** can only ever be `HUMAN_CONFIRMED` on their say-so, and there is
  nothing to go back to

`USER_INPUT` already exists and already covers the second case correctly.

## Options considered

1. **Wait for Q7 before deciding.** Leaves three of five countertop checks unimplementable for an
   answer that, as shown above, cannot change the decision. Rejected.

2. **Reuse `USER_INPUT` for cut-sheet values.** No new member, no migration. But it makes a number
   from a hashed Kohler PDF indistinguishable from a number someone recalled on a phone call. The
   evidence plane would lose the ability to tell a re-checkable fact from an unverifiable one, and
   the trace on a sink finding would say `USER_INPUT` for a value that is fully documented.
   Rejected — this is precisely the collapse the evidence states exist to prevent.

3. **Add `PRODUCT_SPEC` covering every non-drawing source**, documents and typed values alike.
   Simpler vocabulary, same defect as option 2 in a different place: it would let a phone-call
   number wear a document's provenance. Rejected.

4. **Add `PRODUCT_SPEC` as document-backed only**, leaving typed values as `USER_INPUT`. Chosen.

## Decision

Add `PRODUCT_SPEC` to `OperandSource`, defined narrowly:

> **`PRODUCT_SPEC` means the value was read from a manufacturer's specification document that has
> been ingested as a versioned, hashed artifact, exactly like an architectural or shop drawing.**
> A value supplied by a person — by email, on a call, or typed into a form — is `USER_INPUT`, no
> matter how authoritative the person is.

Concretely:

- `OperandSource.PRODUCT_SPEC = "PRODUCT_SPEC"` in `rules/semantic_types.py`
- the client-name prefix `P_` maps to it, alongside `A_` → `ARCH` and `S_` → `SHOP`
- a `PRODUCT_SPEC` operand **must** carry a document version reference. An operand claiming this
  source with no document behind it is an authoring error, not a permissible shortcut
- cut sheets are ingested through the same path as drawings: `documents` / `document_versions`,
  SHA-256, pinned (C1.3, C5.3)
- a per-project product registry — which spec applies to which item — is deferred to its own story
  and is **not** decided here

## Consequences

**Easier.** The sink-cutout family becomes expressible. `CT-3` (#60) can name its expected value's
source instead of having nowhere to point.

**Harder.** Cut sheets become documents, with everything that implies: storage, hashing, versioning,
and eventually extraction from a PDF laid out nothing like a shop drawing. That work is real and
belongs to Track B, not here.

**Forbidden by this.** Backfilling a `PRODUCT_SPEC` operand from a typed value. If a project has no
cut sheet, the honest outcome is `NOT_FOUND` — and per `AGENTS.md` §2.2 that is a result, not a
failure. The temptation this ADR is meant to head off is someone typing the Kohler dimension in
"just for now" and labelling it `PRODUCT_SPEC` because that is what it *is*, in a sense. It is not:
provenance is about what we can re-check, not about where the number ultimately originated.

**Amends no golden rule.** It extends a vocabulary that `AGENTS.md` §2 already assumes is closed and
authored, and adds one more thing the evidence gate can distinguish.

## Safety impact

**Reduces critical false-PASS risk, mildly but in the right direction.**

The alternative in practice — option 2 or 3 — would let an unverifiable number carry the provenance
of a documented one. A sink check comparing a countertop cutout against a *remembered* interior
dimension can produce a confident PASS on a wrong number, and the trace would give a reviewer no
signal that anything was weaker than usual.

Keeping the two apart means a reviewer reading a finding can tell, from the source alone, whether the
expected value is re-checkable. That is the whole reason operand sources are recorded rather than
inferred.

It creates no new path into a verdict: `PRODUCT_SPEC` operands pass the same evidence gate, in the
same states, as everything else.

## Unblocks

- **#60** `A6.3` — CT-3 sink cutout family (width, depth, offsets), currently `needs-architecture`
- Partially informs **#13** `Q5` (sink front offset: minimum or exact) by settling where the
  expected value comes from, though the min-vs-equality question remains a client answer

Does **not** unblock the per-project product registry, which needs its own story, or Q7, which
remains open and now only affects whether the role gets populated.
