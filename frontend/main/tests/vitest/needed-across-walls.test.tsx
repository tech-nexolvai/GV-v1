// @vitest-environment jsdom
/**
 * The results table says what was needed and how far off before the walls are set (#1329).
 *
 * Synthetic data only: pieces add up to 40 1/4", GV's field cut is 1", so the walls need
 * 40 1/4", 41 1/4" or 42 1/4". Every number on screen must be one the check recorded.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { CountertopResult } from '@/api/client';
import type { Outcome } from '@/data/types';
import { CountertopTable, type RowActions } from '@/components/results/countertop-table';
import { neededAcrossWalls, type WallCase, type WallCases } from '@/lib/wall-rule';

const v = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });

const NEEDED = {
  both: v('169', '4', '42 1/4"'),
  one: v('165', '4', '41 1/4"'),
  none: v('161', '4', '40 1/4"'),
};

function wallCase(layout: string, words: string, needed: ReturnType<typeof v>, outcome: Outcome, difference: ReturnType<typeof v>): WallCase {
  return { layout, words, field_cut_count: null, field_cut_total: null, expected_total: needed, outcome, difference };
}

/** The five cases as the API sends them, with each case's recorded difference (printed − needed). */
function cases(
  outcomes: [Outcome, Outcome, Outcome],
  differences: [ReturnType<typeof v>, ReturnType<typeof v>, ReturnType<typeof v>],
  extra: Partial<WallCases>,
): WallCases {
  return {
    cases: [
      wallCase('back_left_right', 'walls at both ends', NEEDED.both, outcomes[0], differences[0]),
      wallCase('back_and_left', 'wall at the left end', NEEDED.one, outcomes[1], differences[1]),
      wallCase('back_and_right', 'wall at the right end', NEEDED.one, outcomes[1], differences[1]),
      wallCase('back_only', 'back wall only', NEEDED.none, outcomes[2], differences[2]),
      wallCase('island', 'island', NEEDED.none, outcomes[2], differences[2]),
    ],
    vendor_wrote: null,
    pieces_add_up_to: v('161', '4', '40 1/4"'),
    field_cut_per_end: v('1', '1', '1"'),
    needed_least: NEEDED.none,
    needed_most: NEEDED.both,
    unavailable_reason: null,
    ...extra,
  };
}

// The vendor wrote 40": short of every case by 2 1/4", 1 1/4" and 1/4".
const SHORT = cases(['FAIL', 'FAIL', 'FAIL'], [v('-9', '4', '-2 1/4"'), v('-5', '4', '-1 1/4"'), v('-1', '4', '-1/4"')], {
  vendor_wrote: v('40', '1', '40"'),
  closest_gap: v('1', '4', '1/4"'),
});
// The vendor wrote 45": over every case.
const OVER = cases(['FAIL', 'FAIL', 'FAIL'], [v('11', '4', '2 3/4"'), v('15', '4', '3 3/4"'), v('19', '4', '4 3/4"')], {
  vendor_wrote: v('45', '1', '45"'),
  closest_gap: v('11', '4', '2 3/4"'),
});
// The vendor wrote 41 3/4": between the cases, over some and short of one.
const BETWEEN = cases(['FAIL', 'FAIL', 'FAIL'], [v('-1', '2', '-1/2"'), v('1', '2', '1/2"'), v('3', '2', '1 1/2"')], {
  vendor_wrote: v('167', '4', '41 3/4"'),
  closest_gap: v('1', '2', '1/2"'),
});
// The vendor wrote 41 1/4": right with a wall at one end, wrong otherwise.
const MIXED = cases(['FAIL', 'PASS', 'FAIL'], [v('-1', '1', '-1"'), v('0', '1', '0"'), v('1', '1', '1"')], {
  vendor_wrote: NEEDED.one,
  closest_gap: v('0', '1', '0"'),
});

function row(id: string, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: 3, label: `Synthetic countertop ${id}`,
    row_location: null, outcome: 'FAIL', needs_decision: true,
    printed_overall: v('40', '1', '40"'),
    pieces: [{ index: 0, value: v('161', '4', '40 1/4"'), kind: 'cabinet', source: 'sealed' }],
    field_cut_per_end: v('1', '1', '1"'), field_cut_count: null, expected_total: null, delta: null,
    hold: null, reviewer_decision: null,
    wall_layout: { config: null, label: null, source: 'not needed', line: 'not needed: wrong for every wall case' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [true] },
    ...overrides,
  };
}

const ACTIONS: RowActions = { onShowDrawing: () => {}, onOpenCard: () => {}, onDecide: () => {}, canDecide: () => false };
const cells = (id: string) => document.querySelectorAll<HTMLElement>(`[data-slot="countertop-table"] tr[data-row-id="${id}"] td`);
const needed = (id: string) => cells(id)[5].textContent;
const difference = (id: string) => cells(id)[6].textContent;
const phoneCard = () => within(screen.getByRole('list', { name: 'Countertops' })).getAllByRole('listitem')[0];

