# ADR-0002 — Module layout and a stdlib-only `units/` package

**Status:** Accepted
**Date:** 2026-08-13
**Decides:** the module architecture in `docs/DESIGN.md` §1–§2
**Deciders:** admin (AnantBisht07)

> Drafted by a coding agent. **Accepted by the admin on 2026-08-13.**
> This also promotes `docs/DESIGN.md` to Accepted.

## Context

`AGENTS.md` §5 declares the repository layout: `app/ workflow/ extraction/ evidence/ rules/
verdict/ retrieval/ reports/ eval/ frontend/`. It has **no home for exact-arithmetic
primitives**.

This surfaced concretely while writing the backlog: `tests/units/test_measurement.py` was
named as the verification target for #39, but no `units/` package exists. Nobody had decided
where the `Measurement` type lives.

It is not a cosmetic question. `Measurement` is needed by `extraction/` (parsing dimensions),
`evidence/` (normalising candidates) **and** `verdict/` (the arithmetic itself). Whatever
hosts it is imported by `verdict/`, and `AGENTS.md` §2.1 requires that the verdict path have
no route to a model, retrieval, network or database. So the hosting decision carries a safety
consequence.

## Options considered

1. **A new stdlib-only `units/` package.** Safe for `verdict/` to import precisely because it
   cannot reach a network, a model or a database. Costs one new top-level package.
2. **Inside `verdict/`.** Then `extraction/` and `evidence/` must import from `verdict/`,
   inverting the dependency direction and making the isolation guard harder to reason about —
   the guard would have to distinguish "importing verdict's utilities" from "importing the
   engine".
3. **Inside `rules/`.** `rules/` will grow YAML loading and Pydantic validation, which
   `verdict/` should not be importing.
4. **Duplicate per package.** Maximum isolation, but tolerance comparison implemented more
   than once. Two implementations that can drift is itself a false-PASS risk.

## Decision

**Option 1.** Add a `units/` package that imports **only the Python standard library**, and
adopt the import table in `docs/DESIGN.md` §2:

| Package | May import | Must never import |
|---|---|---|
| `units/` | stdlib **only** | everything else, including other project packages |
| `verdict/` | `units/`, `rules/` schema data types | extraction, retrieval, network, model SDKs, ORM |
| `rules/` | `units/`, pydantic | extraction, retrieval, network |
| `evidence/` | `units/`, `rules/` | `verdict/` internals |
| `extraction/` | `units/` | `verdict/`, `rules/` |

`units/` having zero project dependencies is the load-bearing constraint. If it ever imports
anything, `verdict/`'s isolation is silently compromised — which is why #36 asserts it.

## Consequences

`AGENTS.md` §5 gains one package. `docs/DESIGN.md` §2 becomes the specification that #36's
guard test asserts row by row, rather than a description.

The core (`units/`, `rules/`, `verdict/`) stays on the standard library, which is why
`pyproject.toml` declares **no runtime dependencies** — everything else is an optional group
installed by the phase that needs it.

## Safety impact

Indirect but structural. A shared primitive in the wrong package is how an import boundary
quietly erodes: someone adds a convenience helper, it pulls in a dependency, and the verdict
service is no longer isolated. Putting the shared code somewhere that *cannot* grow such a
dependency makes the erosion impossible rather than merely discouraged.

## Unblocks

`docs/DESIGN.md` moves to Accepted, which de-risks the 23 ready issues that build against it —
in particular **#36** (whose spec is §2), **#39–#42** (`units/`), **#47–#52** (`verdict/`) and
**#53–#56** (`rules/`).
