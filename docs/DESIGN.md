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
  errors.py         typed unit/parse errors (ImperialParseError, UnknownRoundingError)

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
    NOT_CORROBORATED = "not_corroborated"   # single-unit token; nothing to cross-check

def rounding_band(m: Measurement) -> Decimal:
    """Half the rounding quantum implied by how the value was written.
    Derived, never a magic constant. Raises UnknownRoundingError when raw_text is
    absent — a computed Measurement has no authored token, and the band applies to
    authored values only. Never derive the quantum from the Fraction denominator:
    "2.375" is 19/8, implying 1/8 where the author wrote 1/1000, which yields a band
    ~100x too loose and would accept genuine inconsistencies."""

def check_dual(d: DualDimension) -> Consistency:
    """The F2 corroboration lane. CONSISTENT is independent evidence for the
    reading (not the semantic association). INCONSISTENT -> CONFLICTING -> REVIEW.
    NOT_CORROBORATED when there is no alternate reading: the observation stays
    RAW_CANDIDATE, but it is NOT conflicting — absence is not disagreement, and
    folding it into either other value would be false evidence or false conflict."""

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

- **Operations receive resolved, qualified values only.** Missing and ambiguous input is the
  engine's boundary (§3.10 steps 1–4), not each operation's. An operation handed `None` raises a
  programming error rather than deciding an outcome — defence in depth, never duplicated policy.
  *(An earlier version of this list stated `NOT_FOUND` and `REVIEW_REQUIRED` as operation rules,
  which contradicted §3.10 and invited every operation to reimplement the policy slightly
  differently. Corrected by ADR-0012.)*
- An empty list where ≥1 is required → `NOT_FOUND`, decided by the engine before the operation
  runs. Never a zero sum.
- Boundary semantics stated explicitly in the docstring (`≤` vs `<`) and tested on both sides.
- No operation reads a clock, a file, an environment variable or a network.

Settled edge cases (ADR-0012):

- `exists` — `None`, empty string and empty collection are **absent**; **zero is present**, because
  `0 mm` is a real measurement and an empty string is a failed extraction.
- `contains` — literal and case-sensitive, with no normalisation. Normalising is a judgement about
  whether two spellings mean the same thing, and it belongs upstream in evidence where it is
  recorded and auditable.
- `equals` — `Measurement`, `str`, `StrEnum`, `int`, `Fraction`. Never `float`. Two `Measurement`s
  must share their authored unit; mixed units raise rather than convert.
- `one_of` with an empty allowed set — a rule-authoring error rejected at publish, not a `FAIL`.
- `difference_between` — usable inside `derivations:`, never as a rule's terminal operation: it
  states no expectation, so no honest outcome exists for it.
- `conditional_required(*, when, value)` — `when=True` and present → `PASS`; `when=True` and absent
  → `NOT_FOUND`; `when=False` → `PASS` with the trace recording that the requirement was **not
  exercised**, so a condition that never fires is visible rather than silently reassuring.

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
    product_type: ProductType                   # controlled vocabulary, ADR-0007
    check_type: Literal["internal", "arch_vs_shop", "global"]
    severity: Severity                          # D3
    arithmetic_unit: Unit                       # D1
    inputs: dict[str, InputSelector]
    parameters: dict[str, Parameter] = {}
    derivations: list[Derivation] = []          # D2 — validated acyclic at publish
    applicability: Applicability | GlobalApplicability   # required, ADR-0007
    operation: OperationRef
    on_missing: Outcome = Outcome.NOT_FOUND
    on_ambiguous: Outcome = Outcome.REVIEW_REQUIRED

    model_config = ConfigDict(extra="forbid", frozen=True)
