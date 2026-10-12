// @vitest-environment jsdom
import { useState } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { ResultsDashboard } from '@/components/results/results-dashboard';
import { NeedsYouQueue } from '@/components/queue/needs-you-queue';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { TooltipProvider } from '@/components/ui/tooltip';
import { bucketCounts, bucketOf, defaultFilter, kpis, matchesFilter, rowNeedsYou } from '@/lib/countertop-results';
import { buildQueue } from '@/lib/needs-you-queue';
import { rowQueueKey } from '@/lib/assistant';
import { MeasurementPanel } from '@/pages/MeasurementPanel';
import { ReviewPage } from '@/pages/ReviewPage';

// #1179. Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const exact = (numerator: string, display: string) => ({ numerator, denominator: '1', display });

/** A countertop row as the API sends it before any check run: no finding, no outcome, `needs_decision`. */
function unchecked(id: string, page: number, extra: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: null, row_id: id, page_number: page, label: `Countertop row on page ${page}`, row_location: null,
    outcome: null, needs_decision: true, printed_overall: exact('84', '84"'), pieces: [],
    field_cut_per_end: null, field_cut_count: null, expected_total: null, delta: null, hold: null, reviewer_decision: null,
    wall_layout: { config: null, label: null, source: 'not established' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
    ...extra,
  } as CountertopResult;
}
const checked = (id: string, page: number, extra: Partial<CountertopResult> = {}) =>
  unchecked(id, page, { finding_id: `f-${id}`, outcome: 'REVIEW_REQUIRED', ...extra });
const finding = (id: string, outcome: Finding['outcome'], extra: Partial<Finding> = {}): Finding =>
  ({ id, check_id: `CHECK-${id}`, name: `Synthetic ${id}`, severity: 'FLAG', outcome, reviewer_action: null, created_at: '2026-10-11T10:00:00Z', ...extra }) as Finding;

const handlers = () => ({
  onAction: vi.fn(async () => ({ saved: true as const })),
  onCorrect: vi.fn(async () => ({ saved: true as const })),
  onExcept: vi.fn(async () => ({ saved: true as const })),
});

function dashboard(rows: CountertopResult[], findings: Finding[] = [], blocking = new Set<string>(), canRunChecks = true) {
  const onOpenQueue = vi.fn();
  const view = render(
    <ResultsDashboard
      countertops={{ status: 'ready', rows }} findings={findings} blockingIds={blocking} busy={false} filter={null}
      onFilterChange={vi.fn()} onRetry={vi.fn()} onRefresh={vi.fn()} handlers={handlers()} onBulkDismiss={vi.fn(async () => ({ saved: 0, failed: [] }))}
      onShowDrawing={vi.fn()} onOpenCard={vi.fn()} onOpenQueue={onOpenQueue} canRunChecks={canRunChecks}
    />,
  );
  return { onOpenQueue, ...view };
}

// Every non-GET request a test makes, by path; `writes()` leaves out the free page-picture request
// the Measurements page drawing makes when it mounts.
const posts: string[] = [];
const writes = () => posts.filter((url) => !url.endsWith('/pages/pictures'));
let required: Record<string, unknown>;
let slotRows: unknown[];
let countertopRows: CountertopResult[];
let pkgState: string;
let requiredFails: boolean;
/** When set, the checks request waits for this before answering (a run "starting"). */
let checksGate: Promise<void> | null;

const REQUIRED = {
  quantities: [
    { key: 'SHOP:countertop_depth', semantic_type: 'countertop_depth', source: 'SHOP', many: false, consumers: [{ rule_id: 'CT-DEPTH-001', input_name: 'countertop_depth' }], categories: [] },
    { key: 'SHOP:sink_width', semantic_type: 'sink_width', source: 'SHOP', many: false, consumers: [{ rule_id: 'CT-SINK-001', input_name: 'sink_width' }], categories: [] },
  ],
  page_numbers: [1],
  confirmed_readings: [],
  proposed_readings: [],
  parameters: [],
  discriminators: [],
  rules_published: 2,
  still_reading: false,
  revision_state: 'READY',
};
const SLOT = {
  row_id: 'row-a', page_number: 1, label: 'Synthetic countertop', piece_count: 1, held_reason: null,
  wall_confirmation_allowed: false, values: [], wall_proposal: null, wall_source: null, wall_reason: null,
  wall_layout_choices: [], decision_id: null, confirmed_by: null, decided_at: null, wall_config: 'back_only',
};

