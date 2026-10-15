import { forwardRef } from 'react';
import { ChevronRight } from 'lucide-react';

import type { ReviewItem } from '@/api/review-items';
import { cn } from '@/lib/utils';
import { detailId, pageWord, placeWord } from '@/lib/review-items';
import { StatusWord } from './words';

/** What the guess column says when the AI has no number to offer. */
function noGuessWord(item: Pick<ReviewItem, 'kind' | 'status'>): string {
  if (item.status === 'done') return '';
  if (item.kind === 'wall_choice') return 'Your choice';
  if (item.kind === 'no_countertop_page') return 'Confirm the page';
  if (item.kind === 'architect') return 'Your answer';
  return 'Check by hand';
}

/**
 * One item, closed: a single 56-px line — what is asked, where, the page, and the AI's guess in the
 * number face. The whole line is the button that opens it.
 */
export const ItemRow = forwardRef<HTMLButtonElement, {
  item: ReviewItem;
  open: boolean;
  /** The keyboard's place in the list (J/K), shown even before the row is opened. */
  active: boolean;
  onToggle: () => void;
  onFocus?: () => void;
}>(function ItemRow({ item, open, active, onToggle, onFocus }, ref) {
  return (
    <button
      ref={ref}
      type="button"
      data-slot="item-row"
      data-key={item.key}
      data-active={active || undefined}
      aria-expanded={open}
      aria-controls={open ? detailId(item.key) : undefined}
      onClick={onToggle}
      onFocus={onFocus}
      className={cn(
        'group flex min-h-14 w-full items-center gap-3 px-4 py-2 text-left outline-none sm:h-14 sm:px-6 sm:py-0',
        'transition-colors duration-150 ease-out motion-reduce:transition-none',
        'hover:bg-accent/60 focus-visible:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset',
        active && 'bg-accent/40',
        open && 'bg-muted/60',
        item.status === 'done' && 'text-muted-foreground',
      )}
    >
      <ChevronRight
        aria-hidden="true"
        className={cn('size-4 shrink-0 text-muted-foreground transition-transform duration-150 ease-out motion-reduce:transition-none', open && 'rotate-90')}
      />
      {/* One line from tablet width up; on a phone the guess and the status drop to a second line. */}
      <span className="flex min-w-0 flex-1 flex-col gap-1 sm:flex-row sm:items-center sm:gap-3">
        <span className="flex min-w-0 flex-1 items-baseline gap-3">
          <span className={cn('truncate text-[15px] font-medium', item.status === 'done' ? 'text-muted-foreground' : 'text-foreground')}>{item.what}</span>
          <span className="hidden truncate text-[13px] text-muted-foreground md:inline">{placeWord(item)}</span>
          <span className="num shrink-0 text-[13px] text-muted-foreground">{pageWord(item.page)}</span>
        </span>
        <span className="flex shrink-0 items-center justify-between gap-3 sm:justify-end">
          {item.best_guess ? (
            <span className="num text-[15px] font-medium text-foreground">{item.best_guess.text}</span>
          ) : (
            <span className="text-[13px] text-muted-foreground">{noGuessWord(item)}</span>
          )}
          <StatusWord status={item.status} decidedBy={item.decided_by} className="shrink-0 justify-end sm:w-32" />
        </span>
      </span>
    </button>
  );
});
