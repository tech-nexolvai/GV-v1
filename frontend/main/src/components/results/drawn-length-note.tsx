import { Ruler } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { CountertopResult } from '@/api/client';

/**
 * The row's "drawn length not checked" note (#1107), exactly as the API and the signed report say
 * it, e.g. "Drawn length not checked (no scale): piece 2, the overall". Quiet: it is information,
 * not a result and nothing to click. No note, nothing shown.
 *
 * `clamp` keeps it to two lines in a table cell; the whole text stays in the page for screen
 * readers and in the title for a pointer.
 */
export function DrawnLengthNote({ row, clamp = false, className }: { row: Pick<CountertopResult, 'drawn_length_note'>; clamp?: boolean; className?: string }) {
  const note = row.drawn_length_note?.trim();
  if (!note) return null;
  return (
    <span data-slot="drawn-length-note" title={clamp ? note : undefined} className={cn('flex items-start gap-1 font-sans text-xs text-muted-foreground', className)}>
      <Ruler className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span className={cn('whitespace-normal', clamp && 'line-clamp-2')}>{note}</span>
    </span>
  );
}
