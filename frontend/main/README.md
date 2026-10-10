# GV Review — reviewer frontend

React 19 + TypeScript + Vite 8 (with the React Compiler). Hash routing (`src/app/route.ts`). The API
is reached through the Vite proxy (`/api` → `VITE_API_TARGET`, default `http://127.0.0.1:8000`);
`VITE_PROJECT_ID` in `.env.local` picks the project (see `.env.local.example`).

```bash
npm ci
npm run dev        # http://localhost:5173
npm test           # node component harness + Vitest
npm run lint
npm run build
```

## Styling: two systems side by side (#1029)

The redesign moves screens onto **Tailwind CSS v4 + shadcn/ui**, one screen at a time. Until a
screen moves, it keeps its hand-written CSS and must look exactly as before. Four mechanisms keep
the two apart.

1. **Cascade layers** (`src/styles/tailwind.css`, imported first in `src/main.tsx`):
   `theme < base < legacy < components < utilities`.
   - Every legacy stylesheet wraps itself in `@layer legacy { … }`. A new plain `.css` file must do
     the same; `tests/vitest/legacy-css-layer.test.ts` fails otherwise.
   - Legacy sits above `base`, so its own rules keep winning over Tailwind's defaults. It sits below
     `utilities`, so a Tailwind class on a new component wins over an old element rule.
2. **Tailwind's reset is scoped to new UI** (`src/styles/preflight-scoped.css`).
   - It applies inside `@scope ([data-slot], [data-tw])`. Every shadcn element has a `data-slot`.
   - A page or block built from plain Tailwind classes opts in with a `data-tw` attribute, as
     `#/ui-kit` does.
   - Applied globally, the reset shifted 50 of 72 legacy screenshots by a few pixels.
3. **Legacy element rules stop at new UI.** The global `h1–h6`, `p`, `a`, `button`, `input`,
   `code` and `ul` rules in `src/index.css` are wrapped in `@scope (body) to ([data-slot], [data-tw])`.
   Without that, an old `button { font-family }` rule would reach into shadcn components.
4. **Token names.**
   - The legacy tokens share names with Tailwind's (`--text-sm`, `--radius-md`, `--font-sans`,
     `--shadow-*`, `--ease-*`, `--color-primary`).
   - Tailwind's theme is imported with `theme(inline)` and the shadcn mapping is `@theme inline`.
     Utilities therefore carry their values directly and never read a legacy variable; legacy CSS
     keeps its own values.
   - A legacy class name that is also a utility name would be restyled. `text-muted` is excluded
     with `@source not inline(…)`, and `tests/vitest/legacy-collisions.test.ts` fails on any new
     collision.

**Proof that nothing moved:** before/after screenshots of every page (light and dark, desktop and
phone) and a comparison of every element's computed style against `main`. The results are in PR
#1029 (screenshots are kept locally, never in the repository).

### Theme

- Light and dark are `data-theme` on `<html>` (`index.html`, `src/app/theme.ts`).
- Tailwind's `dark:` variant follows the same attribute, including on any subtree with
  `data-theme="dark"`.
- shadcn's variables (`--background`, `--primary`, `--chart-1`…, `--sidebar-*`) are built from
  the same greys as the legacy tokens.
- **Outcome colours** come in four families, each with `-fg` (text) and `-bg` (tint) variants:
  `--outcome-pass`, `--outcome-fail`, `--outcome-review` and `--outcome-missing`.
  - They are always shown with the outcome glyph and word: use `<OutcomeBadge>` from
    `@/components/ui/outcome-badge`.
  - Contrast is tested in `tests/vitest/ui-tokens.test.ts`.
- **Type:** Geist for words (`font-sans`), IBM Plex Mono with even-width digits for numbers,
  dimensions and ids (the `num` utility).
- Both the outcome colours and the sans face were decided by the admin on 2026-10-08 for the
  redesigned screens. Setting `--font-ui` to the mono face would return to an all-typewriter screen.

## Where a review stands (#1034)

