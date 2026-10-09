import assert from 'node:assert/strict';

import { recordReviewDecision } from '../src/pages/recordReviewDecision.js';
import type { Finding } from '../src/data/types.js';

const finding: Finding = {
  id: 'synthetic-finding', check_id: 'SYNTH-001', name: 'Synthetic comparison',
  outcome: 'REVIEW_REQUIRED', severity: 'FLAG', reviewer_action: null,
  reason: 'A reviewer must choose the recorded layout.',
  trace: { operation: 'abstain', operands: [], comparison: 'No layout was chosen.' },
};

const first = { ...finding, id: 'first' };
const second = { ...finding, id: 'second' };
let current = [first, second];
const update = (apply: (items: Finding[]) => Finding[]) => { current = apply(current); };
const firstSave = await recordReviewDecision('first', 'confirm', async () => undefined, update);
assert.deepEqual(firstSave, { saved: true });
const failedSecond = await recordReviewDecision('second', 'dismiss', async () => {
  throw new Error('connection lost');
}, update);
assert.equal(failedSecond.saved, false);
assert.equal(current[0].reviewer_action, 'confirm', 'failed concurrent action cannot roll back an acknowledged one');
assert.equal(current[1].reviewer_action, null, 'failed action cannot look saved');
await recordReviewDecision('second', 'dismiss', async () => undefined, update);
assert.equal(current[1].reviewer_action, 'dismiss', 'explicit retry records only its finding');

console.log('review decisions: acknowledged actions only, one finding at a time');
