import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { collectFindingPages, decisionPayload, canSignOff, drawingPoints, resultGroups } from '../src/components/output/reviewerResults.js';
import { rowWallSelection } from '../src/components/measure/slotReaderReview.js';
import type { Finding } from '../src/data/types.js';

const first = Array.from({ length: 50 }, (_, i) => ({ id: `finding-${i}` }));
const last = Array.from({ length: 6 }, (_, i) => ({ id: `tail-${i}` }));
const seen: (string | undefined)[] = [];
assert.equal((await collectFindingPages(async (cursor) => {
  seen.push(cursor);
  return cursor ? { items: last, next_cursor: null } : { items: first, next_cursor: 'tail' };
})).length, 56);
assert.deepEqual(seen, [undefined, 'tail']);
await assert.rejects(collectFindingPages(async () => ({ items: first, next_cursor: 'again' })), /complete|repeat/i);
await assert.rejects(collectFindingPages(async (cursor) => {
  if (cursor) throw new Error('failed next page');
  return { items: first, next_cursor: 'next' };
}), /failed next page/);
for (const outcome of ['NOT_FOUND', 'REVIEW_REQUIRED'] as const) {
  for (const action of ['confirm', 'dismiss'] as const) {
    assert.throws(() => decisionPayload('one', outcome, action, '  '), /note/i);
    assert.deepEqual(decisionPayload('one', outcome, action, ' Checked detail '), {
      finding_id: 'one', action, note: 'Checked detail',
    });
  }
}
assert.equal(canSignOff(null), false);
assert.equal(canSignOff({ can_approve: false, blocking_findings: 0 }), false, 'lifecycle refusal still blocks');
assert.equal(canSignOff({ can_approve: true, blocking_findings: 0 }), true);
assert.equal(canSignOff({ can_approve: true, blocking_findings: 1 }), false);
assert.deepEqual(drawingPoints([['0.1','0.2'],['0.5','0.2'],['0.5','0.4']], 1000, 500), [[100,100],[500,100],[500,200]]);
assert.throws(() => drawingPoints([['2','0'],['0','1'],['1','1']], 100, 100), /outline/);
assert.equal(rowWallSelection(undefined, null), '', 'AI proposals are not the selected value');
assert.equal(rowWallSelection('back_only', null), 'back_only');
assert.equal(rowWallSelection(undefined, 'back_only'), 'back_only', 'a saved reviewer choice can be shown');
const base: Finding = { id:'one', check_id:'SYNTH', name:'width', severity:'FLAG', outcome:'REVIEW_REQUIRED', reviewer_action:null };
const located = { ...base, row_location: { page_number:3, page_id:'p', document_version_id:'d', coordinate_space:'stored', polygon:[] } };
assert.deepEqual(resultGroups([base, located]).map(g => g.page), [3, null]);
const page = readFileSync('src/pages/ReviewPage.tsx','utf8');
assert.match(page, /getApprovalReadiness/);
assert.match(page, /canSignOff\(readiness\)/);
assert.match(page, /onChecksQueued=/);
assert.match(page, /<ResultsPanel/);
const card = readFileSync('src/components/chat/FindingCard.tsx','utf8');
assert.match(card, /onAction\(finding.id, pending, reason.trim\(\)\)/, 'notes reach the write handler');
assert.match(card, /Reviewer decision/);
const screen = readFileSync('src/components/output/ResultsPanel.tsx','utf8');
assert.match(screen, /Only what needs me/);
assert.match(screen, /Show on drawing/);
assert.match(screen, /Open countertop card/);
assert.match(screen, /REVIEW_REQUIRED: 'Needs you'/);
assert.match(screen, /NOT_FOUND: 'Not checkable'/);
assert.match(screen, /PASS: 'PASS'/);
assert.match(screen, /FAIL: 'FAIL'/);
console.log('reviewer results: complete pagination, notes, readiness, normalized outlines, row grouping and explicit walls');
