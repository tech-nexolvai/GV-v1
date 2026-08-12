# V1 Research & Build Plan

**Date:** 2026-08-12 · **Status:** proposal for review — not yet locked
**Sources read:** `AGENTS.md`, `CLAUDE.md`, `README.md`, `memory.md`,
`docs/RULE_ENGINE_SPEC.md`, `docs/GV_Backend_Architecture_Proposal.pdf` (26pp),
`docs/GV_V1_Agentic_systemDesign.pdf` (17pp), `data/checklists/Countertop_Checks_Updated.xlsx`,
`data/checklists/Cabinet_Checks.xlsx` **including the 16 embedded diagrams**, one real GV shop-drawing
excerpt (embedded in `Cabinet_Checks.xlsx`).

> **Why this document exists.** The architecture docs describe *how* to build safely. The client
> checklists describe *what* to check. This is the first time both have been read together, and they
> do not fully agree. This document records what the client material actually says, where it
> contradicts our own spec, and what we should build in what order.

---

## 0. Executive summary

The architecture is sound and needs no redesign. What changes is the **rule model** and the
**unit policy**, both driven by evidence found in the client material rather than by opinion.

Five things matter most:

1. **A verified unit trap.** Every dimension on the real drawing is written twice — millimetres with
   the inch fraction in brackets, e.g. `984 [38 3/4]`. Each unit system closes *exactly* within
   itself, but they do **not** convert exactly to each other. Measured worst case on the real
   drawing: **1.600 mm**, which is larger than a 1/16″ tolerance (**1.5875 mm**). Converting
   everything to canonical mm — as `AGENTS.md` currently mandates — can therefore consume the entire
   tolerance budget before a single real error is considered. **This needs a decision (D1).**
2. **Dimension chains close exactly.** On the real drawing `2025 + 1968 + 2019 = 6012` and
   `51 + 533 + 457 + 984 = 2025`, exactly, in both unit systems independently. This gives us a
   *self-verifying extraction check* that needs no ground truth and no client data — the highest-value
   early win available to us.
3. **The rule schema cannot express the client's real rules**, and the gap is bigger than
   `RULE_ENGINE_SPEC.md` identified. Beyond aggregates, we need **derived intermediate values**
   (a small typed computation DAG), **pairwise list comparison**, and **severity**.
4. **Two whole entities are missing from the data model**: a third document role
   (`PRODUCT_SPEC` — sink cut sheets) and **project parameters** (values on no drawing at all).
   Without both, most client checks are unimplementable.
5. **Twelve contradictions/errors in the client checklist** must be resolved before rules are
   authored. At least one — the field-cut count — changes CT-1's arithmetic directly, and
   **no tolerance value appears anywhere in the client material**.

**Critical path right now is entirely unblocked**: units layer → rule engine → parameters. All are
pure Python, testable with synthetic data, and are the safety-critical core. Extraction work is
blocked pending real PDFs (`data/drawings/` is empty).

---

## 1. What the client material actually contains

### 1.1 `Countertop_Checks_Updated.xlsx`

**Sheet1** — five checks, all scoped to one context: *"Countertop with walls on left, right and back
side"* (confirmed by an embedded image captioned *"Vanity with a wall on 3 sides"*).

| Label in file | Rule name | Substance |
|---|---|---|
| CT-1 | Countertop Width Verification | width = fillers + cabinets + field cuts |
| CT-2 | Countertop Depth Verification | depth = cabinet depth + overhang; cabinet depth = carcass + door (3/4″ typical) |
| CT-3 | Sink cutout depth (D) | D = sink interior depth (E) − 0.25″ − 0.25″ |
| CT-3 | Sink cutout width (A) | A = sink interior *[width]* − 0.25″ − 0.25″ |
| CT-3 | Sink front/back offset | front ≥ 4″; back ≈ 2.375″, coordinated with faucet hole |

Note three distinct checks share the ID "CT-3", and the CT-2 header says "Width" while its rule name
says "Depth". These are labelling errors in the source (see §4).

