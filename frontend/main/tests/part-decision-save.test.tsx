import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { createMeasurementDecisionSaver as createPartDecisionSaver, type MeasurementDecisionState as PartDecisionState } from '../src/components/measure/measurementDecisionSave.js';
import { PartDecisionFeedback } from '../src/components/measure/PartDecisionFeedback.js';

for (const failFirst of [true, false]) {
  const save = createPartDecisionSaver();
  const states: Record<string, PartDecisionState> = {};
  let acknowledgements = 0;
  const ui = { state: (key: string, value: PartDecisionState) => { states[key] = value; },
    saved: () => { acknowledgements++; } };
  let reject!: (reason: Error) => void;
  let resolve!: () => void;
  let requests = 0;
  const first = save('part-a', () => { requests++; return new Promise<void>((_, no) => { reject = no; }); }, ui);
  const second = save('part-b', () => { requests++; return new Promise<void>(yes => { resolve = yes; }); }, ui);
  assert.deepEqual(states, { 'part-a': { kind: 'saving' }, 'part-b': { kind: 'saving' } });
  assert.equal(acknowledgements, 0, 'no optimistic decision');
  await save('part-a', async () => { requests++; }, ui);
  assert.equal(requests, 2, 'another part does not unlock the first for duplicate writes');
  if (failFirst) {
    reject(new Error('409: someone else decided <part>')); await first;
    assert.equal(states['part-b'].kind, 'saving');
    resolve(); await second;
  } else {
    resolve(); await second;
    assert.equal(states['part-a'].kind, 'saving');
    await save('part-a', async () => { requests++; }, ui);
    assert.equal(requests, 2, 'completing another row cannot release this row');
    reject(new Error('409: someone else decided <part>')); await first;
  }
  assert.equal(acknowledgements, 1);
  assert.deepEqual(states['part-a'], { kind: 'error', message: '409: someone else decided <part>' });
  assert.deepEqual(states['part-b'], { kind: 'saved' });
  assert.equal(requests, 2, 'no automatic retry');
  const code = '  CODE / 01  ';
  let sent: string | null = null;
  await save('part-a', async () => { requests++; sent = code; }, ui);
  assert.equal(sent, code, 'the controller does not trim codes or reconstruct any number');
  assert.equal(acknowledgements, 2);
  assert.deepEqual(states['part-a'], { kind: 'saved' });
  // Add forms use their own drawing key and follow the same refusal/retry behavior.
  await save('drawing-add', async () => { throw new Error('422: these ends cannot define a part'); }, ui);
  assert.equal(acknowledgements, 2);
  assert.equal(states['part-b'].kind, 'saved');
}
const render = (state?: PartDecisionState) => renderToStaticMarkup(<PartDecisionFeedback state={state} />);
assert.equal(render(), '');
assert.match(render({ kind: 'saving' }), /role="status".*Saving this decision/);
assert.doesNotMatch(render({ kind: 'saving' }), /Decision saved/);
assert.match(render({ kind: 'saved' }), /Decision saved/);
const error = render({ kind: 'error', message: '409 <conflict>' });
assert.match(error, /role="alert"/);
assert.match(error, /409 &lt;conflict&gt;/);
assert.match(error, /entered code has been kept/);
assert.doesNotMatch(error, /Decision saved/);
console.log('part-decision-save: per-row locks, both completion orders, exact errors/codes, manual retry and acknowledgement-only feedback passed');
