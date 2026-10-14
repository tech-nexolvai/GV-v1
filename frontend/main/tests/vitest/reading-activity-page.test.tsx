// @vitest-environment jsdom
/**
 * The review page shows `PackageOut.activity` (#1239) under the stepper: live while reading, the
 * summary after, and nothing while the API sends none. The package answer is mocked from the design
 * of #1238; every value is made up.
 */
import { useState } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ReviewPage } from '@/pages/ReviewPage';

vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const STOPPED = { page: 3, agent: 'Rekha', role: 'the line finder', item: 'p2:row-choice', state: 'stopped', code: 'readers-differ', change: null, attempt: null, line: "Page 3: the line finder's two AIs picked different lines.", at: '2026-10-14T10:00:01Z' };

describe('ReviewPage: the live reading activity (#1239)', () => {
  let pkg: Record<string, unknown>;

  beforeEach(() => {
    pkg = { id: 'pkg', project_id: 'p', vendor: 'Synthetic vendor', state: 'EXTRACTING', created_at: '2026-10-14T09:00:00Z', current_revision_id: 'rev-1', current_revision_number: 1, product_type: 'countertop' };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input).replace(/^.*\/api\/v1/, '');
      if (url === '/projects/p/packages/pkg') return json(pkg);
      if (url.startsWith('/projects/p/packages/pkg/findings')) return json({ items: [], next_cursor: null });
      if (url === '/projects/p/review-sessions') return json({ items: [] });
      if (url.endsWith('/approval-readiness')) return json({ revision_id: 'rev-1', can_approve: false, blocking_findings: 2, blocking_finding_ids: [], reason: 'synthetic' });
      if (url === '/projects/p/packages/pkg/revisions') return json({ package_id: 'pkg', current_revision_number: 1, can_start_revision: false, unfinished_revision: null, items: [] });
      if (url.endsWith('/countertop-results')) return json({ package_id: 'pkg', revision_id: 'rev-1', items: [] });
      if (url === '/rules') return json([]);
      // Measurements while reading: its own "Reading these drawings" block shows.
      if (url.includes('/required-inputs')) {
        return json({ quantities: [], page_numbers: [1], confirmed_readings: [], proposed_readings: [], parameters: [], discriminators: [], rules_published: 0, still_reading: true, revision_state: 'EXTRACTING' });
      }
      return json({ error: 'http_error', message: `not in this test: ${url}`, request_id: 'r' }, 404);
    }));
  });

  function Page({ onRestartingChange }: { onRestartingChange?: (restarting: boolean) => void } = {}) {
    const [actions, setActions] = useState<HTMLElement | null>(null);
    const [title, setTitle] = useState<HTMLElement | null>(null);
    return (
      <TooltipProvider>
        <ShellSlotsContext.Provider value={{ title, actions }}>
          <div ref={setTitle} data-testid="header-title" />
          <div ref={setActions} data-testid="header-actions" />
          <ReviewPage sessionId="pkg" onEvidenceChange={() => {}} onBackToDocuments={() => {}} onRestartingChange={onRestartingChange} />
        </ShellSlotsContext.Provider>
      </TooltipProvider>
    );
  }

  it('while reading: the page and what stopped, under the stepper', async () => {
    pkg.reading = { stage: 'rows', label: "AI readers finding each page's countertop line", pages_done: 2, pages_total: 9, calls: 4, cost_usd: '0.040000', unpriced_calls: 0, retrying: false, updated_at: null };
    // While reading: `now` is the newest line (it repeats the row, so the page count leads).
    pkg.activity = { reading_id: 'r1', finished: false, updated_at: '2026-10-14T10:00:01Z', now: STOPPED.line, items: [STOPPED], summary: { passed: 4, stopped: 1, tried_again: 0, helped: 0, waiting: 1, sentence: 'From the reading, 1 item waits for you.' } };
    render(<Page />);
    const panel = await waitFor(() => {
      const found = document.querySelector('[data-slot="reading-activity"]');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    // Right under the stepper.
    expect(document.querySelector('[data-slot="review-stepper"]')?.nextElementSibling).toBe(panel);
    expect(within(panel).getByText('Reading page 3 of 9…')).toBeTruthy();
    expect(within(panel).getAllByText(STOPPED.line)).toHaveLength(1);
    expect(within(panel).getByText('Line finder')).toBeTruthy();
    expect(panel.textContent).not.toContain('Rekha');
  });

  it('on Measurements during a reading: the panel shows exactly once (#1240 review)', async () => {
    const user = userEvent.setup();
    pkg.reading = { stage: 'rows', label: "AI readers finding each page's countertop line", pages_done: 2, pages_total: 9, calls: 4, cost_usd: '0.040000', unpriced_calls: 0, retrying: false, updated_at: null };
    pkg.activity = { reading_id: 'r1', finished: false, updated_at: '2026-10-14T10:00:01Z', now: STOPPED.line, items: [STOPPED], summary: { passed: 0, stopped: 1, tried_again: 0, helped: 0, waiting: 1, sentence: 'From the reading, 1 item waits for you.' } };
    render(<Page />);
    await waitFor(() => expect(document.querySelector('[data-slot="reading-activity"]')).not.toBeNull());
    await user.click(screen.getByRole('tab', { name: 'Measurements' }));
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Measurements' }).getAttribute('aria-selected')).toBe('true'));
    await screen.findAllByText('Reading these drawings');
    expect(document.querySelectorAll('[data-slot="reading-activity"]')).toHaveLength(1);
    expect(screen.getAllByText(STOPPED.line)).toHaveLength(1);
  });

  it('after reading: the server\'s summary, and "Open Decisions" opens the queue', async () => {
    const user = userEvent.setup();
    pkg.state = 'AWAITING_REVIEW';
    const sentence = 'Tried again on 2 items: 1 now agrees; 1 item waits for you.';
    pkg.activity = { reading_id: 'r1', finished: true, updated_at: '2026-10-14T10:00:09Z', now: sentence, items: [STOPPED], summary: { passed: 12, stopped: 3, tried_again: 2, helped: 1, waiting: 1, sentence } };
    render(<Page />);
    const panel = await waitFor(() => {
      const found = document.querySelector('[data-slot="reading-activity"]');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(within(panel).getByRole('status').textContent).toBe(`${sentence} Open Decisions`);
    expect(panel.querySelector('[data-slot="what-stopped"] summary')?.textContent).toBe('Show what stopped (1)');
    await user.click(within(panel).getByRole('button', { name: 'Open Decisions' }));
    // Decisions is the "Needs you" queue (loaded on first open).
    await waitFor(() => expect(document.querySelector('[data-slot="needs-you-queue"]')).not.toBeNull(), { timeout: 5000 });
  });

  it('the header badge agrees with the Reading step while a failed reading restarts (#1249)', async () => {
    pkg.state = 'FAILED_RETRYABLE';
    pkg.reading = { stage: 'rows', label: "AI readers finding each page's countertop line", pages_done: 0, pages_total: 9, calls: 0, cost_usd: '0', unpriced_calls: 0, retrying: true, updated_at: null };
    const restarting = vi.fn();
    render(<Page onRestartingChange={restarting} />);
    await waitFor(() => expect(document.querySelector('[data-slot="review-stepper"] [data-slot="reading-progress"]')?.textContent).toContain('Starting the reading again after a problem'));
    const badge = screen.getByTestId('header-title').querySelector('[data-slot="package-status-badge"]');
    expect(badge?.textContent).toBe('Restarting after a problem');
    // The sidebar's badge for this review hears the same answer, so all three places agree.
    expect(restarting).toHaveBeenLastCalledWith(true);
  });

  it('the header badge says "Stopped" when nothing restarts (#1249)', async () => {
    pkg.state = 'FAILED_RETRYABLE';
    const restarting = vi.fn();
    render(<Page onRestartingChange={restarting} />);
    await waitFor(() => expect(screen.getByTestId('header-title').querySelector('[data-slot="package-status-badge"]')?.getAttribute('data-status')).toBe('FAILED_RETRYABLE'));
    const badge = screen.getByTestId('header-title').querySelector('[data-slot="package-status-badge"]');
    expect(badge?.textContent).toBe('Stopped after a problem');
    expect(restarting).not.toHaveBeenCalledWith(true);
    expect(document.body.textContent).not.toContain('Restarting');
  });

  it('activity null while reading (before the attempt\'s first event): no panel, the progress line stays', async () => {
    pkg.reading = { stage: 'rows', label: "AI readers finding each page's countertop line", pages_done: 2, pages_total: 9, calls: 4, cost_usd: '0.040000', unpriced_calls: 0, retrying: false, updated_at: null };
    render(<Page />);
    // The package has loaded: the stepper shows its reading line.
    await waitFor(() => expect(document.querySelector('[data-slot="review-stepper"] [data-slot="reading-progress"]')?.textContent).toContain('page 3 of 9'));
    expect(document.querySelector('[data-slot="reading-activity"]')).toBeNull();
  });
});
