import { Check, CloudUpload, FileDown, ListChecks, PenLine, ScanText, TriangleAlert, UserRoundCheck } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { ReviewStep, StepId } from '@/lib/review-stage';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

const ICONS: Record<StepId, typeof Check> = {
  upload: CloudUpload,
  reading: ScanText,
  checks: ListChecks,
  decisions: UserRoundCheck,
  signoff: PenLine,
  report: FileDown,
};

const STATE_WORDS = { done: 'done', current: 'current step', blocked: 'blocked', upcoming: 'not started' } as const;

/** What a step's number means, said for a screen reader. */
function countWords(step: ReviewStep): string {
  if (step.count === null) return '';
  if (step.id === 'checks') return `, ${step.count} recorded`;
  if (step.id === 'decisions') return `, ${step.count} need you`;
  return `, ${step.count}`;
}

/**
 * The review's six steps, always on screen under the header (#1034). Everything shown comes from
 * `reviewStage()`; this only draws it. A blocked step names its reason on hover and to screen
 * readers; the current step on a phone keeps its label, the others shrink to their marker.
 */
export function ReviewStepper({ steps, className }: { steps: ReviewStep[]; className?: string }) {
  const active = steps.find((step) => step.state === 'current' || step.state === 'blocked');
  return (
    <nav aria-label="Review progress" data-slot="review-stepper" className={cn('border-b bg-background px-4 py-2.5 font-sans sm:px-6', className)}>
      <ol className="flex items-center gap-1.5 sm:gap-2">
        {steps.map((step, index) => (
          <li key={step.id} className={cn('flex min-w-0 items-center gap-1.5 sm:gap-2', index < steps.length - 1 && 'flex-1')} aria-current={step.state === 'current' || step.state === 'blocked' ? 'step' : undefined}>
            <StepItem step={step} position={index + 1} total={steps.length} />
            {index < steps.length - 1 && (
              <span aria-hidden="true" className={cn('h-px min-w-3 flex-1', step.state === 'done' ? 'bg-foreground/40' : 'bg-border')} />
            )}
          </li>
        ))}
      </ol>
      {/* On a phone the row is markers only; the current step is named once, underneath. */}
      {active && (
        <p aria-hidden="true" className="mt-1.5 flex items-center gap-1.5 text-xs sm:hidden">
          <span className="font-medium">{active.label}</span>
          {active.count !== null && <span className="num rounded-full bg-muted px-1.5">{active.count}</span>}
          {active.note && <span className="truncate text-muted-foreground">{active.note}</span>}
        </p>
      )}
    </nav>
  );
}

function StepItem({ step, position, total }: { step: ReviewStep; position: number; total: number }) {
  const Icon = ICONS[step.id];
  const active = step.state === 'current' || step.state === 'blocked';
  const content = (
    <span
      data-step={step.id}
      data-state={step.state}
      className={cn(
        'flex min-w-0 items-center gap-1.5 rounded-md py-1 text-sm whitespace-nowrap',
        step.state === 'upcoming' && 'text-muted-foreground',
        step.state === 'done' && 'text-muted-foreground',
        active && 'font-medium text-foreground',
        step.note && 'cursor-help',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'num flex size-6 shrink-0 items-center justify-center rounded-full border text-xs',
          step.state === 'done' && 'border-foreground/30 bg-muted text-foreground',
          step.state === 'current' && 'border-foreground bg-foreground text-background',
          step.state === 'blocked' && 'border-outcome-fail-fg bg-outcome-fail-bg text-outcome-fail-fg',
          step.state === 'upcoming' && 'border-border',
        )}
      >
        {step.state === 'done' ? <Check className="size-3.5" /> : step.state === 'blocked' ? <TriangleAlert className="size-3.5" /> : position}
      </span>
      <Icon aria-hidden="true" className={cn('hidden size-4 shrink-0', active ? 'sm:block' : 'md:block')} />
      <span className={cn('hidden', active ? 'sm:inline' : 'lg:inline')}>{step.label}</span>
      {step.count !== null && (
        <span
          aria-hidden="true"
          className={cn(
            'num rounded-full px-1.5 text-xs',
            step.id === 'decisions' && step.count > 0 ? 'bg-outcome-review-bg text-outcome-review-fg' : 'bg-muted text-muted-foreground',
            'hidden',
            active ? 'sm:inline' : 'lg:inline',
          )}
        >
          {step.count}
        </span>
      )}
      {active && step.id === 'reading' && (
        <span aria-hidden="true" className="hidden h-1 w-14 overflow-hidden rounded-full bg-muted sm:block">
          <span className="block h-full w-1/2 animate-pulse rounded-full bg-foreground/60 motion-reduce:animate-none" />
        </span>
      )}
      {active && step.note && <span className="hidden max-w-56 truncate text-xs font-normal text-muted-foreground xl:inline">{step.note}</span>}
      <span className="sr-only">
        {`Step ${position} of ${total}: ${step.label}, ${STATE_WORDS[step.state]}${countWords(step)}${step.note ? `. ${step.note}` : ''}`}
      </span>
    </span>
  );

  if (!step.note) return content;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="min-w-0 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring">
          {content}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom">{step.note}</TooltipContent>
    </Tooltip>
  );
}
