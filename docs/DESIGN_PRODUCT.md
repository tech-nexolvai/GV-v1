# DESIGN — the product surface (Track D)

Companion to `DESIGN.md`. Owns **D1–D7**: how a finding is retrieved, how it reaches a vendor, what a
reviewer may do, and how a rule gets published.

With the reviewer UI deferred (`D3`), **the redline and the spreadsheet are the product.** They are how
a reviewer sees a finding and how the vendor receives it. That raises the stakes on §3 considerably.

---

## 1. Package layout

```
app/api/      findings.py finding_chain.py finding_export.py
app/review/   session.py evidence_actions.py approval.py ledger.py exceptions.py proposal.py
reports/      redline.py publication.py spreadsheet.py certificate.py
              vendor_patterns.py revision_delta.py
rules/governance/  proposal.py regression.py publish.py
```

## 2. Import rules

| Package | May import | Must never import |
|---|---|---|
| `reports/` | `verdict/` (read), `evidence/`, `storage/` | `extraction/`, `retrieval/` |
| `app/review/` | `app/`, `evidence/` | `verdict/` internals, `rules/` authoring |
| `rules/governance/` | `rules/`, `eval/` | `extraction/`, `retrieval/` |

**`rules/` must never import `app/review/`.** A correction is not a rule, and the import graph is where
that stops being a slogan (D5.3).

---

## 3. D1–D2 — retrieval and the deliverable

### 3.1 A finding must be able to prove itself

```python
@dataclass(frozen=True, slots=True)
class FindingChain:
    finding: Finding
    rule_snapshot: RuleSnapshot          # the snapshot id, never just the rule id
    parameter_versions: Mapping[str, str]
    operands: tuple[SealedOperand, ...]  # each with its evidence reference
    trace: CalculationTrace
    engine_version: str
```

The acceptance test is a **recompute**: load the chain, run `verdict.engine.execute` against it, assert
an identical outcome. If a finding cannot be recomputed from what the API returns, the audit trail is
decoration.

### 3.2 Abstentions render as loudly as failures

Every outcome gets a distinct visual treatment. **None of them is "nothing".**

| Outcome | Redline treatment |
|---|---|
| `PASS` | present, quiet |
| `FAIL` | prominent, with expected vs observed |
| `REVIEW_REQUIRED` | prominent, with *why* it could not be decided |
| `NOT_FOUND` | listed — a check that found nothing is a result |
| `NO_APPLICABLE_RULE` | listed in "what was not checked" |

A `NOT FOUND` that renders as blank space recreates the exact failure `NO_APPLICABLE_RULE` exists to
prevent: **silence reading as approval**. The whole safety argument is that the system abstains when
unsure; if abstention looks identical to approval on the page a human reads, that argument does not
survive contact with the deliverable.

### 3.3 Two report modes

```python
class ReportMode(StrEnum):
    INTERNAL = "internal"     # engine output, for the reviewer
    VENDOR   = "vendor"       # only reviewer-approved content
```

ADR-0010: derived expectations may be shown with their calculation, and **no computed dimension reaches
a vendor without sign-off**. A vendor render from unapproved findings raises.

Spreadsheet values are written as **text preserving exact fractions** — a spreadsheet that converts
`1/8` to `0.125` has silently discarded the distinction the whole units layer exists to keep.

---

## 4. D4–D5 — reviewer authority

```python
class ReviewActionKind(StrEnum):
    CONFIRM = "confirm"       # -> HUMAN_CONFIRMED, one of two states that reach the engine
    CORRECT = "correct"       # -> writes the ledger in the same transaction
    EXCEPT  = "except"        # -> scoped, expiring
    DISMISS = "dismiss"
```

`HUMAN_CONFIRMED` is a direct write into the trusted set. There is no anonymous confirmation — if it
were possible without a named human, the evidence gate would have a door in the back of it.

### 4.1 Exceptions cannot be permanent

```python
@dataclass(frozen=True, slots=True)
class Exception_:
    scope: Literal["finding", "item", "package"]   # never "rule, everywhere"
    reason: str
    approved_by: str
    expires_at: datetime                           # required — no default, no None
```

A permanent, silent, unscoped exception is indistinguishable from having deleted the check.

### 4.2 The proposal gate

*"Corrections silently become rules"* is a named risk. The control is three things together: an
append-only ledger, a **human** proposal gate, and a full regression.

There is no automated path from the ledger to a rule. `tests/test_no_ledger_to_rules.py` asserts it in
the import graph, because a slogan does not survive a refactor.

### 4.3 The review certificate

For a manufacturing sign-off, the artifact that matters a year later is not "what does the system say
now?" but *"what did a named person sign off, on what evidence, under which rules?"*. The certificate is
immutable, content-hashed and reproducible from stored records, and lists dismissed findings as
prominently as accepted ones.

---

## 5. D6 — rule governance

```
propose → validate → full gold-set regression → authorised approval → publish
```

```python
def publish(proposal: RuleProposal, *, approver: Principal) -> RuleSnapshot:
    """Every gate, in order. No override flag exists."""
```

Three refusals, each of which is the point of a gate:

- **A critical false-PASS regression blocks publication outright.** No override.
- **Publication with no gold set available is refused**, rather than passing vacuously. A regression
  check that passes when there is nothing to check against reports a green gate for an unmeasured change.
- **A rule with an unconfirmed tolerance cannot reach production** (ADR-0011). The `±1/8″` that
  circulated for weeks was our own placeholder from a sample file; this gate stops the next one arriving
  in the same disguise.

The approver may not be the proposal's author.

---

## 6. D7 — vendor patterns, and the line

ADR-0006: **vendor identity is metadata, never a rule key.** Every vendor is held to the same rule for
the same layout. Its one legitimate use is spotting patterns — a vendor that repeatedly gets filler
distribution wrong is a conversation, not a different rulebook.

`tests/test_vendor_neutrality.py` asserts, transitively, that neither `verdict/` nor `rules/` can reach
the vendor reporting module, that vendor is not a valid applicability discriminator, and that no
parameter-resolution key is vendor-derived.

Per-vendor scrutiny is a form of the system deciding how carefully to check. It would arrive as a
reasonable-sounding feature request, which is why the refusal belongs in a test rather than a discussion.

---

## 7. Testing convention

Per `DESIGN.md` §4, plus:

- **Recompute tests** for anything claiming auditability (D1.2, D4.4).
- **Render-one-of-each** tests: every outcome appears in the redline and the spreadsheet.
- **Refusal tests** for every gate in §5 — the blocked path is the feature.
- **Import-graph guards** for D5.3 and D7.2.
