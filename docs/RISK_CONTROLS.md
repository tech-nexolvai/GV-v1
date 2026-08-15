# RISK_CONTROLS — the ten risks, their controls, and what enforces them

**Source:** system design §16 (key risks and controls) · **Design:** `docs/DESIGN_CONTROLS.md` §2.5
**Guard:** `tests/test_risk_controls.py` — this file is parsed and checked on every build.

The architecture names ten risks and pairs each with a control. This table records **what actually
enforces each one today**, in a form a test can verify, so a control cannot quietly go missing and a
half-built one cannot pass for finished.

Following ISO 14971, implementation and effectiveness are separate claims. Implementation is checked
mechanically — the artifact is on disk or it is not. Effectiveness is a stated claim naming the test
that would fail if the risk materialised; nothing automates that judgement, and pretending otherwise
would be worse than saying so.

## How to read a row

`Status` is one of:

- **ENFORCED** — every reference resolves. The control exists.
- **PARTIAL** — some references resolve and some do not. **The dangerous state:** part of the control
  is built, and it is easy to assume the rest came with it.
- **PLANNED** — nothing built yet. The owning issue is named.

`Refs` uses a closed syntax so each claim is checkable. Prose like *"enforced by B4"* is not a
reference — `B4` is an epic, not an artifact.

| Prefix | Resolves when |
|---|---|
| `test:path` | the file exists |
| `test:path::name` | the test is **collectable** by pytest, not merely present as text |
| `semgrep:id` | the rule id appears in `.semgrep/gv-rules.yaml` |
| `module:path` | the path is a **file** — a directory proves nothing |
| `cases:path` | the directory holds at least one non-hidden file |

The last two are narrow for a reason. On this guard's first run, `module:eval/gold_set/cases`
resolved — a directory containing nothing but a `.gitkeep`. An empty directory standing in as
evidence of a gold set is the exact false positive the table exists to prevent, so `module:` means a
file and `cases:` requires real content.

<!-- RISK TABLE START -->

## R1 — Numerically plausible but incorrect extraction

**Control (§16):** Independent verification, evidence polygons and strict eligibility states.
**Status:** ENFORCED
**Refs:** module:evidence/corroborate.py, module:evidence/gate.py, module:evidence/polygon.py
**Owner:** #120, #121, #170
**Effectiveness claim:** two independent readers disagreeing produces `CONFLICTING` and never a
chosen winner; `tests/evidence/test_corroborate.py` will fail if confidence is ever allowed to break
the tie.

## R2 — Wrong item/view association

**Control (§16):** Exact identifiers first, metadata and geometry checks, human confirmation for ambiguity.
**Status:** PARTIAL
**Refs:** test:tests/test_vendor_neutrality.py, module:extraction/model/assembly.py, module:extraction/geometry/text_association.py, module:retrieval/approval.py
**Owner:** #168, #180, #190
**Effectiveness claim:** an ambiguous association returns no association rather than the nearest
guess, and a partial assembly run is never silently summed. This is the failure that produces a
finding which is internally consistent, fully traced and completely wrong.

## R3 — VLM hallucination

**Control (§16):** Crop-bounded structured outputs; the VLM cannot create verdict authority.
**Status:** PARTIAL
**Refs:** test:tests/test_verdict_isolation.py, module:extraction/models/validation.py, module:extraction/models/context.py
**Owner:** #250, #252
**Effectiveness claim:** the isolation guard already makes model output unreachable from `verdict/`
transitively, so a hallucinated value cannot reach a decision. What is missing is the input side —
crop-bounded context and strict payload validation that rejects unknown fields.

## R4 — Agent loops or cost runaway

**Control (§16):** Maximum steps, call budget and mandatory abstention.
**Status:** PLANNED
**Refs:** module:extraction/agent/graph.py, module:app/budget/ceiling.py, module:app/budget/overflow.py
**Owner:** #244, #264, #265
**Effectiveness claim:** exceeding any bound produces abstention rather than a partial answer, and
budget overflow becomes REVIEW REQUIRED rather than a verdict on the regions that happened to finish.

