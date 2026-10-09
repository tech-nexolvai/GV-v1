/**
 * What the screen shows while a model is deciding which reading fills which field.
 *
 * **Every number on this panel came from the server having done the thing.** The phases are the
 * five the request genuinely runs and each frame arrives as that phase begins; the percentage is
 * *phases finished*, which the endpoint knows because the sequence has a fixed length; the elapsed
 * clock is measured here. Nothing is on a timer.
 *
 * That distinction is why this component exists beside `ThinkingStream` rather than replacing it.
 * `ThinkingStream` is deliberately indeterminate — a chat request reports no internal position, so
 * a fill that grows would assert a completion fraction nothing measured. This request *does* report
 * its position, so a determinate bar is the honest rendering and an indeterminate one would be
 * throwing away a fact.
 *
 * **The retry is visible, and that is the point of showing phases at all.** When the deterministic
 * guard refuses a proposal the model is asked again with the guard's own sentence. The bar holds
 * where it is — the work that was rejected did not advance anything — and the reason is shown. A
 * reviewer watching that learns the thing worth knowing about this feature: the answer is checked.
 */

import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, CircleDashed, MinusCircle, RefreshCw, ShieldCheck, XCircle } from 'lucide-react';
import { GVMark } from '../brand/GVMark';
import type { AssignmentStep, ProposedMeasurements } from '../../api/client';
import { InfoTip } from '@/components/ui/info-tip';
import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';

export function AssignmentProgress({
  steps,
  result,
  error,
}: {
  /** Every phase frame received so far, in arrival order. */
  steps: AssignmentStep[];
  /** The finished proposal, once the result frame has landed. */
  result: ProposedMeasurements | null;
  /** A transport or server failure, in the server's own words where there is one. */
  error: string | null;
}) {
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef(0);
  const running = result === null && error === null;

  useEffect(() => {
    // Started in the effect, not during render: React may call render more than once for a single
    // commit, so a timestamp taken there can come from an attempt that was discarded.
    startedAt.current = Date.now();
    const timer = window.setInterval(() => setElapsed(Date.now() - startedAt.current), 100);
    return () => window.clearInterval(timer);
  }, []);

  const latest = steps.length > 0 ? steps[steps.length - 1] : null;
  const percent = result ? 100 : (latest?.percent ?? 0);

  // The phase labels are the server's, sent once on the first frame. Kept here rather than written
  // into this file because a local copy would be a second answer to "what are the phases", free to
  // disagree with the endpoint's the first time one is added.
  const labels = steps.find((step) => step.sequence.length > 0)?.sequence ?? [];

  // One row per phase, showing the *last* frame for that phase — a retry re-sends a phase, and the
  // second attempt is the current truth about it.
  const byIndex = new Map<number, AssignmentStep>();
  for (const step of steps) byIndex.set(step.index, step);
  const activeIndex = latest?.index ?? 0;
  const retried = steps.some((step) => step.attempt > 1);

  return (
    <section
      data-slot="assignment-progress"
      className="flex flex-col gap-3 rounded-xl border bg-card p-4"
      role="status"
      aria-live="polite"
      aria-busy={running}
      data-state={error ? 'error' : result ? 'done' : 'running'}
    >
      <header className="flex items-start gap-3">
        <span className="shrink-0" aria-hidden="true">
          <GVMark size={24} animated={running} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <h4 className="text-sm font-semibold">
            {error ? 'The fields were left for you' : result ? 'Filled' : 'Filling the measurements'}
          </h4>
          <p className="text-xs text-muted-foreground">
            {latest && running ? (
              latest.label
            ) : result ? (
              <>
                <span className="num">{result.fields_filled}</span> of <span className="num">{result.fields_total}</span> fields filled from{' '}
                <span className="num">{result.readings_attached}</span> attached readings
              </>
            ) : (
              'Nothing was changed. Type the values yourself, or try again.'
            )}
          </p>
        </div>
        <span className="num shrink-0 text-lg font-semibold">
          {percent}
          <span className="text-xs text-muted-foreground">%</span>
        </span>
      </header>

      {/* Determinate, because the server reports its position. */}
      <Progress value={percent} aria-label="phases finished" />

      <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        Phases finished
        <InfoTip label="What the percentage means">
          <p>The percentage is phases finished: not confidence in the answer, and not a guess at how long the model will take.</p>
        </InfoTip>
        {running && <span className="num">{(elapsed / 1000).toFixed(1)}s</span>}
      </p>

      <ol className="flex flex-col gap-1.5">
        {labels.map((label, position) => {
          const index = position + 1;
          const step = byIndex.get(index) ?? null;
          // **A phase nobody reached is skipped, not done.** A proposal nobody made is never
          // checked, and a tick beside "Checking that answer" would claim a check that did not run
          // — which is the one claim this panel exists to make honestly.
          const state =
            error && index === activeIndex
              ? 'error'
              : step === null && index < activeIndex
                ? 'skipped'
                : step !== null && (result || index < activeIndex)
                  ? 'done'
                  : index === activeIndex
                    ? 'active'
                    : 'waiting';
          return (
            <li
              className={cn(
                'flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm',
                (state === 'waiting' || state === 'skipped') && 'text-muted-foreground',
                state === 'error' && 'text-outcome-fail-fg',
              )}
              key={label}
              data-state={state}
            >
              <span className="flex size-4 items-center justify-center" aria-hidden="true">
                {state === 'error' ? (
                  <XCircle size={14} />
                ) : state === 'done' ? (
                  <CheckCircle2 size={14} />
                ) : state === 'active' ? (
                  <CircleDashed size={14} className="animate-spin motion-reduce:animate-none" />
                ) : state === 'skipped' ? (
                  <MinusCircle size={14} />
                ) : (
                  <CircleDashed size={14} />
                )}
              </span>
              <span>{label}</span>
              {state === 'error' ? (
                <span className="text-xs font-medium">stopped here</span>
              ) : state === 'skipped' ? (
                <span className="text-xs text-muted-foreground">not reached</span>
              ) : (
                step?.detail && <span className="text-xs text-muted-foreground">{step.detail}</span>
              )}
              {step && step.attempt > 1 && (
                <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                  <RefreshCw size={12} aria-hidden="true" /> attempt <span className="num">{step.attempt}</span>
                </span>
              )}
            </li>
          );
        })}
      </ol>

      {retried && (
        <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
          <ShieldCheck className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>
            A check refused the first answer; the model was asked again with the reason.{' '}
            <InfoTip label="Why the bar did not move">
              <p>A deterministic check refused the first answer and the model was asked again with the reason. The bar did not move, because rejected work is not progress.</p>
            </InfoTip>
          </span>
        </p>
      )}

      {result?.unfilled_reason && (
        <p className="flex items-start gap-1.5 text-xs text-outcome-review-fg">
          <ShieldCheck className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>{result.unfilled_reason}. The fields stay empty for you, which is what happens without this step at all.</span>
        </p>
      )}

      {error && (
        <p className="flex items-start gap-1.5 text-xs text-outcome-fail-fg">
          <ShieldCheck className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </p>
      )}

      {result && result.model_id && (
        <p className="text-xs text-muted-foreground">
          Proposed by <span className="num">{result.model_id}</span> · checked by seven structural rules · saved by nobody yet
        </p>
      )}
    </section>
  );
}
