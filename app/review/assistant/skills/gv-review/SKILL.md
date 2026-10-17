---
name: gv-review
description: How GV reviews a millwork vendor's shop drawings (countertops, cabinets) against the architect's drawings and GV's own rules. Read this before you answer any question about the drawing set in /mnt/user-data/uploads, compare the vendor with the architect, or make a cloud drawing (a marked-up PDF).
---

# GV review: the vendor's shop drawing against the architect's drawing

GV gets **shop drawings** from its vendors (countertop and cabinet makers). A GV reviewer checks them
against the **architect's drawings** (the design) and GV's rules, and marks up what is wrong. You do
that review as an AI helper: you find and explain differences; a GV reviewer decides.

Files in this folder (read-only):
- `SKILL.md`: this file. How to work.
- `rules.md`: GV's rules and the numbers in force **for this drawing set** (company standards,
  project values). Read it before you judge any number. Its numbers win over anything written here.
- `checklists/` (when present): GV's own checklists for countertops and cabinets.

## 1. The three layers on a sheet

| Layer | What it is | How you use it |
|---|---|---|
| Architect's drawing | The design ("ID set", "arch set"). Fewer details. | The truth for what must be built. |
| Vendor's shop drawing | The fabricator's detailed build drawing. | What you check, number by number. |
| GV's markup | Notes, clouds, boxes and corrected numbers a GV reviewer added. | GV's notes. Never a design value, never the vendor's value. |

Also seen: **brand prototype / brand standard** sheets (a hotel or restaurant brand's typical
design). They are a reference, not this job's architect drawing: never call them the design. But
**do compare the vendor with them**, mostly where the architect's drawing does not show the item
(no size, no detail): heights, widths, splashes, tags, sinks, door and drawer layout. Report those
differences in their own group, "Differs from the brand prototype", worded "the brand prototype
shows ..., the vendor shows ...; confirm which governs". A reviewer wants to know about them.

## 2. Find which file and pages are whose

Two ways a set arrives:
- **Two PDFs**: one architect set, one vendor set. File names often say so; confirm by title block.
- **One combined PDF** (GV's review format): each sheet carries the architect's drawing and the
  vendor's drawing pasted in as `/Stamp` annotations, each under a heading GV typed as a large
  FreeText note: "ID SET ELEVATION", "ARCH SET", "VENDOR'S SHOP DRAWING (ELEVATION)", "BRAND
  PROTOTYPE (ELEVATION)", or similar. A heading sits above its drawing, or below it as a caption.

Signs of whose drawing it is: the title block (the architect's firm and sheet numbers such as A-xxx
or ID-xxx vs the vendor's company, "SHOP DRAWING", "FOR APPROVAL", the vendor's job number and
revision dates); the level of detail (the vendor shows construction, joints, edge profiles, hardware).
A vendor set often holds a cover, an index, general notes, plans, elevations, sections and details,
and sometimes the brand's prototype sheets copied in.

**Label each half of a sheet, not the sheet.** A combined page often holds two or three drawings,
each under its own heading. An "ARCH SET" or "ID SET" drawing beside a "BRAND PROTOTYPE" drawing on
the same page is the architect's: compare the vendor with it; the prototype is only a reference.
Never call a whole page "prototype" because one half is.

`pdfinfo F.pdf` gives pages; `pdftotext -f 3 -l 3 -layout F.pdf -` a page's words (stamps too).
Every annotation (page, kind, author `/T`, text, box) is in `/mnt/user-data/set/annotations.json`;
headings and GV's notes are FreeText (pypdf reads each page's `/Annots` if you need more).
**The page map is made for you:** read `/mnt/user-data/set/page_map.json` first (per page and per
drawing on it: role architect / vendor / brand_prototype / general from GV's typed headings, round,
area, sheet numbers; boxes in PDF points, origin top-left). It is code's reading of GV's labels: if
a drawing says otherwise, the drawing wins. Never build your own. `status.json` there says whether
the pack is ready; until it is, use the uploads (and `zoom`, which works at once).

## 3. Pair each vendor sheet with the architect's sheet

- Combined sheet: the two drawings on the same page are the pair (stacked or side by side).
- Separate files: match by room or area name, the item's name (vanity, kitchenette, coffee
  station, reception desk...), the elevation or detail callout (the vendor usually quotes the
  architect's sheet and detail number), the plan location and the overall shape.
- Never pair two drawings only because a number matches; unsure: "pairing not certain", and why.

## 4. Read the numbers

- Feet and inches: `3'-2 1/2"` = 38 1/2" = 38.5". `10'-0"` = 120". Give every answer **in inches**,
  with the printed form beside it when it helps: 38 1/2" (printed 3'-2 1/2").
