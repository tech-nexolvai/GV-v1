// @vitest-environment jsdom
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { CountertopResult } from '@/api/client';
import { inchesOf, stripLayout } from '@/lib/countertop-strip';
import { CountertopStrip } from '@/components/results/CountertopStrip';

// Synthetic rows only: nothing here comes from a client drawing.
const x = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });
const piece = (index: number, value: ReturnType<typeof x> | null, kind: string | null = 'cabinet', source: 'sealed' | 'typed' | 'missing' = value ? 'sealed' : 'missing') =>
  ({ index, value, kind, source });
const base: CountertopResult = {
  finding_id: 'f', row_id: 'r', page_number: 1, label: 'Synthetic countertop', row_location: null,
  outcome: 'FAIL', needs_decision: false,
  printed_overall: x('42', '1', '42"'),
  pieces: [piece(0, x('2', '1', '2"'), 'filler'), piece(1, x('105', '8', '13 1/8"')), piece(2, x('87', '4', '21 3/4"')), piece(3, x('2', '1', '2"'), 'filler')],
  field_cut_per_end: x('1', '1', '1"'), field_cut_count: 2,
  expected_total: x('331', '8', '41 3/8"'), delta: x('5', '8', '5/8"'),
  hold: null, reviewer_decision: null,
  wall_layout: { config: 'back_left_right', label: 'back wall and both ends', source: 'drawing clues' },
  agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true, true, true, true, true] },
};
const PASS: CountertopResult = { ...base, outcome: 'PASS', printed_overall: x('331', '8', '41 3/8"'), delta: x('0', '1', '0"') };
const SHORT: CountertopResult = { ...base, printed_overall: x('40', '1', '40"'), delta: x('-11', '8', '-1 3/8"') };
const OVER: CountertopResult = base;
const HELD: CountertopResult = { ...base, outcome: 'REVIEW_REQUIRED', needs_decision: true, expected_total: null, delta: null, hold: { code: 'stone-into-walls', reason: 'The stone runs into wall pockets.' } };
const PANELS: CountertopResult = {
  ...base, outcome: 'PASS', printed_overall: x('55', '1', '55"'),
  pieces: [piece(0, x('5', '1', '5"'), 'filler'), piece(1, x('45', '1', '45"')), piece(2, x('5', '1', '5"'), 'filler')],
  field_cut_count: 0, expected_total: x('55', '1', '55"'), delta: x('0', '1', '0"'),
  wall_layout: { config: 'back_only', label: 'back wall only; no field cut at the ends', source: 'between panels' },
};
const MISSING: CountertopResult = {
  ...base, outcome: 'REVIEW_REQUIRED', needs_decision: true, printed_overall: null, expected_total: null, delta: null,
  pieces: [piece(0, null), piece(1, x('36', '1', '36"')), piece(2, null)],
  hold: { code: 'row-incomplete', reason: 'This row is incomplete.' },
};

const drawn = (row: CountertopResult) => {
  const layout = stripLayout(row, 1000);
  if (layout.status !== 'drawn') throw new Error(`expected a drawing, got: ${layout.reason}`);
  return layout;
};

