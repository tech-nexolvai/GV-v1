# Frontend redesign: plan review and implementation

## Scope and preservation

This is a frontend-only refinement of `FRONTEND_REDESIGN_PLAN.md`, not a replacement product or a backend migration. The latest user brief adds Graniti maroon to the plan's originally monochrome visual system.

Implementation branch: `codex/frontend-review-polish`, isolated in `/Users/apple/GV-v1-ui-polish`. The existing authorized frontend work from `/Users/apple/GV-v1-ui` was copied into this checkout before refinement. That original checkout, its uncommitted work, and the other session's backend remain unchanged. No backend records, drawings, findings, rules, approvals, or artifacts are deleted or replaced. Existing unused frontend assets are retained too.

## Evaluation of the plan

Keep the conversational shell, explicit two-PDF upload, document navigation, exact-value tables, reviewer confirmation, on-demand evidence, and light/dark themes. Preserve all current backend capabilities; visual simplification must not simplify the data model.

Move reliability ahead of cosmetic polish:

1. Show existing findings without requiring a chat prompt. Treat no run/no findings as a state with a next action, not an error. Refresh asynchronous results without overwriting reviewer drafts.
2. Use the actual evidence-crop API. The previous viewer requested an unsupported document route and then attempted to render a PNG as a PDF. Preserve every evidence location and exact operand, including unfamiliar document roles.
3. Make progress and failure language accurate: server-reported chat activity, explicit fallback, and partial-upload recovery that retains the already-created package identity.
4. Apply maroon, legible type, quieter motion, useful widths, and keyboard-safe responsive sheets. Test these against real API responses and separate synthetic UI fixtures.

The user-facing source of truth remains the server. The frontend never changes a verdict, supplies missing values, converts a proposal into a fact by itself, or substitutes demo data for real records.

## Component-library research and decision

