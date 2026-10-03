import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';

import { DrawingRolesList } from '../src/components/measure/DrawingRolesList.js';
import { DrawingRolesLoadState } from '../src/components/measure/DrawingRolesFeedback.js';
import { createMeasurementDecisionSaver, type MeasurementDecisionState } from '../src/components/measure/measurementDecisionSave.js';
import { roleLabel, stillToConfirm, type DrawingView } from '../src/components/measure/drawingRoleChoices.js';

// Two drawings on one sheet, as `GET …/views` lists them: neither confirmed, both suggested by the
// heading the sheet prints above them (#710).
const unconfirmed: DrawingView[] = [
  {
    view_id: 'v-arch',
    page_index: 2,
    tag: 'panel-0',
    role: null,
    suggested_role: 'arch',
    suggested_from: 'ID SET ELEVATION ',
    reason: 'the heading above it',
  },
  {
    view_id: 'v-shop',
    page_index: 2,
    tag: 'panel-1',
    role: null,
    suggested_role: 'shop',
    suggested_from: "VENDOR'S SHOP DRAWING ELEVATION",
    reason: 'the heading above it',
  },
];

const html = renderToStaticMarkup(
  <DrawingRolesList views={unconfirmed} onChoose={() => undefined} />,
);

// The question is asked, and how much is left is stated.
assert.match(html, /Which drawing is which\?/);
assert.match(html, /2 still to confirm\./);
assert.equal(stillToConfirm(unconfirmed), 2);

// **The suggestion is shown, never applied.** Nothing is pressed until a reviewer presses it.
assert.match(html, /the label suggests: Architect&#x27;s drawing/);
assert.match(html, /labelled “ID SET ELEVATION”/);
assert.doesNotMatch(html, /aria-pressed="true"/);

// Each drawing offers both answers.
assert.equal((html.match(/Architect&#x27;s drawing<\/button>/g) ?? []).length, 2);
assert.equal((html.match(/Vendor&#x27;s drawing<\/button>/g) ?? []).length, 2);

// Once a person has answered, that answer is the pressed one and the suggestion is not repeated.
const confirmed = renderToStaticMarkup(
  <DrawingRolesList
    views={[{ ...unconfirmed[0], role: 'arch' }, unconfirmed[1]]}
    decisions={{ 'v-shop': { kind: 'saving' } }}
    onChoose={() => undefined}
  />,
);
assert.match(confirmed, /1 still to confirm\./);
assert.equal((confirmed.match(/aria-pressed="true"/g) ?? []).length, 1);
assert.equal((confirmed.match(/the label suggests/g) ?? []).length, 1);
// The drawing being saved cannot be answered twice.
assert.equal((confirmed.match(/disabled=""/g) ?? []).length, 2);

// A two-PDF package's one drawing per page takes the upload's side: shown, and not asked about.
const fromUpload = renderToStaticMarkup(
  <DrawingRolesList
    views={[{ ...unconfirmed[1], upload_side: 'shop' }]}
    onChoose={() => undefined}
  />,
);
assert.match(fromUpload, /from the upload: Vendor&#x27;s drawing/);
assert.match(fromUpload, /All confirmed\./);
assert.doesNotMatch(fromUpload, /the label suggests/);

assert.equal(roleLabel('arch'), "Architect's drawing");
assert.equal(roleLabel(null), null);

const pending = renderToStaticMarkup(<DrawingRolesList views={unconfirmed}
  decisions={{ 'v-arch': { kind: 'saving' }, 'v-shop': { kind: 'saving' } }} onChoose={() => undefined} />);
assert.equal((pending.match(/disabled=""/g) ?? []).length, 4);
assert.equal((pending.match(/Waiting for the server/g) ?? []).length, 2);
assert.doesNotMatch(pending, /aria-pressed="true"/);

const refused = renderToStaticMarkup(<DrawingRolesList
  views={[{ ...unconfirmed[0], role: 'arch' }, unconfirmed[1]]}
  decisions={{ 'v-arch': { kind: 'error', message: '409 <role conflict>' }, 'v-shop': { kind: 'saved' } }}
  onChoose={() => undefined} />);
assert.match(refused, /role="alert".*Drawing role save was not confirmed/);
assert.match(refused, /409 &lt;role conflict&gt;/);
assert.equal((refused.match(/Drawing role saved/g) ?? []).length, 1);
assert.equal((refused.match(/aria-pressed="true"/g) ?? []).length, 1, 'a refusal does not replace recorded roles');
assert.doesNotMatch(refused, /disabled=""/);

const load = (error: string | null, loading: boolean) => renderToStaticMarkup(
  <DrawingRolesLoadState error={error} loading={loading} onRetry={() => undefined} />);
assert.equal(load(null, false), '', 'empty successful list stays quiet');
assert.match(load(null, true), /role="status".*Loading drawing roles/);
assert.match(load('503 <unavailable>', false), /503 &lt;unavailable&gt;/);
assert.match(load('503', false), /Retry drawing list/);
assert.match(load('503', true), /disabled=""/);
assert.match(load('503', true), /Retrying drawing list/);

// Same keyed controller as parts: one role's acknowledgement cannot release another role's lock.
const save = createMeasurementDecisionSaver();
const states: Record<string, MeasurementDecisionState> = {};
const bodies: { role: string }[] = [];
let refreshes = 0;
let reject!: (error: Error) => void;
const ui = { state: (key: string, value: MeasurementDecisionState) => { states[key] = value; },
  saved: () => { refreshes++; } };
const first = save('v-arch', () => { bodies.push({ role: 'arch' });
  return new Promise<void>((_, no) => { reject = no; }); }, ui);
await save('v-shop', async () => { bodies.push({ role: 'shop' }); }, ui);
await save('v-arch', async () => { bodies.push({ role: 'shop' }); }, ui);
assert.equal(states['v-arch'].kind, 'saving');
assert.equal(refreshes, 1);
reject(new Error('503 role save refused'));
await first;
assert.equal(refreshes, 1, 'failed confirmation does not refresh readings');
assert.deepEqual(bodies, [{ role: 'arch' }, { role: 'shop' }], 'no duplicate, guess or automatic retry');
await save('v-arch', async () => { bodies.push({ role: 'arch' }); }, ui);
assert.equal(refreshes, 2);
assert.equal(states['v-arch'].kind, 'saved');
// These are siblings: sharing packageId as their key leaves stale DOM after a role refresh.
const panel = readFileSync('src/pages/MeasurementPanel.tsx', 'utf8');
assert.match(panel, /<DrawingRoles\s+key=\{`roles:\$\{packageId\}`\}/);
assert.match(panel, /<DrawingParts\s+key=\{`parts:\$\{packageId\}`\}/);
console.log('drawing-roles: independent saves, retained roles, exact refusals, explicit retries and load recovery passed');
