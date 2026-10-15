import { Check, Loader2 } from 'lucide-react';

import type { ReviewFailure } from '@/api/review-items';
import { whereWord } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { NumText, PageTag } from './words';

export interface SaveError {
  message: string;
  retry: () => void;
}

/**
 * The results the check found wrong. They will be clouded on the PDF as they are; the reviewer only
 * agrees, one click each. Shown above the list because they are the strongest thing on the page.
 */
export function FailuresStrip({
  failures,
  busyKey,
  errors,
  onAgree,
}: {
  failures: readonly ReviewFailure[];
  busyKey: string | null;
  errors: Readonly<Record<string, SaveError>>;
  onAgree: (failure: ReviewFailure) => void;
}) {
  if (failures.length === 0) return null;
  const open = failures.filter((failure) => failure.status === 'open').length;
  return (
    <section
      aria-labelledby="failures-strip-title"
      data-slot="failures-strip"
      className="overflow-hidden rounded-xl border border-outcome-fail-fg/35 bg-background"
    >
      <h2 id="failures-strip-title" className="sr-only">Will be clouded as wrong</h2>
      <div aria-hidden="true" className="flex h-12 items-center gap-2 border-b border-outcome-fail-fg/20 bg-outcome-fail-bg px-4 sm:px-6 text-[15px] font-medium text-outcome-fail-fg">
        <OutcomeIcon outcome="FAIL" size={16} />
        Will be clouded as wrong
        <span className="num ml-auto text-[13px] font-normal">{open > 0 ? `${open} to agree` : 'All agreed'}</span>
      </div>
      <ul className="divide-y divide-border">
        {failures.map((failure) => {
          const busy = busyKey === failure.key;
          const error = errors[failure.key];
          return (
            <li key={failure.key} data-slot="failure-row" data-entry-key={failure.key} tabIndex={-1} className="flex outline-none focus-visible:bg-accent/60 min-h-14 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 sm:px-6">
              <span className="inline-flex shrink-0 basis-full items-center gap-1.5 text-[13px] font-medium text-outcome-fail-fg sm:w-20 sm:basis-auto">
                <OutcomeIcon outcome="FAIL" size={14} />
                Wrong
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[15px] text-foreground">
                  {whereWord(failure)}
                  <PageTag page={failure.page} className="ml-2" />
                </span>
                <NumText text={failure.why} className="block text-[13px] leading-5 text-muted-foreground" />
              </span>
              {failure.status === 'open' && failure.actions.includes('agree') ? (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => onAgree(failure)} aria-label={`Agree: ${whereWord(failure)} is wrong`}>
                  {busy ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <Check aria-hidden="true" />}
                  Agree
                </Button>
              ) : (
                <span className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground">
                  <Check aria-hidden="true" className="size-3.5" />
                  {failure.status === 'rechecking' ? 'Re-checking' : 'Agreed'}
                </span>
              )}
              {error && (
                <p role="alert" className="flex basis-full flex-wrap items-center gap-2 text-[13px] text-outcome-fail-fg">
                  {error.message}
                  <Button size="xs" variant="ghost" onClick={error.retry}>Retry</Button>
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
