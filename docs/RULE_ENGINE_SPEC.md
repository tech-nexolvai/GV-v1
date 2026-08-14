# Rule Engine Spec — Extended Operations & Schema (Addendum to AGENTIC_ARCHITECTURE.md §10)

**Date:** 2026-07-30 · **Purpose:** make the *real* first rule (CT-1: countertop width = cabinets +
fillers + field cut, tolerance by wall layout) expressible with typed operations — no `eval`, exact
arithmetic, deterministic. Fixes the three gaps found in the V1 architecture rule model:
(1) no aggregate/sum operation, (2) no variable-length inputs, (3) no per-layout tolerance, (4) no
literal-expected for global rules.

Principles unchanged: typed operation registry (no arbitrary code), `Fraction`→`Decimal`, canonical
unit = mm, every operand must pass the Evidence Gate (`CORROBORATED` or `HUMAN_CONFIRMED`).

---

## 1. Three check types (declared per rule)
```
check_type: internal      # both operands from SHOP (e.g. countertop vs its cabinets)
check_type: arch_vs_shop  # expected from ARCH, actual from SHOP
check_type: global        # actual from SHOP vs a fixed literal standard (no second document)
```

## 2. Extended typed operation registry
Each operation declares operand **arity** (scalar vs list) and types. The engine validates arity
before executing; a type/arity mismatch is a rule-authoring error, never a silent pass.

| Operation | Operands | Returns | Semantics |
|---|---|---|---|
| `exists` | scalar | bool | operand resolved to a value |
| `equals` | scalar, scalar | bool | exact equality (codes, materials) |
| `within_tolerance` | actual:scalar, expected:scalar, tol | verdict + delta | `|actual−expected| ≤ tol` |
| `difference_between` | a:scalar, b:scalar | delta | reports `a−b` (advisory/derived) |
| `minimum` / `maximum` | x:scalar, bound | bool | `x ≥ / ≤ bound` |
| `between` | x:scalar, lo, hi | bool | `lo ≤ x ≤ hi` |
| `one_of` | x:scalar, set:list | bool | membership (e.g. approved materials) |
| `contains` | text:str, substr:str | bool | substring / code presence |
| **`sum`** | list | scalar | Σ of a list (internal helper) |
| **`count`** | list | scalar | length of a list |
| **`count_equals`** | list, n:scalar | bool | `len(list) == n` |
| **`sum_within_tolerance`** | target:scalar, addends:list, tol | verdict + delta | `|target − Σaddends| ≤ tol` ← **CT-1** |
| **`all_within_tolerance`** | list, expected:scalar, tol | verdict | every element within tol (e.g. equal drawer fronts) |
| **`alignment`** | list of positions, tol | verdict | positions colinear/aligned within tol |

`sum_within_tolerance`, `sum`, `count*`, `all_within_tolerance`, `alignment` are the new aggregate
operations. All are pure, deterministic, unit-normalized.

## 3. Schema extensions

### 3a. Inputs — a *selector* resolving to one or many observations
```yaml
inputs:
  <name>:
    source: SHOP | ARCH | PRODUCT_SPEC
    semantic_type: <canonical semantic type>   # e.g. cabinet_width
    scope: same_assembly | same_view | package  # how far to look (uses canonical item/view links)
    cardinality: one | many
```
- `source: PRODUCT_SPEC` reads from a manufacturer's specification document ingested as a
  versioned, hashed artifact — the sink-cutout family's expected values come from here
  (ADR-0015). A value a person supplied is `USER_INPUT`, however authoritative they are: the
  distinction is whether the number can be re-read against stored bytes later, not where it
  originally came from.
- `cardinality: one` → must resolve to exactly one observation. 0 → `on_missing`; >1 ambiguous → `on_ambiguous`.
- `cardinality: many` → resolves to a list (0..n). Empty when the rule requires ≥1 → `on_missing`.
- `scope: same_assembly` uses the canonical model's item/view relationships (the elevation tag / vendor
  unique-ID grouping) to find the cabinets/fillers that belong to *this* countertop.

### 3b. Parameters — project-tunable values (layered: default → project override)
```yaml
parameters:
  <name>:
    default: { value: <number|"fraction">, unit: in | mm }
    scope: project | run
```

### 3c. Applicability variants — the discriminator that sets tolerance (and counts)
```yaml
applicability:
  discriminator: <field established from the drawing or by reviewer, e.g. wall_config>
  variants:
    - when: <value>
      tolerance: { value: <number|"fraction">, unit: in | mm }
      <extra per-variant params, e.g. field_cut_count: <int>>
```
If the discriminator can't be established → `REVIEW_REQUIRED` (we don't guess the layout).

