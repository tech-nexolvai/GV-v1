import { describe, expect, it } from 'vitest';

import type { CountertopResults } from '@/api/client';
import { countertopLines, countertopNote, piecesLine, PIECES_NOT_READ } from '@/components/simple-review/recorded-numbers-lib';

// Synthetic values only: nothing here comes from a client drawing.
type Row = CountertopResults['items'][number];
const exact = (display: string) => ({ numerator: '0', denominator: '1', display });
const piece = (index: number, display: string | null, kind: string | null = null) => ({ index, kind, source: display ? ('sealed' as const) : ('missing' as const), value: display ? exact(display) : null });

function row(change: Partial<Row> = {}): Row {
  return {
    finding_id: 'find-1',
    row_id: 'row-1',
    page_number: 2,
    label: 'Sample row',
    printed_overall: exact('48"'),
    pieces: [piece(0, '3/4"'), piece(1, '46 1/2"'), piece(2, '3/4"')],
    pieces_add_up_to: exact('48"'),
    field_cut_per_end: null,
    field_cut_count: null,
    expected_total: exact('48"'),
    delta: exact('0"'),
    hold: null,
    wall_cases: { cases: [] },
    ...change,
  } as unknown as Row;
}

describe('the recorded pieces line (#1399)', () => {
  it('lists the recorded pieces and the recorded total, never a sum of its own', () => {
    expect(piecesLine(row())).toEqual({ label: 'Pieces', value: '3/4" + 46 1/2" + 3/4" = 48"' });
    // A recorded total that is not the pieces' sum is shown as recorded: nothing is added up here.
    expect(piecesLine(row({ pieces_add_up_to: exact('50"') })).value).toBe('3/4" + 46 1/2" + 3/4" = 50"');
  });

  it('without a recorded total, lists the pieces with no "=" and says the total is missing', () => {
    const held = row({ pieces_add_up_to: null });
    expect(piecesLine(held).value).toBe('3/4" + 46 1/2" + 3/4"');
    expect(countertopNote(held)).toBe('The check recorded no total for the pieces.');
  });

  it('says a piece that was not read in its place, and pieces not read one by one plainly', () => {
    expect(piecesLine(row({ pieces: [piece(0, '3/4"'), piece(1, null)], pieces_add_up_to: null })).value).toBe('3/4" + not read');
    expect(piecesLine(row({ pieces: [piece(0, null), piece(1, null)], pieces_add_up_to: null }))).toEqual({ label: 'Pieces', value: null, missing: PIECES_NOT_READ });
    expect(piecesLine(row({ pieces: [], pieces_add_up_to: null }))).toEqual({ label: 'Pieces', value: null, missing: 'none recorded' });
  });

  it('on a held row, a missing value is "not recorded": it may have been read, but the check recorded none', () => {
    const held = row({ hold: { code: 'x', reason: 'Sample reason.' }, printed_overall: null, pieces: [piece(0, null), piece(1, null)], pieces_add_up_to: null } as Partial<Row>);
    expect(countertopLines(held).slice(0, 2)).toEqual([
      { label: 'The vendor wrote', value: null, missing: 'not recorded' },
      { label: 'Pieces', value: null, missing: 'not recorded' },
    ]);
  });
});

describe('the recorded countertop lines (#1399)', () => {
  it('names the pieces\' kinds only when a reader named one', () => {
    expect(countertopLines(row({ pieces: [piece(0, '3/4"', 'unknown'), piece(1, '46 1/2"', 'unknown')] })).map((line) => line.label)).not.toContain('Pieces are');
    expect(countertopLines(row({ pieces: [piece(0, '3/4"', 'filler'), piece(1, '46 1/2"', 'unknown')] }))[2]).toEqual({ label: 'Pieces are', value: 'filler, not named' });
  });

  it('shows the vendor overall, pieces, need and difference as recorded', () => {
    expect(countertopLines(row())).toEqual([
      { label: 'The vendor wrote', value: '48" (overall)', missing: 'not read' },
      { label: 'Pieces', value: '3/4" + 46 1/2" + 3/4" = 48"' },
      { label: 'Needed overall', value: '48"' },
      { label: 'Difference', value: '0"' },
    ]);
    expect(countertopNote(row())).toBeNull();
  });

  it('checked for every wall layout: each layout\'s need and the closest difference, as recorded', () => {
    const lines = countertopLines(
      row({
        expected_total: null,
        delta: null,
        wall_cases: { cases: [{ layout: 'island', words: 'no walls', expected_total: exact('48"') }, { layout: 'back_left_right', words: 'walls at both ends', expected_total: exact('50"') }], closest_gap: exact('1/2"') } as unknown as Row['wall_cases'],
      }),
    );
    expect(lines.slice(2)).toEqual([
      { label: 'Needed if no walls', value: '48"' },
      { label: 'Needed if walls at both ends', value: '50"' },
      { label: 'Closest difference', value: '1/2", whatever the walls' },
    ]);
  });

  it('a held row or one never checked says why nothing is recorded', () => {
    expect(countertopNote(row({ hold: { code: 'x', reason: 'Sample reason.' } } as Partial<Row>))).toBe('The check held this countertop, so it recorded no numbers to compare. Read them on the drawing.');
    expect(countertopNote(row({ finding_id: null }))).toBe('The checks have not run on this countertop yet, so nothing is recorded.');
  });
});
