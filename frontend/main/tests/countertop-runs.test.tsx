import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { CountertopRunFeedback, CountertopRunsLoadState } from '../src/components/measure/CountertopRunFeedback.js';
import { createMeasurementDecisionSaver, type MeasurementDecisionState } from '../src/components/measure/measurementDecisionSave.js';

import { CountertopRunsList } from '../src/components/measure/CountertopRunsList.js';
import {
  isTheSuggestion,
  partLabel,
  runDecisionLabel,
  runsStillToDecide,
  startingSelection,
  type RunCountertop,
  type RunDrawing,
  type RunsList,
} from '../src/components/measure/countertopRunChoices.js';

// One vendor's drawing as `GET …/countertop-runs` lists it (#893): a countertop (part 2) over two
// cabinets (parts 1 and 3), and a cabinet a person added above the top (part 4), which the filter
// left out. Nothing is decided yet.
const signal = 'A confirmed cabinet on the same drawing as the countertop.';
const countertop: RunCountertop = {
  countertop_item_id: 'top',
  number: 2,
  code: null,
  suggestion: {
    members: [
      { item_id: 'left', number: 1, kind: 'cabinet', position: 1, signal },
      { item_id: 'right', number: 3, kind: 'cabinet', position: 2, signal },
    ],
    left_out: [
      {
        item_id: 'wall',
        number: 4,
        kind: 'cabinet',
        reason: 'Left out: this cabinet reaches higher up the page than the countertop’s top.',
      },
    ],
    warnings: [],
    edge_tolerance: '0.004',
  },
  decision: null,
};

const drawing: RunDrawing = {
  view_id: 'v-shop',
  page_index: 1,
  tag: 'panel-3',
  can_confirm: true,
  why_not: null,
  parts: [
    { item_id: 'left', number: 1, kind: 'cabinet', code: null },
    { item_id: 'right', number: 3, kind: 'cabinet', code: null },
    { item_id: 'wall', number: 4, kind: 'cabinet', code: null },
  ],
  countertops: [countertop],
};

const runs: RunsList = { can_suggest: true, why_not: null, drawings: [drawing] };

const html = renderToStaticMarkup(
  <CountertopRunsList
    runs={runs}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
  />,
);

