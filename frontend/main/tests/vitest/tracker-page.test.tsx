// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Tracker, TrackerItem } from '@/api/client';
import { TrackerPage } from '@/pages/TrackerPage';
import { dayWords, filterItems, fixedNextWords, NO_FILTERS, roundWords, setRoundsWords, stateCounts, stateRank, stateWords, valuesWords } from '@/lib/tracker';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

function item(over: Partial<TrackerItem> & Pick<TrackerItem, 'key' | 'item'>): TrackerItem {
  return {
    package_id: 's1',
    set_name: 'Set one',
    vendor_name: 'Acme Millwork',
    vendor_key: 'acme millwork',
    page: 4,
    vendor_value: '46"',
    architect_value: '37"',
    category: 'cabinet',
    category_label: 'Cabinets',
    kind: 'Height',
    check: 'architect',
    check_label: 'Against the architect',
    state: 'open',
    open_kind: 'wrong',
    status_now: 'different',
    ever_wrong: true,
    fixed_round: null,
    tags: [],
    first_round: 1,
    round_now: 1,
    rounds: [1],
    first_seen_at: '2026-10-01T09:00:00+00:00',
    last_seen_at: '2026-10-02T09:00:00+00:00',
    checks_seen: 1,
    impact: 50,
    decision: null,
    decided_by: null,
    decided_at: null,
    link: { package_id: 's1', chat_id: 'c1', message_id: 'm1', finding_id: 'F1' },
    history: [],
    ...over,
  };
}

const ITEMS: TrackerItem[] = [
  item({ key: 'a', item: 'Outlet height', tags: ['Still wrong'], first_round: 1, round_now: 2, rounds: [1, 2] }),
  item({ key: 'b', item: 'Filler at wall end', state: 'open', open_kind: 'decision', status_now: 'check', kind: 'Width, depth or length' }),
  item({ key: 'c', item: 'Sink base width', state: 'fixed', open_kind: null, fixed_round: 2, round_now: 2, page: 5, link: { package_id: 's1', chat_id: 'c1', message_id: 'm1', finding_id: 'F4' } }),
  item({ key: 'd', item: 'Countertop overall width', state: 'not_a_problem', open_kind: null, decided_by: 'reviewer-two', package_id: 's2', set_name: 'Set two' }),
];

const TRACKER: Tracker = {
  project_id: 'p',
  made_at: '2026-10-03T09:00:00+00:00',
  totals: { sets: 2, checks: 3, items: 4, open: 2, open_wrong: 1, open_decision: 1, fixed: 1, not_a_problem: 1, right_later: 0 },
  items: ITEMS,
  sets: [
    { package_id: 's1', name: 'Set one', vendor_name: 'Acme Millwork', vendor_key: 'acme millwork', product: 'cabinet', items: 3, open: 2, fixed: 1, not_a_problem: 0, last_checked_at: '2026-10-02T09:00:00+00:00', rounds: [{ round: 1, items: 3, open: 3, wrong: 2, decision: 1, fixed: 0 }, { round: 2, items: 2, open: 1, wrong: 1, decision: 0, fixed: 1 }], checks: [] },
    { package_id: 's2', name: 'Set two', vendor_name: 'Acme Millwork', vendor_key: 'acme millwork', product: 'countertop', items: 1, open: 0, fixed: 0, not_a_problem: 1, last_checked_at: '2026-10-01T09:00:00+00:00', rounds: [{ round: 1, items: 1, open: 0, wrong: 0, decision: 0, fixed: 0 }], checks: [] },
  ],
  vendors: [
    {
      vendor_key: 'acme millwork', vendor_name: 'Acme Millwork', sets: 2, rounds: 3, items: 4, mistakes: 2, open: 2,
      fixed_next_round: 1, fixed_next_of: 2, fixed_next_share: '50', average_open_per_round: '1.3',
      by_product: [{ label: 'Cabinets', count: 2, share: '100' }],
      by_kind: [{ label: 'Height', count: 1, share: '50' }, { label: 'Width, depth or length', count: 1, share: '50' }],
      by_check: [{ label: 'Against the architect', count: 2, share: '100' }],
    },
  ],
};

