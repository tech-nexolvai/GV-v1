# DESIGN — cross-cutting controls (Track F)

Companion to `DESIGN.md`. Owns **F1–F6**: security and audit, observability and release metrics,
evaluation persistence, budget ceilings, and the measured upgrade triggers.

These are the controls named in the architecture's risk register. Each is enforced by network, IAM,
database roles, grants or immutable records — **never by prompt instructions**.

---

## 1. Package layout

```
app/audit/      events.py
app/db/         roles.py
app/retention/  __init__.py policy.py
app/telemetry/  tracing.py metrics.py triggers.py threshold_report.py
app/budget/     ceiling.py overflow.py attribution.py
eval/           release_metrics.py runs.py regression.py
storage/        urls.py
deploy/verdict_isolation/
```

---

## 2. F1 — security, privacy and audit

### 2.1 Database roles

| Role | Grants |
|---|---|
| `gv_app` | read/write business tables; no UPDATE/DELETE on immutable ones (C1.12) |
| `gv_worker` | as above, plus extraction and model tables |
| `gv_report` | **read-only**, enforced at the database, not in application code |
| `gv_verdict` | reads only what a verdict needs. **No model or retrieval tables** |

The import guard (`tests/test_verdict_isolation.py`) enforces the static half of verdict isolation;
these grants and the runtime container enforce the other half. Static isolation proves the verdict
*cannot import* retrieval; grants prove it *cannot read* it even if something else hands it a session.

### 2.2 Audit events

Six categories, each with actor, timestamp and trace id: state changes, rule publication, findings,
review actions, exceptions, artifact downloads.

```python
def emit(session, *, category: AuditCategory, actor: str, target: UUID, trace_id: str) -> None:
    """Writes in the caller's transaction. An unaudited state change must not happen."""
```

`tests/audit/test_events.py` enumerates the six categories and fails if one emits nothing.

### 2.3 Verdict runtime isolation

No egress except the database; no model or retrieval secrets in the process environment. A verdict
process that finds itself with network access **fails to start** rather than running degraded.

### 2.4 Retention

Periods per artifact class, applied automatically. Deletion is an audit event — nothing disappears
without a record. Legal-hold and Object-Lock content is exempt.

`AGENTS.md` §6: full drawings and sensitive crops are **never logged** — references and hashes only.

---

## 3. F2 — observability and release metrics

### 3.1 One trace

`package → workflow → task → model call → finding`, carrying `package_id`, `document_version_id`,
`workflow_run_id`, `task_run_id`, page/region, extractor version and rule snapshot. Trace context
survives the Hatchet boundary — a workflow task is not a separate story.

### 3.2 The nine metrics, in order

1. **critical false-PASS rate** ← the primary metric
2. evidence localisation
3. numeric and unit accuracy
4. identifier match precision
5. FAIL recall
6. abstention recall
7. reviewer correction rate
8. reviewer minutes
9. automation coverage

> *"Optimise reviewer minutes and automation coverage **only after** false-PASS, evidence localisation,
> numeric/unit accuracy and match precision meet their release gates."*

The report **structurally refuses** to present metrics 8–9 while any of 1–4 is failing:

```python
def render(results: MetricResults) -> str:
    """Raises MetricsOutOfOrder if a lower-priority metric would be shown above a failing gate."""
```

Automation coverage is the metric a stakeholder will ask about and the one most easily improved by
abstaining less. Presenting it above an unmet safety gate would actively mislead, so the report
structure prevents it rather than trusting the author.

---

## 4. F4 — evaluation persistence

A metric printed to a terminal cannot answer *"was this better or worse than last week, and what
changed?"*. Every run stores the code version, rule snapshots, extractor versions and gold-set version —
everything that could explain a difference.

```python
def compare(run: EvaluationRun, baseline: EvaluationRun) -> RegressionReport:
    """Reports cases that got worse AND better. Attributes the difference to the version that changed."""
```

**Comparison against an absent baseline is refused**, never scored as a pass. D6.2 consumes this output
to block a publish.

---

## 5. F5 — budget ceilings

E1's limits are **per ambiguous region**. A package with two hundred regions respects every one of them
and still runs away. The ceiling here is **per package**.

```python
@dataclass(frozen=True, slots=True)
class Ceiling:
    max_model_calls: int
    max_tokens: int          # resolved GLOBAL -> PROJECT -> RUN like every other parameter (A7)
```

Overflow is a **verdict outcome**, not an error page:

```python
def on_overflow(package_revision_id: UUID) -> Outcome:
    return Outcome.REVIEW_REQUIRED     # the only branch
```

A reviewer reading "REVIEW REQUIRED" assumes the drawing was hard. If the real reason was that we
stopped paying, that is a different decision for them to make — and only the trace can tell them, so the
trace says *"budget exhausted after N regions"*.

Judging a package on the regions that happened to finish, while it looks complete, is a false-PASS path.
That is why this sits with the guardrails that fail closed rather than with cost reporting.

Cost is stored as integer minor units (`cost_micros`) — never a float.

---

## 6. F6 — measured upgrade triggers

Both architecture documents defer Temporal, Qdrant, OpenSearch, GraphRAG, MCP, managed Postgres and a
self-hosted VLM. In each case they are careful not to say *never*: they give a **measured trigger**.

| Measured quantity | Source | Upgrade it gates |
|---|---|---|
| concurrent packages, worker queue depth | C4.3, F2.2 | separate worker pools |
| database availability, recovery events | F2.2 | RDS with PITR |
| workflow recovery interventions | C4.5 | Temporal |
| pgvector latency vs transactional load | B9.4 | dedicated vector service |
| BM25 corpus size and latency | B9.3 | OpenSearch |
| managed VLM spend vs GPU-hour baseline | F5.3 | self-hosted VLM |

**"Not measured" is displayed as prominently as a breach** — an unmeasured trigger is the real risk,
because it makes the deferral permanent by accident.

A crossed threshold drafts an **ADR**, not a ticket: adopting Temporal is an architecture decision and
belongs in the same record as every other one. Only the admin ratifies (`scripts/ratify.py`). Rejecting
an upgrade is a recorded outcome, so the same argument is not re-run in six months.

---

## 7. Testing convention

Per `DESIGN.md` §4, plus:

- **Grant tests** run against a live database — a grant asserted in a mock proves nothing.
- **Enumeration tests** for anything that must hold across a whole surface: six audit categories, every
  route, every immutable table, every trigger.
- **Refusal tests**: no-baseline comparison, out-of-order metrics, budget overflow, unversioned bucket.
- **Adversarial suite** for F1.5, kept as a corpus that grows when a new injection shape is seen.
