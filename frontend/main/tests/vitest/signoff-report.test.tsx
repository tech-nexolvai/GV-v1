// @vitest-environment jsdom
import { useState } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApprovalReadiness, CountertopResult } from '@/api/client';
import { SignOffDialog, SignOffPanel, type SignOffScope } from '@/components/review/signoff-panel';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { TooltipProvider } from '@/components/ui/tooltip';
import { signOffSummary } from '@/lib/countertop-results';
import { ReviewPage } from '@/pages/ReviewPage';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));
// The queue itself is tested in architect-match.test.tsx; here it only says "a pairing was saved".
vi.mock('@/components/queue/needs-you-queue', () => ({
  NeedsYouQueue: ({ open, onPairingSaved }: { open: boolean; onPairingSaved?: () => void }) =>
    open ? <button type="button" onClick={() => onPairingSaved?.()}>Synthetic: save a pairing</button> : null,
}));

const exact = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
const row = (id: string, extra: Partial<CountertopResult> = {}): CountertopResult => ({
  finding_id: `f-${id}`, row_id: id, page_number: 1, label: `Synthetic countertop ${id}`, row_location: null,
  outcome: 'PASS', needs_decision: false, printed_overall: exact('84', '84"'), pieces: [],
  field_cut_per_end: exact('1', '1"'), field_cut_count: 2, expected_total: exact('84', '84"'),
  delta: exact('0', '0"'), hold: null, reviewer_decision: null,
  wall_layout: { config: 'back_left_right', label: 'back wall and both ends', source: 'drawing clues' },
  agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true] },
  ...extra,
});
const decision = (action: string) => ({ action, note: 'synthetic', actor: 'Synthetic Reviewer', time: '2026-10-09T10:00:00Z' });

describe('signOffSummary', () => {
  it('splits countertops by who settled them, adding up to the rows', () => {
    const rows = [
      row('a'),
      row('b', { outcome: 'FAIL', reviewer_decision: decision('confirm') }),
      row('c', { outcome: 'NOT_FOUND', reviewer_decision: decision('dismiss') }),
      row('d', { outcome: 'FAIL', reviewer_decision: decision('dismiss') }), // a dismissed FAIL is the reviewer's call, not "not checkable"
      row('e', { outcome: 'REVIEW_REQUIRED', needs_decision: true }),
      row('f', { outcome: 'PASS', needs_decision: true, reviewer_decision: decision('correct') }), // waiting for a re-run
      row('g', { finding_id: null, outcome: null, needs_decision: true }), // no recorded result: not a finding
    ];
    expect(signOffSummary(rows)).toEqual({ byChecks: 1, byYou: 2, notCheckable: 1, needYou: 2, noResult: 1 });
  });
});

const SCOPE: SignOffScope = { countertops: { byChecks: 7, byYou: 2, notCheckable: 1, needYou: 0, noResult: 0 }, countertopsFailed: false, otherChecks: 3, total: 13 };
const READY: ApprovalReadiness = { revision_id: 'rev', can_approve: true, blocking_findings: 0, blocking_finding_ids: [], reason: null };

describe('SignOffPanel', () => {
  it('says Ready and what the sign-off covers', () => {
    render(<SignOffPanel readiness={READY} ready scope={SCOPE} busy={false} onSignOff={() => {}} onReview={() => {}} />);
    expect(screen.getByText('Ready')).toBeTruthy();
    expect(screen.getByText((_, el) => el?.getAttribute('data-part') === 'countertops').textContent).toBe('7 needed no decision · 2 decided by you · 1 not checkable');
    expect(screen.getByText((_, el) => el?.getAttribute('data-part') === 'other-checks').textContent).toBe('3 recorded results');
    expect(screen.getByText(/approves all/).textContent).toContain('approves all 13 recorded results of this revision. It cannot be undone.');
    expect(screen.getByRole('button', { name: 'Sign off…' }).hasAttribute('disabled')).toBe(false);
  });

  it('says when the countertop split is unavailable, and words a single result in the singular', () => {
    render(<SignOffPanel readiness={READY} ready scope={{ countertops: null, countertopsFailed: true, otherChecks: null, total: 1 }} busy={false} onSignOff={() => {}} onReview={() => {}} />);
    expect(screen.getByText('Not available: the countertop results did not load.')).toBeTruthy();
    expect(screen.getByText(/approves the/).textContent).toBe('Signing off approves the 1 recorded result of this revision. It cannot be undone.');
  });

  it('shows the server\'s blockers and reason, and offers the queue instead of signing', async () => {
    const user = userEvent.setup();
    const onReview = vi.fn();
    render(
      <SignOffPanel
        readiness={{ ...READY, can_approve: false, blocking_findings: 2, reason: 'Synthetic reason from the server.' }}
        ready={false}
        scope={SCOPE}
        busy={false}
        onSignOff={() => {}}
        onReview={onReview}
      />,
    );
    expect(screen.getByText('Not ready')).toBeTruthy();
    expect(screen.getByText((_, el) => el?.tagName === 'SPAN' && el.textContent === '2 results still need a decision.' && el.classList.contains('font-medium'))).toBeTruthy();
    expect(screen.getByText('Synthetic reason from the server.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sign off…' }).hasAttribute('disabled')).toBe(true);
    await user.click(screen.getByRole('button', { name: 'Review them' }));
    expect(onReview).toHaveBeenCalledTimes(1);
  });
});

