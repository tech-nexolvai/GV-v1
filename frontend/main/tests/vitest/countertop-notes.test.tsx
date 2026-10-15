// @vitest-environment jsdom
/**
 * The countertops a held whole-package depth or sink result is about (#1297): named in its notes,
 * not its reason (so a decision carries over while the countertops stay the same), and shown on
 * screen from there. Made-up values only.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Finding } from '@/data/types';
import { OtherChecks } from '@/components/results/other-checks';
import { COUNTERTOP_NOTE_OPENINGS, countertopNotes } from '@/lib/countertop-notes';

vi.mock('@/api/config', () => ({ projectId: () => 'project-1' }));

const NAMES = 'Countertops in this package: Countertop 1 on page 1 and Countertop row on page 2.';
const UNCHECKED = 'Countertops with no result of their own for this check: Countertop row on page 2.';

const held = (notes: string[]): Finding =>
  ({
    id: 'f-depth',
    check_id: 'CT-DEPTH-001',
    name: 'CT-DEPTH-001',
    severity: 'FLAG',
    outcome: 'REVIEW_REQUIRED',
    reason: 'This package has 2 countertops, and this check ran once for the whole package, so one value cannot stand for all of them: held for a person.',
    reviewer_action: null,
    notes,
  }) as Finding;

describe('countertop notes (#1297)', () => {
  it('keeps only the notes that name countertops, in order', () => {
    expect(countertopNotes(held(['Depth reader run 1.', NAMES, UNCHECKED]))).toEqual([NAMES, UNCHECKED]);
    expect(countertopNotes(held(['Widths from confirmed run 1.']))).toEqual([]);
    expect(countertopNotes({ notes: undefined })).toEqual([]);
  });

  it('opens with the server’s own words', () => {
    expect(COUNTERTOP_NOTE_OPENINGS.every((opening) => opening.endsWith(': '))).toBe(true);
  });

  it('Other checks shows the countertops beside the reason', async () => {
    const user = userEvent.setup();
    render(
      <OtherChecks
        findings={[held(['Depth reader run 1.', NAMES])]}
        ruleNames={new Map([['CT-DEPTH-001', 'Countertop depth verification']])}
        blocking={new Set(['f-depth'])}
        onDecide={() => {}}
        onBulkDismiss={async () => ({ saved: 0, failed: [] })}
        packageId="package-1"
        onValuesSaved={() => {}}
        onRunChecks={() => {}}
      />,
    );
    await user.click(screen.getByRole('button', { name: /Other checks/ }));
    await user.hover(screen.getByRole('button', { name: /^Why: / }));

    expect((await screen.findAllByText(NAMES)).length).toBeGreaterThan(0);
    expect(screen.queryAllByText('Depth reader run 1.')).toEqual([]);
  });
});
