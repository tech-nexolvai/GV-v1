# Countertop checks — Raj's structured format (C_Tops_Checks_New.pptx)

Raj sent the countertop deck (`C_Tops_Checks_New.pptx`, received 2026-09-07) — the countertop
equivalent of the cabinet deck, in the same named-variable + diagram + worked-example format. It is the
**master spec for the countertop / sink-cutout family** for the **three-sided layout** ("walls on
either side of the cabinets/countertops"). Format APPROVED (matches the cabinet deck).

**Scope note:** this deck covers the three-sided layout only. Back-only and island layouts may add or
alter tags, so the vocabulary below is authoritative for three-sided and still provisional across all
layouts (CLIENT_FACTS Q20).

---

## The canonical countertop vocabulary (slide-8 variable table — now authoritative)

| Var | Description | Acquisition | Source | Calculation rule |
|---|---|---|---|---|
| `CT001` | Wall-to-wall dimension | Measured | field | `CT001 = CT002+CT003+CT004+CT005+CT006` |
| `CT002` | Left filler width | Calculated | from cabinet calc | — |
| `CT003` | Cab 1 width (left cabinet) | Calculated | from cabinet calc | — |
| `CT004` | Cab 2 width (sink cabinet) | Calculated | from cabinet calc | `CT004 = CAB_SIDE_THK + CT011 + CT012 + CT013 + CAB_SIDE_THK` |
| `CT005` | Cab 3 width (right cabinet) | Calculated | from cabinet calc | — |
| `CT006` | Right filler width | Calculated | from cabinet calc | — |
| `CT007` | Sink **front** offset | Global minimum | company standard (U.N.O) | standard hold dimension |
| `CT008` | Sink **depth** | Specified | client (sink spec) | — |
| `CT009` | Sink **back** offset | Calculated | system | warn if `CT010 − C.T_OH − CT007 − CT008 − B.S_THK` < Global MIN Constant |
| `CT010` | Countertop depth | Calculated | system | `CT010 = C.T_OH + CT007 + CT008 + CT009 + B.S_THK` |
| `CT011` / `CT013` | Sink-hole clearance to sink-cabinet interior faces (left / right) | — | — | — |
| `CT012` | Sink hole **width** | — | — | — |
| `B.S_THK` | **Backsplash thickness** | Specified | client | — |
| `C.T_OH` | Countertop overhang | Specified | client (designer) | — |
| `CAB_SIDE_THK` | Cabinet side-panel thickness | Specified | client | — |

## The formulas (slides 3–8)

- **Width** = wall-to-wall (`CT001`), with an **optional field cut** each side (reviewer input, varies by
  site — "an extra inch or two, cut at the site"; e.g. 90″ + 1″ + 1″ = 92″). Not a constant.
- **Depth** (physical) = **cabinet depth + overhang**, read from the section (slide 7: 24″ + ¾″ = 24¾″).
- **Sink placement** (slide 8, "where most mistakes happen"): the countertop depth decomposes
  front-to-back as `C.T_OH + CT007(front) + CT008(sink) + CT009(back) + B.S_THK(backsplash)`. The **back
  offset is the remainder**; if it falls below the Global MIN Constant, warn.
- **Sink cabinet width** (`CT004`) = side panel + left clearance + cutout width + right clearance + side
  panel = `CAB_SIDE_THK + CT011 + CT012 + CT013 + CAB_SIDE_THK`.

## What it confirms (our open questions)

- **`CT012` = sink hole WIDTH, `CT008` = sink DEPTH** — Q15/Q16/Q17/Q19 settled on the drawing itself.
- **`CT004` is the sink-CABINET OVERALL width** — Q19. The deck's own formula counts both side
  panels (`CAB_SIDE_THK + CT011 + CT012 + CT013 + CAB_SIDE_THK`), so it is the outside-to-outside
  cabinet width, not the interior opening. Read as "interior", a reviewer would enter the opening
  and the check would compare it against a number two panel thicknesses larger.
- **Back offset = derived remainder ≥ a Global MIN Constant** — Q6 structure confirmed, matching our
  authored `ct_back_offset_min` rule.
- **Front offset = global constant, U.N.O** (Q5); **field cut = reviewer input** (Q1);
  **depth = cabinet depth + overhang, cross-checked** (Q13).

## What it ADDS / CHANGES (new rule work — see the codebase prompt)

1. **`B.S_THK` (backsplash thickness) is a NEW variable.** It sits inside the countertop depth and must
   be subtracted in the back-offset remainder. `ct_back_offset_min` used to subtract only front offset
   + sink depth, leaving out overhang (`C.T_OH`) and backsplash (`B.S_THK`) — which overstated the back
   offset by their sum and would pass a sink set too far back. **AUTHORED (#537):** all five terms are
   now in `segments_before_the_back_offset`, and both new parameters are project-scope reviewer inputs
   with no default. Do not re-author the two-term version.
2. **`CAB_SIDE_THK` (cabinet side-panel thickness) is a NEW variable** for the `CT004` sink-cabinet
   width relation. **AUTHORED (#537):** `rules/rulebook/ct_sink_cabinet_width_001.yaml`, with both
   panels written out and no default thickness.
3. The **final CT0xx vocabulary + acquisition types** (Measured / Calculated / Specified / Global) — the
   countertop answer to Q20 for this layout; maps onto our operand sources + parameter layers.
   **RECORDED (#537):** `Acquisition` in `vocabulary/semantic_types.py`, per code — except `CT007`,
   see the open question below.

## Still pending (this deck does NOT close everything)

- **`CT007`: exact value or minimum? — RAJ TO CONFIRM.** The slide-8 variable table gives its
  acquisition as "Global **minimum**"; the deck's prose and Q5 call it a "global **constant** / standard
  hold dimension (U.N.O)". Those are two different verdicts — `>= 4"` passes a sink held six inches
  back, `= 4"` fails it. `ct_sink_offset_front_001` stays **exact** (as authored from Q5) and
  `CT007`'s acquisition is deliberately left unset until Raj answers; nobody should reconcile this
  from the deck alone.
- **The back-offset remainder: warn or fail? — OURS TO DECIDE.** This deck says "warn" when the
  remainder falls below the minimum; the shipped `ct_back_offset_min_001` carries `severity: CRITICAL`
  (pre-dating this deck). Not changed here, because which one is right is a product call about how a
  short back clearance should reach the reviewer, not a reading of the deck.
- **The back-offset Global MIN Constant VALUE** — named, not numbered. Same vendor value Raj still owes
  (Q6 residual).
- **Layout scope** — three-sided only; back-only and island vocabularies still to come (Q20 not fully
  final).
- **The drawings themselves (#274).** This is the *spec*, not the drawings — semantic typing still needs
  real drawings to see how these tags are placed. This clears the **Q20** prerequisite substantially;
  it does not fill `data/drawings/`.

---

**Record impact:** substantially answers Q20 (countertop, three-sided) and confirms the sink-cutout
family (Q6/Q13/Q15–Q19/Q1). Unblocks a pocket of **deterministic rule-authoring** work — buildable now
from this written spec, independent of the drawings (backsplash into the back-offset remainder, the new
parameters, the CT004 relation, the final vocabulary). Does NOT unblock semantic typing, which still
waits on #274.
