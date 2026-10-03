# Connected review-flow QA

This is a frontend contract and interaction test, **not a real drawing review**. The isolated
server has no proxy, loads no environment file and never contacts a backend or model. Its PDFs,
crops, outcomes and exports are explicitly synthetic. Existing fixture packages remain intact.

## Automated check

Run `npm run test:browser-scenarios` in `frontend/main`.

`review-flow.test.mjs` calls the actual upload orchestration and API client against a strict,
in-memory scenario. It checks PDF roles, hashes, storage confirmation order, the revision/session
link, empty findings before checks, exact typed-string transport, rejected unsupported values,
finding/chain identities, reviewer actions, approval and package-scoped export gating.

PDF page counting is stubbed to one page in that test. Values and outcomes are preauthored fixture
responses, not calculated by a replacement engine. The only accepted measurement pair is:

- Vendor depth: `25 1/4 in`
- Approved depth: `25 1/2 in`

The unitless `25` and arbitrary values are deliberately rejected. Missing values remain missing.

## Browser check

Start a new isolated server on a free port; do not replace another session's server:

```sh
GV_QA_PORT=5199 node tests/browser-qa-server.mjs
```

For a manual file-picker check, obtain the two safe fixture PDFs from
`http://127.0.0.1:5199/__qa/pdf/architectural` and `/__qa/pdf/shop`, then open
`http://127.0.0.1:5199/?scenario=review-flow&reset=1#/` and upload them through the form.

If file-picker automation is unavailable, explicitly start **after the synthetic upload**:

`http://127.0.0.1:5199/?scenario=review-flow&fixture-uploaded=1&reset=1#/review/00000000-0000-4000-8000-000000000103`

That option prepares a pair in memory. It must not be described as a browser-performed upload.

1. Confirm the empty review opens without an error and sign-off is disabled.
2. Open Measurements. Enter the two exact fixture values above; leave filler width empty.
3. Run checks. Confirm all four preauthored outcomes appear with unchanged exact values.
4. Open the failed finding's evidence. Both fixture crops should render.
5. Send “Show FAIL findings”. Expect the explicitly synthetic structured fallback, not LLM prose.
6. Open a non-failed finding's details. Its review body must be open on the first click.
   Start a correction draft, close/reopen the row and confirm the unsent draft survives.
7. Confirm the failed and review-required findings; dismiss the missing-value fixture. Sign off.
   These are fixture decisions only; no action changes a recorded outcome.
8. Request PDF, workbook and redline. Verify each response is received; the files contain fixture
   content, not generated production reports.

The read-only `http://127.0.0.1:5199/__qa` page shows requests and retained in-memory records.
Scenario selection is server-wide; use one scenario at a time. `reset=1` intentionally resets
only this isolated fixture. Do not refresh a reset URL while testing draft persistence.

## Evidence limits

The browser regression on 2026-10-03 covered steps 1–8 from the prepared PDF pair at 1280×720.
No application errors were observed in that journey. The original extra nested disclosure was
reproduced and fixed. A browser automation attempt to click its inert contents recorded nothing;
opening the inner card revealed the cause. The fix preserves that inert protection when collapsed.

An attempted mobile viewport override did not change the actual 1280×720 viewport, so this run
does not establish new mobile coverage. Prior mobile checks are separate evidence.

Backend processing, production report generation, OCR, provider behavior, screen readers,
physical devices and native file-picker interaction are not proven by this fixture run.

## Correction and exception save recovery

The `decision-save` scenario is separate from the connected flow and the original evidence fixtures:

`http://127.0.0.1:5199/?scenario=decision-save&reset=1#/review/00000000-0000-4000-8000-000000000101`

1. Open CT-DEPTH-001 details → Correct. Enter `25 1/2 in`. Submit.
   The fixture deliberately delays, then rejects the first save with 503. Inputs/actions should be
   disabled during saving. On rejection the draft stays open and its error is beside the input.
