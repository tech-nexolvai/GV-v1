# DESIGN — the backend platform (Track C)

Companion to `DESIGN.md`, which owns the deterministic core. This document owns **C1–C5**: the data
model, the control-plane API, the package lifecycle, the durable workflow seam and artifact storage.

Nothing here depends on having seen a drawing. The architecture documents fix these contracts, which is
why this track is designable in full today (`DESIGN.md` §5).

---

## 1. Package layout

```
app/
  db/          base.py  session.py  roles.py
  models/      package.py document.py runs.py evidence.py drawing.py
               matching.py rules.py findings.py review.py evaluation.py
  api/         packages.py documents.py findings.py rules.py operations.py guards.py
  auth/        __init__.py dependencies.py roles.py
  lifecycle/   states.py events.py supersede.py side_states.py
  audit/       events.py
workflow/      outbox.py idempotency.py hatchet_app.py review.py retry.py durability.py
storage/       store.py local.py s3.py hashing.py pinning.py urls.py
alembic/       versions/
```

## 2. Import rules

Extends `DESIGN.md` §2, and is enforced by the same transitive import-graph test.

| Package | May import | Must never import |
|---|---|---|
| `verdict/` | `units/`, `rules/` | **anything in this document** |
| `rules/` | `units/` | `app/`, `workflow/`, `storage/` |
| `app/models/` | `units/`, `rules/`, `evidence/` | `extraction/`, `retrieval/`, `workflow/` |
| `app/api/` | `app/`, `storage/`, `workflow/` (enqueue only) | `extraction/`, `retrieval/`, OCR, rendering |
| `workflow/` | everything except `verdict/` internals | — |
| `storage/` | nothing in `app/` | — |

Two rules carry their own guard test because prose will not hold them:

- **`verdict/` gains no persistence.** Storing a finding is not deciding one. `tests/test_verdict_isolation.py` already covers this transitively and must stay green through C1.
- **`app/api/` does no heavy work.** A route module that imports rendering, OCR or extraction fails
  `tests/api/test_no_heavy_work.py` (C2.6).

---

## 3. C1 — the data model

### 3.1 Conventions, fixed before the first table

Renaming a constraint later means editing a shipped migration, which `CLAUDE.md` forbids. So these are
settled here rather than per-model.

```python
# app/db/base.py
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

class Immutable:
    """Marker mixin. C1.12 revokes UPDATE and DELETE on every table carrying it."""
```

- **Primary keys** are `UUID`, generated in Python so an object has identity before it is saved.
- **Timestamps** are `TIMESTAMP(timezone=True)`, always UTC. No naive datetime reaches the database.
- **Exact numbers** are `NUMERIC`, never `DOUBLE PRECISION`. A float column anywhere in `app/models/` is
  an ADR-0001 violation and semgrep rejects it.
- **Enums** are stored as text with a check constraint, not PostgreSQL `ENUM` — adding a value to a
  native enum inside a transaction is a known migration hazard.

### 3.2 The distinction the schema must preserve

Three pairs exist where one member is a claim and the other is a fact. **Each pair is two tables, never
one table with a status column.**

| Claim | Fact | Story |
|---|---|---|
| `observation_candidates` | `canonical_observations` | C1.5 |
| `match_candidates` | `approved_matches` | C1.7 |
| rule proposal | `rule_snapshots` | C1.8, D6 |

A single shared table with a `status` column is all it would take to lose the boundary the entire safety
argument rests on: promotion would become an `UPDATE`, and an `UPDATE` is something any code path can do.

### 3.3 Immutability

`AGENTS.md` §2.7 requires append-only, versioned data. Convention will not hold it — one ORM call is
enough. C1.12 enforces it in PostgreSQL:

```sql
REVOKE UPDATE, DELETE ON <immutable tables> FROM gv_app, gv_worker;
```

Protected: `canonical_observations`, `findings`, `verdict_inputs`, `check_runs`, `rule_snapshots`,
`package_state_events`, `correction_ledger`, `review_actions`, `approvals`, `model_invocations`,
`evaluation_runs`, `metric_results`, `audit_events`.

A re-run writes a **new row** and links to what it superseded. Nothing is edited in place, anywhere.

---

## 4. C2 — the control-plane API

### 4.1 Shape

FastAPI modular monolith, six route groups: packages, documents, findings, review, rules, operations.
The application factory lives in `app/main.py`; settings are a Pydantic `BaseSettings` validated at
startup so a missing value fails immediately rather than at first use.

### 4.2 Two boundaries, both enforced by route-enumerating tests

A guard that each author must remember is a guard that will eventually be forgotten. Both of these walk
the whole route table instead.

**No client-supplied verdict** (C2.5). Backend §10.2: *"The API never accepts a client-provided
PASS/FAIL calculation."*

```python
# app/api/guards.py
FORBIDDEN_IN_REQUESTS = (Outcome, Severity, Tolerance, Measurement)

def assert_no_verdict_fields(app: FastAPI) -> None:
    """Walk every route's request model, including nested and optional fields."""
```

**No heavy work in the control plane** (C2.6). Backend §4.1: uploads use presigned URLs and anything
CPU-heavy is a background task. On one 8 GB VM, rendering inside a request competes with PostgreSQL and
OCR for memory.

