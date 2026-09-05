# Dynamic rulebook — what that already means here, and what it does not

Abhishek asked on the 2026-08-25 call that the rulebook be **"dynamic — project to project, product
to product, vendor-to-vendor… not hard-coded."** Raj answered that **"to some extent it has to be
hard-coded."**

Both are right, and the architecture already draws the line between them. This records where that
line falls, so the requirement is not re-argued from memory and not over-built. Nothing here is a new
decision: every seam below already exists and is exercised by a test. Last updated 2026-09-05.

**The invariant that constrains every answer.** Dynamic here means *data-driven configuration*.
Values, applicability and rule text are data; the arithmetic that turns them into a verdict is Python
and is fixed. No seam in this document lets configuration change *how* a comparison is made, and none
lets a model supply a value — `rules/parameters.py` restricts a parameter's provenance to a closed set
with no member a model could claim. "Dynamic" never becomes "the model decides".

---

### D1. Per-project and per-review VALUES — dynamic today
status: EXISTS
decision: The same published rule reaches different verdicts for different projects, because the numbers it compares come from a layered parameter set — GLOBAL, then PROJECT, then RUN — and not from the rule. A reviewer entering a cabinet depth for one job changes what that job is held to, and changes nothing for any other job. RUN exists as a third layer because a field measurement is true of the day somebody took it; recording it as a project setting would make a stale dimension authoritative on the next review.
because: This is the substance of Abhishek's "project to project". It is already what Level 1.5 uses: `app/api/measurements.py` writes a reviewer's entries into the PROJECT and RUN layers with `Provenance.MEASURED`, and `workflow/stages.py:run_checks` resolves them beneath the rulebook's own declared defaults.
proof: `tests/rules/test_dynamic_rulebook.py:test_the_same_rule_gives_different_verdicts_in_two_projects` — one rule, one shop reading of 25 1/2", PASS for a project with a 1 1/2" overhang and FAIL for one with 1". `test_neither_project_changed_the_rule` asserts both used the same snapshot id, which is what makes it a statement about configuration rather than about two different rules.
source: `rules/parameters.py` (LAYER_PRECEDENCE, `resolve_all`) · `tests/rules/test_parameter_layering.py` · `tests/rules/test_project_scope.py`

### D2. Per-product and per-layout APPLICABILITY — dynamic today
status: EXISTS
decision: A rule applies conditionally, and which variant applies is stated per package rather than coded. `CT-WIDTH-001` adds two field cuts on a three-wall run and none against a back wall; `CAB-FILLER-001` branches on filler symmetry. Adding a layout is a variant in YAML, not a change to Python.
because: This is Abhishek's "product to product". `product_type` selects which rules are candidates at all, and a discriminator selects the variant within one.
proof: `tests/rules/test_dynamic_rulebook.py:test_the_layout_changes_the_verdict_without_touching_the_rule` carries **one published snapshot** through two layouts to two different verdicts. `tests/rules/test_ct1_width.py` covers the field-cut arithmetic per layout and `tests/rules/test_applicability.py` covers resolution, including that an unstated layout abstains rather than being guessed.
source: `rules/applicability.py` · `rules/schema.py` (`Applicability`, `ApplicabilityVariant`) · ADR-0007

### D3. Versioned PUBLISH — dynamic today
status: EXISTS
decision: A rule can be added or changed and published at runtime, as a new immutable snapshot, through an approval gate. A snapshot is content-addressed, so an edited rule is a *different* snapshot rather than the same one quietly meaning something new, and a finding cites the exact text that judged it.
because: Without this, "dynamic" would mean a rulebook that changed underneath findings already recorded — every past verdict would become unexplainable. The gate is what keeps an unconfirmed tolerance out of production while still allowing it to be published to development.
proof: `tests/rules/test_dynamic_rulebook.py:test_a_republished_rule_is_a_new_snapshot_and_the_old_one_still_decides` · `tests/rules/governance/test_production_gate.py` (an unconfirmed tolerance cannot reach production; the approval records which boundary it was for)
source: `rules/snapshot.py:publish` · `rules/governance/` · `app/api/rules.py:publish_rule`