describe('wrong for every wall case, with the walls not set (#1329)', () => {
  it('shows the range the cases need and the smallest gap, short', () => {
    render(<CountertopTable rows={[row('a', { wall_cases: SHORT })]} actions={ACTIONS} />);
    expect(needed('a')).toBe('40 1/4" – 42 1/4"');
    expect(difference('a')).toBe('at least 1/4" short');
    // A failing difference looks like any other failing difference.
    expect(cells('a')[6].querySelector('[data-slot="difference-across-walls"]')?.className).toContain('text-outcome-fail-fg');
    // The phone card says the same.
    expect(phoneCard().textContent).toContain('40 1/4" – 42 1/4"');
    expect(phoneCard().textContent).toContain('at least 1/4" short');
  });

  it('says over when the vendor wrote more than every case needs', () => {
    render(<CountertopTable rows={[row('a', { printed_overall: v('45', '1', '45"'), wall_cases: OVER })]} actions={ACTIONS} />);
    expect(needed('a')).toBe('40 1/4" – 42 1/4"');
    expect(difference('a')).toBe('at least 2 3/4" over');
  });

  it('gives no one direction when the vendor wrote a value between the cases', () => {
    render(<CountertopTable rows={[row('a', { wall_cases: BETWEEN })]} actions={ACTIONS} />);
    expect(difference('a')).toBe('at least 1/2" off');
  });

  it('shows one value, without "at least", when every case needs the same overall', () => {
    const same = cases(['FAIL', 'FAIL', 'FAIL'], [v('-1', '4', '-1/4"'), v('-1', '4', '-1/4"'), v('-1', '4', '-1/4"')], {
      cases: SHORT.cases!.map((item) => ({ ...item, expected_total: NEEDED.none, difference: v('-1', '4', '-1/4"') })),
      needed_most: NEEDED.none,
      vendor_wrote: v('40', '1', '40"'),
      closest_gap: v('1', '4', '1/4"'),
    });
    render(<CountertopTable rows={[row('a', { wall_cases: same })]} actions={ACTIONS} />);
    expect(needed('a')).toBe('40 1/4"');
    expect(difference('a')).toBe('1/4" short');
  });

  it('shows exactly the numbers the check recorded, never its own', () => {
    // Recorded numbers that do not add up from the pieces: the screen must still show them as sent.
    const recorded = { ...SHORT, needed_least: v('7', '8', 'RECORDED-LEAST"'), needed_most: v('9', '8', 'RECORDED-MOST"'), closest_gap: v('1', '8', 'RECORDED-GAP"') };
    render(<CountertopTable rows={[row('a', { wall_cases: recorded })]} actions={ACTIONS} />);
    expect(needed('a')).toBe('RECORDED-LEAST" – RECORDED-MOST"');
    expect(difference('a')).toBe('at least RECORDED-GAP" short');
  });

  it('sorts by the least the cases need', () => {
    const across = neededAcrossWalls(row('a', { wall_cases: SHORT }));
    expect(across?.kind === 'same' && across.least).toEqual(NEEDED.none);
  });
});

describe('the wall cases disagree, with the walls not set (#1329)', () => {
  const mixed = () => row('m', { outcome: 'REVIEW_REQUIRED', printed_overall: NEEDED.one, wall_layout: { config: null, label: null, source: 'not established' }, wall_cases: MIXED });

  it('keeps saying the walls are needed and lists each case\'s needed overall', async () => {
    const user = userEvent.setup();
    render(<CountertopTable rows={[mixed()]} actions={ACTIONS} />);
    expect(needed('m')).toBe('42 1/4" / 41 1/4" / 40 1/4"');
    expect(difference('m')).toBe('walls needed');
    // The expanded row names each case with what it needs and its recorded result.
    await user.click(within(cells('m')[0]).getByRole('button', { name: 'Show details' }));
    const list = document.querySelector('[data-slot="countertop-table"] [data-slot="needed-per-wall-case"]');
    expect([...(list?.querySelectorAll('li') ?? [])].map((item) => item.textContent)).toEqual([
      'walls at both ends: 42 1/4" needed — Needs correction',
      'wall at the left end: 41 1/4" needed — Looks right',
      'wall at the right end: 41 1/4" needed — Looks right',
      'back wall only: 40 1/4" needed — Needs correction',
      'island: 40 1/4" needed — Needs correction',
    ]);
  });

  it('lists the values the check recorded for each case', () => {
    const across = neededAcrossWalls(mixed());
    expect(across?.kind).toBe('mixed');
    if (across?.kind !== 'mixed') return;
    expect(across.cases.map((item) => item.needed)).toEqual(MIXED.cases!.map((item) => item.expected_total!.display));
  });
});

describe('unchanged rows (#1329)', () => {
  it('a row whose walls are set shows its one Needed and Difference', () => {
    const set = row('s', {
      expected_total: NEEDED.one, delta: v('-5', '4', '-1 1/4"'), field_cut_count: 1,
      wall_layout: { config: 'back_and_left', label: 'Back + left', source: 'reviewer', line: 'wall at the left end — chosen by a synthetic reviewer' },
      wall_cases: { cases: [], unavailable_reason: null },
    });
    render(<CountertopTable rows={[set]} actions={ACTIONS} />);
    expect(needed('s')).toBe('41 1/4"');
    expect(difference('s')).toBe('−1 1/4"');
  });

  it('walls chosen after an every-case run still show the table\'s usual "—" until the checks run', () => {
    const chosen = row('c', { wall_layout: { config: 'back_only', label: 'Back only', source: 'reviewer', line: 'back wall only — chosen by a synthetic reviewer' }, wall_cases: SHORT });
    expect(neededAcrossWalls(chosen)).toBeNull();
  });

  it.each([
    ['held, with no wall cases', row('h', { outcome: null, finding_id: null, hold: { code: 'row-held', reason: 'A synthetic hold.' }, wall_layout: { config: null, label: null, source: 'not established' }, wall_cases: { cases: [], unavailable_reason: 'Run the checks to see each choice\'s result.' } })],
    ['field cut changed since the run', row('h', { wall_cases: { cases: [], unavailable_reason: 'The project\'s field cut changed since the last check run.' } })],
    ['no wall cases at all', row('h')],
  ])('%s: "—"', (_name, held) => {
    render(<CountertopTable rows={[held]} actions={ACTIONS} />);
    expect(needed('h')).toBe('—');
    expect(difference('h')).toBe('—');
  });
});
