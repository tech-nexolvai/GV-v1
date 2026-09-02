# CLIENT_FACTS — the authority on what the client has and has not told us

**This file supersedes every other document for client facts.** Where `RAJ_SOURCED_ANSWERS.md`,
`docs/BACKLOG.yaml` or an issue body disagrees with it, this file is right and the other is stale.

It exists because those sources disagreed with each other, and every engineering decision was costing
a reconciliation — which is also how two real errors got made: `CT011`–`CT013` were recorded as
undefined when the diagram defines them, and `±1/8″` circulated for weeks as a client tolerance when
it is our own placeholder.

## How to read an entry

| Field | Meaning |
|---|---|
| `status` | `ANSWERED` or `OPEN`. Nothing else — no "implied", no "partially". If Raj did not say it, it is OPEN. |
| `blocks` | `formula` · `value` · `nothing` — see below |
| `issue` | the tracking issue |
| `source` | the specific cell or image. Not "it's in the checklist". |

### What `blocks` means, and why it is the important field

- **`formula`** — the answer changes *what gets computed*. A rule authored before it lands would
  compute the wrong quantity and pass confidently. `scripts/issue_gate.py` refuses to start such a
  story.
- **`value`** — the answer supplies only a *number*. The rule can be authored now with
  `TOLERANCE_UNCONFIRMED`, which returns REVIEW REQUIRED for every drawing and cannot reach
  production (enforced by `rules/publication.py`). The gate reports READY and says the story
  ships provisional.
- **`nothing`** — informational; no code depends on it.

That distinction is enforced, not advisory: `tests/test_client_facts.py` fails the build if a story
declares a dependency on a question this file does not contain.

### The trap that keeps catching us

**Raj's substantive content is disproportionately inside embedded images, not spreadsheet cells.**
A text search of the workbooks misses most of it. Extract with `zipfile` over `xl/media/` and look at
every image. Several entries below cite an image because the cells say nothing.

<!-- CLIENT FACTS START -->

## Q1 — Field cuts: how many, added to the run or trimmed from it?
status:  ANSWERED
blocks:  formula
issue:   #9
answer:  Two field cuts (one per end) for the 3-wall vanity, ADDED as extra to the wall-to-wall
         dimension; 1" each is typical but customizable per project. Confirmed by Raj: added at
         fabrication, trimmed on site to the field dimension; count is layout-driven (walls on
         both sides), not a fixed number.
source:  Countertop_Checks_Updated.xlsx CT-1 diagram (image1.png) labels the slab
         "1" Extra for Field Cut + 84" + 1" Extra for Field Cut", depth "24"".
         Cells: D15 "1" is typical", D16 "customizable per project". NOTE the cabinet
         run separately carries a 5" field-cut FILLER (Cabinet_Checks image1.png,
         "5" FILLER PANEL TO BE FIELD CUT AS PER SITE CONDITION") — a different element;
         do not conflate it with the countertop's 1"-per-end field cut.

## Q2 — Tolerance values per check and per wall configuration
status:  ANSWERED
blocks:  value
issue:   #10
answer:  EXACT MATCH for V1 — no tolerance band. Compared on inches (see Q12); any dimension that
         does not match exactly is flagged, and the reviewer clears false flags on screen and
         finalizes. A graded/per-check tolerance is deferred past iteration 1.
