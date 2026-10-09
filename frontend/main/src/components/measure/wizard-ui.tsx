import type { ReactNode } from 'react';
import { AlertTriangle, Check, RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { InfoTip } from '@/components/ui/info-tip';
import { cn } from '@/lib/utils';
import { feedbackText, type DecisionFeedback } from './decisionFeedback.js';

/**
 * The small pieces every step of the Measurements wizard is built from (#1124), so the four steps
 * read as one screen: a sentence-case heading, one short line with the count in words, the
 * explanation behind "?", and errors that say what failed with a way to try again.
 */

/**
 * A native select dressed like the shadcn Input. Native on purpose: a phone opens its own picker,
 * and the static tests read `<option selected>` straight from the markup.
 */
export const SELECT_CLASS =
  'h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30 [&>option]:bg-popover [&>option]:text-popover-foreground';

/** A plain text box for a code, a reference or a value. Values add `num` themselves. */
export const INPUT_CLASS =
  'h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-xs outline-none transition-[color,box-shadow] placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30';

/** One part of a step: heading, one line (the count in words), and the explanation behind "?". */
export function StepSection({
  id,
  title,
  line,
  tipLabel,
  tip,
  slot,
  children,
  className,
}: {
  id: string;
  title: ReactNode;
  line?: ReactNode;
  tipLabel?: string;
  tip?: ReactNode;
  slot?: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <section data-slot={slot} aria-labelledby={id} className={cn('flex flex-col gap-3', className)}>
      <div className="flex flex-col gap-1">
        <h3 id={id} className="text-base font-semibold text-foreground">
          {title}
        </h3>
        {(line || tip) && (
          <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-muted-foreground">
            {line}
            {tip && tipLabel && <InfoTip label={tipLabel}>{tip}</InfoTip>}
          </p>
        )}
      </div>
      {children}
    </section>
  );
}

/** A request that failed: the server's words, a warning glyph, and a way to try again. */
export function LoadError({
  children,
  onRetry,
  retryLabel = 'Try again',
  role = 'alert',
}: {
  children: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  /** "status" for one picture that failed among many: an alert per crop would interrupt every time. */
  role?: 'alert' | 'status';
}) {
  return (
    <div role={role} className="flex flex-wrap items-center gap-2 text-sm text-outcome-fail-fg">
      <AlertTriangle className="size-4 shrink-0" aria-hidden="true" />
      <span className="min-w-0 break-words">{children}</span>
      {onRetry && (
        <Button type="button" size="xs" variant="outline" className="text-foreground" onClick={onRetry}>
          <RefreshCw aria-hidden="true" /> {retryLabel}
        </Button>
      )}
    </div>
  );
}

/** A grey line under a control: a fact about it, never an instruction paragraph. */
export function Hint({ children, className, role }: { children: ReactNode; className?: string; role?: string }) {
  return (
    <p role={role} className={cn('text-xs text-muted-foreground', className)}>
      {children}
    </p>
  );
}

/** A caution with its glyph, in the "needs your decision" colour. */
export function Caution({ children, role = 'status', className }: { children: ReactNode; role?: string; className?: string }) {
  return (
    <p role={role} className={cn('flex items-start gap-1.5 text-xs text-outcome-review-fg', className)}>
      <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0">{children}</span>
    </p>
  );
}

/** What happened to the last decision on one item: saving, saved, or the server's refusal. */
export function DecisionLine({ feedback }: { feedback?: DecisionFeedback }) {
  if (!feedback) return null;
  const failed = feedback.kind === 'error';
  return (
    <p
      role={failed ? 'alert' : 'status'}
      className={cn('flex items-start gap-1.5 text-xs', failed ? 'text-outcome-fail-fg' : 'text-muted-foreground')}
    >
      {failed && <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden="true" />}
      {feedbackText(feedback)}
    </p>
  );
}

/** The chosen answer's tick: the filled button is never the only sign of which one was chosen. */
export function ChoiceMark({ chosen }: { chosen: boolean }) {
  return chosen ? <Check aria-hidden="true" data-slot="choice-mark" /> : null;
}
