import assert from 'node:assert/strict';
import { checksFinished, isReviewWorking, reviewOverview, reviewActionCounts } from '../src/pages/reviewState.js';
import type { Finding } from '../src/data/types.js';

const findings: Finding[] = [
  { id: 'old-fail', check_id: 'CT-DEPTH', name: 'Depth', outcome: 'FAIL', severity: 'FLAG', reviewer_action: null },
  { id: 'old-review', check_id: 'CAB-FILLER', name: 'Filler', outcome: 'REVIEW_REQUIRED', severity: 'FLAG', reviewer_action: null },
];
const previous = findings.map((finding) => finding.id);
assert.deepEqual(reviewActionCounts([
  ...findings.map((finding) => ({ ...finding, reviewer_action: 'confirm' as const })),
  { ...findings[0], id: 'passing', outcome: 'PASS', reviewer_action: 'confirm' },
]), { reviewed: 2, total: 2 }, 'confirmed passing findings cannot make review progress exceed its denominator');

// A queued request can return before the worker moves the previous completed state.
assert.equal(checksFinished('AWAITING_REVIEW', previous, findings, false), false);
assert.equal(checksFinished('RUNNING_CHECKS', previous, [{ id: 'new-finding' }], true), false);
assert.equal(checksFinished('GENERATING_OUTPUTS', previous, [{ id: 'new-finding' }], true), false);
assert.equal(checksFinished('AWAITING_REVIEW', previous, [{ id: 'new-finding' }], false), true);
// A legitimate completed run can have no findings: the observed lifecycle can establish completion.
assert.equal(checksFinished('AWAITING_REVIEW', previous, [], true), true);
assert.equal(checksFinished('AWAITING_REVIEW', previous, [], false), false);
assert.equal(checksFinished('FAILED_PERMANENT', previous, findings, false), true);
assert.equal(checksFinished('NEEDS_INPUT', previous, [], true), true);
assert.equal(isReviewWorking('EXTRACTING'), true);
assert.equal(isReviewWorking('NEEDS_INPUT'), false);

const overview = reviewOverview('package', 'AWAITING_REVIEW', findings, '2026-10-03T00:00:00Z');
assert.deepEqual(overview.findings, findings, 'all actual findings are retained, including abstentions');
assert.equal(overview.findings?.[0], findings[0], 'the overview does not rewrite values, outcomes, or provenance');
assert.equal(overview.narration, undefined, 'a deterministic initial overview does not claim a model was used');
assert.match(overview.content, /2 recorded findings/);
assert.match(reviewOverview('package', 'CREATED', [], '').content, /No findings are available yet/);
assert.match(reviewOverview('package', 'EXTRACTING', [], '').content, /being processed/);
assert.match(reviewOverview('package', 'FAILED_PERMANENT', [], '').content, /needs attention/);
console.log('review state: initial findings, empty/failure states, and asynchronous completion passed');
