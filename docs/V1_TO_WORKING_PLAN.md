# V1 → Working: the plan to make the system actually run end-to-end

**Written 2026-09-15.** This supersedes the 2026-09-12 audit as the *action* document. The audit said
what is broken; this says what to do, in what order, with the concrete files, the acceptance test for
each step, and the issue clean-up that goes with it. Every claim here was checked against the code on
`main` and the live issue list, not against the audit alone — several audit items have since been fixed
or turned out to be more complete than the audit implied. Where the two disagree, the code wins and I say so.

---

## 0. The one-paragraph truth

The deterministic half of this product — the verdict engine, the rules, the evidence gate, the audit
trail, the reviewer loop, the reports — is **built, isolated, and tested** (4,793 tests; the verdict
path physically cannot import a model or the network). What is missing to make the system *work as the
room expected* is three things, and only three: **(1)** a handful of seam bugs between "reviewer types a
value" and "the check runs" — all of which are already written and sitting in open, mergeable PRs;
**(2)** the **reading layer**, which reads ~9% of the dimensions off a CAD sheet and must be rebuilt on a
vision model *with a two-independent-read agreement gate* so a confident-wrong number can never pass;
**(3)** the **client-side inputs** we have been blocked on for weeks — the real architectural drawing set
(#274) and the final tag vocabulary for the back-only/island layouts (Q20). Nothing else on the critical
path is unbuilt. The system has never shown a single PASS, and fixing that is a days-long task, not a
rewrite.

---

## 1. What the code actually says today (verified, not from the audit)

| Area | Audit (2026-09-12) | Code on `main` (2026-09-15) | Action |
|---|---|---|---|
| Evidence link dropped when a value is typed (4.2) | "fix written, not merged" | Still broken at `workflow/stages.py:1654-1659` (naive dict overlay; reviewer operand has no `evidence_observation_id`, `workflow/measurements.py:149-155`). **Fix is open as PR #608, mergeable/clean.** | **Merge PR #608.** |
| `CAB-ARCH-VS-SHOP-001` unconfirmed tolerance (4.3) | stale blocker | **Still `tolerance: value: UNCONFIRMED`** (`rules/rulebook/cab_arch_vs_shop_001.yaml:35`). Engine correctly abstains to REVIEW (`verdict/engine.py:193`) and publication gate blocks it (`rules/publication.py`). Q2 (exact match) is answered. | **Re-author to exact (tolerance 0 mm) + regression.** |
| Hostile measurement form (4.4) | broken | **Fixed on `main`.** `frontend/main/src/pages/EnterValuesPage.tsx` has placeholders (`25 1/2" or 648 mm`), plain-English hints, server-message errors. Values pass through as strings; the server parses. | Close as done. Delete dead `ConfirmReadingsPage.tsx`. |
| Q21 distribution / Q9 cabinet pick (4.5) | not built | **Verdict op `filler_distribution` exists** (`verdict/operations/distribution.py`) and abstains rather than picking a cabinet. **No backend endpoint and no UI.** | Build the API + UI on top of the existing op. |
| Model invocations on chat/narration (4.6) | 0 recorded | Confirmed: `app/review/chat_bedrock.py` and `workflow/findings_bedrock.py` call Bedrock directly and never write `InvocationRecord`. Only `app/runs/agent_checkpoints.py` records. | Route both through `app/runs/invocations.py:record`. |
| Checks run only once per revision (#609) | — | Confirmed (lifecycle has no edge back; idempotency key ignores the requester). **Fix open as PR #610, mergeable.** | **Merge PR #610.** |
| Measure says "nothing read" while reading (#605) | — | Confirmed race. **Fix open as PR #606, mergeable.** | **Merge PR #606.** |
| Chat answers in prose (#611) | — | Confirmed; branch `609-say-what-an-outcome-means` exists. | Finish + merge. |
| Reading layer ~9% (4.1) | rebuild on vision | Live routes = pdfplumber vector + **RapidOCR** (not PaddleOCR/docTR as the docs say). Nova adapter (`extraction/models/nova.py`), the local-model adapter (`openmodel.py`), the bounded LangGraph agent (`extraction/agent/`), and the cross-route `SECOND_READER` agreement lane (`evidence/corroborate.py`) are **all built and tested but not wired into `extract_pages`.** | Wire them with a mandatory agreement gate (Phase C). |
| Arch↔shop match returns 0 | inert | Matcher is finished and tested; it has **no input** because nothing populates `drawing_items`/`drawing_views` (`workflow/stages.py:1066` returns an honest zero). | Needs item/view detection (Phase D) + #274. |
| Never a PASS | true | True. No arch numbers, no item detection, no auto semantic typing → the comparison rules can't resolve; the SHOP-internal rules (CT-DEPTH, CT-WIDTH) *can* PASS with confirmed values but no such package has been run. | Phase B builds one. |

**Doc drift worth fixing:** `AGENTS.md`/`memory.md` still say PaddleOCR + docTR; the engine is RapidOCR
(ONNX, Apache-2.0). Update the stack docs so the licence/allow-list record is true.

---

## 2. Target architecture (unchanged spine, one seam rebuilt)

The pipeline spine does not change — it is the safe part and it is correct:

```
upload → immutable store + hashed versions → outbox → workflow
  → ingest → extract_pages → match → validate_evidence → run_checks → generate_outputs
                                               │
              ISOLATED verdict service (typed ops, Fraction/Decimal, no model/net/retrieval)
```

The **only** structural change is inside `extract_pages` and the `validate_evidence` gate: the *reading*
of a number stops being a single accepted read and becomes a **two-independent-read agreement**, and only
an *agreed* number is even eligible to become evidence.

### 2.1 The reading layer, redesigned (the heart of the work)

The invariant that makes this safe is already in the codebase as a dormant lane — we are turning it on and
making it mandatory, not inventing it:

```
                       ┌─ vector read (pdfplumber)  ─┐
region of a drawing ──►├─ raster read (RapidOCR)    ─┤─► two reads of THE SAME region
                       └─ vision read (Nova / local)─┘
                                                       │
                         evidence/corroborate.py  SECOND_READER lane
                                                       │
                    agree exactly? ──► CORROBORATED (eligible for the gate)
                    disagree?      ──► CONFLICTING  ──► REVIEW REQUIRED (never a verdict operand)
                    only one read? ──► RAW_CANDIDATE ──► reviewer confirms, or bounded agent retries
```

Rules that make this defensible (all already expressed in the corroboration/gate code; we are enforcing,
not loosening them):

- **Two *different* extractors** must produce the **same parsed value** (`evidence/corroborate.py`
  `_same_numeric_reading`) — confidence scores are never consulted. This is the direct answer to the
  experiment where Nova read `46"` as `60` and the naive gate accepted it: a second reader would not have
  agreed, so it would land in REVIEW, not PASS.
- **mm↔inch is corroboration, never a second reader and never a verdict operand** (Q12). The dual-unit
  lane can confirm an inch was *read* correctly; it can never decide.
- **Agreement on the number is not agreement on the meaning.** A corroborated number still needs a
  semantic type (Phase D) before it can become an operand; until then it is a confirmed *reading* the
  reviewer accepts, which is exactly the "use this reading" path PR #608 restores.
- **The bounded agent runs only on ambiguity** (`extraction/agent/trigger.py`): RAW_CANDIDATE + fixed
  extraction complete + a crop + a closed ambiguity reason. It plans nothing; it executes ≤1 primary VLM
  call, ≤2 OCR retries, and abstains on any bound. It can never issue a verdict — there is no tool for it.

### 2.2 Where the model is allowed, and where it is forbidden

- **Allowed:** turning pixels into *candidate* numbers (extraction), suggesting which reading fills which
  field (autofill), matching hints (advisory), narrating findings in prose (chat).
- **Forbidden, enforced by `tests/test_verdict_isolation.py`:** supplying any number that reaches
  `verdict/`, picking a tolerance, resolving a numeric disagreement, or approving a package.

---

## 3. The plan, in order (risk-first, each phase shippable)

Ordered so that every phase leaves the demo strictly better than before, and the expensive/blocked work
comes only after the cheap wins are banked.

### Phase A — Land the in-flight fixes and restart (≈1 day, no new code)
The four demo failures are already written. This phase is review + merge + deploy.

1. Merge **PR #606** (#605 — Measure waits for the reader; writes out the "still working" states explicitly).
2. Merge **PR #608** (#607 — accepting a reading keeps its drawing; fixes the 4.2 evidence-drop merge bug).
3. Merge **PR #610** (#609 — recheck after supplying a value; lifecycle edge back + requester in the idempotency key).
4. Finish + merge **#611** (chat renders tables composed from stored findings, never from the model).
5. **Restart the API** (Python does not hot-reload; some merged fixes are not live on the demo box).

**Acceptance:** upload → type a value → Run checks → findings change; evidence panel still shows the crop
after a value is accepted; Measure never says "nothing read" while reading; chat shows a findings table.

### Phase B — Get a real PASS on screen + honest instrumentation (≈2–3 days)
Nothing green has ever appeared. Fix that with a SHOP-internal check that does not need arch data.

1. **Re-author `CAB-ARCH-VS-SHOP-001` to exact** (Q2): `tolerance: {value: 0, unit: mm}` — an exact
   `pairwise_within_tolerance` (or add a dedicated exact `pairwise_equals`). Bump to `1.1.0`. Remove it
   from the unconfirmed-tolerance blocklist; run the full rule regression. This deletes the
   "tolerance has not been supplied by the client" message that reads terribly.
2. **Build one honest PASS package**: a countertop shop drawing where `CT-DEPTH-001` /
   `CT-WIDTH-001` genuinely balance (depth = cabinet_depth + overhang; width = Σcabinets + Σfillers +
   field cuts). With values confirmed through the reading path (post-#608), these resolve to PASS. This is
   real arithmetic on real (confirmed) readings — not a mocked verdict.
3. **Record model invocations on the narration path (4.6)**: wrap `chat_bedrock.py` and
   `findings_bedrock.py` calls through `app/runs/invocations.py:record` with an `InvocationRecord`
   adapter, so "is the AI doing anything?" is answerable from the ledger and F5 cost ceilings count the
   spend they currently miss.

**Acceptance:** at least one finding shows **PASS** with its evidence crop; `model_invocations` is
non-zero after a chat; the publication gate reports zero unconfirmed tolerances for cabinet rules.

### Phase C — Rebuild the reading layer with agreement (≈2–4 weeks, the real work)
This is the difference between the product we have and the product they think they bought. It needs the
model decision (Abhishek) but **not** #274 — it can start on the shop drawings we already have.

1. **Wire the cross-route `SECOND_READER` agreement into `extract_pages`.** Today the vector/OCR/markup
   routes write independent candidates and no cross-route agreement runs (`evidence/corroborate.py`
   `corroborate()` exists but only the DUAL_UNIT lane is called at write time). Compute agreement per
   region across routes; agreement → CORROBORATED-eligible, disagreement → CONFLICTING → REVIEW.
2. **Wire the two chosen vision readers as routes.** Emit `ObservationCandidate`s into the same
   region-keyed pool under distinct extractor names, behind the `InvocationRecord` accounting. Credential
   + smoke-test first (Phase 0 of the old wiring order).

   **Model decision (2026-09-16 — ours, not the client's):** two readers from different families so their
   errors are uncorrelated —
   - **Primary: Amazon Nova Pro** (`amazon.nova-pro-v1:0`) — strong small-text reading and tool use, same
     Bedrock credentials as the existing Nova Lite adapter (~$0.80/$3.20 per 1M in/out).
   - **Independent second reader: Claude Haiku 4.5** (Anthropic on Bedrock) — excellent small-numeral and
     structured-output accuracy, a different vendor so its mistakes differ from Nova's (~$1/$5 per 1M).

   Rough cost ≈ **$0.085 per drawing** (~30 dimensions × 2 reads); at the pilot budget that is well over a
   thousand drawings/month, so cost is not the binding constraint — accuracy (reviewer minutes) is.
   Because the two-read gate catches a wrong read as *disagreement → REVIEW*, model accuracy sets reviewer
   workload, **not** false-PASS risk. Considered and available on Bedrock: Nova Lite (baseline, too weak
   alone — misread `46→60`), Nova Premier, Claude Sonnet 4.6 (top accuracy, ~$3/$15 — the upgrade path if
   reviewer minutes get precious), Mistral Pixtral Large, and **Qwen3-VL 235B** (a document-extraction
   specialist at ~$0.53/$2.66 — a value wildcard). The top OCR models overall (Gemini 3, GLM-OCR,
   PaddleOCR-VL) are **not on Bedrock**; going off-Bedrock for one is possible via the generic
   `openmodel.py` seam but is deferred. The pairing is confirmed against our own crops by the bake-off
   spike (#637) before heavy wiring. Story: #624; bake-off: #637.
3. **Make agreement mandatory before the gate.** A single-route read is never sealed; it is a
   RAW_CANDIDATE that a reviewer confirms or the bounded agent retries. This is the concrete false-PASS
   defense.
4. **Wire the bounded agent for ambiguous regions only** (`extraction/agent/graph.py` +
   `trigger.py`) — it already exists, tested; give it a workflow call site inside `extract_pages` gated on
   `evaluate_trigger`.

**Acceptance:** on the shop drawings we hold, the share of dimensions that reach CORROBORATED (two
independent reads agree) rises materially above today's ~9% single-read survival; **no CONFLICTING reading
ever becomes a verdict operand**; the OCR_VS_VISION experiment's wrong `46→60` read lands in REVIEW.

### Phase D — Semantic typing + item/view detection (≈2–3 weeks, needs Q20; unlocks matching) 
This is the linchpin the layer-status doc calls out: it turns the reviewer's *confirm* into the AI's
*propose*, and it is what makes the arch-vs-shop comparison — the actual product — able to run.

1. **Semantic typing via the safe exact-tag lane** (`app/evidence/automatic_typing.py`): an explicit
   Q20-valid vector tag and a corroborated reading must share one resolved dimension line before a
   canonical observation is auto-typed. Ambiguous suggestions abstain to the reviewer; they never write
   `semantic_guess` or become an operand. **Blocked on Q20** (the tag names for back-only/island).
2. **Populate `drawing_items` / `drawing_views`** from typed observations so `_matchable_items`
   (`workflow/stages.py:1923`) stops returning `[]` and the matcher writes real `match_candidates`.
3. **Grow redline coverage** — it already marks typed+located findings; coverage rises automatically as
   typing grows.

**Acceptance:** `match_candidates` is non-zero on a package that has both an ARCH and a SHOP drawing;
`CAB-ARCH-VS-SHOP-001` runs for real and returns PASS/FAIL (not NOT_FOUND).

### Phase E — Q21 distribution calculator + Q9 cabinet pick (≈1–2 weeks, a real feature)
Raj asked for a system that *calculates* the redistribution, not just checks it. The arithmetic already
exists (`verdict/operations/distribution.py:filler_distribution`); it needs a product around it.

1. **Backend endpoint** that runs `filler_distribution` with the reviewer-chosen adjustable cabinet as an
   explicit input (never auto-detected — Q9), returning the proposed filler/cabinet sizes and abstaining
   (REVIEW) when fillers can't absorb the difference.
2. **UI** on the measurement/review page for the reviewer to pick the adjustable cabinet and see the
   proposed distribution. This is greenfield front and back (confirmed by the frontend map).

**Acceptance:** given a site width that differs from design, the reviewer picks a cabinet and sees a
proposed distribution; the system never picks the cabinet itself.

### Phase F — Gold set + release gate (blocked on #274)
Run the full auto pipeline on the reviewed packages GV sends; assert auto verdicts match the human
answers; measure the **critical false-PASS rate** — the ship gate (Epic A8 / #24, #188, #25).

**Acceptance:** gold-set regression green; critical false-PASS at or below the agreed bar.

---

## 4. Critical path and dependencies

```
Phase A ─► Phase B ─► Phase C ──────────────► Phase F (needs #274)
                          │                     ▲
                          └► Phase D (needs Q20)┘
                          
Phase E is independent of C/D and can run in parallel once A is in.
```

- **We own and can do now:** A, B, C, E. None are blocked on the client.
- **Blocked on the client:** D (Q20 vocabulary), F (#274 real arch set + reviewed gold cases).
- **Owed decisions:** the model choice (Abhishek) gates C's vision route; the "vendor-to-vendor"
  dynamism question (ADR-0006, open since 2026-08-25) and CT007 exact-vs-minimum (memory.md) are owed by
  Raj/Abhishek and should be chased in parallel — none block A/B/C.

---

## 5. Issue clean-up — recommendation (nothing deleted without your go-ahead)

There are ~57 open issues. Most are not "blockers" — they are **up-front epic scaffolding** created on
2026-08-14 whose work has since merged but whose epic issue was never closed, plus a few genuine blockers
and a few deferred items. Recommendation below. I have **not** closed or deleted anything — closing 30+
issues is outward-facing, so I want your confirmation first. I recommend *closing* (not deleting — closed
issues stay searchable and keep the audit trail; deletion loses history).

**A. Fix-and-close now (the live demo bugs — keep open only until merged):**
`#605→PR606`, `#607→PR608`, `#609→PR610`, `#611`, `#541` (stacked-fraction guard), `#542` (flaky pdfium mutex).

**B. Keep open — genuine blockers / active work:**
`#274` (real drawing set — P0, external), `#25`/`#188` (gold set, needs #274), `#501` (second reading
route — this **is** Phase C's cross-route agreement; promote it), `#60` (A6.3 sink-cutout family,
needs-architecture), `#15`/`#16` (client questions), `#29` (arch↔shop matching epic — Phase D).

**C. Verify-then-close (epic containers whose work is substantially merged per `V1_LAYERS_STATUS.md`):**
`#18` A2 units, `#19` A3 vocabulary, `#20` A4 typed ops, `#21` A5 rule schema, `#22` A6 rules, `#23` A7
params, `#28` B4 evidence gate, `#131` B6 page classify (partial), `#132` B7 drawing model, `#133` B8
crops, `#135`–`#139` C1–C5 platform, `#140`/`#141`/`#142`/`#143`/`#144` D1/D2/D4/D5/D6 reviewer+reports,
`#146` E2 Nova adapter (built). Each should be spot-checked against its child stories before closing; I can
do this and post a one-line completion note per issue.

**D. Keep as deferred (not blockers, correctly parked):**
`#221` (S3 versioning/Object Lock), `#268` (threshold report), `#148`/`#149` F2/F4 observability, `#150`
D3 frontend epic, `#151` F3 deployment, `#155`/`#156` F5/F6, `#152`/`#154` B10/B11 geometry/revision.

**E. Open one new tracking issue:** "V1 → Working" umbrella that points at this doc and tracks Phases A–F
as sub-issues, so the plan is the source of truth rather than 57 scattered epics.

---

## 6. What I will not do (the guarantees that make this defensible)

- No model output ever becomes a verdict operand. The `verdict/` isolation tests stay green.
- No guessed tolerance. `CAB-ARCH-VS-SHOP-001` moves to exact because **Q2 answered it**, not because it
  is convenient.
- No auto-typing without the exact-tag lane; ambiguity abstains to the reviewer.
- No "100% automation" claim. The honest product is: the AI reads and proposes, two reads must agree, the
  reviewer confirms, deterministic Python decides, and every number can be traced back to a crop.
```
