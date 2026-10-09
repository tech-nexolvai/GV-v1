import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReportPanel, type ExportStatus } from '../src/components/review/report-panel.js';
import { receiveReport } from '../src/components/output/reportDownload.js';

// The Report panel (#1064) replaced the unmounted SignedDownloadsView; the same rules hold:
// nothing downloadable until the server says the signed files are ready.
const props = { error: null, requesting: false, download: { status: 'idle' } as const, onPrepare: () => {}, onCheck: () => {}, onDownload: () => {} };
const notReady: { status: ExportStatus | null; error: string | null }[] = [
  { status: null, error: null }, // still asking
  { status: 'not_requested', error: null },
  { status: 'preparing', error: null },
  { status: null, error: '503 status unavailable' }, // asking failed
  { status: 'failed', error: null },
];
for (const { status, error } of notReady) {
  const html = renderToStaticMarkup(<ReportPanel {...props} status={status} error={error} />);
  assert.doesNotMatch(html, /Download PDF|Download workbook|Download redline/, `no download while ${status ?? error ?? 'checking'}`);
  assert.match(html, /Check again/);
  if (error !== null) assert.match(html, /role="alert"[^>]*>Could not check the signed files: 503 status unavailable/);
  if (status === 'failed') {
    assert.match(html, /role="alert"/);
    assert.match(html, /nothing was published/);
  }
  if (status === 'not_requested') assert.match(html, /Prepare signed files/);
  else assert.doesNotMatch(html, /Prepare signed files/, 'only a never-requested export offers to prepare (a failed one is not retried by asking)');
}
const ready = renderToStaticMarkup(<ReportPanel {...props} status="ready" />);
for (const label of ['Download PDF', 'Download workbook', 'Download redline']) assert.match(ready, new RegExp(label));
for (const title of ['Findings PDF', 'Workbook', 'Drawing redline']) assert.match(ready, new RegExp(title));
assert.doesNotMatch(ready, /Prepare signed files|Check again/);
assert.equal((ready.match(/data-state="done"/g) ?? []).length, 3, 'requested → preparing → ready, all done');
const preparing = renderToStaticMarkup(<ReportPanel {...props} status="preparing" />);
assert.equal((preparing.match(/data-state="done"/g) ?? []).length, 1);
assert.equal((preparing.match(/data-state="current"/g) ?? []).length, 1);
const pending = renderToStaticMarkup(<ReportPanel {...props} status="ready" download={{ status: 'loading', format: 'pdf' }} />);
assert.equal((pending.match(/disabled=""/g) ?? []).length, 3, 'one download at a time');
assert.match(pending, /Downloading the findings PDF/);
const refused = renderToStaticMarkup(<ReportPanel {...props} status="ready" download={{ status: 'error', format: 'redline', message: '503 try again' }} />);
assert.match(refused, /role="alert"[^>]*>The drawing redline could not be downloaded: 503 try again/);
let deliveries = 0;
await assert.rejects(receiveReport('pdf', async () => new Blob(), () => { deliveries++; }), /empty report/);
await assert.rejects(receiveReport('redline', async () => { throw new Error('403 refused'); }, () => { deliveries++; }), /403 refused/);
assert.equal(deliveries, 0, 'empty or refused reports never download');
for (const format of ['pdf', 'workbook', 'redline'] as const) {
  const blob = new Blob(['synthetic artifact']);
  await receiveReport(format, async (requested) => { assert.equal(requested, format); return blob; }, (received) => {
    assert.equal(received, blob, 'download the server bytes unchanged');
    deliveries++;
  });
}
assert.equal(deliveries, 3);
console.log('report panel: downloads only once the signed files are ready; a failed export promises no retry');
