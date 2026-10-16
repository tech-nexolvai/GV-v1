// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { AssistantStep } from '@/api/assistantTypes';
import { AssistantSteps } from '@/components/assistant/assistant-steps';
import { stepsSummary } from '@/lib/assistant-steps';

// Synthetic data only: nothing here comes from a client drawing.
const STEPS: AssistantStep[] = [
  { kind: 'task', label: 'Added task: Render pages', ok: true, tasks: [{ id: '1', subject: 'Render pages', status: 'pending' }] },
  { kind: 'bash', label: 'Inspect page 2 text', command: 'pdftotext -f 2 -l 2 /mnt/user-data/uploads/made-up.pdf -', output: 'MADE-UP COUNTERTOP 40', ok: true },
  { kind: 'view', label: 'Look at page 2', path: '/home/claude/p-02.png', ok: true },
  { kind: 'review_results', label: 'Read the recorded results for page 2', page: 2, ok: true },
  {
    kind: 'agents', label: 'Ran 2 agents · Page 1', ok: false,
    agents: [
      { description: 'Page 1', model: 'sonnet', ok: true, report: '{"summary": "Made-up report"}', steps: [{ kind: 'bash', label: 'List the pictures', command: 'ls /home/claude', output: 'p-01.png', ok: true }] },
      { description: 'Page 2', model: 'sonnet', ok: false, stopped: 'cost_cap', report: 'Stopped at the cost cap before reporting.', steps: [] },
    ],
  },
];

describe('assistant steps', () => {
  it('sums up what the assistant did like claude.ai', () => {
    expect(stepsSummary(STEPS)).toBe('Ran 1 command, viewed 1 picture, read the results, ran 2 agents');
    expect(stepsSummary([STEPS[0]])).toBe('Kept a task list');
    expect(stepsSummary([])).toBe('');
  });

  it('folds each step under its own description, with commands, output and each helper agent', async () => {
    const user = userEvent.setup();
    render(
      <AssistantSteps
        steps={STEPS}
        plan={[{ id: '1', subject: 'Render pages', status: 'completed' }, { id: '2', subject: 'Verify', active_form: 'Verifying findings', status: 'in_progress' }]}
      />,
    );
    const fold = screen.getByText('Ran 1 command, viewed 1 picture, read the results, ran 2 agents');
    const details = fold.closest('details');
    expect(details?.open).toBe(false);
    await user.click(fold);
    expect(details?.open).toBe(true);
    expect(screen.getByText('Added task: Render pages')).toBeTruthy();
    expect(screen.getByText('Inspect page 2 text')).toBeTruthy();
    expect(screen.getByText('pdftotext -f 2 -l 2 /mnt/user-data/uploads/made-up.pdf -')).toBeTruthy();
    expect(screen.getByText('MADE-UP COUNTERTOP 40')).toBeTruthy();
    expect(screen.getByText('Ran 2 agents · Page 1')).toBeTruthy();
    expect(screen.getByText('Page 1 · Ran 1 command')).toBeTruthy();
    expect(screen.getByText('Page 2 · stopped: cost cap')).toBeTruthy();
    const plan = screen.getByText('Progress').closest('[data-slot="assistant-plan"]') as HTMLElement;
    expect(within(plan).getByText('1/2')).toBeTruthy();
    expect(within(plan).getByText('Verifying findings')).toBeTruthy();
    expect(within(plan).getByText('(Working on it)')).toBeTruthy();
  });

  it('shows nothing for a one-call answer', () => {
    const { container } = render(<AssistantSteps steps={[]} />);
    expect(container.innerHTML).toBe('');
  });
});