**Sheet `Countertop_Checks`** — a clean variable table, which is the better foundation:

| ID | Name | Acquisition | Source |
|---|---|---|---|
| CT001 | Wall to Wall Dimension | Measured | Field Installation |
| CT002 | Left Filler Width | Calculated | System Calc |
| CT003 | Cab 1 Width | Calculated | System Calc |
| CT004 | Cab 2 Width (sink cabinet) | Calculated | System Calc |
| CT005 | Cab 3 Width | Calculated | System Calc |
| CT006 | Right Filler Width | Calculated | System Calc |
| CT007 | Sink Front Offset | Global Minimum | Company standard |
| CT008 | Sink Depth | Specified | G.C / Client |
| CT009 | Sink Back Offset | Calculated | System Calc |
| CT010 | Countertop Depth | Calculated | System Calc |
| CT011 / CT012 / CT013 | (see diagram) | — | — |
| `B.S_THK` | Backsplash Thickness | Specified | G.C / Client |
| `C.T_OH` | Countertop Overhang | Specified | G.C / Client |
| `CAB_SIDE_THK` | Cabinet Side Panel Thickness | Specified | G.C / Client |

Stated formulas:

```
CT001 = CT002 + CT003 + CT004 + CT005 + CT006
CT004 = CAB_SIDE_THK + CT011 + CT012 + CT013 + CAB_SIDE_THK
CT010 = C.T_OH + CT007 + CT008 + CT009 + B.S_THK
CT009 constraint: if (CT010 − C.T_OH − CT007 − CT008 − B.S_THK) < global minimum → WARNING
```

**CT011/CT012/CT013 are undefined in text but fully defined in the diagram** (`CT_image10`, the
annotated plan view): CT012 is the sink hole width, CT011 and CT013 are the clearances from the sink
cabinet's interior faces to the sink cutout. That diagram is the authoritative key to the entire
`CT0xx` vocabulary and should be treated as part of the spec.

### 1.2 `Cabinet_Checks.xlsx`

Two things, both important.

**(a) An arch-vs-shop comparison list** — `A_Wall_2_Wall_Dim`, `A_CAB_1…A_CAB_7`, `A_Filler_Left`,
`A_Filler_Right` against `S_*` equivalents. This is a **pairwise list comparison**, an operation shape
our registry does not have.

**(b) The filler-distribution rule (Condition 1):**

```
if   F_Wall_2_Wall_Dim > A_Wall_2_Wall_Dim
then Wall2Wall_Dim_Diff = F_Wall_2_Wall_Dim − A_Wall_2_Wall_Dim
                        = Filler_Width_Right + Filler_Width_Left
Note: unless otherwise noted, Filler_Width_Right = Filler_Width_Left
      if Wall2Wall_Dim_Diff > Filler_Width_Max × 2, the extra width is distributed along the cabinets
      1. Some cabinets cannot be adjusted — sink cabinets, cabinets with equipment
```

Variables: `F_Wall_2_Wall_Dim` (User Input), `Filler_Width_Left/Right` (Calculated),
`Filler_Width_Min/Max` (User / Global / Project Input).

### 1.3 The real shop drawing (embedded in `Cabinet_Checks.xlsx`)

A `B FRONT VIEW 1:20` elevation of a seven-bay cabinet run. This is the single most informative
artifact we have, and it tells us more about extraction than either PDF does:

- **Dual dimensions throughout**: `6012 [236 3/4]`, `984 [38 3/4]`, `533 [21]`, `51 [2]`, `864 [34]`.
- **Nested dimension chains**: an overall dimension, a mid-level grouping, and per-bay dimensions.
- **Authoritative facts in free text**, not on dimension lines:
  *"5″ FILLER PANEL TO BE FIELD CUT AS PER SITE CONDITION"* at both ends. The filler width and the
  field-cut instruction are **notes**, not dimensions.
- **Material codes**: `PL-02` on doors, `WD-03` on the toe kick — exactly the coded-identifier class
  the retrieval design worries about (`PL-02` vs `PL-03`).
