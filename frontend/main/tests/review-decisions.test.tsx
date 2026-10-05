import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { createDecisionSaver, type DecisionSaveResult } from '../src/components/chat/decisionSave.js';
import { recordReviewDecision } from '../src/pages/recordReviewDecision.js';
import type { Finding } from '../src/data/types.js';

const cardSource = readFileSync('src/components/chat/FindingCard.tsx', 'utf8');
assert.match(cardSource, /defaultExpanded \|\| finding\.outcome === 'FAIL'/,
  'first table opening reveals the card body even for a non-failed finding');
assert.match(cardSource, /finding\.reason/);
assert.match(cardSource, /finding\.notes\.map/, 'persisted provenance stays visible in detail');
assert.match(cardSource, /Recorded trace/);
assert.match(cardSource, /aria-expanded=\{showTrace\}/);
const evidenceSource = readFileSync('src/components/chat/EvidencePanel.tsx', 'utf8');
assert.match(evidenceSource, /downloadEvidenceCrop\(projectId, packageId, evidence\.canonical_observation_id\)/);
assert.match(evidenceSource, /<img className="pdf-pane__crop-image"/);
assert.doesNotMatch(evidenceSource, /downloadDocument|<PdfViewer/,
  'a stored image crop cannot be replaced by a full PDF or passed to a PDF renderer');

const finding: Finding = {
  id: 'synthetic-finding', check_id: 'SYNTH-001', name: 'Synthetic comparison',
  outcome: 'REVIEW_REQUIRED', severity: 'FLAG', reviewer_action: null,
  reason: 'A reviewer must choose the recorded layout.',
  trace: { operation: 'abstain', operands: [], comparison: 'No layout was chosen.' },
};

for (const kind of ['correction', 'exception', 'simple action']) {
  const save = createDecisionSaver();
  let draft = '7/8 in';
  let saved = 0;
  let error: string | null = null;
  const busy: boolean[] = [];
  const ui = {
    busy: (value: boolean) => busy.push(value),
    error: (value: string | null) => { error = value; },
    saved: () => { saved += 1; draft = ''; },
  };
  await save(async () => ({ saved: false, error: 'The server refused this decision.' }), ui);
  assert.equal(draft, '7/8 in', `${kind}: rejected save retains draft`);
  assert.equal(saved, 0);
  assert.match(error!, /server refused/);
  await save(async () => { throw new Error('connection lost'); }, ui);
  assert.equal(draft, '7/8 in', `${kind}: failed request retains draft`);
  assert.match(error!, /connection lost/);
  let resolve!: (result: DecisionSaveResult) => void;
  let requests = 0;
  const pending = save(() => { requests += 1; return new Promise((done) => { resolve = done; }); }, ui);
  await save(async () => { requests += 1; return { saved: true }; }, ui);
  assert.equal(requests, 1, `${kind}: duplicate submission is blocked`);
  resolve({ saved: true });
  await pending;
  assert.equal(saved, 1);
  assert.equal(draft, '', `${kind}: acknowledged success clears draft`);
  assert.equal(busy.at(-1), false);
}

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

console.log('review decisions: first-click detail, recorded reason, retained drafts, and acknowledged actions');
