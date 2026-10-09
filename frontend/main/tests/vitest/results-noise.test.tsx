// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { ArchitectResult, CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { kpis, sharedNotComparedReason } from '@/lib/countertop-results';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';
import { WallGlyph } from '@/components/results/wall-glyph';

// The dashboard asks the rulebook for names; keep it off the network.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
}));

// Synthetic data only (#1126): nothing here comes from a client drawing.
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
const REASON = 'Synthetic: no architect dimension on this sheet measures a countertop or a cabinet run, so there is nothing to compare.';
const OTHER_REASON = 'Synthetic: the architect prints only centre lines on this page.';
const NOTE = 'Drawn length not checked (no scale): piece 2, the overall';
const notCompared = (reason: string): ArchitectResult => ({ outcome: null, finding_id: null, reason: null, needs_decision: false, compared: [], not_compared_reason: reason, pairing_source: null, pairing_judgments: null });
const comparedPass: ArchitectResult = {
  outcome: 'PASS', finding_id: 'arch-pass', reason: null, needs_decision: false, not_compared_reason: null,
  pairing_source: 'code+ais', pairing_judgments: 'code and both AIs',
  compared: [{ kind: 'overall', vendor_piece: null, vendor: x('88', '88"'), architect: x('88', '88"'), delta: x('0', '0"'), vendor_display: '88"', architect_display: '88"', delta_display: '0"', outcome: 'PASS', architect_location: null }],
};

