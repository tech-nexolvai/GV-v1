# ADR-0017 — The client's `CT0xx` codes are the canonical vocabulary

**Status:** Proposed        <!-- Proposed | Accepted | Rejected | Superseded by ADR-NNNN -->
**Date:** 2026-08-15
**Decides:** D6 (#6)
**Deciders:** admin

> A coding agent may draft this record. Only the admin may set `Status: Accepted`.
> `scripts/ratify.py` refuses to unblock anything until the status reads Accepted.

## Context

`rules/semantic_types.py` uses names we invented — `countertop_overall_width`, `cabinet_width`,
`filler_width`. The client's own workbook uses a different set, and has done all along:

- `CT001`–`CT013` in the `Countertop_Checks` variable table
- named variables alongside them: `B.S_THK`, `C.T_OH`, `CAB_SIDE_THK`
- `A_` / `S_` / `F_` prefixes for architectural, shop and field values

Two things make his set the stronger candidate.

**It is anchored to a diagram.** `CT_image10` defines each code *positionally* on an annotated
drawing. Our names are prose, and prose about geometry is exactly where two readers diverge —
"countertop width" is unambiguous until someone asks whether it includes the end panel, which is
`Q3` (#11) and still open precisely because the phrase does not settle it.

**It is already his language.** `D6` governance (#144) requires a human to approve every rule change,
and the human is the client. A rulebook written in vocabulary he has to translate is one he will
approve without fully reading.

## Options considered

1. **Keep our provisional names, map his codes at the boundary.** Preserves the current code with no
   migration. But it puts a translation step between the client and the rules he must approve, and
   every future rule is authored in a vocabulary neither party uses natively. Rejected.

2. **Adopt `CT0xx` and discard the descriptive names.** Cleanest single vocabulary. But a rule file
   reading `CT007 - CT008` is unreadable to anyone without the workbook open, and the people
   debugging a failed check are usually us. Rejected.

3. **Adopt `CT0xx` as canonical, keep the descriptive names as aliases.** The rulebook speaks the
   client's language; our names survive as documentation and as a lookup for readers who do not
   have `CT_image10` to hand. Chosen.

## Decision

`SemanticType` members become the client's codes. The descriptive names we invented become aliases
resolving to them, carrying provenance like every other alias (B7.4).

Concretely:

- `SemanticType` values are `CT001`, `CT002`, … and the named variables `B.S_THK`, `C.T_OH`,
  `CAB_SIDE_THK`
- each member carries a `label()` giving the plain-English name, for reports and error messages
- `ALIASES` maps our former names to the canonical codes, so existing rule files and tests keep
  resolving
- the `A_` / `S_` / `F_` prefixes already in `_PREFIX_SOURCES` are unchanged — they were his
  convention from the start

### What this deliberately does **not** decide

**`CT011`–`CT013` are not adopted.** They appear only inside formula `F38` and are never defined as
rows in the variable table. We do not know what they measure. Creating enum members for them would
be inventing three semantic types and giving them the authority of the client's own vocabulary —
the same class of error as the `±1/8″` placeholder, which reached `RULE_ENGINE_SPEC` §4 and began
reading as fact. They stay unmapped, and `Q15` (#16) asks him to define them.

**Which of `CT009` / `CT010` is read and which is derived stays open.** His table marks *both*
"Calculated" and each formula references the other. That is `Q6` (#14) and this ADR does not touch
it: adopting a vocabulary names the things, it does not settle their dependency direction. Only
`CT001` is explicitly marked `"Measured" / "Field Installation"` — the one value the workbook states
is read off a drawing.

**Whether the codes are final is unknown.** `Q15` records "are `CT001`–`CT013` final or renamed?" as
NOT STATED. This is the main risk in adopting them, and the alias layer is the mitigation: a rename
becomes an alias-table entry, not a schema migration. Adopting a vocabulary that may be renamed is
cheaper than authoring an entire rulebook in one nobody else uses.

## Consequences

**Easier.** Rule files read the way the client's checklist reads, so a rule review is a comparison
rather than a translation. A reviewer with `CT_image10` open can check a rule against the diagram
directly.

**Harder.** A rule file reading `CT007` needs `label()` or the diagram to be intelligible to us. That
is a real cost and the reason option 2 was rejected — the descriptive names have to survive.

**Forbidden by this.** Inventing a code the client has not defined. If a rule needs a semantic type
with no `CT0xx` equivalent, it uses a descriptive name until he assigns one; it does not get a
plausible-looking code. A fabricated `CT014` would be indistinguishable from his own vocabulary at a
glance, and that is exactly how a guess acquires authority.

**Amends no golden rule.** `AGENTS.md` §2 already assumes the vocabulary is closed and authored; this
changes which closed set we author against.

## Safety impact

**Mildly reduces false-PASS risk, by removing a translation step.**

The failure this addresses is not arithmetic. It is a rule authored against *our* reading of
"countertop width" when the client meant something slightly different — the end-panel question (`Q3`)
is a live example of exactly that ambiguity. A rule that computes the wrong quantity correctly
produces a confident PASS, and nothing downstream catches it because the arithmetic is sound.

Naming a check `CT004` and pointing at a diagram that defines `CT004` positionally makes that
divergence visible at authoring time, when it is cheap.

It creates no new path into a verdict: this changes what the semantic types are called, not what may
become one.

## Unblocks

- **#44** `A3.1` — Adopt `CT0xx` canonical codes with aliases, currently `needs-architecture`

Does **not** unblock `Q6` (#14), `Q15` (#16), or any rule authoring in `A6` — those remain blocked on
client answers, and this ADR is careful not to appear to settle them.
