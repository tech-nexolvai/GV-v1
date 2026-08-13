# DESIGN — module architecture for the deterministic core

**Status:** Accepted — ratified by ADR-0002 on 2026-08-13
**Date:** 2026-08-12
**Scope:** Track A (the deterministic core). Tracks B and C are sketched at the end.

> **Why this document exists.** The architecture PDFs describe the *system* — trust zones, data flow,
> stack. The issues describe *requirements* — scope and acceptance criteria. Neither says which module
> owns what, what the interfaces are, or who may import whom. Without that, each issue's design gets
> invented independently and the pieces do not fit. This document is the missing layer: it is the
> contract every story implements against.
>
> Every story's `## Design` section points into a section here. If a story's design is not covered
> here, it is not ready to implement — `scripts/issue_gate.py` enforces that.

---

## 1. Package layout

Adds one package to `AGENTS.md` §5: **`units/`**.

```
units/          NEW. Exact arithmetic primitives. STDLIB ONLY.
  measurement.py    Measurement — the universal numeric value type
  imperial.py       "38 3/4" -> Fraction
  dual.py           "984 [38 3/4]" -> DualDimension
  policy.py         cross-unit consistency + arithmetic-unit policy
  errors.py         typed unit/parse errors

rules/          Rule authoring, schema, applicability, vocabulary, parameters
  semantic_types.py     EXISTS. Canonical vocabulary
  schema.py             Pydantic rule models + JSON Schema
  derivations.py        the typed derivation DAG
  applicability.py      deterministic variant resolver
  snapshot.py           YAML -> canonical JSON -> content hash
  parameters.py         project parameter sets, layered resolution

verdict/        ISOLATED. Typed operations + execution. No I/O of any kind.
  registry.py       operation registry, arity/type validation
  operations/       one module per operation family
    scalar.py  aggregate.py  pairwise.py  alignment.py
  trace.py          CalculationTrace
  engine.py         execute(rule_snapshot, operands) -> Finding
  outcomes.py       Outcome, Severity

evidence/       Candidate -> canonical -> sealed operand; the Evidence Gate
extraction/     PDF repair, vector, OCR, geometry (Track B)
eval/           Gold set, metrics, release gates
```

### Why `units/` is separate

`Measurement` is needed by `extraction/`, `evidence/` **and** `verdict/`. The alternatives were all
worse: putting it in `verdict/` inverts the dependency direction and makes the isolation guard hard to
reason about; putting it in `rules/` drags YAML loading into `verdict/`'s import graph; duplicating it
per package means two implementations of a tolerance comparison, which is itself a false-PASS risk.

A standalone stdlib-only package is safe for `verdict/` to import precisely because it *cannot* reach a
network, a model or a database.

---

## 2. Import rules (enforced by the guard test, issue #36)

| Package | May import | Must never import |
|---|---|---|
| `units/` | Python stdlib **only** | everything else, including other project packages |
| `verdict/` | `units/`, `rules/` schema types (data only) | extraction, retrieval, evidence, network, boto3, ORM, filesystem beyond rule snapshots |
| `rules/` | `units/`, pydantic | extraction, retrieval, network |
| `evidence/` | `units/`, `rules/` | `verdict/` internals |
| `extraction/` | `units/` | `verdict/`, `rules/` |

`units/` having **zero** project dependencies is what makes the whole scheme hold. If `units/` ever
imports anything, `verdict/`'s isolation is silently compromised.

---

## 3. Core types

These are the contracts every Track A story implements against. Signatures are the design; docstrings
and validation are the implementer's job.

### 3.1 `units/measurement.py` — the universal numeric value

```python
class Unit(StrEnum):
    MM = "mm"
    INCH = "in"

@dataclass(frozen=True, slots=True, order=True)
class Measurement:
    """An exact dimension. Never a float, anywhere, ever."""
    exact: Fraction          # the authored value, exactly as written
    unit: Unit               # the unit it was authored in
    raw_text: str | None     # what the drawing/rule literally said
    # canonical mm is derived, not stored — see .mm

    @property
    def mm(self) -> Decimal:            # exact: Decimal("25.4") conversion
    def to(self, unit: Unit) -> "Measurement":
    def __add__ / __sub__ (self, other: "Measurement") -> "Measurement"
    # arithmetic REQUIRES matching .unit — see §3.3. Mixing raises MixedUnitError.
```

