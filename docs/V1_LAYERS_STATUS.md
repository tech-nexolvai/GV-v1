# V1 — layer status (both types)

**As of:** 2026-09-07 · `main` at the V1-loop close (#533). This is the authoritative account of what
is built, what is left, and what still needs adding, for **both product types (cabinet + countertop)**.

## What V1 is (from the meetings)

Review shop drawings against approved designs, for **cabinet and countertop**, each through one shared
pipeline: **ingest → extract dimensions (agentic OCR + vision + geometry) → match arch↔shop → validate
evidence → run that type's rules → verdict → reviewer signs off → output.** The reading layer the
client stressed is **agentic OCR / vision models** ("Agentic OCR we have to use for better accuracy" —
Aug 25 call; Abhishek to pick the models). **The pipeline layers are shared across both types** — they
are not built twice. Only the *rules* are per-type.

## Layer status

| Layer | Status | Done | Left / to add | Blocked by |
|---|---|---|---|---|
| **Orchestration** (workflow spine) | ✅ Done | 6/6 durable stages, idempotency, corroboration | — | — |
| **Ingestion** (PDF → pages) | ✅ Done | reads any PDF → pages, manifest | page classification (title-block locator) | #274 (drawing-specific) |
| **Extraction — mechanical** (text / geometry / OCR) | ✅ Done | vector text + geometry + OCR wired → untyped candidates + crops | — | — |
| **Extraction — semantic** (value → *meaning*) | ⚠️ Gated | reviewer confirmation; exact vector tag + same dimension-line qualification | broader layout vocabulary and ambiguity review; no heuristic verdict operands | Q20/layout config |
| **Retrieval** (arch↔shop match) | ⚠️ Wired but inert | matcher (id→alias→geometry), 85 tests, stage REAL | item/view detection so it returns real matches | #274 + Q20 (semantic) |
| **Agentic AI** (agent + vision/OCR model) | ❌ Not wired | LangGraph agent built; Bedrock/Nova adapter built | wire agent+model into pipeline; creds; agentic-OCR | Abhishek's model decision **+** semantic trigger (#274+Q20) |
| **Rules — cabinet** | ✅ Done | `cab_filler`, `cab_arch_vs_shop` (2), tested | (arch-vs-shop tolerance) | Q2 |
| **Rules — countertop** | ✅ Done | depth, width, sink-cutout ×4 (6), tested | back-offset value; more from the countertop deck | vendor value; Raj's deck |
| **Reviewer UI + human-confirm bridge** | ✅ Done | upload → confirm/type → run → review → sign-off → download | — | — |
| **Output — reviewer artifacts** | ✅ Done | findings workbook, branded PDF, evidence-grounded redline, downloadable after sign-off | broader redline coverage follows qualified typed locations | semantic coverage |

**Hard evidence for the still-unwired agent row:** the drawing-agnostic default keeps
`model_invocations = 0` and creates no automatic canonical observations. The exact-tag lane requires
explicit per-layout configuration; agent/LLM suggestions remain reviewer work and cannot become verdict
operands.

## Summary

- **For both types, the human-operated V1 is done** — ingestion, mechanical extraction (incl. OCR),
  orchestration, both rulesets, the reviewer loop, output. It ships today, on drawings GV already has.
- **For both types, the agentic-AI automated reading is NOT done** — semantic typing, real matching,
  and the agent/vision-OCR layer. Built-but-unwired + partly not built, all gated on **#274 (drawings)
  + Q20 (vocabulary) + Abhishek's model decision.**
- Nothing is per-type-blocked: cabinet and countertop are in the same state; one shared pipeline.

## Wiring order — when #274 + Q20 + the model decision land

The order the automated half comes online, with what each step depends on:

0. **Model connection** *(needs only Abhishek's decision — can start before the drawings)*: credential
   and smoke-test the chosen vision/OCR adapter (Bedrock/Nova, or a parallel adapter to the same
   interface). Proves the model returns structured readings. No pipeline wiring yet.
1. **Page classification** *(needs #274)*: build the title-block locator against real sheets so each
   page is classified (which sheet / view). Feeds the "which sheet to read" routing.
2. **Semantic typing — value → meaning** *(needs #274 + Q20 + the model)*: the linchpin. The safe
   exact-tag lane is now present: an explicit Q20-valid vector tag and numeric reading must share one
   resolved dimension line before a corroborated canonical observation can reach the evidence gate.
   Ambiguous agent/vision suggestions **abstain** to the reviewer; they never write
   `candidate.semantic_guess` or become an operand.
3. **Retrieval item detection** *(needs step 2)*: with items typed/detected, the matcher produces real
   `match_candidates` (arch↔shop) instead of zero.
4. **Agentic-AI trigger** *(part of step 2)*: the agent runs only where fixed typing left ambiguity
   (DESIGN_AI §3.1) — it never runs on a value fixed extraction already settled.
5. **Redline output** *(partly complete)*: typed, sealed operands with a recorded page region now
   produce annotated drawings; untyped or unlocated findings are listed without a mark. Coverage grows
   only as qualified typing grows.
6. **Gold-set validation → release gate** *(needs #274's reviewed set + step 2)*: run the full auto
   pipeline on the reviewed packages; assert auto verdicts match the human answers; measure the
   **critical false-PASS rate** — the ship gate.

**The one that unlocks the most is step 2 (semantic typing).** Steps 3, 5, 6 all wait on it, and it is
what turns the reviewer's *confirm* into the AI's *propose*. Same pipeline, one seam filled.