### 3d. Operation with addends (for aggregates)
```yaml
operation:
  type: sum_within_tolerance
  target: <input name>          # the observed total
  addends:                      # each resolves to a scalar contribution (mm)
    - sum_of: <list input>      # sum a many-cardinality input
    - value: <scalar input>     # a single input
    - repeat: <parameter>       # a parameter counted N times
      times: applicability.<field>
    - literal: { value: .., unit: .. }
  tolerance: applicability.tolerance | { value: .., unit: .. }
```

### 3e. Literal expected — for global rules (no second document)
```yaml
operation:
  type: within_tolerance
  actual:   { source: SHOP, semantic_type: sink_offset_front }
  expected: { literal: { value: 2, unit: in } }   # a fixed standard, not a drawing
  tolerance: { value: 3, unit: mm }
```

## 4. CT-1 fully expressed (the proof it works)
```yaml
id: CT-WIDTH-001
version: 1.0.0
product_type: countertop
check_type: internal
name: Countertop Width Verification
description: >
  Overall countertop width must equal the cabinets + fillers beneath it, plus field cuts.
  Tolerance and field-cut count depend on the wall configuration.

applicability:
  discriminator: wall_config           # back_left_right | back_left | back_only | island
  variants:
    - when: back_left_right             # walls on 3 sides (Raj's starting case)
      tolerance: { value: "1/8", unit: in }
      field_cut_count: 2
    - when: back_left
      tolerance: { value: "1/16", unit: in }
      field_cut_count: 1
    - when: back_only
      tolerance: { value: "1/16", unit: in }
      field_cut_count: 1
    - when: island
      tolerance: { value: "1/8", unit: in }
      field_cut_count: 0

inputs:
  countertop_width:
    source: SHOP
    semantic_type: countertop_overall_width
    scope: same_assembly
    cardinality: one
  cabinet_widths:
    source: SHOP
    semantic_type: cabinet_width
    scope: same_assembly
    cardinality: many
  fillers:
    source: SHOP
    semantic_type: filler_width
    scope: same_assembly
    cardinality: many

parameters:
  field_cut:
    default: { value: "1", unit: in }
    scope: project

operation:
  type: sum_within_tolerance
  target: countertop_width
  addends:
    - sum_of: cabinet_widths
    - sum_of: fillers
    - repeat: field_cut
      times: applicability.field_cut_count
  tolerance: applicability.tolerance

on_missing: NOT_FOUND
on_ambiguous: REVIEW_REQUIRED
```

## 5. Engine execution semantics (deterministic)
For `sum_within_tolerance`:
1. **Establish the applicability variant** (wall_config). Unknown → `REVIEW_REQUIRED`.
2. **Resolve every input** via the canonical model + evidence gate. Any required input missing →
   `NOT_FOUND`; any operand not `CORROBORATED`/`HUMAN_CONFIRMED`, or a `one` selector that's ambiguous
   → `REVIEW_REQUIRED`. (Never sum uncorroborated numbers.)
3. **Normalize all values to mm** — parse imperial as `Fraction` (`"1/8"` → 3.175 mm), keep as `Decimal`.
4. **Compute** `Σaddends` = sum(cabinet_widths) + sum(fillers) + field_cut × field_cut_count.
5. **Verdict** = `PASS` if `|target − Σaddends| ≤ tolerance` else `FAIL`, reporting the delta,
   the tolerance, the variant, and the evidence for every operand.
6. **No model, no retrieval, no memory** touches any step. Pure typed arithmetic.

## 6. What the build must add (small, scoped)
- Operation registry: implement `sum`, `count`, `count_equals`, `sum_within_tolerance`,
  `all_within_tolerance`, `alignment` (the scalar ops already exist).
- Selector resolver: `cardinality` (one/many) + `scope` (same_assembly/same_view/package) against the
  canonical item/view graph.
- Applicability resolver: pick the variant from the discriminator; expose `applicability.<field>` to the
  operation (tolerance, field_cut_count).
- Literal-expected support in scalar operations (global rules).
- Unit layer: `Fraction`("1/8") → mm `Decimal`; tolerance accepts in or mm.
- Pydantic models + JSON Schema for all of the above; every rule validated on publish; every finding
  stores the resolved variant + operand evidence + rule snapshot ID.

## 7. New semantic types this introduces (confirm names with Raj)
`countertop_overall_width`, `cabinet_width`, `filler_width`, `field_cut` (parameter), `wall_config`
(discriminator). The `wall_config` value is established from the plan (walls present on which sides) or
set by the reviewer; if neither, the check abstains (`REVIEW_REQUIRED`).

---
*Net: CT-1 is now expressible with typed, deterministic, no-eval operations, and the same schema covers
internal / arch-vs-shop / global checks and future aggregates. This unblocks Phase 1 of the architecture's
risk-ordered build.*
