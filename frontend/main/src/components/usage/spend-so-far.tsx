import type { ReactNode } from 'react';
import type { ColumnDef } from '@tanstack/react-table';

import type { EarlierRun, ModelSpend, SpendTotals, UsageHistory, UsageProviderCheck } from '@/api/client';
import { DataTable, SortableHeader } from '@/components/data-table/data-table';
import { InfoTip } from '@/components/ui/info-tip';
import { compactCount, costText, formatUsd, fullDayLabel, modelName, purposeWord, routeWord, shortDayLabel } from '@/lib/usage';

/**
 * "Spend so far" (#1165): every AI call recorded for this project, plus earlier runs recorded in other
 * local databases (reading runs, bake-offs, proofs), from `GET /usage/history`. Nothing is computed
 * here but words: totals, per-model rows and earlier runs are the API's. A call with no recorded
 * price is counted as such and never shown as $0; a call from before costs were recorded (#754) is
 * priced from the published rates and the page says how many.
 */

/** "Not priced" when no call has a price, or when the priced part is $0 but some calls have none:
 * "at least $0.00" would read as free. */
function notPriced(row: SpendTotals): boolean {
  return (row.calls > 0 && row.unpriced_calls >= row.calls) || (row.unpriced_calls > 0 && Number(row.cost_usd) === 0);
}

/** Sort key for a cost: a row nobody priced sorts below every priced one, never as the cheapest. */
function costSortKey(row: SpendTotals): number {
  return row.calls > 0 && row.unpriced_calls >= row.calls ? -1 : Number(row.cost_usd);
}

/** A cost in words and figures: the words in the interface face, only the figure in Plex Mono. */
function Cost({ row }: { row: SpendTotals }) {
  const text = costText(row.cost_usd, row.calls, row.unpriced_calls);
  const title = `$${row.cost_usd}${row.unpriced_calls > 0 ? `, ${row.unpriced_calls} calls not priced` : ''}`;
  if (notPriced(row)) return <span className="whitespace-nowrap text-muted-foreground" title={title}>Not priced</span>;
  const lowerBound = text.startsWith('at least ');
  return (
    <span className="whitespace-nowrap" title={title}>
      {lowerBound && <span className="text-muted-foreground">at least </span>}
      <span className="num">{lowerBound ? text.slice('at least '.length) : text}</span>
    </span>
  );
}

/** The model in words with its raw id; with `route`, the route under it on narrow screens (where
 * the route column is hidden), so two rows of the same model can be told apart. */
function ModelLabel({ model, route }: { model: string; route?: ModelSpend['route'] }) {
  const name = modelName(model);
  const label = name === null
    ? <span className="num inline-block max-w-[10rem] truncate align-bottom @2xl:max-w-[18rem]" title={model}>{model}</span>
    : (
      <span className="inline-flex min-w-0 items-baseline gap-2 whitespace-nowrap">
        <span className="font-medium">{name}</span>
        <span className="num hidden max-w-[6rem] truncate align-bottom text-xs text-muted-foreground @sm:inline-block @3xl:max-w-[10rem] @5xl:max-w-[16rem]" title={model}>{model}</span>
      </span>
    );
  if (route === undefined) return label;
  return (
    <span className="flex flex-col">
      {label}
      <span className="text-xs text-muted-foreground @3xl:hidden" data-part="route-under-model">{routeWord(route)}</span>
    </span>
  );
}

