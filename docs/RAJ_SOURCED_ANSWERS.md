# What Raj has actually stated — sourced audit

> **Not authoritative for client facts.** `docs/CLIENT_FACTS.md` is. This file is research
> and history: it marks things "NOT STATED" that the embedded diagrams do state, and its
> Q-numbering is not the issue numbering. Where the two disagree, `CLIENT_FACTS.md` is right.


**Purpose:** separate what Raj *said* (with exact source) from what we've been *inferring*. Anything marked NOT STATED or IMPLIED goes back to Raj as a real question.

**Primary sources searched (all of them):**
- **[Zoom 07-24]** — "Anant BishtAI's Zoom Meeting 2026-07-24" transcript (verbatim). Participants: Abhishek, Ruchira, Rohan, Raj, Anant.
- **[May-03]** — GMT20260503 recording transcript. Contains none of these millwork terms (early discovery call).
- **[CT-orig]** — `Countertop_Checks.xlsx` (Raj, sent 07-24). One rule, CT-1/CT-2.
- **[CT-upd]** — `Countertop_Checks_Updated.xlsx` (Raj, Aug). Sheet1 = CT-1/2/3 rules; sheet "Countertop_Checks" = CT001–CT013 variable table.
- **[CAB]** — `Cabinet_Checks.xlsx` (Raj, Aug).
- **[OURS]** — `Countertop_Checks_SAMPLE_Nexolv.xlsx` (**Nexolv**, sent 07-25). NOT a Raj document.

> **Headline:** Raj has stated **no tolerance, no severity, and no wall-layout variance** in any source. The ±1/8″ figure is *our* placeholder from [OURS], not his answer.

---

## Group 1 — blocking the first check

### Q1 — Tolerances
**ANSWER:** NOT STATED. Raj gave no allowed-error value for any check (countertop width/depth, sink cutout, offsets, cabinet width, filler min/max). His checklists have **no tolerance field**. The only tolerance on record is our own placeholder.
**SOURCE:**
- [OURS] L15: `"±1/8" (0.125"). ⟵ PLACEHOLDER — please confirm your acceptable deviation, or the AWI grade it maps to (Economy / Custom / Premium)."`
- [Zoom 07-24] the closest Raj gets is an *example*, not a tolerance: `"architect wants 340mm, and the vendor makes 440mm, your system should throw the flag"` and `"overall dimensions has to match."` No number for "how close is close enough."
- [CT-orig]/[CT-upd]/[CAB]: no tolerance cell exists.
**CERTAINTY:** NOT STATED. (My prior memory saying "Raj answered ±1/8″ for the 3-wall case" was my synthesis — no primary source exists.)

### Q2 — Field cuts (total 85″ or 86″? added vs trimmed?)
**ANSWER (total):** NOT STATED / the source is internally ambiguous. The worked example literally reads as **two** 1″ field cuts (→ 86″), while the prose says one is typical (→ 85″). We interpreted 85″ in [OURS]; Raj never disambiguated.
**ANSWER (added vs trimmed):** NOT STATED / two sources conflict. Checklist says field cuts are **added to the countertop**; your drawing note says a **filler** is field-cut. Unreconciled.
**SOURCE:**
- [CT-upd] D15: `"Field cuts are typically added along the width of the countertop. 1" is typical"` (singular, *added*).
- [CT-upd] D21: `"If the wall to wall dimension is 84\", total width = 1\" (field cut) + Wall to Wall dimension : 84\" + 1\" ( field cut)"` — reads as 1+84+1 = 86.
- [OURS] B10: `"Example: wall-to-wall 84\"  →  total = 84\" + 1\" (field cut)"` — our reading = 85.
- Drawing note you cite: `"5\" FILLER PANEL TO BE FIELD CUT"` (shop drawing PDF) — implies trimming a filler, not adding to the top.
**CERTAINTY:** NOT STATED.

