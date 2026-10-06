import assert from 'node:assert/strict';
import { fieldReview, REASON, reviewCounts } from '../src/pages/fieldReview.js';
import type { ReviewedCandidate } from '../src/pages/fieldReview.js';

const m = (entries: Array<[string, ReviewedCandidate]>) => new Map<string, ReviewedCandidate>(entries);

const agreed: ReviewedCandidate = { corroboration_status: 'CORROBORATED', corroboration_lane: 'SECOND_READER' };
const base = {
  hasValue: true,
  reviewerOwned: false,
  proposedCandidateIds: ['a'],
  candidates: m([['a', agreed]]),
  placementUnverified: false,
};

// Both readers agreed: no question, no picture.
assert.deepEqual(fieldReview(base), { state: 'agreed', reason: null });
// One reader only, or a different lane: a question, never "agreed".
assert.deepEqual(
  fieldReview({ ...base, candidates: m([['a', { corroboration_status: 'RAW_CANDIDATE' }]]) }),
  { state: 'needs_look', reason: REASON.oneReader },
);
assert.equal(
  fieldReview({
    ...base,
    candidates: m([['a', { corroboration_status: 'CORROBORATED', corroboration_lane: 'DUAL_UNIT' }]]),
  }).state,
  'needs_look',
);
// Readers disagree.
assert.deepEqual(
  fieldReview({ ...base, candidates: m([['a', { corroboration_status: 'CONFLICTING' }]]) }),
  { state: 'needs_look', reason: REASON.differ },
);
// Any one of several readings not agreed makes the whole field a question.
assert.equal(
  fieldReview({
    ...base,
    proposedCandidateIds: ['a', 'b'],
    candidates: m([['a', agreed], ['b', { corroboration_status: 'RAW_CANDIDATE' }]]),
  }).state,
  'needs_look',
);
// The form reader's own reason wins when it gives one.
assert.deepEqual(
  fieldReview({ ...base, candidates: m([['a', { ...agreed, review_reason: 'Stacked fraction.' }]]) }),
  { state: 'needs_look', reason: 'Stacked fraction.' },
);
// Placement not checked, missing reading, empty, reviewer-owned.
assert.equal(fieldReview({ ...base, placementUnverified: true }).reason, REASON.unplaced);
assert.equal(fieldReview({ ...base, candidates: m([]) }).reason, REASON.missing);
assert.deepEqual(fieldReview({ ...base, hasValue: false }), { state: 'empty', reason: REASON.empty });
assert.deepEqual(fieldReview({ ...base, reviewerOwned: true }), { state: 'done', reason: null });
assert.equal(fieldReview({ ...base, proposedCandidateIds: null }).state, 'needs_look');

assert.deepEqual(
  reviewCounts([fieldReview(base), fieldReview({ ...base, hasValue: false }), fieldReview({ ...base, placementUnverified: true })]),
  { agreed: 1, needs_look: 1, done: 0, empty: 1 },
);
console.log('fields both readers agreed on need no look; every other field says why');
