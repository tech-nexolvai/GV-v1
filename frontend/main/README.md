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

## The live screens

The chat-first review (`pages/ChatPage.tsx`, `components/chat/`) is where a drawing set is checked.
The old review screens (the Review list, the Advanced view with its Measurements wizard, and the
docked review assistant) were removed on 2026-10-28; `#/review/<set>` links now open the set's chat.

**Documents and upload** (#1064):
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

**Company settings, Rulebook and Usage** (#1072):
- **Company settings** (`components/settings/CompanySettingsList.tsx`): one table, with a meter of how many have a value and how many GV set. Each row is one line: the value in use, where it comes from (GV standard / rulebook default / not set, as a word), the checks that use it, and a box for a new value. Who set it and when, the rulebook's note on its default, and the default a GV standard replaced sit behind the row's "?" (#1155). Saving sends the same `POST /company-settings`, typed values only.
- **Rulebook** (`components/rulebook/`, counts in `lib/rulebook-overview.ts`): a products × check types grid (a cell filters the table), the severity and the release note every rule shares said once, and the rules as one sortable, searchable table.
- **Usage** (`pages/UsagePage.tsx`, arithmetic in `lib/usage.ts`, charts in `components/usage/usage-charts.tsx`, loaded lazily): `GET /usage` by day and by drawing set, plus `packages-summary` for names and recorded results.
  - **Reading time is shown as not measured.** The API's `package_reading_times` spans saved call times, which one reading writes together; a real duration is requested in #1071.
  - **Calls with no price** are named beside the cost, which leaves them out.
  - **Spend so far** (`components/usage/spend-so-far.tsx`, #1165) sits on top, from `GET /usage/history`: one all-time total (this project's reviews plus earlier runs imported once on 2026-10-11), a by-model table (model in words with its id, route, calls, tokens, cost, unpriced calls; the route also under the name on narrow screens when a model has two) and the earlier runs by day and purpose. Calls from before costs were recorded (#754) are priced from the published rates and counted in a note. It loads on its own, so its failure never hides the rest; a partly unpriced cost reads "at least", and "Not priced" when the priced part is $0. OpenRouter's key usage (all projects) comes from its own request, `GET /usage/provider-check`, off unless `GV_USAGE_PROVIDER_CHECK=true`.

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

