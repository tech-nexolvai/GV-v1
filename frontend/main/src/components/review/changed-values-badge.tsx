import { SlidersHorizontal } from 'lucide-react';

import type { components } from '@/api/schema';
import { cn } from '@/lib/utils';
import { changedValuesSummary } from '@/lib/changed-values';
import { ChangedValuesPanel } from '@/components/output/ChangedValuesPanel';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';

type ChangedValues = components['schemas']['ChangedValuesOut'];

/**
 * The run's project values as a header badge (#1034): one line, and the full table on click. It
 * replaces the always-open panel above the tabs.
 */
export function ChangedValuesBadge({
  state,
  value,
  currentRevisionId,
}: {
  state: 'loading' | 'error' | 'ready';
  value: ChangedValues | null;
  currentRevisionId: string | null;
}) {
  const summary = changedValuesSummary(state, value);
  const attention = state === 'ready' && value !== null && value.message === null && value.company_standards_displaced.length > 0;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-slot="changed-values-badge"
          className={cn(
            'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs whitespace-nowrap outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring',
            attention ? 'border-foreground/40 font-medium text-foreground' : 'text-muted-foreground',
          )}
        >
          <SlidersHorizontal className="size-3.5" aria-hidden="true" />
          {summary}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(36rem,calc(100vw-2rem))]">
        <ChangedValuesPanel state={state} value={value} currentRevisionId={currentRevisionId} />
      </PopoverContent>
    </Popover>
  );
}
