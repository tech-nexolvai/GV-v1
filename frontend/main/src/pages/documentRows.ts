import type { PackageSummary, PackageSummaryPage } from '../api/client';

/**
 * One page of the Documents table: the `packages-summary` answer (#1035), which carries every column
 * in a single request. It replaced one request per card (counts, readiness, exports, reviewers).
 */
export interface DocumentRows {
  rows: PackageSummary[];
  hasMore: boolean;
  nextCursor: string | null;
}

export function documentRowsOf(page: PackageSummaryPage): DocumentRows {
  const nextCursor = page.next_cursor ?? null;
  return { rows: [...page.items], hasMore: nextCursor !== null, nextCursor };
}

export interface DocumentListState {
  data: DocumentRows | null;
  loading: boolean;
  error: string | null;
  /** Only successful responses advance the visible page. Cursors are opaque server tokens. */
  trail: readonly (string | undefined)[];
  requestedTrail: readonly (string | undefined)[];
}
export type DocumentListEvent = { type: 'loading' } | { type: 'first' } | { type: 'next' } | { type: 'previous' } | { type: 'loaded'; data: DocumentRows } | { type: 'failed'; error: string };
const firstPage = [undefined];
export const initialDocumentList: DocumentListState = { data: null, loading: true, error: null, trail: firstPage, requestedTrail: firstPage };

export function restoreDocumentList(cursors: readonly string[]): DocumentListState {
  const trail = [undefined, ...cursors];
  return { ...initialDocumentList, trail, requestedTrail: trail };
}

export function documentNavigation(state: DocumentListState) {
  const next = state.data?.nextCursor;
  const repeatedCursor = next != null && state.trail.includes(next);
  return {
    page: state.trail.length,
    changingPage: state.requestedTrail !== state.trail,
    previousDisabled: state.loading || state.trail.length === 1,
    nextDisabled: state.loading || next == null || repeatedCursor,
    repeatedCursor,
  };
}

/** Refresh failures retain the last received rows, with an explicit stale-data message. */
export function documentListReducer(state: DocumentListState, event: DocumentListEvent): DocumentListState {
  switch (event.type) {
    case 'loading': return { ...state, loading: true, error: null };
    case 'first': return { ...state, requestedTrail: [undefined], loading: true, error: null };
    case 'next': {
      if (documentNavigation(state).nextDisabled) return state;
      return { ...state, requestedTrail: [...state.trail, state.data!.nextCursor!], loading: true, error: null };
    }
    case 'previous': {
      if (documentNavigation(state).previousDisabled) return state;
      return { ...state, requestedTrail: state.trail.slice(0, -1), loading: true, error: null };
    }
    case 'loaded': return { ...state, data: event.data, loading: false, error: null, trail: state.requestedTrail };
    case 'failed': return { ...state, loading: false, error: event.error };
  }
}
