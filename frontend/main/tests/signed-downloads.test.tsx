import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { SignedDownloadsView } from '../src/components/output/SignedDownloadsView.js';
import { receiveReport } from '../src/components/output/reportDownload.js';

const props = { busy: false, request: () => {}, refresh: () => {}, download: () => {} };
for (const status of ['loading', 'not_requested', 'preparing', 'error', 'failed'] as const) {
  const html = renderToStaticMarkup(<SignedDownloadsView {...props} status={status} />);
  assert.doesNotMatch(html, /Download PDF|Download workbook|Download redline/);
  assert.match(html, /Check availability/);
  if (status === 'error') assert.match(html, /role="alert"/);
  if (status === 'not_requested') assert.match(html, /Prepare signed exports/);
}
const ready = renderToStaticMarkup(<SignedDownloadsView {...props} status="ready" />);
for (const label of ['Download PDF', 'Download workbook', 'Download redline']) assert.match(ready, new RegExp(label));
assert.doesNotMatch(ready, /Prepare signed exports/);
assert.match(ready, /<details class="signed-downloads__menu"><summary>Signed reports<\/summary>/);
assert.doesNotMatch(ready, /<details[^>]* open=/, 'signed report choices start collapsed');
const pending = renderToStaticMarkup(<SignedDownloadsView {...props} status="ready" receipt={{ status: 'loading', format: 'pdf' }} />);
assert.equal((pending.match(/disabled=""/g) ?? []).length, 3);
assert.match(pending, /Requesting pdf report/);
const refused = renderToStaticMarkup(<SignedDownloadsView {...props} status="ready" receipt={{ status: 'error', format: 'redline', message: '503 try again' }} />);
assert.ok(refused.indexOf('503 try again') > refused.indexOf('</details>'), 'refusal stays visible outside the closed menu');
assert.match(refused, /role="alert"/);
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
console.log('signed downloads never expose before-review files');
