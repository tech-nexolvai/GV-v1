import { useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { ArrowRight, Search } from 'lucide-react';

import type { PackageSummary } from '@/api/client';
import { DataTable, SortableHeader } from '@/components/data-table/data-table';
import { SetCounts, SetCountsLegend, SetResultLine } from '@/components/documents/set-result';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { matchesSearch, productWord, resultRank } from '@/lib/documents-table';
import { drawingsWords, ranWords } from '@/lib/set-check';

const UNTITLED = 'Untitled document set';

/** Where a row opens: its newest full check's chat, else the set's chats. */
export type OpenSet = (packageId: string, chatId: string | null) => void;

function open(row: PackageSummary, onOpen: OpenSet) {
  onOpen(row.package_id, row.full_check?.chat_id ?? null);
}

/** When the newest full check ended, in Plex Mono; "—" (with words for a reader) before the first. */
function Ran({ row }: { row: PackageSummary }) {
  const check = row.full_check;
  if (!check) {
    return <span className="text-muted-foreground"><span aria-hidden="true">—</span><span className="sr-only">Not checked yet</span></span>;
  }
  return (
    <time className="num text-xs whitespace-nowrap" dateTime={check.ran_at}>
      {ranWords(check.ran_at)}
    </time>
  );
}

/** The review result and, before any check, where the drawings are (the set pack). */
function Result({ row }: { row: PackageSummary }) {
  const drawings = row.full_check || row.checking ? null : drawingsWords(row.drawings);
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <SetResultLine set={row} />
      {drawings && <span data-slot="set-drawings" className="text-xs text-muted-foreground">{drawings}</span>}
    </div>
  );
}

function columns(onOpen: OpenSet): ColumnDef<PackageSummary>[] {
  return [
    {
      id: 'vendor',
      accessorFn: (row) => row.vendor ?? undefined,
      header: ({ column }) => <SortableHeader column={column}>Drawing set</SortableHeader>,
      // Named sets A→Z (or Z→A), ignoring case; untitled sets stay last either way.
      sortUndefined: 'last',
      sortingFn: (a, b) => (a.original.vendor ?? '').localeCompare(b.original.vendor ?? '', undefined, { sensitivity: 'base' }),
      // The name opens the newest check too, so on a phone (no Open column) it is one tap away; there
      // the cell also carries the result, the counts and the date (the other columns are hidden).
      cell: ({ row }) => (
        <div className="flex min-w-0 flex-col gap-1.5">
          <button
            type="button"
            onClick={() => open(row.original, onOpen)}
            className="flex min-w-32 flex-col rounded-sm text-left hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            <span className="font-medium">{row.original.vendor ?? UNTITLED}</span>
            <span className="text-xs text-muted-foreground">Revision <span className="num">{row.original.revision_number}</span></span>
          </button>
          <div data-slot="documents-phone-summary" className="flex flex-col gap-1 sm:hidden">
            <Result row={row.original} />
            {row.original.full_check && <SetCounts counts={row.original.full_check.counts} />}
            {row.original.full_check && <Ran row={row.original} />}
          </div>
        </div>
      ),
    },
    {
      id: 'product',
      accessorFn: (row) => productWord(row.product_type),
      header: 'Product',
      enableSorting: false,
      meta: { className: 'hidden lg:table-cell' },
    },
    {
      id: 'result',
      accessorFn: (row) => resultRank(row),
      header: ({ column }) => <SortableHeader column={column}>Review result</SortableHeader>,
      sortDescFirst: false,
      meta: { className: 'hidden sm:table-cell' },
      cell: ({ row }) => <Result row={row.original} />,
    },
    {
      id: 'counts',
      header: 'Items',
      enableSorting: false,
      meta: { className: 'hidden sm:table-cell' },
      cell: ({ row }) =>
        row.original.full_check ? (
          <SetCounts counts={row.original.full_check.counts} />
        ) : (
          <span className="text-muted-foreground"><span aria-hidden="true">—</span><span className="sr-only">No items yet</span></span>
        ),
    },
    {
      id: 'checked',
      accessorFn: (row) => (row.full_check ? Date.parse(row.full_check.ran_at) : undefined),
      header: ({ column }) => <SortableHeader column={column}>Checked</SortableHeader>,
      sortDescFirst: true,
      sortUndefined: 'last',
      meta: { className: 'hidden md:table-cell' },
      cell: ({ row }) => <Ran row={row.original} />,
    },
    {
      id: 'open',
      header: () => <span className="sr-only">Open</span>,
      enableSorting: false,
      meta: { className: 'hidden sm:table-cell' },
      cell: ({ row }) => (
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label={row.original.full_check ? `Open the full check of ${row.original.vendor ?? UNTITLED}` : `Open the chats of ${row.original.vendor ?? UNTITLED}`}
          onClick={() => open(row.original, onOpen)}
        >
          {/* The word only on very wide screens: at laptop widths the arrow alone keeps the column inside the table. */}
          <span className="hidden 2xl:inline">Open</span> <ArrowRight aria-hidden="true" />
        </Button>
      ),
    },
  ];
}

/**
 * The Documents table (#1064): one row per drawing set from `packages-summary`, showing its newest
 * full check (the chat's one-click check): the review result, the counts its result screen shows,
 * when it ran, and a way into that chat. Sort and search act on the rows of the page that is loaded
 * (the API pages by cursor), and the table says so when there is more than one page. Rows come in
 * the server's order: newest change first, a full check counting as a change.
 */
export function DocumentsTable({
  rows,
  onOpen,
  morePages,
  emptyMessage = 'No reviews yet.',
}: {
  rows: readonly PackageSummary[];
  onOpen: OpenSet;
  /** True when other pages exist, so sort and search say they cover this page only. */
  morePages: boolean;
  /** What an empty page says (the first page and a later one differ). */
  emptyMessage?: string;
}) {
  const [query, setQuery] = useState('');
  const shown = rows.filter((row) => matchesSearch(row, query));
  const searching = query.trim() !== '';
  const askArchitect = rows.some((row) => (row.full_check?.counts.ask_architect ?? 0) > 0);

  return (
    <div data-tw data-slot="documents-table" className="flex flex-col gap-3 font-sans">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full sm:max-w-xs">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search vendor or product"
            aria-label="Search vendor or product"
            className="pl-8"
          />
        </div>
        <SetCountsLegend askArchitect={askArchitect} />
      </div>
      <p className="text-xs text-muted-foreground" role="status" aria-live="polite">
        {searching ? `${shown.length} of ${rows.length} drawing sets match.` : `${rows.length} ${rows.length === 1 ? 'drawing set' : 'drawing sets'}.`}
        {' '}Each shows its newest full check.
        {morePages && ' Sorting and search cover this page only.'}
      </p>
      <DataTable
        label="Drawing sets"
        columns={columns(onOpen)}
        data={shown}
        getRowId={(row) => row.package_id}
        emptyMessage={searching ? `Nothing matches “${query.trim()}”.` : emptyMessage}
      />
    </div>
  );
}