- Two units, `610 [24]`: mm first, inches in brackets. **Inches count**; the mm are only a check.
- Fractions are usually to 1/16"; a stacked one (1 over a bar over 2) is easy to misread: zoom.
- On some sets the numbers are drawn as lines (pen strokes), not text: `pdftotext` returns few or no
  numbers. Then read them from a picture: `zoom` on the area (300 dpi for a small one) and look.
- EQ (equal), TYP (typical), U.N.O. (unless noted otherwise), VIF (verify in field), CL / ℄ (centre
  line), AFF (above finished floor), CLR (clear), O.C. (on centre), NIC (not in contract), BY OTHERS.
- Check a dimension string adds up to its overall. A string that does not add up is a finding on
  that drawing, whichever side it is on (the architect's drawing can be wrong too: say so).
- **Never invent a number.** If you cannot read it, say "unreadable" and which page and spot.

## 5. GV's markup

**What it is:** marks added by GV reviewers when they reviewed the set:
- annotations: FreeText notes and the headings, Square / Circle / Polygon / PolyLine / Line / Ink
  clouds and boxes, callouts (list them with the script above; `/T` is the author);
- on some sets, coloured ink baked inside the pasted drawing: red, blue or green numbers written
  beside or over the vendor's, yellow boxes painted over the vendor's black numbers. Drawings are
  plotted in black and grey, so **anything coloured is GV's markup**.

**The rule:** GV's markup is GV's reviewer comments. Never use a number from it as what the
architect designed or what the vendor drew; the architect's own drawing is the design. You may say
whether GV already flagged an item ("GV flagged: yes / no"). A new vendor submittal has none.

**Compare on pictures with GV's markup hidden.** The drawings themselves are `/Stamp` annotations,
so `pdftoppm -hide-annotations` gives a blank page. The set pack has a copy that keeps only the
stamps (`/mnt/user-data/set/<file>_drawings_only.pdf`, its pages and `zoom` use it; its
`annotations.json` keeps the headings and GV's notes; pypdf makes one by keeping only `/Stamp`).
A page with no stamps is ordinary page content: render it as it is. Coloured ink inside a stamp
stays in the copy; treat it as GV's.

**A vendor number under GV's markup** (a yellow box, a cloud, a red note over it) is still the
vendor's number to find, not "unreadable" at first sight: `read_under_markup` on that spot (or
the drawings-only text, `pdftotext -bbox`), `zoom` at 300 dpi, and its millimetres against its
inches (`610 [24]`: 610 / 25.4 = 24). Only when all three fail: "unreadable", with the page and spot.

## 6. GV's rules first, then everything else against the architect

1. Read `rules.md` (and `checklists/` when present). Apply each rule that fits the item: it says
   what is compared, the formula, and the numbers in force (field cut per wall end, filler limits,
   sink clearance, sink front and back offsets...). GV compares **exactly**: no tolerance unless
   `rules.md` gives one. A value the rule needs that nobody gave is missing: say so, never assume.
2. `review_results` gives what GV's code already recorded for this set. Quote it as recorded; never
   contradict it or restate a result in your own words. Much of a set is not checked by code.
   Its AI page review rows marked `unsure` or `not_shown` are leads, not results: settle each on
   the drawing (a finding, or a reading that shows it matches).
3. Then compare **everything else** on the vendor's drawing with the architect's.

## 7. What to compare (not only overall widths)

**"The architect prints no size here" never means "nothing to compare".** Architects often draw
millwork without dimensions; then compare what is drawn: how many bays, doors, drawers and
shelves, door vs drawer, uppers present and where they start and stop, splashes, the sink and its
position, appliances, collars and holes, tags and notes (scaled sizes: last bullet below). Do
this on **every** paired page, including the second round (revision) pages.

**Every architect dimension is its own item.** Compare a chain segment by segment (wall to outlet,
outlet to outlet, floor to a J-box), never only its total. A dimension that locates a thing (an
outlet, a sink or faucet centre line) is that thing's position: when the vendor does not show the
thing, that is its own finding ("not shown on the vendor", with the architect's values), never a
remark in a width finding. Read the architect's **notes and callouts** as closely as its numbers
("pull-out drawers", "by others"): a note the vendor does not follow is a difference.

- **Sizes**: overall length, each piece and cabinet width, **depth** (countertop = cabinet + overhang),
  **heights** (counter, upper cabinets' bottom and top, backsplash), thicknesses (top, splash).
- **Sink and appliances**: sink centre line from the wall or end, sink model or tag (SNK-...),
  undermount vs drop-in, cut-out size, faucet holes; appliance models and openings, their centre
  lines; rim collars and holes (a collar must fit its hole).
- **Material and finish tags**: CT-, ST-, SS-, QZ-, PL-, LA-, WD-, MT- and the like. The vendor's
  tag on a piece must match the architect's for the same piece.
- **Backsplash and side splash**: present or not, height, thickness, material (stone vs tile). A
  side splash only where the top meets a wall; a free-standing end gets none.
- **Cabinets**: door vs drawer, number of doors and drawers, hinge side, open shelves, end panels,
  toe kick, equipment cabinets (sink, appliance) that must not change size.
- **Hardware and accessories**: pulls (type, size, finish), locks (cam lock), grommets, power and
  data units, legs and supports (shape, size, finish), shelves (fixed vs pull-out).
- **Position, not only size**: an item centred on or aligned with another (microwave over the range,
  sink on its centre line, cabinet over fridge); one that hits a neighbour or misses its opening.
- **Extent and count**: a section the architect draws that the vendor omits, extra vendor pieces;
  how many doors, drawers, bins, fronts and how arranged; which way an island faces (front/rear).
- **Fillers and site fit**: filler widths (see `rules.md` for limits), scribe, field cut (extra
  length cut on site; per wall end).
- **Overhangs, supports, cut-outs**: overhangs, knee space, brackets, corbels; sink, cooktop, outlets.
- **Notes and scope**: "by others", VIF, NIC, edge profile, seams, any architect note the vendor
  dropped or changed.
- **Construction notes** the vendor contradicts, on the architect's or brand sheet ("toe kick
  integrated with door", "door to the floor", "pull-out drawers for bins", "metal lining").
- **ID set vs brand prototype** conflicts: status `check` (Your decision), `check_reason:
  which_governs`.
- **Scaled values**: where the architect prints no size, `measure_on_drawing` (two printed dimensions
  of the same view) gives "about X (±T)": status `check` (Your decision), never `different` (Wrong).
- **Extent first**: before a height or width is Wrong, check what the architect's dimension spans
  (`measure_on_drawing` with `dimension`). Floor to the top of a box, soffit or blocking beyond the
  unit is not the unit: status `check` (Your decision), `check_reason: different_extent`.
- **Printed vs printed is Wrong**: a vendor value printed unlike the architect's printed value for the
  same span (counter height 34" vs 2'-9") is `different` (Wrong), not `check`, unless a
  `check_reason` holds.

## 8. How much work: scale it to the question

- **A one-line fact** (a page count, one value): 1-3 tool calls, no helpers.
- **One item or one page pair** ("is the sink on pages 27-28 the architect's?"): you alone. Open
  only the pages asked (page map, then one `zoom` with every crop you need), 1-3 findings for the
  items asked (not the rest of the page), cite, answer, in a few turns.
- **The whole set** ("what did the vendor get wrong?", a full check): helpers, by page pair. The
  tool calls of one reply run at once: ask for all you need together (8 crops per `zoom`).

## 8a. Working the whole set

In a full check the app has done steps 1-3 (split, helpers, checklist): start at step 4.
1. Read `page_map.json` and look at the contact sheets (`/mnt/user-data/set/contact/`): the set
   at a glance. `TaskCreate` a short plan.
2. Split the page pairs across helpers (`Agent`), **3-5 pages per helper, all started in one turn**,
   each with both drawings of its pages. Brief each in four parts: objective (its pages, what to
   compare), output (findings with boxes, `set_coverage`), sources (files, skill sections 4, 5, 7,
   9, 10), boundaries (only its pages, never guess). The app adds its page map, pictures, `zoom`
   and a budget. Mention non-dimension differences and brand prototypes. Give it GV's recorded
   results on its pages as "already recorded: do not report them again, but still compare
   everything around them, the strings that build them too": a recorded item never closes its area.
3. **The per-page checklist.** Each helper ends its report with one line per page,
   `CHECKLIST p<N>: architect a/b; notes a/b; strings a/b; not compared: <what and why, or none>`
   (the helper brief says what each count covers). A line with a gap is work left, not done.
4. **First** the gaps: each "not compared" line and checklist gap in the helper reports, and each
   re-check you planned (a money or time cap can stop you before the end). Then **verify the leads,
   in batches**: a helper's finding is a lead until something backs it; one `zoom` call of up to 8
   crops checks 4 findings (both sides), then `update_finding` with `verified: true` (or correct
   it), or `drop_finding` when it is wrong. A finding the app lists as backed (a second read
   agrees, or a code check) needs no re-check; one you had no time to check stays unverified.
5. **Whole-set questions cover every page pair** (section 7): every page in `set_coverage`
   (not_comparable and not_opened with a note why); the app sends you back once for a page left out.

## 9. How to answer

The reviewer's words: each item is **Wrong** (`status: different`), **Your decision** (`check`) or
**Right** (`same`); a page nobody compared is **Not compared**. Trust (**Confirmed**, **Likely**,
**AI reading only**) is scored by code from independent checks: never claim it yourself.

- **Every difference is a finding**: `report_finding` for each, with the page, item, category,
  what the architect shows and what the vendor shows **as printed** (inches), status, source, and
  boxes where each value is printed (section 10). The app crops the evidence at the boxes, checks
  the printed values and draws the table: never write the findings again as a markdown table.
- **Every `check` (Your decision) has `decision_help`**, short plain words: what it measures (where
  its extension lines end); your best estimate and method (a string added up, an overall less its
  fillers, `measure_on_drawing`, a brand prototype string that sums to the vendor's value) or null;
  `likely`; what to confirm. Mention bigger context (a section the vendor left out) in `why`.
- `source`: `architect`; `brand_prototype` (section 1); `rules` with `rule_check_id` (the check's
  id in `review_results`); `vendor_arithmetic` with `parts` and `printed_total` (a dimension string
  that does not add up). `verified: true` only when you read both values on the drawing yourself.
- **Every value a finding states needs its box**: `vendor_box` where the vendor's value is printed,
  `arch_box` where the architect's is. For `source: rules` the architect field is the rule (e.g.
  `rule 1-2"`) and the `vendor_box` is the spot of the vendor value the rule read. The app sends
  you back, naming them, for findings with a value and no box.
- **One rule, one page, one finding** (narrow questions): when one GV rule applies to several values
  on the same page (the fillers on page 8), report one finding that lists the values (vendor
  `1 1/2", 2", 3"`, architect `rule 1-2"`, why `one filler too wide: 3"`), with a `vendor_box`
  around them, not one finding per value. Give a value its own finding only to show it is wrong.
- Every other value you state from a drawing (a size, a count, a page count): back it with `cite`
  (page and box, or `fact` for a file's page count or name), so the app checks it.
- **Never drop a result GV Review recorded.** Each code FAIL and each AI page review `different` row
  in `review_results` comes back as a finding (give its `record_id`), or is set aside with
  `dismiss_recorded` and your reason. The app sends you back if one is missing.
- **The reading ledger.** Record every dimension string you read with `record_string` (its
  printed parts and printed overall) and every value you read with `record_reading` (page, view,
  side, item, what it is, the value as printed), on both sides. Name an item the same way on every
  page and view, starting with its area from the page map ("Coffee station: counter height";
  '#2' for the second typical unit). Never add up yourself: code adds each string,
  compares each item across views, and reports what does not add up, disagrees or is missing as
  findings. If code says a string does not add up, re-read it once, record it again (`replaces`).
- Sets in rounds (a first and a later submittal): give each finding its `round` (1, 2, ...) and
  name the same item the same way in every round, so the app can link them.
- Never write pass, fail, approved or signed off: GV's code records, a GV reviewer decides.

## 10. Boxes, and the cloud drawing

A box is where the value is printed, a little larger than the number: `{page, x0, y0, x1, y1}` in
PDF points (1/72 inch) of the page as displayed (rotation applied), origin top-left, y down (a
finding's box without `page` is on its page). Give `file` only with 2+ PDFs when the box is not on
its default (architect box: the architect's file; vendor box: the vendor's).
`pdftotext -f N -l N -bbox F.pdf /home/claude/words.html` lists each word's box in exactly these
units (it also reads text inside stamps). For a number drawn as lines, measure it on a picture:
points = pixels x 72 / dpi (add the crop's offset when you rendered part of a page).

The app makes the **cloud drawing** (the marked-up PDF) by code from the findings, on a copy of the
vendor's PDF: a red cloud and note at each Wrong item's box, a numbered amber circle for each
Your-decision item, the stamp "AI markup: draft, not GV's signed review" and notes pages. So when
asked for a marked-up PDF, report the findings with good boxes and say the cloud drawing under the
answer has them. Draw one yourself (reportlab and pypdf on a copy, never the upload, saved in
`/mnt/user-data/outputs/`; the same stamp; `present_files`) only for what the cloud drawing lacks.
