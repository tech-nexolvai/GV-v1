import { Plus, ArrowRight } from 'lucide-react';
import { useEffect, useReducer, useState } from 'react';
import { listPackages, getFindingCounts, listReviewSessions } from '../api/client';
import { projectId } from '../api/config';
import { StatusBadge } from '../components/ui/Badge';
import { PageFrame, PageLoadError } from '../components/ui/PageFrame';
import { documentListReducer, documentNavigation, restoreDocumentList, loadDocumentRows } from './documentRows';
import { DocumentResults } from './DocumentResults';
import { DocumentRecord, DocumentDetails } from './DocumentRecord';
import { documentGuidance } from './documentGuidance';
import '../components/ui/PageFrame.css';
import './PackagesPage.css';

function loadRows(cursor?: string) {
  const project = projectId();
  return loadDocumentRows({
    packages: () => listPackages(project, cursor ? { cursor } : undefined),
    counts: (id) => getFindingCounts(project, id),
    sessions: () => listReviewSessions(project, { mine: false }),
  });
}

interface PackagesPageProps {
  onOpenReview: (packageId: string) => void;
  onNewPackage: () => void;
  initialCursors?: readonly string[];
  positionNotice?: string | null;
  onPositionChange?: (cursors: readonly string[]) => void;
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  });
}

export function PackagesPage({ onOpenReview, onNewPackage, initialCursors = [], positionNotice, onPositionChange }: PackagesPageProps) {
  const [attempt, setAttempt] = useState(0);
  const [state, dispatch] = useReducer(documentListReducer, initialCursors, restoreDocumentList);
  useEffect(() => {
    let current = true;
    Promise.resolve().then(() => loadRows(state.requestedTrail.at(-1))).then(
      (data) => {
        if (current) {
          dispatch({ type: 'loaded', data });
          onPositionChange?.(state.requestedTrail.slice(1) as string[]);
        }
      },
      (error: unknown) => { if (current) dispatch({ type: 'failed', error: error instanceof Error ? error.message : String(error) }); },
    );
    return () => { current = false; };
  }, [attempt, state.requestedTrail, onPositionChange]);
  function retry() {
    dispatch({ type: 'loading' });
    setAttempt((value) => value + 1);
  }
  const data = state.data;
  const navigation = documentNavigation(state);
  const partial = data?.reviewerError != null || data?.rows.some((row) => row.countsError !== null);

  return (
    <PageFrame title="Documents" className="packages-page"
      description="Open a drawing review to check its findings and evidence, then continue reviewing."
      actions={<button className="btn btn--action" onClick={onNewPackage}>
        <Plus size={14} aria-hidden="true" /> New Document
      </button>}>
      {positionNotice && <p className="page-frame__state" role="status">{positionNotice}</p>}
      {state.loading && <p className="page-frame__state" role="status">{data ? navigation.changingPage ? `Loading page ${state.requestedTrail.length}… Page ${navigation.page} remains visible.` : 'Refreshing document details… Existing rows remain available.' : 'Loading documents…'}</p>}
      {!data && state.error && <PageLoadError title="Documents could not be loaded" message={state.error} onRetry={retry} />}
      {!data && state.error && state.requestedTrail.length > 1 && <button type="button" className="btn btn--ghost" onClick={() => dispatch({ type: 'first' })}>Start from first page</button>}
      {data && (partial || state.error) && <aside className="packages-page__notice" aria-label="Document data availability">
        <div role="status">
          <strong>{state.error ? navigation.changingPage ? `Could not load page ${state.requestedTrail.length}. Still showing page ${navigation.page}.` : 'Refresh failed. Showing the last loaded documents.' : 'Documents loaded; some details are unavailable.'}</strong>
          <p>{state.error ?? 'You can still open each review. Unavailable results are not zero findings.'}</p>
          {data.reviewerError && <p>Reviewer details unavailable: {data.reviewerError}</p>}
          {data.rows.some((row) => row.countsError !== null) && <details>
            <summary>Result request details</summary>
            <ul>{data.rows.filter((row) => row.countsError !== null).map((row) => <li key={row.document.id}>
              {row.document.vendor ?? row.document.id}: {row.countsError}
            </li>)}</ul>
          </details>}
        </div>
        <button type="button" className="btn btn--ghost" disabled={state.loading} onClick={retry}>{navigation.changingPage ? 'Retry page' : 'Retry unavailable details'}</button>
      </aside>}
      {data && <nav className="packages-page__pagination" aria-label="Document pages">
        <p role="status" aria-atomic="true">Page {navigation.page} · {data.rows.length} document{data.rows.length === 1 ? '' : 's'} on this page</p>
        <div>
          {navigation.page > 1 && <button type="button" className="btn btn--ghost" disabled={state.loading} onClick={() => dispatch({ type: 'first' })}>First page</button>}
          <button type="button" className="btn btn--ghost" disabled={navigation.previousDisabled} onClick={() => dispatch({ type: 'previous' })}>Previous page</button>
          <button type="button" className="btn btn--ghost" disabled={navigation.nextDisabled} onClick={() => dispatch({ type: 'next' })}>Next page</button>
        </div>
      </nav>}
      {navigation.repeatedCursor && <p role="alert" className="page-frame__state">The server repeated a page cursor. Further navigation is unavailable; existing documents remain visible.</p>}
      {data && data.rows.length === 0 && <p className="page-frame__state">{navigation.page === 1 ? 'No documents yet. Start a review with New Document.' : 'No documents on this page. Use Previous page to return to the earlier results.'}</p>}
      {data && data.rows.length > 0 && <ul className="document-cards" role="list" aria-label="Drawing reviews">
          {data.rows.map((row) => (
            <li key={row.document.id} className="document-card">
              <div className="document-card__header">
                <DocumentRecord row={row} />
                <button type="button" className="btn btn--action"
                aria-label={`Open review for ${row.document.vendor ?? 'document'} (${row.document.id})`}
                onClick={() => onOpenReview(row.document.id)}>
                Open review
                <ArrowRight size={16} aria-hidden="true" />
                </button>
              </div>
              <div className="document-card__meta">
                <StatusBadge status={row.document.state} />
                <span>Submitted <time dateTime={row.document.created_at}>{formatDate(row.document.created_at)}</time></span>
              </div>
              <div className="document-card__results">
                <p className="document-card__results-heading">Recorded check results</p>
                {row.counts && row.counts.total > 0 && <p className="document-card__count-note">
                  {row.counts.total} recorded check{row.counts.total === 1 ? '' : 's'} — these count checks, not drawings or individual dimensions.
                </p>}
                <DocumentResults counts={row.counts} error={row.countsError} />
              </div>
              <p className="document-card__guidance"><strong>Next step</strong> {documentGuidance(row.document.state, row.counts)}</p>
              <DocumentDetails row={row} reviewerUnavailable={data.reviewerError !== null} />
            </li>
          ))}
      </ul>}
    </PageFrame>
  );
}
