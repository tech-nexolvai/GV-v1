import { useState } from 'react';
import { Check, ChevronRight, Loader2 } from 'lucide-react';

import type { NotCheckedItem } from '@/api/review-items';
import { cn } from '@/lib/utils';
import { pageWord } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import type { SaveError } from './failures-strip';

/**
 * The checks that never ran (the reader they need is off). Quiet and closed by default: they are not
 * the AI's mistakes, and they are printed NOT CHECKED on the cloud PDF. One click clears them all.
 */
export function NotCheckedGroup({
  items,
  note,
  busy,
  error,
  onClearAll,
}: {
  items: readonly NotCheckedItem[];
  note: string | null;
  busy: boolean;
  error: SaveError | null;
  onClearAll: () => void;
}) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;
  const waiting = items.filter((item) => item.status === 'open').length;

  return (
    <section aria-labelledby="not-checked-title" data-slot="not-checked-group" className="rounded-xl border border-dashed border-border">
      <div className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 px-6 py-2">
        <h2 id="not-checked-title" className="min-w-0 flex-1">
          <button
            type="button"
            aria-expanded={open}
            aria-controls="not-checked-list"
            onClick={() => setOpen((value) => !value)}
            className="flex items-center gap-3 rounded-md text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronRight aria-hidden="true" className={cn('size-4 shrink-0 text-muted-foreground transition-transform duration-150 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
            <span className="inline-flex items-center gap-1.5 text-[15px] font-medium text-outcome-missing-fg">
              <OutcomeIcon outcome="NOT_FOUND" size={14} />
              Not checked
            </span>
            <span className="num text-[13px] text-muted-foreground">{items.length}</span>
          </button>
        </h2>
        {waiting > 0 ? (
          <Button size="sm" variant="outline" onClick={onClearAll} disabled={busy}>
            {busy && <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
            Clear all as not checked
          </Button>
        ) : (
          <span className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground">
            <Check aria-hidden="true" className="size-3.5" />
            Cleared as not checked
          </span>
        )}
        {note && <p className="basis-full pl-7 text-[13px] leading-5 text-muted-foreground">{note}</p>}
        {error && (
          <p role="alert" className="flex basis-full items-center gap-2 pl-7 text-[13px] text-outcome-fail-fg">
            Not saved: {error.message}
            <Button size="xs" variant="ghost" onClick={error.retry}>Retry</Button>
          </p>
        )}
      </div>
      <div
        className={cn('grid transition-[grid-template-rows] duration-150 ease-out motion-reduce:transition-none', open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}
      >
        <ul id="not-checked-list" hidden={!open} className="min-h-0 overflow-hidden border-t border-dashed border-border">
          {items.map((item) => (
            <li key={item.key} className="flex h-11 items-center gap-3 px-6 pl-[3.25rem] text-[13px] text-muted-foreground">
              <span className="min-w-0 flex-1 truncate">
                <span className="text-foreground">{item.check}</span>
                <span className="ml-2">{item.countertop?.label ?? item.row_label ?? `Page ${item.page}`}</span>
              </span>
              <span className="num shrink-0">{pageWord(item.page)}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
