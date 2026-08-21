# V1 verdict model — exact match, inches only

The product-level decision that fixes how V1 decides pass/fail, from Raj's 2nd email reply
(2026-08-16). Recorded here because it fixes the verdict *operator*, not just a client fact — the
facts themselves live in `docs/CLIENT_FACTS.md` Q2 and Q12. Last updated 2026-08-18.

---

### D1. What tolerance does the V1 verdict apply to a numeric check?
status: DECIDED
decision: Exact match — a zero-width comparison, no tolerance band. A numeric check passes only on an exact match; any difference is flagged, and the reviewer clears false flags on screen and finalises. Graded / per-check tolerances are deferred past iteration 1. Consequence you may not have noticed: this is deliberately **review-heavy** — expect a high flag rate and low automation-coverage in V1; that is the false-PASS-safe posture, not a defect, and the reviewer clearing flags *is* the tolerance.
because: Raj chose exact-match for iteration 1 in place of a number, having judged no single band is safe — the tightest millwork goes is ~1/16", yet even that fails client QC at a flush, no-wall edge.
source: Raj 2nd email reply, `docs/CLIENT_FACTS.md` Q2: "Lets go for exact match in the first iteration of our product and let the reviewer make the decision on the flagged dimensions. He/she can remove the false flags on the screen and finalize the document."
affects: rules/ (authoring), verdict/, eval/ (critical_false_pass gate), reviewer UI, #274 (validation)

### D2. Which unit is authoritative for the verdict — mm or inches?
status: DECIDED
decision: Inches. mm on GV drawings is the vendor's machine reference only and is ignored for the verdict; comparisons are inch-vs-inch on exact fractions (`Fraction`). Consequence you may not have noticed: this dissolves the mm↔inch conversion-noise problem ADR-0001 was built around — the verdict never compares across units — so ADR-0011's `cross_unit_allowance` is not needed by any V1 rule and stays at its safe default (`None` = refuse mixing). mm may still corroborate that an inch was *read* correctly, but it is never a verdict operand.
because: Raj — U.S. construction is I-P; mm exists on the sheet only because vendor machines prefer it; "as long as inches match, then we should be good."
source: Raj 2nd email reply, `docs/CLIENT_FACTS.md` Q12: "Ignore the metric dimensions, U.S construction works on I-P system, so we consider only feet and inches… As long as inches match, then we should be good."
affects: extraction/ (unit selection), verdict/, ADR-0001, ADR-0011

### D3. What does exact-match mean for the tolerance machinery already built?
status: DECIDED
decision: V1 rules author **no per-check tolerance value** — they assert exact equality (a zero-width comparison), so `within_tolerance` with a client band and the `TOLERANCE_UNCONFIRMED` sentinel are simply not exercised in V1. The machinery stays in place for future graded tolerances. Consequence you may not have noticed: **the geometric tolerances are NOT removed by this.** `endpoint_tolerance` (#181, containment) and dimension-text proximity (#180) are *extraction* tolerances — how close a line's ends sit to an item — and remain required, empirical, and still gated on real drawings. Exact-match governs the *verdict comparison* only; do not read it as "no tolerances anywhere."
because: with exact-match there is no tolerance number to confirm, so the "unconfirmed tolerance → REVIEW REQUIRED" path never triggers; a numeric check is either an exact match or a flag.
source: `rules/schema.py` (`TOLERANCE_UNCONFIRMED`, `Tolerance.is_confirmed`); AGENTS.md §7 (typed operation registry); `docs/CLIENT_FACTS.md` Q2.
affects: rules/schema.py, verdict/operations/, eval/metrics.py

---

**Citation note (pre-existing drift, flagged not fixed here).** The "an unconfirmed tolerance cannot
reach production" rule is cited as *"(ADR-0011)"* in `scripts/client_facts.py`, the `docs/CLIENT_FACTS.md`
header, and `docs/DESIGN_PRODUCT.md` §5 — but **ADR-0011 is "a declared cross-unit allowance, distinct
from tolerance,"** and its own Options section rejects reusing `Tolerance` precisely *because* that type
carries the `UNCONFIRMED` sentinel. So ADR-0011 is not the source of the unconfirmed-tolerance rule.
A one-line correction is worth making where those three cite it, but it is out of scope for this file.
