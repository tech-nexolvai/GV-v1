import assert from 'node:assert/strict';
import { createDecisionSaver, type DecisionSaveResult } from '../src/components/chat/decisionSave.js';

for (const kind of ['correction', 'exception']) {
  const save = createDecisionSaver();
  const original = { value: '9007199254740993/7 in', reason: 'Recorded <text>\nunchanged', date: '2030-01-01' };
  let draft: typeof original | null = { ...original };
  const busy: boolean[] = [];
  const errors: (string | null)[] = [];
  let saved = 0;
  const ui = { busy: (v: boolean) => busy.push(v), error: (v: string | null) => errors.push(v),
    saved: () => { saved += 1; draft = null; } };
  for (const refusal of ['No authoritative reading', 'Several drawing readings', '422: correction needs a unit']) {
    await save(async () => ({ saved: false, error: refusal }), ui);
    assert.deepEqual(draft, original, `${kind}: refusal retains every field`);
    assert.equal(errors.at(-1), refusal, 'show original error, not a fabricated success');
    assert.equal(saved, 0);
  }
  await save(async () => { throw new Error('disconnected'); }, ui);
  assert.deepEqual(draft, original, 'unknown outcome preserves input; no automatic retry');
  assert.match(errors.at(-1)!, /Save was not confirmed.*disconnected/);
  let resolve!: (result: DecisionSaveResult) => void;
  let requests = 0;
  const pending = save(() => { requests += 1; return new Promise(r => { resolve = r; }); }, ui);
  assert.equal(busy.at(-1), true);
  assert.equal(errors.at(-1), null, 'manual retry clears the prior error');
  assert.deepEqual(draft, original, 'pending is not success');
  await save(async () => { requests += 1; return { saved: true }; }, ui);
  assert.equal(requests, 1, 'rapid submit/Enter cannot duplicate an in-flight write');
  resolve({ saved: true });
  await pending;
  assert.equal(saved, 1);
  assert.equal(draft, null, 'only acknowledged success clears the form');
  assert.deepEqual(busy, [true, false, true, false, true, false, true, false, true, false]);
}
console.log('decision-save: refused and rejected saves retain drafts; in-flight submissions locked; only success clears');