- **View/elevation tags**: circled `D`, `G`, `E`, `G`, `F` — the linkage for `scope: same_assembly`.
- **`END PANEL`** callouts at both ends, distinct from fillers.

A second diagram (`CAB_image2`) defines a check that appears in **no** text checklist:
`Cabinet Height = Toe-Kick Height + Drawer 1 + Drawer 2 + Drawer 3 Height`.

### 1.4 The sink specification (`CT_image8`)

Sink dimensions are taken from the **manufacturer's cut sheet** — the example given is Kohler
K-2330-G — with its own dual dimensions (`15-5/8" (397 mm)`, `19-3/4" (502 mm)`) and a
*"Recommended ADA Installation"* block (34″/864 mm max height, 27″/686 mm knee clearance,
8″/203 mm, 9″/229 mm min, 13″/330 mm max).

Note the real drawing's cabinet height is `864 [34]` — exactly the ADA maximum.

---

## 2. Findings

### F1 — The dual-unit rounding trap (verified, highest technical risk)

`AGENTS.md` §6 mandates canonical mm and conversion of all values. Measured against the real drawing:

| mm on drawing | inch on drawing | inch → mm | delta |
|---:|---:|---:|---:|
| 6012 | 236 3/4 | 6013.450 | **1.450** |
| 2025 | 79 3/4 | 2025.650 | 0.650 |
| 1968 | 77 1/2 | 1968.500 | 0.500 |
| 984 | 38 3/4 | 984.250 | 0.250 |
| 864 | 34 | 863.600 | 0.400 |
| 100 | 4 | 101.600 | **1.600** |

**Worst delta 1.600 mm > 1/16″ tolerance (1.5875 mm).**

Neither number is wrong; each is a correct rounding of the same design intent. But mixing them inside
one arithmetic check injects up to 1.6 mm of pure noise. A check with a 1/16″ tolerance could
therefore FAIL on a perfect drawing, or PASS on a drawing that is 1/16″ out — the second being a
false-PASS, the primary safety metric.

**Recommendation (decision D1):**
- Keep canonical mm for storage, cross-document comparison and display.
- Additionally persist the **exact original token** as a `Fraction` plus its original unit on every
  observation. Never discard what the drawing actually said.
- Give each rule an `arithmetic_unit`. All operands entering one arithmetic operation must share the
  authored unit system; if they don't, either apply an explicitly declared rounding allowance or
  return REVIEW REQUIRED. Never silently mix.
- Treat the bracketed alternate unit as a **corroboration lane** with a rounding-aware band, not as
  an independent operand.

This modifies a golden rule and needs explicit sign-off before implementation.

### F2 — Dual dimensions are a cheap corroboration lane

Every dimension is stated twice by the drawing's author. That is an independent statement of the same
fact available from pure vector extraction, before any OCR. It can promote `RAW_CANDIDATE` →
`CORROBORATED` at near-zero cost and materially reduce how often we need PaddleOCR + docTR agreement.

Limitation, and it matters: it corroborates the **reading of the number**, not the **semantic
association** of that number to an item. Semantic association still needs geometry or human
confirmation.

### F3 — Dimension-chain closure is a self-verifying extraction check

Verified exactly on the real drawing, in both unit systems independently:

```
2025 + 1968 + 2019 = 6012          79 3/4 + 77 1/2 + 79 1/2 = 236 3/4
  51 + 533 + 457 + 984 = 2025            2 + 21 + 18 + 38 3/4 = 79 3/4
```

If a chain fails to close, we have mis-read or mis-associated something — detectable **without any
ground truth, any client answer key, or any rule**. This should be a first-class extraction validator
and is the strongest candidate for the Phase 1 "one deterministic check", because it can be built and
proven today.

### F4 — A third document role is required

`document_role` is currently `ARCH | SHOP`. Sink interior dimensions come from the manufacturer's cut
sheet — a third document. Without a `PRODUCT_SPEC` role the entire sink-cutout check family (three of
the five countertop checks) has no authoritative source and can only return NOT FOUND.

