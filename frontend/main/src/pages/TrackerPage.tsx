/**
 * The job tracker (#1578): every item GV Review found across the project's drawing sets, rounds and
 * checks, and the vendor scorecard. A project manager reads where things stand without opening a PDF.
 *
 * Built by code from the saved chat answers and the reviewer's decisions (`GET /tracker`); $0, the
 * same numbers every time. Clicking an item opens its finding in its chat. The workbook download
 * carries the same filters as the screen.
 *
 * Design (spec in the PR): one 70rem column; four numbers once; the items table is the main thing;
 * sets and the scorecard below it. Greys throughout; red only for Wrong and green only for Fixed,
 * always beside the word. Words in Geist, numbers in Plex Mono (`num`).
 */

import { useMemo, useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { Download, Search } from 'lucide-react';

import { getTracker, trackerExportUrl, type Tracker, type TrackerItem, type TrackerSet, type TrackerVendor } from '@/api/client';
import { projectId } from '@/api/config';
import { useAsync } from '@/api/useAsync';
import { DataTable, SortableHeader } from '@/components/data-table/data-table';
import { PageFrame, PageLoadError } from '@/components/ui/PageFrame';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import {
  dayWords,
  filterItems,
  fixedNextWords,
  NO_FILTERS,
  roundWords,
  setRoundsWords,
  stateCounts,
  stateRank,
  stateWords,
  valuesWords,
  type StateFilter,
  type Tone,
  type TrackerFilters,
} from '@/lib/tracker';

type Link = TrackerItem['link'];

/** Rows shown before "Show all": a job can have hundreds of items. */
const FIRST_ROWS = 25;

export interface TrackerPageProps {
  /** Open the item's finding in its chat. */
  onOpenItem: (link: Link) => void;
  /** Open a drawing set's chats. */
  onOpenSet: (packageId: string) => void;
  /** Tests pass a loader; the page asks the API otherwise. */
  load?: () => Promise<Tracker>;
}

const DOT: Record<Tone, string> = {
  wrong: 'bg-outcome-fail',
  right: 'bg-outcome-pass',
  neutral: 'bg-transparent ring-[1.5px] ring-inset ring-muted-foreground/60',
};

const WORD: Record<Tone, string> = {
  wrong: 'text-outcome-fail-fg font-medium',
  right: 'text-outcome-pass-fg font-medium',
  neutral: 'text-muted-foreground',
};

function StateMark({ item }: { item: TrackerItem }) {
  const words = stateWords(item);
  return (
    <span data-slot="tracker-state" data-tone={words.tone} className="inline-flex flex-col gap-0.5">
      <span className={cn('inline-flex items-center gap-1.5 text-[13px] whitespace-nowrap', WORD[words.tone])}>
        <span aria-hidden="true" className={cn('inline-block size-2 shrink-0 rounded-full', DOT[words.tone])} />
        {words.word}
      </span>
      {words.detail && <span className="pl-3.5 text-xs text-muted-foreground">{words.detail}</span>}
    </span>
  );
}

function itemColumns(onOpenItem: (link: Link) => void, oneSet: boolean): ColumnDef<TrackerItem>[] {
  return [
    {
      id: 'state',
      accessorFn: (row) => stateRank(row),
      header: ({ column }) => <SortableHeader column={column}>State</SortableHeader>,
      cell: ({ row }) => <StateMark item={row.original} />,
      meta: { className: 'w-[11.5rem] align-top' },
    },
    {
      id: 'item',
      accessorFn: (row) => row.item,
      header: ({ column }) => <SortableHeader column={column}>Item</SortableHeader>,
      cell: ({ row }) => {
        const item = row.original;
        return (
          <span className="grid min-w-0 content-start gap-1">
            <button
              type="button"
              onClick={(event) => { event.stopPropagation(); onOpenItem(item.link); }}
              title={`${item.item}: open its finding in the chat`}
              className="flex w-full min-w-0 items-start rounded-sm text-left text-[13.5px] leading-snug font-medium [overflow-wrap:anywhere] hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              <span className="line-clamp-2">{item.item}</span>
            </button>
            <span className="text-xs text-muted-foreground">
              {item.kind}
              {item.tags.map((tag) => <span key={tag} data-slot="tracker-tag"> · {tag}</span>)}
            </span>
            {/* Phones: what the hidden columns say, on one quiet line. */}
            <span className="text-xs text-muted-foreground md:hidden">
              {[oneSet ? null : item.set_name, roundWords(item), item.page != null ? `p.${item.page}` : null].filter(Boolean).join(' · ')}
            </span>
          </span>
        );
      },
      meta: { className: 'min-w-[14rem] whitespace-normal align-top' },
    },
    ...(oneSet
      ? []
      : [
          {
            id: 'set',
            accessorFn: (row: TrackerItem) => row.set_name,
            header: ({ column }) => <SortableHeader column={column}>Drawing set</SortableHeader>,
            cell: ({ row }) => <span className="line-clamp-2 text-[13px] whitespace-normal">{row.original.set_name}</span>,
            meta: { className: 'hidden md:table-cell max-w-[12rem] align-top' },
          } satisfies ColumnDef<TrackerItem>,
        ]),
    {
      id: 'round',
      accessorFn: (row) => row.first_round * 100 + row.round_now,
      header: ({ column }) => <SortableHeader column={column}>Round</SortableHeader>,
      cell: ({ row }) => <span className="num text-[13px] whitespace-nowrap">{roundWords(row.original)}</span>,
      meta: { className: 'hidden md:table-cell align-top' },
    },
    {
      id: 'page',
      accessorFn: (row) => row.page ?? 0,
      header: ({ column }) => <SortableHeader column={column}>Page</SortableHeader>,
      cell: ({ row }) => (row.original.page != null ? <span className="num text-[13px]">p.{row.original.page}</span> : null),
      meta: { className: 'hidden md:table-cell align-top' },
    },
    {
      id: 'values',
      accessorFn: (row) => valuesWords(row),
      header: () => <span className="inline-flex h-8 items-center">Vendor drew</span>,
      enableSorting: false,
      cell: ({ row }) => {
        const item = row.original;
        return (
          <span className="grid gap-0.5 text-[13px]">
            <span className="num line-clamp-1 [overflow-wrap:anywhere]" title={item.vendor_value}>{item.vendor_value || '—'}</span>
            {item.architect_value && <span className="line-clamp-2 text-xs text-muted-foreground [overflow-wrap:anywhere]" title={item.architect_value}>should be <span className="num">{item.architect_value}</span></span>}
          </span>
        );
      },
      meta: { className: 'hidden lg:table-cell w-[11rem] max-w-[13rem] whitespace-normal align-top' },
    },
    {
      id: 'checks',
      accessorFn: (row) => row.checks_seen,
      header: ({ column }) => <SortableHeader column={column}>Checks</SortableHeader>,
      sortDescFirst: true,
      cell: ({ row }) => <span className="num text-[13px]">{row.original.checks_seen}</span>,
      meta: { className: 'hidden lg:table-cell align-top text-right' },
    },
    {
      id: 'last',
      accessorFn: (row) => row.last_seen_at,
      header: ({ column }) => <SortableHeader column={column}>Last seen</SortableHeader>,
      sortDescFirst: true,
      cell: ({ row }) => <span className="text-[13px] whitespace-nowrap text-muted-foreground">{dayWords(row.original.last_seen_at)}</span>,
      meta: { className: 'hidden md:table-cell align-top' },
    },
  ];
}

function Numbers({ tracker }: { tracker: Tracker }) {
  const { totals } = tracker;
  const cells: { label: string; value: number; sub: React.ReactNode }[] = [
    {
      label: 'Open now',
      value: totals.open,
      sub: (
        <>
          <span aria-hidden="true" className="mr-1 inline-block size-1.5 translate-y-[-1px] rounded-full bg-outcome-fail" />
          <span className="num">{totals.open_wrong}</span> wrong · <span className="num">{totals.open_decision}</span> your decision
        </>
      ),
    },
    {
      label: 'Fixed',
      value: totals.fixed,
      sub: 'right in a later round',
    },
    { label: 'Not a problem', value: totals.not_a_problem, sub: "a reviewer's call" },
    {
      label: 'Drawing sets',
      value: totals.sets,
      sub: (
        <>
          <span className="num">{totals.checks}</span> {totals.checks === 1 ? 'check' : 'checks'} with items
        </>
      ),
    },
  ];
  return (
    <dl data-slot="tracker-numbers" className="grid grid-cols-2 overflow-hidden rounded-xl border lg:grid-cols-4">
      {cells.map((cell, at) => (
        <div key={cell.label} className={cn('flex flex-col gap-1 p-4', at % 2 === 1 && 'border-l', at >= 2 && 'border-t lg:border-t-0', at === 2 && 'lg:border-l')}>
          <dt className="text-[13px] text-muted-foreground">{cell.label}</dt>
          <dd className="num text-2xl font-medium tracking-tight">{cell.value}</dd>
          <dd className="text-xs text-muted-foreground">{cell.sub}</dd>
        </div>
      ))}
    </dl>
  );
}

const STATE_BUTTONS: { id: StateFilter; label: string }[] = [
  { id: 'open', label: 'Open' },
  { id: 'fixed', label: 'Fixed' },
  { id: 'not_a_problem', label: 'Not a problem' },
  { id: 'all', label: 'All' },
];

function FilterBar({ tracker, filters, onChange }: { tracker: Tracker; filters: TrackerFilters; onChange: (next: TrackerFilters) => void }) {
  const counts = stateCounts(tracker.items, filters);
  const vendors = tracker.vendors;
  const selectClass = 'h-8 max-w-[14rem] min-w-0 truncate rounded-lg border bg-background px-2 text-[13px] focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none';
  return (
    <div data-slot="tracker-filters" className="flex flex-wrap items-center gap-2">
      <div role="group" aria-label="Which items" className="inline-flex rounded-lg border bg-muted/40 p-0.5">
        {STATE_BUTTONS.map((button) => {
          const on = filters.state === button.id;
          return (
            <button
              key={button.id}
              type="button"
              aria-pressed={on}
              onClick={() => onChange({ ...filters, state: button.id })}
              className={cn(
                'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[13px] whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none',
                on ? 'bg-background font-medium shadow-xs' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {button.label}
              <span className="num text-xs text-muted-foreground">{counts[button.id]}</span>
            </button>
          );
        })}
      </div>
      {tracker.sets.length > 1 && (
        <select aria-label="Drawing set" className={selectClass} value={filters.packageId ?? ''} onChange={(event) => onChange({ ...filters, packageId: event.target.value || null })}>
          <option value="">Every drawing set</option>
          {tracker.sets.map((set) => <option key={set.package_id} value={set.package_id}>{set.name}</option>)}
        </select>
      )}
      {vendors.length > 1 && (
        <select aria-label="Vendor" className={selectClass} value={filters.vendorKey ?? ''} onChange={(event) => onChange({ ...filters, vendorKey: event.target.value || null })}>
          <option value="">Every vendor</option>
          {vendors.map((vendor) => <option key={vendor.vendor_key} value={vendor.vendor_key}>{vendor.vendor_name}</option>)}
        </select>
      )}
      <label className="relative ml-auto flex w-full min-w-0 items-center sm:w-64">
        <Search aria-hidden="true" className="pointer-events-none absolute left-2.5 size-3.5 text-muted-foreground" />
        <input
          type="search"
          value={filters.text}
          onChange={(event) => onChange({ ...filters, text: event.target.value })}
          placeholder="Find an item, page or value"
          aria-label="Find an item, page or value"
          className="h-8 w-full rounded-lg border bg-background pr-2 pl-8 text-[13px] placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        />
      </label>
    </div>
  );
}

function SetsList({ sets, onOpenSet }: { sets: readonly TrackerSet[]; onOpenSet: (packageId: string) => void }) {
  return (
    <ul data-slot="tracker-sets" className="divide-y rounded-xl border">
      {sets.map((set) => {
        const checks = set.checks.length;
        return (
          <li key={set.package_id} className="grid gap-1 px-4 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center md:gap-6">
            <span className="grid min-w-0 gap-0.5">
              <button
                type="button"
                onClick={() => onOpenSet(set.package_id)}
                className="min-w-0 truncate rounded-sm text-left text-[14px] font-medium hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                title={`${set.name}: open its chats`}
              >
                {set.name}
              </button>
              <span className="num text-xs text-muted-foreground">{setRoundsWords(set)}</span>
            </span>
            <span className="flex flex-wrap items-baseline gap-x-4 gap-y-0.5 text-[13px] text-muted-foreground md:justify-end">
              <span><span className="num text-foreground">{set.open}</span> open</span>
              <span><span className="num">{set.fixed}</span> fixed</span>
              <span><span className="num">{checks}</span> {checks === 1 ? 'check' : 'checks'}</span>
              <span>last {dayWords(set.last_checked_at)}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function Ranked({ title, entries }: { title: string; entries: TrackerVendor['by_kind'] }) {
  const top = entries.slice(0, 4);
  const most = Math.max(1, ...top.map((entry) => entry.count));
  return (
    <div className="grid min-w-0 content-start gap-2">
      <h4 className="text-xs text-muted-foreground">{title}</h4>
      {top.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">None yet</p>
      ) : (
        <ol className="grid gap-2">
          {top.map((entry) => (
            <li key={entry.label} className="grid gap-1">
              <span className="flex items-baseline justify-between gap-3 text-[13px]">
                <span className="min-w-0 truncate" title={entry.label}>{entry.label}</span>
                <span className="num shrink-0 text-muted-foreground">{entry.count}</span>
              </span>
              <span aria-hidden="true" className="block h-1 overflow-hidden rounded-full bg-muted">
                <span className="block h-full rounded-full bg-muted-foreground/45" style={{ width: `${Math.round((100 * entry.count) / most)}%` }} />
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function VendorCard({ vendor }: { vendor: TrackerVendor }) {
  return (
    <article data-slot="tracker-vendor" className="grid gap-5 rounded-xl border p-4 sm:p-5">
      <header className="grid gap-1">
        <h3 className="truncate text-[15px] font-semibold" title={vendor.vendor_name}>{vendor.vendor_name}</h3>
        <p className="text-xs text-muted-foreground">
          <span className="num">{vendor.sets}</span> {vendor.sets === 1 ? 'drawing set' : 'drawing sets'} · <span className="num">{vendor.rounds}</span> {vendor.rounds === 1 ? 'round' : 'rounds'} · <span className="num">{vendor.mistakes}</span> {vendor.mistakes === 1 ? 'mistake' : 'mistakes'} · <span className="num">{vendor.open}</span> open
        </p>
      </header>
      <dl className="grid gap-3 sm:grid-cols-2">
        <div className="grid gap-0.5">
          <dt className="text-xs text-muted-foreground">Fixed on the next round</dt>
          <dd className="num text-[15px] font-medium">{fixedNextWords(vendor)}</dd>
        </div>
        <div className="grid gap-0.5">
          <dt className="text-xs text-muted-foreground">Open items per round, on average</dt>
          <dd className="num text-[15px] font-medium">{vendor.average_open_per_round ?? '—'}</dd>
        </div>
      </dl>
      <div className="grid gap-5 sm:grid-cols-[repeat(3,minmax(0,1fr))] sm:gap-6">
        <Ranked title="Most by product" entries={vendor.by_product} />
        <Ranked title="Most by what it touches" entries={vendor.by_kind} />
        <Ranked title="Most by check" entries={vendor.by_check} />
      </div>
    </article>
  );
}

/** The workbook's address with the screen's filters; none when no project is configured. */
function exportUrl(filters: TrackerFilters, vendor: string | null): string | null {
  try {
    return trackerExportUrl(projectId(), { state: filters.state, packageId: filters.packageId, vendor });
  } catch {
    return null;
  }
}

function defaultLoad() {
  return getTracker(projectId());
}

export function TrackerPage({ onOpenItem, onOpenSet, load = defaultLoad }: TrackerPageProps) {
  const [attempt, setAttempt] = useState(0);
  const tracker = useAsync(load, [attempt, load]);
  const [filters, setFilters] = useState<TrackerFilters>(NO_FILTERS);
  const [showAll, setShowAll] = useState(false);
  const ready = tracker.status === 'ready' ? tracker.data : null;
  const oneSet = (ready?.sets.length ?? 0) <= 1;
  const columns = useMemo(() => itemColumns(onOpenItem, oneSet), [onOpenItem, oneSet]);
  const shown = useMemo(() => (ready ? filterItems(ready.items, filters) : []), [ready, filters]);
  const vendor = filters.vendorKey ? ready?.vendors.find((v) => v.vendor_key === filters.vendorKey)?.vendor_name ?? null : null;
  const exportHref = exportUrl(filters, vendor);

  return (
    <PageFrame
      title="Tracker"
      description="Every item GV Review found, across drawing sets, rounds and checks. Made by code from the saved checks and the reviewers' decisions, so it never forgets and never varies."
      actions={
        ready && ready.items.length > 0 && exportHref ? (
          <a
            href={exportHref}
            download
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-[13px] font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <Download aria-hidden="true" className="size-3.5" />
            Download XLSX
          </a>
        ) : null
      }
    >
      {tracker.status === 'loading' && (
        <div role="status" className="grid gap-4">
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-64 w-full rounded-xl" />
          <span className="sr-only">Gathering every item…</span>
        </div>
      )}
      {tracker.status === 'error' && (
        <PageLoadError
          title="The tracker could not be loaded"
          message={`${tracker.error.message} This is not a report of zero items.`}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      )}
      {ready && ready.totals.checks === 0 && (
        <div data-slot="tracker-empty" className="grid gap-1 rounded-xl border p-6 text-sm">
          <p className="font-medium">No checks have found items yet.</p>
          <p className="text-muted-foreground">Run a full check on a drawing set: every item it finds appears here, and later checks and rounds add to its history.</p>
        </div>
      )}
      {ready && ready.totals.checks > 0 && (
        <div className="grid gap-10">
          <Numbers tracker={ready} />

          <section aria-labelledby="tracker-items" className="grid gap-3">
            <div className="flex flex-wrap items-end justify-between gap-2">
              <h2 id="tracker-items" className="text-base font-semibold">Items</h2>
              <p className="text-xs text-muted-foreground">Click an item to open its finding in the chat.</p>
            </div>
            <FilterBar tracker={ready} filters={filters} onChange={setFilters} />
            <DataTable
              label="Items found"
              columns={columns}
              data={shown}
              getRowId={(row) => row.key}
              onRowClick={(row) => onOpenItem(row.link)}
              rowLimit={showAll ? undefined : FIRST_ROWS}
              emptyMessage={filters.text.trim() ? `Nothing matches “${filters.text.trim()}”.` : filters.state === 'open' ? 'Nothing open here.' : 'No items here.'}
            />
            {shown.length > FIRST_ROWS && (
              <div className="flex items-center justify-between gap-3 text-[13px] text-muted-foreground">
                <span>
                  Showing <span className="num">{showAll ? shown.length : FIRST_ROWS}</span> of <span className="num">{shown.length}</span>
                </span>
                <button
                  type="button"
                  onClick={() => setShowAll(!showAll)}
                  className="inline-flex h-8 items-center rounded-lg border bg-background px-3 font-medium text-foreground hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                >
                  {showAll ? `Show the first ${FIRST_ROWS}` : `Show all ${shown.length}`}
                </button>
              </div>
            )}
          </section>

          <section aria-labelledby="tracker-sets" className="grid gap-3">
            <h2 id="tracker-sets" className="text-base font-semibold">Drawing sets</h2>
            <SetsList sets={ready.sets} onOpenSet={onOpenSet} />
          </section>

          <section aria-labelledby="tracker-vendors" className="grid gap-3">
            <div className="grid gap-1">
              <h2 id="tracker-vendors" className="text-base font-semibold">Vendor scorecard</h2>
              <p className="text-xs text-muted-foreground">Mistakes are items that were Wrong at least once, less those a reviewer called not a problem. A fix counts only when the next round was checked.</p>
            </div>
            <div className={cn('grid gap-4', ready.vendors.length > 1 && 'xl:grid-cols-2')}>
              {ready.vendors.map((entry) => <VendorCard key={entry.vendor_key} vendor={entry} />)}
            </div>
          </section>
        </div>
      )}
    </PageFrame>
  );
}