### Q3 — Does "cabinet width" include the end panel?
**ANSWER:** IMPLIED yes (from a formula), but not stated for the drawing you're reading. Raj's variable table builds a cabinet width *inclusive of side-panel thickness on both sides*; it does not tell you whether the "Cab 1 Width" arrow on this specific drawing spans the 51 mm end segment.
**SOURCE:** [CT-upd, Countertop_Checks sheet] F38: `"CT004 = CAB_SIDE_THK + CT011 + CT012 + CT013 + CAB_SIDE_THK"` (side-panel thickness counted twice, bracketing the inner segments). B47/C47: `"CAB_SIDE_THK … Cabinet Side Panel Thickness"`.
**CERTAINTY:** IMPLIED (from the formula). The drawing-specific reading is NOT STATED.

---

## Group 2 — remaining checks

### Q4 — Severity per check
**ANSWER:** NOT STATED, with one exception. No severity/criticality field exists in Raj's checklists (the "Severity" row is *ours*, [OURS] M16). The **only** severity-like instruction Raj authored is a **warning** on the sink back offset. No others.
**SOURCE:**
- [CT-upd, Countertop_Checks sheet] G43 (CT009 Sink Back Offset): `"If CT010 - C.T_OH - CT007 - CT008 - B.S_THK is less than Global MIN Constant, program should throw warning"` (STATED — this is the back-offset warning you remember).
- [OURS] M16: `"Severity (NEW) … Revise & Resubmit … ⟵ or 'Note' if minor"` (our placeholder, awaiting his fill).
**CERTAINTY:** NOT STATED for all except the CT009 back-offset warning (STATED, in his document).

### Q5 — Sink front offset: minimum or exact? (and back offset 2.375)
**ANSWER:** The document **contradicts itself**. "Required Inputs" says *minimum*; "Pass/Fail Criteria" says *exact equality*. Raj never resolved which governs.
**SOURCE:** [CT-upd] Sheet1, CT-3 offset column —
- X19: `"Front offset B. Typical dimension is 4\" minimum"`
- X20: `"Back offset C. … Typical is 2.375"`
- X27 (Pass): `"Front offset = 4\". Back offset = 2.375"`  ← exact
- X29 (Fail): `"Front offset <> 4\". Back offset <> 2.375"`  ← exact
- Also [Countertop_Checks sheet] CT007 D41/G41: `"Global Minimum" / "Global Constant. ( U.N.O )"` (leans *minimum*).
**CERTAINTY:** NOT STATED (which of min vs exact governs). Both readings are quoted above.