Implies: a per-project product/spec registry, and cut sheets ingested as versioned, hashed documents
like any other source.

### F5 — Project parameters are a missing first-class entity

Values required by the rules that appear on **no drawing**:

| Parameter | Client-stated source |
|---|---|
| Door thickness (3/4″ typical) | typical/standard |
| Countertop overhang (`C.T_OH`) | G.C / Client — "designer's choice" |
| Backsplash thickness (`B.S_THK`) | G.C / Client |
| Cabinet side panel thickness (`CAB_SIDE_THK`) | G.C / Client |
| Field cut size (1″ typical, customisable per project) | project-by-project |
| `Filler_Width_Min` / `Filler_Width_Max` | User / Global / Project input |
| Sink front offset minimum (4″) | Company standard |
| Sink depth (`CT008`) | G.C / Client |
| Field wall-to-wall (`F_Wall_2_Wall_Dim`) | Measured on site |

The backend data model (§10.1 of the proposal) has **no aggregate for these**. We need versioned
`project_parameter_sets` with layered resolution (global standard → project override → run override),
full provenance, and a hard rule: **a missing parameter is NOT FOUND, never a silent default**. This
is also a UI requirement — a reviewer must be able to enter and see them.

### F6 — Rules need derived intermediate values, not a single operation

Client formulas chain. `CT010` is built from five operands, one of which (`CT009`) is itself obtained
by rearranging that same formula and then compared against a minimum. `CT004` is built from five
sub-dimensions. The current schema has a single `operation:` block and cannot express this.

**Recommendation:** add a `derivations:` block — named intermediates, each computed by a *typed*
operation from inputs, parameters or earlier derivations, forming a DAG validated as acyclic at
publish time. Still typed, still no `eval`, still exact. Without this, most real client checks are
inexpressible. This is a larger gap than the aggregate operations `RULE_ENGINE_SPEC.md` already flagged.

### F7 — Severity is missing, and the primary metric depends on it

