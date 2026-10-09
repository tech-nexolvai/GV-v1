import { useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { AlertCircle, ArrowRight, Check, Search } from 'lucide-react';

import type { PackageSummary } from '@/api/client';
import { DataTable, SortableHeader } from '@/components/data-table/data-table';
import { Button } from '@/components/ui/button';
import { InfoTip } from '@/components/ui/info-tip';
import { Input } from '@/components/ui/input';
import { PackageStatusBadge } from '@/components/ui/package-status-badge';
import { OutcomeBar, OutcomeLegend } from '@/components/documents/outcome-bar';
import { matchesSearch, productWord, resultTotal, statusRank } from '@/lib/documents-table';

const UNTITLED = 'Untitled document set';

/** States in which someone may still need to decide something; elsewhere "Needs you" is not asked. */
const IN_REVIEW = new Set(['AWAITING_REVIEW', 'NEEDS_INPUT', 'CHANGES_REQUESTED']);

function underReview(row: PackageSummary): boolean {
  return IN_REVIEW.has(row.state) && !row.approved;
}

function formatDay(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

function formatMoment(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/**
 * A yes/no column: a check mark, or a dash; the words are always there for a screen reader. Plain
 * colour, because the outcome colours mean results, not facts like "signed off".
 */
function YesNo({ yes, yesWord, noWord }: { yes: boolean; yesWord: string; noWord: string }) {
  return yes ? (
    <span className="inline-flex items-center text-foreground" title={yesWord}>
      <Check className="size-4" aria-hidden="true" />
      <span className="sr-only">{yesWord}</span>
    </span>
  ) : (
    <span className="text-muted-foreground" title={noWord}>
      <span aria-hidden="true">—</span>
      <span className="sr-only">{noWord}</span>
    </span>
  );
}

function columns(onOpen: (packageId: string) => void): ColumnDef<PackageSummary>[] {
  return [
    {
      id: 'vendor',
      accessorFn: (row) => row.vendor ?? undefined,
      header: ({ column }) => <SortableHeader column={column}>Vendor</SortableHeader>,
      // Named sets A→Z (or Z→A), ignoring case; untitled sets stay last either way.
      sortUndefined: 'last',
      sortingFn: (a, b) => (a.original.vendor ?? '').localeCompare(b.original.vendor ?? '', undefined, { sensitivity: 'base' }),
      // The name opens the review too, so on a phone (no Open column) it is one tap away.
      cell: ({ row }) => (
        <button
          type="button"
          onClick={() => onOpen(row.original.package_id)}
          className="flex min-w-32 flex-col rounded-sm text-left hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          <span className="font-medium">{row.original.vendor ?? UNTITLED}</span>
          <span className="text-xs text-muted-foreground">Revision <span className="num">{row.original.revision_number}</span></span>
        </button>
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
      id: 'status',
      accessorFn: (row) => statusRank(row.state),
      header: ({ column }) => <SortableHeader column={column}>Status</SortableHeader>,
      sortDescFirst: false,
      cell: ({ row }) => <PackageStatusBadge status={row.original.state} />,
    },
    {
      id: 'results',
      header: () => (
        <span className="inline-flex items-center gap-1.5">
          Results
          <InfoTip label="About these results">
            <p>Each check&apos;s <strong>recorded result</strong>: what it found when it ran. These count checks, not drawings or individual dimensions.</p>
            <p>A decision does not change a recorded result. &ldquo;Needs your decision&rdquo; here counts the checks that could not decide on their own, decided since or not. <strong>Needs you</strong> counts what still needs a decision.</p>
          </InfoTip>
        </span>
      ),
      enableSorting: false,
      cell: ({ row }) => <OutcomeBar outcomes={row.original.outcomes} />,
    },
    {
      id: 'needs',
      // Only a review still under review is asked (as the old cards did): readiness is "now", so a
      // signed-off review with an exception that has since expired would otherwise show a count
      // nobody can act on. Not under review sorts below every count.
      accessorFn: (row) => (underReview(row) ? row.needs_decision : -1),
      header: ({ column }) => <SortableHeader column={column}>Needs you</SortableHeader>,
      sortDescFirst: true,
      cell: ({ row }) => {
        const count = row.original.needs_decision;
        if (!underReview(row.original)) {
          return <span className="text-muted-foreground"><span aria-hidden="true">—</span><span className="sr-only">Not under review</span></span>;
        }
        if (count > 0) {
          return (
            <span className="num inline-flex items-center gap-1 rounded-full border border-outcome-review-fg/60 bg-outcome-review-bg px-2 py-0.5 text-xs font-medium text-outcome-review-fg">
              <AlertCircle className="size-3.5" aria-hidden="true" />
              {count}
              <span className="sr-only">{count === 1 ? ' result needs' : ' results need'} your decision</span>
            </span>
          );
        }
        // Nothing recorded yet is "—", never a reassuring zero.
        return resultTotal(row.original.outcomes) === 0 ? (
          <span className="text-muted-foreground"><span aria-hidden="true">—</span><span className="sr-only">No results yet</span></span>
        ) : (
          <span className="num text-muted-foreground">0</span>
        );
      },
    },
    {
      id: 'approved',
      accessorFn: (row) => row.approved,
      header: 'Signed off',
      enableSorting: false,
      cell: ({ row }) => <YesNo yes={row.original.approved} yesWord="Signed off" noWord="Not signed off" />,
    },
    {
      id: 'files',
      accessorFn: (row) => row.signed_exports_ready,
      header: 'Signed files',
      enableSorting: false,
      meta: { className: 'hidden md:table-cell' },
      cell: ({ row }) => (
        <YesNo yes={row.original.signed_exports_ready} yesWord="Signed files ready" noWord="Signed files not ready" />
      ),
    },
    {
      id: 'updated',
      accessorFn: (row) => Date.parse(row.updated_at),
      header: ({ column }) => <SortableHeader column={column}>Updated</SortableHeader>,
      sortDescFirst: true,
      meta: { className: 'hidden md:table-cell' },
      cell: ({ row }) => (
        <time className="num text-xs whitespace-nowrap" dateTime={row.original.updated_at} title={formatMoment(row.original.updated_at)}>
          {formatDay(row.original.updated_at)}
        </time>
      ),
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
          aria-label={`Open review for ${row.original.vendor ?? UNTITLED}`}
          onClick={() => onOpen(row.original.package_id)}
        >
          {/* The word only on very wide screens: at laptop widths the arrow alone keeps the column inside the table. */}
          <span className="hidden 2xl:inline">Open</span> <ArrowRight aria-hidden="true" />
        </Button>
      ),
    },
  ];
}

/**
 * The Documents table (#1064): one row per review from `packages-summary`, sortable by column and
 * searchable by vendor or product. Both act on the rows of the page that is loaded (the API pages by
 * cursor and has no server-side search yet), and the table says so when there is more than one page.
 */
export function DocumentsTable({
  rows,
  onOpen,
  morePages,
  emptyMessage = 'No reviews yet.',
}: {
  rows: readonly PackageSummary[];
  onOpen: (packageId: string) => void;
  /** True when other pages exist, so sort and search say they cover this page only. */
  morePages: boolean;
  /** What an empty page says (the first page and a later one differ). */
  emptyMessage?: string;
}) {
  const [query, setQuery] = useState('');
  const shown = rows.filter((row) => matchesSearch(row, query));
  const searching = query.trim() !== '';

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
        <OutcomeLegend />
      </div>
      <p className="text-xs text-muted-foreground" role="status" aria-live="polite">
        {searching ? `${shown.length} of ${rows.length} reviews match.` : `${rows.length} ${rows.length === 1 ? 'review' : 'reviews'}.`}
        {morePages && ' Sorting and search cover this page only.'}
      </p>
      <DataTable
        label="Drawing reviews"
        columns={columns(onOpen)}
        data={shown}
        getRowId={(row) => row.package_id}
        initialSorting={[{ id: 'updated', desc: true }]}
        emptyMessage={searching ? `Nothing matches “${query.trim()}”.` : emptyMessage}
      />
    </div>
  );
}
