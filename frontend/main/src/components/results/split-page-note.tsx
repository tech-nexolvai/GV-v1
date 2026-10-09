import { CircleDashed } from 'lucide-react';

import type { CountertopResult } from '@/api/client';
import { cn } from '@/lib/utils';

/**
 * A page the two AIs split on (#1093), where a countertop's picture would be: no line was chosen, so
 * nothing on it was read and there is nothing to draw. The reason is the server's, word for word.
 */
export function SplitPageNote({ hold, className }: { hold: NonNullable<CountertopResult['hold']>; className?: string }) {
  return (
    <div data-slot="split-page" role="note" className={cn('flex flex-col gap-1 rounded-lg border border-dashed p-3 font-sans text-sm', className)}>
      <p className="inline-flex items-center gap-1.5 font-medium">
        <CircleDashed className="size-3.5 text-muted-foreground" aria-hidden="true" /> No countertop line chosen
      </p>
      <p className="text-muted-foreground">{hold.reason}</p>
      <p className="text-xs text-muted-foreground">
        Nothing on this page was read, so there is no countertop picture.
      </p>
    </div>
  );
}
