// @vitest-environment jsdom
import { render, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { getCountertopItemsArchitect, type ArchitectResult, type CountertopItemArchitect, type CountertopResult } from '@/api/client';
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
const entry = (itemId: string, page: number, extra: Partial<CountertopItemArchitect> = {}): CountertopItemArchitect => ({
  countertop: item(itemId, page), row_id: null, state: 'not_compared', reason: null, finding_id: null, outcome: null,
  pairs: [], pairing_source: null, pairing_judgments: null, ...extra,
});
const piece = (vendorPiece: number | null, type: string | null = null) => ({
  kind: vendorPiece === null ? ('overall' as const) : ('piece' as const), label: vendorPiece === null ? 'overall' : `piece ${vendorPiece}`,
  vendor_piece: vendorPiece, piece_item_id: type ? `piece-${vendorPiece}` : null, piece_item_type: type,
});
const compared = (vendorPiece: number | null): ArchitectResult['compared'][number] => ({
  kind: vendorPiece === null ? 'overall' : 'piece', vendor_piece: vendorPiece, vendor: null, architect: null, delta: null,
  vendor_display: null, architect_display: null, delta_display: null, outcome: null,
});
const NOT_CHECKED = "Not checked yet: run the checks to compare this row with the architect's drawing.";
const block = (extra: Partial<ArchitectResult>): ArchitectResult => ({ compared: [], needs_decision: false, ...extra });

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

describe("the words of the architect line: always its row's own result (#1273, #1279 review)", () => {
  it('a row with no result says what the row says: not checked yet, or not compared and why', () => {
    expect(itemArchitectWords(block({ not_compared_reason: NOT_CHECKED }))).toBe(`Architect: ${NOT_CHECKED}`);
    const nothing = block({ not_compared_reason: 'The architect prints only blocking here.' });
    expect(itemArchitectWords(nothing)).toBe('Architect: not compared — The architect prints only blocking here.');
    expect(itemArchitectWords(nothing, [], 'The architect prints only blocking here.')).toBe('Architect: not compared (the reason is above the table)');
    expect(itemArchitectWords(nothing, [], 'Another reason.')).toBe('Architect: not compared — The architect prints only blocking here.');
    expect(itemArchitectWords(null)).toBeNull();
    expect(itemArchitectWords(block({}))).toBeNull();
  });

  it('a recorded PASS or FAIL is compared, naming each confirmed piece from the endpoint', () => {
    const fail = block({ finding_id: 'f', outcome: 'FAIL', needs_decision: true, pairing_source: 'code+ais', compared: [compared(null), compared(2), compared(3)] });
    expect(itemArchitectWords(fail, [piece(null), piece(2, 'cabinet_width'), piece(3)])).toBe(
      'Architect: compared the overall, piece 2 (cabinet) and piece 3 — Needs correction',
    );
    // With no piece names (the endpoint failed) the line still stands, from the row alone.
    expect(itemArchitectWords({ ...fail, outcome: 'PASS', needs_decision: false })).toBe('Architect: compared the overall, piece 2 and piece 3 — Looks right');
  });

  it('a recorded result that waits for the reviewer never says compared', () => {
    // One judgment: confirm the pairing.
    expect(itemArchitectWords(block({ finding_id: 'f', outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'code', compared: [compared(null)] }))).toBe(
      'Architect: waits for you — Confirm the pairing: only code matched these',
    );
    // Two judgments, but a value the check may not use: the check's own reason.
    expect(itemArchitectWords(block({ finding_id: 'f', outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'code+ais', reason: 'The value is in millimetres, not inches.' }))).toBe(
      'Architect: waits for you — The value is in millimetres, not inches.',
    );
    // Nobody paired it.
    expect(itemArchitectWords(block({ finding_id: 'f', outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'none' }))).toBe(
      "Architect: waits for you — pair the architect's dimension with the vendor's",
    );
    // Decided by the reviewer: said as recorded, not as waiting.
    expect(itemArchitectWords(block({ finding_id: 'f', outcome: 'REVIEW_REQUIRED', needs_decision: false, pairing_source: 'code+ais' }))).toBe(
      'Architect: Needs your decision',
    );
  });
});

describe('the architect line under each confirmed countertop (#1273)', () => {
  it("each countertop heading carries its row's line, with the confirmed piece named by the endpoint", async () => {
    vi.mocked(getCountertopItemsArchitect).mockResolvedValue({
      package_id: 'pkg', revision_id: 'rev',
      items: [
        // The endpoint's own state is not what the line says: the row's result is.
        entry('item-a', 1, { state: 'compared' }),
        entry('item-b', 2, { state: 'compared', pairs: [piece(2, 'cabinet_width')] }),
      ],
    });
    const rows: CountertopResult[] = [
      { ...ROWS[0], architect: block({ not_compared_reason: NOT_CHECKED }) },
      { ...ROWS[1], architect: block({ finding_id: 'f-b', outcome: 'FAIL', compared: [compared(2)] }) },
    ];

    setup(rows);

    expect(within(heading('item-a')).getByText(`Architect: ${NOT_CHECKED}`)).toBeTruthy();
    expect(await within(heading('item-b')).findByText('Architect: compared piece 2 (cabinet) — Needs correction')).toBeTruthy();
    expect(getCountertopItemsArchitect).toHaveBeenCalledWith('p', 'pkg');
  });

  it('nothing is asked when no countertop is confirmed, and a failed answer only loses the piece kinds', async () => {
    vi.mocked(getCountertopItemsArchitect).mockClear();
    setup(ROWS.map((row) => ({ ...row, countertop_item: null })));
    expect(getCountertopItemsArchitect).not.toHaveBeenCalled();

    vi.mocked(getCountertopItemsArchitect).mockRejectedValue(new Error('offline'));
    setup([{ ...ROWS[0], architect: block({ finding_id: 'f', outcome: 'PASS', compared: [compared(1)] }) }], 'pkg-2');
    await vi.waitFor(() => expect(getCountertopItemsArchitect).toHaveBeenCalledWith('p', 'pkg-2'));
    expect(within(heading('item-a')).getByText('Architect: compared piece 1 — Looks right')).toBeTruthy();
  });
});
