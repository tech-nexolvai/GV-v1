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
design). They are a reference, not this job's architect drawing: never treat them as the design.
Mention a difference with one only as "the brand prototype shows ...", separately.

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
   the pictures are, and to read this file's sections 4, 5 and 7 first (they can open it too). Ask for
   page-by-page findings: page numbers, item, the exact printed value on each side, whether GV
   flagged it, how sure, and the list of pages it opened.
6. **Verify every lead yourself before you state it**: open that crop on both drawings and read the
   numbers. A lead you checked and found wrong, drop. One you had no time to check, list under
   "Leads not checked yet"; never drop it silently.

## 9. How to answer

- Lead with a short summary, then a table, countertop items first, then cabinets, then the rest:

| Page | Item | Architect shows | Vendor shows | Why it matters |
|---|---|---|---|---|
| 7 (arch p.6) | Pantry top depth | 26 1/4" | 25 3/4" | Top 1/2" short of the design |

- Inches, with the printed form when useful. Say which side looks wrong, or "needs a person to
  check". Say whether GV already flagged it.
- Mark what you verified on the drawing yourself vs what is a lead from a helper you have not
  checked. Name pages you did not open.
- Never write pass, fail, approved or signed off as your own judgement: GV's code records results
  and a GV reviewer decides. Your answer is an AI reading, to check before acting.

## 10. Making a marked-up PDF

When asked for a marked-up PDF: work on a **copy** of the vendor's PDF (never the upload), draw a
red cloud and a short numbered note at each item, put "AI markup: draft, not GV's signed review" on
the first page, list every note again on a page added at the end, save to
`/mnt/user-data/outputs/`, check it, and hand it over with `present_files`.

Where an item is: `pdftotext -f N -l N -bbox F.pdf /home/claude/words.html` lists each word's box
in points, origin top-left (it also reads text inside stamps; pdfplumber's `extract_words` reads
only page-content text). For a number drawn as lines, measure it on a picture:
points = pixels x 72 / dpi. PDF drawing space starts bottom-left: `y = page_height - y_top`. Check
`page.rotation` and the mediabox origin; if either is not 0, convert accordingly. On a sheet whose
drawings are pasted stamps, the drawing is painted over your marks: put each note in clear space.

```python
import io, logging; logging.disable(logging.WARNING)
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import red
SRC, OUT = "/mnt/user-data/uploads/F.pdf", "/mnt/user-data/outputs/F - AI markup.pdf"
ITEMS = [(7, (412, 300, 470, 312), 'Arch 26 1/4" deep, vendor 25 3/4"')]  # page, (x0, top, x1, bottom), note
TITLE = "AI markup: draft, not GV's signed review"

def cloud(c, x0, y0, x1, y1, r=5):  # scallops outward along the four edges of a box
    for ax, ay, bx, by, start in ((x0, y0, x1, y0, 180), (x1, y0, x1, y1, 270),
                                  (x1, y1, x0, y1, 0), (x0, y1, x0, y0, 90)):
        n = max(2, int(max(abs(bx - ax), abs(by - ay)) // (2 * r)))
        for i in range(n):
            cx, cy = ax + (bx - ax) * (i + .5) / n, ay + (by - ay) * (i + .5) / n
            c.arc(cx - r, cy - r, cx + r, cy + r, start, 180)

def overlay(pw, ph, draw):  # one transparent page drawn with reportlab
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(pw, ph))
    c.setStrokeColor(red); c.setFillColor(red); draw(c); c.save(); buf.seek(0)
    return PdfReader(buf).pages[0]

w = PdfWriter(clone_from=PdfReader(SRC))
numbered = list(enumerate(ITEMS, 1))
for n, page in enumerate(w.pages, 1):
    pw, ph = float(page.mediabox.width), float(page.mediabox.height)
    marks = [(k, box, note) for k, (p, box, note) in numbered if p == n]
    def draw(c, marks=marks, n=n, ph=ph):
        if n == 1:
            c.setFont("Helvetica-Bold", 14); c.drawString(24, ph - 30, TITLE)
        for k, (x0, top, x1, bottom), note in marks:
            cloud(c, x0 - 6, ph - bottom - 6, x1 + 6, ph - top + 6)
            c.setFont("Helvetica-Bold", 10); c.drawString(x1 + 10, ph - top + 4, f"AI {k}: {note}")
    if marks or n == 1:
        page.merge_page(overlay(pw, ph, draw))
pw, ph = float(w.pages[0].mediabox.width), float(w.pages[0].mediabox.height)
def notes(c):  # every note again, on a page added at the end
    c.setFont("Helvetica-Bold", 14); c.drawString(36, ph - 40, TITLE); c.setFont("Helvetica", 10)
    for i, (k, (p, _box, note)) in enumerate(numbered):
        c.drawString(36, ph - 64 - 14 * i, f"AI {k}  (page {p})  {note}")
w.add_blank_page(pw, ph).merge_page(overlay(pw, ph, notes))
w.write(OUT)
```
Then render each marked page (`pdftoppm -f N -l N -r 80 -png ...`) and `view` it to check every
cloud sits on its item before you call `present_files`. Keep notes short and in inches; one cloud
per item; the note says what the architect shows and what the vendor shows.
