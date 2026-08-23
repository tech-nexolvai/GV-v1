# A6.3 (#60) — authoring the sink-cutout rules

Go/no-go for the three sink-cutout checks (cutout width, cutout depth, front/back offset). Grounded
in `docs/CLIENT_FACTS.md` and the ADRs. The key finding: the client's width/depth defect is **local
to one rule**, so most of the family ships now. Last updated 2026-08-22.

---

### D1. Which of the sink-cutout rules can be authored now?
status: DECIDED
decision: Author **cutout depth** and **front offset** now — both fully confirmed. Author **back offset** with its minimum marked UNCONFIRMED (D3). **Hold cutout width** until the client resolves the width/depth question (D5). The width/depth ambiguity does **not** contaminate the others: depth is derived from the interior *depth* (undisputed by text and diagram alike), and the offsets never touch the interior dimensions. Cutout depth also needs a reviewer-tunable **clearance** parameter (default ~0.25″ each side, the client's stated *typical*), authored the same way as the 4″ front-offset default.
because: front offset is exact equality to a configurable 4″ (Q5); depth = interior depth − 2×clearance with the interior depth reviewer-provided (Q7) and the clearance a documented default; only the *width* formula's input is the unconfirmed S19 typo (Q15).
source: docs/CLIENT_FACTS.md Q5, Q6, Q7, Q2/Q12 (exact match, inches), Q15
affects: #60; rules/rulebook/

### D2. Split "front and back offset" into two rules.
status: DECIDED
decision: Author front and back offset as **separate** rules. Front = exact equality to the configurable 4″ (live now). Back = derived remainder (countertop depth − front offset − sink depth) checked **≥ a global minimum** (UNCONFIRMED until supplied).
because: they are different operations (exact-equality vs minimum) at different readiness; bundling them would hold the confirmed front check hostage to the back's missing number.
source: CLIENT_FACTS Q5 (front, exact), Q6 (back = remainder ≥ pending minimum)
affects: #60

### D3. Back-offset minimum missing → model as a required parameter (NOT_FOUND), not a tolerance; keep it unpublished.
status: DECIDED (amended 2026-08-22 — the original said `TOLERANCE_UNCONFIRMED`; that was wrong, see below)
decision: The minimum is a **required GLOBAL parameter**, not a tolerance. Author the back-offset rule so the missing minimum resolves through `resolve_required` to `Outcome.NOT_FOUND` — the deliberate "a required input is absent, go supply it" signal (`rules/parameters.py:518-545`) — and **do not publish it to production** until the client supplies the value. Do **not** leave it unwritten (a silent gap: the back offset simply isn't checked and reads as fine — the false-pass-by-omission the system exists to prevent). Do **not** mark it `TOLERANCE_UNCONFIRMED`: it is not a tolerance (the type-swap **ADR-0011** forbids), and that channel returns `REVIEW_REQUIRED`, where a missing input must return `NOT_FOUND`.
because: an authored rule returning `NOT_FOUND` makes the gap explicit and sends the reviewer to supply the value; an unwritten rule makes it invisible. Correcting the original: the back offset is a `minimum` check with no tolerance, so `TOLERANCE_UNCONFIRMED` doesn't fit it, and `parameters.py` deliberately returns `NOT_FOUND` (not `REVIEW_REQUIRED`) for a missing parameter — see D6 for why the gate must still catch it.
source: rules/parameters.py (`resolve_required` → `outcome_for_missing_parameter` → `NOT_FOUND`); rules/publication.py (`is_production_ready` counts tolerances only); ADR-0011 (do not reuse the tolerance/UNCONFIRMED type for a different concept); CLIENT_FACTS Q6
affects: #60; rules/publication.py (D6)

### D4. CT-3 identifier collision → mint provisional internal ids now.
status: DECIDED
decision: Mint unambiguous **descriptive** internal ids (`sink_cutout_width`, `sink_cutout_depth`, `sink_offset_front`, `sink_offset_back`), each keyed to the quantity it verifies, with a mapping to the client's labels. Do not wait for the client to rename.
because: rule ids must be unique or the engine cannot store or resolve three rules sharing "CT-3"; and the client already said the vocabulary is provisional, final tags after the layouts are finalized (Q20/3.9), so provisional ids are sanctioned. A later client rename is a **one-line mapping change** because the id is decoupled from the label — the rule keys on the quantity, not the name (the C1.8 ruling).
source: CLIENT_FACTS Q18 (three-way CT-3 collision), Q20 (provisional per Raj 3.9); docs/decisions/C1_8_APPLICABILITY_SCOPE.md
affects: #60; the id mapping

### D5. Width/depth typo → ask the client; do not author from the diagram.
status: DECIDED
decision: Do **not** author the cutout-width rule from the diagram over the spreadsheet text. Ask the client which interior dimension drives the cutout width. The diagram is evidence for a *sharp question*, not a substitute for the answer.
because: the text-vs-diagram conflict is a contradiction within the client's own inputs, and choosing the diagram is guessing on a pass/fail-determining, manufacturable dimension — the exact "guess an ambiguous input" the abstention rule forbids, and a wrong guess is a confident false verdict (the highest-cost error). Verified against the record: Raj's 3.8 `CT011/CT012/CT013` confirmation is a **different** measurement (the cutout's clearances to the sink *cabinet's* interior faces), so it does **not** resolve the S19 formula, which sizes the cutout against the *sink's own cut-sheet* dimension — Q15 is genuinely still open.
source: CLIENT_FACTS Q15 (OPEN, "not confirmed by Raj"); Raj email 3.8 (confirms cabinet-side geometry only); AGENTS.md §2 (abstention rule)
affects: #60; the client message

### D6. The publication gate must count unresolved required parameters, not only unconfirmed tolerances.
status: DECIDED
decision: Extend `rules/publication.py` so `is_production_ready` refuses a rule that can only ever return `NOT_FOUND` — i.e. one that depends on a **required parameter which must come from a GLOBAL or PROJECT layer and has no value there** (a client-owed value, like the back-offset minimum). Give it the same "cannot decide anything in production" verdict the gate already gives an unconfirmed tolerance. Per-run reviewer inputs (e.g. the sink cut-sheet dimensions) are **not** this case — they arrive at run time and must not block publication. Do not force the minimum through the tolerance channel to trip the gate (D3).
because: `is_production_ready` today counts only tolerances (`unconfirmed_tolerance_count`), so a rule blocked on a missing client-owed parameter "stops looking provisional" — the exact failure `publication.py`'s own docstring names, one field over. The back-offset rule is the first instance: no tolerance, so the gate reports it production-ready while it can only return `NOT_FOUND`. A gate that catches one way a rule cannot decide and not the other is half a gate.
source: rules/publication.py (`is_production_ready`, `unconfirmed_tolerance_count`); rules/schema.py:363 (`Rule.parameters`); rules/parameters.py (`ParameterLayer` GLOBAL/PROJECT/RUN, `resolve_required`)
affects: rules/publication.py; a new follow-up story; #60 (the back-offset rule stays unpublished until this lands or the client supplies the minimum)

---

**Ships now:** cutout depth, front offset, and the back-offset rule (UNCONFIRMED, REVIEW-only) — three of four units, with the fourth's gap visible rather than silent. **Held:** cutout width, on one client answer. **Verification note:** the two email asks were checked against Raj's replies — the back-offset minimum is a *promised* value (he committed to it, pending vendor), so it is a follow-up; the width/depth question is genuinely unanswered, so it is a new ask.