| Option | Fit | Decision |
|---|---|---|
| [Radix Primitives](https://www.radix-ui.com/primitives/docs/overview/accessibility) | Unstyled, React-compatible accessibility primitives; can keep existing CSS, tokens and Lucide icons. [Dialog](https://www.radix-ui.com/primitives/docs/components/dialog) provides focus containment, Escape and controlled sheets. | Adopt only `@radix-ui/react-dialog` for navigation/evidence overlays. MIT; React 19 supported. |
| [Base UI](https://base-ui.com/react/overview/about) | Strong unstyled alternative compatible with the existing styling approach. | Viable alternative, but a second primitive system is unnecessary for this slice. |
| [shadcn/ui](https://ui.shadcn.com/docs/installation/manual) | Editable component source and broad ecosystem. Its standard setup introduces additional styling conventions/tooling here. | Do not migrate the whole frontend for two overlays. Revisit if broader component replacement is justified. |

The measured reason for adding a dependency is missing modal focus containment/restoration, not a desire to restyle already-working tables. No new chart library, UI framework migration, model library, or backend dependency is introduced.

## Implemented slice

- Maroon action/navigation accents; neutral surfaces; consistent light/dark contrast and readable supporting labels. Chat/composer use a comfortable width, forms and tables retain wider working space.
- Explicit two-PDF upload with a labeled Start review button. Unreadable PDFs are validated before package creation. Later failure preserves the saved package ID and offers Open saved review instead of claiming nothing was recorded or silently creating a duplicate.
- Initial recorded-findings overview, clean empty/processing states, isolated optional-session failure, and asynchronous check-completion refresh.
- One extraction polling loop; drafts, deliberately cleared fields, proposal IDs and repeated cabinet widths survive refresh and chat/measurement tab changes.
- Equal-width candidates keep separate run positions. Failed drawing confirmation aborts Save/Run instead of silently substituting a provenance-free typed value. Successful confirmation receipts are retained for retry because the existing backend confirmation endpoint is not idempotent; an unknown/network outcome is never treated as confirmed.
- Crop-first evidence images through the supported endpoint, loading/retry/full-size controls, exact values and raw provenance. All findings cursor pages and all evidence locations are retained. Unavailable detail never removes the underlying finding/outcome.
- Correction refuses to guess when multiple backing observations exist. Existing approval/export gates remain in place.
- Chat displays actual backend stage events or neutral waiting text. No timer-driven claim that chat is running checks, and no simulated token-by-token reveal of an already-complete response.
- Responsive Radix sheets, focus trap/Escape/opener restoration; skip-to-content preserves the hash route. Workflow detail remains accessible without consuming the entire mobile header.
- Evidence is scoped to a route/package, and late requests are cancelled when leaving a review. Progress counts use the same actionable set for numerator and denominator.

## Browser evidence and checks

Real-backend preview: `http://127.0.0.1:5192/`, using the existing local backend on 8020. Read-only navigation verified the existing package, no-findings state, Measurements, Documents, Rulebook and Usage. No upload, save, confirmation, check request or approval was sent to that backend.

Separate test-only preview: `node tests/browser-qa-server.mjs` from `frontend/main`, on port 5193. It renders the actual App with locally intercepted synthetic API responses and a permanent **SYNTHETIC UI QA · NO BACKEND CONNECTION** label. Unsupported mutations are refused rather than forwarded. No user data is replaced.

Browser checks verified:

- Both evidence images decoded at their recorded 640×230 synthetic image size; exact ARCH/SHOP values and source details remain visible.
- No-provider chat shows the deterministic-fallback disclosure and retains the selected finding.
- Unsaved `24 7/8 in` and an intentionally blank field survive tab changes and refresh in the synthetic form; no Save was pressed.
- Empty review is actionable, sign-off is disabled, and irrelevant failure prompts are absent.
- Approved synthetic fixture exposes PDF/workbook/redline controls; all three clicks completed without a UI error. This verifies download handling, not production artifact generation or approval.
- At a measured 390px width, document width stays 390px, the evidence sheet fits, and both crop images load. At 1600px, evidence becomes a 520px side panel.
- Mobile navigation traps Shift+Tab, Escape dismisses it, and focus returns to Open menu. Evidence Escape returns focus to its View evidence button. Skip-to-content focuses main without changing the hash route. Cross-package navigation removes old evidence.

Screenshots are kept outside the source checkout in the task's `artifacts/frontend-audit-2026-10-03/` directory. They include before/after upload, real empty review, light/dark documents, synthetic findings, evidence, fallback chat, measurement drafts, mobile and approved-state controls.

One test invocation from `frontend/main` produced five `FileNotFoundError` failures because the existing contrast tests use repo-relative paths. Rerunning the focused Python checks from the repository root passed; no test was weakened or changed to conceal those failures.

## Verification boundary

Build, lint, component tests, focused UI/safety Python tests, unchanged OpenAPI comparison, and browser checks are reported in the implementation handoff. Existing-backend browser checks are read-only. Any populated synthetic UI preview is explicitly separate, intercepted locally, and cannot modify backend records. It validates frontend behavior, not OCR accuracy, real model invocation, report generation, or a production end-to-end run.

The full dependency audit reported one existing dev-only `brace-expansion` advisory. Production dependency audit reported zero vulnerabilities; no broad dependency upgrade was performed as part of the redesign.

Verification commands (run Python from the repository root):

```sh
cd frontend/main
npm run build
npm run lint
npm run test:components
npm run test:polish
```

The existing component runner contains 12 test programs; the new polish runner contains 8. The focused Python set is `test_frontend_tokens`, `test_contrast`, `test_outcome_labels`, `test_no_confidence_on_review_screens`, `test_api_client_paths`, `test_repo_hygiene`, `test_verdict_isolation`, and `rules/test_semantic_types`. Its result is **88 passed, 1 skipped**; the skip is the explicitly opt-in remote CodeRabbit-schema comparison, not a failed semantic or repository guard. The stored OpenAPI schema equals the unchanged backend export (46 paths).

## Next slice and release

The follow-up slice is isolated on `codex/frontend-outcome-consistency`, based on PR #830; it does not update or merge that PR. It consolidates outcome glyphs across badges, cards, tables and chat summaries through one decorative `OutcomeIcon`. All five stored outcomes retain the existing shared wording, and each has a distinct shape without relying on colour. The rendering regression test checks identical glyph geometry, unchanged exact values, no finding mutation, and no extra screen-reader or keyboard stops. Run it with `npm run test:outcomes`.

Approval/download and partial-upload browser scenarios are exercised only against the separate synthetic harness. They test the frontend contract, not production storage, approval policy, OCR or report generation. No real backend record is approved or uploaded for these checks.

Follow-up browser results:

- Native file inputs accepted two generated synthetic PDFs. An intentional shop-storage 503 retained one package, both document registrations and one confirmed architect PDF. Start review became disabled and Open saved review opened the same package; no replacement or extraction was requested.
- Three explicit synthetic review actions unlocked sign-off. This found a stale-state defect: download controls became available while the header/sidebar still said Awaiting Review. After a successful approval response, the frontend now re-fetches both authoritative package views; the repeat test showed Approved in both, without changing any verdict.
- PDF, workbook and redline downloaded to disk and matched fixture bytes exactly (698, 2227 and 732 bytes). The browser automation's download-event waiter timed out even though the PDF was saved; filesystem verification established completion. No download code was changed based on that tooling timeout.
- The 390px viewport has no document overflow. All outcome labels remain visible; decorative glyphs add neither screen-reader announcements nor keyboard stops.

Reproduce locally: `GV_QA_PORT=5195 node tests/browser-qa-server.mjs`, then use `?scenario=partial-upload#/` or `?scenario=approval#/review/00000000-0000-4000-8000-000000000101`. `node tests/browser-qa-create-pdfs.mjs` generates synthetic PDFs in a unique temporary directory for the native file chooser. `/__qa` displays in-memory scenario counters and requests. Run `npm run test:browser-scenarios` for the scenario contract regression tests. No scenario forwards to the backend; unimplemented writes are refused.

Raise a frontend-only PR with screenshots and tests. Before merging, pull the latest origin/main in the integration checkout, reconcile the concurrent session's work, rerun checks, and inspect the final diff. Do not merge or discard another session's changes merely to make the branch clean.
