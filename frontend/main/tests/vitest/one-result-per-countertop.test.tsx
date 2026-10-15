// @vitest-environment jsdom
/**
 * One depth or sink result per countertop (#1201): in the "Other checks" list, a countertop's own
 * result says which countertop it is for, so six results of one check never read alike. A
 * whole-package result is named as before. Made-up values only.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Finding } from '@/data/types';
import { OtherChecks } from '@/components/results/other-checks';
import type { CountertopResult } from '@/api/client';
import { checkWithCountertop } from '@/lib/countertop-results';
import { buildQueue } from '@/lib/needs-you-queue';

vi.mock('@/api/config', () => ({ projectId: () => 'project-1' }));

const NAMES = new Map([['CT-DEPTH-001', 'Countertop depth verification']]);

const result = (id: string, scope_label: string | null): Finding =>
  ({
    id,
    check_id: 'CT-DEPTH-001',
    name: 'CT-DEPTH-001',
    severity: 'FLAG',
    outcome: 'REVIEW_REQUIRED',
    reason: 'Waiting for the countertop depth of this countertop.',
    reviewer_action: null,
    scope_label,
    notes: [],
  }) as unknown as Finding;

describe('one result per countertop (#1201)', () => {
  it('names the countertop a result is for, and only then', () => {
    expect(checkWithCountertop(result('a', 'Countertop row on page 2'), NAMES)).toBe(
      'Countertop depth verification: Countertop row on page 2',
    );
    expect(checkWithCountertop(result('b', 'Package revision'), NAMES)).toBe('Countertop depth verification');
    expect(checkWithCountertop(result('c', null), NAMES)).toBe('Countertop depth verification');
    expect(checkWithCountertop(result('d', 'Countertop 1 on page 3'), new Map())).toBe('Countertop 1 on page 3');
  });

  it('Other checks lists each countertop’s result under its own name', async () => {
    const user = userEvent.setup();
    render(
      <OtherChecks
        findings={[result('a', 'Countertop row on page 1'), result('b', 'Countertop row on page 2')]}
        ruleNames={NAMES}
        blocking={new Set(['a', 'b'])}
        onDecide={() => {}}
        onBulkDismiss={async () => ({ saved: 0, failed: [] })}
      />,
    );
    await user.click(screen.getByRole('button', { name: /Other checks/ }));

    expect(screen.getByText('Countertop depth verification: Countertop row on page 1')).toBeTruthy();
    expect(screen.getByText('Countertop depth verification: Countertop row on page 2')).toBeTruthy();
  });

  it('the Needs-you queue lists a row’s own depth result as a check, never as the row’s width', () => {
    const row = { row_id: 'row-1', finding_id: 'f-width', page_number: 1, label: 'Countertop row on page 1', needs_decision: false } as unknown as CountertopResult;
    const depth = { ...result('f-depth', 'Countertop row on page 1'), scope_row_candidate_id: 'row-1' } as Finding;
    const width = { ...result('f-width-new', 'Countertop row on page 1'), check_id: 'CT-WIDTH-001', scope_row_candidate_id: 'row-1' } as Finding;

    const items = buildQueue([row], [depth, width], new Set(['f-depth', 'f-width-new']));

    // The new width result still belongs to its row's item; the depth result is its own check.
    expect(items.map((item) => item.key)).toEqual(['finding:f-depth']);
  });
});
