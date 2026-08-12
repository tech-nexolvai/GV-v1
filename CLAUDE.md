# CLAUDE.md — Claude Code guide for the GV V1 build

**Read `AGENTS.md` first — it is the single source of truth for this project** (what we're
building, the golden rules, the architecture, the stack, the repo layout, coding standards,
the build order, and the release gates). This file only adds Claude-Code-specific working
notes. If anything here conflicts with `AGENTS.md`, `AGENTS.md` wins.

## The one rule (never break it, in any code or suggestion)
**The AI reads. Evidence qualifies. Deterministic Python decides. A reviewer signs off.**
The pass/fail verdict is exact arithmetic — never an LLM judgment. The verdict service has no
model/retrieval/memory/internet access and uses no `eval`. Primary metric = critical false-PASS rate.

## How to work in this repo
- **Plan before big changes.** For anything touching `verdict/`, `rules/`, `evidence/`, or the data
  model, use plan mode / propose the approach first — these are the safety-critical zones.
- **Build in the risk order in `AGENTS.md` §8.** Start at Phase 0 (gold set) → Phase 1 (small core
  loop). Do not jump ahead to the durable platform or the agent before the core is proven.
- **Tests are part of "done."** No change to `verdict/`/`rules/`/`evidence/` merges without unit tests
  and a gold-set run that does not regress critical false-PASS. Run: `pytest` (and the `eval/` harness).
- **Respect the trust boundaries.** Never import extraction/retrieval/network code into `verdict/`.
  Never let retrieval output become a verdict operand.
- **Exact numbers only** in the verdict path: `Fraction`/`Decimal`, canonical mm; unknown unit → REVIEW.
- **Explain in plain English** in commit messages, PRs, and any doc/UI copy. No jargon dumps; no
  over-promising; never claim 100% automation.
- **No AGPL deps** (PyMuPDF, Ultralytics YOLO). Check licences before adding a dependency.

## Conventions
- Lint/format: `ruff` + `black`; types: `mypy` (strict in `verdict/`, `rules/`, `evidence/`).
- Commits: short, imperative, scoped (e.g. `verdict: add sum_within_tolerance operation`).
- Migrations via Alembic; never edit a shipped migration — add a new one.
- Keep `memory.md` updated when a real decision is made or an open question is resolved.

## Where things are
- `AGENTS.md` — full project rules and architecture.
- `memory.md` — locked decisions, open must-fixes, client status.
- `docs/` — the two architecture PDFs + `RULE_ENGINE_SPEC.md`.

## Current open must-fix (before the rules phase)
The typed operation registry can't yet express the real first rules (countertop width =
sum of cabinets + fillers + field cut, tolerance by wall layout; cabinet-filler distribution).
Fold in `docs/RULE_ENGINE_SPEC.md` (aggregate `sum_within_tolerance`, variable-length inputs,
applicability-driven tolerance, literal/USER_INPUT operands) at Phase 3. See `AGENTS.md` §7.
