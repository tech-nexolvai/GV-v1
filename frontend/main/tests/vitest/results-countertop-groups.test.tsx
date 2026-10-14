// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { groupByCountertop, sectionRows } from '@/lib/countertop-results';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';

// Results grouped per confirmed countertop (#1245). Synthetic values only.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
}));

const exact = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });
const base: CountertopResult = {
  finding_id: null, row_id: 'r', page_number: 1, label: 'Countertop row on page 1', row_location: null,
  outcome: 'PASS', needs_decision: false, printed_overall: exact('84', '1', '84"'), pieces: [],
  field_cut_per_end: exact('1', '1', '1"'), field_cut_count: 2, expected_total: exact('84', '1', '84"'),
  delta: exact('0', '1', '0"'), hold: null, reviewer_decision: null,
  wall_layout: { config: 'back_left_right', label: 'back wall and both ends', source: 'drawing clues' },
  agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true] },
};
const item = (id: string, page: number, number = 1) => ({ item_id: id, page_number: page, number, label: `Countertop ${number} on page ${page}` });
const GROUPED: CountertopResult[] = [
  { ...base, row_id: 'row-p2', page_number: 2, label: 'Countertop row on page 2', finding_id: 'w-p2', countertop_item: item('item-b', 2) },
  { ...base, row_id: 'row-p1', page_number: 1, label: 'Countertop row on page 1', finding_id: 'w-p1', countertop_item: item('item-a', 1) },
  { ...base, row_id: 'row-p3', page_number: 3, label: 'Countertop row on page 3', finding_id: 'w-p3', countertop_item: null },
];
const finding = (id: string, outcome: Finding['outcome'], extra: Partial<Finding> = {}): Finding =>
  ({ id, check_id: `CHECK-${id}`, name: `Synthetic ${id}`, severity: 'FLAG', outcome, reviewer_action: null, ...extra }) as Finding;
const FINDINGS: Finding[] = [
  finding('w-p1', 'PASS'), finding('w-p2', 'PASS'), finding('w-p3', 'PASS'),
  // A countertop's own result that is not on its row, and one keyed by its row.
  finding('own-a', 'REVIEW_REQUIRED', { scope_item_id: 'item-a' }),
  finding('row-b', 'NOT_FOUND', { scope_row_candidate_id: 'row-p2' }),
  // The whole package's.
  finding('pkg-depth', 'NOT_FOUND', { scope_label: 'Package revision' }),
];

function setup(
  rows: CountertopResult[],
  { filter = 'all', countertopsChecked }: { filter?: 'all' | 'needs-you' | 'fail'; countertopsChecked?: boolean } = {},
) {
  const props = {
    countertops: { status: 'ready', rows, countertopsChecked } as CountertopsState,
    findings: FINDINGS,
    blockingIds: new Set(['own-a', 'pkg-depth']),
    busy: false,
    filter,
    onFilterChange: vi.fn(),
    onRetry: vi.fn(),
    onRefresh: vi.fn(),
    handlers: {
      onAction: vi.fn(async () => ({ saved: true as const })),
      onCorrect: vi.fn(async () => ({ saved: true as const })),
      onExcept: vi.fn(async () => ({ saved: true as const })),
    },
    onBulkDismiss: vi.fn(async (ids: string[]) => ({ saved: ids.length, failed: [] })),
    onShowDrawing: vi.fn(),
    onOpenCard: vi.fn(),
  };
  return render(<ResultsDashboard {...props} />);
}

const table = () => document.querySelector<HTMLElement>('[data-slot="countertop-table"] table')!;
const bodyRows = () => [...table().querySelectorAll<HTMLTableRowElement>('tbody > tr')];

describe('grouping per countertop: pure rules', () => {
  it('no confirmed countertop: no groups, and every other result stays as it was', () => {
    const rows = GROUPED.map((row) => ({ ...row, countertop_item: null }));
    const others = FINDINGS.slice(3);
    expect(groupByCountertop(rows, others)).toEqual({ groups: [], wholePackage: others });
    expect(sectionRows(rows, [])).toEqual([{ group: null, rows }]);
  });

  it('a result keyed by a grouped row, or by the countertop, goes under it; the rest is the whole package', () => {
    const { groups, wholePackage } = groupByCountertop(GROUPED, FINDINGS.slice(3));
    expect(groups.map((group) => [group.item.item_id, group.rowIds, group.findings.map((f) => f.id)])).toEqual([
      ['item-a', ['row-p1'], ['own-a']],
      ['item-b', ['row-p2'], ['row-b']],
    ]);
    expect(wholePackage.map((f) => f.id)).toEqual(['pkg-depth']);
  });

  it('rows keep their order within each countertop; the ungrouped rows come last', () => {
    const { groups } = groupByCountertop(GROUPED, []);
    expect(sectionRows(GROUPED, groups).map((section) => [section.group?.item.item_id ?? null, section.rows.map((r) => r.row_id)])).toEqual([
      ['item-a', ['row-p1']],
      ['item-b', ['row-p2']],
      [null, ['row-p3']],
    ]);
  });
});

