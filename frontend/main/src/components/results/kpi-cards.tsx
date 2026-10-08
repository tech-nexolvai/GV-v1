import { LayoutList, ListChecks } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { Filter, Kpis } from '@/lib/countertop-results';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';

interface Card {
  filter: Filter;
  value: number;
  word: string;
  icon: React.ReactNode;
  tone?: string;
}

/**
 * The review in five numbers (#1039): a number, one word and an icon each, nothing to read. Each
 * card is a button that filters the table below to the rows it counts.
 */
export function KpiCards({ kpis, active, onSelect }: { kpis: Kpis; active: Filter; onSelect: (filter: Filter) => void }) {
  const cards: Card[] = [
    { filter: 'all', value: kpis.countertops, word: 'Countertops', icon: <LayoutList className="size-4" aria-hidden="true" /> },
    { filter: 'automatic', value: kpis.automatic, word: 'Automatic', icon: <ListChecks className="size-4" aria-hidden="true" /> },
    { filter: 'needs-you', value: kpis.needsYou, word: 'Needs you', icon: <OutcomeIcon outcome="REVIEW_REQUIRED" size={16} />, tone: 'text-outcome-review-fg' },
    { filter: 'pass', value: kpis.pass, word: 'PASS', icon: <OutcomeIcon outcome="PASS" size={16} />, tone: 'text-outcome-pass-fg' },
    { filter: 'fail', value: kpis.fail, word: 'FAIL', icon: <OutcomeIcon outcome="FAIL" size={16} />, tone: 'text-outcome-fail-fg' },
  ];
  return (
    <div data-slot="kpi-cards" className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5 lg:gap-3">
      {cards.map((card) => {
        const pressed = active === card.filter;
        return (
          <button
            key={card.filter}
            type="button"
            aria-pressed={pressed}
            onClick={() => onSelect(card.filter)}
            className={cn(
              'flex flex-col items-start gap-1 rounded-xl border bg-card px-3.5 py-3 text-left outline-none transition-colors hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring',
              pressed && 'border-foreground/60 ring-1 ring-foreground/20',
            )}
          >
            <span className={cn('flex items-center gap-1.5 text-xs text-muted-foreground', card.tone)}>
              {card.icon}
              <span className={card.tone}>{card.word}</span>
            </span>
            <span className="num text-2xl leading-none font-medium sm:text-3xl">{card.value}</span>
          </button>
        );
      })}
    </div>
  );
}
