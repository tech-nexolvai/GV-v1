import { Plus, ArrowRight } from 'lucide-react';
import { useEffect, useReducer, useState } from 'react';
import { listPackages, getFindingCounts, listReviewSessions } from '../api/client';
import { projectId } from '../api/config';
import { StatusBadge } from '../components/ui/Badge';
import { PageFrame, PageLoadError } from '../components/ui/PageFrame';
import { documentListReducer, initialDocumentList, loadDocumentRows } from './documentRows';
import { DocumentResults } from './DocumentResults';
import '../components/ui/PageFrame.css';
import './PackagesPage.css';

function loadRows() {
  const project = projectId();
  return loadDocumentRows({
    packages: () => listPackages(project),
    counts: (id) => getFindingCounts(project, id),
    sessions: () => listReviewSessions(project, { mine: false }),
  });
}

interface PackagesPageProps {
  onOpenReview: (packageId: string) => void;
  onNewPackage: () => void;
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  });
}

export function PackagesPage({ onOpenReview, onNewPackage }: PackagesPageProps) {
  const [attempt, setAttempt] = useState(0);
  const [state, dispatch] = useReducer(documentListReducer, initialDocumentList);
  useEffect(() => {
    let current = true;
    Promise.resolve().then(loadRows).then(
      (data) => { if (current) dispatch({ type: 'loaded', data }); },
      (error: unknown) => { if (current) dispatch({ type: 'failed', error: error instanceof Error ? error.message : String(error) }); },
    );
    return () => { current = false; };
  }, [attempt]);
  function retry() {
    dispatch({ type: 'loading' });
    setAttempt((value) => value + 1);
  }
  const data = state.data;
  const partial = data?.reviewerError != null || data?.rows.some((row) => row.countsError !== null);

  return (
    <PageFrame title="Documents" className="packages-page"
      description="Review submissions from vendors against the architectural set and rulebook."
      actions={<button className="btn btn--action" onClick={onNewPackage}>
        <Plus size={14} aria-hidden="true" /> New Document
      </button>}>
      {state.loading && <p className="page-frame__state" role="status">{data ? 'Refreshing document details… Existing rows remain available.' : 'Loading documents…'}</p>}
      {!data && state.error && <PageLoadError title="Documents could not be loaded" message={state.error} onRetry={retry} />}
      {data && (partial || state.error) && <aside className="packages-page__notice" aria-label="Document data availability">
        <div role="status">
          <strong>{state.error ? 'Refresh failed. Showing the last loaded documents.' : 'Documents loaded; some details are unavailable.'}</strong>
          <p>{state.error ?? 'You can still open each review. Unavailable results are not zero findings.'}</p>
          {data.reviewerError && <p>Reviewer details unavailable: {data.reviewerError}</p>}
          {data.rows.some((row) => row.countsError !== null) && <details>
            <summary>Result request details</summary>
            <ul>{data.rows.filter((row) => row.countsError !== null).map((row) => <li key={row.document.id}>
              {row.document.vendor ?? row.document.id}: {row.countsError}
            </li>)}</ul>
          </details>}
        </div>
        <button type="button" className="btn btn--ghost" disabled={state.loading} onClick={retry}>Retry unavailable details</button>
      </aside>}
      {data && data.rows.length === 0 && <p className="page-frame__state">No documents yet. Start a review with New Document.</p>}
      {data?.hasMore && <p className="page-frame__state">Showing the first {data.rows.length} documents returned by the server. Older documents are not included in this view.</p>}
      {data && data.rows.length > 0 && <div className="packages-table-wrap" role="region" aria-label="Documents table; scroll horizontally for all columns" tabIndex={0}>
        <table className="packages-table">
          <thead><tr>
            <th scope="col">Document ID</th><th scope="col">Vendor</th><th scope="col">Project</th>
            <th scope="col">Category</th><th scope="col">Status</th><th scope="col">Results</th>
            <th scope="col">Submitted</th><th scope="col">Reviewer</th>
            <th scope="col"><span className="sr-only">Open review</span></th>
          </tr></thead>
          <tbody>{data.rows.map(({ document: pkg, counts, countsError, reviewer }) => (
            <tr key={pkg.id} className="packages-table__row" onClick={() => onOpenReview(pkg.id)}>
              <td data-label="Package ID"><span className="packages-table__id">{pkg.id}</span></td>
              <td data-label="Vendor"><span className="packages-table__vendor">{pkg.vendor ?? '—'}</span></td>
              <td data-label="Project"><span className="packages-table__project">{pkg.project_id}</span></td>
              <td data-label="Category"><span className="packages-table__category">—</span></td>
              <td data-label="Status"><StatusBadge status={pkg.state} size="sm" /></td>
              <td data-label="Results"><DocumentResults counts={counts} error={countsError} /></td>
              <td data-label="Submitted"><span className="packages-table__date">{formatDate(pkg.created_at)}</span></td>
              <td data-label="Reviewer"><span className="packages-table__reviewer">{reviewer ?? (data.reviewerError ? 'Unavailable' : 'Not listed')}</span></td>
              <td><button type="button" className="btn btn--subtle btn--icon"
                aria-label={`Open review for ${pkg.vendor ?? 'document'} (${pkg.id})`}
                onClick={(event) => { event.stopPropagation(); onOpenReview(pkg.id); }}>
                <ArrowRight size={16} className="packages-table__arrow" aria-hidden="true" />
              </button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>}
    </PageFrame>
  );
}
