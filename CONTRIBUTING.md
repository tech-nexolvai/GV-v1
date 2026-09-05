# CONTRIBUTING — how to work an issue

This project is built by two people: an **admin**, who owns the plan, all architectural decisions and
all client questions; and a **dev**, who implements. Work is driven entirely by GitHub issues — the dev
should never need more than an issue number.

Coding agents (Codex, Claude Code, Cursor) must follow this document exactly. Read
[`AGENTS.md`](AGENTS.md) first — it holds the project's non-negotiable invariants.

---

## Running the stack locally

Until #416 there was no documented way to start this application at all — `make check` and `make db`
were the only targets, so the API and the durable workflow could both be built and neither could be run.

Local development only: no reverse proxy, no TLS, no cloud. #151 owns deployment.

**Once, per clone:**

```bash
make install                 # the same dependencies CI installs
cp .env.example .env         # then read the note below before trusting it
make up                      # PostgreSQL and the Hatchet engine, waits until both are healthy
make token                   # mints a client token and prints the line to add to .env
make migrate                 # brings the application database to head
```

**Every time, in three terminals:**

```bash
make serve                   # the API on :8000
make worker                  # runs the review stages
make dispatch                # drains the outbox into the engine
```

**All three are needed for a package to move, and the failure if one is missing is quiet.** Without the
dispatcher, the API accepts a package, commits the outbox row and returns success — and nothing ever
runs it. Without the worker, the engine accepts the workflow and no stage executes. Neither looks like
an error from the outside, which is why they are three named targets rather than a paragraph.

Check it is all up:

```bash
curl -s localhost:8000/health    # the process is alive
curl -s localhost:8000/ready     # 200 only when the schema is at head; 503 says run make migrate
open http://localhost:8888       # the engine dashboard: workflow runs, and why one failed
```

### What runs where

| Service | Where | Port | Notes |
|---|---|---|---|
| PostgreSQL | Compose | **5433** | Not 5432, so it cannot collide with a PostgreSQL you already run |
| Hatchet engine | Compose | 8888, 7070 | Dashboard and gRPC, on the engine's own documented ports |
| API | host | 8000 | `make serve` |
| Worker | host | — | `make worker` |
| Dispatcher | host | — | `make dispatch` |

Two logical databases on the one PostgreSQL: `gv` for the application, `hatchet` for the engine. They
are separate because the engine runs its own migrations, and sharing one database would put
`hatchet-migrate` and Alembic in charge of the same namespace.

**A note on `.env.example`:** it predates `app/config.py`'s `GV_` prefix and names `DATABASE_URL` where
the application reads `GV_DATABASE_URL`, with port 5432 where this stack uses 5433. Copying it and
expecting it to work will not work. That is #417's to fix, not this story's — until then, use the
variable names `make token` and `make migrate` print.

---

## Two sessions at once: use a worktree

Two people — or two agents — in one checkout fight over three things, and the third is expensive.

