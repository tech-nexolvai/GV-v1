# Call with Raj — talking points (2026-08-22)

Plain-language agenda for the call. Part A is what Raj has already confirmed (so we recap, not
re-ask). Part B is what's still open — the questions for this call, most important first.

**Two things matter most on this call:**
1. **The real drawings** — the single biggest thing; everything we measure quality against is built from them.
2. **The sink cut-out (width vs depth)** — the question Raj said he needs a call to explain. Let him walk us through it.

---

## Part A — What Raj has already told us (confirm, don't re-open)

**Q. How strict should the checks be — how close is "close enough"?**
A. For the first version, **exact match**: the system flags any difference, and the reviewer clears the false alarms on screen. No tolerance band for now.

**Q. Drawings show both millimetres and inches, and they don't always match. Which one counts?**
A. **Inches.** The millimetres are only for the vendor's machines — ignore them; if the inches match, we're good.

**Q. When the real wall doesn't match the drawing, how do you fit the cabinets?**
A. Adjust the **fillers first** (kept between **1″ and 2″**). If that's not enough, the **reviewer picks which cabinet to resize** — some (e.g. a microwave cabinet) must not change. Works whether the site is larger *or* smaller than the drawing.

**Q. Is the little end strip part of the cabinet?**
A. No — it's a **filler**, a separate strip between the wall and the cabinet. There's none if there's no wall on that side.

**Q. Sink front offset — 4 inches minimum, or exactly 4?**
A. Treat **4″ as the value**, adjustable under special cases. (Checked as an exact match, per the rule above.)

**Q. Sink back offset — is it a fixed number?**
A. No — it's **whatever's left** after the front offset and the sink depth are taken out of the countertop depth. It has a **minimum** (below which the faucet hole won't fit) — *still coming from the vendor* (see Part B).

**Q. Countertop depth vs the sink offsets — which drives which?**
A. **Depth is set from the cabinet depth + overhang.** The offsets are checked against it; if they don't add up (sink hole too big) the system flags it → change the sink.

**Q. Field cuts — how many, and added or trimmed?**
A. Extra is **added at the factory**, then **trimmed on site** to fit. How many depends on which sides have walls.

**Q. Where do the sink dimensions come from?**
A. The **reviewer provides them per project** — uploads the spec sheet or types the numbers in.

**Q. How does the system know which cabinets can't be resized?**
A. The **reviewer decides** which cabinet may change.

**Q. Are the check names/tags final?**
A. **No — use them provisionally** for the demo; final names come once all the layouts are settled.

**Q. Which layouts are in scope?**
A. **Back-wall-only and island are in** (drawings coming). Three-sided already covered.

**Q. The way the sink cut-out sits inside the sink cabinet (the clearances)?**
A. **Confirmed** — our reading was correct.

---

## Part B — Still open — what we need from this call

### 1. The real drawings *(the biggest unblocker — it holds up almost everything downstream)*
We need real completed jobs to test against. Even a handful lets us start.
- **Most valuable:** one job where a **real mistake was caught, with your markup** — that's our proof the system catches genuine errors, not just that it agrees with correct work.
- **Variety** across the three layouts (three-sided, back-wall-only, island).

### 2. The sink cut-out: width from width, or width from depth? *(the one you wanted to explain)*
When you size the hole cut for the sink — its **width** — is it based on the sink's **inside width** (less a little each side), or its **inside depth**? Your notes and your diagram point to different ones, and we don't want to guess on a cut dimension. **Please walk us through it.**

### 3. The back-offset minimum
You said the back offset is whatever's left, but never below a minimum or the faucet hole won't fit. **Do you have that minimum from the vendor yet?** Until we do, that one check stays at "needs review."

### 4. Which flags are serious vs "just worth a look"?
Right now everything gets flagged and you decide. For our own quality tracking we'd like a rough split: **which flags mean a drawing must NOT go to the factory until it's fixed (a real error)**, versus which are just worth a glance? Even a loose grouping (e.g. "any wrong dimension = serious") helps.

### 5. A few checklist rows that look like they contradict themselves
About **four spots** read as small wording slips — **most around the sink** (a "width" that seems to be defined from a "depth"), and **one on the countertop-depth check** (its heading says "width" but the check is about depth). Can we **walk through them live** so we build the right thing? *(Also: three different sink checks are labelled the same — cut-out depth, cut-out width, and the offsets. We've treated them as three separate checks; please confirm.)*

### 6. ADA — in or out for the first version?
One cabinet height in the drawings is **34 inches**, which is the ADA maximum. Should the system check ADA compliance, or is that **out of scope** for now?

### 7. Notes that override a default ("unless otherwise noted")
When a drawing carries a note that overrides a normal value, how do you want the system to handle it — **follow the note automatically, or flag it for the reviewer**?

### 8. Two smaller ones
- **Two-wall (corner) layout** — in scope for the first version, or just three-sided / back-only / island?
- **The "site smaller than the drawing" case** — you offered to sketch it; that sketch would help a lot.

---

**If the call runs short, get #1 and #2** — the drawings, and the sink cut-out width/depth. Those two unblock the most; the rest can go on the follow-up.
