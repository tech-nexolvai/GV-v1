# F1.6 — Verdict runtime isolation: rulings

Resolves the collision between #257 as written and the static isolation guard. Grounded only in
shipped docs, ADRs and contracts; anything unsettled is marked `OPEN` rather than guessed, because
each ruling becomes an enforced control. Last updated 2026-08-17.

---

## D1. Where does `assert_isolated()` live?
status: DECIDED
decision: In `deploy/verdict_isolation/preflight.py`, run by the container entrypoint which `exec`s the verdict process only if the check passes — so the process never starts unisolated. `verdict/startup.py` is **not** created, and the forbidden-import list stands **unchanged**: `socket` stays banned in `verdict/`, because a preflight under `deploy/` is never scanned by the guard (it walks the transitive import graph from `verdict/` only). Consequence you may not have noticed: this is a plan correction, not a design change — #257's own `implements:` field already says `deploy/verdict_isolation/`; only its Implementation-plan *Files* list contradicts that by adding `verdict/startup.py`.
because: DESIGN_CONTROLS §1 places the artifacts in `deploy/verdict_isolation/` and #257's `implements:` names it, while `tests/test_verdict_isolation.py` forbids `socket` in `verdict/` — so the egress probe can only live outside `verdict/`, exactly where the design already put it.
source: docs/DESIGN_CONTROLS.md §1 (package layout) + §2.3 · #257 contract (`implements: deploy/verdict_isolation/`) · tests/test_verdict_isolation.py (`FORBIDDEN_FOR_VERDICT` includes `socket`; walk begins at `verdict/`, never `deploy/`)
affects: #257 (remove `verdict/startup.py` from Files); deploy/verdict_isolation/preflight.py; tests/test_verdict_isolation.py (untouched)

## D2. What discharges "asserted by a runtime test, not only by configuration"?
status: DECIDED
decision: Option (a) — the preflight performs a real egress probe on every production start (the runtime assertion) plus unit tests of that logic including the refusal path; a privileged-container CI test that demonstrates kernel-level blocking is **not** required.
because: DESIGN_CONTROLS §2.5 separates verification of *implementation* (automatable — the preflight exists and its logic is unit-tested) from verification of *effectiveness* (the kernel actually blocks — a named claim, never automated here), and #257's own test plan uses a misconfigured fixture, not a privileged container.
source: docs/DESIGN_CONTROLS.md §2.5 (ISO-14971 implementation vs effectiveness) + §7 (refusal tests) · #257 test plan
affects: #257; tests/test_verdict_runtime_isolation.py; docs/RISK_CONTROLS.md (effectiveness claim wording)

