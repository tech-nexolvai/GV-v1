import type { PageWithoutCountertop } from '@/api/client';

/**
 * Pages where both AIs found no countertop line (#1093): tall units, cover-like sheets. Listed with
 * the AI's reason so nothing vanishes from the review, but not blocking and nothing to click
 * (decision log 2026-10-09). The signed report lists them too.
 */
export function NoCountertopPages({ pages }: { pages: readonly PageWithoutCountertop[] }) {
  if (pages.length === 0) return null;
  return (
    <section data-slot="no-countertop-pages" aria-labelledby="no-countertop-pages" className="flex flex-col gap-2 rounded-xl border border-dashed px-4 py-3 font-sans">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id="no-countertop-pages" className="text-sm font-medium">Pages with no countertop found</h2>
        <p className="text-xs text-muted-foreground">Listed only. Nothing to decide.</p>
      </div>
      <ul className="flex flex-col gap-1.5 text-sm">
        {pages.map((page) => (
          <li key={page.page_number} className="flex gap-3">
            <span className="num w-16 shrink-0 text-muted-foreground">Page {page.page_number}</span>
            <span className="text-muted-foreground">{page.reason}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
