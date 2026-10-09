import type { CountertopResults, PageWithoutCountertop } from '@/api/client';
import { cn } from '@/lib/utils';

/** A second countertop line an AI named on a page whose countertop was read (#1108). */
export type RowNotChecked = NonNullable<CountertopResults['rows_not_checked']>[number];

/**
 * Pages where both AIs found no countertop line (#1093): tall units, cover-like sheets. Listed with
 * the AI's reason so nothing vanishes from the review, but not blocking and nothing to click
 * (decision log 2026-10-09). The signed report lists them too.
 */
export function NoCountertopPages({ pages }: { pages: readonly PageWithoutCountertop[] }) {
  if (pages.length === 0) return null;
  return (
    <ListedOnly
      slot="no-countertop-pages"
      title="Pages with no countertop found"
      items={pages.map((page) => ({ key: `page-${page.page_number}`, page: page.page_number, reason: page.reason }))}
    />
  );
}

/**
 * Countertop lines not checked (#1108): a second countertop line an AI named on a page. V1 reads one
 * line per page, so this one was not checked. Listed with the API's reason, never blocking, nothing
 * to click; the signed report lists them too.
 */
export function RowsNotChecked({ rows }: { rows: readonly RowNotChecked[] }) {
  if (rows.length === 0) return null;
  return (
    <ListedOnly
      slot="rows-not-checked"
      title="Countertop lines not checked"
      items={rows.map((row, index) => ({ key: `row-${row.page_number}-${index}`, page: row.page_number, reason: row.reason }))}
    />
  );
}

/** Both lists, side by side on a wide screen and stacked on a phone; nothing when both are empty. */
export function ListedPages({
  pagesWithoutCountertop,
  rowsNotChecked,
}: {
  pagesWithoutCountertop: readonly PageWithoutCountertop[];
  rowsNotChecked: readonly RowNotChecked[];
}) {
  if (pagesWithoutCountertop.length === 0 && rowsNotChecked.length === 0) return null;
  const both = pagesWithoutCountertop.length > 0 && rowsNotChecked.length > 0;
  return (
    <div data-slot="listed-pages" className={cn('grid items-start gap-3', both && 'lg:grid-cols-2')}>
      <NoCountertopPages pages={pagesWithoutCountertop} />
      <RowsNotChecked rows={rowsNotChecked} />
    </div>
  );
}

function ListedOnly({ slot, title, items }: { slot: string; title: string; items: { key: string; page: number; reason: string }[] }) {
  return (
    <section data-slot={slot} aria-labelledby={slot} className="flex flex-col gap-2 rounded-xl border border-dashed px-4 py-3 font-sans">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id={slot} className="text-sm font-medium">{title}</h2>
        <p className="text-xs text-muted-foreground">Listed only. Nothing to decide.</p>
      </div>
      <ul className="flex flex-col gap-1.5 text-sm">
        {items.map((item) => (
          <li key={item.key} className="flex gap-3">
            <span className="w-16 shrink-0 text-muted-foreground">Page <span className="num">{item.page}</span></span>
            <span className="text-muted-foreground">{item.reason}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