describe('tracker words (#1578)', () => {
  it('says each state one way, coloured only for Wrong and Fixed', () => {
    expect(stateWords(ITEMS[0])).toEqual({ word: 'Open · Wrong', tone: 'wrong', detail: null });
    expect(stateWords(ITEMS[1])).toEqual({ word: 'Open · Your decision', tone: 'neutral', detail: null });
    expect(stateWords(ITEMS[2])).toEqual({ word: 'Fixed · round 2', tone: 'right', detail: null });
    expect(stateWords(ITEMS[3])).toEqual({ word: 'Not a problem', tone: 'neutral', detail: 'reviewer-two' });
    expect(stateWords({ ...ITEMS[0], state: 'right_later' }).word).toBe('Right on a later check');
    expect(ITEMS.map(stateRank)).toEqual([0, 1, 2, 4]);
  });

  it('filters by state, set, vendor and words, and counts each state under the other filters', () => {
    expect(filterItems(ITEMS, NO_FILTERS).map((i) => i.key)).toEqual(['a', 'b']);
    expect(filterItems(ITEMS, { ...NO_FILTERS, state: 'all', packageId: 's2' }).map((i) => i.key)).toEqual(['d']);
    expect(filterItems(ITEMS, { ...NO_FILTERS, state: 'all', text: 'p.5' }).map((i) => i.key)).toEqual(['c']);
    expect(filterItems(ITEMS, { ...NO_FILTERS, state: 'all', vendorKey: 'other' })).toEqual([]);
    expect(stateCounts(ITEMS, { ...NO_FILTERS, packageId: 's1' })).toEqual({ open: 2, fixed: 1, not_a_problem: 0, all: 3 });
  });

  it('writes rounds, values and fixes in plain words', () => {
    expect(roundWords(ITEMS[0])).toBe('Round 1 → 2');
    expect(roundWords(ITEMS[1])).toBe('Round 1');
    expect(valuesWords(ITEMS[0])).toBe('46" · should be 37"');
    expect(setRoundsWords(TRACKER.sets[0])).toBe('Round 1 · 3 open → Round 2 · 1 open');
    expect(fixedNextWords(TRACKER.vendors[0])).toBe('1 of 2 (50%)');
    expect(fixedNextWords({ fixed_next_round: 0, fixed_next_of: 0, fixed_next_share: null })).toBe('No item re-checked in a next round yet');
    expect(dayWords('not a date')).toBe('');
  });
});

describe('the tracker page (#1578)', () => {
  it('shows the numbers once, open items first, and opens an item in its chat', async () => {
    const onOpenItem = vi.fn();
    render(<TrackerPage onOpenItem={onOpenItem} onOpenSet={() => {}} load={async () => TRACKER} />);
    const table = await screen.findByRole('table', { name: 'Items found' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getAllByRole('button')[0].textContent)).toEqual(['Outlet height', 'Filler at wall end']);
    expect(within(rows[0]).getByText('Open · Wrong')).toBeTruthy();
    expect(within(rows[0]).getByText(/Still wrong/)).toBeTruthy();

    await userEvent.click(within(rows[0]).getByRole('button', { name: /Outlet height/ }));
    expect(onOpenItem).toHaveBeenCalledWith(ITEMS[0].link);

    await userEvent.click(screen.getByRole('button', { name: /Fixed/ }));
    expect(within(table).getByText('Fixed · round 2')).toBeTruthy();
    const link = screen.getByRole('link', { name: /Download XLSX/ });
    expect(link.getAttribute('href')).toBe('/api/v1/projects/p/tracker.xlsx?state=fixed');

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Drawing set' }), 's2');
    await userEvent.click(screen.getByRole('button', { name: /^All/ }));
    expect(within(table).getByText('Countertop overall width')).toBeTruthy();
    expect(screen.getByRole('link', { name: /Download XLSX/ }).getAttribute('href')).toBe('/api/v1/projects/p/tracker.xlsx?package_id=s2');
  });

  it('shows the sets and the vendor scorecard', async () => {
    const onOpenSet = vi.fn();
    render(<TrackerPage onOpenItem={() => {}} onOpenSet={onOpenSet} load={async () => TRACKER} />);
    expect(await screen.findByText('Round 1 · 3 open → Round 2 · 1 open')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Set two' }));
    expect(onOpenSet).toHaveBeenCalledWith('s2');
    const card = screen.getByRole('article');
    expect(within(card).getByText('1 of 2 (50%)')).toBeTruthy();
    expect(within(card).getByText('1.3')).toBeTruthy();
    expect(within(card).getByText('Width, depth or length')).toBeTruthy();
  });

  it('says when nothing was found, and when it could not load (never as zero)', async () => {
    const empty: Tracker = { ...TRACKER, totals: { ...TRACKER.totals, checks: 0, items: 0, open: 0 }, items: [], sets: [], vendors: [] };
    const { unmount } = render(<TrackerPage onOpenItem={() => {}} onOpenSet={() => {}} load={async () => empty} />);
    expect(await screen.findByText('No checks have found items yet.')).toBeTruthy();
    expect(screen.queryByRole('link', { name: /Download XLSX/ })).toBeNull();
    unmount();
    render(<TrackerPage onOpenItem={() => {}} onOpenSet={() => {}} load={async () => { throw new Error('Server down.'); }} />);
    expect(await screen.findByText(/This is not a report of zero items/)).toBeTruthy();
  });
});
