---
name: gv-review
description: How GV reviews a millwork vendor's shop drawings (countertops, cabinets) against the architect's drawings and GV's own rules. Read this before you answer any question about the drawing set in /mnt/user-data/uploads, compare the vendor with the architect, or make a marked-up PDF.
---

# GV review: the vendor's shop drawing against the architect's drawing

GV gets **shop drawings** from its vendors (the countertop and cabinet makers) before anything is
built. A GV reviewer checks them against the **architect's drawings** (the design) and GV's own
rules, and marks up what is wrong. You do that same review, as an AI helper: you find
and explain differences; a GV reviewer decides. You cannot change the review or record a result.

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
- **Two PDFs**: one architect set, one vendor set. File names often say so; confirm from the title
  blocks.
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

```bash
pdfinfo F.pdf                                              # pages, page size
pdftotext -f 3 -l 3 -layout F.pdf - 2>/dev/null | head -60 # one page's words (also text inside stamps)
```
```python
# Every annotation: page, kind, author, text. Headings and GV's notes are FreeText.
import logging; logging.disable(logging.WARNING)
from pypdf import PdfReader
r = PdfReader("/mnt/user-data/uploads/F.pdf")
for n, page in enumerate(r.pages, 1):
    for a in page.get("/Annots") or []:
        a = a.get_object()
        print(n, a.get("/Subtype"), a.get("/T"), repr(str(a.get("/Contents") or ""))[:80],
              [round(float(v)) for v in a.get("/Rect")])
```
Build a page map first: page -> whose drawing (architect / vendor / prototype / general), area or
room, sheet number. Write it to `/home/claude/page_map.md` and keep it up to date.

## 3. Pair each vendor sheet with the architect's sheet

- Combined sheet: the two drawings on the same page are the pair (stacked or side by side).
- Separate files: match by room or area name, the item's name (vanity, kitchenette, coffee
  station, reception desk...), the elevation or detail callout (the vendor usually quotes the
  architect's sheet and detail number), the plan location and the overall shape.
- Never pair two drawings only because a number matches. If you are not sure it is the same wall
  or piece, say "pairing not certain" and why.

## 4. Read the numbers

- Feet and inches: `3'-2 1/2"` = 38 1/2" = 38.5". `10'-0"` = 120". Give every answer **in inches**,
  with the printed form beside it when it helps: 38 1/2" (printed 3'-2 1/2").
- Two units, `610 [24]`: millimetres first, inches in brackets. **Inches count**; use the mm only to
  check that you read the inches right.
- Fractions are usually to 1/16". A stacked fraction (small 1 over a bar over 2) is easy to misread:
  zoom in.
- On some sets the numbers are drawn as lines (pen strokes), not text: `pdftotext` returns few or no
  numbers. Then read them from a picture: render the area at 300 dpi and look.
- Abbreviations: EQ (equal), TYP (typical), U.N.O. (unless noted otherwise), VIF (verify in field),
  CL / ℄ (centre line), AFF (above finished floor), CLR (clear), O.C. (on centre), NIC (not in
  contract), BY OTHERS.
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
whether GV already flagged an item ("GV flagged: yes / no"). Sets sent by vendors for a new review
usually have no GV markup at all.

**Compare on pictures with GV's markup hidden.** The drawings themselves are `/Stamp` annotations,
so `pdftoppm -hide-annotations` gives a blank page. Instead make a copy that keeps only the stamps,
and render that (read the headings from the annotation list first: the copy drops them):
```python
import logging; logging.disable(logging.WARNING)
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, NameObject
w = PdfWriter(clone_from="/mnt/user-data/uploads/F.pdf")
for page in w.pages:
    if "/Annots" in page:
        keep = [a for a in page["/Annots"] if a.get_object().get("/Subtype") == "/Stamp"]
        page[NameObject("/Annots")] = ArrayObject(keep)
w.write("/home/claude/F_drawings_only.pdf")
```
A page with no stamps is ordinary page content: render it as it is. Coloured ink inside a stamp
stays in the copy; treat it as GV's. A vendor number under a yellow box may still be in the text
(`pdftotext` of the drawings-only copy) even when the picture hides it.

## 6. GV's rules first, then everything else against the architect

1. Read `rules.md` (and `checklists/` when present). Apply each rule that fits the item: it says
   what is compared, the formula, and the numbers in force (field cut per wall end, filler limits,
   sink clearance, sink front and back offsets...). GV compares **exactly**: no tolerance unless
   `rules.md` gives one. A value the rule needs that nobody has given is missing: say what is
   missing, never assume one.
2. `review_results` gives what GV's code already recorded for this set. Quote it as recorded; never
   contradict it or restate a result in your own words. Much of a set is not checked by code.
3. Then compare **everything else** on the vendor's drawing with the architect's.

## 7. What to compare (not only overall widths)

**"The architect prints no size here" never means "nothing to compare".** Architects often draw
millwork without dimensions; then compare what is drawn: how many bays, doors, drawers and
shelves, door vs drawer, uppers present and where they start and stop, splashes, the sink and its
position, appliances, collars and holes, tags and notes. Scale only to say "about", and say so. Do
this on **every** paired page, including the second round (revision) pages.

- **Sizes**: overall length, each piece and cabinet width, **depth** (countertop depth = cabinet
  depth + overhang), **heights** (counter height above floor, upper cabinets' bottom and top,
  backsplash height), thicknesses (top, splash).
- **Sink and appliances**: sink centre line from the wall or end, sink model or tag (SNK-...),
  undermount vs drop-in, cut-out size, faucet holes; appliance models and openings,
  their centre lines; rim collars and holes (a collar must fit its hole).
