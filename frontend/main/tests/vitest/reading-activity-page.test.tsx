// @vitest-environment jsdom
/**
 * The review page shows `PackageOut.activity` (#1239) under the stepper: live while reading, the
 * summary after, and nothing while the API sends none. The package answer is mocked from the design
 * of #1238; every value is made up.
 */
import { useState } from 'react';
import { render, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ReviewPage } from '@/pages/ReviewPage';

vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const STOPPED = { page: 3, agent: 'Rekha', role: 'the line finder', item: 'p2:row-choice', state: 'stopped', code: 'readers-differ', change: null, attempt: null, line: 'Page 3: the line finder stopped: the two AIs picked different lines.', at: '2026-10-14T10:00:01Z' };

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
      return json({ error: 'http_error', message: `not in this test: ${url}`, request_id: 'r' }, 404);
    }));
  });

  function Page() {
    const [actions, setActions] = useState<HTMLElement | null>(null);
    const [title, setTitle] = useState<HTMLElement | null>(null);
    return (
      <TooltipProvider>
        <ShellSlotsContext.Provider value={{ title, actions }}>
          <div ref={setTitle} data-testid="header-title" />
          <div ref={setActions} data-testid="header-actions" />
          <ReviewPage sessionId="pkg" onEvidenceChange={() => {}} onBackToDocuments={() => {}} />
        </ShellSlotsContext.Provider>
      </TooltipProvider>
    );
  }

  it('while reading: the page and what stopped, under the stepper', async () => {
    pkg.reading = { stage: 'rows', label: "AI readers finding each page's countertop line", pages_done: 2, pages_total: 9, calls: 4, cost_usd: '0.040000', unpriced_calls: 0, retrying: false, updated_at: null };
    pkg.activity = { reading_id: 'r1', updated_at: null, now: null, items: [STOPPED], summary: { passed: 4, stopped: 1, tried_again: 0, helped: 0, waiting: 1, sentence: null } };
    render(<Page />);
    const panel = await waitFor(() => {
      const found = document.querySelector('[data-slot="reading-activity"]');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    // Right under the stepper.
    expect(document.querySelector('[data-slot="review-stepper"]')?.nextElementSibling).toBe(panel);
    expect(within(panel).getByText('Reading page 3 of 9…')).toBeTruthy();
    expect(within(panel).getByText('Page 3: the line finder stopped: the two AIs picked different lines.')).toBeTruthy();
    expect(within(panel).getByText('Line finder')).toBeTruthy();
    expect(panel.textContent).not.toContain('Rekha');
  });

  it('after reading: the summary, whose counts open Decisions', async () => {
    const user = userEvent.setup();
    pkg.state = 'AWAITING_REVIEW';
    pkg.activity = { reading_id: 'r1', updated_at: null, now: 'Tried again on 2 items: 1 now agree; 1 item waits for you.', items: [STOPPED], summary: { passed: 12, stopped: 3, tried_again: 2, helped: 1, waiting: 1, sentence: 'Tried again on 2 items: 1 now agree; 1 item waits for you.' } };
    render(<Page />);
    const panel = await waitFor(() => {
      const found = document.querySelector('[data-slot="reading-activity"]');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(within(panel).getByRole('status').textContent).toBe('Tried again on 2 items: 1 now agree; 1 item waits for you.');
    await user.click(within(panel).getByRole('button', { name: '1 item waits for you' }));
    // Decisions is the "Needs you" queue (loaded on first open).
    await waitFor(() => expect(document.querySelector('[data-slot="needs-you-queue"]')).not.toBeNull(), { timeout: 5000 });
  });

  it('shows nothing while the API sends no activity', async () => {
    pkg.reading = { stage: 'rows', label: "AI readers finding each page's countertop line", pages_done: 2, pages_total: 9, calls: 4, cost_usd: '0.040000', unpriced_calls: 0, retrying: false, updated_at: null };
    render(<Page />);
    // The package has loaded: the stepper shows its reading line.
    await waitFor(() => expect(document.querySelector('[data-slot="review-stepper"] [data-slot="reading-progress"]')?.textContent).toContain('page 3 of 9'));
    expect(document.querySelector('[data-slot="reading-activity"]')).toBeNull();
  });
});
