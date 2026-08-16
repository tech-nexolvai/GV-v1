# BUILD_ORDER — dependency and readiness decisions

Authoritative rulings on what `requires:` means, the review-cluster build order, and the
correction-rate metric. Supersedes scattered reasoning. Grounded only in shipped docs, ADRs, the
gate, and the seven issue contracts; anything the project has not settled is marked `OPEN`.

Last updated 2026-08-17.

Two entries carry amendments, marked inline and dated. The rulings themselves stand; both
amendments extend a ruling's reach rather than reversing it. They exist because the question that
produced this file named seven issues and asked for their order, so the analysis stopped at that
boundary — and the boundary was drawn one layer too shallow. The amendments are kept visible rather
than folded in, because the way this file was scoped wrong is the same failure it was written to
prevent, and that is worth being able to see.

---

### D1. What belongs in `requires:` — merged vs design-settled, direct vs transitive?
status: DECIDED
decision: `requires:` holds only **direct** dependencies that are **decisions** (D-series ADRs) or **client answers** (Q-series), and they block until *settled* — an accepted ADR or a recorded `CLIENT_FACTS.md` answer — never until a code PR merges. Consequence you may not have noticed: the gate enforces only `Dn`/`Qn` entries plus the `status:` and `design:` fields — an **issue number in `requires:` is printed but never checked** — so a code/build-order dependency is held by `status: deferred`, not by `requires:`; #236 passed because its `status` was `ready`, and editing only its `requires:` would not have stopped it.
because: CONTRIBUTING.md defines the field as "decisions or client answers that must land first," and `issue_gate.py` resolves `Qn` against `CLIENT_FACTS.md` and keys readiness off `status:`/`design:` — it never fetches a required issue to see whether it is closed, and it computes no transitive closure.
source: CONTRIBUTING.md (contract block + comment) · scripts/issue_gate.py (`client_fact_verdict` matches `\bQ\d+\b` only; digit entries are display-formatted) · the `deferred` entry in the gate's `STATUS` table
affects: scripts/issue_gate.py, CONTRIBUTING.md, every agent contract; the cluster in D2

### D2. Correct build order for the review cluster, and the `requires:` each should carry
status: DECIDED
decision: Order: **#200 → {#202, #234} → #233 → {#230, #235} → #236**. Contracts should read — **#200** `status: ready`, `requires: []` (root: the review + `correction_ledger` *tables*, `app/models/review.py`). **#202** `status: deferred`, `requires: [#200]` (DB append-only over those tables; plus the earlier immutable-table stories outside this cluster). **#234** `status: deferred`, `requires: [#200]` (exceptions only; independent of the ledger). **#233** `status: deferred`, `requires: [#200, #202]` (ledger write/query *logic*; its own criterion cites C1.12). **#230** `status: deferred`, `requires: [#233, #200]` (a correction writes the ledger in the same transaction). **#235** `status: deferred`, `requires: [#233]` (reads ledger corrections). **#236** `status: deferred`, `requires: [#233, #200, #230]` (ledger, plus the `review_action` and `observation` FKs it joins for per-extractor attribution). Consequence you may not have noticed: this is not one bad contract — **six of the seven declare `status: ready, requires: []`**, so the gate reports READY for all six; only #200 is genuinely ready.
because: each downstream contract's own Scope/Acceptance names the artifact it consumes (#230 "writes to the correction ledger (D5.1)"; #233 "append-only … (C1.12)"; #236 "computed from the ledger" + per-extractor), and #200 is the only one whose inputs already exist on `main`; note #200 owns the ledger *table* while #233 owns the ledger *logic*, so their boundary must stay explicit or they collide.
source: the seven GitHub agent contracts (scope + acceptance) · docs/DESIGN_PRODUCT.md §4 · docs/DESIGN_PLATFORM.md §3.3 (immutable-table list) · AGENTS.md §8 (correction ledger is Phase 7)
affects: #195, #198, #199, #200, #202, #230, #233, #234, #235, #236