### D4. Vendor LABEL variation — the seam exists, unbuilt above it
status: EXISTS (seam) / DEFERRED (use)
decision: Vendor-specific printed labels map to GV's canonical terms through the `aliases` table — "Cab." meaning cabinet. Rows are immutable, carry who added the spelling and why, and are versioned against the rulebook so a past decision can be replayed with the alias table as it stood.
because: This is the *only* vendor-shaped variation the architecture accepts, and it is deliberately about **reading**, not judging. A vendor may spell a thing differently; a vendor may not be held to a different standard. The table is built and the layer that would populate it — extraction, matching — is not, so there is nothing to wire yet.
source: `app/models/drawing.py:Alias` (migration 0009) · `app/models/drawing.py:ItemIdentifier`

---

## What is deliberately NOT dynamic

### D5. Rules do not vary by vendor
status: DECIDED — and enforced
decision: No rule, variant or parameter may be selected by vendor. `rules/schema.py` refuses `vendor`, `vendor_id`, `vendor_name` and `submitter` as discriminator names outright, and `tests/test_vendor_neutrality.py` walks the imports to prove the deciding packages cannot even reach vendor reporting.
because: ADR-0006 is explicit — *"Every vendor is held to the same rule for the same layout. Rules are GV's own standards, so selecting a rule set by vendor would mean holding one vendor to a different standard than another — which is not what the client asked for and would be difficult to defend."* Vendor identity is metadata: it identifies the project and feeds error-pattern reporting.
note: A one-off exception is a reviewer-approved note on a finding, not a vendor rule set.
source: ADR-0006 ("Vendor is metadata, never a rule key") · ADR-0005 · `rules/schema.py` · `tests/test_vendor_neutrality.py`

### D6. Should vendor change the RULES, or only the labels?
status: **BLOCKED — DECISION OWED**
owed by: Abhishek and Raj, together
question: Abhishek asked for "vendor-to-vendor" dynamism. The architecture currently reads that as **labels**, per D4, and refuses it as **rules**, per D5 and ADR-0006. Those are different products. If GV genuinely intends to hold different vendors to different standards — a looser filler maximum for one fabricator, say — that reverses a ratified ADR and needs saying out loud, with the reason it is defensible to the vendor being held to the stricter one.
not implemented either way, deliberately: building vendor-conditional rules would quietly reverse ADR-0006; building a guard against something already guarded would be theatre. The refusal in `rules/schema.py` stands until the decision changes it.
what would change if the answer is "rules": ADR-0006's vendor section, the discriminator refusal, and the neutrality test — a coordinated change, not a configuration flag.

---

## What is deferred, and why

### D7. An in-app rule-authoring UI
status: DEFERRED — not started, deliberately
because: Rules are authored as YAML and published through the existing gate, which is enough for a rulebook of eight rules that is **not final**. `docs/decisions/CAB_CHECKS_FORMAT.md` records Raj's own format arriving on 2026-09-04 with four questions still open; `CLIENT_FACTS` Q20 has the countertop vocabulary provisional and Q21 has the filler maximum at two different numbers. An editor built now would encode today's shape of a rulebook that is still moving, and the first thing it would need is the decision in D6.
when: after the rulebook stabilises and after somebody other than a developer needs to change one.

### D8. A knowledge base behind the rules
status: DEFERRED
because: The same reason, one layer up. A knowledge base is worth building when there is a body of settled rules to hold; there are eight rules, one of which (`CAB-ARCH-VS-SHOP-001`) cannot decide at all — its tolerance is literally the string `UNCONFIRMED`, because the client has not supplied it (Q2) — and a second (`CAB-FILLER-001`) carries a default the client has since contradicted (Q21: 2" by email, 3–4" on the call).

---

## The honest summary

**Dynamic today, and demonstrated by a verdict:** the numbers a rule compares (per project, per
review), which variant of a rule applies (per product, per layout), and the rule text itself (added or
changed at runtime, versioned, approval-gated). A reviewer can point the same rule at two projects and
get two different answers without anybody touching Python.

**Hard-coded on purpose, as Raj said it must be:** the arithmetic, and the standard every vendor is
held to.

**Not built, and correctly so:** an authoring UI and a knowledge base, both waiting on a rulebook that
is still being written.

**Owed:** D6 — whether "vendor-to-vendor" means labels or rules.