### Q6 — CT009 (sink back offset) vs CT010 (countertop depth): which is read, which is derived?
**ANSWER:** NOT STATED — the checklist marks **both** "Calculated" and each formula references the other (circular). Neither is marked as measured/read-off-drawing.
**SOURCE:** [CT-upd, Countertop_Checks sheet] —
- CT009 D43=`"Calculated"`; G43 derives it: `"CT010 - C.T_OH - CT007 - CT008 - B.S_THK"`.
- CT010 D46=`"Calculated"`; F46=`"CT010 = C.T_OH + CT007 + CT008 + CT009 + B.S_THK"` (uses CT009).
- (Contrast CT001 D35=`"Measured" / "Field Installation"` — the one thing explicitly read.)
**CERTAINTY:** NOT STATED (circular definition; one must be the read input but he doesn't say which).

### Q7 — Sink spec sheets (e.g. Kohler K-2330-G) supplied per project?
**ANSWER:** NOT STATED. No commitment to supply them. The checklist says sink interior dimensions come from "production specification" / client, which *implies* an external sheet, but Raj never committed to providing it each project. (No "Kohler"/"K-2330" string appears in any checklist.)
**SOURCE:** [CT-upd] N23/S23/X23: `"Sink interior dimension ( E ) should comply with the production specification"`; [Countertop_Checks sheet] CT008 E42: `"G.C / Client"`.
**CERTAINTY:** NOT STATED.

### Q8 — Room smaller than designed
**ANSWER:** NOT STATED. The cabinet checklist defines only **Condition 1 = field larger than arch** (extra goes into fillers). There is no condition for smaller.
**SOURCE:** [CAB] H17–H21: `"Condition : 1 … if field dimension F_Wall_2_Wall_Dim > A_Wall_2_Wall_Dim then Wall2Wall Dim_Diff = F - A = Filler_Width_Right + Filler_Width_Left"`. No Condition 2 exists.
**CERTAINTY:** NOT STATED.

### Q9 — Non-adjustable cabinets: how identified on the drawing?
**ANSWER:** Partially stated. Raj **names the types** that can't change width, but **not how to detect them** on a drawing.
**SOURCE:** [CAB] H25: `"1. There are cabinets where widths cannot be adjusted like Sink Cabinets, Cabs with Equipment"`.
**CERTAINTY:** STATED (which types) / NOT STATED (how they're flagged on the drawing).

### Q10 — Two depth formulas — both? cross-check?
**ANSWER:** Both formulas are in his document (actually 2–3 depth expressions). Whether they must cross-check is NOT STATED.
**SOURCE:** [CT-upd] Sheet1 —
- I23 (via cabinet+overhang): `"If the depth of the cabinet is 22\", overhang is 1\", Depth of the countertop is 23\""`.
- N27 (via sink cutout): `"Depth of countertop = Sink cutout depth ( D ) + B + C"`.
- [Countertop_Checks sheet] CT010 (via offsets): `"CT010 = C.T_OH + CT007 + CT008 + CT009 + B.S_THK"`.
**CERTAINTY:** NOT STATED (that they should reconcile).

### Q11 — "U.N.O." (Unless Otherwise Noted)
**ANSWER:** The term is used as "a default that a drawing note may override," and the cabinet sheet spells the pattern out — but Raj gave **no handling instruction** for how the system should detect/apply the override.
**SOURCE:** [Countertop_Checks sheet] G41 `"Global Constant. ( U.N.O )"`, G43 `"Global MIN Constant. ( U.N.O )"`; [CAB] H22–H23: `"Note: Unless otherwise noted, Filler_Width_Right = Filler_Width_Left"`.
**CERTAINTY:** STATED (the convention exists) / NOT STATED (how to handle it).

### Q12 — ADA in scope?
**ANSWER:** NOT STATED. No mention of ADA or the 34″ max in any checklist or in the transcript.
**SOURCE:** No occurrence in any source. (The 34″/ADA figures you cite are on the sink/spec sheet, not in Raj's checklist or words.)
**CERTAINTY:** NOT STATED.

### Q13 — Other layouts (island / two-wall / back-only) — now or later?
**ANSWER:** NOT STATED. **Every** countertop rule is scoped to walls on three sides; no other layout exists in the checklist, and Raj never said when they'd come.
**SOURCE:** [CT-upd] D5/I5/N5/S5/X5 all identical: `"Countertop with walls on left, right and back side"`.
**CERTAINTY:** NOT STATED. (Note: this also *corrects* our earlier assumption that "CT-1 vs CT-2 = same check, different wall layouts." In [CT-upd], CT-2 is **Depth**, not a layout variant — so that assumption was wrong.)

### Q14 — mm vs inches: which governs?
**ANSWER:** NOT STATED. Raj reads both units aloud but never says which is authoritative when they disagree. (Our note "inch & mm never disagree" was our own claim — and your measured 1.6 mm gap disproves it.)
**SOURCE:** [Zoom 07-24] Raj: `"the depth, let's say you see 1 inch and 5 3 tenths of an inch, or 440mm"` — dual units, no governing rule.
**CERTAINTY:** NOT STATED.

---

## Group 3 — suspected checklist errors (did Raj correct any?)

**Overall:** No Raj communication correcting any of these exists on record. Each is present in his checklist exactly as you describe; none is marked resolved.

1. **Sink cutout *width* = "sink interior *depth* (E) − 0.25 − 0.25"** — PRESENT, uncorrected. SOURCE [CT-upd] S19: `"Sink cutout width ( A ) = sink interior depth ( E ) - 0.25\" - 0.25\""`. (Width defined from a *depth* — looks wrong.) CERTAINTY: NOT STATED (no correction).
2. **Fail criteria "Width of countertop <> Sink cutout *depth* (A) + F + G"** — PRESENT, uncorrected; and note the Pass line uses *width*. SOURCE [CT-upd] S29: `"Width of countertop  <> Sink cutout depth ( A ) + F + G"` vs S27: `"Width of countertop  = Sink cutout width ( A ) + F + G"`. CERTAINTY: NOT STATED.
3. **Header "CT-2: Countertop Width Verification" over a Depth rule** — PRESENT, uncorrected. SOURCE [CT-upd] I3 header `"CT-2 : Countertop Width Verification"` but I9 rule name `"Countertop Depth Verification"`. CERTAINTY: NOT STATED.
4. **Three different checks all labelled CT-3** — PRESENT, uncorrected. SOURCE [CT-upd] N3 `"CT-3 : Countertop sink cutout depth ( D )"`, S3 `"CT-3 : Countertop sink cutout width ( A )"`, X3 `"CT-3 : Countertop sink cutout front /back offset"`. CERTAINTY: NOT STATED.
5. **"Width of countertop = Sink cutout width (A) + F + G" reads like the sink cabinet, not the countertop** — PRESENT, uncorrected. SOURCE [CT-upd] S27 (as above). CERTAINTY: NOT STATED.
6. **Are CT001–CT013 codes final / renamed?** — NOT STATED. The table uses CT001–CT010 plus named vars (B.S_THK, C.T_OH, CAB_SIDE_THK); CT011–CT013 are referenced in a formula (F38) but never defined as rows. No indication of renaming. CERTAINTY: NOT STATED.

---

## Group 4 — commitments

### Q15 — What did Raj commit to send, and when?
**ANSWER:**
- **Revise/expand the checklist:** STATED. `"Anand, that's just the beginning. Please go to the format. Just put your comments on it. I will make the changes accordingly."` and `"That's the basic stuff. I will add a few more … once you let me know that this format is good."` — no date given.
- **Complete real project (shop + arch set) for countertop & cabinet:** STATED by **Abhishek** (not Raj): `"we will take cabinet and countertop from the millwork side, and they'll provide you with shop drawings and architectural sets for those."` No date. (To date, shop drawings arrived; a matched **arch set** has not.)
- **5–10 previously reviewed drawings with mark-ups:** **NOT STATED.** No such commitment appears in the transcript or any file. This is ours to ask for.
**SOURCE:** [Zoom 07-24] lines as quoted above.
**CERTAINTY:** STATED (checklist expansion, by Raj; project shop+arch, by Abhishek) / NOT STATED (gold-set of reviewed drawings; all dates).

### Q16 — Anything implying more automation than "compute a value, a human signs off"?
**ANSWER:** Yes — two signals worth a scope conversation:
1. **The checklist expects the system to *derive* dimensions, not only check them.** Fillers and depth are marked "Calculated," and the cabinet sheet says extra width must be *distributed* across cabinets — i.e. the system is being asked to *produce* filler/cabinet dimensions. That is exactly your worry.
   - SOURCE: [CAB] T19/T20: `"Filler_Width_Right / _Left … Calculated Value"`; H24: `"Wall2Wall_Dim_Diff > Filler_Width_Max X 2, then the extra width need to be distributed along the cabinets"`; [Countertop_Checks sheet] CT002/CT006 fillers = `"Calculated"`.
2. **Ruchira asked for interdependency prompting** ("if I modify item A, prompt me to modify item B") — broader than checking. It was **deferred**, not scoped in.
   - SOURCE: [Zoom 07-24] Ruchira: `"if I modify a shop drawing that … would affect the design of another item … Would it prompt me to modify that as well?"`
**Counter-signal (keeps V1 narrow):** Abhishek explicitly reduced V1 to comparison: `"Let's start with our agent looking at both the files based on the checklist … let the agent only compare the two drawings … for now, comparing is the thing."`
**CERTAINTY:** STATED (all three quotes). The *derive-vs-check* tension is real and unresolved — raise it before the demo.
