import { Plus } from 'lucide-react';
import { useEffect, useReducer, useState } from 'react';
import { getPackagesSummary, PACKAGES_SUMMARY_PAGE_SIZE } from '../api/client';
import { projectId } from '../api/config';
import { DocumentsTable } from '@/components/documents/documents-table';
import { Button } from '@/components/ui/button';
import { PageFrame, PageLoadError } from '@/components/ui/PageFrame';
import { documentListReducer, documentNavigation, documentRowsOf, restoreDocumentList } from './documentRows';

/** One request per page (#1064): `packages-summary` carries every column the table shows. */
async function loadRows(cursor?: string) {
  return documentRowsOf(await getPackagesSummary(projectId(), { cursor, limit: PACKAGES_SUMMARY_PAGE_SIZE }));
}

interface PackagesPageProps {
  onOpenReview: (packageId: string) => void;
  onNewPackage: () => void;
  initialCursors?: readonly string[];
  positionNotice?: string | null;
  onPositionChange?: (cursors: readonly string[]) => void;
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
  const morePages = navigation.page > 1 || Boolean(data?.hasMore);

  return (
    <PageFrame title="Documents"
      description="Every drawing review in this project. Open one to see its results."
      actions={<Button type="button" onClick={onNewPackage}>
        <Plus aria-hidden="true" /> New review
      </Button>}>
      <div className="flex flex-col gap-3">
        {positionNotice && <p className="text-sm text-muted-foreground" role="status">{positionNotice}</p>}
        {state.loading && (
          <p className="text-sm text-muted-foreground" role="status">
            {data
              ? navigation.changingPage ? `Loading page ${state.requestedTrail.length}… Page ${navigation.page} stays visible.` : 'Refreshing…'
              : 'Loading documents…'}
          </p>
        )}
        {!data && state.error && <PageLoadError title="Documents could not be loaded" message={state.error} onRetry={retry} />}
        {!data && state.error && state.requestedTrail.length > 1 && (
          <Button type="button" variant="ghost" className="self-start" onClick={() => dispatch({ type: 'first' })}>Start from first page</Button>
        )}
        {data && state.error && (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-muted/50 px-3 py-2 text-sm" role="alert">
            <span>
              {navigation.changingPage
                ? `Could not load page ${state.requestedTrail.length}; still showing page ${navigation.page}.`
                : 'Refresh failed; showing the last loaded reviews.'}{' '}
              <span className="text-xs">{state.error}</span>
            </span>
            <Button type="button" size="sm" variant="outline" disabled={state.loading} onClick={retry}>
              {navigation.changingPage ? 'Retry page' : 'Retry'}
            </Button>
          </div>
        )}
        {navigation.repeatedCursor && (
          <p role="alert" className="text-sm">The server repeated a page cursor. Further pages are unavailable; the reviews shown stay visible.</p>
        )}
        {data && (
          <DocumentsTable
            rows={data.rows}
            onOpen={onOpenReview}
            morePages={morePages}
            emptyMessage={navigation.page === 1 ? 'No reviews yet. Start one with New review.' : 'No reviews on this page. Use Previous page to go back.'}
          />
        )}
        {data && morePages && (
          <nav aria-label="Document pages" className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <p role="status" aria-atomic="true" className="text-muted-foreground">
              Page <span className="num">{navigation.page}</span> · <span className="num">{data.rows.length}</span> on this page
            </p>
            <div className="flex gap-2">
              {navigation.page > 1 && <Button type="button" size="sm" variant="ghost" disabled={state.loading} onClick={() => dispatch({ type: 'first' })}>First page</Button>}
              <Button type="button" size="sm" variant="outline" disabled={navigation.previousDisabled} onClick={() => dispatch({ type: 'previous' })}>Previous page</Button>
              <Button type="button" size="sm" variant="outline" disabled={navigation.nextDisabled} onClick={() => dispatch({ type: 'next' })}>Next page</Button>
            </div>
          </nav>
        )}
      </div>
    </PageFrame>
  );
}
