# CLIENT_FACTS.md — authoritative reconciliation of Raj's inputs

Single source of truth for what the client (Raj) has and has not specified. Supersedes all prior
scattered reconciliations. Where the source documents disagree, this file is authoritative about
*what is known* — it does not invent answers.

Sources are cited by artifact. Note: **most of Raj's substantive definitions live in the workbooks'
embedded diagrams, not in the cells** — those diagrams were read as images. A text-only pass misses
them (it is what wrongly recorded CT011–CT013 as "never defined").

- `status` = **ANSWERED** (Raj stated it explicitly, including in his own dimensioned diagrams) | **OPEN** (not explicitly stated — go ask).
- `blocks` = **formula** (answer changes what gets computed; a rule written first would compute the wrong quantity and pass confidently) | **value** (only supplies a number/label; the rule can be written now with a placeholder that refuses a verdict) | **nothing** (informational / scope).
- Artifacts: `Countertop_Checks_Updated.xlsx` (cells + 10 embedded diagrams), `Cabinet_Checks.xlsx` (cells + 3 diagrams), `Countertop_Checks.xlsx` (original), `Countertop_Checks_SAMPLE_Nexolv.xlsx` (**OURS**, not Raj).
- Last updated 2026-08-14.

---

## Q1 — Field cuts: how many, added to the run or trimmed from it?
status:  ANSWERED
blocks:  formula
issue:   #9
answer:  Two field cuts (one per end) for the 3-wall vanity, ADDED as extra to the wall-to-wall dimension; 1" each is typical but customizable per project.
source:  Countertop_Checks_Updated.xlsx CT-1 diagram (image1.png) labels the slab
         "1" Extra for Field Cut  +  84"  +  1" Extra for Field Cut", depth "24"".
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
         "PLACEHOLDER — please confirm". Not Raj's number.

## Q3 — Does cabinet width include the run's end panel (the 51 mm / 2" piece)?
status:  OPEN
blocks:  formula
issue:   #11
answer:  — (inference only, evidence conflicts)
source:  Cabinet_Checks B-Front-View (image1.png) labels the 51 [2] piece "END PANEL" and
         dimensions it SEPARATELY from the first cabinet "533 [21]" — suggesting separate.
         But the Cab 1 Width detail (image2.png) draws its arrow spanning both the 51 [2]
         and 533 [21]. Cell formula CT004 = CAB_SIDE_THK + CT011+CT012+CT013 + CAB_SIDE_THK
         counts a cabinet's OWN side panels inside its width. Raj never states whether
         CT003 (Cab 1 Width) includes the run's end panel.

## Q4 — Severity per check: critical vs advisory
status:  OPEN
blocks:  value
issue:   #12
answer:  —
source:  Exactly one severity-like instruction exists: variable sheet G43 (CT009 sink back
         offset) "…program should throw warning". No critical/advisory classification on
         any other check. The "Severity" column is ours (sample row L16), not Raj's.

## Q5 — Sink front offset: "4" minimum" or "= 4""?
status:  OPEN
blocks:  formula
issue:   #13
answer:  — (the document contradicts itself)
source:  Countertop_Checks_Updated Sheet1, CT-3 offset column: X19 "Front offset B.
         Typical dimension is 4" minimum" vs X27 "Front offset = 4"" and X29
         "Front offset <> 4"". Variable sheet CT007 also reads "Global Minimum / (U.N.O)".
         Raj never reconciles minimum vs exact (they diverge at exactly 4").

## Q6 — CT009 vs CT010: which is read off the drawing, which is derived?
status:  OPEN
blocks:  formula
issue:   #14
answer:  — (circular definition; neither designated as the read input)
source:  Variable sheet marks BOTH "Calculated" and each references the other:
         CT010 (D46; F46 = C.T_OH+CT007+CT008+CT009+B.S_THK) and
         CT009 (D43; G43 = CT010−C.T_OH−CT007−CT008−B.S_THK). Master diagram image10.png
         shows CT010 as overall "Countertop Depth" and CT009 as the back-offset gap, but
         designates neither as the independent measured value.

## Q7 — Will GV supply manufacturer sink cut sheets per project?
status:  OPEN
blocks:  value
issue:   #15
answer:  —
source:  Countertop_Checks_Updated text panel (image8.png): "Fabricator needs sink
         specification… every countertop brand has their sink specifications in their
         website. For training purpose, we can take K-2330-G from KOHLER as an example."
         States the sheets exist on brand sites and Kohler is an example — no commitment
         that GV supplies the relevant sheet with each project. Sink checks need the
         interior dimension (E) from that sheet.

## Q8 — Field dimension smaller than design (Condition 1 only covers larger)
status:  OPEN
blocks:  formula
issue:   #15
answer:  —
source:  Cabinet_Checks Sheet1 defines only "Condition : 1" (H17-H21): "if
         F_Wall_2_Wall_Dim > A_Wall_2_Wall_Dim then … Filler_Width_Right + Filler_Width_Left".
         No condition for field < design exists in any cell or diagram.

## Q9 — How are non-adjustable cabinets identified on a drawing?
status:  OPEN
blocks:  formula
issue:   #15
answer:  — (types named; detection method not stated)
source:  Cabinet_Checks H25 names the fixed-width types ("Sink Cabinets, Cabs with
         Equipment"). B-Front-View (image1.png) tags cabinets "PL-02" and "TRASH", but no
         rule states how the system should detect a non-adjustable cabinet in order to skip
         it when distributing extra width.

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
         "6012 [236 3/4]", "984 [38 3/4]". Raj never states which unit is authoritative.
         (mm-canonical is OUR internal choice, not his.)

## Q13 — Two countertop-depth formulas: both? cross-check?
status:  OPEN
blocks:  formula
issue:   #15
answer:  — (three depth expressions coexist; agreement not stated)
source:  Variable sheet CT010 (F46) = overhang + front + cutout depth + back + backsplash;
         Sheet1 I19/I23 depth = cabinet depth + overhang ("22" + 1" = 23""); Sheet1 N27 =
         "Sink cutout depth (D) + B + C". Raj never says whether all apply or must reconcile.

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
answer:  — (no canonical scheme or A–G ↔ CT0xx map stated)
source:  Sheet1 rules use letters (A cutout width, B front offset, C back offset, D cutout
         depth, E sink interior dim, F/G interior-face clearances); the variable sheet and
         master diagram image10.png use CT001–CT013 (which the diagram defines positionally).
         Raj never states which scheme is canonical or gives the mapping — we inferred it,
         so any rule that references "F"/"G" cannot resolve to CT011/CT013 without his confirmation.