describe('countertop picture: layout', () => {
  it('draws pieces in proportion to their exact widths', () => {
    const layout = drawn(OVER);
    const [filler, a, b] = layout.pieces;
    expect(a.w / b.w).toBeCloseTo((105 / 8) / (87 / 4), 6);
    expect(a.w / filler.w).toBeCloseTo((105 / 8) / 2, 6);
    // Pieces sit side by side, after the left field-cut cap.
    expect(a.x).toBeCloseTo(filler.x + filler.w, 6);
    expect(filler.x).toBeCloseTo(layout.caps.left!.w, 6);
  });

  it('labels every value with the exact text from the API, never a computed number', () => {
    const layout = drawn(OVER);
    expect(layout.pieces.map((p) => p.label)).toEqual(['2"', '13 1/8"', '21 3/4"', '2"']);
    expect(layout.printed?.label).toBe('42"');
    expect(layout.needed?.label).toBe('41 3/8"');
    expect(layout.caps.left?.label).toBe('+1"');
  });

  it('draws the printed and needed brackets to the same scale, from the wall face', () => {
    const short = drawn(SHORT);
    expect(short.printed!.x).toBe(0);
    expect(short.needed!.x).toBe(0);
    expect(short.printed!.w / short.needed!.w).toBeCloseTo(40 / (331 / 8), 6);
    const over = drawn(OVER);
    expect(over.printed!.w).toBeGreaterThan(over.needed!.w);
  });

  it('puts field-cut caps only at ends that have a wall', () => {
    expect(drawn(OVER).caps.left && drawn(OVER).caps.right).toBeTruthy();
    expect(drawn(PANELS).caps).toEqual({ left: null, right: null });
    expect(drawn({ ...OVER, wall_layout: { ...OVER.wall_layout, config: 'island' } }).caps).toEqual({ left: null, right: null });
    expect(drawn({ ...OVER, wall_layout: { config: null, label: null, source: 'not established' } }).walls).toBeNull();
  });

  it('a held row is overlaid and shows no difference', () => {
    const layout = drawn(HELD);
    expect(layout.held?.reason).toBe('The stone runs into wall pockets.');
    expect(layout.difference).toBeNull();
  });

  it('with any width missing it draws equal boxes and says it is not to scale', () => {
    const layout = drawn(MISSING);
    expect(layout.toScale).toBe(false);
    expect(new Set(layout.pieces.map((p) => Math.round(p.w))).size).toBe(1);
    expect(layout.pieces.map((p) => p.label)).toEqual([null, '36"', null]);
    expect(layout.summary).toMatch(/Not to scale/);
  });

  it('refuses to draw with no pieces or a width that is not an exact positive fraction', () => {
    expect(stripLayout({ ...OVER, pieces: [] }, 1000)).toEqual({ status: 'refused', reason: 'Pieces not read' });
    expect(stripLayout({ ...OVER, pieces: [piece(0, x('1.5', '1', '1.5"'))] }, 1000).status).toBe('refused');
    expect(stripLayout({ ...OVER, pieces: [piece(0, x('3', '0', '?'))] }, 1000).status).toBe('refused');
    expect(stripLayout({ ...OVER, pieces: [piece(0, x('-3', '1', '-3"'))] }, 1000).status).toBe('refused');
    expect(inchesOf(x('105', '8', '13 1/8"'))).toBe(13.125);
    expect(inchesOf(x('1e3', '1', 'x'))).toBeNull();
  });

  it('summarises the facts in words for screen readers', () => {
    expect(drawn(SHORT).summary).toBe('Printed 40", needed 41 3/8", short by 1 3/8"; 4 pieces 2", 13 1/8", 21 3/4", 2"; back wall and both ends.');
    expect(drawn(OVER).summary).toMatch(/over by 5\/8"/);
    expect(drawn(PASS).summary).toMatch(/no difference/);
  });
});

describe('countertop picture: drawing', () => {
  const states: [string, CountertopResult][] = [
    ['PASS', PASS], ['FAIL short', SHORT], ['FAIL over', OVER], ['held', HELD], ['between panels', PANELS], ['missing', MISSING],
  ];

  it.each(states)('%s', (_name, row) => {
    const { container } = render(<CountertopStrip row={row} />);
    expect(container.innerHTML).toMatchSnapshot();
  });

  it('pairs the difference colour with its glyph, and shows none for a held row', () => {
    const short = render(<CountertopStrip row={SHORT} />).container;
    const callout = short.querySelector('[data-slot="strip-difference"]');
    expect(callout?.textContent).toBe('−1 3/8"');
    expect(callout?.querySelector('[data-outcome-icon="FAIL"]')).not.toBeNull();
    const pass = render(<CountertopStrip row={PASS} />).container;
    expect(pass.querySelector('[data-slot="strip-difference"] [data-outcome-icon="PASS"]')).not.toBeNull();
    const held = render(<CountertopStrip row={HELD} />).container;
    expect(held.querySelector('[data-slot="strip-difference"]')).toBeNull();
    expect(held.querySelector('[data-held="true"]')).not.toBeNull();
  });

  it('missing pieces are dashed boxes marked "?"', () => {
    const { container } = render(<CountertopStrip row={MISSING} />);
    const missing = container.querySelectorAll('[data-piece][data-source="missing"]');
    expect(missing).toHaveLength(2);
    missing.forEach((g) => {
      expect(g.querySelector('rect')?.getAttribute('stroke-dasharray')).toBeTruthy();
      expect(g.textContent).toContain('?');
    });
    expect(container.textContent).toContain('Not to scale');
  });

  it('full size lets every piece be focused and explains it, including its source', () => {
    const { container } = render(<CountertopStrip row={OVER} size="full" />);
    const pieces = container.querySelectorAll('[data-piece]');
    expect(pieces[1].getAttribute('tabindex')).toBe('0');
    expect(pieces[1].querySelector('title')?.textContent).toBe('Cabinet 2: 13 1/8" — both AIs read it the same');
    expect(pieces[0].querySelector('title')?.textContent).toMatch(/^Filler 1/);
  });

  it('the hold reason can defer to the card around it', () => {
    const { container } = render(<CountertopStrip row={HELD} size="full" showHoldReason={false} />);
    const chip = container.querySelector('span[title="The stone runs into wall pockets."]');
    expect(chip?.textContent).toBe('Held');
    // The reason still reaches screen readers through the drawing's own title.
    expect(container.querySelector('svg title')?.textContent).toContain('wall pockets');
  });

  it('a picture that cannot be drawn says so in one line', () => {
    const { container } = render(<CountertopStrip row={{ ...OVER, pieces: [] }} />);
    expect(container.textContent).toBe(' No picture: pieces not read');
    expect(container.querySelector('svg[role="img"]')).toBeNull();
  });
});