```

`extra="forbid"` is deliberate: an unknown field in a rule file is an authoring error, and silently
ignoring it is how a tolerance goes missing unnoticed.

`applicability` is **required** for the same reason (ADR-0007). A rule with no layout
discriminator declares `applicability: {scope: global}`; omitting the block is an authoring error,
not an implicit "applies to everything". A forgotten discriminator read as unconditional would
apply one layout's tolerance to every layout.

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

## 3.12 Project scope (ADR-0006)

A project is one finalized vendor and one brand. It does **two distinct jobs**, and conflating
them is a mistake:

| Role | What it does |
|---|---|
| **Resolver key** | supplies parameter overrides for a check — filler min/max, field cut size, tolerances — layering over global defaults, exactly as the client's checklist describes with *"Global / Project Based Input"* |
| **Isolation boundary** | retrieval and matching filter by project, so one project's references can never be offered as evidence in another's review |

### What lives where

```python
# rules/project.py — the minimal projection the resolver needs
@dataclass(frozen=True, slots=True)
class ProjectScope:
    project_id: str
    parameter_overrides: Mapping[str, Quantity]
```

**No brand or vendor field here.** Those are business metadata belonging to the full project
record in the control plane (Track C). `rules/` must not import `app/` (§2), so the deterministic
core takes only the identifier and the overrides.

**Vendor is metadata, never a rule key.** It identifies the project and feeds error-pattern
reporting. Every vendor is held to the same rule for the same layout — rules are GV's own
standards, and selecting a rule set by vendor would mean holding one vendor to a different
standard than another. A one-off exception is a reviewer-approved note on a finding.

**Brand-prototype standards are deferred** to a later layer, behind a measured need.

### The four resolver keys together

```
category (cabinet/countertop)     -> which checklist
+ layout/config (wall_config)     -> which variant (1/8" vs 1/16")
+ project scope                   -> parameter overrides + reference isolation
+ effective version               -> which snapshot, recorded on the finding
= the rule and parameters for this check
```

### Effective version: highest semver

The resolver selects the **highest `rule.version`** among published snapshots for a rule id, and
`publish` enforces that **`(rule_id, version)` maps to exactly one content hash**. Editing a
published rule without bumping its version is a hard error, not a silent second snapshot — which
turns an ambiguity the resolver could not have resolved into a loud failure at publish time.

To change a published rule, bump its version.

---

## 3.13 `rules/applicability.py` — which rules apply, and what went unchecked (ADR-0007)

```python
@dataclass(frozen=True, slots=True)
class CheckContext:
    product_type: ProductType
    project: ProjectScope
    discriminators: Mapping[str, str]        # {"wall_config": "back_left_right"}

@dataclass(frozen=True, slots=True)
class ApplicableRule:
    snapshot: RuleSnapshot                   # the effective version
    variant: ApplicabilityVariant | None     # None only for {scope: global}

@dataclass(frozen=True, slots=True)
class Abstention:
    outcome: Outcome                         # NO_APPLICABLE_RULE | REVIEW_REQUIRED
    reason: str
    rule_id: str | None

@dataclass(frozen=True, slots=True)
class Resolution:
    applicable: tuple[ApplicableRule, ...]
    abstentions: tuple[Abstention, ...]
    project: ProjectScope

def resolve(store: SnapshotStore, context: CheckContext) -> Resolution: ...
```

**Abstentions are returned in band, never as an empty list.** An empty list is silence, and it
makes every caller responsible for remembering that empty means `NO_APPLICABLE_RULE` — a caller
that forgets emits nothing, so no finding exists to be wrong. Coverage is
`checked_count / considered_count`, which is what makes "we checked 6 of 9" reportable rather
than implied.

**Two abstentions, two different instructions.** A discriminator the drawing did not establish
is `REVIEW_REQUIRED` and sends a reviewer to the drawing. A discriminator whose value no variant
covers is `NO_APPLICABLE_RULE` and sends them to the rulebook.

**Project scope is carried, never used to filter.** No rule is selected by project; the scope
rides through to supply the parameter layer §3.9 reads.

**No priority and no firing order.** Width, depth and sink checks all apply to one countertop.
`applicable` is sorted by rule id for stable reporting only.

*#55.*

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