`src/lib/review-stage.ts` → `reviewStage(facts)` is the one place that says which of the six steps a
review is on (Upload → Reading → Checks → Decisions → Sign off → Report) and what the single next
action is. It reads only recorded facts:
- package state;
- the live run's finding count;
- the approval-readiness answer;
- signed-export status;
- two in-session flags: checks just queued, and values saved after the last run (the API records
  no time for values).

Several places read it, so they always agree:
- the review stepper (`components/review/review-stepper.tsx`);
- the header's primary button (`components/review/next-action.tsx`, placed through `HeaderActions`);
- the Sign off and Report panels on Results (#1064, below).

Unknown facts stay unknown. A readiness answer that has not loaded shows no count, never 0. Every
state is unit-tested in `tests/vitest/review-stage.test.ts`.

The shell is shadcn's Sidebar (`components/shell/app-sidebar.tsx`, collapse remembered under
`gv-sidebar-collapsed`) and a top bar (`components/shell/app-topbar.tsx`). The top bar carries the
breadcrumb, the screen's title extras (`HeaderTitleExtra`) and its actions (`HeaderActions`).

## Results dashboard (#1039)

The review opens on `components/results/results-dashboard.tsx`:
- five KPI cards;
- one outcome donut (lazy-loaded with the chart library);
- the countertop table: TanStack + shadcn Table, stacked rows on phones, ↑/↓, Enter and D on rows;
- "Other checks", with one bulk "Mark not checkable…".

