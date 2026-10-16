import { useId, useState } from 'react';
import { ChevronRight } from 'lucide-react';

import { Collapse } from '@/components/chat/chat-motion';
import type { ChatCoverage, CoverageStatus } from '@/lib/chat-api';
import { COVERAGE_ORDER, COVERAGE_WORDS, coverageCounts, coveragePages, coveragePageWords, coverageWords } from '@/lib/chat-findings';
import { cn } from '@/lib/utils';

/**
 * Chat v2 (#1483): "nothing missed", shown. A thin strip under a whole-set answer says how many
 * pages were compared ("58 of 60 pages compared · 2 not comparable · 0 not opened") over a
 * segmented bar; it opens to a grid of page squares coloured by what happened to each page (hover
 * or focus shows its note). Clicking a page that differs narrows the findings table to it.
 * The pattern differs as well as the colour: filled, striped, or an empty dashed square.
 */

const SQUARE_CLASS: Record<CoverageStatus, string> = {
  same: 'border-outcome-pass-fg/40 bg-outcome-pass/70 text-white',
  different: 'border-outcome-fail-fg bg-outcome-fail-fg text-background',
  not_comparable: 'border-outcome-missing-fg/50 bg-[repeating-linear-gradient(135deg,var(--outcome-missing-bg)_0_3px,var(--outcome-missing)_3px_4px)] text-foreground',
  not_opened: 'border-dashed border-outcome-review-fg bg-transparent text-outcome-review-fg',
};

const BAR_CLASS: Record<CoverageStatus, string> = {
  same: 'bg-outcome-pass/70',
  different: 'bg-outcome-fail-fg',
  not_comparable: 'bg-outcome-missing',
  not_opened: 'bg-outcome-review/40',
};

export interface CoverageStripProps {
  coverage: ChatCoverage;
  /** The page the findings table is narrowed to (its square is marked), or null. */
  page: number | null;
  /** Narrow the findings table to a page that differs. */
  onPickPage?: (page: number) => void;
}

export function CoverageStrip({ coverage, page, onPickPage }: CoverageStripProps) {
  const [open, setOpen] = useState(false);
  const gridId = useId();
  const counts = coverageCounts(coverage);
  const pages = coveragePages(coverage);
  const words = coverageWords(counts);

  return (
    <section data-slot="chat-coverage" aria-label="Pages compared" className="grid min-w-0 gap-1.5">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={gridId}
        onClick={() => setOpen(!open)}
        className="group/coverage grid w-full max-w-md min-w-0 gap-1 rounded-md py-0.5 text-left focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground group-hover/coverage:text-foreground">
          <span data-slot="chat-coverage-words" className="min-w-0 tabular-nums">{words}</span>
          <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
        </span>
        <span data-slot="chat-coverage-bar" aria-hidden="true" className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
          {counts.total > 0 && COVERAGE_ORDER.map((status) => counts[status] > 0 && (
            <span key={status} data-status={status} className={cn('h-full', BAR_CLASS[status])} style={{ width: `${(counts[status] / counts.total) * 100}%` }} />
          ))}
        </span>
      </button>

      <Collapse open={open} id={gridId}>
        <div className="grid gap-2 pt-1 pb-0.5">
          <ul data-slot="chat-coverage-grid" aria-label="Pages" className="flex flex-wrap gap-1">
            {pages.map((entry) => {
              const label = coveragePageWords(entry);
              const picked = page === entry.page;
              const square = cn(
                'num grid h-6 min-w-6 place-items-center rounded-[5px] border px-1 text-xs leading-none',
                SQUARE_CLASS[entry.status] ?? SQUARE_CLASS.not_opened,
                picked && 'ring-2 ring-ring ring-offset-1 ring-offset-background',
              );
              return (
                <li key={entry.page} data-slot="chat-coverage-page" data-page={entry.page} data-status={entry.status}>
                  {entry.status === 'different' && onPickPage ? (
                    <button
                      type="button"
                      title={label}
                      aria-label={`${label}. Show its findings`}
                      aria-pressed={picked}
                      onClick={() => onPickPage(entry.page)}
                      className={cn(square, 'cursor-pointer hover:opacity-85 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none')}
                    >
                      {entry.page}
                    </button>
                  ) : (
                    // A focusable square, so its note can be read without a mouse too.
                    <span tabIndex={0} role="img" title={label} aria-label={label} className={cn(square, 'focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none')}>
                      {entry.page}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          <ul aria-label="What the colours mean" className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
            {COVERAGE_ORDER.map((status) => (
              <li key={status} className="inline-flex items-center gap-1">
                <span aria-hidden="true" className={cn('inline-block size-2.5 rounded-[3px] border', SQUARE_CLASS[status])} />
                {COVERAGE_WORDS[status]} <span className="num">{counts[status]}</span>
              </li>
            ))}
          </ul>
        </div>
      </Collapse>
    </section>
  );
}
