import { LayoutList, ListChecks } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { Filter, Kpis } from '@/lib/countertop-results';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';

interface Card {
  filter: Filter;
  /** How many of the six phone columns it takes: three cells on the first row, two on the second. */
  span: string;
  value: number;
  word: string;
  icon: React.ReactNode;
  tone?: string;
}

/**
 * The review in five numbers (#1039): a number, one word and an icon each, nothing to read. Each
 * number filters the table below to the rows it counts, except "Needs you", which opens the queue to
 * work through them one at a time (#1050) when there is anything to do and a queue to open.
 *
 * On a phone the five sit in a compact block of two short rows, three then two (#1126), each a number
 * beside its word, so the first countertop is near the top and no word is cut. From the small
 * breakpoint up they are cards.
 */
export function KpiCards({ kpis, active, onSelect, onOpenQueue }: { kpis: Kpis; active: Filter; onSelect: (filter: Filter) => void; onOpenQueue?: () => void }) {
  const cards: Card[] = [
    { filter: 'all', span: 'col-span-2', value: kpis.countertops, word: 'Countertops', icon: <LayoutList className="hidden size-4 sm:block" aria-hidden="true" /> },
    { filter: 'automatic', span: 'col-span-2', value: kpis.automatic, word: 'Automatic', icon: <ListChecks className="hidden size-4 sm:block" aria-hidden="true" /> },
    { filter: 'needs-you', span: 'col-span-2', value: kpis.needsYou, word: 'Needs you', icon: <OutcomeIcon outcome="REVIEW_REQUIRED" size={14} className="shrink-0" />, tone: 'text-outcome-review-fg' },
    { filter: 'pass', span: 'col-span-3', value: kpis.pass, word: 'PASS', icon: <OutcomeIcon outcome="PASS" size={14} className="shrink-0" />, tone: 'text-outcome-pass-fg' },
    { filter: 'fail', span: 'col-span-3', value: kpis.fail, word: 'FAIL', icon: <OutcomeIcon outcome="FAIL" size={14} className="shrink-0" />, tone: 'text-outcome-fail-fg' },
  ];
  return (
    <div
      data-slot="kpi-cards"
      // Phone: the 1px gaps over the border colour draw the lines between cells.
      className="grid grid-cols-6 gap-px overflow-hidden rounded-xl border bg-border sm:grid-cols-3 sm:gap-2 sm:overflow-visible sm:rounded-none sm:border-0 sm:bg-transparent lg:grid-cols-5 lg:gap-3"
    >
      {cards.map((card) => {
        const opensQueue = card.filter === 'needs-you' && onOpenQueue !== undefined && card.value > 0;
        const pressed = !opensQueue && active === card.filter;
        return (
          <button
            key={card.filter}
            type="button"
            aria-pressed={opensQueue ? undefined : pressed}
            aria-label={opensQueue ? `Needs you ${card.value}: review them one at a time` : undefined}
            data-opens-queue={opensQueue || undefined}
            onClick={() => (opensQueue ? onOpenQueue() : onSelect(card.filter))}
            className={cn(
              // Phone: a cell, the number beside its word. Wider: a card, the word above the number.
              'flex min-w-0 flex-row-reverse items-center justify-center gap-1.5 bg-card px-2 py-2.5 text-left outline-none transition-colors hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset',
              'sm:col-span-1 sm:flex-col sm:items-start sm:justify-start sm:gap-1 sm:rounded-xl sm:border sm:px-3.5 sm:py-3',
              card.span,
              pressed && 'bg-accent/60 sm:border-foreground/60 sm:bg-card sm:ring-1 sm:ring-foreground/20',
            )}
          >
            <span className={cn('flex min-w-0 items-center gap-1 text-xs leading-tight text-muted-foreground sm:gap-1.5', card.tone)}>
              {card.icon}
              {/* Never cut: a word may wrap at its space ("Needs / you"), never mid-word. */}
              <span className={card.tone}>{card.word}</span>
            </span>
            <span className="num text-xl leading-none font-medium sm:text-3xl">{card.value}</span>
          </button>
        );
      })}
    </div>
  );
}
