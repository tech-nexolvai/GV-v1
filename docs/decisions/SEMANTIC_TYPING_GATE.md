# Semantic typing at scale — an evidence gate, not a classifier shortcut

## Decision

The reader may propose what a dimension means, but a proposal is not a verdict operand.  A reading
becomes typed evidence in one of only two ways:

1. **Exact mechanical tag.** A vector-extracted tag is an exact member of the vocabulary approved
   for this layout, and that tag and the numeric reading are both attached by the existing,
   deterministic line-association result to one identical dimension line.  That is a reproducible
   fact about the drawing rather than a proximity guess.  It is stored as `CORROBORATED`, with both
   raw candidates and the `MECHANICAL_TAG` corroboration lane retained for audit.
2. **Reviewer confirmation.** The existing confirmation flow remains `HUMAN_CONFIRMED` and carries
   the reviewer identity.  It is the required path whenever the mechanical conditions are absent.

Every other outcome is **`REVIEW_REQUIRED` for semantic typing**: unknown or provisional tag,
tag/read association missing, multiple incompatible tags, OCR-only tag, or an agent suggestion.
It does not create a canonical observation, therefore no operand reaches arithmetic and the rule
continues to abstain safely.

`observation_candidates.semantic_guess` stays null.  It is immutable raw-reader output; writing a
model or heuristic label there would erase the distinction between a reading and an approved
meaning.  The new lane mints a separate canonical observation only after it proves the tag binding.

## Vocabulary scope

Q20 is final only for the three-sided countertop layout.  The deployment must explicitly pass the
set of tags approved for a layout to the typing stage; there is no global default and no A–G to
`CT0xx` inference.  The mechanical resolver accepts an exact `SemanticType` value only — it does
not normalise an alias, expand a synonym, or infer a type from position.

That makes direct tags such as `CT007` usable where Raj has finalised them, while keeping back-only,
island, and generic cabinet/filler geometry out of the automatic lane until their tags are genuinely
settled.

## Agentic ambiguity path

The configured LLM/agent may receive a bounded candidate id, the allowed vocabulary, and evidence
references and return a structured **suggestion**.  It may not return a number, a verdict, or an
operand.  Its confidence is diagnostic only: an agent suggestion is always `REVIEW_REQUIRED` until
a reviewer confirms it.  This is intentional.  A self-reported confidence is not independent
evidence that a label is correct, and allowing it to qualify a value would make a verdict-flipping
guess look auditable.

The agent is invoked only from an explicit ambiguity/reviewer action, never as part of the ordinary
drawing-agnostic pipeline.  An unavailable provider yields the same review-required state.

## Verdict wall

`workflow/evidence_operands.py` is still the only route from canonical evidence to a rule input, and
`evidence/gate.py` still seals only qualified statuses.  The rule engine receives a typed
`VerdictOperand`, never an agent response, a tag string, an OCR confidence, or geometry.  It retains
sole ownership of exact comparison and PASS/FAIL.

## Measurement and rollout

Typing is measured as exact semantic-type accuracy and coverage against answer keys whose provenance
explicitly says `PM-confirmed`.  Self-verified value cases whose types are marked
`heuristic-unconfirmed` are excluded: using them as labels would grade the heuristic against itself.
The current eligible seed is Board Room 1 page 13 (two `filler_width`, three `cabinet_width`).
The report must state the denominator and never generalise that five-case seed into a production
accuracy claim.

Before enabling a layout, run this metric on a materially larger human-confirmed, held-out set and
review every automatic type and abstention.  A decline in coverage is acceptable; an unreviewed type
that changes a deterministic outcome is not.