describe('SignOffDialog', () => {
  function Harness({ signer, onConfirm, ready = true, error = null }: { signer: string | null; onConfirm: () => void; ready?: boolean; error?: string | null }) {
    const [open, setOpen] = useState(true);
    return <SignOffDialog open={open} onOpenChange={setOpen} signer={signer} vendor="Synthetic vendor" revision={2} scope={SCOPE} ready={ready} busy={false} error={error} onConfirm={onConfirm} />;
  }

  it('names the signer and the set, and says it cannot be undone', () => {
    render(<Harness signer="Synthetic Reviewer" onConfirm={() => {}} />);
    const dialog = screen.getByRole('dialog', { name: 'Sign off this review?' });
    expect(within(dialog).getByText('Synthetic Reviewer')).toBeTruthy();
    expect(dialog.textContent).toContain('Synthetic vendor, revision 2: all 13 recorded results are approved.');
    expect(dialog.textContent).toContain('This cannot be undone.');
  });

  it('says the sign-in is recorded when no sitting names the signer', () => {
    render(<Harness signer={null} onConfirm={() => {}} />);
    expect(screen.getByText('The sign-off is recorded under your sign-in.')).toBeTruthy();
  });

  it('Keep reviewing closes it without signing; Sign off confirms once', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const { unmount } = render(<Harness signer="Synthetic Reviewer" onConfirm={onConfirm} />);
    await user.click(screen.getByRole('button', { name: 'Keep reviewing' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(onConfirm).not.toHaveBeenCalled();
    unmount();
    render(<Harness signer="Synthetic Reviewer" onConfirm={onConfirm} />);
    await user.click(screen.getByRole('button', { name: 'Sign off' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('cannot confirm once the server no longer says ready, and shows why nothing was signed', () => {
    render(<Harness signer="Synthetic Reviewer" onConfirm={() => {}} ready={false} error="409 synthetic refusal" />);
    expect(screen.getByRole('button', { name: 'Sign off' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('alert').textContent).toBe('Nothing was signed: 409 synthetic refusal');
  });
});

describe('ReviewPage: sign-off, then the signed report', () => {
  let state: string;
  let exportsStatus: string;
  let approveStatus: number;
  const posts: string[] = [];
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const listed = (id: string, outcome: string, action: string | null) => ({
    id, rule_id: `CHECK-${id}`, outcome, severity: 'FLAG', package_revision_id: 'rev', scope_item_id: null, scope_row_candidate_id: null,
    row_location: null, reason: null, reviewer_reason: null, scope_label: null, notes: [], created_at: '2026-10-09T09:00:00Z',
    reviewer_action: action ? { action, actor: 'Synthetic Reviewer', note: 'synthetic', at: '2026-10-09T10:00:00Z' } : null,
  });

  beforeEach(() => {
    state = 'AWAITING_REVIEW';
    exportsStatus = 'preparing';
    approveStatus = 201;
    posts.length = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input).replace(/^.*\/api\/v1/, '');
      const method = init?.method ?? 'GET';
      if (method === 'POST') posts.push(url);
      if (url === '/projects/p/packages/pkg') {
        return json({ id: 'pkg', project_id: 'p', vendor: 'Synthetic vendor', state, created_at: '2026-10-09T09:00:00Z', current_revision_id: 'rev', current_revision_number: 2, product_type: 'countertop' });
      }
      if (url.startsWith('/projects/p/packages/pkg/findings?') || url === '/projects/p/packages/pkg/findings') {
        return json({ items: [listed('f-a', 'PASS', null), listed('o-depth', 'NOT_FOUND', 'dismiss')], next_cursor: null });
      }
      if (url === '/projects/p/review-sessions') {
        return json({ items: [{ id: 's', package_revision_id: 'rev', reviewer: 'Synthetic Reviewer', created_at: '2026-10-09T09:30:00Z', completed_at: null }] });
      }
      if (url.endsWith('/approval-readiness')) return json(READY);
      if (url.endsWith('/countertop-results')) return json({ package_id: 'pkg', revision_id: 'rev', items: [row('a')] });
      if (url === '/projects/p/review-sessions/s/approve' && method === 'POST') {
        if (approveStatus !== 201) return json({ error: 'http_error', message: 'synthetic refusal', request_id: 'r' }, approveStatus);
        state = 'APPROVED';
        return json({ approval_id: 'ap', package_revision_id: 'rev', approved_by: 'Synthetic Reviewer', findings_approved: 2, state: 'APPROVED' }, 201);
      }
      if (url.endsWith('/signed-exports')) return json({ approval_id: 'ap', status: exportsStatus });
      if (url === '/rules') return json([]);
      return json({ error: 'http_error', message: `not in this test: ${url}`, request_id: 'r' }, 404);
    }));
  });

  function Page() {
    const [actions, setActions] = useState<HTMLElement | null>(null);
    return (
      <TooltipProvider>
        <ShellSlotsContext.Provider value={{ title: null, actions }}>
          <div ref={setActions} data-testid="header-actions" />
          <ReviewPage sessionId="pkg" onEvidenceChange={() => {}} onBackToDocuments={() => {}} />
        </ShellSlotsContext.Provider>
      </TooltipProvider>
    );
  }

  it('never signs on one click: the header and the panel both open the confirmation', async () => {
    const user = userEvent.setup();
    render(<Page />);
    const panel = await screen.findByRole('region', { name: 'Sign off' });
    await waitFor(() => expect(within(panel).getByText('Ready')).toBeTruthy());
    expect(panel.querySelector('[data-part="countertops"]')?.textContent).toBe('1 needed no decision · 0 decided by you · 0 not checkable');
    expect(panel.querySelector('[data-part="other-checks"]')?.textContent).toBe('1 recorded result');

    // The header's Sign off opens the dialog; Keep reviewing signs nothing.
    await user.click(within(screen.getByTestId('header-actions')).getByRole('button', { name: 'Sign off' }));
    const dialog = await screen.findByRole('dialog', { name: 'Sign off this review?' });
    expect(within(dialog).getByText('Synthetic Reviewer')).toBeTruthy();
    expect(dialog.textContent).toContain('all 2 recorded results are approved');
    await user.click(within(dialog).getByRole('button', { name: 'Keep reviewing' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(posts.filter((url) => url.endsWith('/approve'))).toEqual([]);

    // The panel's button, then confirm (clicked twice, fast): exactly one approval.
    await user.click(within(panel).getByRole('button', { name: 'Sign off…' }));
    const confirm = within(await screen.findByRole('dialog')).getByRole('button', { name: 'Sign off' });
    await user.dblClick(confirm);
    await waitFor(() => expect(posts.filter((url) => url.endsWith('/approve'))).toEqual(['/projects/p/review-sessions/s/approve']));

    // Signed: the Report panel replaces the Sign off panel and follows the export status.
    const report = await screen.findByRole('region', { name: 'Signed report' });
    expect(screen.queryByRole('region', { name: 'Sign off' })).toBeNull();
    await waitFor(() => expect(within(report).getByText(/Preparing the signed files/)).toBeTruthy());
    expect(within(report).queryByRole('button', { name: /Download/ })).toBeNull();
    exportsStatus = 'ready';
    await user.click(within(report).getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(within(report).getByRole('button', { name: 'Download PDF' })).toBeTruthy());
    expect(within(report).getByRole('button', { name: 'Download workbook' })).toBeTruthy();
    expect(within(report).getByRole('button', { name: 'Download redline' })).toBeTruthy();
    // Still exactly one approval, and nothing offers Sign off any more.
    expect(posts.filter((url) => url.endsWith('/approve'))).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /^Sign off/ })).toBeNull();
  });

  // #1100: sign-off waits for a check run after a reviewer pairs architect dimensions. Two guards
  // for the screen's side of it, true before #1101 and kept true.
  const PAIRING_NEEDS_RERUN = "A reviewer paired the architect's dimensions after the last check run. Run the checks so the pairing is compared before signing off.";
  function serveReadiness(readiness: () => ApprovalReadiness) {
    const served = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (input, init) =>
      String(input).endsWith('/approval-readiness') ? json(readiness()) : served(input, init));
  }

  it('after a pairing is saved, the page asks for a check run and offers no sign-off', async () => {
    const user = userEvent.setup();
    serveReadiness(() => ({ revision_id: 'rev', can_approve: false, blocking_findings: 1, blocking_finding_ids: ['f-a'], reason: '1 findings still need a valid reviewer decision or a check rerun after correction. Add a note when required.' }));
    render(<Page />);
    const header = screen.getByTestId('header-actions');
    await user.click(await within(header).findByRole('button', { name: 'Review 1 item' }));
    expect(within(header).queryByRole('button', { name: 'Run checks' })).toBeNull();
    await user.click(await screen.findByRole('button', { name: 'Synthetic: save a pairing' }));

    // The saved pairing turns the next step into a check run: the header stops offering the review
    // and never offers sign-off; the stepper says why.
    expect(await within(header).findByRole('button', { name: 'Run checks' })).toBeTruthy();
    expect(within(header).queryByRole('button', { name: 'Review 1 item' })).toBeNull();
    expect(within(header).queryByRole('button', { name: 'Sign off' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Sign off' })).toBeNull();
    expect(screen.getAllByText('Values changed since the last run').length).toBeGreaterThan(0);
    expect(posts.filter((url) => url.endsWith('/approve'))).toEqual([]);
  });

  it('the Results tab says what still needs you with the outcome glyph and in words, not a coloured number alone (#1155)', async () => {
    serveReadiness(() => ({ revision_id: 'rev', can_approve: false, blocking_findings: 3, blocking_finding_ids: ['f-a'], reason: 'Synthetic: still blocked.' }));
    render(<Page />);
    const tab = await screen.findByRole('tab', { name: 'Results 3 need you' });
    const badge = tab.querySelector('[data-slot="results-tab-badge"]')!;
    expect(badge.querySelector('[data-outcome-icon="REVIEW_REQUIRED"]')).not.toBeNull();
    expect(badge.querySelector('.num')?.textContent).toBe('3');
  });

  it("while the server holds sign-off for a pairing, the panel shows its reason and Sign off stays off", async () => {
    serveReadiness(() => ({ revision_id: 'rev', can_approve: false, blocking_findings: 0, blocking_finding_ids: [], reason: PAIRING_NEEDS_RERUN }));
    render(<Page />);
    const panel = await screen.findByRole('region', { name: 'Sign off' });
    await waitFor(() => expect(within(panel).getByText('Not ready')).toBeTruthy());
    expect(within(panel).getByText(PAIRING_NEEDS_RERUN)).toBeTruthy();
    expect(within(panel).queryByText(/still need/)).toBeNull();
    expect(within(panel).getByRole('button', { name: 'Sign off…' }).hasAttribute('disabled')).toBe(true);
    expect(within(screen.getByTestId('header-actions')).getByRole('button', { name: 'Sign off' }).hasAttribute('disabled')).toBe(true);
  });

  it('a refused sign-off keeps the dialog open and says nothing was signed', async () => {
    const user = userEvent.setup();
    approveStatus = 409;
    render(<Page />);
    const panel = await screen.findByRole('region', { name: 'Sign off' });
    await waitFor(() => expect(within(panel).getByText('Ready')).toBeTruthy());
    await user.click(within(panel).getByRole('button', { name: 'Sign off…' }));
    const dialog = await screen.findByRole('dialog', { name: 'Sign off this review?' });
    await user.click(within(dialog).getByRole('button', { name: 'Sign off' }));
    await waitFor(() => expect(within(dialog).getByRole('alert').textContent).toMatch(/^Nothing was signed: .*synthetic refusal/));
    expect(screen.getByRole('dialog')).toBe(dialog);
    expect(screen.queryByRole('region', { name: 'Signed report' })).toBeNull();
  });
});