// The question is asked, and how much is left is stated.
assert.match(html, /Parts under each countertop/);
assert.match(html, /Page 2: the vendor&#x27;s drawing/);
assert.match(html, /1 still to decide\./);
assert.equal(runsStillToDecide(runs), 1);

// The suggested run is shown left to right, each part named as "Parts of each drawing" names it,
// with why; the wall cabinet is shown as left out, with why, and is not in the run.
assert.match(html, /Countertop, part 2/);
assert.ok(html.indexOf('part 1 (cabinet)') < html.indexOf('part 3 (cabinet)'));
assert.equal((html.match(/class="countertop-runs__members"/g) ?? []).length, 1);
assert.match(html, /part 4 \(cabinet\): Left out/);
assert.match(html, /Not decided yet\./);

// **The suggestion is ticked for the person to keep or change, never sent without their click.**
// The two suggested parts start ticked; the left-out one does not.
assert.deepEqual(startingSelection(countertop), ['left', 'right']);
assert.equal((html.match(/checked=""/g) ?? []).length, 2);
assert.equal((html.match(/type="checkbox"/g) ?? []).length, 3);
assert.match(html, />Confirm this run<\/button>/);

// **There is no "confirm all".** Two buttons, both about this one countertop.
assert.doesNotMatch(html, /confirm all|accept all|confirm every/i);
assert.equal((html.match(/<button/g) ?? []).length, 2);
assert.match(html, />Not this countertop&#x27;s run<\/button>/);
assert.equal((html.match(/role="group"/g) ?? []).length, 1);
assert.doesNotMatch(html, /aria-pressed="true"/);

// Ticking a different set of parts makes it a correction, whatever order they were ticked in.
assert.equal(isTheSuggestion(countertop, ['right', 'left']), true);
assert.equal(isTheSuggestion(countertop, ['left']), false);
assert.equal(isTheSuggestion(countertop, ['left', 'right', 'wall']), false);
assert.equal(isTheSuggestion({ ...countertop, suggestion: null }, []), false);

// A confirmed run that is still read starts the boxes from what was confirmed, and is said in a
// sentence naming who and which parts.
const confirmed: RunCountertop = {
  ...countertop,
  decision: {
    decision: 'confirmed',
    decided_by: 'reviewer@example.com',
    decided_at: '2026-10-03T12:00:00Z',
    members: [
      { item_id: 'left', number: 1, kind: 'cabinet', position: 1, signal, stands: true },
      { item_id: 'wall', number: 4, kind: 'cabinet', position: 2, signal, stands: true },
    ],
    read: true,
    why_not_read: null,
    edge_tolerance: '0.004',
  },
};
assert.deepEqual(startingSelection(confirmed), ['left', 'wall']);
assert.equal(
  runDecisionLabel(confirmed),
  'Confirmed by reviewer@example.com: part 1 (cabinet), part 4 (cabinet), left to right.',
);

// A confirmed run that is no longer read says why, and the boxes start from the suggestion again.
const stale: RunCountertop = {
  ...confirmed,
  decision: {
    ...confirmed.decision!,
    read: false,
    why_not_read: 'A part in this run was taken back or corrected after the run was confirmed.',
  },
};
assert.deepEqual(startingSelection(stale), ['left', 'right']);
const staleHtml = renderToStaticMarkup(
  <CountertopRunsList
    runs={{ ...runs, drawings: [{ ...drawing, countertops: [stale] }] }}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
  />,
);
assert.match(staleHtml, /role="status">A part in this run was taken back/);
assert.match(staleHtml, /Nothing left to decide\./);

// A withdrawal is the pressed answer.
const withdrawn: RunCountertop = {
  ...countertop,
  decision: {
    decision: 'withdrawn',
    decided_by: 'reviewer@example.com',
    decided_at: '2026-10-03T12:00:00Z',
    members: [],
    read: false,
    why_not_read: null,
    edge_tolerance: null,
  },
};
assert.equal(runDecisionLabel(withdrawn), "Said not to be this countertop's run by reviewer@example.com.");
const withdrawnHtml = renderToStaticMarkup(
  <CountertopRunsList
    runs={{ ...runs, drawings: [{ ...drawing, countertops: [withdrawn] }] }}
    onConfirm={() => undefined}
    onWithdraw={() => undefined}
  />,
);
assert.match(withdrawnHtml, /aria-pressed="true"[^>]*>Not this countertop&#x27;s run/);

// Without a stated tolerance nothing is suggested, nothing can be confirmed, and the page says why.
const unstated: RunsList = {
  can_suggest: false,
  why_not: 'No run can be suggested or confirmed yet: GV_RUN_EDGE_TOLERANCE has not been set.',
  drawings: [{ ...drawing, countertops: [{ ...countertop, suggestion: null }] }],
};
const unstatedHtml = renderToStaticMarkup(
  <CountertopRunsList runs={unstated} onConfirm={() => undefined} onWithdraw={() => undefined} />,
);
assert.equal(runsStillToDecide(unstated), 0);
assert.match(unstatedHtml, /GV_RUN_EDGE_TOLERANCE has not been set/);
assert.match(unstatedHtml, /disabled=""[^>]*>Confirm the ticked parts<\/button>/);

// On a drawing no longer confirmed as the vendor's, the boxes are locked and the page says why.
const architects: RunsList = {
  ...runs,
  drawings: [{ ...drawing, can_confirm: false, why_not: 'This drawing is no longer confirmed as the vendor’s.' }],
};
const architectsHtml = renderToStaticMarkup(
  <CountertopRunsList runs={architects} onConfirm={() => undefined} onWithdraw={() => undefined} />,
);
assert.match(architectsHtml, /<fieldset[^>]*disabled=""/);
assert.match(architectsHtml, /no longer confirmed as the vendor/);

// A part is named by its kind when it has no number.
assert.equal(partLabel({ number: null, kind: 'filler' }), 'an unnumbered filler');

const two = { ...runs, drawings: [{ ...drawing, countertops: [countertop, { ...countertop, countertop_item_id: 'other', number: 5 }] }] };
const pending = renderToStaticMarkup(<CountertopRunsList runs={two}
  decisions={{ top: { kind: 'saving' }, other: { kind: 'saving' } }}
  onConfirm={() => undefined} onWithdraw={() => undefined} />);
assert.equal((pending.match(/<fieldset[^>]*disabled=""/g) ?? []).length, 2);
assert.equal((pending.match(/Waiting for the server/g) ?? []).length, 2);
const locked = renderToStaticMarkup(<CountertopRunsList runs={runs} locked
  onConfirm={() => undefined} onWithdraw={() => undefined} />);
assert.equal((locked.match(/<button[^>]*disabled=""/g) ?? []).length, 2);
assert.match(locked, /<fieldset[^>]*disabled=""/);
const error = renderToStaticMarkup(<CountertopRunFeedback state={{ kind: 'error', message: '409 <conflict>' }} />);
assert.match(error, /409 &lt;conflict&gt;/);
assert.match(error, /ticked parts are kept/);
assert.doesNotMatch(error, /Run decision saved/);
assert.match(renderToStaticMarkup(<CountertopRunFeedback state={{ kind: 'saved' }} />), /Run decision saved/);
const load = (error: string | null, loading: boolean, hasDrawings: boolean) => renderToStaticMarkup(
  <CountertopRunsLoadState error={error} loading={loading} hasDrawings={hasDrawings} onRetry={() => undefined} />);
assert.equal(load(null, false, false), '', 'empty list stays quiet');
assert.match(load(null, true, false), /role="status"/);
assert.match(load('503 <unavailable>', false, true), /previous list is still shown/);
assert.match(load('503 <unavailable>', false, true), /503 &lt;unavailable&gt;/);
assert.match(load('503', true, true), /disabled=""/);

const save = createMeasurementDecisionSaver();
const states: Record<string, MeasurementDecisionState> = {};
let readbacks = 0;
let reject!: (error: Error) => void;
const sent: string[][] = [];
const ui = { state: (id: string, state: MeasurementDecisionState) => { states[id] = state; }, saved: () => { readbacks++; } };
const first = save('top', () => { sent.push(['right', 'left']); return new Promise<void>((_, no) => { reject = no; }); }, ui);
await save('other', async () => undefined, ui);
await save('top', async () => { sent.push(['wall']); }, ui);
assert.equal(states.top.kind, 'saving');
reject(new Error('409 refused')); await first;
assert.equal(readbacks, 1, 'refusal does not refresh or fabricate a decision');
assert.deepEqual(sent, [['right', 'left']], 'no sorting, duplicate submit or automatic retry');
await save('top', async () => { sent.push(['right', 'left']); }, ui);
assert.equal(readbacks, 2);
assert.equal(states.other.kind, 'saved');
const panel = readFileSync('src/pages/MeasurementPanel.tsx', 'utf8');
assert.match(panel, /<CountertopRuns key=\{`runs:\$\{packageId\}`\}/);
assert.match(panel, /onDecided=\{\(\) => setPartsDecided/);
assert.match(readFileSync('src/components/measure/DrawingParts.tsx', 'utf8'), /saved: \(\) => \{\s*setDecided[\s\S]*?onDecided\?\.\(\)/);
console.log('countertop runs: upstream restrictions, independent saves, exact refusals, retry and stale-list locking passed');
