import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { SignedDownloadsView } from '../src/components/output/SignedDownloadsView.js';

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
console.log('signed downloads never expose before-review files');
