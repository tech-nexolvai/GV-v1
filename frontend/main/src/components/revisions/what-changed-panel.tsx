/**
 * "What changed" on Results for revision 2 and later (#1180): pages added, removed and changed
 * between the previous revision and this one (by each page's content), and the countertops whose
 * stored result is not the same. Every word and number is the API's (`GET …/revisions/compare`).
 */

import { ArrowRight, GitCompareArrows } from 'lucide-react';
import { compareRevisions, type RevisionComparison } from '@/api/client';
import { useAsync } from '@/api/useAsync';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  COUNTERTOP_CHANGE_WORDS,
  PAGE_CHANGE_WORDS,
  changedCountertops,
  changedPages,
  pageSummary,
  uncomparedDrawings,
} from '@/lib/revisions';

type Side = RevisionComparison['countertops'][number]['before'];

function SideResult({ side }: { side: Side }) {
  if (!side) return <span className="text-sm text-muted-foreground">Not in this revision</span>;
  if (!side.outcome) return <span className="text-sm text-muted-foreground">Not checked yet</span>;
  return <OutcomeBadge outcome={side.outcome} />;
}

export function WhatChangedView({ comparison }: { comparison: RevisionComparison }) {
  const pages = changedPages(comparison);
  const unread = uncomparedDrawings(comparison);
  const rows = changedCountertops(comparison);
  return (
    <Card data-slot-name="what-changed" className="font-sans">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <GitCompareArrows className="size-4" aria-hidden="true" />
          What changed since revision <span className="num">{comparison.before_revision_number}</span>
        </CardTitle>
        <CardDescription>
          Pages: <span className="num">{pageSummary(comparison)}</span>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        {unread.length > 0 && (
          <ul className="flex flex-col gap-1 text-muted-foreground">
            {unread.map((item) => <li key={item.key}>{item.text}</li>)}
          </ul>
        )}
        <section aria-label="Pages that changed" className="flex flex-col gap-2">
          <h3 className="text-sm font-medium">Pages</h3>
          {pages.length === 0 ? (
            <p className="text-muted-foreground">No page was added, removed or changed.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {pages.map((page) => (
                <li key={page.key} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium">{PAGE_CHANGE_WORDS[page.change]}</span>
                  <span>
                    {page.drawing}, page <span className="num">{page.pageNumber}</span>
                    {page.change === 'moved' && page.beforePageNumber !== null && (
                      <> (was page <span className="num">{page.beforePageNumber}</span>)</>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section aria-label="Countertops that changed" className="flex flex-col gap-2">
          <h3 className="text-sm font-medium">Countertops</h3>
          {!comparison.countertops_read && (
            <p className="text-muted-foreground">
              Revision <span className="num">{comparison.after_revision_number}</span> has not been read yet. Its countertops
              appear here once the drawings are read.
            </p>
          )}
          {comparison.countertops_read && !comparison.countertops_checked && (
            <p className="text-muted-foreground">
              The checks have not run on this revision yet. Run them to see each countertop's new result.
            </p>
          )}
          {comparison.countertops_checked && rows.length === 0 && (
            <p className="text-muted-foreground">Every countertop has the same result as before.</p>
          )}
          {rows.length > 0 && (
            <ul className="flex flex-col gap-2">
              {rows.map((row) => (
                <li key={`${row.page_number}:${row.label}`} className="flex flex-col gap-1 rounded-md border p-2">
                  <span className="font-medium">
                    {row.label} <span className="text-muted-foreground">· {COUNTERTOP_CHANGE_WORDS[row.change]}</span>
                  </span>
                  <span className="flex flex-wrap items-center gap-2">
                    <SideResult side={row.before} />
                    <ArrowRight className="size-4 text-muted-foreground" aria-label="then" />
                    <SideResult side={row.after} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </CardContent>
    </Card>
  );
}

/** Loads the comparison of the newest revision with the one before it. */
export function WhatChangedPanel({ projectId, packageId, reloadKey = 0 }: { projectId: string; packageId: string; reloadKey?: number }) {
  const state = useAsync(() => compareRevisions(projectId, packageId), [projectId, packageId, reloadKey]);
  if (state.status === 'loading') return <Skeleton className="h-24 w-full" aria-label="Loading what changed" />;
  if (state.status === 'error') {
    return <p className="text-sm text-muted-foreground" role="alert">What changed could not be loaded: {state.error.message}</p>;
  }
  return <WhatChangedView comparison={state.data} />;
}
