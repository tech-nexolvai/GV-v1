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
