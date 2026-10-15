import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';

import type { ReviewFailure } from '@/api/review-items';
import { whereWord } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { liveRecordedNumbers, type RecordedNumbers, type RecordedNumbersLoader } from './recorded-numbers-lib';
import { NumText } from './words';

type LoadState = { status: 'loading' } | { status: 'error'; error: Error } | { status: 'ready'; numbers: RecordedNumbers };

/** Beside the drawings when opened from a wrong row: what the check recorded, and why it is wrong. */
export function RecordedNumbersPanel({
  projectId,
  packageId,
  failure,
  load = liveRecordedNumbers,
}: {
  projectId: string;
  packageId: string;
  failure: ReviewFailure;
  load?: RecordedNumbersLoader;
}) {
  const [state, setState] = useState<{ key: string; attempt: number; state: LoadState } | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    load(projectId, packageId, failure).then(
      (numbers) => live && setState({ key: failure.key, attempt, state: { status: 'ready', numbers } }),
      (error: unknown) => live && setState({ key: failure.key, attempt, state: { status: 'error', error: error instanceof Error ? error : new Error(String(error)) } }),
    );
    return () => {
      live = false;
    };
  }, [load, projectId, packageId, failure, attempt]);

  const shown: LoadState = state && state.key === failure.key && state.attempt === attempt ? state.state : { status: 'loading' };

  return (
    <section
      aria-labelledby="recorded-numbers-title"
      data-slot="recorded-numbers"
      className="flex max-h-56 shrink-0 flex-col gap-3 overflow-y-auto border-b border-border bg-background p-4 md:max-h-none md:w-72 md:border-b-0 md:border-l"
    >
      <div>
        <h3 id="recorded-numbers-title" className="text-[15px] leading-5 font-semibold">What the check recorded</h3>
        <p className="text-[12px] leading-4 text-muted-foreground">
          {whereWord(failure)}
          {failure.page != null && <>, <span className="num">p.{failure.page}</span></>}
        </p>
      </div>
      <NumText text={failure.why} className="text-[13px] leading-5 text-outcome-fail-fg" />
      {shown.status === 'loading' && (
        <div role="status" className="flex flex-col gap-2">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-2/3" />
          <span className="sr-only">Loading the recorded numbers…</span>
        </div>
      )}
      {shown.status === 'error' && (
        <p role="alert" className="flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground">
          The recorded numbers could not be loaded: {shown.error.message}
          <Button size="xs" variant="ghost" onClick={() => setAttempt((n) => n + 1)}>
            <RefreshCw /> Retry
          </Button>
        </p>
      )}
      {shown.status === 'ready' && shown.numbers.lines.length === 0 && (
        <p className="text-[13px] text-muted-foreground">The check recorded no numbers for this result.</p>
      )}
      {shown.status === 'ready' && shown.numbers.lines.length > 0 && (
        <ul className="flex flex-col divide-y divide-border text-[13px]">
          {shown.numbers.lines.map((line, index) => (
            <li key={index} className="flex items-baseline justify-between gap-3 py-1.5">
              <span className="text-muted-foreground">{line.label}</span>
              {line.value === null ? <span className="text-muted-foreground italic">not read</span> : <span className="num font-medium text-foreground">{line.value}</span>}
            </li>
          ))}
        </ul>
      )}
      {shown.status === 'ready' && shown.numbers.comparison && <p className="num text-[12px] leading-4 text-muted-foreground">{shown.numbers.comparison}</p>}
    </section>
  );
}