describe('results grouped per countertop on screen (#1245)', () => {
  it('each countertop heads its rows and its other results; "Whole package" holds the rest', async () => {
    setup(GROUPED);

    const headings = [...table().querySelectorAll<HTMLElement>('tr[data-slot="countertop-group"]')];
    expect(headings.map((h) => h.querySelector('.font-medium')?.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      'Countertop 1 · page 1',
      'Countertop 1 · page 2',
      'Not confirmed as a countertop yet',
    ]);
    // Each heading sits right above its own row.
    const order = bodyRows().map((row) => row.dataset.itemId ?? row.dataset.rowId).filter(Boolean);
    expect(order).toEqual(['item-a', 'row-p1', 'item-b', 'row-p2', 'row-p3']);
    // The countertop's other results are listed under its heading, with Decide where it needs you.
    const first = within(headings[0]);
    expect(first.getByText('Synthetic own-a')).toBeTruthy();
    expect(first.getByRole('button', { name: 'Decide' })).toBeTruthy();
    expect(within(headings[1]).getByText('Synthetic row-b')).toBeTruthy();

    const whole = screen.getByRole('region', { name: 'Whole package' });
    await userEvent.click(within(whole).getByRole('button', { name: /Whole package/ }));
    expect(within(whole).getByText('Synthetic pkg-depth')).toBeTruthy();
    expect(within(whole).queryByText('Synthetic own-a')).toBeNull();
    expect(within(whole).queryByText('Synthetic row-b')).toBeNull();
  });

  it('with no confirmed countertop the screen is as before: no headings, "Other checks"', () => {
    setup(GROUPED.map((row) => ({ ...row, countertop_item: null })));

    expect(table().querySelectorAll('[data-slot="countertop-group"]')).toHaveLength(0);
    expect(screen.getByRole('region', { name: 'Other checks' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Whole package' })).toBeNull();
  });
});

describe('a countertop hidden by the filter never hides its results (#1247 review, M2)', () => {
  // Only the ungrouped row needs the reviewer, so "Needs you" (the default) shows only it.
  const NEEDS: CountertopResult[] = GROUPED.map((row) =>
    row.row_id === 'row-p3' ? { ...row, outcome: 'REVIEW_REQUIRED', needs_decision: true } : row,
  );

  async function wholePackage({ openedAlready = false } = {}) {
    const whole = screen.getByRole('region', { name: 'Whole package' });
    if (!openedAlready) await userEvent.click(within(whole).getByRole('button', { name: /Whole package/ }));
    return whole;
  }

  async function expectBothInWholePackage({ openedAlready = false } = {}) {
    const whole = await wholePackage({ openedAlready });
    expect(within(whole).getByText('Synthetic own-a')).toBeTruthy();
    expect(within(whole).getByText('Synthetic row-b')).toBeTruthy();
    expect(within(whole).getByText('Synthetic pkg-depth')).toBeTruthy();
    // Each shown exactly once on the whole screen.
    expect(screen.getAllByText('Synthetic own-a')).toHaveLength(1);
    expect(screen.getAllByText('Synthetic row-b')).toHaveLength(1);
  }

  it('"Needs you": a countertop whose own row is filtered out keeps its results in "Whole package", with Decide', async () => {
    setup(NEEDS, { filter: 'needs-you' });

    expect(table().querySelectorAll('tr[data-slot="countertop-group"][data-item-id]')).toHaveLength(0);
    await expectBothInWholePackage();
    // Its result that needs the reviewer can still be decided, from "Whole package".
    const whole = screen.getByRole('region', { name: 'Whole package' });
    const own = within(whole).getByText('Synthetic own-a').closest('li') as HTMLElement;
    expect(within(own).getByRole('button', { name: 'Decide' })).toBeTruthy();
  });

  it('a search that leaves out a countertop keeps its results in "Whole package"', async () => {
    setup(GROUPED);
    await userEvent.type(screen.getByRole('textbox', { name: 'Find a page or countertop' }), '3');

    expect(table().querySelectorAll('tr[data-slot="countertop-group"][data-item-id]')).toHaveLength(0);
    await expectBothInWholePackage();
  });

  it('no row matches at all: every countertop\'s results are in "Whole package"', async () => {
    setup(GROUPED, { filter: 'fail' });

    expect(document.querySelector('[data-slot="countertop-table"]')).toBeNull();
    await expectBothInWholePackage();
  });

  it('a package whose checks include no countertop shows no table: the results stay in the list', async () => {
    setup(GROUPED, { countertopsChecked: false });

    expect(document.querySelector('[data-slot="countertop-table"]')).toBeNull();
    // On such a package the list is open from the start (#1205).
    await expectBothInWholePackage({ openedAlready: true });
  });

  it('a countertop still shown keeps its results under it, and only the hidden one\'s move', async () => {
    setup(GROUPED);
    await userEvent.type(screen.getByRole('textbox', { name: 'Find a page or countertop' }), '1');

    const headings = [...table().querySelectorAll<HTMLElement>('tr[data-slot="countertop-group"]')];
    expect(headings.map((h) => h.dataset.itemId)).toEqual(['item-a']);
    expect(within(headings[0]).getByText('Synthetic own-a')).toBeTruthy();
    const whole = await wholePackage();
    expect(within(whole).getByText('Synthetic row-b')).toBeTruthy();
    expect(within(whole).queryByText('Synthetic own-a')).toBeNull();
  });
});
