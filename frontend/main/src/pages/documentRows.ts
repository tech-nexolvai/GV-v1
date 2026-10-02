import type { FindingCounts, PackagePage, ReviewSessionPage } from '../api/client';

export interface DocumentRow {
  document: PackagePage['items'][number];
  counts: FindingCounts | null;
  countsError: string | null;
  reviewer: string | null;
}
export interface DocumentRows {
  rows: DocumentRow[];
  reviewerError: string | null;
  hasMore: boolean;
}
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

/** Supplementary failures must not discard successfully loaded document records. */
export async function loadDocumentRows(api: {
  packages: () => Promise<PackagePage>;
  counts: (id: string) => Promise<FindingCounts>;
  sessions: () => Promise<ReviewSessionPage>;
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
    try {
      return { document, reviewer, counts: await api.counts(document.id), countsError: null };
    } catch (error) {
      return { document, reviewer, counts: null, countsError: message(error) };
    }
  }));
  return { rows, reviewerError, hasMore: page.next_cursor !== null };
}

export interface DocumentListState {
  data: DocumentRows | null;
  loading: boolean;
  error: string | null;
}
export type DocumentListEvent = { type: 'loading' } | { type: 'loaded'; data: DocumentRows } | { type: 'failed'; error: string };
export const initialDocumentList: DocumentListState = { data: null, loading: true, error: null };

/** Refresh failures retain the last received rows, with an explicit stale-data message. */
export function documentListReducer(state: DocumentListState, event: DocumentListEvent): DocumentListState {
  switch (event.type) {
    case 'loading': return { ...state, loading: true, error: null };
    case 'loaded': return { data: event.data, loading: false, error: null };
    case 'failed': return { ...state, loading: false, error: event.error };
  }
}
