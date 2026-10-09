import { describe, expect, it } from 'vitest';

import { reviewStage, type ReviewFacts, type StepId, type StepState } from '@/lib/review-stage';

const base: ReviewFacts = { state: 'AWAITING_REVIEW', findingsTotal: 9, readiness: { blockingFindings: 3, canApprove: false, reason: '3 findings need a decision' }, exports: null };

function states(facts: ReviewFacts): Record<StepId, StepState> {
  return Object.fromEntries(reviewStage(facts).steps.map((s) => [s.id, s.state])) as Record<StepId, StepState>;
}

describe('reviewStage: one step per state', () => {
  it('upload: nothing stored yet', () => {
    const stage = reviewStage({ ...base, state: 'CREATED', findingsTotal: 0, readiness: null });
    expect(stage.current).toBe('upload');
    expect(states({ ...base, state: 'CREATED', findingsTotal: 0, readiness: null })).toMatchObject({ upload: 'current', reading: 'upcoming', report: 'upcoming' });
    expect(stage.next).toMatchObject({ kind: 'wait-upload', disabled: true });
  });

  it.each(['UPLOADED', 'INGESTING', 'EXTRACTING', 'MATCHING', 'VALIDATING_EVIDENCE'])('reading: package state %s', (state) => {
    const stage = reviewStage({ ...base, state, findingsTotal: 0, readiness: null });
    expect(stage.current).toBe('reading');
    expect(stage.next).toMatchObject({ kind: 'wait-reading', label: 'Reading…', disabled: true });
  });

  it('reading: the measurement screen says it is still reading, with the stage named', () => {
    const stage = reviewStage({ ...base, state: 'NEEDS_INPUT', stillReading: true, readingStage: 'VALIDATING_EVIDENCE', findingsTotal: 0, readiness: null });
    expect(stage.current).toBe('reading');
    expect(stage.steps[1].note).toBe('validating evidence');
    expect(stage.next.reason).toMatch(/validating evidence/);
  });

  it('checks: reading finished, no recorded run yet → Run checks', () => {
    const stage = reviewStage({ ...base, state: 'NEEDS_INPUT', findingsTotal: 0, readiness: { blockingFindings: 0, canApprove: false, reason: 'No findings' } });
    expect(states({ ...base, state: 'NEEDS_INPUT', findingsTotal: 0, readiness: null })).toMatchObject({ upload: 'done', reading: 'done', checks: 'current', decisions: 'upcoming' });
    expect(stage.next).toEqual({ kind: 'run-checks', label: 'Run checks', disabled: false, reason: null });
  });

  it.each(['RUNNING_CHECKS', 'GENERATING_OUTPUTS'])('checks: running (%s)', (state) => {
    const stage = reviewStage({ ...base, state });
    expect(stage.current).toBe('checks');
    expect(stage.next).toMatchObject({ kind: 'checking', disabled: true });
  });

  it('checks: queued from this screen', () => {
    expect(reviewStage({ ...base, checksQueued: true }).next.kind).toBe('checking');
  });

  it('checks: values saved after the last run → Run checks again, with a short reason', () => {
    const stage = reviewStage({ ...base, valuesChangedSinceRun: true });
    expect(stage.current).toBe('checks');
    expect(stage.steps[2].note).toBe('Values changed since the last run');
    expect(stage.next.kind).toBe('run-checks');
  });

  it('decisions: N items need the reviewer → Review N items', () => {
    const stage = reviewStage(base);
    expect(states(base)).toMatchObject({ checks: 'done', decisions: 'current', signoff: 'upcoming' });
    expect(stage.steps[2].count).toBe(9);
    expect(stage.steps[3].count).toBe(3);
    expect(stage.next).toEqual({ kind: 'review', label: 'Review 3 items', disabled: false, reason: null });
    expect(reviewStage({ ...base, readiness: { blockingFindings: 1, canApprove: false, reason: null } }).next.label).toBe('Review 1 item');
  });

  it('decisions: readiness not loaded → no count, never 0, and not done', () => {
    const stage = reviewStage({ ...base, readiness: null });
    expect(stage.current).toBe('decisions');
    expect(stage.steps[3].count).toBeNull();
    expect(stage.next.label).toBe('Review results');
  });

  it('sign off: nothing blocks → Sign off enabled', () => {
    const ready = { ...base, readiness: { blockingFindings: 0, canApprove: true, reason: null } };
    expect(states(ready)).toMatchObject({ decisions: 'done', signoff: 'current', report: 'upcoming' });
    expect(reviewStage(ready).next).toEqual({ kind: 'sign-off', label: 'Sign off', disabled: false, reason: null });
  });

  it('sign off: the server refuses with nothing left to decide → blocked, with its reason on the step and the button', () => {
    const refused = { ...base, readiness: { blockingFindings: 0, canApprove: false, reason: 'An earlier sign-off is still open' } };
    const stage = reviewStage(refused);
    expect(stage.steps[4]).toMatchObject({ state: 'blocked', note: 'An earlier sign-off is still open' });
    expect(stage.next).toMatchObject({ kind: 'sign-off', disabled: true, reason: 'An earlier sign-off is still open' });
  });

  it('report: approved, files being prepared', () => {
    const stage = reviewStage({ ...base, state: 'APPROVED', exports: 'preparing' });
    expect(states({ ...base, state: 'APPROVED', exports: 'preparing' })).toMatchObject({ decisions: 'done', signoff: 'done', report: 'current' });
    expect(stage.next).toMatchObject({ kind: 'preparing-report', disabled: true });
    expect(stage.steps[3].count).toBeNull();
  });

  it('report: approved, files failed → blocked step, and no button that promises a retry (#1064)', () => {
    const stage = reviewStage({ ...base, state: 'APPROVED', exports: 'failed' });
    expect(stage.steps[5].state).toBe('blocked');
    // Asking again returns the same failed request; only an admin's retry helps.
    expect(stage.next).toMatchObject({ kind: 'prepare-report', label: 'Report failed', disabled: true });
    expect(stage.next.reason).toMatch(/admin/);
  });

  it('report: approved, files never requested → Prepare report', () => {
    expect(reviewStage({ ...base, state: 'APPROVED', exports: 'not_requested' }).next).toMatchObject({ kind: 'prepare-report', label: 'Prepare report', disabled: false });
  });

  it('report: approved, export status unknown → waits, never offers a download', () => {
    expect(reviewStage({ ...base, state: 'APPROVED', exports: null }).next).toMatchObject({ disabled: true });
  });

  it('all done: approved and the report is ready → Download report', () => {
    const stage = reviewStage({ ...base, state: 'APPROVED', exports: 'ready' });
    expect(stage.current).toBeNull();
    expect(Object.values(states({ ...base, state: 'APPROVED', exports: 'ready' }))).toEqual(Array(6).fill('done'));
    expect(stage.next).toEqual({ kind: 'download-report', label: 'Download report', disabled: false, reason: null });
  });

  it('failed for good: the current step is blocked and nothing claims success', () => {
    const stage = reviewStage({ ...base, state: 'FAILED_PERMANENT', findingsTotal: 0, readiness: null });
    expect(stage.steps.find((s) => s.id === stage.current)?.state).toBe('blocked');
    expect(stage.next).toMatchObject({ kind: 'none', label: 'Processing failed', disabled: true });
    expect(stage.next.reason).toMatch(/do not mean the drawing passed/);
  });

  it('retrying: the current step stays current with a short note', () => {
    const stage = reviewStage({ ...base, state: 'FAILED_RETRYABLE', findingsTotal: 0, readiness: null });
    const current = stage.steps.find((s) => s.id === stage.current);
    expect(current?.state).toBe('current');
    expect(current?.note).toMatch(/retry/);
  });

  it.each(['CANCELLED', 'SUPERSEDED'])('historical (%s): no action', (state) => {
    expect(reviewStage({ ...base, state }).next).toMatchObject({ kind: 'none', disabled: true });
  });

  it('never marks a step done from a missing fact', () => {
    const unknown = reviewStage({ state: 'AWAITING_REVIEW', findingsTotal: null, readiness: null, exports: null });
    expect(unknown.current).toBe('checks');
    expect(unknown.steps.filter((s) => s.state === 'done').map((s) => s.id)).toEqual(['upload', 'reading']);
  });
});
