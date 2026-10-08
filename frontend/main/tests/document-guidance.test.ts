import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { documentNextStep } from '../src/pages/documentNextStep.js';
import type { DocumentRow } from '../src/pages/documentRows.js';
import type { ApprovalReadiness, FindingCounts } from '../src/api/client';

// The Documents card's next step (#1034) replaced the "Next step" prose. The same safety rules hold:
// counts alone never approve, processing never looks finished, unavailable is not empty.
const zero: FindingCounts = { total: 0, passed: 0, failed: 0, review_required: 0, not_found: 0, no_applicable_rule: 0, critical_failed: 0 };
const mixed: FindingCounts = { ...zero, total: 9, failed: 1, not_found: 8 };
const ready = (blocking: number, canApprove: boolean): ApprovalReadiness => ({
  revision_id: '00000000-0000-4000-8000-000000000001', blocking_findings: blocking, blocking_finding_ids: [], can_approve: canApprove, reason: canApprove ? null : 'synthetic reason',
});
const row = (state: string, counts: FindingCounts | null, extra: Partial<DocumentRow> = {}) =>
  ({ document: { state } as DocumentRow['document'], counts, ...extra }) as DocumentRow;
const original = JSON.stringify(mixed);

assert.equal(documentNextStep(row('AWAITING_REVIEW', mixed, { readiness: ready(3, false) })).label, 'Review 3 items');
assert.equal(documentNextStep(row('AWAITING_REVIEW', mixed, { readiness: ready(1, false) })).label, 'Review 1 item');
assert.equal(documentNextStep(row('AWAITING_REVIEW', mixed)).label, 'Review results', 'no readiness → no invented count');
assert.equal(documentNextStep(row('AWAITING_REVIEW', mixed, { readiness: ready(0, true) })).label, 'Sign off', 'only the server says ready');
for (const counts of [{ ...zero, total: 2, passed: 2 }, { ...zero, total: 2, no_applicable_rule: 2 }]) {
  assert.notEqual(documentNextStep(row('AWAITING_REVIEW', counts)).label, 'Sign off', 'counts alone never approve');
}
const refused = documentNextStep(row('AWAITING_REVIEW', mixed, { readiness: ready(0, false) }));
assert.equal(refused.disabled, true);
assert.equal(refused.reason, 'synthetic reason');
assert.equal(documentNextStep(row('NEEDS_INPUT', zero)).label, 'Run checks');
assert.equal(documentNextStep(row('AWAITING_REVIEW', null)).label, 'Results unavailable', 'unavailable is not empty');
for (const state of ['EXTRACTING', 'VALIDATING_EVIDENCE']) assert.equal(documentNextStep(row(state, mixed)).label, 'Reading…');
for (const state of ['RUNNING_CHECKS', 'GENERATING_OUTPUTS']) assert.equal(documentNextStep(row(state, mixed)).label, 'Checking…');
// FAILED_RETRYABLE is left to the server's readiness answer, exactly as the review's Sign off button is.
for (const state of ['EXTRACTING', 'RUNNING_CHECKS']) {
  assert.doesNotMatch(documentNextStep(row(state, mixed, { readiness: ready(0, true) })).label, /Sign off|Download/, 'processing never looks finished');
}
assert.equal(documentNextStep(row('APPROVED', mixed, { exports: 'ready' })).label, 'Download report');
assert.equal(documentNextStep(row('APPROVED', mixed, { exports: 'preparing' })).label, 'Preparing report…');
assert.equal(documentNextStep(row('APPROVED', mixed)).disabled, true, 'unknown export status never offers a download');
for (const state of ['CANCELLED', 'SUPERSEDED']) assert.equal(documentNextStep(row(state, mixed)).kind, 'none');
assert.equal(documentNextStep(row('FAILED_PERMANENT', zero)).label, 'Processing failed');
assert.equal(JSON.stringify(mixed), original);

const page = readFileSync('src/pages/PackagesPage.tsx', 'utf8');
assert.match(page, /row.counts.total > 0/, 'no count note for missing or zero summaries');
assert.match(page, /\{row.counts.total\} recorded check/, 'total comes directly from the API');
assert.match(page, /not drawings or individual dimensions/);
assert.doesNotMatch(page, /Next step<\/strong>/, 'the prose is gone; the card shows the next action instead');
console.log('document next step: same words as the review header; counts never approve; processing, unavailable and terminal states stay honest');
