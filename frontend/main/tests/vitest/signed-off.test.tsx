// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';
import { createDecisionSaver, feedbackText, type DecisionFeedback } from '@/components/measure/decisionFeedback';
import { isClosedState, refuseClosed, SIGNED_OFF_NOTE, SignedOffContext } from '@/lib/signed-off';

vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
}));

// Synthetic numbers only (#1182): one countertop and one package-level check still needing someone.
const exact = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });
const ROW: CountertopResult = {
  finding_id: 'f-need', row_id: 'need-p9', page_number: 9, label: 'Countertop row on page 9', row_location: null,
  outcome: 'REVIEW_REQUIRED', needs_decision: true, printed_overall: exact('84', '1', '84"'), pieces: [],
  field_cut_per_end: exact('1', '1', '1"'), field_cut_count: 2, expected_total: null, delta: null,
  hold: { code: 'stone-short-of-ends', reason: 'The stone stops short of the walls.' }, reviewer_decision: null,
  wall_layout: { config: 'back_left_right', label: 'back wall and both ends', source: 'drawing clues' },
  agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true] },
};
const finding = (id: string, extra: Partial<Finding> = {}): Finding =>
  ({ id, check_id: `CHECK-${id}`, name: `Synthetic ${id}`, severity: 'FLAG', outcome: 'NOT_FOUND', reviewer_action: null, ...extra }) as Finding;

function dashboard(signedOff: boolean) {
  const handlers = { onAction: vi.fn(), onCorrect: vi.fn(), onExcept: vi.fn() };
  return render(
    <SignedOffContext.Provider value={signedOff}>
      <ResultsDashboard
        countertops={{ status: 'ready', rows: [ROW] } as CountertopsState}
        findings={[finding('f-need', { outcome: 'REVIEW_REQUIRED' }), finding('o-depth', { scope_label: 'Countertop depth' })]}
        blockingIds={new Set(['f-need', 'o-depth'])}
        busy={false}
        filter={null}
        onFilterChange={vi.fn()}
        onRetry={vi.fn()}
        onRefresh={vi.fn()}
        handlers={handlers as never}
        onBulkDismiss={vi.fn()}
        onShowDrawing={vi.fn()}
        onOpenCard={vi.fn()}
      />
    </SignedOffContext.Provider>,
  );
}

describe('after sign-off nothing is offered for deciding (#1182)', () => {
  it('offers Decide on an open review', () => {
    dashboard(false);
    expect(screen.getAllByRole('button', { name: 'Decide' }).length).toBeGreaterThan(0);
  });

  it('offers no Decide once the review is signed off: the server would refuse it', () => {
    dashboard(true);
    expect(screen.queryAllByRole('button', { name: 'Decide' })).toHaveLength(0);
  });
});

describe('which reviews are closed to change (#1182)', () => {
  it.each(['APPROVED', 'CHANGES_REQUESTED', 'SUPERSEDED', 'CANCELLED'])('%s is closed', (state) => {
    expect(isClosedState(state)).toBe(true);
  });

  it.each(['AWAITING_REVIEW', 'RUNNING_CHECKS', 'NEEDS_INPUT', null, undefined])('%s is open', (state) => {
    expect(isClosedState(state)).toBe(false);
  });

  it('a closed review sends nothing and says why where the saved answer would be', async () => {
    const save = createDecisionSaver();
    const shown = new Map<string, DecisionFeedback>();
    const saved = vi.fn();

    await save('part-one', refuseClosed, (key, feedback) => shown.set(key, feedback), saved);

    expect(saved).not.toHaveBeenCalled();
    expect(feedbackText(shown.get('part-one'))).toBe(`Decision not saved — ${SIGNED_OFF_NOTE}`);
  });
});
