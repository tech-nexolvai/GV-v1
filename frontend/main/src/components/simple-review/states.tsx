import { CheckCheck, CircleAlert, RotateCcw } from 'lucide-react';

import { ApiError } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';

/** While the items load: the page's own shape in grey, so nothing jumps when they arrive. */
export function ReviewSkeleton() {
  return (
    <div data-slot="review-skeleton" className="flex flex-col gap-6 pt-8">
      <p role="status" className="sr-only">Loading this review's items…</p>
      <div className="flex flex-col gap-3">
        <Skeleton className="h-9 w-72" />
        <Skeleton className="h-5 w-96 max-w-full" />
      </div>
      <Skeleton className="h-28 w-full rounded-xl" />
      <div className="overflow-hidden rounded-xl border border-border">
        {Array.from({ length: 5 }, (_, index) => (
          <div key={index} className="flex h-14 items-center gap-4 border-b border-border px-6 last:border-b-0">
            <Skeleton className="h-4 w-4" />
            <Skeleton className="h-4 flex-1" />
            <Skeleton className="h-4 w-16" />
            <Skeleton className="h-4 w-24" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** Nothing on the page waits for a person. */
export function NothingNeedsYou({ checked }: { checked: number }) {
  return (
    <div role="status" data-slot="nothing-needs-you" className="flex flex-col items-center gap-2 rounded-xl border border-border px-6 py-12 text-center">
      <CheckCheck aria-hidden="true" className="size-7 text-muted-foreground" />
      <p className="text-[20px] leading-7 font-semibold text-foreground">Nothing needs you</p>
      <p className="max-w-[52ch] text-[15px] leading-6 text-muted-foreground">
        {checked > 0 ? (
          <>
            All <span className="num">{checked}</span> items are checked. Create the final cloud PDF below.
          </>
        ) : (
          'The AI settled every number on this set. Create the final cloud PDF below.'
        )}
      </p>
    </div>
  );
}

/** The items could not be loaded: what failed, the request id a report quotes, and Retry. */
export function LoadError({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div role="alert" className="mt-8 flex flex-col items-start gap-3 rounded-xl border border-outcome-fail-fg/40 bg-outcome-fail-bg px-6 py-5">
      <p className="flex items-center gap-2 text-[15px] font-medium text-outcome-fail-fg">
        <CircleAlert aria-hidden="true" className="size-4" />
        This review's items could not be loaded
      </p>
      <p className="text-[13px] leading-5 text-foreground">{error.message}</p>
      {requestId && (
        <p className="text-[13px] text-muted-foreground">
          Request <span className="num">{requestId}</span>
        </p>
      )}
      <Button size="sm" variant="outline" onClick={onRetry}>
        <RotateCcw aria-hidden="true" />
        Retry
      </Button>
    </div>
  );
}

/**
 * Something that blocks sign-off is missing from this page. That must never happen, so it is loud,
 * and it points to the advanced view, where every record is listed.
 */
export function UnaccountedBanner({ count, onOpenAdvanced }: { count: number; onOpenAdvanced: () => void }) {
  return (
    <div role="alert" data-slot="unaccounted-banner" className="mt-6 flex flex-wrap items-center gap-3 rounded-xl border border-outcome-fail-fg bg-outcome-fail-fg px-6 py-3 text-[15px] text-background">
      <CircleAlert aria-hidden="true" className="size-4 shrink-0" />
      <span className="min-w-0 flex-1">
        <span className="num">{count}</span> {count === 1 ? 'record that blocks' : 'records that block'} sign-off {count === 1 ? 'is' : 'are'} missing from this page.
      </span>
      <Button size="sm" variant="secondary" onClick={onOpenAdvanced}>
        Open the advanced view
      </Button>
    </div>
  );
}