- **Material and finish tags**: CT-, ST-, SS-, QZ-, PL-, LA-, WD-, MT- and the like. The vendor's
  tag on a piece must match the architect's for the same piece.
- **Backsplash and side splash**: present or not, height, thickness, material (stone vs tile). A
  side splash only where the top meets a wall; a free-standing end gets none.
- **Cabinets**: door vs drawer, number of doors and drawers, hinge side, open shelves, end panels,
  toe kick, equipment cabinets (sink, appliance) that must not change size.
- **Fillers and site fit**: filler widths (see `rules.md` for limits), scribe, field cut (extra
  length cut on site; per wall end).
- **Overhangs and supports**: front and side overhang, knee space, brackets, corbels.
- **Cut-outs**: sink, cooktop, grommets, outlets.
- **Notes and scope**: "by others", "VIF", "NIC", edge profile, seams, anything the architect notes
  that the vendor dropped or changed.

## 8. Working a big set

1. `TaskCreate` a short plan; keep it updated with `TaskUpdate`.
2. `pdfinfo` each file; `pdftotext -layout` page by page to build the page map (section 2).
3. Make the drawings-only copy (section 5). Render every page once at 100-150 dpi for an overview:
   `mkdir -p /home/claude/pages && pdftoppm -r 100 -png /home/claude/F_drawings_only.pdf /home/claude/pages/p`.
4. To read small numbers, render just the area at 300 dpi:
   `pdftoppm -f 7 -l 7 -r 300 -x 1200 -y 900 -W 1400 -H 1000 -png F_drawings_only.pdf /home/claude/zoom`
   (`-x -y -W -H` are pixels at that dpi: points x 300 / 72). Then `view` it.
5. For many pages, split them across helpers (`Agent`, up to 4 at once) **by building or area**, so
   each helper has both the architect's and the vendor's pages of its areas. Tell each one where
   the pictures are, to read this file's sections 4, 5, 7, 9 and 10 first, to record each
   difference with `report_finding` (with boxes) and the pages it compared with `set_coverage`,
   and to list the pages it opened. Brief them on non-dimension differences and brand prototypes.
6. **Verify every lead yourself before you state it** (a helper's finding is a lead): open that
   crop on both drawings and read the numbers, then `update_finding` with `verified: true` (or
   correct it). A finding you checked and found wrong: `drop_finding`. One you had no time to check
   stays unverified.
7. **Whole-set questions cover every page pair** (section 7), yours or a helper's. Record every
   page with `set_coverage`: same, different, not_comparable (note why) or not_opened (note why).
   The app sends you back once if a page is not accounted for.

## 9. How to answer

- **Every difference is a finding**: `report_finding` for each, with the page, item, category,
  what the architect shows and what the vendor shows **as printed** (inches), status, source, and
  boxes where each value is printed (section 10). The app crops the evidence at the boxes, checks
  the printed values and draws the table: never write the findings again as a markdown table.
- `source`: `architect`; `brand_prototype` (section 1); `rules` with `rule_check_id` (the check's
  id in `review_results`); `vendor_arithmetic` with `parts` and `printed_total` (a dimension string
  that does not add up). `verified: true` only when you read both values on the drawing yourself.
- Every other value you state from a drawing (a size, a count, a page count): back it with `cite`
  (page and box, or `fact` for a file's page count or name), so the app checks it.
- **Never drop a result GV Review recorded.** Each code FAIL and each AI page review `different` row
  in `review_results` comes back as a finding (give its `record_id`), or is set aside with
  `dismiss_recorded` and your reason. The app sends you back if one is missing.
- **The reading ledger.** Record every dimension string you read with `record_string` (its
  printed parts and printed overall) and every value you read with `record_reading` (page, view,
  side, item, what it is, the value as printed), on both sides. Name an item the same way on every
  page and view ('#2' for the second typical unit). Never add up yourself: code adds each string,
  compares each item across views, and reports what does not add up, disagrees or is missing as
  findings. When code says a string does not add up, re-read it once and record it again with
  `replaces`.
- Sets in rounds (a first and a later submittal): give each finding its `round` (1, 2, ...) and
  name the same item the same way in every round, so the app can link them.
- Match the question: yes/no or a number, one line; "summarize", one or two lines; a list, a short
  list; a comparison, the findings plus at most two plain lines. No preamble, no disclaimer, no
  restating the question. One line on what you did not check when it matters. Unclear: ask one
  short question back.
- Never write pass, fail, approved or signed off as your own judgement: GV's code records results
  and a GV reviewer decides.

## 10. Boxes, and the marked-up PDF

A box is where the value is printed: `{page, x0, y0, x1, y1}` in PDF points of the page as shown,
origin top-left, a little larger than the number. Give `file` only when the set has more than one
PDF and the box is not on the default (architect box: the architect's file; vendor box: the vendor's).
`pdftotext -f N -l N -bbox F.pdf /home/claude/words.html` lists each word's box in exactly these
units (it also reads text inside stamps). For a number drawn as lines, measure it on a picture:
points = pixels x 72 / dpi (add the crop's offset when you rendered part of a page).

The reviewer's **Download marked-up PDF** button draws, by code, a red cloud and a note at each
finding's vendor box (else its architect box) on a copy of the vendor's PDF, the stamp
"AI markup: draft, not GV's signed review" on page 1 and a notes page. So when asked for a marked-up PDF,
report the findings with good boxes and say the button under the answer makes it. Draw one
yourself (reportlab and pypdf on a copy, never the upload, saved in `/mnt/user-data/outputs/`; the
same stamp and notes page; check it by rendering; `present_files`) only when the reviewer asks for
something the button does not make.
