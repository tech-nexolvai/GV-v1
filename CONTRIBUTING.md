# CONTRIBUTING — how to work an issue

This project is built by two people: an **admin**, who owns the plan, all architectural decisions and
all client questions; and a **dev**, who implements. Work is driven entirely by GitHub issues — the dev
should never need more than an issue number.

Coding agents (Codex, Claude Code, Cursor) must follow this document exactly. Read
[`AGENTS.md`](AGENTS.md) first — it holds the project's non-negotiable invariants.

---

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
requires: []           # decisions or client answers that must land first
read:
  - AGENTS.md
  - docs/V1_RESEARCH_AND_PLAN.md
verification: tests/units/test_measurement.py
```

That contract — not your judgement — determines readiness.

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
```

`--start` only works on a READY issue — you cannot claim something blocked.

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
3. Branch from `main` using the name the gate printed: `<N>-<slug>`.
4. Implement **only** what the issue's **Scope** states.
5. Write the test named in `verification` before or alongside the implementation.
6. Satisfy every **Definition of Done** checkbox.
7. Open a PR whose description contains `Closes #<N>`.

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