**Design rules.** Immutable, hashable, ordered. `float` appears nowhere — not in the type, not in its
operations, not in its tests. `raw_text` is never discarded: D1 requires the original token be
preserved so a reviewer can see what the drawing actually said.

*Implemented by #39. Consumed by everything.*

### 3.2 `units/imperial.py` and `units/dual.py` — parsing

```python
def parse_imperial(text: str) -> Fraction:
    """'4' | '3/4' | '38 3/4' | '2.375' | '5"' -> exact Fraction.
    Raises ImperialParseError. Never guesses, never defaults."""

@dataclass(frozen=True, slots=True)
class DualDimension:
    primary: Measurement          # as drawn (mm on GV drawings)
    alternate: Measurement | None # the bracketed value

def parse_dual(text: str) -> DualDimension:
    """'984 [38 3/4]' -> DualDimension. A single-unit token yields alternate=None
    without raising."""
```

*Implemented by #40, #41.*

### 3.3 `units/policy.py` — the D1 policy

```python
class Consistency(StrEnum):
    CONSISTENT_WITHIN_ROUNDING = "consistent_within_rounding"
    INCONSISTENT = "inconsistent"

def rounding_band(m: Measurement) -> Decimal:
    """Half the rounding quantum implied by how the value was written.
    Derived, never a magic constant."""

def check_dual(d: DualDimension) -> Consistency:
    """The F2 corroboration lane. CONSISTENT is independent evidence for the
    reading (not the semantic association). INCONSISTENT -> CONFLICTING -> REVIEW."""

def require_same_unit(*ms: Measurement) -> Unit:
    """The D1 guard. Raises MixedUnitError if operands were authored in different
    unit systems and no allowance is declared. Callers turn that into REVIEW."""
```

**Why this exists.** Measured on the real GV drawing, mm and inch renderings of the same dimension
differ by up to 1.600 mm — larger than a 1/16″ tolerance (1.5875 mm). Silent conversion can consume the
whole tolerance budget and produce a false PASS.

*`check_dual` implemented by #42. `require_same_unit` is #43, blocked on D1.*

### 3.4 `verdict/outcomes.py`

```python
class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_FOUND = "NOT_FOUND"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NO_APPLICABLE_RULE = "NO_APPLICABLE_RULE"   # D7 — never renders as clean

class Severity(StrEnum):                        # D3
    CRITICAL = "CRITICAL"; MAJOR = "MAJOR"; MINOR = "MINOR"; ADVISORY = "ADVISORY"
```

`NO_APPLICABLE_RULE` is a distinct outcome, not a flavour of PASS. Silence is the most dangerous
false-PASS there is.

### 3.5 `verdict/trace.py` — the calculation trace

```python
@dataclass(frozen=True)
class TracedOperand:
    name: str
    value: Measurement | Fraction | str | None
    source: str            # SHOP | ARCH | PRODUCT_SPEC | LITERAL | USER_INPUT
    evidence_ref: str | None

@dataclass(frozen=True)
class CalculationTrace:
    operation: str
    operands: tuple[TracedOperand, ...]
    intermediates: tuple[tuple[str, object], ...]   # named derivations, in order
    comparison: str                                  # "|6012 - 6012| = 0 <= 3.175"
    tolerance: Measurement | None
    arithmetic_unit: Unit
    outcome: Outcome
    engine_version: str
    operation_version: str
```

**The test of a good trace:** a reviewer can reconstruct the verdict by hand from it alone. If they
cannot, the trace is incomplete.

*Implemented by #47.*

### 3.6 `verdict/registry.py` — typed operations