They overwrite each other's `frontend/main/.env.local`, they race on the same ports, and **they share
the test database**. The last one is not what it looks like. `tests/app/postgres_fixture.py` already
gives every test its own schema, so schemas were never the problem: `alembic/versions/0020_pg_trgm.py`
runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` **unqualified**, so it lands in whichever schema is
first on `search_path` — the per-test one — while an extension is a *database-wide* object. The second
concurrent run's `IF NOT EXISTS` finds it already present, skips, and its schema then cannot see
`gin_trgm_ops`. Every test touching a trigram index fails, the failure count is large and uniform, and
it looks exactly like a regression. Six test runs were discarded to this in one day before anybody
worked out what it was.

So the isolation has to be per **database**, and a worktree gets one:

```bash
make worktree NAME=512-my-branch     # its own checkout, database and ports
cd .claude/worktrees/512-my-branch
make where                           # what this checkout uses
make check                           # against its own test database
make demo                            # its own ports, its own data
```

`make where` in any checkout prints its database names and ports. The main checkout keeps the plain
`gv`, `gvtest`, `:8000` and `:5173` — README, `.env.example` and this file all name them, and renaming
them would make those instructions wrong. A linked worktree gets a suffix and a port offset derived
from its own name, stable across runs so a stale server never holds the port the next run wants.

Every checkout drives **one** PostgreSQL: `COMPOSE_PROJECT_NAME` is pinned, because compose otherwise
names a project after its directory and a worktree would start a second database that cannot bind 5433.

## The demo, in one command

```bash
make demo
```

Stack, database, schema, rulebook, a seeded package with one deliberate error, the API and the UI —
and it is re-runnable without cleaning anything up first. It prints the URL and the project id at the
end, and Ctrl-C stops both servers.

**The two spellings of the database URL.** `alembic` reads a bare `DATABASE_URL`; it is a separate
tool and knows nothing of this application's `GV_` prefix. The application reads `GV_DATABASE_URL`
through `Settings`. Following one instruction with the other variable set gives a server that refuses
to start and blames a setting you have just made — `make demo` exports both explicitly, side by side,
so the difference stays visible rather than becoming folklore.

`GV_DEV_PRINCIPAL`, `GV_DEV_PROJECTS` and `VITE_PROJECT_ID` are **exported for the process, never
written to a file**. Neither of the first two is a field on `Settings`, and `extra="forbid"` means a
`.env` containing them stops the API starting; `.env.local` is the file two sessions used to fight over.

## The one command

```bash
python scripts/issue_gate.py <issue-number>
```

**Run this before writing a single line of code.** The gate decides whether work may start. You do not.

| Exit code | Meaning | What to do |
|---|---|---|
| `0` | READY | Implement it. The gate printed your brief: what to read, the scope, the acceptance criteria, the Definition of Done, and the branch name. |
| `2` | BLOCKED | **Stop.** Do not write code. The reason and the blocking issues are printed. Add `--comment` to record the block on the issue. |
| `3` | ADMIN ONLY | A decision or a client question. Never yours to answer. |
| `4` | MALFORMED | The issue has no agent contract. Do not guess — ask the admin. |

Every issue carries a machine-readable contract near the top of its body:

```yaml
status: ready          # ready | needs-architecture | blocked-client | blocked-data | deferred | epic | admin-only
owner: dev
requires: []           # decisions, client answers or producing issues that must land first
read:
  - AGENTS.md
  - docs/V1_RESEARCH_AND_PLAN.md
verification: tests/units/test_measurement.py
```

That contract — not your judgement — determines readiness.

**If a story consumes a table, type, module or file that another open issue is responsible for
creating, name that issue in `requires:` *and* set `status: deferred` until that issue is merged —
the gate prints an issue number in `requires:` but does not enforce it, so `status:` is what
actually holds a build-order dependency.**

That sentence exists because a whole cluster of stories declared `status: ready` with `requires: []`
while depending on tables nothing had built yet, and the gate dutifully reported READY for every one
of them.

Only one kind of `requires:` entry is machine-checked:

| Entry | Meaning | Enforced by the gate |
|---|---|---|
| `Q5` | a client answer | **yes** — resolved against `docs/CLIENT_FACTS.md` |
| `D3` | a decision or ADR | no — held by `status: needs-architecture` |
| `199` | the issue that builds what this one consumes | no — held by `status: deferred` |

So write both fields, and understand that only `status:` stops anybody. Producing issues go in as
bare digits in a block list — `- 199`, never `- #199`, because the parser strips whitespace-`#` as a
YAML comment and the brief adds the `#` back when it prints. The full worked example is
`docs/BUILD_ORDER.md`.

---

## Claiming work: readiness vs execution state

Two different questions, tracked separately — conflating them hides both.

| Question | Answered by |
|---|---|
| *May work start?* | `status:` label + the contract (`ready`, `needs-architecture`, …) |
| *Is it being worked?* | `state:` label (`state:in-progress`, `state:in-review`), plus the assignee |

```bash
python scripts/issue_gate.py 40 --start    # claims it: state:in-progress + assigns to you
python scripts/issue_gate.py 40 --review   # PR opened: state:in-review
python scripts/issue_gate.py 40 --done     # merged and closed: no state label at all
```

`--start` only works on a READY issue — you cannot claim something blocked.

**Run `--done` after the merge.** It is the step that keeps these labels worth reading: without it
the other two are a one-way trip, and every issue the project has finished goes on claiming to be
active work. There is no `state:done` on purpose — the label answers "is this being worked?", and for
something closed the answer is no label rather than a different one. That the work finished is already
recorded, by the closed issue and the PR that closed it.

`--done` refuses on an open issue, because clearing the state of live work would make it look
unclaimed — and an unclaimed issue looks normal, so nobody would notice.

