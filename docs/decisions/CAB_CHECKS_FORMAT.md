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
