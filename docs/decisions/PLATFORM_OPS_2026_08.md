# Platform / ops decisions — 2026-08-22

Rulings on five operational items handed off for decision, plus two housekeeping notes. Code changes
named here are for the dev to make; this file is the decision record. Grounded in the repo + AGENTS.md.

---

### D1. Hatchet task-level timeout and retries are unset.
status: DECIDED
decision: **Retries — wire now.** Map the FULL `RETRY_POLICY[stage]` (#216) into `workflow/review.py`'s `register()`: `retries = max_attempts - 1`, plus the backoff base/cap if Hatchet's `task()` exposes them (it supports backoff config) — so the policy table is the single source and can't drift from the engine. If a policy field has no Hatchet equivalent, note the gap rather than silently dropping it. **Timeout — defer**, but pin it to B6.x's acceptance ("set per-stage `execution_timeout` from measured stage durations"), not just "leave it". A number chosen before measuring a real drawing set is a guess, but the SDK's 60s default is wrong the moment B6.x puts rendering/OCR behind a stage, so it must be tracked, not forgotten.
because: `RETRY_POLICY` exists but nothing wires it, so table and engine are already independent (a silent inconsistency); the timeout has no measured basis yet, and AGENTS §8 is measure-before-hardening.
source: workflow/review.py `register()` · #216 (RETRY_POLICY: 3 attempts / 2s base / 120s cap) · hatchet-sdk 1.37.2 defaults (60s, 0 retries) · AGENTS.md §8
affects: workflow/review.py; the B6.x (#163) contract

### D2. Rename `max_concurrent_page_tasks`?
status: DECIDED
decision: Keep the name. #163 (B6.4) makes it literal, and the docstrings in `app/config.py` and `workflow/hatchet_app.py` already state its current effect. A rename now + rename-back at #163 is churn plus a stale-reference window for no gain.
because: a documented forward-looking name costs nothing; the round-trip costs two changes.
source: app/config.py, workflow/hatchet_app.py docstrings; #163
affects: none (no change)

### D3. Observability has no home.
status: DECIDED
decision: Create a dedicated **F2-observability story in Phase 6** (design: `DESIGN_CONTROLS §3`): the OpenTelemetry tracer setup, the span-naming convention, and one `trace_id` propagated package→finding. Do **not** instrument `run_stage` piecemeal inside an unrelated story — declining that (in #215) was right; a cross-cutting convention shaped by its first caller is the failure mode. Until the story lands the requirement is **UNMET**, and `opentelemetry-sdk` being declared in `pyproject.toml` must not read as done (one logger, zero spans today).
because: AGENTS §8 lists observability as a Phase-6 exit-gate deliverable and DESIGN_CONTROLS §3 (F2) is its design home; this is deferred-to-a-phase, not deferred-forever.
source: AGENTS.md §8 + §6 requirement (structured logging, OTel spans, one trace_id); DESIGN_CONTROLS §3; pyproject.toml (otel declared, unused)
affects: a new Phase-6 story

### D4. CI hardening sweep (zizmor).
status: DECIDED
decision: Do the sweep as its **own** change — pin all actions to commit SHAs and drop persisted checkout credentials across `quality` and `guards`. Scope the **guards credential change separately and test it**: guards reads issues with a token and must keep that access while dropping persisted checkout creds, so it cannot be changed blind. A one-job fix (#388) leaves the rest exposed.
because: unpinned actions (supply-chain) and persisted credentials (token exposure) are real risks; the fix to guards' token handling needs its own testing.
source: zizmor findings; #388 (partial fix)
affects: .github/workflows/ (quality, guards) — its own PR

### D5. Weekly cron scope.
status: DECIDED (confirms the call already made)
decision: Keep `quality` and `guards` **out** of the weekly cron — it buys only the schema-drift check. Full CI already runs on every PR, so paying private-repo Actions quota to re-run it weekly adds little. One caveat: if runtime dependencies are **not** lock-pinned, add a lightweight weekly dependency-resolution + licence check as drift insurance (that is the one thing PRs don't cover); if they are pinned, the schema-drift-only cron is correct as-is.
because: the cron should buy only what PRs don't already cover — drift that happens without a PR — not duplicate PR CI against a metered quota.
source: ci.yml (quota note); the schema-drift cron
affects: ci.yml (no change unless deps float)

---

**Housekeeping, no ruling needed:**
- **ADR-0011 citation drift — already fixed.** `scripts/client_facts.py`, `docs/DESIGN_PRODUCT.md §5` and the `docs/CLIENT_FACTS.md` header now cite `rules/publication.py` / `TOLERANCE_UNCONFIRMED`, not ADR-0011 (correct — the rule is a publish-gate mechanism, not an ADR). The only remaining ADR-0011 reference in CLIENT_FACTS is the footer's *correct* usage ("ADR-0011's cross-unit allowance").
- **alembic.ini test-path sweep — do it, dev-owned.** Resolve `Config("alembic.ini")` relative to the repo root (via `__file__`), not the working directory, in the three remaining files (`tests/app/test_migrations_roundtrip.py`, `tests/lifecycle/test_{supersede,events}.py` — whichever of the five are unfixed), matching the fix already applied to the two touched files. Cheap, test-only, keep it consistent.