source:  Raj 2nd email reply: "Lets go for exact match in the first iteration of our product and let
         the reviewer make the decision on the flagged dimensions. He/she can remove the false flags
         on the screen and finalize the document." He explained why a single band is hard — the
         tightest millwork goes is ~1/16", but even that fails client QC at some locations (a
         countertop flush with the cabinet, no wall on that side) — hence exact-match-plus-reviewer
         rather than a number. (The earlier ±1/8" was our placeholder.) V1 rules use exact equality,
         not within_tolerance; the reviewer is the tolerance.

## Q3 — Does cabinet width include the run's end panel (the 51 mm / 2" piece)?
status:  ANSWERED
blocks:  formula
issue:   #11
answer:  No — the 51 mm / 2" piece is a FILLER, not part of the cabinet. It is a strip between the
         wall and the cabinet, and is absent when there is no wall on that side.
source:  Raj email reply (3.2): "Filler piece is not part of the cabinet. This is just a small
         strip between the wall and the cabinet. There won't be any such piece if there is no wall
         on the side of the cabinet." Resolves the earlier conflict — the cabinet's OWN side panels
         stay inside cabinet width (cell CT004), but the run's end strip is a separate filler.

## Q4 — Severity per check: critical vs advisory
status:  ANSWERED
blocks:  value
issue:   #12
answer:  V1 flags EVERYTHING with no severity split — the reviewer decides. Severity tiers
         (red / yellow / orange) are deliberately deferred until after 10–50 projects, because
         whether a flag is serious depends on the project and product and can't be pinned now.
         Confirmed by Raj AND Abhishek on the call.
source:  Call 2026-08-25. Raj: "sometimes we might decide that it's not serious… it might become
         serious in certain situations." Abhishek: "flag everything… when we do 10, 15, 20, 50
         projects… they can select it as 'just worth a look'… for now flagging is important." By
         explicit client choice no rule declares CRITICAL in V1, so critical_false_pass_rate reports
         NOT MEASURED — now a decision, not a gap.

## Q5 — Sink front offset: "4" minimum" or "= 4""?
status:  ANSWERED
blocks:  formula
issue:   #13
answer:  A configurable global default of 4", checked by EXACT equality now that Q2 is settled
         (exact-match for V1). Not a hard minimum. The BACK offset is a different animal — it is
         calculated, not a global constant (see Q6).
source:  Raj email reply (3.3): "Typical value is 4". Vary rarely is changes. Keep a global
         variable as 4" that can be changed if required under special circumstances." Q2's 2nd-reply
         exact-match decision fixes the operator — check the offset equals the configured 4" exactly.
         Raj's 2nd reply also resolved the back offset: a derived remainder with a pending global
         minimum, not the 2.375" we had assumed (Q6).

## Q6 — CT009 vs CT010: which is read off the drawing, which is derived?
status:  ANSWERED
blocks:  formula
issue:   #14
answer:  CT010 (countertop depth) is primary — set from cabinet depth + overhang. The offset sum
         CT007+CT008+CT009 is checked against it; if it EXCEEDS CT010 the program flags (sink hole
         too big → reviewer changes the sink). CT009 (back offset) is the constrained REMAINDER —
         whatever is left after front offset + sink depth — and carries a global MINIMUM (below it
         the faucet hole will not fit). That minimum value is STILL PENDING — on the call (2026-08-25)
         Raj said he had overlooked it and will email the vendor for the number.
source:  Raj email reply (3.6): "CT010 Should be equal to CT007 + CT008 + CT009. If the value
         exceeds, then the program should throw a flag." 2nd reply (follow-up 1): "after the front
         offset and depth of the sink taken care of, whatever left will become the back offset… never
         put backoffset as global offset, because it is a calculated value… I will give a global
         minimum for that variable after checking with the vendor." So back offset is NOT the fixed
         2.375" we assumed — it is derived, with a pending global-minimum guard.

## Q7 — Will GV supply manufacturer sink cut sheets per project?
status:  ANSWERED
blocks:  value
issue:   #15
answer:  Per project the reviewer provides the sink spec: they upload the sheet and the program
         reads the dimensions, OR the reviewer types the values into input fields for that drawing
         set. Reviewer-provided per project, not centrally by GV.
source:  Raj email reply (3.4): "For every project, sink dimensions changes. Shop drawing reviewer
         will upload the specs. Either your program will read the specs and grab the dimensions or
         the reviewer will input the necessary values in the input data fields for specific drawing
         set." For V1 the manual-input path is the safe MVP (no extraction risk).

## Q8 — Field dimension smaller than design (Condition 1 only covers larger)
status:  ANSWERED
blocks:  formula
issue:   #15
answer:  Same distribution logic as larger, but subtract: shrink the fillers first (down to the
         MINIMUM filler width, 1"), and if the difference still cannot be absorbed, reduce cabinet
         widths (site narrower than the arch drawing). Symmetric with Q21.
source:  Raj 2nd email reply (follow-up 3): "Same logic applies. First the difference should be
         adjusted with the fillers based on max and minimum fillers allowed. Keep minimum filler
         width as 1" and create a mutable variable which can be adjusted later. If the difference
         cannot be squeezed, then the cabinet widths has to be reduced because the width of the site
         is less than the architectural drawing." Raj offered to add an illustration if the flow is
         unclear.

## Q9 — How are non-adjustable cabinets identified on a drawing?
status:  ANSWERED
blocks:  formula
issue:   #15
answer:  The system does not auto-detect them — the reviewer chooses via the UI which cabinet may
         be adjusted, because some (e.g. a microwave cabinet) must not deviate or the appliance
         won't fit. Fillers are adjusted first; a cabinet only if the filler exceeds its maximum.
source:  Raj email reply (3.1): "Program should give flexibility through UI / UX to the shop drawing
         reviewer to decide which cabinet should be adjusted… some cabinets must not deviate from
         the designer's original dimensions. For instance, if the microwave cabinet width is
         adjusted… microwave might not fit." So identification is reviewer-driven, not automated.

## Q10 — "U.N.O." (unless-otherwise-noted) override handling
status:  ANSWERED
blocks:  formula
issue:   #15
answer:  Handled by the global-default + reviewer-override model, not by auto-reading a drawing note.
         Each check has a GLOBAL default (e.g. front offset 4"); before a run the reviewer may set
         PROJECT overrides (e.g. "3.5" is fine here"). The drawing is checked against the effective
         value, and where the drawing differs the system FLAGS it for the reviewer to accept or reject.
         The system also prints a summary of every global-vs-project override for the reviewer to
         verify. No override given → the global takes over; a required field left blank → the reviewer
         is prompted (a mandatory form field).
source:  Call 2026-08-25. Raj: "you have the global variables… the reviewer says I can manage with
         3.5 inch… wherever the global and project-specific variables differ, you can send a report,
         a summary of the variable discrepancies… if they don't give the input, global values take
         over… imagine filling a form, there are some mandatory entries." So the override is a
         reviewer-set input, not a note the system reads off the drawing.

## Q11 — Is ADA in V1 scope? (cabinet height 864 mm = the 34" ADA max)
status:  ANSWERED
blocks:  nothing
issue:   #15
answer:  Deferred. ADA is important and WILL be checked in the main version — Raj calls it one of the
         most important checks — but it is OUT of the demo / V1 so the demo isn't delayed by piling on
         checks. (The 4" front offset is itself an ADA rule and stays; full ADA compliance waits.)
source:  Call 2026-08-25. Raj: "you have to check eventually ADA, one of the most important… but if
         you put too many things now, a demo might get delayed… for the demo version don't put too
         many things. Basic thing works, small things we can add for the main version."

## Q12 — When mm and bracketed inches disagree, which governs?
status:  ANSWERED
blocks:  formula
issue:   #15
answer:  INCHES govern; ignore mm for the verdict. mm on GV drawings is only the vendor's machine
         reference. The check is inch-vs-inch; if the inches match, it passes.
source:  Raj 2nd email reply: "Ignore the metric dimensions, U.S construction works on I-P system,
         so we consider only feet and inches. The mm's shown on the drawing is for the vendors
         reference because their machines work better with mm's. As long as inches match, then we
         should be good." This dissolves the mm/inch conversion-noise problem ADR-0001 addressed —
         we never compare across units; mm may still corroborate that an inch was READ correctly
         (our call), but it is not a verdict operand. Pairs with Q2 exact-match — exact equality on
         inch fractions via Fraction (ADR-0001).

## Q13 — Two countertop-depth formulas: both? cross-check?
status:  ANSWERED
blocks:  formula
issue:   #15
answer:  Cross-check. CT010 (from cabinet depth + overhang) is primary, and the offset sum
         CT007+CT008+CT009 is checked against it — flag if the offset sum EXCEEDS CT010. The
         offset expression is a constraint on the primary depth, not an independent formula.
source:  Raj email reply (3.6): "CT010 Should be equal to CT007 + CT008 + CT009. If the value
         exceeds, then the program should throw a flag." Same answer as Q6, resolving whether the
         depth expressions reconcile.

## Q14 — Are non-three-wall layouts (island / two-wall / back-only) in V1 scope?
status:  ANSWERED
blocks:  nothing
issue:   #15
answer:  Yes — back-wall-only and island are in scope; Raj is supplying drawings for both. Two-wall
         was not mentioned. (Ties to Q20: the layout set is being finalized, which gates final tags.)
source:  Raj 2nd email reply: "We had drawings only with the walls on all three sides, I had to
         request our millwork manager to get the drawings with wall at the back only and also for
         island. We should be able to give them today or tomorrow." Supplying drawings for a layout
         is confirmation it is in scope. The 3-wall geometric refusal logic already built (#181) is
         layout-agnostic; the empirical numbers per layout still come from the drawings. Call
         2026-08-25: Raj has the shop drawings and will send the architectural set "tomorrow" (#274);
         two-wall is still not explicitly ruled in or out.

## Q15 — S19 "interior depth" should be width
status:  ANSWERED
blocks:  formula
issue:   #16
answer:  Confirmed live: cutout WIDTH = sink inside WIDTH − clearance each side (the spreadsheet's
         "interior depth" was the typo; width comes from width, depth from depth). The clearance
         defaults to 1/4" but is an EDITABLE per-project variable — it varies by fabricator (sometimes
         1/8"). Raj will confirm the exact fabricator value. Unblocks the cutout-width rule (A6.3 D5).
source:  Call 2026-08-25. Anant: "your diagram [shows] the cut-out width as an actual width… that
         matches the natural reading." Raj confirmed, and on the clearance: "1/4, but make sure that
         is editable, sometimes it's 1/8… a project-specific variable." Raj: "my CT012 is width."

## Q16 — S29 "cutout depth" should be width
status:  ANSWERED
blocks:  formula
issue:   #16
answer:  Resolved by the same confirmation as Q15 — the cutout WIDTH rule keys on width, so the
         "depth" wording in the fail line is the slip. Raj asked us to highlight the contradictory
         cells and send them back; he will correct the labels in his sheet.
source:  Call 2026-08-25. Raj: "I might have contradicted myself somewhere… just tell your tech team
         where it's contradictory, highlight that one and send it back, and I will fix it." Meaning
         confirmed (width from width); the label fix is Raj's, via the highlighted list.

## Q17 — I3 header says CT-2 Width but the rule computes Depth
status:  ANSWERED
blocks:  formula
issue:   #16
answer:  No issue — Raj checked it live and confirmed it is the COUNTERTOP depth check, correctly
         (not a sink dimension). Our confusion, not his error. He also confirmed his own variable
         naming: CT012 = width, CT008 = depth.
source:  Call 2026-08-25. Raj (reading SYNC010): "it says countertop depth only, it's not sink depth.
         No issues." Anant: "we just got confused, I guess." Raj: "my CT012 is width, and CT008 is
         the depth."

## Q18 — Three different checks all labelled CT-3
status:  ANSWERED
blocks:  formula
issue:   #16
answer:  We mint our own unambiguous ids (docs/decisions/A6_3_SINK_CUTOUT.md D4); Raj confirmed the
         three are genuinely separate checks (cutout depth, cutout width, offsets) and gave his real
         variable names (CT012 = width, CT008 = depth). Raj will fix the duplicate CT-3 labels in his
         own sheet once we send the highlighted list. Our tags stay provisional (Q20) until his final
         tags land.
source:  Call 2026-08-25. Raj asked what "labelled the same" meant, then confirmed his names and said
         "just highlight that one and send it back, I will fix it." No engine-level blocker — our ids
         are already distinct.

## Q19 — S27 "width of countertop" describes the sink cabinet
status:  ANSWERED
blocks:  formula
issue:   #16
answer:  Meaning confirmed — the sum (cutout width + the two clearances) is the sink-cabinet interior
         width, and the clearances belong to the cutout-vs-cabinet geometry Raj confirmed. The "width
         of countertop" wording is a label slip on Raj's sheet, going in the highlighted list for him
         to correct.
source:  Call 2026-08-25. Raj confirmed the CT011/CT012/CT013 clearance geometry earlier (email 3.8)
         and, on the call, that contradictory labels should be highlighted and sent back for him to
         fix. Geometry settled; only the label wording is Raj's to correct.

## Q20 — Two naming schemes coexist (letters A–G on Sheet1, CT0xx on the variable sheet)
status:  OPEN
blocks:  formula
issue:   #16
answer:  — (vocabulary NOT final; provisional for the demo only, per Raj)
source:  Sheet1 rules use letters (A cutout width, B front offset, C back offset, D cutout
         depth, E sink interior dim, F/G interior-face clearances); the variable sheet and
         master diagram image10.png use CT001–CT013. Raj email reply (3.8) confirms our
         CT011/CT012/CT013 reading ("Yes. Thats correct."), but (3.9): "Please dont hard code the
         vocabulary yet. Please use these for the demo, because final tags will be given once we
         finalize all the possible layouts." So final tags are explicitly deferred and gated on the
         layouts — keep semantic_types provisional; do not author final rules against these names.
         The A–G ↔ CT0xx letter mapping remains our inference, unconfirmed by Raj.

## Q21 — Calculate or check: does the system derive filler/cabinet sizes or only verify them?
status:  ANSWERED
blocks:  formula
issue:   #15
answer:  Calculate. When the site differs from the design (e.g. 88" design, 90" site) the program
         distributes the difference — adjust the fillers first (keeping cabinet sizes) within the
         filler bounds MIN 1" .. MAX (see note); if a filler would fall outside that, the reviewer
         chooses via UI which cabinet to adjust (some must not move). Works both directions (Q8).
         MIN 1" is firm. The MAX default is UNSETTLED: Raj's email said "use 2"", but the 2026-08-25
         call reportedly put the working max at 3–4" ("a 6" filler looks like a panel"). Both are
         mutable/project-tunable, so this only sets the default — but the default changes when a
         cabinet gets resized vs a filler widened. CONFIRM the max before authoring it.
source:  Raj email reply (3.1), full 3-step procedure, plus the 2nd reply follow-ups giving the
         numbers: max filler width "please use 2". It is not too big or too small"; min filler width
         "Keep minimum filler width as 1" and create a mutable variable which can be adjusted later".
         Call 2026-08-25 (per the meeting summary, NOT in the transcript segment on hand): max filler
         3–4", 6" reads as a panel — so treat the 2" email figure as a typical, not the ceiling, and
         reconcile the MAX default with Raj. Materially larger than a pass/fail checker: an
         interactive filler-then-cabinet distribution with reviewer choice.

<!-- CLIENT FACTS END -->

## What this changes about work already shipped

**ADR-0017 (#44) stands, with a narrower claim than it made.** Adopting `CT001`–`CT013` as the
canonical vocabulary is unaffected — the diagram defines them positionally and `rules/semantic_types.py`
carries only our own descriptive aliases.

What Q20 blocks is different and stricter than the ADR assumed: **the A–G ↔ `CT0xx` mapping is
inferred by us, never stated by Raj.** So a rule authored against Sheet1's letters — `A`, `F`, `G` —
cannot be resolved to `CT012`, `CT011`, `CT013` without his confirmation. The vocabulary is safe; the
letter mapping is not, and no rule may rely on it.

**Q1 is answered and changes CT-1.** Two field cuts, one per end, **added** to the wall-to-wall
dimension, 1″ typical and per-project customisable. The 5″ cabinet filler field-cut is a **different
element** and must not be folded into the same term.

**Raj's emailed answers land six clarifications; the two blockers do not.** Q3, Q5, Q6, Q7, Q9 and
Q13 are now ANSWERED from Raj directly, and Q21 records a real scope expansion — the system must
*calculate* filler/cabinet distribution, not only check it. But tolerances (Q2), the mm-vs-inch
question (Q12) and the real drawings / gold-set were not answered, so nothing yet moves off "Needs
Review". Q3.9 also confirms the `CT0xx` vocabulary is **not final** (final tags follow the layouts),
which reinforces keeping `rules/semantic_types.py` provisional — ADR-0017's aliases stay soft.

**2nd reply (2026-08-16) — the two blockers ARE now resolved for V1.** Tolerance (Q2): **exact match**
for iteration 1, reviewer clears false flags — so rules use exact equality, not `within_tolerance`,
and no client number is pending. mm-vs-inch (Q12): **inches govern, mm ignored** — the conversion-noise
problem is gone. Field-smaller (Q8), the filler bounds (Q21: MIN 1" / MAX 2"), and the layout scope
(Q14: back-only + island in scope) all landed too. Real drawings are promised "today or tomorrow"
(still #274, not yet in hand). **The V1 verdict model is therefore: exact inch-fraction equality
(Fraction), flag every mismatch, reviewer finalises** — a deliberately review-heavy, false-PASS-safe
posture, not tolerance bands. Neither ADR-0011's cross-unit allowance nor the `TOLERANCE_UNCONFIRMED`
sentinel is exercised in V1 (rationale in `docs/decisions/V1_VERDICT_MODEL.md`).
Two small residuals remain: the **back-offset global minimum** (Q6 — Raj checking the vendor) and the
checklist typos (Q15–Q19 — our separate list). A clarification meeting was offered.