Every number comes from `GET …/countertop-results` (#1035), and none is computed in the browser.
`lib/countertop-results.ts` holds the pure rules: buckets, sort order, filters, the difference's sign
and words, and `recordEach` for the bulk action.

A row's bucket is what the record says (needs you, then PASS / FAIL / not checkable); a reviewer's
choice appears only under "Decided by". Decisions go through the review page's existing handlers.
The Decide dialog uses the same note rule as the finding card (`actionNeedsNote`).

**The countertop picture** (`components/results/CountertopStrip.tsx`, layout in
`lib/countertop-strip.ts`, #1043) draws one countertop result from the shared spec (vault: "V1 backend -
everything left (Codex)", section 4), which the signed PDF follows too:
- pieces to scale, with the printed overall above and the needed total below;
- field-cut caps only at walled ends, wall blocks and the back-wall line;
- the difference beside it.

Labels are the API's exact text; floats only place boxes. With any width missing it is drawn "not to
scale"; with no pieces, or a non-exact value, it is not drawn at all. It has two sizes: `compact` in
the Results row details and `full` in the Measurements countertop card.

**Show on drawing** (`components/drawing/`, maths in `lib/drawing-viewer.ts`, #1045) is one viewer for
a countertop row or a finding. It opens as a side Sheet on desktop and a full-screen Dialog on a phone,
and its code loads the first time it is opened.
- The page picture with the stored outline. Outlines, other countertops and reading spots sit in an SVG
  whose viewBox is the page's own 0–1 square over the picture, so zoom and pan cannot move them off it.
- Zoom and pan (buttons, wheel, pinch, drag), "Fit", "Find outline", a page strip, and keys
  (+ − 0 F ← →, Esc).
- The countertop picture and the stored crops, each with its exact value printed over it.

It only reads. A picture that is not rendered yet is shown as "not ready"; the viewer never asks the
worker to render anything.

**The "Needs you" queue** (`components/queue/`, rules in `lib/needs-you-queue.ts`, #1050) walks through
everything still blocking sign-off, one item at a time: countertops by page, then the package-level
checks. It opens from the header's "Review N items" and the "Needs you" card.
- **What blocks** is the server's answer (`countertop-results.needs_decision` and readiness); the queue
  only walks through it.
- **The decision** is the same form as the Results "Decide" dialog (`results/decision-form.tsx` +
  `use-decision-draft.ts`), through the page's own handlers.
- **Wall answers** use the countertop card's endpoint: never pre-selected, sent only on a click.
- **"Waiting for a check run"** comes from the server: a row's walls or widths saved after its result
  (`slot-rows.decided_at` against `findings.created_at`), or a correction anywhere in a finding's
  history. A wall answer saved in the same sitting counts at once, before the rows reload.
- **It never claims "all done"** while the readiness API still blocks on results it does not hold
  (for example new results from a run made while it was open): it offers to load them instead.
- **Keys:** J / K, 1 / 2 / 3, N, Enter. Their hints are left out on a touch screen (#1155). **"Change decision"** records a new action; the history
  popover reads `GET .../findings/{id}/actions`.

**The Measurements wizard** (`pages/MeasurementPanel.tsx`, step bar in `pages/MeasurementSectionNav.tsx`,
counting in `lib/measure-steps.ts`, #1061) shows the screen as four steps:
1. Drawings & parts.
2. Countertops.
3. Values.
4. Settings & run checks.

How it works:
- **The other steps stay mounted and are only hidden,** so drafts inside them survive a switch. A plain
  wrapper does the hiding, because the Values section's own grid would override `hidden`.
- **The counts:** each section reports its count (`onProgress`, from the same "still to do" helpers it
  prints). Values and Settings count from the form.
- **The action bar:** Save values / Run checks / See findings live in a bar that stays visible.
  - They record the values and settings (steps 3–4) from any step; steps 1–2 save through their own buttons.
  - The bar says what saving also records (AI-filled values it confirms, layout answers) and warns about
    countertop rows with unsaved changes.
  - `#measure-run-checks` is where the header's "Run checks" lands, and "Open countertop card" switches
    to step 2.
- **Behaviour and API calls are unchanged.** Only layout and wording moved: paragraphs now sit behind
  "?" (`components/ui/info-tip.tsx`), the wall choice is picture buttons, and fields are
  two-column rows.

**Documents, upload, sign-off and the signed report** (#1064):
- **Documents** (`pages/PackagesPage.tsx`, `components/documents/`, arithmetic in
  `lib/documents-table.ts`) is one table from `packages-summary`: one request per page of up to 200,
  every column included. Loaded the first time Documents opens.
  - **The bar shows recorded results** in the agreed outcome words; a decision does not change them.
    What still needs a decision is the server's `needs_decision`, in its own column. A review with
    nothing recorded says "No results yet", never 0.
  - **Sort and search** act on the loaded page (the API has neither yet, requested in #1065), and the
    table says so when there is more than one page.
- **Upload** (`components/upload/upload-progress.tsx`, rules in `lib/upload-progress.ts`): one bar per
  drawing with its real share of bytes sent. The PUT is an `XMLHttpRequest` (`fetch` reports no upload
  progress) to the same ticket URL, with the same method and headers.
- **Sign off** (`components/review/signoff-panel.tsx`): when the stepper reaches Sign off, a panel shows
  the server's readiness and what is being signed. Every Sign off button (header, panel, queue) opens
  a confirmation naming the signer (from the reviewer's own sittings) and saying it cannot be undone;
  only its confirm calls `approve`.
- **Report** (`components/review/report-panel.tsx`): after sign-off, requested → preparing → ready from
  `signed-exports`, then one card per signed file. Downloads appear only when ready. A failed export
  promises no retry, because asking again returns the same failed request (an admin retries it).

**Company settings, Rulebook and Usage** (#1072):
- **Company settings** (`components/settings/CompanySettingsList.tsx`): one table, with a meter of how many have a value and how many GV set. Each row is one line: the value in use, where it comes from (GV standard / rulebook default / not set, as a word), the checks that use it, and a box for a new value. Who set it and when, the rulebook's note on its default, and the default a GV standard replaced sit behind the row's "?" (#1155). Saving sends the same `POST /company-settings`, typed values only.
- **Rulebook** (`components/rulebook/`, counts in `lib/rulebook-overview.ts`): a products × check types grid (a cell filters the table), the severity and the release note every rule shares said once, and the rules as one sortable, searchable table.
- **Usage** (`pages/UsagePage.tsx`, arithmetic in `lib/usage.ts`, charts in `components/usage/usage-charts.tsx`, loaded lazily): `GET /usage` by day and by drawing set, plus `packages-summary` for names and recorded results.
  - **Reading time is shown as not measured.** The API's `package_reading_times` spans saved call times, which one reading writes together; a real duration is requested in #1071.
  - **Calls with no price** are named beside the cost, which leaves them out.
  - **Spend so far** (`components/usage/spend-so-far.tsx`, #1165) sits on top, from `GET /usage/history`: one all-time total (this project's reviews plus earlier runs imported by `scripts/import_spend_history.py`), a by-model table (model in words with its id, route, calls, tokens, cost, unpriced calls) and the earlier runs by day and purpose. It loads on its own, so its failure never hides the rest; a partly unpriced cost reads "at least", and "Not priced" when the priced part is $0.

**Matches the architect** (the vendor-vs-architect check, CT-ARCH-WIDTH-001; `lib/architect.ts`, `components/results/architect-line.tsx`, `components/queue/architect-pairing.tsx`, #1085):
- **Results, the phone list and the countertop card** show an "Architect" line under the vendor's:
  - **compared:** what the architect's drawing says, the difference and the result, in the API's exact text;
  - **not compared:** grey "Not compared: <reason>", with no chip and no action. It is not a finding;
  - **one judgment:** amber "Confirm the pairing: only code matched these" (or "only the AIs").
- **Counting:** a countertop needs you when either check does; it is FAIL when either recorded a FAIL, and PASS only when every recorded result passed. The architect finding is not listed under Other checks.
- **The queue's "Matches the architect?" item:**
  - **One judgment:** Confirm the pairing / Pair it differently / Nothing comparable, through `GET/POST .../slot-rows/{row_id}/architect-pairing`. The item then waits for a check run; a 409 offers a reload.
  - **No pairing yet:** Pair it / Nothing comparable.
  - **A PASS or FAIL** uses the decision form.
- **Show on drawing** outlines the architect's compared dimension in grey (`compared[].architect_location`), and the picker outlines each offered span (`spans[].location`). A missing location is said in words.

**The review assistant** (`components/assistant/`, rules in `lib/assistant.ts`, client `streamAssistant`, #1129)
replaces the old chat. The header's "Assistant" button opens it: docked beside the review when the review area is 1180px or
wider (so the review keeps 760px), laid over its right side otherwise, and the whole screen (a modal) at 900px and below. Esc closes it and focus
returns to the button; it stays mounted while hidden, so the conversation survives.
- **Words from the model, everything else from the records.** `[[n]]` markers become page chips; evidence
  (a countertop card, what still blocks sign-off, listed pages) is drawn from the API's own rows by id, and an
  id the records do not hold is skipped. "Matches the records" shows only when the server's guard checked
  the answer (`checked`, mode `llm` or `records_only`).
- **Navigation only.** Chips and buttons open the drawing viewer or the queue (only at an item the queue
  lists); nothing in the panel records a decision. On a phone the panel closes first.
- **Context:** the page or countertop last opened goes with the question as `focus`, until its chip is removed.

**Legacy islands.** A legacy component shown inside new UI is wrapped in an element with `data-legacy`.
Tailwind's scoped reset stops there, and the legacy element rules apply again inside it.

## Imports

- **New code** uses the `@/` alias (`@/components/ui/button`, `@/lib/utils`). It is set in
  `vite.config.ts`, `tsconfig.app.json` and `tsconfig.test.json`.
- **Existing files** keep relative imports.
- The node harness compiles with `tsc` and then runs `tsc-alias`, which rewrites `@/…` to relative
  paths and adds `.js` extensions. Components it tests may therefore use shadcn primitives too
  (`tests/outcome-badge.test.tsx` proves it).

## shadcn/ui components

The primitives live in `src/components/ui/` (lower-case file names). Beside them sit the legacy
`StatusBadge.tsx` (outcome badges and the package-state words) and `OutcomeIcon.tsx`, and
`PageFrame.tsx`, the Tailwind page header every supporting page shares (#1125). Add one with:

```bash
npx shadcn@latest add <name>
```

Then check four things:

- **Imports:** change `import { cn } from "cn"` to `@/lib/utils` if the CLI wrote the former.
- **CSS:** revert anything it appended to `src/styles/tailwind.css`. It writes `.dark { … }` and
  default-coloured variables, but this app's tokens are already there and dark mode is `data-theme`.
- **Dependencies:** don't add `next-themes`. Use `useDocumentTheme()` (`src/hooks/use-document-theme.ts`).
- **Checks:** `npm run lint` and `npm test`.

Data tables use `@/components/data-table/data-table` (TanStack Table v8). Charts use
`@/components/ui/chart` (Recharts 3).

**The UI kit:** `#/ui-kit` (not linked from the sidebar) shows every primitive in both themes with
made-up data. It is the reference for the redesign and loads as its own chunk.

## Tests

| Command | What | Where |
|---|---|---|
| `npm run test:components` | `renderToStaticMarkup` + `node:assert`, no DOM; pins markup of the legacy components | `tests/*.test.ts(x)`, listed in `tsconfig.test.json` |
| `npm run test:ui` | Vitest + Testing Library + jsdom: clicks, open/close, theme; plus the styling guards above | `tests/vitest/` |
| `npm test` | both | |

Python checks in the repository root also read this frontend: `tests/test_frontend_tokens.py`
(every `var(--x)` is defined), `test_contrast.py`, `test_outcome_labels.py`,
`test_no_confidence_on_review_screens.py` and `test_api_client_paths.py`.

## Acceptance walkthrough (P10, #1106): local only

`e2e/v1-acceptance.spec.ts` (Playwright) walks the screens exactly as a reviewer would. Its steps:
1. Documents, then the set.
2. Results against the API.
3. Show on drawing (stored outline, or none for a split page).
4. The "Needs you" queue: walls, and held rows "not checkable" with `TEST ONLY` notes.
5. Run the checks again, and see the decisions on unchanged results **carried over**.
6. The rest of the queue, and bulk "not checkable" for the other checks.
7. Sign off through the confirmation.
8. The PDF, workbook and redline; the PDF's text is checked.

**Any difference between the screen and the API fails the walk.** Differences from earlier runs (the AIs vary run to run) are only written to `differences.md`.

**It stays out of CI:** it needs a real drawing set. Run it locally, against a **restored copy** behind a local API with the reader off:

1. **Make a mid-review copy** with the backend's acceptance kit, which reads the set and checks it once, answering nothing. This step makes paid AI calls: ask the admin first. Then restore it into a new database on the local Postgres (never the main one). Render its page pictures with `POST .../pages/pictures` (free, no AI).
2. **Run the API and worker** on that copy with `GV_CLAUDE_READER_ENABLED=0` and no keys or cloud credentials reachable. Run this app's dev server with `VITE_API_TARGET` pointing at that API.
3. **Write an expectations file outside the repo** (it holds the set's facts, so never commit it):
   ```json
   {
     "set": "name for the notes",
     "package_id": "…",
     "walls": { "4": "back_only", "9": "proposal" },
     "first": [{ "page_number": 3, "outcome": "REVIEW_REQUIRED", "hold": null }],
     "after": { "4": ["PASS"] },
     "no_countertop": [6, 8]
   }
   ```
   - `first` is the kit's first-run table;
   - `walls` says which wall answer to give on which page (`"proposal"` confirms what the readers propose);
   - `after` and `no_countertop` are only compared and written down, never asserted.
4. **Run the walk:**
   ```
   GV_E2E_BASE_URL=http://localhost:<dev port> GV_E2E_API=http://127.0.0.1:<api port>/api/v1 \
   GV_E2E_PROJECT=<project id> GV_E2E_EXPECT=/path/expect.json GV_E2E_OUT=/path/outside/repo \
   GV_E2E_PYTHON=<a python with pypdf> npx playwright test
   ```
   - It uses the installed Chrome, so no browser is downloaded.
   - The video needs Playwright's ffmpeg (`npx playwright install ffmpeg`), or set `GV_E2E_VIDEO=off`.
   - Everything it saves goes to `GV_E2E_OUT`: video, numbered screenshots, the three files, `report.txt` and `differences.md`. Keep that folder out of the repo; it shows client drawings.

**It writes decisions and a sign-off into the copy.** Restore the copy before running it again.