## R5 — False PASS

**Control (§16):** Primary metric, check-type release gates, and no inference-based tolerance decisions.
**Status:** PARTIAL
**Refs:** module:eval/metrics.py, module:eval/release_gates.py, semgrep:gv-no-default-tolerance, cases:eval/gold_set/cases
**Owner:** #274, #188
**Effectiveness claim:** `critical_false_pass_rate` returns `None` rather than `0` when nothing was
measured, so an unmeasured gate cannot read as a passing one. The metric and the gate runner both
exist; **there is no gold set to run them against**, so the primary metric currently reports NOT
MEASURED for every check.

## R6 — Rule injection or unsafe expressions

**Control (§16):** Typed operation registry; no `eval`.
**Status:** ENFORCED
**Refs:** module:verdict/registry.py, test:tests/test_verdict_isolation.py, semgrep:gv-no-nondeterminism-in-verdict, semgrep:gv-no-float-in-decision-path
**Owner:** #47
**Effectiveness claim:** operations resolve through a typed registry with arity and type validation,
and the isolation guard fails the build on any `eval(` in `verdict/`. A rule cannot carry executable
text because there is no code path that would execute it.

## R7 — Retrieval contaminates truth

**Control (§16):** Authority class enforced; the verdict process has no retrieval access.
**Status:** ENFORCED
**Refs:** test:tests/test_verdict_isolation.py, module:verdict/operands.py
**Owner:** #36
**Effectiveness claim:** the guard walks the transitive import graph, so `verdict/` cannot reach
`retrieval/` even indirectly. `VerdictOperand` admits only `CORROBORATED` and `HUMAN_CONFIRMED`, and
retrieval can produce neither.

## R8 — Revision confusion

**Control (§16):** Immutable document hashes and revision-aware source selection.
**Status:** PLANNED
**Refs:** module:storage/hashing.py, module:extraction/revision.py, module:extraction/supersession.py
**Owner:** #219, #183, #184, #185
**Effectiveness claim:** unresolved supersession produces REVIEW REQUIRED for every finding drawn
from the sheet, with no tie-break. Neither half is built: **`storage/` does not exist**, contrary to
the claim in #157's own body that C5 had shipped hashes.

## R9 — Licensing surprise

**Control (§16):** Approved dependency list and model-weight licence review.
**Status:** ENFORCED
**Refs:** test:tests/test_licences.py, semgrep:gv-no-agpl-import
**Owner:** #33
**Effectiveness claim:** the test reads the metadata of everything actually installed, so a
transitive AGPL dependency is caught and not merely one named in `pyproject.toml`. PyMuPDF and
Ultralytics YOLO — the two most tempting libraries in this domain — are both AGPL and both rejected.

## R10 — Corrections silently become rules

**Control (§16):** Append-only ledger, human proposal gate and full regression.
**Status:** PLANNED
**Refs:** module:app/review/ledger.py, module:app/review/proposal.py, module:rules/governance/regression.py
**Owner:** #233, #235, #238
**Effectiveness claim:** no automated path exists from the correction ledger to a rule change, and
publication blocks on a critical false-PASS regression with no override. The proposal gate requires
a human to raise the change.

<!-- RISK TABLE END -->

## What this table says today

Three of ten risks are enforced: rule injection, retrieval contamination and licensing. All three are
properties of the deterministic core, which is the part that is built.

Two are partial. **R5 (false PASS) is the one to look at** — the metric and the release gates both
exist, and there is no gold set to run them against, so the project's primary safety metric currently
reports NOT MEASURED for every check. That is not a bug in the metric; it is `#274` and `#188`
waiting on the client.

Five are planned, and they cluster in `evidence/`, `extraction/`, `retrieval/` and `storage/` — the
four packages that are still empty.

A reader should take from this that the deterministic core is defensible and the extraction pipeline
is not yet, which is the true position and the reason the build order is what it is.
