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
  production (ADR-0011). The gate reports READY and says the story ships provisional.
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
status:  OPEN
blocks:  value
issue:   #10
answer:  —
source:  No tolerance figure appears in any Raj-authored file (cells or the 10 embedded
         diagrams). The ±1/8" figure circulating in our docs came from
         Countertop_Checks_SAMPLE_Nexolv.xlsx, which WE authored, labelled
         "PLACEHOLDER — please confirm". Not Raj's number. Re-asked in the email; Raj did not
         answer section 2 (no "Please see my response" marker on it). His severity answer (3.5)
         "flag any deviation" is in tension with a tolerance band and is NOT a band — re-asked
         reframed. Still the main blocker: nothing can pass or fail until this lands.

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
status:  OPEN
blocks:  value
issue:   #12
answer:  — (Raj declined the split; interim posture below)
source:  Raj email reply (3.5): "Difficult to categorize between production blockers vs flagged for
         review. I think any dimension, cabinet type, sink type deviations should be highlighted."
         So the interim rule is flag-all-deviations; the per-check CRITICAL vs advisory split we
         asked for is explicitly not provided — so critical_false_pass_rate still has no CRITICAL
         rule to measure. Ships provisional (blocks: value).

## Q5 — Sink front offset: "4" minimum" or "= 4""?
status:  ANSWERED
blocks:  formula
issue:   #13
answer:  A configurable global default of 4" — a target value the offset is checked against, not a
         hard minimum. The tolerance band around it is Q2 (still open).
