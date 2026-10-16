// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { AssistantStep } from '@/api/assistantTypes';
import { AssistantSteps } from '@/components/assistant/assistant-steps';
import { stepsSummary } from '@/lib/assistant-steps';

// Synthetic data only: nothing here comes from a client drawing.
const STEPS: AssistantStep[] = [
  { kind: 'plan', label: 'Updated the task list', ok: true, tasks: [{ title: 'Render pages', status: 'done' }] },
  { kind: 'bash', label: 'Ran a command', command: 'pdftotext -f 2 -l 2 /work/made-up.pdf -', output: 'MADE-UP COUNTERTOP 40', ok: true },
  { kind: 'view', label: 'Viewed /out/p-02.png', path: '/out/p-02.png', ok: true },
  { kind: 'review_results', label: 'Read the recorded results for page 2', page: 2, ok: true },
  {
    kind: 'delegate', label: 'Ran 2 agents · Page 1', ok: false,
    agents: [
      { title: 'Page 1', pages: [1], ok: true, report: '{"summary": "Made-up report"}', steps: [{ kind: 'bash', label: 'Ran a command', command: 'ls /out', output: 'p-01.png', ok: true }] },
      { title: 'Page 2', pages: [2], ok: false, stopped: 'cost_cap', report: 'Stopped at the cost cap before reporting.', steps: [] },
    ],
  },
];

describe('assistant steps', () => {
  it('sums up what the assistant did like claude.ai', () => {
    expect(stepsSummary(STEPS)).toBe('Ran 1 command, viewed 1 picture, read the results, ran 2 agents');
    expect(stepsSummary([])).toBe('');
  });

  it('folds the commands, their output and each helper agent', async () => {
    const user = userEvent.setup();
    render(<AssistantSteps steps={STEPS} plan={[{ title: 'Render pages', status: 'done' }, { title: 'Compare', status: 'in_progress' }]} />);
    const fold = screen.getByText('Ran 1 command, viewed 1 picture, read the results, ran 2 agents');
    const details = fold.closest('details');
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    await user.click(fold);
    expect(details?.open).toBe(true);
    expect(screen.getByText('pdftotext -f 2 -l 2 /work/made-up.pdf -')).toBeTruthy();
    expect(screen.getByText('MADE-UP COUNTERTOP 40')).toBeTruthy();
    expect(screen.getByText('Ran 2 agents · Page 1')).toBeTruthy();
    expect(screen.getByText('Page 1 · Ran 1 command')).toBeTruthy();
    expect(screen.getByText('Page 2 · stopped: cost cap')).toBeTruthy();
    const plan = screen.getByText('Progress').closest('[data-slot="assistant-plan"]') as HTMLElement;
    expect(within(plan).getByText('1/2')).toBeTruthy();
    expect(within(plan).getByText('(Working on it)')).toBeTruthy();
  });

  it('shows nothing for a one-call answer', () => {
    const { container } = render(<AssistantSteps steps={[]} />);
    expect(container.innerHTML).toBe('');
  });
});