> **Amended 2026-08-17 — the cluster is nine issues, not seven, and #200 is not a root.**
> #200's correction pins "the finding revision it applies to", and no issue in the seven creates a
> findings table. **#199** (C1.9 — `check_runs`, `verdict_inputs`, `findings`, `finding_evidence`)
> does. #199 in turn stores the rule **snapshot** id, so it needs **#198** (C1.8), and requires that
> "every finding links to the evidence that produced it", so it needs **#195** (C1.5 —
> `canonical_observations`, `evidence_artifacts`), which is also the `observation` FK #236 joins for
> per-extractor attribution.
>
> Order: **{#195, #198} → #199 → #200 → {#202, #234} → #233 → {#230, #235} → #236**.
> Contracts: **#195** `ready`, `requires: []`. **#198** `ready`, `requires: []`. **#199** `deferred`,
> `requires: [195, 198]`. **#200** `deferred`, `requires: [199]`. Everything from #202 onward keeps
> the ruling's dependencies, with #200's own now transitively real.
>
> The genuinely ready roots are **#195 and #198**, not #200. Seven of the nine are wrongly `ready`.

### D3. Was D5.4 ever intended to be buildable before D5.1?
status: DECIDED
decision: No — #236 (D5.4) is strictly downstream of the ledger #233 (D5.1); it may not be built against a stub or protocol-typed ledger, because its per-extractor attribution needs the ledger's real join fields (`review_action` from #200, `observation` from #230). Consequence you may not have noticed: eval/metrics.py and docs/DESIGN.md label the correction *ledger* as "D5.4" — the ledger is D5.1/#233 and D5.4/#236 is the metric; fix that mislabel or it re-confuses the next reader.
because: #236's own acceptance requires the metric be "computed from the ledger, never from a separate tally that could drift," and eval/metrics.py already ships it as NOT MEASURED, noted "needs the correction ledger."
source: #236 acceptance criteria · eval/metrics.py (`review_derived_placeholders`, `reviewer_correction_rate` note) · docs/DESIGN.md §"Two of the nine cannot be computed from a gold set"
affects: #236, #233; eval/metrics.py, docs/DESIGN.md

### D4. Does the ledger store the extractor version at correction time, or derive it by join?
status: DECIDED
decision: Neither as posed — a correction pins the **immutable finding revision it applies to**, and the extractor version is read from *that pinned revision*, which is "what the reviewer saw" and cannot drift, because a re-run produces a new revision and never mutates the old one. Consequence you may not have noticed: do **not** also copy the extractor-version string into the ledger row — that is a second copy that can disagree with the revision, the same "separate tally that could drift" #236 already forbids, one level down.
because: #200 and #233 both pin a correction to "the finding revision it applies to," and golden-rule 7 makes every revision immutable, so joining through the pin yields the reviewer-time fact without denormalising it.
source: #200 + #233 acceptance ("against which finding revision") · AGENTS.md §2 (golden rule 7, versioned & immutable) · docs/DESIGN_PRODUCT.md §3.1 (`FindingChain` immutable & recomputable)
affects: #195, #199, #233, #230, #236; app/review/ledger.py

> **Amended 2026-08-17 — this ruling binds #199, not #233.**
> The reasoning holds and the "do not denormalise the version" consequence stands. But the pin it
> relies on lands on a table that does not exist and is not scoped where the ruling assumes:
> `verdict/finding.py:Finding` is a pure dataclass inside the verdict path, which may never import
> DB or extraction code, and #200's scope lists no findings table. **#199** owns `findings`.
>
> So the enforceable requirement is on #199: a finding must be able to reach the extractor version
> that produced its evidence — via its check run, or via `finding_evidence` to the #195 observation
> and its extraction run. #199 already says "a re-run produces a new finding linked to a new check
> run", which is exactly the immutability this ruling depends on; what is not yet stated is that the
> extractor version must be reachable from it.
>
> If #199 ships without that path, #236 becomes unbuildable a second time, six issues later, for the
> same reason — which is the failure this whole file exists to stop.

### D5. What is the denominator of "correction rate"?
status: OPEN
because: it is a definitional choice with three incompatible meanings — corrections ÷ findings-acted-on, ÷ findings-presented, or ÷ observations-extracted — and a release metric cannot inherit its denominator from whoever writes the code first.
source: not stated anywhere — #236 fixes the numerator ("from the ledger") and the grouping ("per check type, per extractor") but never the denominator; DESIGN_CONTROLS.md §3.2 and eval/metrics.py leave it unspecified. Settled by an admin ADR (or an amendment to #236's acceptance) naming one base; for report parity it should be a count over the same set the sibling rates use (e.g. `critical_false_pass_rate`'s denominator is critical checks the gold set expects to FAIL).
affects: #236; eval/correction_rate.py; DESIGN_CONTROLS.md §3.2

### D6a. `implements:` path collision — `eval/metrics/correction_rate.py`
status: DECIDED
decision: Use `eval/correction_rate.py` — a peer module beside `eval/metrics.py`, matching `eval/localisation.py`; #236's `implements: eval/metrics/correction_rate.py` is wrong and must be changed.
because: `eval/metrics.py` is a module, so a package `eval/metrics/` would shadow it and break every `from eval.metrics import …` (e.g. `eval/release_gates.py`), and `eval/metrics.py` already delegates its heavy metric to the sibling `eval/localisation.py`.
source: filesystem (`eval/metrics.py` is a file; `eval/localisation.py` exists) · eval/metrics.py (`from eval.localisation import …`) · ADR-0002 (module layout)
affects: #236 (`implements:` field), eval/correction_rate.py, tests/eval/test_correction_rate.py

### D6b. Return type — `Decimal` vs `Fraction`
status: DECIDED
decision: `Fraction`. #236's plan returning `Decimal` is wrong.
because: `MetricResult.value` is typed `Fraction | None` and `_rate()` constructs `Fraction`, so a `Decimal` fails strict mypy and would put a rounded ratio behind a release gate; a ratio of two counts is exactly a rational.
source: eval/metrics.py (`MetricResult.value: Fraction | None`, `_rate`) · docs/DESIGN.md §"Three rules this module exists to hold" ("Values are exact rationals. `Fraction`, never float") · ADR-0001
affects: #236, eval/correction_rate.py

### D7. A general rule so a consumer declares its producer
status: DECIDED
decision: Add to CONTRIBUTING.md, in the contract section: *"If a story consumes a table, type, module or file that another open issue is responsible for creating, name that issue in `requires:` **and** set `status: deferred` until that issue is merged — the gate prints an issue number in `requires:` but does not enforce it, so `status:` is what actually holds a build-order dependency."*
because: the failure was a consuming story declaring `status: ready` with an empty `requires:`, and only `status:` is machine-checked for build order — so the rule must bind the status field, not `requires:` alone.
source: scripts/issue_gate.py (readiness keys off `status:`/`design:`/`Qn` only) · CONTRIBUTING.md (contract definition). A stronger, philosophy-consistent enforcement — DESIGN_CONTROLS.md §2.5, "a file on disk is a fact, a closed issue is a claim" — would gate a consumer on the *existence of the producer's `implements:` artifact* rather than on issue state; that is a gate change, noted, not decided here.
affects: CONTRIBUTING.md; every future agent contract