beforeEach(() => {
  posts.length = 0;
  required = REQUIRED;
  pkgState = 'NEEDS_INPUT';
  requiredFails = false;
  checksGate = null;
  slotRows = [SLOT];
  countertopRows = [unchecked('row-a', 1), unchecked('row-b', 2)];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^.*\/api\/v1/, '');
    const method = init?.method ?? 'GET';
    if (method !== 'GET') posts.push(url);
    if (url === '/projects/p/packages/pkg') {
      return json({ id: 'pkg', project_id: 'p', vendor: 'Synthetic vendor', state: pkgState, created_at: '2026-10-11T09:00:00Z', current_revision_id: 'rev', current_revision_number: 1, product_type: 'countertop' });
    }
    if (url.startsWith('/projects/p/packages/pkg/findings')) return json({ items: [], next_cursor: null });
    if (url === '/projects/p/review-sessions') return json({ items: [] });
    if (url.endsWith('/approval-readiness')) return json({ revision_id: 'rev', can_approve: false, blocking_findings: 0, blocking_finding_ids: [], reason: 'Synthetic: no results yet.' });
    if (url.endsWith('/countertop-results')) return json({ package_id: 'pkg', revision_id: 'rev', items: countertopRows });
    if (url.endsWith('/changed-values')) return json({ items: [] });
    if (url === '/rules') return json([]);
    if (url.includes('/required-inputs')) return requiredFails ? json({ error: 'http_error', message: 'Synthetic outage', request_id: 'r' }, 500) : json(required);
    if (url.endsWith('/signed-exports')) return json({ approval_id: 'ap', status: 'ready' });
    if (url.includes('/candidates') && method === 'GET') return json({ candidates: [], page_number: 1, total: 0 });
    if (url.endsWith('/confirm')) return json({}, 201);
    if (url.endsWith('/semantic-types')) return json(['countertop_depth', 'sink_width']);
    if (url.endsWith('/views')) return json({ views: [] });
    if (url.endsWith('/parts')) return json({ drawings: [] });
    if (url.endsWith('/countertop-runs')) return json({ can_suggest: false, why_not: null, drawings: [] });
    if (url.endsWith('/reading-parts')) return json({ can_suggest: true, why_not: null, drawings: [] });
    if (url.endsWith('/slot-rows')) return json({ rows: slotRows });
    if (url.endsWith('/measurements') && method === 'POST') return json({ parameters: [], measurements: [], lists: [] }, 201);
    if (url.endsWith('/checks') && method === 'POST') {
      if (checksGate) await checksGate;
      return json({ accepted_id: 'acc-00000001', package_revision_id: 'rev' }, 202);
    }
    if (url.includes('/picture')) return json({ error: 'http_error', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
    if (url.endsWith('/pages/pictures')) return json({ queued: false }, 202);
    return json({ error: 'http_error', message: `not in this test: ${url}`, request_id: 'r' }, 404);
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe('"Not checked" is not "Needs you" (#1179)', () => {
  it('a row with no recorded result is not checked: never needs you, never a queue item', () => {
    const rows = [unchecked('a', 1), unchecked('b', 2, { hold: { code: 'row-held', reason: 'Synthetic hold.' } }), checked('c', 3)];
    expect(rows.map(bucketOf)).toEqual(['not-checked', 'not-checked', 'needs-you']);
    expect(rows.map(rowNeedsYou)).toEqual([false, false, true]);
    expect(kpis(rows)).toMatchObject({ countertops: 3, needsYou: 1, notChecked: 2 });
    expect(bucketCounts(rows)).toEqual({ 'needs-you': 1, 'not-checked': 2, fail: 0, pass: 0, 'not-checkable': 0 });
    expect(matchesFilter(rows[0], 'needs-you')).toBe(false);
    expect(matchesFilter(rows[0], 'not-checked')).toBe(true);
    expect(buildQueue(rows, [finding('f-c', 'REVIEW_REQUIRED')], new Set(['f-c'])).map((item) => item.key)).toEqual(['row:c']);
    expect(rowQueueKey(rows[0])).toBeNull();
    // Before any run nothing can be decided, so the table opens on all of them.
    expect(defaultFilter(rows.slice(0, 2))).toBe('all');
  });

  it('before any check run: no "Needs you" count, a "Not checked" state with its words, and nothing opens the queue', async () => {
    const user = userEvent.setup();
    const { onOpenQueue } = dashboard([unchecked('a', 1), unchecked('b', 2)]);
    const cards = document.querySelector<HTMLElement>('[data-slot="kpi-cards"]')!;
    expect(cards.textContent).toMatch(/Countertops need you\s*0/);
    await user.click(within(cards).getByRole('button', { name: /Countertops need you/ }));
    expect(onOpenQueue).not.toHaveBeenCalled();
    expect(document.querySelector('[data-slot="not-checked-notice"]')?.textContent).toBe('2 countertops not checked yet. Run the checks to get a result.');
    expect(screen.getByRole('radio', { name: /Not checked\s*2/ })).toBeTruthy();
    const table = document.querySelector<HTMLElement>('[data-slot="countertop-table"] table')!;
    const firstRow = table.querySelector<HTMLElement>('tr[data-row-id="a"]')!;
    expect(firstRow.querySelector('[data-slot="not-checked"]')?.textContent).toBe(' Not checked');
    expect(firstRow.textContent).toContain('Run the checks to get a result');
    expect(table.textContent).not.toContain('Needs you');
    const legend = await screen.findByRole('list', { name: 'Countertop outcomes' }, { timeout: 10_000 });
    expect(legend.querySelector('[data-bucket="not-checked"]')?.textContent).toBe('Not checked2');
    // Its own glyph, not the dashed circle of "Not checkable" (review fix).
    expect(legend.querySelector('[data-bucket="not-checked"] .lucide-hourglass')).not.toBeNull();
    expect(firstRow.querySelector('[data-slot="not-checked"] .lucide-hourglass')).not.toBeNull();
  }, 15_000);

  it('says "run the checks" only when Run checks is the next action; after a run, "no result on the last run"', () => {
    const rows = [unchecked('a', 1), checked('c', 3)];
    // Still reading (or checks running): no instruction it cannot follow.
    const waiting = dashboard([unchecked('a', 1)], [], new Set(), false);
    expect(document.querySelector('[data-slot="not-checked-notice"]')?.textContent).toBe('1 countertop not checked yet.');
    expect(document.body.textContent).not.toContain('Run the checks to get a result');
    waiting.unmount();
    // Checks have run (findings exist) and made no result for row a: say so, and point to its card.
    dashboard(rows, [finding('f-c', 'REVIEW_REQUIRED')], new Set(['f-c']), true);
    expect(document.querySelector('[data-slot="not-checked-notice"]')?.textContent).toBe('1 countertop with no result from the last run. Open its countertop card to see why.');
  });

  it('after a run, a row with no result says so in the table and keeps "Open countertop card"', () => {
    const rows = [unchecked('a', 1), checked('c', 3)];
    render(
      <ResultsDashboard
        countertops={{ status: 'ready', rows }} findings={[finding('f-c', 'REVIEW_REQUIRED')]} blockingIds={new Set(['f-c'])} busy={false} filter="all"
        onFilterChange={vi.fn()} onRetry={vi.fn()} onRefresh={vi.fn()} handlers={handlers()} onBulkDismiss={vi.fn(async () => ({ saved: 0, failed: [] }))}
        onShowDrawing={vi.fn()} onOpenCard={vi.fn()} canRunChecks
      />,
    );
    const row = document.querySelector<HTMLElement>('[data-slot="countertop-table"] table tr[data-row-id="a"]')!;
    expect(row.textContent).toContain('No result for this countertop on the last run');
    expect(row.textContent).not.toContain('Run the checks to get a result');
    expect(within(row).getByRole('button', { name: 'Open countertop card' })).toBeTruthy();
  });

  it("the queue's empty state says to run the checks; a checked row in the same list is still an item", () => {
    const props = {
      open: true, opening: 1, onOpenChange: vi.fn(), rowsReady: true, projectId: 'p', packageId: 'pkg', handlers: handlers(),
      next: { kind: 'run-checks' as const, label: 'Run checks', disabled: false, reason: null }, onAct: vi.fn(), onWallSaved: vi.fn(), onOpenCard: vi.fn(),
    };
    const { unmount } = render(<NeedsYouQueue {...props} rows={[unchecked('a', 1), unchecked('b', 2)]} findings={[]} blocking={new Set()} />);
    const done = document.querySelector<HTMLElement>('[data-slot="queue-done"]')!;
    expect(done.textContent).toContain('Nothing to decide yet');
    expect(done.textContent).toContain('2 countertops not checked yet. Run the checks to get a result.');
    expect(within(done).getByRole('button', { name: 'Run checks' })).toBeTruthy();
    unmount();
    render(<NeedsYouQueue {...props} rows={[unchecked('a', 1), checked('c', 3)]} findings={[finding('f-c', 'REVIEW_REQUIRED')]} blocking={new Set(['f-c'])} />);
    // One item, with its noun: the unchecked row is not counted.
    expect(document.querySelector('[data-slot="queue-progress"]')?.textContent).toBe('0 of 1 item');
    expect(document.querySelectorAll('[data-slot="queue-item"]')).toHaveLength(1);
  });

  it('the empty queue says only what its button does: no "run the checks" while checks are running', () => {
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rowsReady projectId="p" packageId="pkg" handlers={handlers()}
        next={{ kind: 'checking', label: 'Checking…', disabled: true, reason: 'Checks are running. Results refresh when they finish.' }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onOpenCard={vi.fn()} rows={[unchecked('a', 1)]} findings={[]} blocking={new Set()}
      />,
    );
    const done = document.querySelector<HTMLElement>('[data-slot="queue-done"]')!;
    expect(done.querySelector('[data-slot="queue-not-checked"]')?.textContent).toBe('1 countertop not checked yet.');
    expect(within(done).getByRole('button', { name: 'Checking…' }).hasAttribute('disabled')).toBe(true);
  });

  it('a split page whose result went away in a re-run still says why there is nothing to decide', () => {
    const split = checked('s', 3, { hold: { code: 'row-choice-split', reason: 'Synthetic: the AIs picked different lines.' }, row_location: null, printed_overall: null });
    const props = {
      open: true, opening: 1, onOpenChange: vi.fn(), rowsReady: true, projectId: 'p', packageId: 'pkg', handlers: handlers(),
      next: { kind: 'review' as const, label: 'Review 1 item', disabled: false, reason: null }, onAct: vi.fn(), onWallSaved: vi.fn(), onOpenCard: vi.fn(),
    };
    const { rerender } = render(<NeedsYouQueue {...props} rows={[split]} findings={[finding('f-s', 'REVIEW_REQUIRED')]} blocking={new Set(['f-s'])} />);
    expect(document.querySelectorAll('[data-slot="queue-item"]')).toHaveLength(1);
    rerender(<NeedsYouQueue {...props} rows={[{ ...split, finding_id: null, outcome: null }]} findings={[]} blocking={null} />);
    expect(document.querySelector('[data-slot="split-no-result"]')?.textContent).toContain('No result for this page on the last run');
  });
});

describe('every blocking count carries its noun (#1179)', () => {
  it('countertops on the card, checks on Other checks; no bare "Needs you" number', async () => {
    const rows = [checked('a', 1), checked('b', 2), unchecked('c', 3)];
    const findings = [finding('f-a', 'REVIEW_REQUIRED'), finding('f-b', 'REVIEW_REQUIRED'), finding('o-1', 'NOT_FOUND', { scope_label: 'Synthetic depth' })];
    dashboard(rows, findings, new Set(['f-a', 'f-b', 'o-1']));
    const cards = document.querySelector<HTMLElement>('[data-slot="kpi-cards"]')!;
    expect(cards.textContent).toMatch(/Countertops need you\s*2/);
    expect(within(cards).getByRole('button', { name: '2 countertops need you: review them one at a time' })).toBeTruthy();
    expect(within(screen.getByRole('region', { name: 'Other checks' })).getByRole('button', { name: /Other checks/ }).textContent).toContain('1 check needs you');
    // The donut's "Needs your decision" number is the card's: shown once, on the card.
    const legend = await screen.findByRole('list', { name: 'Countertop outcomes' }, { timeout: 10_000 });
    expect(legend.querySelector('[data-bucket="needs-you"] .num')?.classList.contains('sr-only')).toBe(true);
  }, 15_000);
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

describe("the header's Run checks runs the checks (#1179)", { timeout: 20_000 }, () => {
  it('from Results: saves the values and posts the run, stays on Results and shows the queued state', async () => {
    const user = userEvent.setup();
    render(<Page />);
    const header = screen.getByTestId('header-actions');
    await user.click(await within(header).findByRole('button', { name: 'Run checks' }));
    // Step 4's own calls, in its order: the values first, then the checks.
    await waitFor(() => expect(writes()).toEqual(['/projects/p/packages/pkg/measurements', '/projects/p/packages/pkg/checks']));
    expect((await screen.findByText('Checks queued. The results update here when they have run.')).closest('[role="status"]')).not.toBeNull();
    expect(await within(header).findByRole('button', { name: 'Checking…' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Results/ }).getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('asks first when the save would also confirm AI-filled values, and runs only on its Run checks', async () => {
    const user = userEvent.setup();
    required = {
      ...REQUIRED,
      proposed_readings: [{ field_key: 'SHOP:sink_width', name: 'sink_width', source: 'SHOP', many: false, placement_verified: false, values: [{ candidate_id: 'c-1', value: '30"', page_index: 0 }] }],
    };
    render(<Page />);
    const header = screen.getByTestId('header-actions');
    await user.click(await within(header).findByRole('button', { name: 'Run checks' }));
    const dialog = await screen.findByRole('dialog', { name: 'Run the checks?' });
    // Each value it confirms, by field, with its value and that it is the AI's proposal.
    const listed = dialog.querySelector('[data-part="confirm-ai-values"]')!;
    expect([...listed.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['Sink width30"AI proposal, not checked against the drawing’s lines']);
    // Measurements was never opened: looking first is the primary action.
    expect(within(dialog).getByRole('button', { name: 'Review the values first' }).getAttribute('data-variant')).toBe('default');
    expect(within(dialog).getByRole('button', { name: 'Run checks' }).getAttribute('data-variant')).toBe('outline');
    expect(writes()).toEqual([]);
    await user.click(within(dialog).getByRole('button', { name: 'Run checks' }));
    await waitFor(() => expect(writes()).toEqual([
      '/projects/p/packages/pkg/candidates/c-1/confirm',
      '/projects/p/packages/pkg/measurements',
      '/projects/p/packages/pkg/checks',
    ]));
    expect(await screen.findByText('Checks queued. The results update here when they have run.')).toBeTruthy();
  });

  it('a refused save is said on Results, nothing is queued, and the header offers Run checks again', async () => {
    const user = userEvent.setup();
    const served = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (input, init) =>
      String(input).endsWith('/measurements') && init?.method === 'POST'
        ? json({ error: 'validation_error', message: 'Synthetic: sink width needs a unit.', request_id: 'r' }, 422)
        : served(input, init));
    render(<Page />);
    const header = screen.getByTestId('header-actions');
    await user.click(await within(header).findByRole('button', { name: 'Run checks' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('The checks were not run. Synthetic: sink width needs a unit.');
    expect(posts.filter((url) => url.endsWith('/checks'))).toEqual([]);
    expect(within(header).getByRole('button', { name: 'Run checks' }).hasAttribute('disabled')).toBe(false);
    await user.click(within(alert).getByRole('button', { name: 'Open Measurements' }));
    expect(screen.getByRole('tab', { name: 'Measurements' }).getAttribute('aria-selected')).toBe('true');
  });
});

describe('Measurements step 3 says what is missing (#1179)', { timeout: 15_000 }, () => {
  async function valuesHint() {
    render(<MeasurementPanel packageId="pkg" />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /Values/ }, { timeout: 5000 }));
    return waitFor(() => {
      const hint = document.querySelector<HTMLElement>('[data-slot="values-hint"]');
      expect(hint).not.toBeNull();
      return hint!;
    });
  }

  it('after a reading that found the countertops: which project values have no value, and the one action', async () => {
    const hint = await valuesHint();
    expect(hint.textContent).toBe('The countertops were read; no project values were. 2 values have no value and no default: Countertop depth and Sink width. Type them in below.');
    expect(hint.textContent).not.toContain('Nothing was read');
  });

  it('only when nothing at all was read: the real "nothing was read" message', async () => {
    slotRows = [];
    const hint = await valuesHint();
    expect(hint.textContent).toBe('Nothing was read off these drawings. Check on Documents that both finished uploading and were read.');
  });
});

describe("the header's Run checks: guards and the dialog (#1179 review)", { timeout: 20_000 }, () => {
  const AI = { field_key: 'SHOP:sink_width', name: 'sink_width', source: 'SHOP', many: false, placement_verified: true, values: [{ candidate_id: 'c-1', value: '30"', page_index: 0 }] };

  it('layout answers only: lists each with its answer, marks the drawing\'s proposal, and sends it with the run', async () => {
    const user = userEvent.setup();
    required = {
      ...REQUIRED,
      discriminators: [{ name: 'wall_config', rule_ids: ['CT-WIDTH-001'], choices: ['back_only', 'island'], proposal: { value: 'back_only', crop_artifact_id: 'crop', model_id: 'code', prompt_id: 'slot-walls-drawing-clues-v1', confirmed: false, requires_confirmation: false } }],
    };
    render(<Page />);
    await user.click(await within(screen.getByTestId('header-actions')).findByRole('button', { name: 'Run checks' }));
    const dialog = await screen.findByRole('dialog', { name: 'Run the checks?' });
    expect(dialog.querySelector('[data-part="confirm-ai-values"]')).toBeNull();
    expect([...dialog.querySelectorAll('[data-part="confirm-layout"] li')].map((li) => li.textContent)).toEqual(['Wall configBack wall onlyproposed from the drawing']);
    await user.click(within(dialog).getByRole('button', { name: 'Run checks' }));
    await waitFor(() => expect(writes()).toEqual(['/projects/p/packages/pkg/measurements', '/projects/p/packages/pkg/checks']));
    const sent = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).endsWith('/checks') && init?.method === 'POST')!;
    expect(JSON.parse(String(sent[1]!.body))).toEqual({ discriminators: { wall_config: 'back_only' } });
  });

  it('Cancel and "Review … first" write nothing; once Measurements was opened, Run checks is the primary action', async () => {
    const user = userEvent.setup();
    required = { ...REQUIRED, proposed_readings: [AI] };
    render(<Page />);
    const header = screen.getByTestId('header-actions');
    await user.click(await within(header).findByRole('button', { name: 'Run checks' }));
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await user.click(within(header).getByRole('button', { name: 'Run checks' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Review the values first' }));
    expect(screen.getByRole('tab', { name: 'Measurements' }).getAttribute('aria-selected')).toBe('true');
    expect(writes()).toEqual([]);
    // Opened now: asked again from the header, Run checks leads.
    await user.click(within(header).getByRole('button', { name: 'Run checks' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('button', { name: 'Run checks' }).getAttribute('data-variant')).toBe('default');
    await user.click(within(dialog).getByRole('button', { name: /Review the values first/ }));
    expect(writes()).toEqual([]);
  });

  it('a countertop row with unsaved changes stops the run, with its count in words', async () => {
    const user = userEvent.setup();
    slotRows = [{ ...SLOT, values: [{ key: 'piece', label: 'Piece 1', position: 0, value: null, suggestion: null, source: 'missing', review_reason: null, needs_value: true }] }];
    render(<Page />);
    await user.click(await screen.findByRole('tab', { name: 'Measurements' }));
    await user.click(within(await screen.findByRole('navigation', { name: 'Measurement steps' }, { timeout: 5000 })).getByRole('button', { name: /Countertops/ }));
    await user.type(await screen.findByPlaceholderText('Enter the value with its unit', undefined, { timeout: 5000 }), '3"');
    await user.click(screen.getByRole('tab', { name: /Results/ }));
    await user.click(within(screen.getByTestId('header-actions')).getByRole('button', { name: 'Run checks' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('The checks were not run. 1 countertop row has unsaved changes in Measurements, step 2.');
    expect(alert.querySelector('.num')?.textContent).toBe('1');
    expect(writes()).toEqual([]);
  });

  it('one run only: pressed twice while starting, a single request; "Starting checks…" until it is queued', async () => {
    const user = userEvent.setup();
    let open!: () => void;
    checksGate = new Promise<void>((resolve) => { open = resolve; });
    render(<Page />);
    const header = screen.getByTestId('header-actions');
    const run = await within(header).findByRole('button', { name: 'Run checks' });
    await user.dblClick(run);
    const starting = await within(header).findByRole('button', { name: 'Starting checks…' });
    expect(starting.hasAttribute('disabled')).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
    open();
    expect(await within(header).findByRole('button', { name: 'Checking…' })).toBeTruthy();
    expect(writes().filter((url) => url.endsWith('/checks'))).toHaveLength(1);
  });

  it('a step 4 run already starting keeps the header off and says nothing extra', async () => {
    const user = userEvent.setup();
    let open!: () => void;
    checksGate = new Promise<void>((resolve) => { open = resolve; });
    render(<Page />);
    await user.click(await screen.findByRole('tab', { name: 'Measurements' }));
    await user.click(within(await screen.findByRole('navigation', { name: 'Measurement steps' }, { timeout: 5000 })).getByRole('button', { name: /Settings/ }));
    await new Promise((resolve) => setTimeout(resolve, 350)); // step 4's own settle guard
    await user.click(document.getElementById('measure-run-checks')!);
    const header = screen.getByTestId('header-actions');
    expect((await within(header).findByRole('button', { name: 'Starting checks…' })).hasAttribute('disabled')).toBe(true);
    expect(screen.queryByText(/The checks were not run/)).toBeNull();
    open();
    expect(await within(header).findByRole('button', { name: 'Checking…' })).toBeTruthy();
    expect(writes().filter((url) => url.endsWith('/checks'))).toHaveLength(1);
  });

  it('from the Measurements tab: runs, and opens step 4 where its own button is', async () => {
    const user = userEvent.setup();
    render(<Page />);
    await user.click(await screen.findByRole('tab', { name: 'Measurements' }));
    await screen.findByRole('navigation', { name: 'Measurement steps' }, { timeout: 5000 });
    await user.click(within(screen.getByTestId('header-actions')).getByRole('button', { name: 'Run checks' }));
    await waitFor(() => expect(writes()).toEqual(['/projects/p/packages/pkg/measurements', '/projects/p/packages/pkg/checks']));
    expect(document.getElementById('measure-run-checks')).not.toBeNull();
    expect(await screen.findByText('Checks queued. The results update here when they have run.')).toBeTruthy();
  });

  it('a form that cannot load is said, and nothing is written', async () => {
    const user = userEvent.setup();
    requiredFails = true;
    render(<Page />);
    await user.click(await within(screen.getByTestId('header-actions')).findByRole('button', { name: 'Run checks' }));
    const alert = await screen.findByRole('alert', {}, { timeout: 5000 });
    expect(alert.textContent).toContain('The checks were not run. The measurements could not be loaded: Synthetic outage');
    expect(writes()).toEqual([]);
  });

  it('while the server is checking, or once signed off, nothing offers a run: header and step 4 are off', async () => {
    const user = userEvent.setup();
    for (const [state, reason] of [['RUNNING_CHECKS', 'Checks are already running.'], ['APPROVED', 'This review is signed off']] as const) {
      pkgState = state;
      const view = render(<Page />);
      const header = screen.getByTestId('header-actions');
      await waitFor(() => expect(within(header).queryByRole('button', { name: 'Run checks' })).toBeNull());
      await user.click(screen.getByRole('tab', { name: 'Measurements' }));
      await user.click(within(await screen.findByRole('navigation', { name: 'Measurement steps' }, { timeout: 5000 })).getByRole('button', { name: /Settings/ }));
      expect(document.getElementById('measure-run-checks')?.hasAttribute('disabled')).toBe(true);
      expect(document.querySelector('[data-part="run-blocked"]')?.textContent).toContain(reason);
      view.unmount();
    }
    expect(writes()).toEqual([]);
  });

  it('asks again when what it would record changed after the reviewer agreed', async () => {
    required = { ...REQUIRED, proposed_readings: [AI] };
    const onHeaderRun = vi.fn();
    render(<MeasurementPanel packageId="pkg" headerRun={{ id: 1, confirmed: '{"ai":[],"layout":[]}' }} onHeaderRun={onHeaderRun} />);
    await waitFor(() => expect(onHeaderRun).toHaveBeenCalled(), { timeout: 5000 });
    expect(onHeaderRun.mock.calls[0][0]).toMatchObject({ id: 1, kind: 'confirm', list: { aiValues: [{ field: 'Sink width', value: '30"' }] } });
    expect(writes()).toEqual([]);
  });
});
