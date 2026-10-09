import { lazy, Suspense, useState } from 'react';
import { RefreshCw, Search } from 'lucide-react';

import { listRules, type CountertopResult } from '@/api/client';
import { useAsync } from '@/api/useAsync';
import type { Finding } from '@/data/types';
import {
  bucketCounts,
  defaultFilter,
  failsNeedingYou,
  kpis as computeKpis,
  matchesFilter,
  sortRows,
  type Filter,
} from '@/lib/countertop-results';
import { architectFindingIds } from '@/lib/architect';
import { architectItemKey } from '@/lib/needs-you-queue';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { KpiCards } from './kpi-cards';
import { CountertopTable, type RowActions } from './countertop-table';
import { DecideDialog, type DecideHandlers } from './decide-dialog';
import { OtherChecks, type BulkResult } from './other-checks';

// The chart library is fetched only when a review has results to draw.
const OutcomeChart = lazy(() => import('./outcome-chart'));

export type CountertopsState =
  | { status: 'loading' }
  | { status: 'error'; error: string }
  | { status: 'ready'; rows: CountertopResult[] };

const CHIPS: { value: Filter; word: string }[] = [
  { value: 'all', word: 'All' },
  { value: 'needs-you', word: 'Needs you' },
  { value: 'fail', word: 'FAIL' },
  { value: 'pass', word: 'PASS' },
  { value: 'held', word: 'Held' },
];

/**
 * The review opens here (#1039): five numbers, one chart, one table — no reading required. Every
 * number comes from the countertop-results API; decisions go through the page's existing handlers.
 */
