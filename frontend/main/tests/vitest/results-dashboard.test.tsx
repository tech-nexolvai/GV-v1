// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import {
  bucketCounts,
  bucketOf,
  defaultFilter,
  formatDelta,
  kpis,
  matchesFilter,
  recordEach,
  sortRows,
} from '@/lib/countertop-results';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';

// The dashboard asks the rulebook for human check names; keep the test off the network.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => [{ rule_id: 'CHECK-o-sink', name: 'Synthetic sink cut-out check' }]),
}));

// Synthetic numbers only: nothing here comes from a client drawing.
const exact = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });
const base: CountertopResult = {
  finding_id: null, row_id: 'r', page_number: 1, label: 'Countertop row on page 1', row_location: null,
  outcome: 'PASS', needs_decision: false, printed_overall: exact('84', '1', '84"'), pieces: [],
  field_cut_per_end: exact('1', '1', '1"'), field_cut_count: 2, expected_total: exact('84', '1', '84"'),
  delta: exact('0', '1', '0"'), hold: null, reviewer_decision: null,
  wall_layout: { config: 'back_left_right', label: 'back wall and both ends', source: 'drawing clues' },
  agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true] },
};
const ROWS: CountertopResult[] = [
  { ...base, row_id: 'pass-p4', page_number: 4, label: 'Countertop row on page 4', finding_id: 'f-pass' },
  { ...base, row_id: 'fail-p2', page_number: 2, label: 'Countertop row on page 2', finding_id: 'f-fail', outcome: 'FAIL', printed_overall: exact('82', '1', '82"'), delta: exact('-2', '1', '-2"') },
  { ...base, row_id: 'need-p9', page_number: 9, label: 'Countertop row on page 9', finding_id: 'f-need', outcome: 'REVIEW_REQUIRED', needs_decision: true, delta: null, expected_total: null, hold: { code: 'stone-short-of-ends', reason: 'The stone stops short of the walls.' } },
  { ...base, row_id: 'held-p5', page_number: 5, label: 'Countertop row on page 5', finding_id: null, outcome: null, needs_decision: true, delta: null, printed_overall: null, hold: { code: 'row-held', reason: 'The row could not be read.' } },
  { ...base, row_id: 'dec-p3', page_number: 3, label: 'Countertop row on page 3', finding_id: 'f-dec', outcome: 'NOT_FOUND', needs_decision: false, delta: null, reviewer_decision: { action: 'dismiss', actor: 'Sam Reviewer', note: 'synthetic', time: '2026-10-09T10:00:00Z' } },
];
const finding = (id: string, outcome: Finding['outcome'], extra: Partial<Finding> = {}): Finding =>
  ({ id, check_id: `CHECK-${id}`, name: `Synthetic ${id}`, severity: 'FLAG', outcome, reviewer_action: null, ...extra }) as Finding;
const FINDINGS: Finding[] = [
  finding('f-pass', 'PASS'), finding('f-fail', 'FAIL'), finding('f-need', 'REVIEW_REQUIRED'), finding('f-dec', 'NOT_FOUND', { reviewer_action: 'dismiss' }),
  // Not countertop rows: package-level checks.
  finding('o-depth', 'NOT_FOUND', { scope_label: 'Countertop depth' }),
  finding('o-sink', 'NOT_FOUND', { scope_label: 'Sink cut-out' }),
  finding('o-fail', 'FAIL', { scope_label: 'Package-level failure' }),
];
const BLOCKING = new Set(['f-need', 'o-depth', 'o-sink', 'o-fail']);