---

## Architecture comes before implementation

If the gate reports `needs-architecture`, an architectural decision (a **D-series** issue) has not been
ratified. If it reports `blocked-client`, a value the issue depends on does not exist yet — most often a
**tolerance**.

**Dev:** stop. That is the end of your involvement with that issue.

**Admin:** the decision *is* the next action, and the gate will hand you the brief:

```bash
python scripts/issue_gate.py 1 --role admin      # prints the decision brief for D1
```

### The loop that unblocks work

1. `issue_gate.py <decision-issue> --role admin` — prints context, options, recommendation, and what
   the decision unblocks
2. Draft an ADR in `docs/adr/` from `TEMPLATE.md` — **an agent may write this**
3. The **admin** sets `Status: Accepted`. Nothing else may. `ratify.py` refuses to proceed otherwise
4. Ratify, and every dependent story is rewritten automatically:

```bash
python scripts/ratify.py D1 --adr docs/adr/0001-unit-policy.md
python scripts/ratify.py Q2 --answer "1/8 inch for three-wall vanity tops"

python scripts/ratify.py D2 --adr <file> --dry-run   # preview what it would unblock
```

Ratifying rewrites each dependent story's contract to `status: ready`, swaps its labels, and comments on
it. A story with several dependencies is only released when the **last** one clears — ratifying D2 while
Q2 and Q13 remain outstanding leaves the story blocked and says so.

This is what makes "architecture first" real rather than aspirational: a blocked story cannot pass the
gate until its ADR exists and is accepted, and once it is, nobody has to remember to update anything.

Do not work around a block by picking a plausible value. That is the single most damaging thing you can
do to this codebase, and the reason is in the next section.

---

## Before you push

```bash
make check          # the CI chain, locally, in the same order
make check-fast     # same without semgrep, for a tight edit loop
```

Every CI failure on this project so far has been an **environment difference**, not a logic bug: a
dependency in an extra the job did not install, a test module that resolved locally and not on the
runner, a linter nobody ran. `scripts/check.py` closes that gap and does two things `pytest` alone
does not.

**It refuses to run in a drifted environment.** If your interpreter is missing an extra that CI
installs, it says so and stops. A green run in a stale venv is worse than a red one, because it is
believed.

**It tells you what it could not check.** The PostgreSQL tests skip without `DATABASE_URL`, and a
model/migration mismatch has already reached CI that way. To run them:

```bash
make db
export DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv
make check
```

---

## What a story must contain before it can be worked

Current spec-driven practice — GitHub Spec Kit, AWS Kiro, and the tooling that grew up around coding
agents in 2025–26 — separates three things that are easy to blur:

| Artifact | Answers | Lives in |
|---|---|---|
| **Spec** | *what* and *why* | the issue: Context, Scope, Acceptance criteria |
| **Plan** | *how* — interface, files, order of work | the issue: **Implementation plan** |
| **Design** | which module owns it, and what it may import | `docs/DESIGN*.md`, cited by `design:` |

The reason for the split is the failure mode it prevents. An issue that states only *what* leaves the
interface to whoever implements it first, and the pieces stop fitting. An issue that inlines the whole
architecture duplicates `DESIGN.md` and drifts from it. So: **architecture in the design docs, execution
order in the issue, and the `design:` field is the link between them.**

### The Implementation plan section

Every story carries one. It has five parts:

**Approach** — the ordered steps. Not a restatement of the scope; the sequence someone would actually
work in, including what must exist first.

**Interface** — the types and signatures this story adds, as real Python. This is the part that stops
two stories inventing incompatible versions of the same thing. If it contradicts the design doc, the
design doc wins and the issue is wrong.

**Files** — exact paths, marked new or changed, including the test file.

**Golden-rule check** — which of the invariants in `AGENTS.md` §2 this story can plausibly violate, and
what specifically keeps it from doing so. Adapted from Spec Kit's *Constitution Check*: the point is to
name the rule **before** writing the code, not to audit afterwards. A story that touches `verdict/`,
`rules/` or `evidence/` and claims no applicable rule is almost certainly wrong.

**Test plan** — the specific cases, failure modes first. For a safety-critical module this must include
a boundary-exact test on both sides, a missing-operand test and an ambiguity test (`DESIGN.md` §4).

### When a plan cannot honestly be written