export function ResultsDashboard({
  countertops,
  findings,
  blockingIds,
  busy,
  filter: requestedFilter,
  onFilterChange,
  onRetry,
  onRefresh,
  handlers,
  onBulkDismiss,
  onShowDrawing,
  onOpenCard,
  onOpenQueue,
}: {
  countertops: CountertopsState;
  /** All recorded findings of the live run (for decisions and the "other checks"). */
  findings: Finding[];
  /** Findings the readiness API says still need someone. Null while that answer loads. */
  blockingIds: ReadonlySet<string> | null;
  busy: boolean;
  /** Set by the header's "Review N items"; null leaves the dashboard's own choice. */
  filter: Filter | null;
  onFilterChange: (filter: Filter | null) => void;
  onRetry: () => void;
  onRefresh: () => void;
  handlers: DecideHandlers;
  onBulkDismiss: (ids: string[], note: string) => Promise<BulkResult>;
  onShowDrawing: (row: CountertopResult) => void;
  onOpenCard: (row: CountertopResult) => void;
  /** Opens the "Needs you" queue (#1050) from the "Needs you" card, or at one item (its key). */
  onOpenQueue?: (startAt?: string) => void;
}) {
  const [search, setSearch] = useState('');
  // Human names for the rule ids, for the "other checks" list. A missing rulebook only costs names.
  const rules = useAsync(() => listRules(), []);
  const ruleNames = new Map(rules.status === 'ready' ? rules.data.map((r) => [r.rule_id, r.name] as [string, string]) : []);
  const [deciding, setDeciding] = useState<{ finding: Finding; title: string } | null>(null);

  if (countertops.status === 'loading') return <DashboardSkeleton />;
  if (countertops.status === 'error') {
    return (
      <section data-tw className="flex flex-col items-start gap-3 p-4 font-sans sm:p-6" role="alert">
        <p className="text-sm">The countertop results could not be loaded: {countertops.error}</p>
        <Button size="sm" variant="outline" onClick={onRetry}>
          <RefreshCw /> Try again
        </Button>
      </section>
    );
  }

  const rows = countertops.rows;
  const filter = requestedFilter ?? defaultFilter(rows);
  const counts = computeKpis(rows);
  const query = search.trim().toLowerCase();
  const visible = sortRows(rows).filter(
    (row) => matchesFilter(row, filter) && (!query || String(row.page_number) === query || row.label.toLowerCase().includes(query)),
  );
  const byId = new Map(findings.map((f) => [f.id, f]));
  // A countertop's own results: its width, and whether it matches the architect (#1085). Both are
  // shown on its row, so neither is listed again under "Other checks".
  const countertopIds = new Set([...rows.map((r) => r.finding_id).filter(Boolean), ...architectFindingIds(rows)]);
  const others = findings.filter((f) => !countertopIds.has(f.id));
  const blocking = blockingIds ?? new Set<string>();

  const chipCount: Record<Filter, number> = {
    all: counts.countertops,
    'needs-you': counts.needsYou,
    fail: counts.fail,
    pass: counts.pass,
    held: counts.held,
    automatic: counts.automatic,
  };

  const actions: RowActions = {
    onShowDrawing,
    onOpenCard,
    canDecide: (row) => row.needs_decision && row.finding_id !== null && byId.has(row.finding_id),
    onDecide: (row) => {
      const finding = row.finding_id ? byId.get(row.finding_id) : undefined;
      if (finding) setDeciding({ finding, title: row.label });
    },
    architectFinding: (row) => (row.architect?.finding_id ? byId.get(row.architect.finding_id) : undefined),
    onDecideArchitect: (row) => {
      const finding = row.architect?.finding_id ? byId.get(row.architect.finding_id) : undefined;
      if (finding) setDeciding({ finding, title: `${row.label}: matches the architect?` });
    },
    onPairArchitect: onOpenQueue ? (row) => onOpenQueue(architectItemKey(row.row_id)) : undefined,
  };

  return (
    <section data-tw data-slot="results-dashboard" aria-label="Results" className="flex flex-col gap-4 p-4 font-sans sm:p-6">
      {rows.length === 0 ? (
        <div className="flex flex-col items-start gap-2 rounded-xl border border-dashed p-6">
          <p className="font-medium">No countertops in this run</p>
          <p className="text-sm text-muted-foreground">Run checks from Measurements to see results here.</p>
        </div>
      ) : (
        <>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-center">
            <KpiCards kpis={counts} active={filter} onSelect={(next) => onFilterChange(next)} onOpenQueue={onOpenQueue ? () => onOpenQueue() : undefined} />
            <div className="rounded-xl border bg-card px-4 py-3">
              <Suspense fallback={<Skeleton className="h-32 w-64" />}>
                <OutcomeChart counts={bucketCounts(rows)} failNeedsYou={failsNeedingYou(rows)} />
              </Suspense>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={filter}
              onValueChange={(next) => next && onFilterChange(next as Filter)}
              aria-label="Show"
              className="flex-wrap"
            >
              {CHIPS.concat(filter === 'automatic' ? [{ value: 'automatic', word: 'Automatic' }] : []).map((chip) => (
                <ToggleGroupItem key={chip.value} value={chip.value} className="gap-1.5 px-2.5">
                  {chip.word}
                  <span className="num text-xs text-muted-foreground">{chipCount[chip.value]}</span>
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            <div className="ml-auto flex w-full items-center gap-1 sm:w-auto">
              <div className="relative flex-1 sm:w-56 sm:flex-none">
                <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Page or countertop" aria-label="Find a page or countertop" className="h-8 pl-8" />
              </div>
              <Button size="icon-sm" variant="ghost" onClick={onRefresh} disabled={busy} aria-label="Refresh results">
                <RefreshCw className={busy ? 'animate-spin motion-reduce:animate-none' : undefined} />
              </Button>
            </div>
          </div>

          {visible.length === 0 ? (
            <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
              {query ? `Nothing matches “${search.trim()}”.` : filter === 'needs-you' ? 'Nothing needs you here.' : 'No countertops in this group.'}
            </p>
          ) : (
            <CountertopTable rows={visible} actions={actions} />
          )}
        </>
      )}

      <OtherChecks
        findings={others}
        ruleNames={ruleNames}
        blocking={blocking}
        onDecide={(finding) => setDeciding({ finding, title: finding.scope_label ?? finding.name })}
        onBulkDismiss={onBulkDismiss}
      />

      <DecideDialog
        // One dialog per finding: a draft can never move from one finding to another.
        key={deciding?.finding.id ?? 'none'}
        finding={deciding?.finding ?? null}
        title={deciding?.title ?? ''}
        open={deciding !== null}
        onOpenChange={(open) => !open && setDeciding(null)}
        handlers={handlers}
      />
    </section>
  );
}

function DashboardSkeleton() {
  return (
    <section data-tw aria-busy="true" aria-label="Loading results" className="flex flex-col gap-4 p-4 sm:p-6">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5 lg:gap-3">
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-20 rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-8 w-80" />
      {[0, 1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </section>
  );
}
