import assert from 'node:assert/strict';
import { createDecisionSaver, type DecisionSaveResult } from '../src/components/chat/decisionSave.js';
import { recordReviewDecision } from '../src/pages/recordReviewDecision.js';
import { reviewActionCounts } from '../src/pages/reviewState.js';
import { handoffAvailability } from '../src/pages/reviewHandoffState.js';
import type { Finding } from '../src/data/types.js';

for (const kind of ['correction', 'exception', 'confirm', 'dismiss']) {
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

// The actual controller used by ReviewPage: concurrent findings may settle in either order.
for (const failFirst of [true, false]) {
  const original: Finding[] = [
    { id: 'missing', check_id: 'A', name: 'A', outcome: 'NOT_FOUND', severity: 'FLAG', reviewer_action: null },
    { id: 'review', check_id: 'B', name: 'B', outcome: 'REVIEW_REQUIRED', severity: 'FLAG', reviewer_action: null,
      found: '9007199254740993/7 in', reason: 'Original <recorded> reason' },
  ];
  let current = original;
  let reject!: (error: Error) => void;
  let resolve!: () => void;
  let updates = 0;
  const update = (apply: (findings: Finding[]) => Finding[]) => { updates += 1; current = apply(current); };
  const dismissal = recordReviewDecision('missing', 'dismiss', () => new Promise<void>((_, no) => { reject = no; }), update);
  const confirmation = recordReviewDecision('review', 'confirm', () => new Promise<void>(yes => { resolve = yes; }), update);
  assert.strictEqual(current, original, 'pending saves do not change findings or counts');
  assert.equal(updates, 0);
  assert.deepEqual(reviewActionCounts(current), { reviewed: 0, total: 2 });
  assert.equal(handoffAvailability({ findingsCount: 2, needsAction: 2, approved: false,
    sessionCompleted: false, signing: false, working: false }).signOffDisabled, true);

  // A newly loaded row or an unrelated saved decision must not be reverted by a stale snapshot.
  const extra: Finding = { ...original[0], id: 'new-record', reviewer_action: 'except' };
  current = [...current, extra];
  if (failFirst) { reject(new Error('503 rejected')); await dismissal; resolve(); await confirmation; }
  else { resolve(); await confirmation; reject(new Error('503 rejected')); await dismissal; }
  assert.equal(updates, 1, 'only the successful response updates the UI');
  assert.equal((await dismissal).saved, false);
  assert.equal((await confirmation).saved, true);
  assert.strictEqual(current[0], original[0], 'failed finding untouched');
  assert.strictEqual(current[2], extra, 'unrelated record retained by identity');
  assert.deepEqual(current[1], { ...original[1], reviewer_action: 'confirm' });
  assert.equal(original[1].reviewer_action, null, 'original backend-shaped object is never mutated');
  assert.deepEqual(reviewActionCounts(current), { reviewed: 2, total: 3 });
  await recordReviewDecision('missing', 'dismiss', async () => {}, update);
  assert.deepEqual(current.map(f => f.reviewer_action), ['dismiss', 'confirm', 'except']);
  assert.deepEqual(current.map(f => f.outcome), ['NOT_FOUND', 'REVIEW_REQUIRED', 'NOT_FOUND']);
  assert.equal(current[1].found, '9007199254740993/7 in', 'no number conversion');
  const before = current;
  await recordReviewDecision('review', 'confirm', async () => { throw new Error('session refused'); }, update);
  assert.strictEqual(current, before, 'session/network failure cannot roll back a prior acknowledgement');
}
{
  let complete!: () => void;
  let current: Finding[] = [];
  const request = recordReviewDecision('previous-run-finding', 'confirm',
    () => new Promise<void>(resolve => { complete = resolve; }),
    apply => { current = apply(current); });
  complete();
  await request;
  assert.deepEqual(current, [], 'a late save cannot reinsert an old finding after the displayed run changes');
}
console.log('decision-save: all decisions await acknowledgement; concurrent failure preserves saved rows, exact values and verdicts; drafts and submit locks retained');
