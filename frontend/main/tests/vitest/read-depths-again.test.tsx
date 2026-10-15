// @vitest-environment jsdom
/**
 * "Read depths again" (#1308) on a waiting depth or sink check: shown only when the server says the
 * depth and sink reader is switched on, it always asks first because it costs money, says about how
 * many questions and how many are free, and says why it cannot be asked.
 *
 * Synthetic data only (#1106): nothing here comes from a client drawing.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Finding } from '@/data/types';
import { getReadDepthsAgain, readDepthsAgain, type ReadDepthsAgainStatus } from '@/api/client';
import { NeedsValues } from '@/components/results/needs-values';
import { forgetReadDepthsAgain, questionsWords } from '@/lib/read-depths-again';

vi.mock('@/api/config', () => ({ projectId: () => 'project-1' }));
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  getReadDepthsAgain: vi.fn(),
  readDepthsAgain: vi.fn(async () => ({ accepted_id: 'work-1', package_revision_id: 'revision-1' })),
}));

const ON: ReadDepthsAgainStatus = {
  switched_on: true,
  can_ask: true,
  refused: null,
  checks: ['CT-DEPTH-001', 'CT-SINK-CUTOUT-WIDTH-001'],
  estimate: {
    pages: 3,
    countertops: 2,
    questions_low: 12,
    questions_high: 24,
    stored_answers: 0,
    basis: 'About 6 to 12 questions for each countertop.',
  },
  last: null,
};

const finding = (checkId: string): Finding =>
  ({
    id: 'f-depth',
    check_id: checkId,
    name: checkId,
    severity: 'FLAG',
    outcome: 'NOT_FOUND',
    reviewer_action: null,
    missing_inputs: [
      {
        key: 'countertop_depth',
        label: 'Countertop depth',
        unit: 'in',
        where_to_find: "The vendor's plan or section.",
        scope: 'drawing',
        askable: false,
        sources: [],
        saved: null,
      },
    ],
    depth_readings: [],
  }) as unknown as Finding;

beforeEach(() => {
  forgetReadDepthsAgain();
  vi.mocked(getReadDepthsAgain).mockReset();
  vi.mocked(readDepthsAgain).mockClear();
});

describe('Read depths again (#1308)', () => {
  it('is not shown when the depth and sink reader is switched off', async () => {
    vi.mocked(getReadDepthsAgain).mockResolvedValue({ switched_on: false, can_ask: false, refused: null, checks: [], estimate: null, last: null });
    render(<NeedsValues finding={finding('CT-DEPTH-001')} packageId="package-1" onSaved={() => {}} />);

    await vi.waitFor(() => expect(getReadDepthsAgain).toHaveBeenCalledWith('project-1', 'package-1'));
    expect(screen.queryByRole('button', { name: /Read depths again/ })).toBeNull();
  });

  it('is not shown on a check the reader does not fill', async () => {
    vi.mocked(getReadDepthsAgain).mockResolvedValue(ON);
    render(<NeedsValues finding={finding('CT-WIDTH-001')} packageId="package-1" onSaved={() => {}} />);

    await vi.waitFor(() => expect(getReadDepthsAgain).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /Read depths again/ })).toBeNull();
  });

  it('asks first, says it costs money and about how many questions, then queues it', async () => {
    const user = userEvent.setup();
    vi.mocked(getReadDepthsAgain).mockResolvedValue(ON);
    render(<NeedsValues finding={finding('CT-DEPTH-001')} packageId="package-1" onSaved={() => {}} />);

    await user.click(await screen.findByRole('button', { name: 'Read depths again…' }));

    expect(readDepthsAgain).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog', { name: 'Read depths again?' });
    expect(dialog.textContent).toContain('This costs money');
    expect(dialog.textContent).toContain('About 12 to 24 questions');
    expect(dialog.textContent).toContain('2 countertops on 3 pages');

    await user.click(screen.getByRole('button', { name: 'Read depths again (costs money)' }));

    expect(readDepthsAgain).toHaveBeenCalledWith('project-1', 'package-1');
    expect(await screen.findByText(/The depths are read in the background/)).toBeTruthy();
  });

  it('cancelling asks nothing', async () => {
    const user = userEvent.setup();
    vi.mocked(getReadDepthsAgain).mockResolvedValue(ON);
    render(<NeedsValues finding={finding('CT-DEPTH-001')} packageId="package-1" onSaved={() => {}} />);

    await user.click(await screen.findByRole('button', { name: 'Read depths again…' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(readDepthsAgain).not.toHaveBeenCalled();
  });

  it('says why it cannot be asked, and the last request in the server words', async () => {
    vi.mocked(getReadDepthsAgain).mockResolvedValue({
      ...ON,
      can_ask: false,
      refused: 'Depths have already been asked to be read again on this drawing set.',
      last: { request_id: 'work-1', asked_at: '2026-10-15T10:00:00Z', state: 'waiting', sentence: 'Waiting to start, or reading now.' },
    });
    render(<NeedsValues finding={finding('CT-DEPTH-001')} packageId="package-1" onSaved={() => {}} />);

    const button = await screen.findByRole('button', { name: 'Read depths again…' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Depths have already been asked to be read again on this drawing set.')).toBeTruthy();
    expect(screen.getByText('Last request: Waiting to start, or reading now.')).toBeTruthy();
  });

  it('shows a refusal from the server and keeps the dialog', async () => {
    const user = userEvent.setup();
    vi.mocked(getReadDepthsAgain).mockResolvedValue(ON);
    vi.mocked(readDepthsAgain).mockRejectedValueOnce(new Error('This drawing set is being read or checked right now.'));
    render(<NeedsValues finding={finding('CT-DEPTH-001')} packageId="package-1" onSaved={() => {}} />);

    await user.click(await screen.findByRole('button', { name: 'Read depths again…' }));
    await user.click(screen.getByRole('button', { name: 'Read depths again (costs money)' }));

    expect((await screen.findByRole('alert')).textContent).toBe('This drawing set is being read or checked right now.');
    expect(screen.getByRole('dialog', { name: 'Read depths again?' })).toBeTruthy();
  });

  it('says exactly how many when an earlier reading counted them', () => {
    expect(questionsWords({ ...ON.estimate!, questions_low: 80, questions_high: 80 })).toBe('About 80 questions');
    expect(questionsWords({ ...ON.estimate!, questions_low: 1, questions_high: 1 })).toBe('About 1 question');
  });
});
