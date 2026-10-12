// @vitest-environment jsdom
import { useState } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ReviewPage } from '@/pages/ReviewPage';

// Synthetic data only (#1180): nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const revision = (number: number) => ({
  revision_id: `rev-${number}`, revision_number: number, state: number === 2 ? 'NEEDS_INPUT' : 'SUPERSEDED',
  created_at: '2026-10-09T09:00:00Z', current: number === 2, read_only: number !== 2,
  supersedes_revision_number: number === 2 ? 1 : null, superseded_by_revision_number: number === 1 ? 2 : null,
  signed_off_at: null, signed_off_by: null, drawings: [],
});

const result = (label: string) => ({
  finding_id: null, row_id: `row-${label}`, page_number: 1, label, row_location: null, outcome: null,
  needs_decision: false, printed_overall: null, pieces: [], field_cut_per_end: null, field_cut_count: null,
  expected_total: null, delta: null, hold: null, reviewer_decision: null,
  wall_layout: { config: null, label: null, source: 'not established' },
  agreement: { both_readers_agreed_on_row: false, code_clue_used: false, values_agreed: [] },
});

describe('ReviewPage: revisions (#1180)', () => {
  const requested: string[] = [];
  let canStart = true;
  let unfinished: number | null = null;

  beforeEach(() => {
    requested.length = 0;
    canStart = true;
    unfinished = null;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input).replace(/^.*\/api\/v1/, '');
      requested.push(url);
      if (url === '/projects/p/packages/pkg') {
        return json({ id: 'pkg', project_id: 'p', vendor: 'Synthetic vendor', state: unfinished === null ? 'NEEDS_INPUT' : 'UPLOADING', created_at: '2026-10-09T09:00:00Z', current_revision_id: 'rev-2', current_revision_number: 2, product_type: 'countertop' });
      }
      if (url.startsWith('/projects/p/packages/pkg/findings')) return json({ items: [], next_cursor: null });
      if (url === '/projects/p/review-sessions') return json({ items: [] });
      if (url.endsWith('/approval-readiness')) return json({ revision_id: 'rev-2', can_approve: false, blocking_findings: 0, blocking_finding_ids: [], reason: 'synthetic' });
      if (url === '/projects/p/packages/pkg/revisions') {
        return json({ package_id: 'pkg', current_revision_number: 2, can_start_revision: canStart, unfinished_revision: unfinished, items: [revision(1), revision(2)] });
      }
      if (url.startsWith('/projects/p/packages/pkg/revisions/compare')) {
        return json({
          package_id: 'pkg', before_revision_number: 1, after_revision_number: 2, drawings: [],
          pages_added: 0, pages_removed: 0, pages_changed: 1, pages_unchanged: 4, countertops: [], countertops_read: true, countertops_checked: false,
        });
      }
      if (url.endsWith('/countertop-results?revision=1')) return json({ package_id: 'pkg', revision_id: 'rev-1', items: [result('Synthetic revision 1 countertop')] });
      if (url.endsWith('/countertop-results')) return json({ package_id: 'pkg', revision_id: 'rev-2', items: [] });
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

  it('offers "New revision…" and opens the upload pre-filled with the vendor', async () => {
    const user = userEvent.setup();
    render(<Page />);
    await user.click(await within(screen.getByTestId('header-actions')).findByRole('button', { name: 'More actions' }));
    await user.click(await screen.findByRole('menuitem', { name: 'New revision…' }));

    const dialog = await screen.findByRole('dialog', { name: 'New revision 3' });
    expect(within(dialog).getByText('Synthetic vendor')).toBeTruthy();
    expect(within(dialog).getByText('Countertop')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Start revision 3' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows "Revision 2 of 2", what changed, and revision 1 read-only with a banner', async () => {
    const user = userEvent.setup();
    render(<Page />);

    const changed = await screen.findByText(/What changed since revision/);
    expect(changed.textContent).toContain('What changed since revision 1');
    const switcher = await within(screen.getByTestId('header-title')).findByRole('button', { name: /Revision 2 of 2/ });
    await user.click(switcher);
    await user.click(await screen.findByRole('menuitemradio', { name: /Revision 1/ }));

    await waitFor(() => expect(screen.getByText('Synthetic revision 1 countertop')).toBeTruthy());
    const banner = document.querySelector('[data-slot-name="read-only-banner"]');
    expect(banner?.getAttribute('role')).toBe('status');
    expect(banner?.textContent).toContain('Revision 1 is read-only.');
    expect(requested).toContain('/projects/p/packages/pkg/countertop-results?revision=1');
    // Nothing on the read-only view can record a decision.
    expect(screen.queryByRole('button', { name: /Sign off|Decide/ })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Back to revision 2' }));
    await waitFor(() => expect(document.body.textContent).not.toContain('Revision 1 is read-only.'));
  });

  it('offers to finish a revision that was started but never uploaded, even while it is uploading', async () => {
    const user = userEvent.setup();
    unfinished = 2;
    render(<Page />);
    await user.click(await within(screen.getByTestId('header-actions')).findByRole('button', { name: 'More actions' }));
    const item = await screen.findByRole('menuitem', { name: 'Finish uploading revision 2…' });
    expect(item.getAttribute('aria-disabled')).toBeNull();
    await user.click(item);
    expect(await screen.findByRole('dialog', { name: 'Finish revision 2' })).toBeTruthy();
  });

  it('does not offer a new revision to someone who may not upload', async () => {
    const user = userEvent.setup();
    canStart = false;
    render(<Page />);
    await waitFor(() => expect(requested).toContain('/projects/p/packages/pkg/revisions'));
    await user.click(await within(screen.getByTestId('header-actions')).findByRole('button', { name: 'More actions' }));
    expect(await screen.findByRole('menuitem', { name: 'Project values' })).toBeTruthy();
    expect(screen.queryByRole('menuitem', { name: /revision/ })).toBeNull();
  });
});