2. Close and reopen the details. The unsent draft should remain. Explicitly submit again. Only after
   201 should the form close and the recorded reviewer action appear. The FAIL verdict stays FAIL.
3. Open CAB-FILLER-001 → Exception. Enter `Synthetic QA exception` and date `2030-01-01`.
   Repeat the reject/retry check. Both fields remain after rejection; one action is recorded on success.
4. Inspect `/__qa` for two attempts per decision, two actions, and unchanged findings.

This fixture limits its accepted payloads to the exact examples above. It does not parse values,
calculate verdicts or persist anything to a backend. Its depth chain deliberately has only one
correctable observation; other scenarios retain the original two observations. In the `approval`
scenario the two-observation correction must still be refused without clearing the draft. Findings
without an observation must likewise keep the draft and report the refusal.

Automated state tests: `npm run test:decision-save` (rejection, refusal, concurrency, retry and success).
Draft retention applies while the finding card stays mounted; it is not cross-refresh persistence.

## Confirm/dismiss acknowledgement and concurrent failure

Use a fresh isolated server and the `action-save` scenario:

`http://127.0.0.1:5199/?scenario=action-save&reset=1#/review/00000000-0000-4000-8000-000000000101`

1. Open CT-SINK-CUTOUT-WIDTH-001 and CAB-FILLER-001 details. Dismiss the missing-value fixture and,
   while it is pending, confirm the filler fixture. These are test decisions, not real approvals.
2. Both cards show Saving; counts must remain unchanged until acknowledgement. The harness delays
   dismiss by 12 seconds and confirm by 1.5 seconds to expose the ordering reliably.
3. Confirm succeeds; reviewed count becomes one. Dismiss then rejects with an intentional 503.
   Its inline error must not erase the saved confirmation, change a verdict or mark dismissal saved.
4. Explicitly retry Dismiss. After acknowledgement the count becomes two. Refresh results and check
   that the two saved actions remain. `/__qa` must show one rejection and two successful action writes.

No optimistic reviewed count, stale whole-list rollback or automatic retry is allowed. The pending
lock is per mounted card. This fixture does not establish production concurrency or network reliability.
# Current-origin setting passages

Use `GV_QA_PORT=5202 node tests/browser-qa-server.mjs`, then open
`http://127.0.0.1:5202/?settings=1&crop-error=once#/review/00000000-0000-4000-8000-000000000101`.
This adds one explicitly synthetic setting pointer to the in-memory required-inputs response.
It does not connect to the backend and refuses measurement saves.

