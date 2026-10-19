/**
 * The job tracker's words and filters (#1578), kept out of the page so they are tested on their own.
 *
 * One meaning per word: "Open · Wrong" (red), "Open · Not compared" (grey: #1619, nothing to compare),
 * "Closed · round N" (green), "Not a problem" (grey, with the person), "Right on a later check" (grey:
 * the AI's reading changed, not the drawing). Colour only for Wrong and Right, always beside the word.
 */

import type { TrackerItem, TrackerSet, TrackerVendor } from '@/api/client';

export type StateFilter = 'open' | 'fixed' | 'not_a_problem' | 'all';
export type Tone = 'wrong' | 'right' | 'neutral';

export interface StateWords {
  word: string;
  tone: Tone;
  /** Small grey words after it: who said "Not a problem". */
  detail: string | null;
}

export function stateWords(item: Pick<TrackerItem, 'state' | 'open_kind' | 'fixed_round' | 'decided_by'>): StateWords {
  switch (item.state) {
    case 'open':
      return item.open_kind === 'wrong'
        ? { word: 'Open · Wrong', tone: 'wrong', detail: null }
        : { word: 'Open · Not compared', tone: 'neutral', detail: null };
    case 'fixed':
      // #1712: the comment letter's round-2 word.
      return { word: item.fixed_round ? `Closed · round ${item.fixed_round}` : 'Closed', tone: 'right', detail: null };
    case 'not_a_problem':
      return { word: 'Not a problem', tone: 'neutral', detail: item.decided_by || null };
    default:
      return { word: 'Right on a later check', tone: 'neutral', detail: null };
  }
}

/** Sorts the State column: what needs work first. */
export function stateRank(item: Pick<TrackerItem, 'state' | 'open_kind'>): number {
  if (item.state === 'open') return item.open_kind === 'wrong' ? 0 : 1;
  return { fixed: 2, right_later: 3, not_a_problem: 4 }[item.state] ?? 5;
}

/** "Round 1", or "Round 1 → 2" when it was seen in more than one. */
export function roundWords(item: Pick<TrackerItem, 'first_round' | 'round_now'>): string {
  return item.first_round === item.round_now ? `Round ${item.round_now}` : `Round ${item.first_round} → ${item.round_now}`;
}

export interface TrackerFilters {
  state: StateFilter;
  packageId: string | null;
  vendorKey: string | null;
  /** Words to find in the item, its set, its values or what it touches; empty finds all. */
  text: string;
}

export const NO_FILTERS: TrackerFilters = { state: 'open', packageId: null, vendorKey: null, text: '' };

type Filterable = Pick<TrackerItem, 'state' | 'package_id' | 'vendor_key'> & Partial<Pick<TrackerItem, 'item' | 'set_name' | 'kind' | 'vendor_value' | 'architect_value' | 'page'>>;

function matchesText(item: Filterable, text: string | undefined): boolean {
  const needle = (text ?? '').trim().toLowerCase();
  if (!needle) return true;
  const page = item.page != null ? `p.${item.page} page ${item.page}` : '';
  return [item.item, item.set_name, item.kind, item.vendor_value, item.architect_value, page].some((value) => (value ?? '').toLowerCase().includes(needle));
}

export function matchesState(item: Pick<TrackerItem, 'state'>, state: StateFilter): boolean {
  return state === 'all' || item.state === state;
}

export function filterItems<T extends Filterable>(items: readonly T[], filters: TrackerFilters): T[] {
  return items.filter(
    (item) =>
      matchesState(item, filters.state) &&
      (filters.packageId === null || item.package_id === filters.packageId) &&
      (filters.vendorKey === null || item.vendor_key === filters.vendorKey) &&
      matchesText(item, filters.text),
  );
}

/** How many items each state button would show, under the set and vendor filters. */
export function stateCounts(items: readonly Filterable[], filters: TrackerFilters): Record<StateFilter, number> {
  const scoped = filterItems(items, { ...filters, state: 'all' });
  return {
    open: scoped.filter((item) => item.state === 'open').length,
    fixed: scoped.filter((item) => item.state === 'fixed').length,
    not_a_problem: scoped.filter((item) => item.state === 'not_a_problem').length,
    all: scoped.length,
  };
}

/** "Round 1 · 12 open → Round 2 · 5 open". */
export function setRoundsWords(set: Pick<TrackerSet, 'rounds'>): string {
  return set.rounds.map((round) => `Round ${round.round} · ${round.open} open`).join(' → ');
}

/** "7 of 12 (58%)", or why there is no figure yet. */
export function fixedNextWords(vendor: Pick<TrackerVendor, 'fixed_next_round' | 'fixed_next_of' | 'fixed_next_share'>): string {
  if (vendor.fixed_next_of === 0 || vendor.fixed_next_share === null) return 'No item re-checked in a next round yet';
  return `${vendor.fixed_next_round} of ${vendor.fixed_next_of} (${vendor.fixed_next_share}%)`;
}

/** "18 Oct" (this year) or "18 Oct 2025": the day only, in the viewer's language. */
export function dayWords(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  return at.toLocaleDateString(undefined, at.getFullYear() === now.getFullYear() ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' });
}

/** The vendor's value and what it should be, on one line: `46" · should be 37"`. */
export function valuesWords(item: Pick<TrackerItem, 'vendor_value' | 'architect_value'>): string {
  const vendor = item.vendor_value.trim();
  const should = item.architect_value.trim();
  if (vendor && should) return `${vendor} · should be ${should}`;
  return vendor || (should ? `should be ${should}` : '');
}