source:  Raj email reply (3.3): "Typical value is 4". Vary rarely is changes. Keep a global
         variable as 4" that can be changed if required under special circumstances." The
         min-vs-exact contradiction resolves to one project-overridable target (4"). Raj did not
         address the back offset (2.375") — assume the same pattern but confirm before relying.

## Q6 — CT009 vs CT010: which is read off the drawing, which is derived?
status:  ANSWERED
blocks:  formula
issue:   #14
answer:  CT010 (countertop depth) is primary — set from cabinet depth + overhang. The offset sum
         CT007+CT008+CT009 is checked against it; if it EXCEEDS CT010 the program flags (sink hole
         too big → reviewer changes the sink). CT009 is the constrained value, CT010 the read one.
source:  Raj email reply (3.6): "Countertop depth is decided based on the depth of the cabinet and
         countertop overhang. CT010 Should be equal to CT007 + CT008 + CT009. If the value exceeds,
         then the program should throw a flag." Breaks the earlier circular definition.

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
status:  OPEN
blocks:  formula
issue:   #15
answer:  —
source:  Cabinet_Checks Sheet1 defines only "Condition : 1" (H17-H21): "if
         F_Wall_2_Wall_Dim > A_Wall_2_Wall_Dim then … Filler_Width_Right + Filler_Width_Left".
         No condition for field < design exists in any cell or diagram. Raj email reply (3.1) walks
         the field-LARGER case only (88" design, 90" site, extra distributed to fillers then
         cabinets); the field-SMALLER case is still unaddressed.

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
status:  OPEN
blocks:  formula
issue:   #15
answer:  —
source:  "U.N.O." marks a default that a drawing note may override — variable sheet G41
         (CT007) "Global Constant. (U.N.O)", G43 (CT009) "(U.N.O)"; Cabinet_Checks H22-H23
         "Note: Unless otherwise noted, Filler_Width_Right = Filler_Width_Left". Raj never
         states how the system should detect a note and apply the override.

## Q11 — Is ADA in V1 scope? (cabinet height 864 mm = the 34" ADA max)
status:  OPEN
blocks:  nothing
issue:   #15
answer:  — (scope decision, not stated)
source:  "864 [34]" appears only as the cabinet HEIGHT in Cabinet_Checks B-Front-View
         (image1.png). "Recommended ADA Installation 34" (864 mm)" appears on the Kohler
         reference sheet (image8.png). Neither states that ADA compliance is a V1 check;
         the height matching 34" may be coincidental.

## Q12 — When mm and bracketed inches disagree, which governs?
status:  OPEN
blocks:  formula
issue:   #15
answer:  —
source:  All shop-drawing dimensions are dual and the inches are rounded, so they disagree:
         Cabinet_Checks image1.png "533 [21]" (533 mm = 20.98", shown as 21"),
         "6012 [236 3/4]", "984 [38 3/4]". Raj never states which unit is authoritative
         (mm-canonical is OUR internal choice, not his). Re-asked in the email; Raj's reply did not
         answer it. Weak signal only: every number in his reply is in inches (88", 90", 4"), never
         mm. ADR-0001 keeps us safe meanwhile (arithmetic in the authored unit; mixed units abstain).

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
status:  OPEN
blocks:  nothing
issue:   #15
answer:  — (only the 3-wall case is documented)
source:  Every rule context cell reads "Countertop with walls on left, right and back side"
         (D5/I5/N5/S5/X5); the layout image is titled "1 Vanity with a wall on 3 sides"
         (image6.png). No island/two-wall/back-only rule or scope statement exists.

## Q15 — S19 "interior depth" should be width
status:  OPEN
blocks:  formula
issue:   #16
answer:  — (suspected typo; not confirmed by Raj)
source:  Countertop_Checks_Updated S19: "Sink cutout width (A) = sink interior depth (E)
         − 0.25" − 0.25"" derives a WIDTH from an interior DEPTH. Master diagram image10.png
         shows A / CT012 is a horizontal WIDTH. Raj has not confirmed the correction.

## Q16 — S29 "cutout depth" should be width
status:  OPEN
blocks:  formula
issue:   #16
answer:  — (suspected typo; not confirmed by Raj)
source:  S29 (fail line of the sink cutout WIDTH rule): "Width of countertop <> Sink cutout
         depth (A) + F + G" labels (A) as "depth", while the pass line S27 uses "Sink cutout
         width (A)". Inconsistent; not confirmed by Raj.

## Q17 — I3 header says CT-2 Width but the rule computes Depth
status:  OPEN
blocks:  formula
issue:   #16
answer:  — (suspected mislabel; not confirmed by Raj)
source:  I3 header "CT-2 : Countertop Width Verification" sits over a rule whose body is
         depth — I9 "Countertop Depth Verification", I11 "…Depth of the countertop",
         I19/I23 depth logic. The name feeds our vocabulary, so the mismatch matters.

## Q18 — Three different checks all labelled CT-3
status:  OPEN
blocks:  formula
issue:   #16
answer:  — (id collision; not renamed by Raj)
source:  Three headers share the CT-3 id: N3 "CT-3 : Countertop sink cutout depth (D)…",
         S3 "CT-3 : Countertop sink cutout width (A)…", X3 "CT-3 : Countertop sink cutout
         front /back offset…". As rule keys they collide. Raj has not disambiguated them.

## Q19 — S27 "width of countertop" describes the sink cabinet
status:  OPEN
blocks:  formula
issue:   #16
answer:  — (suspected mislabel; not confirmed by Raj)
source:  S27 "Width of countertop = Sink cutout width (A) + F + G". Per master diagram
         image10.png, A = CT012 (sink hole width) sits between F = CT011 and G = CT013
         (clearances from the sink cabinet's interior faces) — so this sum is the SINK
         CABINET interior width, not the countertop width. Not confirmed by Raj.

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
         distributes the extra — add to the fillers first (keeping cabinet sizes); check each
         adjusted filler does not exceed a MAXIMUM filler width; if it does, the reviewer chooses
         via UI which cabinet to adjust (some must not move).
source:  Raj email reply (3.1), full 3-step procedure. Materially larger than a pass/fail checker —
         an interactive filler-then-cabinet distribution with reviewer choice. Residual open value:
         the maximum-filler-width default is "the designer's choice" with no number given, and is
         needed before the demo can run this path.

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
