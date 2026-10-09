import { Check } from 'lucide-react';

import { cn } from '@/lib/utils';
import { MEASURE_STEPS, isComplete, stepCountWords, type MeasureStep, type StepCount } from '@/lib/measure-steps';

/**
 * The Measurements wizard's step bar (#1061): four steps, each with its count in words and a check mark
 * when everything in it is done. Choosing a step shows it; the others stay mounted (and hidden), so
 * their unsaved drafts survive. The app routes through `#`, so these are buttons, never links.
 */
export function MeasurementSectionNav({
  current = 'drawings',
  counts = {},
  onPick,
}: {
  current?: MeasureStep;
  counts?: Partial<Record<MeasureStep, StepCount | null>>;
  onPick?: (step: MeasureStep) => void;
}) {
  return (
    <nav data-tw className="font-sans" aria-label="Measurement steps">
      <ol className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {MEASURE_STEPS.map((step) => {
          const count = counts[step.id] ?? null;
          const complete = isComplete(count);
          const active = step.id === current;
          return (
            <li key={step.id}>
              <button
                type="button"
                aria-controls={step.target}
                aria-current={active ? 'step' : undefined}
                data-step={step.id}
                onClick={() => onPick?.(step.id)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-left transition-colors hover:bg-accent/60 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
                  active && 'border-foreground bg-accent',
                )}
              >
                <span
                  className={cn(
                    'num flex size-6 shrink-0 items-center justify-center rounded-full border text-xs',
                    complete && 'border-foreground bg-foreground text-background',
                  )}
                  aria-hidden="true"
                >
                  {complete ? <Check className="size-3.5" /> : step.number}
                </span>
                <span className="flex min-w-0 flex-col">
                  <span className="text-sm leading-tight font-medium">{step.label}</span>
                  <span className="text-xs text-muted-foreground" data-slot="step-count">
                    {stepCountWords(count)}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
