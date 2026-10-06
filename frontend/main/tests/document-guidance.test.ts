import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { documentGuidance } from '../src/pages/documentGuidance.js';
import type { FindingCounts } from '../src/api/client';

const zero: FindingCounts = { total: 0, passed: 0, failed: 0, review_required: 0, not_found: 0, no_applicable_rule: 0, critical_failed: 0 };
const mixed: FindingCounts = { ...zero, total: 9, failed: 1, not_found: 8 };
const original = JSON.stringify(mixed);
const next = documentGuidance('AWAITING_REVIEW', mixed);
assert.match(next, /inspect failed checks and their evidence/);
assert.match(next, /confirm missing inputs in Measurements/);
assert.doesNotMatch(next, /reviewer decision|approve|sign off|\b9\b/, 'advice must not invent a decision or recalculate counts');
assert.match(documentGuidance('AWAITING_REVIEW', { ...zero, total: 1, review_required: 1 }), /reviewer decision/);
assert.match(documentGuidance('NEEDS_INPUT', { ...zero, total: 1, passed: 1 }), /missing inputs/, 'package state still matters when recorded checks pass');
assert.match(documentGuidance('AWAITING_REVIEW', zero), /not a pass/);
assert.match(documentGuidance('CREATED', zero), /check uploads/);
assert.match(documentGuidance('CREATED', null), /missing summary/, 'unavailable is not empty');
for (const state of ['EXTRACTING', 'RUNNING_CHECKS', 'GENERATING_OUTPUTS', 'FAILED_RETRYABLE']) {
  assert.match(documentGuidance(state, mixed), /do not mean processing has finished/);
  assert.doesNotMatch(documentGuidance(state, mixed), /confirm missing|inspect failed/);
}
assert.match(documentGuidance('APPROVED', mixed), /approved review/);
assert.doesNotMatch(documentGuidance('APPROVED', mixed), /confirm missing/, 'do not reopen approved findings through guidance');
for (const state of ['CANCELLED', 'SUPERSEDED']) assert.match(documentGuidance(state, mixed), /historical record/);
assert.match(documentGuidance('FAILED_PERMANENT', zero), /processing failure/);
assert.match(documentGuidance('A_FUTURE_STATE', mixed), /recorded status/);
for (const counts of [{ ...zero, total: 2, passed: 2 }, { ...zero, total: 2, no_applicable_rule: 2 }]) {
  assert.match(documentGuidance('AWAITING_REVIEW', counts), /do not approve/);
  assert.doesNotMatch(documentGuidance('AWAITING_REVIEW', counts), /ready to|sign off|all checks passed/i);
}
assert.equal(JSON.stringify(mixed), original);
const page = readFileSync('src/pages/PackagesPage.tsx', 'utf8');
assert.match(page, /row.counts.total > 0/, 'no count note for missing or zero summaries');
assert.match(page, /\{row.counts.total\} recorded check/, 'total comes directly from the API');
assert.match(page, /not drawings or individual dimensions/);
console.log('document guidance: counts-only meaning, processing/empty/unavailable distinctions, human review, terminal states, and unchanged facts passed');
