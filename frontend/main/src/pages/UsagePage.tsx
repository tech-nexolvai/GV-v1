/**
 * What this project's AI reading has cost, counted from the API (#1072).
 *
 * The previous version of this page was a fixed table, and one of its numbers was
 * **"False-PASS rate 0.0%"** — the primary safety metric of the whole system, shown as perfect with
 * nothing behind it. A critical false PASS is a wrong dimension that a reviewer was told was right;
 * claiming a rate of zero without measuring one is the single most misleading thing this UI could
 * say. It also listed an audit trail attributing actions and timestamps to a named person who never
 * performed them.
 *
 * So everything here is counted from the API or said to be not measured. AI calls, cost and tokens
 * come from `GET /usage` (#1035), by day and by drawing set; names and recorded results come from
 * `packages-summary`. **Reading time is not shown as a number:** the API's reading times span the
 * saved call times, and the calls of one reading are saved together (a real duration is #1071).
 */

import { lazy, Suspense, useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { getPackagesSummary, getUsage, PACKAGES_SUMMARY_PAGE_SIZE } from '../api/client';
import { projectId } from '../api/config';
import { useAsync } from '../api/useAsync';
import { PageFrame, PageLoadError, PageLoading } from '@/components/ui/PageFrame';
import { DataTable, SortableHeader } from '@/components/data-table/data-table';
import { InfoTip } from '@/components/ui/info-tip';
import { Skeleton } from '@/components/ui/skeleton';
import { resultTotal } from '@/lib/documents-table';
import { costByDay, costText, modelWord, outcomeTotals, outcomesByUploadDay, usageBySet, usageKpis, type SetUsage } from '@/lib/usage';

// The chart library is fetched the first time Usage opens (#1072).
const CostByDayChart = lazy(() => import('@/components/usage/usage-charts').then((m) => ({ default: m.CostByDayChart })));
const OutcomesByDayChart = lazy(() => import('@/components/usage/usage-charts').then((m) => ({ default: m.OutcomesByDayChart })));

async function loadUsage() {
  const project = projectId();
  const [byDay, bySet, summary] = await Promise.all([
    getUsage(project, { groupBy: 'day' }),
    getUsage(project, { groupBy: 'package' }),
    getPackagesSummary(project, { limit: PACKAGES_SUMMARY_PAGE_SIZE }),
  ]);
  return { byDay, bySet, summary };
}

function Kpi({ label, value, sub, tip }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tip?: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border p-4" data-part="kpi">
      <span className="flex items-center gap-1.5 text-sm text-muted-foreground">{label}{tip}</span>
      <span className="text-2xl font-semibold">{value}</span>
      {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
    </div>
  );
}

const SET_COLUMNS: ColumnDef<SetUsage>[] = [
  {
    id: 'set',
    accessorFn: (row) => row.vendor ?? row.packageId,
    header: ({ column }) => <SortableHeader column={column}>Drawing set</SortableHeader>,
    cell: ({ row }) => row.original.vendor
      ? <span className="font-medium">{row.original.vendor}</span>
      : <span className="text-muted-foreground">Not on the loaded list* <code className="text-xs">{row.original.packageId.slice(0, 8)}</code></span>,
  },
  {
    id: 'calls',
    accessorFn: (row) => row.calls,
    header: ({ column }) => <SortableHeader column={column}>AI calls</SortableHeader>,
    sortDescFirst: true,
    cell: ({ row }) => (
      <span className="whitespace-nowrap">
        <span className="num">{row.original.calls}</span>
        {row.original.failed > 0 && <span className="text-xs text-muted-foreground"> · <span className="num">{row.original.failed}</span> failed</span>}
      </span>
    ),
  },
  {
    id: 'tokens',
    accessorFn: (row) => row.inputTokens + row.outputTokens,
    header: 'Tokens in / out',
    enableSorting: false,
    meta: { className: 'hidden md:table-cell' },
    cell: ({ row }) => <span className="num text-xs whitespace-nowrap">{row.original.inputTokens.toLocaleString()} / {row.original.outputTokens.toLocaleString()}</span>,
  },
  {
    id: 'models',
    accessorFn: (row) => row.models.join(' '),
    header: 'Models',
    enableSorting: false,
    meta: { className: 'hidden lg:table-cell' },
    cell: ({ row }) => <span className="text-xs text-muted-foreground">{row.original.models.map(modelWord).join(' · ')}</span>,
  },
  {
    id: 'cost',
    // A set nobody priced sorts below every priced one, never as the cheapest.
    accessorFn: (row) => (row.calls > 0 && row.unpriced >= row.calls ? -1 : Number(row.cost)),
    header: ({ column }) => <SortableHeader column={column}>Cost</SortableHeader>,
    sortDescFirst: true,
    cell: ({ row }) => (
      <span className="whitespace-nowrap" title={`$${row.original.cost}${row.original.unpriced > 0 ? `, ${row.original.unpriced} calls not priced` : ''}`}>
        <span className="num">{costText(row.original.cost, row.original.calls, row.original.unpriced)}</span>
      </span>
    ),
  },
];

export function UsagePage() {
  const [attempt, setAttempt] = useState(0);
  const usage = useAsync(loadUsage, [attempt]);

  return (
    <PageFrame title="Usage" description="What this project's AI reading has cost, and what the checks found.">
      {usage.status === 'loading' && <PageLoading>Counting…</PageLoading>}

      {/* Failure and emptiness must not look alike. Zeroes on a screen that could not reach the
          server would read as "nothing has been spent", which is a claim nobody made. */}
      {usage.status === 'error' && (
        <PageLoadError title="Usage could not be loaded" message={`${usage.error.message} These figures are unavailable; this is not a report of zero usage.`} onRetry={() => setAttempt((value) => value + 1)} />
      )}

      {usage.status === 'ready' && (() => {
        const { byDay, bySet, summary } = usage.data;
        const kpis = usageKpis(bySet.totals, bySet.groups);
        const days = costByDay(byDay.groups);
        const sets = usageBySet(bySet.groups, summary.items);
        const outcomeDays = outcomesByUploadDay(summary.items);
        const recorded = resultTotal(outcomeTotals(outcomeDays));
        const allUnpriced = kpis.calls > 0 && kpis.unpricedCalls >= kpis.calls;
        return (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-part="kpis">
              <Kpi label="Sets with AI calls" value={<span className="num">{kpis.setsWithCalls}</span>} sub="drawing sets (reading or chat)" />
              <Kpi
                label="AI calls"
                value={<span className="num">{kpis.calls.toLocaleString()}</span>}
                sub={kpis.failedCalls > 0 ? <><span className="num">{kpis.failedCalls}</span> failed</> : 'none failed'}
              />
              <Kpi
                label="Cost"
                value={allUnpriced
                  ? <span className="text-base font-medium text-muted-foreground">Not priced</span>
                  : <span className="num" title={`$${kpis.cost}`}>{costText(kpis.cost, kpis.calls, kpis.unpricedCalls)}</span>}
                sub={kpis.calls === 0
                  ? 'no AI calls yet'
                  : allUnpriced
                    ? 'no call has a recorded price'
                    : kpis.unpricedCalls > 0
                      ? <><span className="num">{kpis.unpricedCalls}</span> {kpis.unpricedCalls === 1 ? 'call has' : 'calls have'} no price, so the real cost is higher</>
                      : 'every call priced'}
              />
              <Kpi
                label="Reading time per set"
                value={<span className="text-base font-medium text-muted-foreground">Not measured yet</span>}
                tip={(
                  <InfoTip label="Why reading time is not measured">
                    <p>The AI calls of one reading are saved together when it finishes, so their times cannot say how long it took. A real start-to-finish time per set has been asked of the backend.</p>
                  </InfoTip>
                )}
              />
            </div>

            {kpis.calls === 0 ? (
              <p className="text-sm text-muted-foreground">No AI calls are recorded in this project yet.</p>
            ) : (
              <section aria-labelledby="cost-by-day" className="flex flex-col gap-2 rounded-xl border p-4">
                <h2 id="cost-by-day" className="flex items-center gap-1.5 text-base font-semibold">
                  Cost by day
                  <InfoTip label="About cost by day">
                    <p>The cost of the AI calls made each day (UTC), from the prices recorded with each call. A call with no price adds nothing here; the Cost card says if there are any.</p>
                  </InfoTip>
                </h2>
                <Suspense fallback={<Skeleton className="h-48 w-full" />}>
                  <CostByDayChart days={days} />
                </Suspense>
                {days.some((day) => day.unpriced > 0) && (
                  <p className="text-xs text-muted-foreground">Some calls have no price; on those days the bar shows at least the cost.</p>
                )}
              </section>
            )}

            {sets.length > 0 && (
              <section aria-labelledby="cost-by-set" className="flex flex-col gap-2">
                <h2 id="cost-by-set" className="text-base font-semibold">By drawing set</h2>
                <DataTable label="AI usage by drawing set" columns={SET_COLUMNS} data={sets} getRowId={(row) => row.packageId} initialSorting={[{ id: 'cost', desc: true }]} />
                {sets.some((set) => set.vendor === null) && (
                  <p className="text-xs text-muted-foreground">* Not among the newest {summary.items.length} drawing sets loaded here, so shown by its id.</p>
                )}
              </section>
            )}

            <section aria-labelledby="outcomes-by-day" className="flex flex-col gap-2 rounded-xl border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 id="outcomes-by-day" className="flex items-center gap-1.5 text-base font-semibold">
                  Recorded results over time
                  <InfoTip label="About recorded results over time">
                    <p>Each drawing set&apos;s recorded results, added up by the day it was uploaded (UTC). These count checks, not drawings or dimensions, and a reviewer&apos;s decision does not change them.</p>
                  </InfoTip>
                </h2>
              </div>
              {summary.next_cursor && (
                <p className="text-xs text-muted-foreground">Only the newest <span className="num">{summary.items.length}</span> drawing sets are counted here.</p>
              )}
              {outcomeDays.length === 0 ? (
                <p className="text-sm text-muted-foreground">No drawing sets yet.</p>
              ) : recorded === 0 ? (
                <p className="text-sm text-muted-foreground">No recorded results yet.</p>
              ) : (
                <Suspense fallback={<Skeleton className="h-48 w-full" />}>
                  <OutcomesByDayChart days={outcomeDays} />
                </Suspense>
              )}
            </section>
          </div>
        );
      })()}
    </PageFrame>
  );
}