## D3. How is "the database" identified in the egress allowlist?
status: DECIDED (ruled 2026-08-17; was OPEN)
decision: An explicit **IP or narrow CIDR** allowlist; a **hostname in the DB configuration is refused at startup**. The DB address is injected as an IP/CIDR by the run-config, and the egress probe targets literal IPs — **no name resolution happens anywhere in the isolated process**. If the allowlist is absent, or the DB config carries a hostname, the process refuses to start (per D4).
because: DESIGN_CONTROLS §2.3 permits "no egress except the database", and a DNS lookup is a second egress to a *different* host that a poisoned resolver could redirect — so the only reading that honours the invariant literally removes name resolution, which forces IP/CIDR pinning; this is the stricter of two defensible readings (the weaker one allows the container's internal resolver) and is chosen on fail-closed grounds, consistent with D4. Consequence you may not have noticed: `docker-compose.yml` has no network config today, so this requires `postgres` on a user-defined network with a static IP handed to the verdict service — which couples this ruling to the D6 container gap: the pinned IP cannot exist until the run-config that owns the network does.
source: docs/DESIGN_CONTROLS.md §2.3 ("no egress except the database") + §2.5 / AGENTS.md §2 (fail-closed) — ruled by the decision authority on the strict reading; not previously written in a doc.
affects: #257; deploy/verdict_isolation/preflight.py; docker-compose.yml (user-defined network + static DB IP)

## D4. If the preflight cannot determine whether egress is blocked — start or refuse?
status: DECIDED
decision: Refuse. "Cannot determine" is treated as "not isolated"; the verdict process does not start.
because: the posture is fail-closed everywhere — DESIGN_CONTROLS §2.3 "fails to start rather than running degraded", AGENTS §2 golden rules 1 and 4 (abstain under uncertainty), §2.5 "PARTIAL is the dangerous middle" — and an unverifiable safety control is a failed one, not a passed one. Consequence you may not have noticed: this needs an explicit third branch in the preflight (reachable / blocked / indeterminate→refuse), and #257's acceptance names only "network present", so amend it or an implementer folds indeterminate into "absent" and starts.
source: docs/DESIGN_CONTROLS.md §2.3 + §2.5 · AGENTS.md §2 (golden rules 1, 4)
affects: #257; deploy/verdict_isolation/preflight.py

## D5. Which environment variables count as "model and retrieval secrets"?
status: DECIDED
decision: An **allow-list** — the verdict process environment is constructed to hold only a named set (the database connection plus non-secret process essentials); everything else, including any future provider's variable, is absent by default.
because: the invariant is absolute — AGENTS §5 "Runs as its own container with **no external creds**" and §2.1 "no model credentials, no … retrieval … no outbound internet" — and only an allow-list guarantees an absolute absence; a deny-list fails open the instant a new provider adds a variable nobody enumerated. Consequence you may not have noticed: the run-config must **build** the env from the allow-list, not inherit the host env and scrub it — inherit-then-deny is precisely the failure mode this forbids.
source: AGENTS.md §5 (verdict container, "no external creds") + §2.1 · docs/DESIGN_CONTROLS.md §2.1 (`gv_verdict` "reads only what a verdict needs")
affects: #257; deploy/verdict_isolation/ (run configuration)

## D6. Does #257 own creating `deploy/` and an application container, or only the isolation layer?
status: DECIDED
decision: The isolation layer only — `deploy/verdict_isolation/` plus the verdict process's run-configuration that carries the network restriction, the env allow-list and the preflight entrypoint. It does **not** own building a general application image or a deployment platform. Consequence you may not have noticed: no story currently creates the verdict container it is meant to isolate (as of the ruling: no `deploy/` at all. `#257` has since added `deploy/verdict_isolation/` with the preflight, entrypoint and a `compose.yml` carrying the network and environment; what is still missing is any `image:` or `build:` for the verdict service, so nothing runs the preflight), so #257 has an undeclared upstream dependency — it must either scope in the minimal verdict-service run-config or be deferred on a deployment story that does not yet exist.
because: #257's `implements: deploy/verdict_isolation/` and DESIGN_CONTROLS §1 scope it to the isolation artifacts, and its acceptance is entirely isolation properties — none of it is "build the application image", which is a C4/deployment concern the repo has not created.
source: #257 (`implements:`, scope, acceptance) · docs/DESIGN_CONTROLS.md §1 · AGENTS.md §5 ("Runs as its own container"). No C4 deployment/container story or artifact exists (verified at ruling time: no `deploy/`, no Dockerfile; `#257` added the isolation artifacts but no runnable image). The container-ownership question is settled only by a deployment-track (C4) story, currently absent.
affects: #257; deploy/; docker-compose.yml

## D7. New `RISK_CONTROLS.md` row, or extend an existing one?
status: DECIDED
decision: Extend the existing verdict-isolation rows — **R7** (retrieval contamination; its control is "the verdict process has no retrieval access") primarily, and **R3** (model reaching verdict) for the secret-absence half — with a new `Refs` entry (`module:deploy/verdict_isolation/preflight.py`, `test:tests/test_verdict_runtime_isolation.py::<name>`). Do **not** add a new row: runtime isolation is the runtime half of an existing control, not a new risk. Consequence you may not have noticed: the guard only checks artifact **existence**, so once `preflight.py` lands the ref resolves and the row reads ENFORCED even if no container actually runs it (D6) — so while the container is unwired the honest status is **PARTIAL** (include a ref that does not yet resolve), and the ref must be added in the **same PR** that creates the artifact, which is exactly the wrong-status-on-merge trap that hit R8 on #309.
because: DESIGN_CONTROLS §2.1 frames static and runtime isolation as "two halves" of one control, and RISK_CONTROLS.md is risk-centric (ten risks from system-design §16, multiple refs per row), so runtime isolation strengthens R3/R7 rather than naming an eleventh risk.
source: docs/RISK_CONTROLS.md (R3/R6/R7 already reference `tests/test_verdict_isolation.py`; the ENFORCED/PARTIAL/PLANNED rules) · docs/DESIGN_CONTROLS.md §2.1 + §2.5
affects: docs/RISK_CONTROLS.md (R7, R3); tests/test_risk_controls.py
