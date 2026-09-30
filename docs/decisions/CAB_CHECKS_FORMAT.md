# Cabinet checks — Raj's structured format (cab_Checks_New.pptx)

Raj sent a 6-slide deck (`cab_Checks_New.pptx`, received 2026-09-04) laying out the **cabinet /
wall-to-wall filler distribution** logic as named variables + scenarios + worked examples. This is the
format he proposed for the whole rulebook; countertops will follow in the same shape.

**Verdict: format APPROVED.** This is the structured, named, example-driven input we had been asking
for — it replaces the spreadsheet's `CT0xx`/A–G tangle for the cabinet side. We told Raj yes and sent
back four clarifying questions (below). Countertops in this same format are the real prize, because the
sink cut-out labels that were crossed in the spreadsheet will finally land unambiguously.

---

## Canonical cabinet vocabulary (slide 2 glossary — now authoritative for cabinets)

| Variable | Meaning |
|---|---|
| `W2W_DIM_ARCH` | Wall-to-wall dimension in the architectural drawing |
| `W2W_DIM_SITE` | Wall-to-wall dimension measured at the site |
| `FILLER_WIDTH_MIN` / `FILLER_WIDTH_MAX` | Minimum / maximum allowed filler width |
| `CAB_REGULAR` | Cabinet without equipment (the only kind we may resize) |
| `CAB_EQUIP` | Cabinet with equipment (never resized — the equipment must fit) |
| `SINGLE_DOOR_CAB_WIDTH_MIN` / `_MAX` | Single-door cabinet width bounds |
| `DOUBLE_DOOR_CAB_WIDTH_MIN` / `_MAX` | Double-door cabinet width bounds |
| `DRAWER_CAB_WIDTH_MIN` / `_MAX` | Drawer cabinet width bounds |

These names are clean and map 1:1 onto our descriptive-id approach. They **supersede the spreadsheet's
letter/`CT0xx` naming for cabinets**. (Countertop final tags are still deferred — CLIENT_FACTS Q20.)

## Distribution logic (slides 3 & 5) — two scenarios, two-step precedence

The rule when site ≠ arch, in both directions, adjusts **fillers first, regular cabinets second, and
the equipment cabinet never**:

- **Scenario 1 — `W2W_DIM_SITE < W2W_DIM_ARCH` (site smaller).**
  Step 1: reduce filler widths, honoring `FILLER_WIDTH_MIN`.
  Step 2: if a filler would fall below its minimum, reduce `CAB_REGULAR` widths (never `CAB_EQUIP`),
  honoring the per-type `..._CAB_WIDTH_MIN`. Throw an error if the rules are violated.
- **Scenario 2 — `W2W_DIM_SITE > W2W_DIM_ARCH` (site larger).**
  Step 1: increase filler widths, honoring `FILLER_WIDTH_MAX`.
  Step 2: if a filler would exceed its maximum, increase `CAB_REGULAR` widths, honoring the per-type
  `..._CAB_WIDTH_MAX`. Throw an error if the rules are violated.

This is exactly the calculate-then-flag distribution behind CLIENT_FACTS Q8 (field-smaller), Q9
(only regular cabinets move) and Q21 (calculate, not just check) — now written down in Raj's own terms.

## Worked examples → distribution-logic test cases (gold candidates)

Both examples are synthetic (no drawings), so they are **unit test cases for the distribution
calculator**, not full gold-set package cases (the gold set at `eval/gold_set/` expects real arch/shop
PDFs). Both examples happen to exercise BOTH steps — fillers alone can't absorb the 8", so regular
cabinets move too. Dev to formalize as tests for the distribution module; each layout row is
`filler | CAB_REGULAR | CAB_EQUIP | CAB_REGULAR | filler`.