```python
class Arity(StrEnum): SCALAR = "scalar"; LIST = "list"

@dataclass(frozen=True)
class OperationSpec:
    name: str
    version: str
    operands: Mapping[str, Arity]
    fn: Callable[..., "OperationResult"]

@dataclass(frozen=True)
class OperationResult:
    outcome: Outcome
    delta: Measurement | None
    trace: CalculationTrace

REGISTRY: Final[dict[str, OperationSpec]]

def register(spec: OperationSpec) -> None: ...
def resolve(name: str) -> OperationSpec:
    """Unknown name -> UnknownOperationError. Never a fallback."""
def validate_operands(spec, operands) -> None:
    """Arity/type mismatch -> RuleAuthoringError, raised BEFORE any arithmetic."""
```

**Non-negotiable.** Operations are looked up by name from this dict. No `eval`, no `getattr` on
user-supplied strings, no dynamic import. A rule supplies a *name*, never code.

*Implemented by #47. Operations by #48–#51.*

### 3.7 Operation signature convention

Every operation is a pure function returning `OperationResult`:

```python
def within_tolerance(*, actual: Measurement, expected: Measurement,
                     tolerance: Measurement) -> OperationResult: ...

def sum_within_tolerance(*, target: Measurement, addends: Sequence[Measurement],
                         tolerance: Measurement) -> OperationResult: ...

def pairwise_within_tolerance(*, left: Mapping[str, Measurement],
                              right: Mapping[str, Measurement],
                              tolerance: Measurement) -> OperationResult: ...
```

Keyword-only, no positional ambiguity. `pairwise_*` takes **mappings keyed by identifier** — not
sequences — because pairing by position is unsafe, and a key-set difference is itself a finding.

Shared rules for all operations:

- A missing required operand → `NOT_FOUND`. Never a default, never zero.
- An empty list where ≥1 is required → `NOT_FOUND`. Never a zero sum.
- Ambiguity → `REVIEW_REQUIRED`.
- Boundary semantics stated explicitly in the docstring (`≤` vs `<`) and tested on both sides.
- No operation reads a clock, a file, an environment variable or a network.

*#48–#52.*

### 3.8 `rules/schema.py` — the rule model

```python
class OperandSource(StrEnum):     # extends the existing enum
    ARCH; SHOP; PRODUCT_SPEC; LITERAL; USER_INPUT      # PRODUCT_SPEC is D4

class Cardinality(StrEnum): ONE = "one"; MANY = "many"
class Scope(StrEnum): SAME_ASSEMBLY; SAME_VIEW; PACKAGE

class InputSelector(BaseModel):
    source: OperandSource
    semantic_type: SemanticType
    scope: Scope
    cardinality: Cardinality

class Derivation(BaseModel):        # D2
    name: str
    operation: str
    inputs: list[str]               # inputs, parameters or earlier derivations

class ApplicabilityVariant(BaseModel):
    when: str
    tolerance: Tolerance
    extras: dict[str, int] = {}     # e.g. field_cut_count

class Rule(BaseModel):
    id: str; version: str
    product_type: str
    check_type: Literal["internal", "arch_vs_shop", "global"]
    severity: Severity                          # D3
    arithmetic_unit: Unit                       # D1
    inputs: dict[str, InputSelector]
    parameters: dict[str, Parameter] = {}
    derivations: list[Derivation] = []          # D2 — validated acyclic at publish
    applicability: Applicability | None
    operation: OperationRef
    on_missing: Outcome = Outcome.NOT_FOUND
    on_ambiguous: Outcome = Outcome.REVIEW_REQUIRED

    model_config = ConfigDict(extra="forbid", frozen=True)
```

`extra="forbid"` is deliberate: an unknown field in a rule file is an authoring error, and silently
ignoring it is how a tolerance goes missing unnoticed.

*#53. Derivations #54 (D2). Applicability #55 (D7). Snapshots #56.*

### 3.9 `rules/parameters.py` — values on no drawing

```python
class ParameterLayer(StrEnum): GLOBAL = "global"; PROJECT = "project"; RUN = "run"

@dataclass(frozen=True)
class ResolvedParameter:
    name: str
    value: Measurement
    layer: ParameterLayer        # which layer supplied it
    provenance: str              # "G.C / Client", "Company standard", "Measured"

def resolve(name, sets) -> ResolvedParameter:
    """GLOBAL -> PROJECT -> RUN, last wins. Missing -> ParameterMissingError,
    which the caller turns into NOT_FOUND. There is no fallback path."""
```