1. Open Measurements and go to Settings. The cited setting starts empty; no parsed answer is shown.
2. The first two passage requests fail (covers React StrictMode's initial double mount).
3. Type any fixture entry and choose Retry passage image. The crop loads; the entry stays unchanged.
4. Choose Enter it another way. Source and reference controls remain available; returning to the
   cited passage preserves the typed entry. No action here confirms a reading or runs checks.

The source/citation contracts are from origin main #828/#871. Integration preserves the redesign's
package-scoped draft retention and confirmation receipts. The added crop loader revokes blob URLs
and ignores late responses after disposal; retry only performs the existing crop GET.
Component tests cover blind-entry non-leakage, exact citation/source payloads, request disposal,
recovery states, and source selection. This fixture is not evidence of real drawing extraction,
server validation, or persisted settings; those remain backend responsibilities.
# Reading confirmation feedback

Run the isolated server on `GV_QA_PORT=5203` and open
`http://127.0.0.1:5203/?scenario=reading-confirmation&reset=1#/review/00000000-0000-4000-8000-000000000101`.
Open Measurements, expand the shop readings under Vendor depth, and click use.

- The first explicit request returns a synthetic 503: the field stays blank and the exact refusal
  appears beside it. Pending state disables the chips. No background retry occurs.
- Click use again: synthetic 201 acknowledges the selected meaning; the existing exact reading
  fills the field once. The candidate leaves the offers. No check or measurement-save is sent.
- Reload without `reset=1` to check the fixture's confirmed-readings response refills the field.
- Edit a single-value field before/during confirmation: the reviewer edit takes priority, and the
  success message says it was kept rather than falsely claiming that the AI value replaced it.
- `/__qa` exposes the synthetic request log. This is frontend QA only, not live OCR, server
  persistence or gold-set accuracy. Customer confirmation endpoints are not exercised by this test.

## Reading-list failure must not hide the measurement form

Run `GV_QA_PORT=5204 node tests/browser-qa-server.mjs` and open
`http://127.0.0.1:5204/?readings=retry#/review/00000000-0000-4000-8000-000000000101`.

1. Open Measurements. The first two candidate requests deliberately fail with 503 (initial
   StrictMode load). Required fields and recorded confirmations must remain visible.
2. Reading counts must say unavailable, not zero; do not show "Nothing was read".
3. Enter `19 in` in Vendor depth, then choose Retry readings. The candidate reappears; the typed
   entry stays `19 in`. Retry must not confirm, save measurements or run checks.
4. Use `?readings=limit` for a persistent synthetic 413. The same form stays available with the
   exact refusal and an explicit retry. No truncated reading list is invented.

The real package was also inspected read-only: required-inputs returned 200 while candidates
returned 413 due to the backend's 500-reading cap. The form previously disappeared because the
three requests were combined with Promise.all. Required fields now remain usable when candidates
or the vocabulary fail. A required-inputs failure still blocks the form rather than inventing fields.

This is partial-load recovery, not a solution for browsing large candidate lists. Backend filtering
or pagination is still required for those packages. No backend, stored value, drawing, rule, verdict,
or schema was changed. The synthetic retry proves UI draft retention, not live server persistence.

## Current field value versus recorded reading

Use the isolated server on 5204 with no query flags. Open Measurements and edit Approved depth
from its prefilled reading to `26 in`. The label must switch from "you confirmed this reading"
to "you typed this". Clear it: the label becomes "needs a value". Neither action changes the
recorded confirmation count or sends a backend write. Re-entering even the original text remains
a reviewer edit; the UI does not infer confirmation from numeric equality.

Also use `?scenario=reading-confirmation&reset=1`. Confirm Vendor depth (first attempt intentionally
fails, second succeeds), then edit its field. The historical success receipt disappears, the current
value is marked typed, and the persisted confirmation stays on record. Pending/error feedback is
not discarded by edits. Unit tests cover edited, empty, proposed, placement-unverified, exact-tagged
and reviewer-confirmed origins. These are frontend provenance checks, not a semantic-type change.

## Parts of each drawing: current-origin integration and recovery

Origin #886 adds one-at-a-time human decisions on suggested drawing parts. The redesign retains
its endpoint payloads, exact codes, role restrictions and decisions; no backend logic is authored here.

Run `GV_QA_PORT=5205 node tests/browser-qa-server.mjs` and open
`http://127.0.0.1:5205/?parts=retry#/review/00000000-0000-4000-8000-000000000101`.

1. Open Measurements. The initial list requests fail with synthetic 503s (twice for StrictMode).
   Required measurement fields remain usable; Retry parts list is available.
2. Retry. The list loads and the previous error disappears. The stored code crop initially fails.
3. Edit the first part's code without confirming it. Retry part 1 crop. The synthetic image loads;
   the code draft remains and the part still says Not decided yet. No decision is sent by retry.
4. A failed refresh retains a previously loaded list with an explicit stale-list explanation.
   This state is covered by the recovery component test. Image disposal/retry/late-response safety
   reuses the tested passage-image loader; image decode errors also expose retry.

The fixture's image is a synthetic placeholder, not a claim that the reader found a part. All
part writes are refused by this isolated server. Successful live part confirmation is not tested.
At verification time the real app's parts GET returned 404; this UI phase does not claim the running
backend has deployed the new route. Backend services and customer data were left untouched.

## Drawing-part decisions: independent saves and inline refusal

Run `GV_QA_PORT=5206 node tests/browser-qa-server.mjs` and open
`http://127.0.0.1:5206/?parts=decisions#/review/00000000-0000-4000-8000-000000000101`.

1. In Measurements → Parts of each drawing, edit the first code to `  REVIEWER-CODE  `.
   Click Cabinet on part 1, then Not a part on part 2 while the first is pending.
2. Each pending row disables its own code/actions and says Saving this decision. The first
   fixture response takes 12 seconds and rejects with 503; the second acknowledges after 1.5s.
   Only the acknowledged decision may change the recorded list/count. A completed second save
   must not release the first row's lock or erase its later error.
3. Part 1 keeps its draft and reports the exact refusal beside that part. Part 2 stays withdrawn.
   Explicitly click Cabinet again. After 201, part 1 becomes confirmed, both receipts say saved,
   and the list reports nothing left to decide. No background retry is allowed.
4. `/__qa/state` must show exactly one withdrawal plus the refused and retried confirmation,
   with the code's surrounding spaces retained in both requests. No checks are requested.

This is in-memory synthetic QA only. The harness accepts only its two known part IDs in this
explicit mode; other writes remain refused. Controller tests exercise both completion orders,
duplicate-submit prevention, exact error/code preservation and the independent add-form key.
Part decisions retain the backend's role restrictions and per-person confirmation requirement.

## Drawing roles: retry, independent saves and stable sections

Run `GV_QA_PORT=5207 node tests/browser-qa-server.mjs` and open
`http://127.0.0.1:5207/?roles=decisions&reset=1#/review/00000000-0000-4000-8000-000000000101`.

1. Open Measurements. The initial drawing-list requests fail with synthetic 503s (twice for
   StrictMode). Enter `26 in` in Vendor depth, then Retry drawing list. The exact draft must stay;
   the error must disappear and exactly one “Which drawing is which?” section must appear.
2. Choose Architect's drawing for panel 1, then Vendor's drawing for panel 2 while panel 1 is
   pending. Each row disables only its own choices and waits for its own acknowledgement.
   Panel 1 refuses after 12s; panel 2 acknowledges after 1.5s. The refusal appears beside panel 1;
   panel 2 stays saved, with only its role selected. No suggestion becomes a saved role by itself.
3. Explicitly choose Architect's drawing again for panel 1. Both rows now show saved, the list
   says All confirmed, and `26 in` remains in the form. Confirmation triggers the existing
   parent read refresh; it does not run checks or save measurement values.
4. `/__qa` must show only three role POSTs: shop 201, arch 503, arch 201; their bodies are exactly
   `{role: "shop"}` and `{role: "arch"}`. No retry may submit a role without the person's click.

Browser evaluation caught a separate reconciliation bug: sibling DrawingRoles and DrawingParts
used the same package key. A successful role refresh left duplicate lists and stale pending/error
DOM. Their keys now have separate roles/parts prefixes while remaining package-scoped. The retry
and success flow was rerun to verify one section and correct receipts; a source regression guard
also protects the distinct keys. Unit tests cover acknowledgement-only state, same-row locking,
exact escaped server errors, retained recorded roles and quiet successful empty lists.

This remains synthetic UI verification, not proof of live OCR or backend persistence. Backend
code, schemas and customer records are unchanged. The previously observed live parts-route 404
and 500-candidate list limit are not resolved by this phase.

## Filler-distribution results stay tied to their submitted inputs

Run `GV_QA_PORT=5208 node tests/browser-qa-server.mjs` and open
`http://127.0.0.1:5208/?distribution=1&reset=1#/review/00000000-0000-4000-8000-000000000101`.
Use a fresh document navigation to reset the scenario. This fixture supplies explicitly synthetic
architectural widths and rule limits, never project defaults in production.

1. Open Measurements → Filler distribution. Enter `32 in` as the site width and classify the
   single cabinet as Double door. Click Calculate distribution, then change the width to `33 in`
   while the five-second response is pending.
2. The response for `32 in` must show “Earlier result — not for the current inputs”. Its numbers,
   table, message and calculation stay intact. Expand Inputs used for this result: it must show
   `32 in`, the original cabinet/filler widths, the classification, and all eight submitted limits.
3. Calculate again. The second fixture request returns 503. The old result and its warning must
   remain, alongside the exact error; the current field must still read `33 in`.
4. Explicitly calculate once more. The `33 in` response becomes current and the old error/warning
   disappear. Change the cabinet type to Drawer: the result immediately becomes earlier again,
   without a request, altered response, saved measurement or check run.
5. `/__qa/state` must show exactly three calculator POSTs: `32 in` → 200, `33 in` → 503,
   `33 in` → 200, and zero checks/measurement writes. The fixture accepts only its documented
   inputs, has no proxy, and its request/response were checked against the backend Pydantic schema.

Before this fix, step 2 presented “Proposal returned” beside the new input with no warning.
The UI now captures the submitted request before awaiting the response and compares it with the
current request as text/structure only. It neither normalizes numeric equality nor repeats the
distribution arithmetic. Missing/unlinked inputs cannot claim a current result. The last response
is retained during recalculation/failure; this is component state, not a new persisted history.

Tests cover site width, cabinet widths/types/order, filler changes, every limit, missing inputs,
exact restoration, unknown provenance, collapsed input details, and no request mutation.
Browser checks cover late arrival, refusal, manual retry and classification edits. This is
synthetic frontend verification, not a live calculation or OCR accuracy claim.

## Countertop-run recovery and independent decisions

Run `GV_QA_PORT=5209 node tests/browser-qa-server.mjs` and open
`http://127.0.0.1:5209/?runs=decisions&reset=1#/review/00000000-0000-4000-8000-000000000101`.
This is an isolated, visibly labelled synthetic fixture; its tolerance is not a production default.

1. Open Measurements. Initial run-list GETs return 503 (twice for StrictMode). Retry countertop
   runs must recover the list without submitting a decision. There is one run section only.
2. For countertop part 4, untick part 2 and confirm the ticked parts. While this request is
   pending, choose Not this countertop's run for part 5. Both rows show their own pending state.
3. Part 5 acknowledges after 1.5 seconds; part 4 refuses after 12 seconds with a synthetic 409.
   The follow-up GET also fails with 503. Keep the previous list visible, explicitly label it
   as previous, and disable its decisions until a successful retry. Show the exact refusal beside
   part 4; its corrected checkbox selection must survive. Part 5 retains its saved receipt.
4. Retry the list: it must read, not resubmit. Part 5 now shows the server's withdrawal, while
   part 4 still has only part 1 ticked. Explicitly confirm part 4 again; it now acknowledges and
   displays the server's confirmed members. The error clears only for that row.
5. `/__qa/state` must show exactly three decision POSTs: withdrawal of 605 → 201, confirmation
   of 604 → 409, confirmation of 604 → 201. Both confirmation bodies contain only part ID 601;
   withdrawal has no submitted selection. There are zero measurement writes and zero check runs.

Browser verification on 2026-10-04 completed all five steps. The fixture validates against the
upstream `RunsOut` schema; the committed OpenAPI matches all 55 current backend paths. Component
tests retain the upstream role/tolerance restrictions, ordered members, warnings and explicit
human confirmation, and add exact escaped errors, row-specific locks, retry and stale-list gates.

Evaluation: the acknowledgement/refusal sits beside the affected countertop, selections survive
failure, and a stale list cannot silently accept another decision. Existing suggestions, excluded
parts and warnings remain visible; no backend data is removed or replaced. This verifies frontend
behaviour, not live OCR or database persistence. Upstream grouping is not yet consumed by rules;
confirming a run does not itself perform a dimensional check. No backend source was authored or
database migrated in this phase. Live deployment still requires the upstream API/migration and
an explicitly configured run-edge tolerance; the frontend does not invent one.
