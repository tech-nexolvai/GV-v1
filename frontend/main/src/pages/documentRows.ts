import type { ApprovalReadiness, FindingCounts, PackagePage, ReviewSessionPage, SignedExportStatus } from '../api/client';

export interface DocumentRow {
  document: PackagePage['items'][number];
  counts: FindingCounts | null;
  countsError: string | null;
  reviewer: string | null;
  /** The approval-readiness answer for a package under review (#1034); absent when not asked or unavailable. */
  readiness?: ApprovalReadiness | null;
  /** Signed-export status for an approved package (#1034); absent when not asked or unavailable. */
  exports?: SignedExportStatus['status'] | null;
}
export interface DocumentRows {
  rows: DocumentRow[];
  reviewerError: string | null;
  hasMore: boolean;
  nextCursor: string | null;
}
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

/** Package states in which someone may still need to decide something. */
const IN_REVIEW = new Set(['AWAITING_REVIEW', 'NEEDS_INPUT', 'CHANGES_REQUESTED']);

/** Supplementary failures must not discard successfully loaded document records. */
export async function loadDocumentRows(api: {
  packages: () => Promise<PackagePage>;
  counts: (id: string) => Promise<FindingCounts>;
  sessions: () => Promise<ReviewSessionPage>;
  /** Optional: what still needs the reviewer, for packages under review. */
  readiness?: (id: string) => Promise<ApprovalReadiness>;
  /** Optional: whether the signed files are ready, for approved packages. */
  exports?: (id: string) => Promise<SignedExportStatus>;
}): Promise<DocumentRows> {
  const page = await api.packages(); // A primary-list failure remains a genuine load error.
  let reviewerError: string | null = null;
  let reviewers = new Map<string, string>();
  try {
    const sessions = await api.sessions();
    reviewers = new Map(sessions.items.map((session) => [session.package_revision_id, session.reviewer]));
  } catch (error) { reviewerError = message(error); }
  const rows = await Promise.all(page.items.map(async (document) => {
    const reviewer = reviewers.get(document.current_revision_id) ?? null;
    // Supplementary and independent: either failing leaves its fact unknown, never invented.
    const [readiness, exports] = await Promise.all([
      api.readiness && IN_REVIEW.has(document.state) ? api.readiness(document.id).catch(() => null) : Promise.resolve(null),
      api.exports && document.state === 'APPROVED' ? api.exports(document.id).then((answer) => answer.status, () => null) : Promise.resolve(null),
    ]);
    try {
      return { document, reviewer, counts: await api.counts(document.id), countsError: null, readiness, exports };
    } catch (error) {
      return { document, reviewer, counts: null, countsError: message(error), readiness, exports };
    }
  }));
  const nextCursor = page.next_cursor ?? null;
  return { rows, reviewerError, hasMore: nextCursor !== null, nextCursor };
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