Some stories describe work whose shape genuinely is not knowable yet — extraction internals that depend
on drawings nobody has seen. `DESIGN.md` §5 names them, and for those the Implementation plan states the
**approach and the open decisions** rather than a false interface.

That is not a lesser plan. A specification invented ahead of the evidence is worse than an honest gap,
because it looks finished.

---

## The abstention rule

> **If you need a value, tolerance, threshold or decision that is not written in the issue, stop and
> comment on the issue. Never choose one yourself.**

This product decides whether manufactured cabinetry matches an approved design. Its primary safety
metric is the **critical false-PASS rate** — a wrong PASS can be built. A guessed tolerance does not
produce an obvious bug; it produces a confident, plausible, wrong verdict that a reviewer may sign off.

The product's own core principle applies to the people and agents building it:

> The AI reads. Evidence qualifies. Deterministic Python decides. A reviewer signs off.

Missing input → abstain. Never invent. That rule binds the codebase *and* the contributor.

---

## Workflow

1. `python scripts/issue_gate.py <N>` — if it does not exit `0`, stop.
2. Read every file the gate listed, in the order given.
3. Branch from `main` using **the name the gate printed** — do not invent one.
   The convention is `<issue-number>-<what-the-issue-is-about>`, e.g.
   `56-immutable-hashed-rule-snapshots`. The number comes first so any branch is traceable to
   its issue at a glance; the internal story code (`A5.4`) is deliberately stripped because it
   tells a reader nothing.
4. Implement **only** what the issue's **Scope** states.
5. Write the test named in `verification` before or alongside the implementation.
6. Satisfy every **Definition of Done** checkbox.
7. Open a PR whose description contains `Closes #<N>`.
8. **Never delete the branch after merging.** No `--delete-branch`. Merged branches stay on the
   remote as a record of what was done and when — a merged PR alone does not preserve that.

### Scope discipline

Implement what the issue says and nothing more. If you notice a real problem outside the issue's scope,
**comment on the issue or open a new one** — do not fix it in passing. Unrequested changes in
`verdict/`, `rules/` or `evidence/` are especially unwelcome, because every change there needs its own
test and gold-set regression.

### Never edit these without the admin

- `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`
- `memory.md` and anything in `docs/`
- `rules/rulebook/` — published rules are immutable; add a new version, never edit a shipped one
- Anything under `data/` — proprietary client material, and it must never be committed

---

## Definition of Done

Every story carries this. All of it, every time.

- [ ] Unit tests written and passing; **failure modes tested, not just the happy path**
- [ ] `ruff` and `black` clean; `mypy` clean (strict in `verdict/`, `rules/`, `evidence/`)
- [ ] No new dependency without a licence check — **no AGPL** (see `AGENTS.md` §2.8)
- [ ] Gold-set run does not regress critical false-PASS (once a gold set exists)
- [ ] Plain-English docstrings and copy; no over-promising
- [ ] Traceability recorded: the issue's **Source** and **Verification** are satisfied

---

## Four mistakes that will be rejected

These are the ones a capable agent makes on this repo specifically. Each has, or will have, an
automated guard — but knowing them beats tripping them.

| Mistake | Why it is tempting | Why it is fatal here |
|---|---|---|
| Adding **PyMuPDF** or **Ultralytics YOLO** | The most popular libraries for the job | AGPL. This is a commercial product. Use `pdfplumber` / `pypdfium2` / `pikepdf`. |
| Using **`float`** for a measurement | Normal Python instinct | Reintroduces rounding into a safety-critical calculation. Use `Fraction` and `Decimal`. |
| Importing extraction, retrieval, network or model code into **`verdict/`** | Convenient and looks harmless | Breaks the one invariant the entire product rests on (`AGENTS.md` §2.1). |
| **Inventing a missing tolerance or value** | Unblocks you immediately | Produces a false PASS — the exact failure the product exists to prevent. |

---

## Traceability

Safety-critical practice (DO-178C, ISO 26262) requires every requirement to link back to its origin and
forward to its verification evidence. Each story therefore states:

- **Source** — where the requirement came from: a client workbook cell, a section of
  [`docs/V1_RESEARCH_AND_PLAN.md`](docs/V1_RESEARCH_AND_PLAN.md), or an architecture document
- **Verification** — the test file that proves it

A story is not done until both hold. If you cannot trace a requirement to its source, that is a
question for the admin, not a gap for you to fill.
