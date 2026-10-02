import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReviewHandoff } from '../src/pages/ReviewHandoff.js';
import { ReviewPackageDetails } from '../src/pages/ReviewPackageDetails.js';
import { handoffAvailability, receiveReport } from '../src/pages/reviewHandoffState.js';
import type { ReportFormat } from '../src/pages/reviewHandoffState.js';

const ready = {
  findingsCount: 3, needsAction: 0, approved: false, sessionCompleted: false, signing: false, working: false,
};
assert.equal(handoffAvailability(ready).signOffDisabled, false);
for (const block of [
  { findingsCount: 0 }, { needsAction: 1 }, { approved: true },
  { sessionCompleted: true }, { signing: true }, { working: true },
]) {
  assert.equal(handoffAvailability({ ...ready, ...block }).signOffDisabled, true, JSON.stringify(block));
}
assert.equal(handoffAvailability(ready).downloadsDisabled, true);
assert.equal(handoffAvailability({ ...ready, approved: true }).downloadsDisabled, false);
assert.equal(handoffAvailability({ ...ready, approved: true, working: true }).downloadsDisabled, true);

const props = {
  ...ready, reviewed: 2, requiringReview: 2, download: { status: 'idle' } as const,
  onSignOff: () => undefined, onDownload: () => undefined,
};
const unsigned = renderToStaticMarkup(<ReviewHandoff {...props} />);
assert.match(unsigned, /Sign Off/);
assert.doesNotMatch(unsigned, /<details/, 'exports are unavailable until actual approval');
assert.match(unsigned, /Sign off to unlock reports/);
const missing = renderToStaticMarkup(<ReviewHandoff {...props} findingsCount={0} />);
assert.match(missing, /disabled=""/);
assert.match(missing, /Run checks before signing off/);
const waiting = renderToStaticMarkup(<ReviewHandoff {...props} working />);
assert.match(waiting, /Wait for the current checks/);
assert.match(waiting, /disabled=""/);
const approved = renderToStaticMarkup(<ReviewHandoff {...props} approved />);
assert.match(approved, /<details class="review-handoff__downloads">/);
assert.doesNotMatch(approved, /<details[^>]* open=/, 'download disclosure starts closed');
for (const label of ['PDF report', 'Findings workbook', 'Drawing redline']) assert.ok(approved.includes(label));
assert.equal((approved.match(/class="review-handoff__option"/g) ?? []).length, 3, 'all existing artifacts remain offered');
assert.doesNotMatch(approved, />Sign Off</);
assert.match(approved, /redline requires located drawing evidence/);
const closedSession = renderToStaticMarkup(<ReviewHandoff {...props} sessionCompleted />);
assert.match(closedSession, /session is closed/);
assert.doesNotMatch(closedSession, /Signed off\./, 'a closed session is not evidence of package approval');
const loading = renderToStaticMarkup(<ReviewHandoff {...props} approved download={{ status: 'loading', format: 'pdf' }} />);
assert.match(loading, /Requesting pdf report/);
assert.equal((loading.match(/disabled=""/g) ?? []).length, 3);
assert.doesNotMatch(loading, /Download started/);
const refused = renderToStaticMarkup(<ReviewHandoff {...props} approved download={{ status: 'error', format: 'redline', message: 'No located evidence exists.' }} />);
assert.match(refused, /role="alert"/);
assert.ok(refused.indexOf('No located evidence exists.') > refused.indexOf('</details>'), 'error remains visible with exports collapsed');
assert.match(refused, /Signed off\./, 'download refusal does not undo recorded approval');

// Exercise real receipt logic without server requests or generated substitute artifacts.
const delivered: Blob[] = [];
for (const format of ['pdf', 'workbook', 'redline'] as const) {
  const blob = new Blob([`synthetic ${format} response`]);
  await receiveReport(format, async (requested: ReportFormat) => {
    assert.equal(requested, format);
    return blob;
  }, (received) => delivered.push(received));
  assert.equal(delivered.at(-1), blob, 'the exact response blob, not recreated report content, is handed off');
}
let deliverCalls = 0;
await assert.rejects(receiveReport('pdf', async () => { throw new Error('403 not approved'); }, () => { deliverCalls += 1; }), /403 not approved/);
await assert.rejects(receiveReport('workbook', async () => new Blob([]), () => { deliverCalls += 1; }), /empty report/);
assert.equal(deliverCalls, 0);
let resolve!: (blob: Blob) => void;
const pending = receiveReport('redline', () => new Promise<Blob>((done) => { resolve = done; }), () => { deliverCalls += 1; });
assert.equal(deliverCalls, 0, 'starting a request does not claim a report was received');
resolve(new Blob(['synthetic response bytes']));
await pending;
assert.equal(deliverCalls, 1);
console.log('review handoff: approval gates, closed disclosure, 3 artifacts, pending/empty/error responses, exact blob delivery passed');

const packageDetails = renderToStaticMarkup(<ReviewPackageDetails
  packageId="00000000-0000-4000-8000-000000000101"
  projectId="00000000-0000-4000-8000-000000000201">
  <ol aria-label="Recorded workflow"><li>Upload</li><li>Review</li></ol>
</ReviewPackageDetails>);
assert.match(packageDetails, /<details class="review-package-details">/);
assert.doesNotMatch(packageDetails, /<details[^>]* open=/, 'secondary details start collapsed, not deleted');
for (const content of ['Details &amp; steps', 'Record IDs', 'Package', 'Project',
  '00000000-0000-4000-8000-000000000101', '00000000-0000-4000-8000-000000000201',
  'Recorded workflow', 'Upload', 'Review', 'deterministic checks', 'optional AI narration']) {
  assert.ok(packageDetails.includes(content), `preserve ${content}`);
}
assert.doesNotMatch(packageDetails, /data-tooltip/, 'method explanation is readable on touch, not hover-only');
assert.match(packageDetails, /tabindex="0" role="region" aria-label="Package details and review steps"/,
  'scrollable details remain reachable by keyboard');
assert.doesNotMatch(packageDetails, /<button/, 'secondary context cannot approve, rerun or mutate records');
console.log('review package details: exact IDs, passed workflow and touch-readable explanation retained');
