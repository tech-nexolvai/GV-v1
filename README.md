# Graniti Vicentia — V1 Shop-Drawing Review

AI-assisted review of vendor **shop drawings** against the approved **architectural set** and a
**checklist** — returning **PASS / FAIL / NOT FOUND / REVIEW REQUIRED** per check, each with the
exact page as evidence, plus a marked-up ("redline") PDF for the vendor. It validates drawings; it
never designs them. A human always signs off.

> **Core principle:** *The AI reads. Evidence qualifies. Deterministic Python decides. A reviewer
> signs off.* The verdict is exact arithmetic — never an AI guess. Primary safety metric = the
> critical **false-PASS** rate.

## Scope (V1)
- **In:** cabinets + countertops; digital PDFs (primary) with a scanned-page fallback; selected
  dimensions, materials and identifiers; evidence-linked findings + redline PDF; reviewer corrections
  captured.
- **Deferred:** other categories, Google Drive integration, brand-prototype training, cross-item
  interdependencies, automatic rule learning, autonomous approval, multi-tenant, HA/autoscale.

## Start here (read in this order)
1. **`AGENTS.md`** — the build guide: golden rules, architecture, stack, repo layout, coding
   standards, build order, release gates. (Codex/Cursor/most agents read this; Claude Code reads
   `CLAUDE.md`, which points here.)
2. **`memory.md`** — locked decisions, open must-fixes, and client status.
3. **`docs/`** — the authoritative design:
   - `GV_Backend_Architecture_Proposal.pdf` — primary backend design.
   - `GV_V1_Agentic_systemDesign.pdf` — V1 architecture + stack + cost.
   - `RULE_ENGINE_SPEC.md` — extended rule operations/schema (needed for the real first rules).

## Build order (risk-first — see `AGENTS.md` §8)
`0 gold-set → 1 core loop → 2 canonical evidence → 3 gate + rules → 4 bounded agent →
5 matching/retrieval → 6 durable platform → 7 reviewer product.`
Prove extraction accuracy on real GV drawings **before** building the durable platform.

## Stack (summary)
Python 3.12 · FastAPI + Pydantic · React/Vite + PDF.js · Hatchet OSS · LangGraph (extraction only) ·
PostgreSQL (+ pg_trgm, pgvector) · S3 · pikepdf · pypdfium2 · pdfplumber · PaddleOCR · docTR · OpenCV ·
Shapely · Nova 2 Lite via Bedrock (ambiguous crops only) · ReportLab/pypdf/openpyxl · pytest gold-set ·
OpenTelemetry · Docker Compose (one 8 GB VM). No AGPL deps.

## Repository layout
See `AGENTS.md` §5. Key zones: `app/` (control plane), `workflow/` (Hatchet), `extraction/`,
`evidence/`, `rules/`, **`verdict/` (isolated, deterministic)**, `retrieval/` (advisory), `reports/`,
`eval/` (gold-set), `frontend/`.

## Notes
- This is the standalone **V1 build** repo (separate from the throwaway proof-of-concept demo that
  got client buy-in). Run `git init` here to start version control.
- **`data/` and `eval/gold_set/cases/` are git-ignored** — they hold proprietary client drawings and
  answer keys. Never commit real drawings; keep them local or in private storage.
- **Semantic type names are provisional** — defined in one place (`rules/semantic_types.py`); confirm
  with Raj before Phase 3 so a rename stays a one-file change.
- Everything is versioned, hashed and reproducible; the verdict path is physically isolated with no
  model/retrieval/memory access. Do not weaken those boundaries.