### 4.3 Authorisation

Project scope is an isolation boundary, not a filter (ADR-0006). It is applied as a shared dependency,
and `tests/api/test_authorisation.py` enumerates every route and fails on any that omits it.

Roles: `reviewer` (confirm evidence, approve packages), `rule_admin` (publish snapshots), `admin`.
A user in project A cannot read, list, or infer the existence of project B's data — including through
404-versus-403 differences.

---

## 5. C3 — the package lifecycle

```
CREATED → UPLOADING → UPLOADED → INGESTING → EXTRACTING → MATCHING
        → VALIDATING_EVIDENCE → RUNNING_CHECKS → GENERATING_OUTPUTS
        → AWAITING_REVIEW → APPROVED | CHANGES_REQUESTED
```

Side states: `FAILED_RETRYABLE`, `FAILED_PERMANENT`, `NEEDS_INPUT`, `CANCELLED`, `SUPERSEDED`.

```python
# app/lifecycle/states.py
TRANSITIONS: dict[PackageState, frozenset[PackageState]]   # data, so it can be rendered and reviewed

def transition(session, package_id, to, *, actor, reason) -> PackageStateEvent:
    """The only way a package changes state. Illegal transitions raise; nothing falls through."""
```

The value is in what is *not* allowed: a package cannot reach `AWAITING_REVIEW` without having run
checks, and cannot be approved from any side state.

### Supersede is not an edit

> *"A new document revision never overwrites an old version; it supersedes the prior package revision and
> starts a new workflow run."*

The prior revision's findings, evidence and events stay exactly as they were. This is what makes a
six-month-old review defensible: the answer to *"what did you tell us in March?"* must not be
*"whatever the system says today."*

---

## 6. C4 — the durable workflow seam

**Hatchet owns execution; PostgreSQL owns business truth.** Backend §2 is explicit that business state
must stay queryable and portable even if the workflow engine changes.

### 6.1 The transactional outbox

The dual-write problem: write package state and start a workflow, and either can fail after the other
succeeds — leaving a package with no workflow, or a workflow with no package.

```python
# workflow/outbox.py
def enqueue(session, *, workflow, payload) -> None:
    """Write the outbox row in the SAME transaction as the business change. Never starts anything."""

def dispatch_committed() -> int:
    """Poll committed rows and start workflows. At-least-once; safe because starting is idempotent."""
```

### 6.2 Idempotency

Backend §9.2 gives the key exactly:

```
document_version_id + page/region + task_type + extractor_version + config_hash
```

Stored with a unique constraint on `task_runs`, so a retry is a no-op returning the prior result. The key
must be stable across processes and restarts: no clock, no randomness, no dict iteration order.

This is what makes interrupt-and-resume safe. LangGraph interrupts restart the node, so every side effect
before an interrupt must be idempotent — paid model calls are the expensive case, half-written evidence
the dangerous one.

### 6.3 Failure policy (backend §9.4)

| Situation | Policy |
|---|---|
| Malformed PDF | one repair attempt, recorded |
| OCR disagreement | **never** auto-resolved — mark `CONFLICTING` |
| Unknown unit | route to REVIEW REQUIRED (ADR-0001) |
| Transient failure | bounded retries with backoff; exhaustion is a visible outcome |

---

## 7. C5 — artifact storage

Cloud provisioning is deferred; the artifact contract is not. Findings reference artifacts by key and
hash, so making that a boundary now means the backend can change without touching anything that reads
evidence.

```python
# storage/store.py
class ArtifactStore(Protocol):
    def put(self, key: str, data: BinaryIO, *, content_type: str) -> StoredArtifact: ...
    def get(self, key: str) -> BinaryIO: ...
    def exists(self, key: str) -> bool: ...
    def uri(self, key: str) -> str: ...

@dataclass(frozen=True, slots=True)
class StoredArtifact:
    key: str
    sha256: str
    size: int
    backend_version_id: str | None   # S3 version id; None for the local backend
```

The interface exposes no S3 concepts — a reader cannot tell which backend is in use, and `local.py` and
`s3.py` pass the identical contract test suite.

**Writing an existing key with different bytes is an error, never an overwrite.** Every artifact carries
a SHA-256 recorded in PostgreSQL, and a document version pins the exact bytes every downstream fact was
extracted from. Without that pin, an observation is a claim about "the drawing" — a file that may since
have been replaced.

---

## 8. Testing convention

`tests/<package>/test_<module>.py`, mirroring the source tree, per `DESIGN.md` §4.

Track C additions, because the failures that matter here are not unit-level:

- **Migration round-trip** — models and migrations agree; autogenerate produces an empty diff.
- **Induced-failure tests** for the outbox: kill between the business write and the dispatch, assert no
  orphan in either direction.
- **Kill-and-resume tests** for the workflow: interrupt mid-flight, assert the resumed run produces an
  identical result and repeats no paid call.
- **Route-enumerating tests** for anything that must hold across the whole API surface (C2.2, C2.5, C2.6).

A test that only covers the happy path does not satisfy the Definition of Done.
