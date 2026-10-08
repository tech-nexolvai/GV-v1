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
- the Documents cards (`pages/documentNextStep.ts`).

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

**Legacy islands.** A legacy component shown inside new UI (the chat and the drawing panel in shadcn
Sheets) is wrapped in an element with `data-legacy`. Tailwind's scoped reset stops there, and the
legacy element rules apply again inside it.

## Imports

- **New code** uses the `@/` alias (`@/components/ui/button`, `@/lib/utils`). It is set in
  `vite.config.ts`, `tsconfig.app.json` and `tsconfig.test.json`.
- **Existing files** keep relative imports.
- The node harness compiles with `tsc` and then runs `tsc-alias`, which rewrites `@/…` to relative
  paths and adds `.js` extensions. Components it tests may therefore use shadcn primitives too
  (`tests/outcome-badge.test.tsx` proves it).

## shadcn/ui components

The primitives live in `src/components/ui/` (lower-case file names; the legacy `StatusBadge.tsx`,
`OutcomeIcon.tsx` and `PageFrame.tsx` sit beside them). Add one with:

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
