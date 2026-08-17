# C1.8 (#198) — persisting rule applicability scope: corrections

Not new decisions — a plan-versus-ratified-ADR conflict, resolved by the ADR (same shape as #236
D6a/D6b and #257 D1). ADR-0006 and ADR-0007 are Accepted and the shipped code already implements
them; the #198 plan re-expresses a rejected design in DDL. Grounded throughout. Last updated
2026-08-17.

---

### D1. Discriminators — a column per discriminator, or a child table?
status: DECIDED
decision: A child table, one row per discriminator: `RuleApplicabilityScope(rule_snapshot_id, discriminator, value)` with `UNIQUE(rule_snapshot_id, discriminator, value)`, where `value` is the variant's `when`. No `wall_config` column. Consequence you may not have noticed: whatever validates a write must reject a `discriminator` naming a project or vendor, or the table becomes the backdoor for the submitter-keyed discriminators `rules/schema.py:190` already bans.
because: ADR-0007 rejects the per-discriminator column as its alternative 2 ("a rule keyed on material or mount type would need the signature changed") and chooses the mapping; a column is that rejected design in DDL, and — since AGENTS.md forbids editing a shipped migration — every new discriminator would need a fresh one.
source: ADR-0007 (Options — what the resolver takes, alt 2 rejected / alt 3 chosen) · rules/applicability.py:32 (`CheckContext.discriminators: Mapping[str, str]`) · rules/schema.py:218 (`Applicability.discriminator: str`), :185 (`ApplicabilityVariant.when`), :190 (forbidden submitter discriminators)
affects: #198; app/models (RuleApplicabilityScope)

### D2. Does `project_id` belong on the applicability scope?
status: DECIDED
decision: No — drop it. Project is a `CheckContext` runtime input and a parameter-set key (ADR-0009), never a stored rule-applicability condition; the scope stores no project column. Consequence you may not have noticed: this is the same error as `wall_config` one layer up — the plan kept it as a "fixed key", but a project column recreates the per-project rule sets ADR-0005/0006 refuse.
because: ADR-0006 states rules are GV's own standards and "no rule is selected by project"; ADR-0007 carries `ProjectScope` through the `Resolution` "never used to filter".
source: ADR-0006 (Project scope; Vendor is metadata) · ADR-0007 (Decision — "Project scope is carried, never used to filter")
affects: #198; app/models (RuleApplicabilityScope)

### D3. `product_type` type and nullability?
status: DECIDED
decision: The `ProductType` enum, `NOT NULL` — not `Mapped[str | None]`. A rule always has a category.
because: ADR-0007's third open question made `product_type` a `ProductType` enum validated at publish (a free string lets a typo publish and match nothing), and `CheckContext.product_type` is non-optional.
source: ADR-0007 ("Three questions the draft left open" — product_type becomes a controlled `ProductType` enum) · rules/applicability.py (`CheckContext.product_type: str`, required)
affects: #198; rules/semantic_types.py (`ProductType`), app/models

### D4. Is `check_type` one of the resolver keys?
status: DECIDED
decision: No — remove it from the four-key framing. It may persist as a scalar rule attribute, but it is not an applicability-resolution key.
because: the four keys (ADR-0006 "full picture") are category, layout/config discriminator, project scope and effective version; `check_type` (internal / arch-vs-shop / global) decides which *documents* load, not which rule applies to an item.
source: ADR-0006 (the full-picture key list) · ADR-0007 (Context — "keys on four things")
affects: #198

### D5. Where does effective version live?
status: DECIDED
decision: On the snapshot — `rule.version`, selected as highest semver at resolve time — not as a column on the applicability scope. Nothing to add; it was absent from the plan only because `check_type` occupied its slot.
because: ADR-0006 selects the effective rule by highest `rule.version` with `(rule_id, version)` unique per content hash, and `rules/snapshot.py` already carries version on the snapshot record.
source: ADR-0006 (Effective version by semver) · rules/snapshot.py (`SnapshotRecord.version`, `compute_snapshot_id` → `sha256:…`, `DuplicateVersion`)
affects: #198

### D6. The `DESIGN.md §3.12` citation for "the four resolver keys"
status: DECIDED
decision: Wrong reference — repoint it to `DESIGN.md §3.13` (the resolver, ADR-0007), with ADR-0006's "full picture" for the key list.
because: §3.12 is titled "Project scope (ADR-0006)" and covers only project scope; the resolver interface is §3.13.
source: docs/DESIGN.md §3.12 (title "Project scope (ADR-0006)") vs §3.13 ("rules/applicability.py … ADR-0007")
affects: #198 (acceptance-criterion citation)

---

**Unchanged from the plan (correct as written):** `snapshot_id` stored in the `sha256:…` form `compute_snapshot_id` emits; `canonical_json` stored alongside so the hash is re-derivable; a round-trip test that rehashes the stored column. Net scope shape: `product_type` (enum scalar) + the discriminator child table, and nothing else.
