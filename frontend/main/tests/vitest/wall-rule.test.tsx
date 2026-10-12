// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { CountertopResult } from '@/api/client';
import { WallRuleInfo, WallsLine } from '@/components/results/wall-rule-info';
import { choiceResult, fieldCutOf, fieldCutRuleLines, fieldCutRuleSentence, fieldCutValueLine, workedExample, type WallCases } from '@/lib/wall-rule';

// Synthetic data only (#1202): pieces 94", the vendor wrote 95", a 1" field cut.
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
const CASES: WallCases = {
  cases: [
    { layout: 'back_left_right', words: 'walls at both ends', field_cut_count: 2, field_cut_total: x('2', '2"'), expected_total: x('96', '96"'), outcome: 'FAIL' },
    { layout: 'back_and_left', words: 'wall at the left end', field_cut_count: 1, field_cut_total: x('1', '1"'), expected_total: x('95', '95"'), outcome: 'PASS' },
    { layout: 'back_and_right', words: 'wall at the right end', field_cut_count: 1, field_cut_total: x('1', '1"'), expected_total: x('95', '95"'), outcome: 'PASS' },
    { layout: 'back_only', words: 'back wall only', field_cut_count: 0, field_cut_total: x('0', '0"'), expected_total: x('94', '94"'), outcome: 'FAIL' },
    { layout: 'island', words: 'island', field_cut_count: 0, field_cut_total: x('0', '0"'), expected_total: x('94', '94"'), outcome: 'FAIL' },
  ],
  vendor_wrote: x('95', '95"'),
  pieces_add_up_to: x('94', '94"'),
  field_cut_per_end: x('1', '1"'),
  unavailable_reason: null,
};

function result(layout: CountertopResult['wall_layout'], overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: 'f', row_id: 'r', page_number: 1, label: 'Synthetic countertop', row_location: null,
    outcome: 'FAIL', needs_decision: true, printed_overall: x('99', '99"'), pieces: [],
    field_cut_per_end: null, field_cut_count: null, expected_total: null, delta: null, hold: null,
    reviewer_decision: null, wall_layout: layout,
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
    ...overrides,
  };
}

describe('the field-cut rule in plain words (#1202)', () => {
  it('says the rule, its three cases and the project value, with no sum the check did not record', () => {
    expect(fieldCutRuleSentence('1"')).toBe("GV's field-cut rule: where the stone meets a wall, it is made 1\" longer and trimmed on site to fit.");
    expect(fieldCutRuleLines('1"', CASES)).toEqual([
      { walls: 'Walls at both ends', adds: '+1" + 1" = +2"' },
      { walls: 'Wall at one end', adds: '+1"' },
      { walls: 'No end walls (back wall only, island, or the stone stops at side panels)', adds: 'no field cut' },
    ]);
    // Without a recorded total the parts are shown, never added up here.
    expect(fieldCutRuleLines('3/4"')[0].adds).toBe('+3/4" + 3/4"');
    expect(fieldCutValueLine('1"')).toBe('1" is this project\'s field cut now (it can be changed per project).');
    expect(fieldCutRuleSentence(null)).toContain('longer by the field cut');
  });

  it('works the example with the row\'s own sealed numbers, only when the check recorded every case', () => {
    expect(workedExample(CASES)).toBe('Pieces add up to 94". Both ends → should be 96"; one end → 95"; no end walls → 94". Vendor wrote 95".');
    expect(workedExample({ cases: [], unavailable_reason: 'Run the checks.' })).toBeNull();
    expect(workedExample(null)).toBeNull();
  });

  it('gives each choice the recorded result, or none', () => {
    expect(choiceResult(CASES, 'back_and_right')).toEqual({ needed: '95"', outcome: 'PASS' });
    expect(choiceResult(CASES, 'other')).toBeNull();
    expect(choiceResult(null, 'island')).toBeNull();
  });

  it('opens from a keyboard-reachable "i" with an accessible name', async () => {
    const user = userEvent.setup();
    render(<WallRuleInfo fieldCut={'1"'} cases={CASES} />);
    const button = screen.getByRole('button', { name: "About GV's field-cut rule" });
    await user.tab();
    expect(document.activeElement).toBe(button);
    expect(button.textContent).toBe('i');
    await user.keyboard('{Enter}');
    expect(await screen.findByText(/it is made 1" longer and trimmed on site to fit/)).toBeTruthy();
    expect(screen.getByRole('rowheader', { name: 'Walls at both ends' })).toBeTruthy();
    expect(screen.getByText('+1" + 1" = +2"')).toBeTruthy();
    expect(screen.getByText('1" is this project\'s field cut now (it can be changed per project).')).toBeTruthy();
    expect(document.querySelector('[data-slot="wall-rule-example"]')?.textContent).toContain('Vendor wrote 95".');
  });
});

describe('the results line says which walls were used and who decided (#1202)', () => {
  it.each([
    [{ config: 'back_left_right', label: 'back wall and both ends', source: 'code ink and both AIs' as const, line: 'walls at both ends — seen on the drawing at both ends and confirmed by both AIs' }, 'Walls: walls at both ends — seen on the drawing at both ends and confirmed by both AIs'],
    [{ config: 'back_and_left', label: 'back wall and left end', source: 'reviewer' as const, line: 'wall at the left end — chosen by reviewer@example.com' }, 'Walls: wall at the left end — chosen by reviewer@example.com'],
    [{ config: null, label: null, source: 'not needed' as const, line: 'not needed: wrong for every wall case' }, 'Walls: not needed: wrong for every wall case'],
    [{ config: null, label: null, source: 'not established' as const }, 'Walls: not set yet'],
  ])('%o', (layout, words) => {
    render(<WallsLine row={result(layout, { wall_cases: CASES })} />);
    const line = document.querySelector('[data-slot="walls-line"]');
    expect(line?.textContent?.replace(/i$/, '')).toBe(words);
    expect(screen.getByRole('button', { name: "About GV's field-cut rule" })).toBeTruthy();
  });

  it('explains with the project\'s field cut now, else the one the result used (#1202 review)', () => {
    // A changed field cut: the "i" shows the current value, not the result's old one.
    expect(fieldCutOf(result({ config: null, label: null, source: 'not established' }, { field_cut_per_end: x('1', '1"'), field_cut_in_force: '3/4"' }))).toBe('3/4"');
    expect(fieldCutOf(result({ config: null, label: null, source: 'not established' }, { field_cut_per_end: x('1', '1"') }))).toBe('1"');
    expect(fieldCutOf(result({ config: null, label: null, source: 'not established' }, { wall_cases: CASES }))).toBe('1"');
    expect(fieldCutOf(result({ config: null, label: null, source: 'not established' }, { field_cut_in_force: '3/4"' }))).toBe('3/4"');
    expect(fieldCutOf(result({ config: null, label: null, source: 'not established' }))).toBeNull();
  });
});
