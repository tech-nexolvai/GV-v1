# ADR-0019 — Model-invocation provenance for review-time model calls

**Status:** Proposed        <!-- Proposed | Accepted | Rejected | Superseded by ADR-NNNN -->
**Date:** 2026-09-16
**Decides:** the provenance shape of a `model_invocations` row for a call that is not an extraction (#620)
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. Only the admin may set `Status: Accepted`.
> `scripts/ratify.py` and the issue gate keep #620 blocked until the status reads Accepted.

## Context

`model_invocations` records every model call — success or failure — so that "is the AI actually
doing anything?" is answerable from the ledger and so that the F5 per-package cost ceiling can sum
real spend. Today it does not: two paid, reviewer-facing model calls never write a row.

- `app/review/chat_bedrock.py:BedrockReviewerChat.compose` calls Bedrock `converse` directly and
  returns a narrative batch. No `record(...)`.
- `workflow/findings_bedrock.py:BedrockFindingsComposer.compose/_attempt` does the same for the
  findings narration.

Only the extraction/agent path records (`app/runs/invocations.py:record`, wired from
`app/runs/agent_checkpoints.py`). So `model_invocations` reads **0**, and the cost ceiling that is
supposed to count these calls counts nothing.

The blocker is structural, not a missing call. `app/models/runs.py:ModelInvocation` — the
`model_invocations` table — makes **`extraction_run_id` a `NOT NULL` foreign key to
`extraction_runs`** (`ondelete="RESTRICT"`), and `extraction/models/invocations.py:InvocationRecord`
requires `extraction_run_id: UUID` at construction. **A reviewer-chat or findings-narration call
belongs to a package revision and a set of findings — it has no extraction run.** There is nowhere
to put its provenance, so it cannot be recorded as the table stands. The table is also `Immutable`
(append-only), which is a property this decision must preserve, not weaken.

This is not a value the client owes and it is not a tolerance. It is our own data-model choice about
how a non-extraction model call is attributed, and it is `needs-architecture` precisely because it
changes a safety-adjacent, append-only table.

## Options considered

1. **Widen the provenance on `model_invocations`.** Make `extraction_run_id` nullable, add a nullable
   `package_revision_id` foreign key, and a table `CHECK` that **exactly one** of the two is set.
   One table, one "every call is recorded" invariant, queryable by package for the cost ceiling.
   Cost: an Alembic migration on an `Immutable` table and a widened `InvocationRecord`.
2. **A sibling table `review_model_invocations`** keyed on `package_revision_id`. The extraction
   table is untouched, but "every model call is recorded" now lives in two places, and every cost
   sum, every "how much did the AI do?" query and every F5 ceiling has to union two tables and stay
   in step as columns evolve. Two schemas drift; one does not.
3. **Mint a synthetic extraction run per package.** Rejected. It writes rows to `extraction_runs`
   that never extracted anything, so "an extraction run" stops meaning what it says, and every query
   over extraction runs has to learn to exclude the fakes. It buys a smaller migration by corrupting
   a load-bearing concept.

## Decision

**Option 1 — widen the provenance.**

- Migration (a new Alembic revision; never edit a shipped one): make
  `model_invocations.extraction_run_id` nullable, add nullable `package_revision_id`
  (`ForeignKey("package_revisions.id", ondelete="RESTRICT")`), and add a `CheckConstraint` named
  `model_invocation_one_origin` asserting
  `(extraction_run_id IS NULL) <> (package_revision_id IS NULL)` — exactly one origin, never both,
  never neither.
- `extraction/models/invocations.py:InvocationRecord` gains an optional `package_revision_id: UUID |
  None = None` and validates the same exactly-one-of rule at construction, mirroring the existing
  `_exact_count` discipline, so a call with no origin cannot be built rather than being refused only
  by the database.
- `app/runs/invocations.py:record` passes `package_revision_id` through to the row.
- A small adapter (e.g. `app/runs/review_invocations.py:record_review_invocation`) maps a Bedrock
  `converse` response — usage tokens, latency, the model/prompt/template ids, the outcome including
  refusal and timeout — to an `InvocationRecord` carrying `package_revision_id`, and both call sites
  are wrapped in `try/finally` so a failed call still records exactly one row with zero output
  tokens.

`crop_artifact_id`, `candidate_id`, `assembled_context` and `bound_pt` stay `None` for a review-time
call — they describe extraction, and a narration call has none of them.

## Consequences

- **Easier:** the ledger finally answers "is the AI doing anything?" for the reviewer-facing calls,
  and the F5 cost ceiling can sum one table to see per-package spend. A single invariant — every
  model call, whoever made it, is one row — holds again.
- **Harder:** one migration on an append-only table, and every reader of `extraction_run_id` must
  tolerate `NULL`. The `CHECK` makes the "exactly one origin" rule impossible to violate, which is
  the point.
- **Forbids:** a `model_invocations` row with no origin, or with both — the database refuses it.
- **Does not amend any golden rule in `AGENTS.md`.** The table stays append-only/immutable; §2.7 is
  preserved by adding columns and a constraint, not by mutating rows.

## Safety impact

**None on the critical false-PASS rate.** This is accounting only. Reviewer-chat and
findings-narration output is untrusted narrative that never becomes a verdict operand (`AGENTS.md`
§2.1), and this decision does not change what any model is allowed to do — only that the call is
recorded. If anything it improves safety-adjacent visibility: the F5 budget ceiling can now see the
spend it currently misses, so abstention-on-overflow counts these calls instead of under-counting
them.

## Unblocks

- **#620** — record model invocations on the reviewer-chat and findings-narration paths. Once this
  ADR is Accepted, set #620's contract `design:` to `docs/adr/0019-review-model-invocation-provenance.md`
  and `status: ready`.
