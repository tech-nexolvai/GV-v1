# Grounded reading research — proposed, not approved for product

Status: **research finding; awaiting an admin decision**. This record does not change the rulebook, evidence gate, live reader, or reviewer workflow.

## Problem and measured boundary

The current live worker ranks a candidate dimension row before asking a vision model to copy labels. It has no independent semantic identification of the physical object and no proof that the selected row measures that object's two ends. A narrow crop can provide legible digits while losing the context needed to associate them. Ambiguous row selection or one held span prevents a complete chain; that preserves safety but leaves the form empty.

In a read-only audit of two retained product runs, **0 of 77 keyed width inputs** became form proposals. Stored model-attempt records were complete, but the current keys lack independently confirmed object endpoints and row identity, so “correct object” and “correct row” cannot yet be scored. Prepared-crop reading results must not be reported as live-worker end-to-end results.

A private, offline two-picture prototype asked two Bedrock vision makers narrow questions with a close-up and a numbered full drawing view. Across 14 keyed pages it made 108 calls and recorded $0.375012 in usage. Both makers chose the same candidate row on three pages, but no choice had independent code verification against the object's ends. No complete dimension chain was produced and no form value was saved. The held-out set was run once. These numbers **do not** establish a critical false-PASS rate; no verdict was attempted.

## Proposed architecture, subject to approval

1. Create independent, person-confirmed gold labels for object ends, candidate row, span-to-label ownership and source-ink ownership. Missing annotations mean “unscored,” not “right.”
2. Build an immutable evidence packet for each question: high-resolution close-up containing target and ticks/leader, plus a full vendor view with the target visibly marked. Hash the exact image bytes, page transform, prompt/version and candidate IDs; link each raw model attempt to them.
3. Ask a sequence of checkable questions: identify the object and its ends, compare every plausible row with those ends, then associate each marked span with its printed label. “Cannot tell” is a first-class answer. A page without a row candidate uses the existing whole-page path only for review suggestions.
4. Code verifies geometric claims using independently calibrated linework/endpoint evidence. Two makers agreeing with each other is not geometric proof. No endpoint tolerance is approved in this record; it requires keyed measurement and an admin decision.
5. Keep the current source-ink, cut-off, compound/stacked, exact-text, exact-unit, drawn-length, counter-break, and evidence-gate protections. Model-pointed boxes are display hints, not proof of a value or its owner. “Pieces add up” never chooses a row.
6. Show individually qualified pieces as proposals and ask the reviewer about unresolved links. A partial chain never yields an automatic PASS. Only an eligible, complete chain reaches the unchanged deterministic checker; a person signs off.

Before any automatic form promotion, run the **real worker path** on a development set and a once-only held-out set. Report every stage's right/wrong/unmeasured counts, sealed-wrong values, complete chains, and critical false-PASS. Stop on a wrong object, row or value that could enter the form, or any critical false-PASS. Audit image hashes and every model attempt. Keep client drawings, crops, labels and values outside this public repository.

## Build sequence if approved

Separate issues should cover (1) independent gold labels and scorecard; (2) immutable two-picture packets and attempt audit; (3) object/row questioning with a measured geometric gate; (4) marked-span label association and current safety guards; (5) partial-proposal UI and full-worker acceptance. Each is behind a flag and independently testable; no automatic rollout follows merely from a successful prompt experiment.

External services that receive drawings require separate admin approval. The current proposal uses the existing Bedrock account and repository-approved libraries only. Do not add AGPL components.
