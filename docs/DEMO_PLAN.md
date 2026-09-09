# DEMO_PLAN — human-operated V1 polish backlog (Graniti + Nexolv)

**What this is.** The prioritized polish backlog for making the HUMAN-OPERATED V1 solid and demo-ready.
Authored 2026-09-09 — recreated, because the earlier plan this repo's docs referenced was never actually
committed here. This is the plan the polish work draws from; new polish scope gets added *here first*,
not invented ad hoc.

**Context.** V1 ships human-operated today: the reviewer uploads the two PDFs, confirms/types the
dimensions (human-confirm bridge), runs the checks, reviews findings, signs off, downloads the workbook.
The AI reading half is at the wall (Raj's reviewed drawings, #274) and is OUT of this plan. This plan is
only about making the thing that already works credible and robust for the Graniti demo.

## Pre-implementation baseline (2026-09-09 inventory)

- **Reviewer loop:** genuinely wired — API-backed findings, reviewer actions, approval, signed-off
  workbook download. ✅
- **Evidence:** crops are mechanically generated and stored, but the UI
  (`frontend/main/src/components/chat/EvidencePanel.tsx`) shows only coordinates + extracted text, not
  the stored crop image.
- **Workbook:** structurally sound and exact-value-safe, but a plain two-sheet export — no demo-facing
  presentation layer.
- **Visual system:** warm / editorial (Inter / Josefin / JetBrains, coloured actions) — NOT the required
  black/white, ChatGPT-style, monospace system.

## The prioritized cut (build vertically, top to bottom)

### 1. Real evidence crop viewer — highest demo value
Render the ALREADY-STORED crop image for each finding in `EvidencePanel`, so a finding is visibly grounded
in the actual region of the drawing it came from. The crops already exist (`evidence/crop.py` →
`evidence_artifacts`); this is a *display* gap, not new extraction.
- **Boundary:** this shows a MECHANICAL crop of a real candidate (the pixels the reading came from). It is
  NOT the redline — no boxes or annotations asserting *meaning* or placement on the full drawing; redline
  stays blocked on semantic typing.
- Crops render at runtime from the evidence store; no client crop is committed. Tests use the committed
  non-drawing PDF / synthetic fixtures.

### 2. Branded, reviewer-ready workbook
The workbook is the audience's handoff artifact. Add a professional Graniti + Nexolv cover / summary sheet,
and make the findings sheet readable (clear columns, outcomes, the reviewer's sign-off). Keep it
exact-value-safe — never reconstruct a value from display text (ADR-0001 / #529).

### 3. End-to-end reviewer-state polish + the visual system
- Clear progress across upload → confirm/type → run → review → sign-off → download; graceful failure
  states everywhere (no dead ends, no uncaught crashes — a demo dies on a crash).
- Align the visual system to the demo requirement: black/white, ChatGPT-style, monospace/typewriter,
  Graniti + Nexolv branding. Consistent, not per-screen improvisation.

## Demo UI requirements (the visual system)
Black/white, ChatGPT-style, monospace/typewriter type, Graniti + Nexolv branding — applied consistently
across every screen.

## Hard stops (invariants — do not cross)
- Polishes the HUMAN-operated loop ONLY. No auto-typing, no fake reading; the semantic-type guard stays
  green (`model_invocations = 0`, nothing types a candidate on its own).
- The evidence viewer shows REAL mechanical crops of real candidates; it does NOT fabricate meaning or
  placement, and is NOT the redline.
- No wiring the model or the agent into anything.
- Proprietary data (drawings, crops, dimensions) stays under `data/` (gitignored) or the runtime store;
  nothing client is committed (repo-hygiene guard green). Demo/test content uses the committed
  non-drawing PDF or synthetic fixtures.

## Out of scope / deferred
- **Redline** (annotated drawing) — blocked on semantic typing (#274 + Q20).
- **The AI reading half** (semantic typing, matching, agentic reader) — at the wall; see
  `docs/V1_LAYERS_STATUS.md`.