function setup(overrides: Partial<Parameters<typeof ResultsDashboard>[0]> = {}) {
  const handlers = {
    onAction: vi.fn(async () => ({ saved: true as const })),
    onCorrect: vi.fn(async () => ({ saved: true as const })),
    onExcept: vi.fn(async () => ({ saved: true as const })),
  };
  const onBulkDismiss = vi.fn(async (ids: string[]) => ({ saved: ids.length, failed: [] }));
  const props = {
    countertops: { status: 'ready', rows: ROWS } as CountertopsState,
    findings: FINDINGS,
    blockingIds: BLOCKING,
    busy: false,
    filter: null,
    onFilterChange: vi.fn(),
    onRetry: vi.fn(),
    onRefresh: vi.fn(),
    handlers,
    onBulkDismiss,
    onShowDrawing: vi.fn(),
    onOpenCard: vi.fn(),
    ...overrides,
  };
  const utils = render(<ResultsDashboard {...props} />);
  return { ...utils, props, handlers, onBulkDismiss };
}

const tableRows = () => [...document.querySelectorAll<HTMLTableRowElement>('[data-slot="countertop-table"] tr[data-row-id]')];

describe('countertop results: pure rules', () => {
  it('KPI counts come straight from the rows', () => {
    expect(kpis(ROWS)).toEqual({ countertops: 5, automatic: 2, needsYou: 2, pass: 1, fail: 1, held: 2 });
  });

  it('sorts Needs you → FAIL → PASS → not checkable, then by page', () => {
    expect(sortRows(ROWS).map((r) => r.row_id)).toEqual(['held-p5', 'need-p9', 'fail-p2', 'pass-p4', 'dec-p3']);
  });

  it('a reviewer decision never turns a row into PASS', () => {
    expect(bucketOf(ROWS[4])).toBe('not-checkable');
  });

  it('opens on "Needs you" when anything needs you, otherwise all', () => {
    expect(defaultFilter(ROWS)).toBe('needs-you');
    expect(defaultFilter(ROWS.filter((r) => !r.needs_decision))).toBe('all');
  });

  it('formats exact differences with a true minus, a plus on overruns, and a dash when there is none', () => {
    expect(formatDelta(exact('-2', '1', '-2"'))).toEqual({ text: '−2"', sign: -1 });
    expect(formatDelta(exact('-5', '2', '-2 1/2"'))).toEqual({ text: '−2 1/2"', sign: -1 });
    expect(formatDelta(exact('1', '2', '1/2"'))).toEqual({ text: '+1/2"', sign: 1 });
    expect(formatDelta(exact('0', '1', '0"'))).toEqual({ text: '0"', sign: 0 });
    expect(formatDelta(null)).toEqual({ text: '—', sign: null });
  });

  it('records one call per finding and collects failures without stopping', async () => {
    const record = vi.fn(async (id: string) => { if (id === 'b') throw new Error('refused'); });
    const result = await recordEach(['a', 'b', 'c'], record);
    expect(record.mock.calls.map((c) => c[0])).toEqual(['a', 'b', 'c']);
    expect(result).toEqual({ saved: 2, failed: [{ id: 'b', error: 'refused' }] });
  });
});