**Case CAB-DIST-1 (Scenario 1, slide 4):** `W2W_DIM_ARCH = 90"`, `W2W_DIM_SITE = 82"` (−8"),
`FILLER_WIDTH_MIN = 2"`, equip fixed at 36".
- Arch:  `3 | 24 | 36 | 24 | 3`  = 90"
- Expected site: `2 | 21 | 36 | 21 | 2` = 82"
- Derivation: fillers 3→2 (−2" total) → 78" for cabinets → 36" equip fixed → 42" ÷ 2 regular = 21" each.

**Case CAB-DIST-2 (Scenario 2, slide 6):** `W2W_DIM_ARCH = 88"`, `W2W_DIM_SITE = 96"` (+8"),
`FILLER_WIDTH_MAX = 3"`, equip fixed at 36".
- Arch:  `2 | 24 | 36 | 24 | 2`  = 88"
- Expected site: `3 | 27 | 36 | 27 | 3` = 96"
- Derivation: fillers 2→3 (+2" total) → 90" for cabinets → 36" equip fixed → 54" ÷ 2 regular = 27" each.

## Open questions sent back to Raj (2026-09-04) — PENDING CLIENT

1. **Default values for the MIN/MAX variables** (filler + per-type cabinet), or confirm they are
   per-project reviewer inputs. The examples use filler MIN 2" / MAX 3" as *variable values*, not
   committed defaults — this is the same unresolved default behind CLIENT_FACTS Q21's filler-max flag
   (email said 1"/2", the 2026-08-25 call summary said 3–4", these examples 2"/3" — all illustrative).
2. **How a cabinet's type is identified** (single-door / double-door / drawer) and **which cabinet is
   the equipment cabinet** — tagged on the drawing, or reviewer-entered? The rules key off both.
3. **Equipment-cabinet width** — confirm it is fixed from the equipment spec (per-cabinet input), not a
   global variable. The glossary lists single/double/drawer bounds but no equipment-width variable;
   the examples treat 36" as a fixed minimum.
4. **Uneven splits** — when the leftover doesn't divide evenly between the two regular cabinets, the
   rounding rule (nearest 1/8"? 1/4"?) and which cabinet takes the remainder. Matters because the
   verdict is exact-match (V1_VERDICT_MODEL).

---

**Record impact:** reinforces Q8, Q9, Q21 (distribution logic, now in Raj's own vocabulary) and gives
CABINETS a clean named vocabulary; does NOT close Q20 (countertop final tags still deferred until the
layouts and the countertop deck land). The four open questions above are the follow-ups.

---

## Update 2026-09-27 — the second deck (`cab_Checks_Sep_21.pptx`) answers two of the four

The 2026-09-04 record above stands; this is what the 2026-09-21 deck adds. Implemented in #676 as
`cabinet_run_distribution`. The record above is left as written — it is the account of what we knew
on 2026-09-04, and the ambiguity it describes is real history, not an error to tidy away.

**Question 2 is answered: reviewer-entered, by bounding box.** Slide 11 — *"User should be able to
draw a bounding box around a cabinet and categorize that as a particular equipment cabinet and
confirm width should be changed or cannot be changed."* The reviewer classifies; the program
computes from the classification. This also settles the Q9 reading that `filler_distribution` and
this document had taken opposite ways: that operation stopped at `CABINET_SELECTION_REQUIRED`
because *"Q9 assigns cabinet selection to the reviewer"*, while §"Distribution logic" above read Q9
as *"only regular cabinets move"* with the calculation ours. **Both halves are right.** Reviewer
picks what may move; arithmetic decides by how much. Q9 is refined, not contradicted.

Slide 11 also says the equipment list itself is still coming: *"For example, sink cabinet, MW
cabinet, under counter refrigeration cabinet. We will provide the complete list we typically follow
for the final run."* Until it arrives the reviewer's classification is the only source, which is
what `cabinet_type` encodes — no list is hard-coded anywhere (#674).

**Question 3 is answered by conduct.** The equipment cabinet holds 36" in both worked examples and
slide 3 gives the reason — *"the equipment cabinet dimensions should not be reduced otherwise
equipment will not fit."* It is a per-cabinet input, and the operation needs no width variable for
it because it never moves. It binds in both directions: scenario 2 grows the run, and an opening
too wide for the appliance is as wrong as one too narrow.

**Question 1 stays open** (#674) and **question 4 has grown a sibling.** Both worked examples divide
evenly — 6" over two cabinets — so the deck still never states:

- **4a, the original.** How a remainder that does not divide into a drawable width is apportioned.
  4" over three regular cabinets is 22 2/3", which is not a width anybody draws or cuts.
- **4b, new.** How a change is shared between fillers that started *unequal* — a layout slide 12
  names as in scope — *"Also unequal fillers (left vs. right), and a wall on only one side."* 2" and
  4" losing 2" between them could be 1"+3", 2"+2" or 1 1/2"+2 1/2", all inside the bound.

`cabinet_run_distribution` abstains on both rather than choosing, and hands the reviewer the exact
share it computed. Under exact match (V1_VERDICT_MODEL) a picked width is not a rounding preference;
it is the verdict.

**The five outcomes (slide 12), and how they map.** Outcomes 1 and 2 are the two-step correction;
3 is *"mark the cabinets with green checks and change only the fillers"*, recorded as
`cabinets_retained`; 4 is *"should not force a fix… flag 'cannot be resolved, RFI to architect'"*,
which is a first-class result carrying the unabsorbed amount and the bound that blocked it, never
an exception; 5 is *"report a pass"*.

**Record impact:** closes questions 2 and 3; question 1 remains with #674; question 4 splits into 4a
and 4b, both abstained on in code and both listed in `memory.md`.