The checklist explicitly asks for a **warning** (the `CT009` back-offset constraint: *"program should
throw warning"*). Our outcome model has only PASS / FAIL / NOT FOUND / REVIEW REQUIRED.

More seriously: **"critical false-PASS rate" is the primary release metric, and nothing in the schema
defines which checks are critical.** The metric is currently unmeasurable as specified.

**Recommendation:** add `severity: CRITICAL | MAJOR | MINOR | ADVISORY` to the rule schema; define the
primary metric as false-PASS on CRITICAL rules; render ADVISORY outcomes as warnings, not failures.

### F8 — A new operation class: pairwise list comparison

`A_CAB_1…7` vs `S_CAB_1…7` is a list-to-list comparison keyed by identifier. Existing and proposed
operations are scalar or whole-list aggregates; none do this. Need
`pairwise_within_tolerance(list_a, list_b, key, tolerance)` with **explicit count-mismatch handling** —
a differing cabinet count is itself a finding, and must never silently compare the shorter list.

### F9 — Two rules exist only in diagrams

- **Cabinet height decomposition** (`CAB_image2`): `height = toe_kick + drawer_1 + drawer_2 + drawer_3`.
  A vertical sum check, absent from every text checklist.
- **ADA compliance** (`CT_image8`): the cut sheet carries a recommended-ADA block, and the real
  drawing's 864 mm height is exactly the 34″ ADA maximum. Possibly a global rule family — needs a
  scope decision.

### F10 — Extraction targets are richer than "dimensions on dimension lines"

`memory.md` records Raj as confirming "dimensions live on dimension lines". The real drawing shows
that is true but incomplete. Rule-relevant facts also live in **free-text notes** (the 5″ filler panel
and its field-cut instruction), **material codes**, **view tags** and the **title block**. Extraction
scope must include annotation text, not dimensions alone.

### F11 — Scope tension: "validate, never design"

The client's variable table marks most values *"Calculated / System Calc"* and asks the system to
distribute surplus width across adjustable cabinets. That is closer to design assistance than to
validation, and `README.md` states the product "validates drawings; it never designs them".

**Recommendation:** the engine may compute derived *expectations* and display them in a finding as
"derived expected value" with its full calculation trace — that is arithmetic, and it is how a
reviewer understands a FAIL. But V1 must not emit computed dimensions to a vendor as instructions
without reviewer sign-off. Worth stating explicitly rather than leaving to interpretation.

### F12 — Rule coverage is one wall configuration only

Every client countertop check is scoped to walls on three sides. The applicability resolver must
return an explicit **"no applicable rule"** outcome for island / back-only / two-wall layouts, surfaced
as REVIEW REQUIRED. An unchecked package must never render as clean — silence is the most dangerous
possible false-PASS.

---

## 3. Reconciliation: client material vs `RULE_ENGINE_SPEC.md`

| Topic | `RULE_ENGINE_SPEC.md` | Client material | Verdict |
|---|---|---|---|
| CT-1 formula | width = Σcab + Σfiller + field_cut × n | `CT001 = CT002+CT003+CT004+CT005+CT006`, plus field cuts | **Agrees** |
| Tolerance by `wall_config` | 1/8″ and 1/16″ per variant | **No tolerance appears anywhere** | **Unsourced — our assumption** |
| `field_cut_count` | 2 for three-wall, 1, 0 | Ambiguous (see §4 Q1) | **Unresolved — changes arithmetic** |
| Wall configs | four variants | one only (three walls) | Spec is ahead of the client |
| Sink checks | not covered | three checks | **Spec gap** |
| Depth checks | not covered | two formulas | **Spec gap** |
| Derived values | not supported | required throughout | **Spec gap (F6)** |
| Pairwise lists | not supported | required (cabinets) | **Spec gap (F8)** |
| Severity | not supported | warning requested | **Spec gap (F7)** |
| Naming | `countertop_overall_width` etc. | `CT001…CT013`, `A_/S_/F_` prefixes | **Adopt client codes as canonical** |

**Recommendation on naming:** adopt the client's `CT0xx` codes as the canonical vocabulary — they are
unambiguous, diagram-linked and already the client's own language — with our descriptive names as
aliases. This resolves the open question in `memory.md` about confirming names, and inverts the
current provisional approach in `rules/semantic_types.py`.

---

## 4. Contradictions and errors in the client material

These must be resolved before rules are authored. Numbered for use as client questions.

**Blocking — cannot author CT-1 correctly without an answer:**

- **Q1 — Field cut count.** The sheet says: *"If the wall to wall dimension is 84″, total width =
  1″ (field cut) + Wall to Wall dimension : 84″ + 1″ (field cut)"*. This reads as either **85″** (one
  field cut) or **86″** (two). Our spec assumed two for a three-wall layout. This changes CT-1's
  arithmetic directly. Also: the real drawing calls out a **5″ filler panel to be field cut**, which
  suggests the field cut is taken out of an oversized filler rather than added to the countertop —
  a third possible reading.
- **Q2 — Tolerances.** No tolerance value appears anywhere in the client material. The 1/8″ and 1/16″
  in our spec are assumptions. Needed per check and per wall configuration.
- **Q3 — Does "cabinet width" include the end panel?** In `CAB_image2` the "Cab 1 Width" bracket
  appears to span both the `51 [2]` end segment and the `533 [21]` cabinet. Materially changes the
  CT-1 sum.
- **Q4 — Severity.** Which checks are critical (manufacturing risk) vs advisory?
- **Q5 — Front offset: minimum or equality?** The sheet says "4″ minimum" (X19) and "Front offset = 4″"
  as pass criteria (X27); `CT007` acquisition says "Global Minimum". These are different checks.
- **Q6 — `CT009` / `CT010`: which is observed and which is derived?** Both are marked "Calculated",
  and each is defined in terms of the other.

**Needed before Phase 3, not blocking today:**

- **Q7 — Sink cut sheets.** Will GV supply manufacturer spec sheets per project? Confirms the
  `PRODUCT_SPEC` document role (F4).