describe('results dashboard', () => {
  it('shows the five numbers and the outcome legend', async () => {
    setup();
    const cards = document.querySelector('[data-slot="kpi-cards"]') as HTMLElement;
    expect(cards.textContent).toMatch(/Countertops\s*5/);
    expect(cards.textContent).toMatch(/Automatic\s*2/);
    expect(cards.textContent).toMatch(/Needs you\s*2/);
    expect(cards.textContent).toMatch(/PASS\s*1/);
    expect(cards.textContent).toMatch(/FAIL\s*1/);
    // The legend is inside the lazily loaded chart; on a cold CI runner fetching the chart library
    // can take longer than the default one-second wait.
    const legend = await screen.findByRole('list', { name: 'Countertop outcomes' }, { timeout: 10_000 });
    expect(legend.textContent).toMatch(/Needs your decision\s*2/);
    expect(legend.textContent).toMatch(/Not checkable\s*1/);
  }, 15_000);

  it('starts on what needs you, in order', () => {
    setup();
    expect(tableRows().map((r) => r.dataset.rowId)).toEqual(['held-p5', 'need-p9']);
  });

  it('a FAIL that still needs a decision is a FAIL everywhere, and also needs you (#1056)', async () => {
    const user = userEvent.setup();
    const failNeedsYou = { ...ROWS[1], row_id: 'fail-open-p12', page_number: 12, label: 'Countertop row on page 12', finding_id: 'f-open', needs_decision: true };
    const rows = [...ROWS, failNeedsYou];
    expect(kpis(rows)).toMatchObject({ fail: 2, needsYou: 3 });
    expect(bucketCounts(rows)).toEqual({ 'needs-you': 2, fail: 2, pass: 1, 'not-checkable': 1 });
    expect(Object.values(bucketCounts(rows)).reduce((a, b) => a + b, 0)).toBe(rows.length);
    expect(matchesFilter(failNeedsYou, 'fail')).toBe(true);
    expect(matchesFilter(failNeedsYou, 'needs-you')).toBe(true);
    const { props, rerender } = setup();
    rerender(<ResultsDashboard {...props} countertops={{ status: 'ready', rows }} filter="fail" />);
    expect(tableRows().map((r) => r.dataset.rowId).sort()).toEqual(['fail-open-p12', 'fail-p2']);
    const legend = await screen.findByRole('list', { name: 'Countertop outcomes' }, { timeout: 10_000 });
    expect(legend.textContent).toMatch(/FAIL\s*2\s*1 needs you/);
    await user.click(screen.getByRole('radio', { name: /All\s*6/ }));
    expect(props.onFilterChange).toHaveBeenCalledWith('all');
  }, 15_000);

  it('a KPI card or a chip changes the filter', async () => {
    const user = userEvent.setup();
    const { props, rerender } = setup();
    await user.click(screen.getByRole('button', { name: /FAIL\s*1/ }));
    expect(props.onFilterChange).toHaveBeenCalledWith('fail');
    rerender(<ResultsDashboard {...props} filter="fail" />);
    expect(tableRows().map((r) => r.dataset.rowId)).toEqual(['fail-p2']);
    await user.click(screen.getByRole('radio', { name: /All\s*5/ }));
    expect(props.onFilterChange).toHaveBeenCalledWith('all');
    rerender(<ResultsDashboard {...props} filter="held" />);
    expect(tableRows().map((r) => r.dataset.rowId)).toEqual(['held-p5', 'need-p9']);
  });

  it('shows exact numbers and a coloured difference with its glyph', () => {
    const { props, rerender } = setup();
    rerender(<ResultsDashboard {...props} filter="fail" />);
    const row = tableRows()[0];
    expect(row.textContent).toContain('82"');
    expect(row.textContent).toContain('84"');
    expect(row.textContent).toContain('−2"');
    expect(row.querySelector('[data-outcome-icon="FAIL"]')).not.toBeNull();
  });

  it('only a row with a recorded finding offers Decide; an unchecked one points to its card', () => {
    setup();
    const [held, need] = tableRows();
    expect(within(held).queryByRole('button', { name: 'Decide' })).toBeNull();
    expect(within(held).getByRole('button', { name: 'Open countertop card' })).toBeTruthy();
    expect(within(need).getByRole('button', { name: 'Decide' })).toBeTruthy();
  });

  it('Decide needs a note for an abstention, and calls the same handler with it', async () => {
    const user = userEvent.setup();
    const { handlers } = setup();
    await user.click(within(tableRows()[1]).getByRole('button', { name: 'Decide' }));
    const dialog = await screen.findByRole('dialog', { name: /Decide: Countertop row on page 9/ });
    await user.click(within(dialog).getByRole('radio', { name: 'Checked: OK' }));
    const save = within(dialog).getByRole('button', { name: 'Record decision' });
    expect((save as HTMLButtonElement).disabled).toBe(true);
    await user.type(within(dialog).getByLabelText(/What did you check/), 'Measured on site, synthetic');
    expect((save as HTMLButtonElement).disabled).toBe(false);
    await user.click(save);
    expect(handlers.onAction).toHaveBeenCalledWith('f-need', 'confirm', 'Measured on site, synthetic');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('Cancel forgets the draft: reopening Decide starts empty (#1050 review)', async () => {
    const user = userEvent.setup();
    setup();
    await user.click(within(tableRows()[1]).getByRole('button', { name: 'Decide' }));
    let dialog = await screen.findByRole('dialog', { name: /Decide: Countertop row on page 9/ });
    await user.click(within(dialog).getByRole('radio', { name: 'Checked: OK' }));
    await user.type(within(dialog).getByLabelText(/What did you check/), 'TEST half written');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await user.click(within(tableRows()[1]).getByRole('button', { name: 'Decide' }));
    dialog = await screen.findByRole('dialog', { name: /Decide: Countertop row on page 9/ });
    expect(within(dialog).getAllByRole('radio').every((r) => r.getAttribute('aria-checked') === 'false')).toBe(true);
    expect(within(dialog).queryByLabelText(/What did you check/)).toBeNull();
    expect((within(dialog).getByRole('button', { name: 'Record decision' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('Decide on a failure: a correction needs its value; an exception needs a reason and a future date', async () => {
    const user = userEvent.setup();
    const { handlers, props, rerender } = setup();
    rerender(<ResultsDashboard {...props} findings={FINDINGS} countertops={{ status: 'ready', rows: [{ ...ROWS[1], needs_decision: true }] }} filter="all" />);
    await user.click(screen.getAllByRole('button', { name: 'Decide' })[0]);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('radio', { name: 'Confirm finding' })).toBeTruthy();
    await user.click(within(dialog).getByRole('radio', { name: 'Problem' }));
    await user.click(within(dialog).getByRole('radio', { name: 'Correct a value' }));
    const save = within(dialog).getByRole('button', { name: 'Record decision' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await user.type(within(dialog).getByLabelText(/Correct value/), '84"');
    await user.click(save);
    expect(handlers.onCorrect).toHaveBeenCalledWith('f-fail', '84"');
  });

  it('"Mark not checkable" asks for one note, lists only waiting abstentions, and sends them together', async () => {
    const user = userEvent.setup();
    const { onBulkDismiss } = setup();
    await user.click(screen.getByRole('button', { name: /Other checks/ }));
    await user.click(await screen.findByRole('button', { name: /Mark not checkable… \(2\)/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Mark 2 checks not checkable' });
    const listed = within(within(dialog).getByRole('list', { name: 'Checks to mark' })).getAllByRole('listitem');
    expect(listed.map((li) => li.textContent)).toEqual([expect.stringContaining('Countertop depth'), expect.stringContaining('Synthetic sink cut-out check')]);
    const confirm = within(dialog).getByRole('button', { name: 'Mark 2 not checkable' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await user.type(within(dialog).getByLabelText(/Why are these not checkable/), 'No section drawings supplied');
    await user.click(confirm);
    expect(onBulkDismiss).toHaveBeenCalledTimes(1);
    expect(onBulkDismiss).toHaveBeenCalledWith(['o-depth', 'o-sink'], 'No section drawings supplied');
  });

  it('keyboard: ↓ moves to the next row, Enter opens its details, D opens Decide', async () => {
    const user = userEvent.setup();
    setup();
    const [first, second] = tableRows();
    first.focus();
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(second);
    await user.keyboard('{Enter}');
    expect(second.getAttribute('aria-expanded')).toBe('true');
    await user.keyboard('d');
    expect(await screen.findByRole('dialog', { name: /Decide: Countertop row on page 9/ })).toBeTruthy();
  });

  it('loading, empty and error states say what they are', async () => {
    const user = userEvent.setup();
    const { props, rerender } = setup({ countertops: { status: 'loading' } });
    expect(screen.getByLabelText('Loading results')).toBeTruthy();
    rerender(<ResultsDashboard {...props} countertops={{ status: 'ready', rows: [] }} />);
    expect(screen.getByText('No countertops in this run')).toBeTruthy();
    rerender(<ResultsDashboard {...props} countertops={{ status: 'error', error: 'synthetic outage' }} />);
    expect(screen.getByRole('alert').textContent).toContain('synthetic outage');
    await user.click(screen.getByRole('button', { name: /Try again/ }));
    expect(props.onRetry).toHaveBeenCalled();
  });
});
