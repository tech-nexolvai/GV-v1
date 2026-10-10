// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { toFinding } from '@/api/findings';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';
import { NeedsYouQueue } from '@/components/queue/needs-you-queue';
import { recordReviewDecision } from '@/pages/recordReviewDecision';

// The dashboard asks the rulebook for names; keep it off the network.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
}));

// Synthetic data only (#1106): nothing here comes from a client drawing.
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
const carried = { action: 'dismiss', note: 'TEST not checkable here', actor: 'Synthetic Reviewer', time: '2026-10-09T10:00:00Z', carried_over: true, carried_from_finding_id: 'old-held' };
const fresh = { ...carried, carried_over: false, carried_from_finding_id: null };

function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`, row_location: null,
    outcome: 'REVIEW_REQUIRED', needs_decision: false, printed_overall: x('88', '88"'), pieces: [],
    field_cut_per_end: null, field_cut_count: null, expected_total: null, delta: null,
    hold: { code: 'counter-break', reason: 'Synthetic: the countertop breaks.' }, reviewer_decision: carried,
    wall_layout: { config: null, label: null, source: 'not established' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
    ...overrides,
  } as CountertopResult;
}
const finding = (id: string, extra: Partial<Finding> = {}): Finding =>
  ({ id, check_id: 'CHECK-OTHER', name: 'Synthetic other check', severity: 'FLAG', outcome: 'NOT_FOUND', reviewer_action: null, created_at: '2026-10-09T10:00:00Z', ...extra }) as Finding;

describe('carried-over decisions (#1073)', () => {
  it('the findings list keeps the carried flag', () => {
    const listed = {
      id: 'f', rule_id: 'CHECK-1', outcome: 'NOT_FOUND', severity: 'FLAG', package_revision_id: 'rev', scope_item_id: null, scope_row_candidate_id: null,
      row_location: null, reason: null, reviewer_reason: null, scope_label: null, notes: [], created_at: '2026-10-09T09:00:00Z',
      reviewer_action: { action: 'dismiss', actor: 'Synthetic Reviewer', note: 'TEST', at: '2026-10-09T10:00:00Z', carried_over: true, carried_from_finding_id: 'old' },
    };
    expect(toFinding(listed as never).reviewer_carried_over).toBe(true);
    expect(toFinding({ ...listed, reviewer_action: { ...listed.reviewer_action, carried_over: false } } as never).reviewer_carried_over).toBe(false);
    expect(toFinding({ ...listed, reviewer_action: null } as never).reviewer_carried_over).toBe(false);
  });

  function dashboard(rows: CountertopResult[], findings: Finding[]) {
    render(
      <ResultsDashboard
        countertops={{ status: 'ready', rows } as CountertopsState}
        findings={findings}
        blockingIds={new Set()}
        busy={false}
        filter="all"
        onFilterChange={() => {}}
        onRetry={() => {}}
        onRefresh={() => {}}
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        onBulkDismiss={vi.fn(async () => ({ saved: 0, failed: [] }))}
        onShowDrawing={() => {}}
        onOpenCard={() => {}}
      />,
    );
  }

  it('Results say "carried over" beside a carried decision, in words, and not beside a fresh one', async () => {
    dashboard([row('c', 4), row('n', 7, { reviewer_decision: fresh })], [finding('f-c'), finding('f-n')]);
    const table = document.querySelector('[data-slot="countertop-table"] table')!;
    const carriedRow = table.querySelector('tr[data-row-id="c"]')!;
    expect(carriedRow.querySelector('[data-slot="carried-over"]')?.textContent).toBe('carried over');
    expect(carriedRow.textContent).toContain('Not checkable');
    expect(table.querySelector('tr[data-row-id="n"] [data-slot="carried-over"]')).toBeNull();
    // The phone list says it too.
    const card = within(screen.getByRole('list', { name: 'Countertops' })).getAllByRole('listitem').find((li) => li.textContent?.includes('Countertop · page 4'))!;
    expect(card.querySelector('[data-slot="carried-over"]')?.textContent).toBe('carried over');
  });

  it('an other check decided before the re-run says "carried over"', async () => {
    const user = userEvent.setup();
    dashboard([row('c', 4)], [finding('f-c'), finding('other', { reviewer_action: 'dismiss', reviewed_by: 'Synthetic Reviewer', reviewer_carried_over: true })]);
    const region = screen.getByRole('region', { name: 'Other checks' });
    await user.click(within(region).getByRole('button', { name: /Other checks/ }));
    expect(region.querySelector('[data-slot="carried-over"]')?.textContent).toBe('carried over');
  });
});

describe('a carried decision changed by the reviewer', () => {
  it('is the reviewer\'s own from then on: the label goes at once, not at the next refresh', async () => {
    let findings: Finding[] = [finding('other', { reviewer_action: 'dismiss', reviewed_by: 'Synthetic Reviewer', reviewer_carried_over: true })];
    const saved = await recordReviewDecision('other', 'confirm', async () => undefined, (apply) => { findings = apply(findings); });
    expect(saved).toEqual({ saved: true });
    expect(findings[0]).toMatchObject({ reviewer_action: 'confirm', reviewer_carried_over: false });
  });
});

describe('the queue: carried-over decisions', { timeout: 15_000 }, () => {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/slot-rows')) return json({ rows: [] });
      if (url.endsWith('/rules')) return json([]);
      if (url.endsWith('/actions')) {
        return json({ items: [
          { id: 'a2', finding_id: 'f-c', action: 'dismiss', actor: 'Synthetic Reviewer', note: 'TEST not checkable here', created_at: '2026-10-09T10:00:00Z', carried_over: true, carried_from_finding_id: 'old-held' },
        ] });
      }
      if (url.includes('/chain')) return json({ operands: [], trace: { kind: 'abstention', cause: 'held', reason: 'Synthetic.' } });
      return json({ error: 'http_error', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it('the decided summary and the history say the decision was carried over', async () => {
    const user = userEvent.setup();
    // Listed when the queue opened, decided since (the server no longer counts it as blocking).
    const rows = [row('c', 4, { needs_decision: true }), row('w', 2, { reviewer_decision: null, needs_decision: true, hold: null, outcome: 'FAIL' })];
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={rows} rowsReady findings={[finding('f-c'), finding('f-w', { outcome: 'FAIL' })]} blocking={new Set(['f-w'])}
        projectId="p" packageId="k"
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'sign-off', label: 'Sign off', disabled: true, reason: 'Synthetic.' }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onOpenCard={vi.fn()}
        startAt="row:c"
      />,
    );
    const decided = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('[data-slot="queue-decided"]');
      if (!found) throw new Error('no decided summary yet');
      return found;
    });
    expect(decided.querySelector('[data-slot="carried-over"]')?.textContent).toBe('carried over');
    await user.click(within(decided).getByRole('button', { name: /History/ }));
    const history = await screen.findByRole('list', { name: 'Decisions, newest first' });
    expect(history.querySelector('[data-slot="carried-over"]')?.textContent).toBe('carried over');
  });

  it('says that a check run asks again only what it changes', () => {
    // One answer waits for a run (a correction); another item is still open.
    const rows = [
      row('corr', 3, { needs_decision: true, reviewer_decision: { ...fresh, action: 'correct' } }),
      row('w', 2, { reviewer_decision: null, needs_decision: true, hold: null, outcome: 'FAIL' }),
    ];
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={rows} rowsReady findings={[finding('f-corr'), finding('f-w', { outcome: 'FAIL' })]} blocking={new Set(['f-corr', 'f-w'])}
        projectId="p" packageId="k"
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'run-checks', label: 'Run checks', disabled: false, reason: null }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onOpenCard={vi.fn()}
        startAt="row:w"
      />,
    );
    const note = document.querySelector('[data-slot="queue-run-first"]')!;
    expect(note.textContent).toContain('a decision made now carries over only if its result comes back unchanged');
    expect(note.textContent).not.toContain('decisions made now will be asked again');
  });
});