- **Q8 — The other branch.** Condition 1 only defines `F_Wall_2_Wall > A_Wall_2_Wall`. What happens
  when the field dimension is *smaller* than design? This is common and consequential.
- **Q9 — Non-adjustable cabinets.** How is a sink or equipment cabinet identified *in the drawing*
  so the distribution logic can exclude it?
- **Q10 — "U.N.O." handling.** "Unless Noted Otherwise" appears twice. A drawing note can override a
  global constant. Proposed V1 policy: detect the override and return REVIEW REQUIRED rather than
  auto-applying it.
- **Q11 — ADA.** In V1 scope or not? (F9)
- **Q12 — Dual units.** When mm and the bracketed inches disagree beyond rounding, which governs?
- **Q13 — Two depth formulas.** `cabinet_depth + overhang` vs the five-term `CT010`. Both? A
  cross-check between them?
- **Q14 — Wall configurations.** Are layouts other than three-wall in V1 scope?

**Source errors to confirm as typos (we should not silently "fix" a client spec):**

- **Q15** — `S19`: *"Sink cutout width (A) = sink interior **depth** (E) − 0.25 − 0.25"* — should be
  interior *width*.
- **Q16** — `S29` fail criteria: *"Width of countertop <> Sink cutout **depth** (A) + F + G"* — should
  be *width*.
- **Q17** — `I3` header reads "CT-2 : Countertop **Width** Verification" while its rule name is
  "Countertop **Depth** Verification".
- **Q18** — Three different checks are all labelled **CT-3**. Unique IDs needed.
- **Q19** — `S27`: *"Width of countertop = Sink cutout width (A) + F + G"* is dimensionally wrong.
  From the diagram this describes the **sink cabinet** segment (`CT004`), not the whole countertop.
- **Q20** — Two parallel naming schemes coexist (letters A–G on Sheet1, `CT0xx` codes on the variable
  sheet). Proposal: `CT0xx` canonical, letters as aliases.

---

## 5. Decisions requested

| ID | Decision | Recommendation |
|---|---|---|
| **D1** | Unit policy — canonical mm vs authored-unit arithmetic | Adopt F1: canonical mm for storage, exact original token preserved, rule-level `arithmetic_unit`, no silent mixing. Amends `AGENTS.md` §6. |
| **D2** | Add `derivations:` DAG to the rule schema | Adopt (F6). Without it most client rules are inexpressible. |
| **D3** | Add `severity` to the rule schema | Adopt (F7). The primary metric is unmeasurable without it. |
| **D4** | Add `PRODUCT_SPEC` document role | Adopt (F4). |
| **D5** | Add `project_parameter_sets` aggregate | Adopt (F5). |
| **D6** | Canonical vocabulary = client `CT0xx` codes | Adopt (§3). Resolves an open `memory.md` question. |
| **D7** | Explicit "no applicable rule" outcome | Adopt (F12). Silence must never read as clean. |
| **D8** | Derived expectations shown, never issued as instructions | Adopt (F11). |

---

## 6. Build plan

**Sequencing principle (unchanged from `AGENTS.md` §8):** risk first. What changes is that we now know
the riskiest unknowns concretely, and we know which are blocked.

### Track A — unblocked, critical path, start now

Pure Python, synthetic tests, no client dependency, and it is the safety-critical core.

