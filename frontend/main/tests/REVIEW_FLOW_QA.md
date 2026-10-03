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