function modelColumns(models: readonly ModelSpend[]): ColumnDef<ModelSpend>[] {
  const seen = new Map<string, number>();
  for (const row of models) seen.set(row.model, (seen.get(row.model) ?? 0) + 1);
  return [
  {
    id: 'model',
    accessorFn: (row) => modelName(row.model) ?? row.model,
    header: ({ column }) => <SortableHeader column={column}>Model</SortableHeader>,
    cell: ({ row }) => <ModelLabel model={row.original.model} route={(seen.get(row.original.model) ?? 0) > 1 ? row.original.route : undefined} />,
  },
  {
    id: 'route',
    accessorFn: (row) => routeWord(row.route),
    header: 'Through',
    enableSorting: false,
    meta: { className: 'hidden @3xl:table-cell' },
    cell: ({ row }) => <span className={row.original.route === 'unknown' ? 'whitespace-nowrap text-muted-foreground' : 'whitespace-nowrap'}>{routeWord(row.original.route)}</span>,
  },
  {
    id: 'calls',
    accessorFn: (row) => row.calls,
    header: ({ column }) => <SortableHeader column={column}>Calls</SortableHeader>,
    sortDescFirst: true,
    meta: { className: 'hidden @sm:table-cell' },
    cell: ({ row }) => <span className="num">{row.original.calls.toLocaleString()}</span>,
  },
  {
    id: 'tokens',
    accessorFn: (row) => row.input_tokens + row.output_tokens,
    header: 'Tokens in / out',
    enableSorting: false,
    meta: { className: 'hidden @5xl:table-cell' },
    cell: ({ row }) => (
      <span className="num whitespace-nowrap" title={`${row.original.input_tokens.toLocaleString()} in, ${row.original.output_tokens.toLocaleString()} out`}>
        {compactCount(row.original.input_tokens)} / {compactCount(row.original.output_tokens)}
      </span>
    ),
  },
  {
    id: 'cost',
    accessorFn: costSortKey,
    header: ({ column }) => <SortableHeader column={column}>Cost</SortableHeader>,
    sortDescFirst: true,
    cell: ({ row }) => <Cost row={row.original} />,
  },
  {
    id: 'unpriced',
    accessorFn: (row) => row.unpriced_calls,
    header: 'No price',
    enableSorting: false,
    meta: { className: 'hidden @lg:table-cell' },
    cell: ({ row }) => (
      <span className={row.original.unpriced_calls > 0 ? 'num' : 'num text-muted-foreground'}>{row.original.unpriced_calls.toLocaleString()}</span>
    ),
  },
  ];
}

const RUN_COLUMNS: ColumnDef<EarlierRun>[] = [
  {
    id: 'day',
    accessorFn: (row) => row.day,
    header: ({ column }) => <SortableHeader column={column}>Date</SortableHeader>,
    sortDescFirst: true,
    cell: ({ row }) => <span className="whitespace-nowrap" title={fullDayLabel(row.original.day)}>{shortDayLabel(row.original.day)}</span>,
  },
  {
    id: 'purpose',
    accessorFn: (row) => purposeWord(row.purpose),
    header: 'What for',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="inline-block max-w-[7rem] truncate align-bottom whitespace-nowrap @lg:max-w-none" title={`${purposeWord(row.original.purpose)} (${row.original.source_label})`}>
        {purposeWord(row.original.purpose)}
      </span>
    ),
  },
  {
    id: 'models',
    accessorFn: (row) => row.models.join(' '),
    header: 'Models',
    enableSorting: false,
    meta: { className: 'hidden @2xl:table-cell' },
    cell: ({ row }) => {
      const words = row.original.models.map((model) => modelName(model) ?? model).join(' · ');
      return <span className="inline-block max-w-[22rem] truncate align-bottom whitespace-nowrap text-muted-foreground" title={row.original.models.join(', ')}>{words}</span>;
    },
  },
  {
    id: 'calls',
    accessorFn: (row) => row.calls,
    header: 'Calls',
    enableSorting: false,
    meta: { className: 'hidden @md:table-cell' },
    cell: ({ row }) => <span className="num">{row.original.calls.toLocaleString()}</span>,
  },
  {
    id: 'cost',
    accessorFn: costSortKey,
    header: ({ column }) => <SortableHeader column={column}>Cost</SortableHeader>,
    sortDescFirst: true,
    cell: ({ row }) => <Cost row={row.original} />,
  },
];

function TotalLine({ label, totals }: { label: string; totals: SpendTotals }) {
  return (
    <span className="whitespace-nowrap">
      {label} <Cost row={totals} />
    </span>
  );
}

/** What OpenRouter reports this deployment's key has used: every project using the key, so it is a
 * cross-check, not this project's figure. Loaded on its own; nothing is shown while it loads. */
export function ProviderLine({ check }: { check: UsageProviderCheck }) {
  if (check.status !== 'ok' || check.used_usd === null) {
    return <p className="text-sm text-muted-foreground" data-part="provider-check">OpenRouter key usage (all projects) could not be read just now.</p>;
  }
  return (
    <p className="text-sm text-muted-foreground" data-part="provider-check">
      OpenRouter key usage (all projects): <span className="num text-foreground" title={`$${check.used_usd}`}>{formatUsd(check.used_usd)}</span>
    </p>
  );
}