function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`, row_location: null,
    outcome: 'PASS', needs_decision: false, printed_overall: x('88', '88"'),
    pieces: [{ index: 0, value: x('44', '44"'), source: 'sealed', kind: 'cabinet' }, { index: 1, value: x('44', '44"'), source: 'sealed', kind: 'cabinet' }],
    field_cut_per_end: x('0', '0"'), field_cut_count: 0, expected_total: x('88', '88"'), delta: x('0', '0"'),
    hold: null, reviewer_decision: null, drawn_length_note: null,
    wall_layout: { config: 'back_only', label: 'back wall only', source: 'drawing clues' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true, true] },
    ...overrides,
  } as CountertopResult;
}
const finding = (id: string, outcome: Finding['outcome']): Finding =>
  ({ id, check_id: 'CT-WIDTH-001', name: 'Synthetic width', severity: 'FLAG', outcome, reviewer_action: null, created_at: '2026-10-09T10:00:00Z' }) as Finding;

function dashboard(rows: CountertopResult[], extra: Partial<Extract<CountertopsState, { status: 'ready' }>> = {}, filter: 'all' | null = 'all') {
  const findings = rows.flatMap((r) => (r.finding_id ? [finding(r.finding_id, r.outcome ?? 'REVIEW_REQUIRED')] : []));
  const blocking = new Set(rows.filter((r) => r.needs_decision && r.finding_id).map((r) => r.finding_id!));
  return render(
    <ResultsDashboard
      countertops={{ status: 'ready', rows, ...extra } as CountertopsState}
      findings={findings}
      blockingIds={blocking}
      busy={false}
      filter={filter}
      onFilterChange={() => {}}
      onRetry={() => {}}
      onRefresh={() => {}}
      handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
      onBulkDismiss={vi.fn(async () => ({ saved: 0, failed: [] }))}
      onShowDrawing={() => {}}
      onOpenCard={() => {}}
      onOpenQueue={vi.fn()}
    />,
  );
}

const table = () => document.querySelector('[data-slot="countertop-table"] table') as HTMLElement;
const phoneCards = () => [...document.querySelectorAll<HTMLLIElement>('[data-slot="countertop-table"] ul[aria-label="Countertops"] > li')];

describe('the architect notice (#1126)', () => {
  it('finds the one reason every countertop shares, and only then', () => {
    expect(sharedNotComparedReason([row('a', 1, { architect: notCompared(REASON) }), row('b', 2, { architect: notCompared(REASON) }), row('c', 3)])).toBe(REASON);
    expect(sharedNotComparedReason([row('a', 1, { architect: notCompared(REASON) }), row('b', 2, { architect: notCompared(OTHER_REASON) })])).toBeNull();
    expect(sharedNotComparedReason([row('a', 1, { architect: notCompared(REASON) }), row('b', 2, { architect: comparedPass })])).toBeNull();
    expect(sharedNotComparedReason([row('a', 1), row('b', 2)])).toBeNull(); // no architect result at all
  });

  it('every row not compared for the same reason: one notice above the table, no line under each row', async () => {
    const user = userEvent.setup();
    dashboard([row('a', 1, { architect: notCompared(REASON) }), row('b', 2, { architect: notCompared(REASON) }), row('c', 3, { architect: notCompared(REASON) })]);
    const notices = document.querySelectorAll('[data-slot="architect-notice"]');
    expect(notices).toHaveLength(1);
    const notice = notices[0] as HTMLElement;
    expect(notice.textContent).toContain('Matches the architect: not compared on any countertop');
    expect(table().querySelectorAll('tr[data-architect-row]')).toHaveLength(0);
    for (const card of phoneCards()) expect(card.querySelector('[data-slot="architect-line"]')).toBeNull();
    // The whole reason, once, behind "Why?".
    const why = within(notice).getByRole('button', { name: /Why\?/ });
    expect(why.getAttribute('aria-expanded')).toBe('false');
    expect(notice.querySelector('[data-slot="architect-notice-reason"]')!.hasAttribute('hidden')).toBe(true);
    await user.click(why);
    expect(why.getAttribute('aria-expanded')).toBe('true');
    expect(notice.querySelector('[data-slot="architect-notice-reason"]')!.hasAttribute('hidden')).toBe(false);
    expect(notice.querySelector('[data-slot="architect-notice-reason"]')!.textContent).toBe(REASON);
    expect(document.body.textContent!.split(REASON).length - 1).toBe(1);
    // A row's details still carry its own reason.
    const tr = table().querySelector<HTMLTableRowElement>('tr[data-row-id="a"]')!;
    await user.click(within(tr).getByRole('button', { name: 'Show details' }));
    expect((tr.nextElementSibling as HTMLElement).textContent).toContain(REASON);
  });

  it('reasons that differ, or any row compared, keep the per-row lines and no notice', () => {
    const { unmount } = dashboard([row('a', 1, { architect: notCompared(REASON) }), row('b', 2, { architect: notCompared(OTHER_REASON) })]);
    expect(document.querySelector('[data-slot="architect-notice"]')).toBeNull();
    expect(table().querySelector('tr[data-architect-row="a"]')!.textContent).toContain(REASON);
    expect(table().querySelector('tr[data-architect-row="b"]')!.textContent).toContain(OTHER_REASON);
    unmount();
    dashboard([row('a', 1, { architect: notCompared(REASON) }), row('b', 2, { architect: comparedPass })]);
    expect(document.querySelector('[data-slot="architect-notice"]')).toBeNull();
    expect(table().querySelectorAll('tr[data-architect-row]')).toHaveLength(2);
  });
});

describe('the drawn-length note (#1107)', () => {
  it('is on its row in the table, the phone card and the row details; a row without one shows nothing', async () => {
    const user = userEvent.setup();
    dashboard([row('noted', 1, { drawn_length_note: NOTE }), row('plain', 2)]);
    const noted = table().querySelector<HTMLTableRowElement>('tr[data-row-id="noted"]')!;
    const plain = table().querySelector<HTMLTableRowElement>('tr[data-row-id="plain"]')!;
    expect(noted.querySelector('[data-slot="drawn-length-note"]')?.textContent).toBe(NOTE);
    expect(plain.querySelector('[data-slot="drawn-length-note"]')).toBeNull();
    const [card, plainCard] = phoneCards();
    expect(card.querySelector('[data-slot="drawn-length-note"]')?.textContent).toBe(NOTE);
    expect(plainCard.querySelector('[data-slot="drawn-length-note"]')).toBeNull();
    await user.click(within(noted).getByRole('button', { name: 'Show details' }));
    expect((noted.nextElementSibling as HTMLElement).querySelector('[data-slot="drawn-length-note"]')?.textContent).toBe(NOTE);
    await user.click(within(plain).getByRole('button', { name: 'Show details' }));
    expect((plain.nextElementSibling as HTMLElement).querySelector('[data-slot="drawn-length-note"]')).toBeNull();
  });
});

describe('countertop lines not checked (#1108)', () => {
  const NOT_CHECKED = [
    { page_number: 4, reason: 'Synthetic: an AI named a second countertop line on this page; one line per page is read.' },
    { page_number: 9, reason: 'Synthetic: a second countertop line on this page was not read.' },
  ];

  it('lists page and reason beside the pages with no countertop, with nothing to click', () => {
    dashboard([row('a', 1)], { rowsNotChecked: NOT_CHECKED, pagesWithoutCountertop: [{ page_number: 15, reason: 'Synthetic: tall units only.' }] });
    const list = screen.getByRole('region', { name: 'Countertop lines not checked' });
    const items = within(list).getAllByRole('listitem');
    expect(items.map((li) => li.textContent)).toEqual([`Page 4${NOT_CHECKED[0].reason}`, `Page 9${NOT_CHECKED[1].reason}`]);
    expect(within(list).queryByRole('button')).toBeNull();
    expect(list.textContent).toContain('Listed only. Nothing to decide.');
    // Next to the existing list, in the same place.
    const both = document.querySelector('[data-slot="listed-pages"]')!;
    expect(both.contains(screen.getByRole('region', { name: 'Pages with no countertop found' }))).toBe(true);
    expect(both.contains(list)).toBe(true);
  });

  it('never blocks: the counts and what needs you are the same with or without it', () => {
    const rows = [row('a', 1), row('b', 2, { outcome: 'REVIEW_REQUIRED', needs_decision: true })];
    dashboard(rows, { rowsNotChecked: NOT_CHECKED });
    const before = kpis(rows);
    const cards = document.querySelector('[data-slot="kpi-cards"]')!;
    expect(cards.textContent).toMatch(new RegExp(`Needs you\\s*${before.needsYou}`));
    expect(cards.textContent).toMatch(new RegExp(`Countertops\\s*${before.countertops}`));
  });

  it('is absent when the list is empty or the API sent none', () => {
    const { unmount } = dashboard([row('a', 1)], { rowsNotChecked: [] });
    expect(screen.queryByRole('region', { name: 'Countertop lines not checked' })).toBeNull();
    unmount();
    dashboard([row('a', 1)]);
    expect(screen.queryByRole('region', { name: 'Countertop lines not checked' })).toBeNull();
    expect(document.querySelector('[data-slot="listed-pages"]')).toBeNull();
  });
});

describe('the walls, in words (#1126)', () => {
  it('a layout nobody has set says "Walls not set", never a lone "?"', () => {
    render(<WallGlyph layout={{ config: null, label: null, source: 'not established' }} />);
    const glyph = document.querySelector('[data-slot="wall-glyph"]')!;
    expect(glyph.textContent).toBe('Walls not set');
    expect(glyph.textContent).not.toContain('?');
    expect(glyph.querySelector('svg')).toBeNull();
  });

  it('beside a "Walls" label it says only "Not set"; a set layout keeps its picture and source', () => {
    const { unmount } = render(<WallGlyph labelled layout={{ config: null, label: null, source: 'not established' }} />);
    expect(document.querySelector('[data-slot="wall-glyph"]')!.textContent).toBe('Not set');
    unmount();
    render(<WallGlyph layout={{ config: 'back_left_right', label: 'back wall and both ends', source: 'reviewer' }} />);
    const glyph = document.querySelector('[data-slot="wall-glyph"]')!;
    expect(glyph.querySelector('svg[role="img"]')?.getAttribute('aria-label')).toContain('back wall and both ends');
    expect(glyph.textContent).toContain('reviewer');
  });

  it('in the table, under the "Walls" header, an unset row says "Not set"', () => {
    dashboard([row('a', 1, { wall_layout: { config: null, label: null, source: 'not established' } })]);
    const cell = table().querySelector('tr[data-row-id="a"]')!.querySelectorAll('td')[7];
    expect(cell.textContent).toBe('Not set');
  });

  it('a layout with no picture is named in words, never by its raw code', () => {
    const { unmount } = render(<WallGlyph layout={{ config: 'l_shape', label: null, source: 'reviewer' }} />);
    expect(document.querySelector('[data-slot="wall-glyph"]')!.textContent).toBe('Walls set (no picture)');
    unmount();
    render(<WallGlyph layout={{ config: 'l_shape', label: 'back wall and left end', source: 'reviewer' }} />);
    expect(document.querySelector('[data-slot="wall-glyph"]')!.textContent).toBe('Walls: back wall and left end (no picture)');
    expect(document.body.textContent).not.toContain('l_shape');
  });
});

describe('counts once (#1126)', () => {
  const rows = [
    row('pass', 1),
    row('fail', 2, { outcome: 'FAIL', delta: x('-2', '-2"') }),
    row('held', 3, { outcome: 'REVIEW_REQUIRED', needs_decision: true, delta: null, hold: { code: 'row-held', reason: 'Synthetic hold.' } }),
    row('dec', 4, { outcome: 'NOT_FOUND', needs_decision: false, delta: null, reviewer_decision: { action: 'dismiss', actor: 'Sam Reviewer', note: 'synthetic', time: '2026-10-09T10:00:00Z' } }),
  ];

  it('the KPI words are never cut short, and sit three then two on a phone', () => {
    dashboard(rows);
    const cards = document.querySelector('[data-slot="kpi-cards"]')!;
    expect(cards.querySelectorAll('.truncate')).toHaveLength(0);
    expect([...cards.querySelectorAll('button')].map((b) => [...b.classList].find((c) => c.startsWith('col-span-')))).toEqual(['col-span-2', 'col-span-2', 'col-span-2', 'col-span-3', 'col-span-3']);
  });

  it('the filter chips carry no numbers the cards already show; only "Held" keeps its count', () => {
    dashboard(rows);
    const chips = [...document.querySelectorAll<HTMLElement>('[aria-label="Show"] [role="radio"]')];
    expect(chips.map((chip) => chip.textContent)).toEqual(['All', 'Needs you', 'FAIL', 'PASS', 'Held1']);
  });

  it('the donut legend shows words and glyphs, and numbers only for the slices no card counts', async () => {
    dashboard(rows);
    const legend = await screen.findByRole('list', { name: 'Countertop outcomes' }, { timeout: 10_000 });
    const shown = [...legend.querySelectorAll('li')].filter((li) => [...li.querySelectorAll('span.num')].some((n) => !n.classList.contains('sr-only'))).map((li) => li.textContent);
    expect(shown).toEqual(['Needs your decision1', 'Not checkable1']);
    // Every count is still there for a screen reader, since the ring is hidden from it.
    expect(legend.textContent).toMatch(/FAIL\s*1/);
    expect(legend.textContent).toMatch(/PASS\s*1/);
    expect(legend.querySelectorAll('[data-outcome-icon]').length).toBeGreaterThanOrEqual(4);
  }, 15_000);

  it('on a phone only the ring is left out: the legend, its counts and the FAILs that need you stay', async () => {
    const failOpen = row('fail-open', 5, { outcome: 'FAIL', needs_decision: true, delta: x('-1', '-1"') });
    dashboard([...rows, failOpen]);
    const legend = await screen.findByRole('list', { name: 'Countertop outcomes' }, { timeout: 10_000 });
    // Nothing around the legend is hidden at phone width; the ring alone is (shown from sm up).
    for (let el: HTMLElement | null = legend; el; el = el.parentElement) expect(el.classList.contains('hidden')).toBe(false);
    const ring = document.querySelector('[data-slot="outcome-chart"] [data-slot="chart"]')!;
    expect(ring.classList.contains('hidden')).toBe(true);
    expect(ring.classList.contains('sm:flex')).toBe(true);
    expect(legend.querySelector('[data-slot="fail-needs-you"]')?.textContent).toBe('1 needs you');
  }, 15_000);
});
