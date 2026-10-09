// @vitest-environment jsdom
/**
 * The assistant on the review page (#1129): the header button opens and closes it, focus comes back
 * to the button, and the panel stays mounted while hidden so its conversation survives. Synthetic
 * data only, against a fake API that answers GETs.
 */
import { useState } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { TooltipProvider } from '@/components/ui/tooltip';

vi.mock('@/api/config', () => ({ projectId: () => 'synthetic-project' }));
const { ReviewPage } = await import('@/pages/ReviewPage');

const PKG = 'synthetic-package';
const v = (n: string, d: string, display: string) => ({ numerator: n, denominator: d, display });
const ROW = {
  finding_id: 'f-r4', row_id: 'r4', page_number: 4, label: 'Synthetic countertop', row_location: null,
  outcome: 'FAIL', needs_decision: true, printed_overall: v('84', '1', '84"'), pieces: [], field_cut_per_end: null,
  field_cut_count: 0, expected_total: v('86', '1', '86"'), delta: v('-2', '1', '-2"'), hold: null, reviewer_decision: null,
  wall_layout: { config: null, label: null, source: 'not established' },
  agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
};

function answer(url: string): unknown {
  const path = new URL(url, 'http://test').pathname;
  if (path.endsWith(`/packages/${PKG}`)) return { id: PKG, project_id: 'synthetic-project', vendor: 'Synthetic set', state: 'AWAITING_REVIEW', current_revision_id: 'rev', current_revision_number: 1 };
  if (path.endsWith('/findings')) return { items: [{ id: 'f-r4', rule_id: 'SYNTH', outcome: 'FAIL', severity: 'MAJOR', package_revision_id: 'rev', reviewer_action: null, scope_label: 'Synthetic countertop', scope_row_candidate_id: 'r4' }], next_cursor: null };
  if (path.endsWith('/review-sessions')) return { items: [], next_cursor: null };
  if (path.endsWith('/approval-readiness')) return { blocking_finding_ids: ['f-r4'], blocking_findings: 1, can_approve: false, reason: 'One result needs you', revision_id: 'rev' };
  if (path.endsWith('/countertop-results')) return { items: [ROW], package_id: PKG, revision_id: 'rev', pages_without_countertop: [], rows_not_checked: [] };
  if (path.endsWith('/assistant')) return { enabled: true, model_label: 'Synthetic Model', keeps_no_data: true, starters: ['Why did page 4 fail?'] };
  return null;
}

const calls: { url: string; method: string }[] = [];
beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push({ url, method });
    const body = method === 'GET' ? answer(url) : null;
    return body === null
      ? { ok: false, status: 404, headers: new Headers(), json: async () => ({ error: 'not_found', message: 'Not in this synthetic API', request_id: 'r' }) }
      : { ok: true, status: 200, headers: new Headers(), json: async () => body };
  }));
});
afterEach(() => vi.unstubAllGlobals());

function Harness() {
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <TooltipProvider>
      <ShellSlotsContext.Provider value={{ title: null, actions }}>
        <div ref={setActions} />
        <ReviewPage sessionId={PKG} onEvidenceChange={() => {}} onBackToDocuments={() => {}} />
      </ShellSlotsContext.Provider>
    </TooltipProvider>
  );
}

describe('the assistant on the review page', { timeout: 15_000 }, () => {
  it('opens from the header button, closes on Escape or Close, and gives focus back to the button', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const button = await screen.findByRole('button', { name: 'Assistant' }, { timeout: 5000 });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(document.getElementById('review-assistant')).toBeNull(); // not mounted until first opened

    await user.click(button);
    const panel = screen.getByRole('complementary', { name: 'Assistant' });
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(button.getAttribute('aria-controls')).toBe('review-assistant');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Your question' })));
    await screen.findByText('Why did page 4 fail?');

    await user.keyboard('{Escape}');
    expect(panel.hidden).toBe(true);
    expect(document.getElementById('review-assistant')).toBe(panel); // still mounted, only hidden
    expect(button.getAttribute('aria-expanded')).toBe('false');
    await waitFor(() => expect(document.activeElement).toBe(button));

    await user.click(button);
    expect(panel.hidden).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Close assistant' }));
    expect(panel.hidden).toBe(true);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Assistant' })));
    // Opening and closing asked the server for nothing but reads.
    expect(calls.every((call) => call.method === 'GET')).toBe(true);
  });
});
