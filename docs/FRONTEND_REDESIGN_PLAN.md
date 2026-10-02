# Frontend redesign — a ChatGPT-style, black-and-white GV Review

**Goal.** Rebuild the look and layout of the reviewer app as a familiar ChatGPT/Groq-style chat interface, in
strict black and white with a refined monospace face, **without changing the backend and without breaking any
flow that works today**. The backend is read-only for this work: every screen keeps calling the same endpoints
with the same requests.

**The client's locked brief** (2026-07): a ChatGPT-style interface — sidebar of reviews on the left, Graniti
branding top-middle, Nexolv branding bottom-right, strict black and white, a good monospace/typewriter face.
*Why:* the client tried plain ChatGPT and it failed on accuracy; a familiar shell makes "same interface, but it
actually works" land.

## What is wrong today (seen in the browser against the real API, and in the code)

- **Brief broken:** maroon brand colour, beige surfaces, Inter + Josefin headings, brand top-left, Nexolv
  top-right at 1.8:1 contrast, a graph-paper background.
- **Layout:** the "IDE" preset pins an evidence panel open on every review screen — even the welcome screen —
  and squeezes the chat to 360–480px. The findings table (~560px) overflows; rule ids wrap one letter per line;
  the "View evidence" button is pushed off-screen. The resizer does nothing (`!important` beats it).
- **Broken:** the sidebar "Reviews" links pass a review-session id where a package id is expected; "Measure"
  opens a blank page; the evidence viewer asks for a PDF endpoint that does not exist (404) and then feeds the
  PNG crop to pdf.js ("Invalid PDF structure"); dark mode is unreachable; Documents hides its own loading /
  error / empty rows; the "thinking" stages run on timers, not on the stream's real `stage` events.
- **Readability:** values shown as `101/4 in` instead of `25 1/4"`; 9–11px text in 9 files; uppercase
  sender labels, avatars and timestamps on every message; three different icon sets for the same outcome.
- **No URL state:** a refresh always drops the reviewer back to the start.

## What we keep (it works and tests depend on it)

- Every API call and the error/empty/loading honesty (`useAsync`, `ApiError`).
- `OUTCOME_LABELS` — the status words, tested against the backend.
- The findings table internals (`sortFindings`, real `<table>`, `ftable__*` classes, `aria-expanded`).
- Chat behaviour: stay pinned to the bottom, the auto-growing composer (Enter / Shift+Enter), the SSE chat
  stream with its three fallbacks, reduced-motion handling.
- The elevation diagram geometry and the measurement form logic.

## Design decisions

- **Colour:** black, white and greys only, light and dark (system default, with a toggle). Old token names are
  re-pointed at the new values so every component turns monochrome at once. No colour carries meaning on its own.
- **Status without colour:** shape, fill and weight. *Needs correction* = solid filled chip ✕; *Needs your
  decision* = solid outline ▲; *Waiting on a value* = dashed outline ◌; *Looks right* = plain ✓. One source
  (`outcomeVisual`) for every component. Arch = hollow square □, Shop = filled square ■.
- **Font:** IBM Plex Mono (400/500/600 + italic) for the whole UI — typewriter lineage, clear 0/O and 1/l/I,
  which matters for dimensions like `1'-0 1/2"`. Loaded with `<link>` + preconnect, not a CSS `@import`.
- **Shell (ChatGPT):** full-height 260px sidebar — New review, the reviews list grouped by date, then Rulebook /
  Documents; footer with Company settings, Usage and theme. Collapses to a rail; a drawer under 768px. Header 56px:
  review title left, **Graniti centred**, actions right. **Nexolv bottom-right**, small and quiet.
- **Chat:** centred column, max 768px. User messages right-aligned in a soft bubble; assistant messages full
  width, no bubble, no avatar. Pill composer pinned to the bottom. Suggestion chips only when useful.
- **Evidence panel (like ChatGPT canvas):** closed by default; opens beside the chat when a finding is opened
  (≥1100px), full-screen sheet below that. Shows the **crop first** (it exists); the full-drawing viewer only
  when the backend can serve the PDF.
- **No motion for decoration:** no hover lifts, staggers or page transitions; 120–280ms fades/slides; honour
  reduced motion.

## Stages (each one builds, passes every check, and is checked in the browser before the next)

| # | Stage | What changes |
|---|---|---|
| 0 | Prep | Remove dead code (App.css, unused page/assets), fonts via `<link>`, theme attribute + toggle wiring. No visible layout change. |
| 1 | Tokens | Monochrome light/dark tokens, Plex Mono, type scale (nothing under 12px), radii, quieter motion; `outcomeVisual` glyph/fill rules in the same stage so meaning never depends on colour. |
| 2 | Shell | New sidebar (real packages grouped by date — fixes the broken session links), centred Graniti, Nexolv corner, collapse + mobile drawer, IDE preset and dead resizer removed, evidence panel on demand, page + package kept in the URL. |
| 3 | Chat | Message layout, pill composer, real `stage` events in the thinking indicator, welcome = "Start a review" with the two drawing slots and a required vendor (no pre-filled sample vendor). |
| 4 | Findings & evidence | Compact findings table that fits the column, readable values (`25 1/4"`), row → evidence panel, crop-first evidence with no broken PDF viewer, simpler review header. |
| 5 | Pages | Documents (status rows visible), Rulebook, Company settings, Usage on one centred frame; sign-off + export as a clear menu. |

**Guards every stage keeps green:** `tsc -p tsconfig.app.json` (no unused imports), `vite build`, `npm run lint`,
`npm run test:components`, `npm run api:check`, and the Python checks that read the frontend —
`test_frontend_tokens`, `test_contrast`, `test_outcome_labels`, `test_no_confidence_on_review_screens`,
`test_api_client_paths`. **No sample or made-up data on any screen** — the lesson of #816/#821.

## Not in this work (needs a backend change or a client decision)

- **Full drawing viewer:** the backend has no PDF download endpoint, so the panel shows the evidence crop.
- **A project picker / project list:** there is no endpoint; the project still comes from `VITE_PROJECT_ID`.
- **A readable package name in the sidebar:** packages carry only a vendor; the list shows vendor + date.
- **Showing FLAG / NEEDS REVIEW instead of today's labels:** needs the client's sign-off and a backend change.
- **Turning the measurement form into inline chat cards:** a larger rewrite of a 1,600-line form; this work
  restyles it and gives it room, and the card version is a follow-up.
- **The logo:** the current file is a red JPEG inside an SVG; it is shown in greyscale here. A vector,
  black-and-white mark should come from GV.
