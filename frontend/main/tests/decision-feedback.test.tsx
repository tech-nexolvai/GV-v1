import assert from 'node:assert/strict';
import { createDecisionSaver, feedbackText, type DecisionFeedback } from '../src/components/measure/decisionFeedback.js';

async function main() {
  const save = createDecisionSaver();
  const state = new Map<string, DecisionFeedback>();
  let recorded = 0;
  const update = (key: string, feedback: DecisionFeedback) => state.set(key, feedback);
  let release!: () => void;
  const first = save('part-one', () => new Promise<void>((resolve) => { release = resolve; }), update, () => { recorded++; });
  await save('part-one', async () => { throw new Error('duplicate request reached the server'); }, update, () => { recorded++; });
  assert.equal(state.get('part-one')?.kind, 'saving');
  await save('part-two', async () => { throw new Error('server refused'); }, update, () => { recorded++; });
  assert.equal(feedbackText(state.get('part-two')), 'Decision not saved — server refused');
  assert.equal(recorded, 0);
  release();
  await first;
  assert.equal(state.get('part-one')?.kind, 'saved');
  assert.equal(recorded, 1);
  await save('part-two', async () => {}, update, () => { recorded++; });
  assert.equal(recorded, 2);
  console.log('measurement decisions: independent saves, refusal, retry, and duplicate guard');
}

await main();
