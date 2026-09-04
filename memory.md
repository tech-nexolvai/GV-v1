# memory.md — GV V1 project memory (decisions, open items, status)

> Durable, repo-level record of *why* things are the way they are. Update this whenever a real
> decision is made or an open question is resolved. Newest notes at the top of each section.
> (This is the project's decision log — distinct from any coding-agent's private/session memory.)

## The invariant (never changes)
The AI reads. Evidence qualifies. Deterministic Python decides. A reviewer signs off. The verdict
is exact arithmetic against a tolerance — never an LLM judgment. Primary safety metric = critical
**false-PASS** rate.

## Locked decisions (with rationale)
- **Workflow-first, not agent-first.** The product is a durable document-processing + approval
  workflow; the AI is a bounded step inside it. Orchestrator = **Hatchet OSS** (Postgres-backed,
  MIT, retries/DAGs/durable waits). Temporal is deferred behind measured triggers (don't pay
  production-workflow overhead before the extraction core is proven).
- **Deterministic verdict, physically isolated.** Separate `verdict/` service/container with no
  model/retrieval/memory/internet access and no `eval`. Only evidence-gate-approved, versioned
  operands cross into it. This is the whole product's trust and the false-PASS defense.
- **Two-stage evidence:** `ObservationCandidate` (raw, uncertain) → `CanonicalObservation`
  (normalized, corroborated) → sealed `VerdictOperand` (post-gate). Raw model/OCR output can never
  jump straight into a check.
- **Evidence states:** RAW_CANDIDATE / CORROBORATED / CONFLICTING / HUMAN_CONFIRMED / REJECTED — only
  CORROBORATED or HUMAN_CONFIRMED may enter the verdict. OCR disagreement → CONFLICTING → REVIEW.
- **Exact-first matching / advisory retrieval:** exact normalized ID → aliases → hard filters →
  geometry → pg_trgm → BM25/FTS → dense pgvector → RRF (exact ID pinned). Retrieval only suggests;
  it never supplies an operand. (Coded IDs like X-223 vs X-233 are why exact/lexical beats embeddings.)
- **Data ownership:** PostgreSQL = business truth + audit; Hatchet Postgres = execution state; S3 =
  immutable originals/crops/reports (versioning + hashes; Object Lock optional). One managed engine;
  split to RDS/Qdrant/OpenSearch/graph only on measured triggers.
- **Reliability patterns:** transactional outbox (no dual-write between DB + Hatchet), idempotency
  keys, append-only versioning, package state machine (CREATED→…→APPROVED with side states).
- **Rules:** authored in YAML → validated (Pydantic + JSON Schema) → immutable JSON snapshots; a
  deterministic **Applicability Resolver** picks the snapshot; changes need human approval + full
  gold-set regression; never auto-generated from corrections.
- **Extraction:** digital-first (pdfplumber vector geometry) is the primary, cheap, reproducible
  path; PaddleOCR + docTR (must agree) for raster; Nova 2 Lite via Bedrock only for ambiguous crops.
  LangGraph is bounded (6–8 steps, ≤2 OCR retries, ≤2 VLM calls, abstain → REVIEW) and cannot issue
  verdicts. **Nova 2 Lite:** structured outputs unsupported → use client-side tool schema + strict
  Pydantic validation.
- **Licensing:** no AGPL — avoid PyMuPDF and Ultralytics YOLO; use pdfplumber (MIT), pypdfium2,
  pikepdf (MPL), PaddleOCR/docTR (Apache), ReportLab (BSD).
- **Deployment (pilot):** one 8 GB VM, Docker Compose; verdict process isolated even on the shared
  VM. ~Rs 6,000–9,000/month planning budget.
- **The UI states nothing the backend has not said (#468).** Screens show fetched data or an explicit
  blank — never an illustrative number. The workspace had been claiming a critical false-PASS rate of
  0.0%, drawing a fake drawing under a real extraction polygon, and writing its own FAIL verdicts with
  invented millimetre tolerances. Loading, failure and emptiness are three distinct states: "no
  findings" on a failed fetch reads as *this drawing is clean*. Where nothing is on the wire the
  screen says so — including the signed-in reviewer's name, which is shown as a generic label until an
  identity endpoint exists, having previously displayed the client's name to everyone.

## OPEN — must-fix / to resolve
- **Rule engine can't yet express the real first rules.** Typed op set lacks aggregate/variable-input
  operations. Before Phase 3, fold in `docs/RULE_ENGINE_SPEC.md`: `sum_within_tolerance` + `sum`,
  variable-length inputs (cardinality one/many, scope same_assembly), applicability variants
  (tolerance + field-cut count by wall_config), literal + USER_INPUT operand sources. Needed for
  CT-1 (countertop width = cabinets + fillers + field cut) and Raj's cabinet-filler distribution.
- **Semantic type vocabulary** — confirm exact names with Raj: `countertop_overall_width`,
  `cabinet_width`, `filler_width`, `wall_config`, `field_dimension`, materials, etc.
- **How `wall_config` is established** — read from the plan (walls on which sides) or reviewer input;
  if neither → REVIEW.
- **Cabinet-filler "distribute across adjustable cabinets"** logic + which cabinets are non-adjustable
  (sink / equipment) — needs a typed representation; the on-site field dimension is a USER_INPUT.

## Client status (waiting on / confirmed)
- **Waiting on Raj:** full countertop + cabinet rules (with tolerance + severity), one complete real
  project (shop + arch set with a countertop-on-cabinets example), global rules per item type, and a
  5–10 case gold-set of past reviewed drawings with mark-ups. All "will send."
- **Confirmed by Raj:** tolerance depends on wall layout; checklist targets vanity tops (reusable for
  kitchen if same layout); review is by category but must cross-check compatibility (countertop vs
  cabinet); dimensions live on dimension lines; inch & mm always agree (mm canonical); build against
  FINAL sets; items can carry a **vendor-supplied unique ID in the drawing tag** → prefer exact-ID
  matching; VIF in a final set is a flag.

## Domain facts to remember
- Fillers absorb the gap between the real wall-to-wall and the sum of cabinets, split L/R, each within
  min/max; a large gap distributes across ADJUSTABLE cabinets (not sink/equipment). The on-site "field
  dimension" is on no drawing (user input).
- The four outcomes: PASS/FAIL (verdict engine) · NOT FOUND (missing authoritative input) ·
  REVIEW REQUIRED (conflict / ambiguity / judgment) — the last two are the honest-abstention path.