The `TOLERANCE_UNCONFIRMED` sentinel (#57) lives here: a rule carrying it is publishable for
development but can only ever return `REVIEW_REQUIRED`. An unset tolerance is **not** zero.

### 3.10 `verdict/engine.py` — execution order

```python
def execute(snapshot: RuleSnapshot,
            operands: Mapping[str, VerdictOperand],
            parameters: Mapping[str, ResolvedParameter]) -> Finding:
```

Fixed sequence, and it fails safe at every step:

1. Resolve the applicability variant → cannot establish it → `REVIEW_REQUIRED`
2. Check every operand is `CORROBORATED` or `HUMAN_CONFIRMED` → else `REVIEW_REQUIRED`
3. Required operand missing → `NOT_FOUND`
4. `require_same_unit` across operands → mixed → `REVIEW_REQUIRED` (D1)
5. Evaluate derivations in topological order
6. Execute the operation
7. Emit `Finding` with trace, snapshot id, engine version, operand evidence refs

Steps 1–4 all precede any arithmetic. The engine never computes on unqualified input.

---

## 3.11 Which rule version a review uses (ADR-0005)

Three axes are routinely confused. They are separate, and only the second is about versions.

| Axis | Decided by | Example |
|---|---|---|
| **Which rule and variant applies** | the **drawing** | a three-wall countertop selects the `back_left_right` variant, a two-wall selects `back_left` — different tolerances, one rule |
| **Which rulebook version applies** | the **run** | a tolerance was edited last month; a re-run of an old review must not silently apply the new one |
| **Per-vendor rules** | nobody — this does not exist | rules are GV's own standards |

**Tolerances differing between layouts (1/8″ vs 1/16″) is the first axis, not the second.**
Those are variants of one rule selected by `wall_config`. Reading that as a version difference
is the mistake this section exists to prevent.

### The rule

- The resolver takes the **latest published snapshot per applicable rule at run time**.
- **Every finding records the snapshot IDs it used.** This is a correctness requirement of the
  engine, not a reporting nicety: a finding without them is unreproducible and looks identical
  to one that is.
- **An old review is reproduced by replaying its recorded snapshots**, never by re-resolving.
- **Per-project pinning is not built in V1**, deferred behind a measured need.
- **Per-vendor rule sets do not exist.** Vendor identity is recorded for error-pattern
  reporting only, never to select a rulebook.

Consequence: a re-run that does not replay uses current rules and may differ from the original.
Accepted — the original findings stay intact and reproducible from what they recorded, so
"what judged this drawing on the day?" is always answerable.

---

## 4. Testing convention

`tests/<package>/test_<module>.py`, mirroring the source tree. Every Track A story names its test file
in its contract's `verification` field, and that file is the story's proof of completion.

Required for every safety-critical module: a boundary-exact test on both sides, a missing-operand test,
and an ambiguity test. Happy-path-only tests do not satisfy the Definition of Done.

---

## 5. Tracks B and C

Deliberately not designed yet — designing extraction before we have real PDFs would be guesswork.
`docs/V1_RESEARCH_AND_PLAN.md` §6 holds the intent; a design section is added here when Track B starts.

The one Track B commitment worth recording now: **dimension-chain closure** (#27) is a first-class
extraction validator, not a rule. It verifies extraction quality with no ground truth, because the
drawing's own dimension chains sum exactly within each unit system.

---

## 6. What this document does not decide

Design that depends on an unratified decision is deliberately absent. Where a section above cites D1,
D2, D3, D4 or D7, the interface is sketched but must not be implemented until the ADR is accepted.

| Decision | Blocks the design of |
|---|---|
| D1 unit policy | `require_same_unit`, `Rule.arithmetic_unit` |
| D2 derivations | `Derivation`, engine step 5 |
| D3 severity | `Severity`, the critical false-PASS metric |
| D4 product spec | `OperandSource.PRODUCT_SPEC` |
| D7 no applicable rule | `Outcome.NO_APPLICABLE_RULE`, applicability resolver |