| Epic | Substance | Exit gate |
|---|---|---|
| **A1 Foundations** | Pin deps, ruff/black/mypy/pytest, CI, pre-commit, ADR log, verdict import-guard test | CI green; a test fails if `verdict/` imports extraction/retrieval/network |
| **A2 Units & tokens** | `Fraction`/`Decimal`, exact inch↔mm, dual-token parser for `984 [38 3/4]`, rounding-consistency policy (D1) | Exhaustive tests incl. the F1 table; no lossy path exists |
| **A3 Semantic vocabulary** | `CT0xx` canonical + aliases + descriptive names, single module (D6) | Every rule and observation references constants, never strings |
| **A4 Typed operation registry** | Existing scalar ops + `sum`, `count`, `count_equals`, `sum_within_tolerance`, `all_within_tolerance`, `alignment`, `pairwise_within_tolerance` (F8) | Exhaustive per-operation tests incl. arity/type/unit failures |
| **A5 Rule schema & snapshots** | Pydantic + JSON Schema, `derivations:` DAG (D2), `severity` (D3), applicability variants, YAML→immutable hashed JSON | Acyclic-DAG validation on publish; malformed rules rejected loudly |
| **A6 Rule authoring** | CT-1, CT-2, cabinet filler distribution, pairwise arch-vs-shop, cabinet height | Each authored rule executes against synthetic operands with a full calculation trace |
| **A7 Project parameters** | Versioned parameter sets, layered resolution, `USER_INPUT` operand source (D5) | Missing parameter → NOT FOUND, never a default |
| **A8 Eval harness** | Gold-case schema, metric implementations, release-gate runner | Runs on synthetic cases; slots in real cases unchanged |

> **Note:** A6 can be *structurally* complete while tolerances remain placeholders pending **Q2**.
> Placeholders must be explicit and must fail loudly rather than defaulting — an unset tolerance is
> not zero.

### Track B — blocked on client data

`data/drawings/` is empty; `eval/gold_set/manifest.yaml` is `cases: []`.

| Epic | Substance | Blocked on |
|---|---|---|
| **B1 Gold set** | Annotate real packages: values, units, IDs, polygons, matches, expected findings | Raj: real project + 5–10 reviewed cases |
| **B2 Extraction core** | pikepdf repair → pypdfium2 render → pdfplumber vector; dimension-token parser; note/material/tag extraction (F10) | Real PDFs |
| **B3 Chain-closure validator** | The self-verifying check from F3 | Real PDFs (design can start now) |
| **B4 Canonical evidence + gate** | Candidate → canonical → sealed operand; dual-unit corroboration lane (F2); evidence states | B2 |
| **B5 Matching** | Exact ID → aliases → geometry, advisory lanes after | B2, B4 |

### Track C — deferred until A and B are proven

**C1** bounded LangGraph agent · **C2** Hatchet durable platform, outbox, observability ·
**C3** reviewer UI, redline PDF, correction ledger, approvals · **C4** deployment.

Unchanged from the architecture: do not build the durable platform before extraction accuracy is
proven on real GV drawings.

### Recommended first move

**A1 → A2 → A4** in that order. A2 is where the verified F1 risk lives, A4 is the crown jewel, and
both are fully testable today with zero client dependency. A3 is small and can land alongside.

---

## 7. Risks

| Risk | Impact | Control |
|---|---|---|
| Unit rounding consumes the tolerance budget (F1, verified) | False PASS — the primary safety metric | D1; no silent unit mixing; dedicated test suite from the measured table |
| Tolerances are assumed, not client-confirmed (Q2) | Every verdict is unsound | Placeholders must fail loudly; block Phase 3 sign-off on Q2 |
| Field-cut count ambiguity (Q1) | CT-1 arithmetic wrong | Blocking question; do not guess |
| Client checklist errors treated as spec (Q15–Q20) | Wrong rules authored confidently | Confirm as typos with Raj; never silently correct a client spec |
| Rules cover one wall config only (F12) | Unchecked packages look clean | D7 explicit "no applicable rule" |
| Gold set never arrives | Nothing is measurable; no release gate can pass | Track A proceeds regardless; escalate early — this is the long pole |
| Scope drift toward design (F11) | Contractual and liability exposure | D8; reviewer sign-off on any emitted value |

---

## 8. What is genuinely blocked vs what is not

**Not blocked (weeks of work available now):** the entire units layer, operation registry, rule
schema, derivations engine, applicability resolver, parameter model, eval harness, and rule authoring
with placeholder tolerances. This is the safety-critical core and the hardest part of the product.

**Blocked on Raj:** tolerance values (Q2), the field-cut question (Q1), the gold set, and all
extraction work.

**The long pole is the gold set.** Without it no metric can be computed and no release gate can pass,
regardless of how much code exists. That should be escalated now rather than at Phase 3.
