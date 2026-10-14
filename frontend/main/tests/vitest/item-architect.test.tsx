// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { getCountertopItemsArchitect, type CountertopItemArchitect, type CountertopResult } from '@/api/client';
import { itemArchitectWords } from '@/lib/item-architect';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';

// Each confirmed countertop's one read-only line about the architect (#1273). Synthetic values only.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
  getCountertopItemsArchitect: vi.fn(),
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
const item = (id: string, page: number) => ({ item_id: id, page_number: page, number: 1, label: `Countertop 1 on page ${page}` });
const ROWS: CountertopResult[] = [
  { ...base, row_id: 'row-p1', page_number: 1, countertop_item: item('item-a', 1) },
  { ...base, row_id: 'row-p2', page_number: 2, label: 'Countertop row on page 2', countertop_item: item('item-b', 2) },
];
const status = (itemId: string, page: number, extra: Partial<CountertopItemArchitect>): CountertopItemArchitect => ({
  countertop: item(itemId, page), row_id: null, state: 'not_compared', reason: null, pairs: [],
  pairing_source: null, pairing_judgments: null, ...extra,
});
const pair = (kind: 'piece' | 'overall', label: string, type: string | null = null) => ({
  kind, label, vendor_piece: kind === 'overall' ? null : 2, piece_item_id: type ? 'piece-1' : null,
  piece_item_type: type, architect_candidate_id: 'a-1',
});

function setup(rows: CountertopResult[], packageId: string | undefined = 'pkg') {
  return render(
    <ResultsDashboard
      countertops={{ status: 'ready', rows } as CountertopsState}
      findings={[]}
      blockingIds={new Set()}
      busy={false}
      filter="all"
      onFilterChange={vi.fn()}
      onRetry={vi.fn()}
      onRefresh={vi.fn()}
      handlers={{
        onAction: vi.fn(async () => ({ saved: true as const })),
        onCorrect: vi.fn(async () => ({ saved: true as const })),
        onExcept: vi.fn(async () => ({ saved: true as const })),
      }}
      onBulkDismiss={vi.fn(async (ids: string[]) => ({ saved: ids.length, failed: [] }))}
      onShowDrawing={vi.fn()}
      onOpenCard={vi.fn()}
      packageId={packageId}
    />,
  );
}

const heading = (itemId: string) =>
  document.querySelector<HTMLElement>(`table tr[data-slot="countertop-group"][data-item-id="${itemId}"]`)!;

describe('the words of the architect line (#1273)', () => {
  it('not compared says the stored reason, or points above the table when it is said there', () => {
    const found = status('i', 1, { reason: 'The architect prints only blocking here.' });
    expect(itemArchitectWords(found)).toBe('Architect: not compared — The architect prints only blocking here.');
    expect(itemArchitectWords(found, 'The architect prints only blocking here.')).toBe(
      'Architect: not compared (the reason is above the table)',
    );
    expect(itemArchitectWords(found, 'Another reason.')).toBe('Architect: not compared — The architect prints only blocking here.');
  });

  it('waiting for the reviewer says what to do; compared names each pair and its confirmed piece', () => {
    expect(itemArchitectWords(status('i', 1, { state: 'waits_for_reviewer', reason: 'Pair it (one click).' }))).toBe(
      'Architect: waits for you — Pair it (one click).',
    );
    expect(
      itemArchitectWords(status('i', 1, { state: 'compared', pairs: [pair('overall', 'overall'), pair('piece', 'piece 2', 'cabinet_width'), pair('piece', 'piece 3')] })),
    ).toBe('Architect: compared the overall, piece 2 (cabinet) and piece 3');
  });
});

describe('the architect line under each confirmed countertop (#1273)', () => {
  it('each countertop heading carries its own line, read from the new endpoint', async () => {
    vi.mocked(getCountertopItemsArchitect).mockResolvedValue({
      package_id: 'pkg', revision_id: 'rev',
      items: [
        status('item-a', 1, { reason: 'The architect prints only blocking here.' }),
        status('item-b', 2, { state: 'compared', pairs: [pair('piece', 'piece 2', 'cabinet_width')] }),
      ],
    });

    setup(ROWS);

    expect(await within(heading('item-a')).findByText('Architect: not compared — The architect prints only blocking here.')).toBeTruthy();
    expect(within(heading('item-b')).getByText('Architect: compared piece 2 (cabinet)')).toBeTruthy();
    expect(getCountertopItemsArchitect).toHaveBeenCalledWith('p', 'pkg');
  });

  it('nothing is asked when no countertop is confirmed, and a failed answer shows no line', async () => {
    vi.mocked(getCountertopItemsArchitect).mockClear();
    setup(ROWS.map((row) => ({ ...row, countertop_item: null })));
    expect(getCountertopItemsArchitect).not.toHaveBeenCalled();

    vi.mocked(getCountertopItemsArchitect).mockRejectedValue(new Error('offline'));
    setup(ROWS, 'pkg-2');
    await vi.waitFor(() => expect(getCountertopItemsArchitect).toHaveBeenCalledWith('p', 'pkg-2'));
    expect(screen.queryByText(/^Architect:/)).toBeNull();
  });
});