export function SpendSoFar({ history, providerCheck }: { history: UsageHistory; providerCheck?: ReactNode }) {
  const { totals, this_project: thisProject, earlier } = history;
  const allUnpriced = notPriced(totals);
  const labels = [...new Set(history.earlier_runs.map((run) => run.source_label))];
  return (
    <section aria-labelledby="spend-so-far" data-part="spend-so-far" className="@container flex flex-col gap-4 rounded-xl border p-4">
      <div className="flex flex-col gap-1">
        <h2 id="spend-so-far" className="flex items-center gap-1.5 text-base font-semibold">
          Spend so far
          <InfoTip label="About spend so far">
            <p>Every AI call recorded for this project&apos;s reviews, plus earlier runs on this machine that are not tied to this project&apos;s reviews (reading runs, model comparisons, proofs). Each call is counted once.</p>
            <p>The cost adds up the calls that have a price. A call with no recorded price is counted, not treated as free, so the real cost is higher when there are any. Calls made before costs were recorded (30 Sept 2026) are priced from the published price list.</p>
          </InfoTip>
        </h2>
        <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1" data-part="all-time-total">
          {allUnpriced ? (
            <span className="text-2xl font-semibold text-muted-foreground">Not priced</span>
          ) : (
            <span className="flex items-baseline gap-2">
              {totals.unpriced_calls > 0 && <span className="text-sm text-muted-foreground">at least</span>}
              <span className="num text-3xl font-semibold tracking-tight" title={`$${totals.cost_usd}`}>{formatUsd(totals.cost_usd)}</span>
            </span>
          )}
          <span className="text-sm text-muted-foreground"><span className="num">{totals.calls.toLocaleString()}</span> AI calls in all</span>
        </p>
        <p className="flex flex-wrap gap-x-3 gap-y-1 text-sm text-muted-foreground" data-part="spend-split">
          <TotalLine label="This project's reviews" totals={thisProject} />
          <span aria-hidden="true">·</span>
          <TotalLine label="Earlier runs" totals={earlier} />
        </p>
        {totals.unpriced_calls > 0 && (
          <p className="text-sm" data-part="unpriced">
            <span className="num">{totals.unpriced_calls.toLocaleString()}</span> {totals.unpriced_calls === 1 ? 'call has' : 'calls have'} no price, so the real cost is higher.
          </p>
        )}
        {totals.priced_later_calls > 0 && (
          <p className="text-sm text-muted-foreground" data-part="priced-later">
            <span className="num">{totals.priced_later_calls.toLocaleString()}</span> {totals.priced_later_calls === 1 ? 'call' : 'calls'} from before costs were recorded {totals.priced_later_calls === 1 ? 'is' : 'are'} priced from published rates (<span className="num">{formatUsd(totals.priced_later_cost_usd)}</span> of the total).
          </p>
        )}
        {providerCheck}
      </div>

      {history.by_model.length > 0 && (
        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">By model</h3>
          <DataTable label="AI spend by model" columns={modelColumns(history.by_model)} data={[...history.by_model]} getRowId={(row) => `${row.model}|${row.route}`} initialSorting={[{ id: 'cost', desc: true }]} />
        </div>
      )}

      <div className="flex flex-col gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          Earlier runs
          {labels.length > 0 && (
            <InfoTip label="Where earlier runs come from">
              <p>Calls recorded in other databases on this machine, not tied to this project&apos;s reviews (reading runs, model comparisons, proofs), grouped by day and what they were for. Source: {labels.join('; ')}.</p>
            </InfoTip>
          )}
        </h3>
        {history.earlier_runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">No earlier runs are recorded for this project.</p>
        ) : (
          <DataTable label="Earlier AI runs" columns={RUN_COLUMNS} data={[...history.earlier_runs]} getRowId={(row) => `${row.day}|${row.purpose}|${row.source_label}`} initialSorting={[{ id: 'day', desc: true }]} />
        )}
      </div>
    </section>
  );
}
