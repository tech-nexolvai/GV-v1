# New inputs from the Raj call — 2026-08-25

Captures the items from the 2026-08-25 client call that are **new topics**, not answers to the 21
tracked questions in `docs/CLIENT_FACTS.md` (those were folded in directly — see Q4, Q10, Q11,
Q15–Q19, Q21). These are extraction/build inputs and engineering direction. Grounded in the meeting
transcript + the client's meeting summary. Where the transcript segment on hand does not cover an
item, it is marked **summary-only (unverified)**.

---

### N1. Sink cut-out color-coding — vendors draw the cut-out in a distinct color (blue).
status: NOTED (extraction aid, not a rule)
detail: Raj suggested vendors outline the sink cut-out in a specific color (blue) so the system can
locate it reliably on the countertop drawing. This is an **extraction hint**, not a check — it makes
the cut-out region easy to segment before any dimension is read. It is a *convention we can request*,
not something the current drawings guarantee, so the extractor must still work without it (fall back
to geometry/labels) and treat color as a locator, never as verdict evidence.
because: the cut-out is the hardest region to find on a busy countertop plan; a color convention turns
a search problem into a mask. But relying on it would make us brittle to any vendor who does not follow
it — hence locator-only.
affects: extraction/ (cut-out region detection); a possible line in the vendor drawing-standards ask.
open: is blue a standard we ask all vendors to adopt, or a one-vendor nicety? Confirm with Raj.

### N2. View-based checklist — each check reads from a specific drawing view.
status: PENDING CLIENT INPUT (Raj to provide the full per-view list)
detail: Checks are not uniform across the sheet — they live in specific views:
  - **Plan**: cut-out position, front/back offsets, wall-to-wall dimension.
  - **Elevation**: cabinet widths, fillers, tags.
  - **Section**: overhang (only checkable in a section — not visible in plan or elevation).
Raj offered to give the complete mapping of check → view. Until it lands, treat the above as the
working set and do not assume a check can be read from a view it does not appear in.
because: routing a check to the wrong view means reading a dimension that isn't there → NOT_FOUND or,
worse, reading the wrong number. Overhang is the clear case: absent from plan/elevation, so a plan-only
pipeline silently never checks it.
affects: extraction routing (which page/view feeds which check); rule authoring (the view a rule's
evidence comes from). Ties to Q20 — the full checklist arrives with the finalized layouts.
open: full check→view mapping from Raj (owed).

### N3. Unit-conversion feature — normalize to inches before comparison.
status: TO BUILD (Anant's action item)
detail: When a drawing gives a dimension only in mm or feet, convert to inches before the check.
Support **mm → inches** and **feet → inches**. **Yards are out of scope.** This does NOT change the
verdict model (inches remain authoritative, mm is ignored for the verdict per Q12) — it exists so a
value expressed in another unit can still be *read into* the inch comparison, not so units are compared
across each other.
because: some drawings label in mm only; without conversion those read as NOT_FOUND even though the
value is present. Keep it a read-time normalization, upstream of the Fraction comparison (ADR-0001),
so the exact-match logic is untouched.
affects: extraction/normalization; must stay consistent with Q12 (inches govern) and ADR-0001.

### N4. Model / infrastructure direction — under research, not decided.
status: RESEARCH (Abhishek leading; no commitment yet)
detail: Directions raised for the extraction/vision + answer-generation stack:
  - **AWS Bedrock** — one key fronting multiple models (vs OpenRouter).
  - **Open-source vision models** — Qwen, Kimi, NVIDIA Nemotron — for drawing understanding.
  - **Agentic OCR** — iterative read/verify rather than single-pass OCR.
  - **OpenAI** for answer generation (existing credits available).
because: recorded so the direction isn't lost; NOT a locked architecture decision. Any actual choice
goes through the normal design path and must preserve the core invariant — the model reads and
qualifies evidence, deterministic Python decides the verdict, whatever the model.
affects: future extraction/model story; does not change any current interface.
open: Abhishek's research outcome; a proper design doc before adoption.

---

**Verification note:** N1 and N2 are in the 36-minute transcript segment and are quoted from it. N3
and N4 draw on the client meeting summary; the parts beyond the transcript segment (e.g. the filler
min/max numbers behind Q21's flag) are **summary-only and still to be reconciled** with Raj / the full
recording.
