import { cva } from 'class-variance-authority';

import { cn } from '@/lib/utils';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import type { Outcome } from '@/data/types';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';

/**
 * Which colour family each recorded outcome uses. Colour is only ever the second signal: the glyph
 * (OutcomeIcon) and the word (OUTCOME_LABELS, agreed with the backend narration) always come with
 * it, and the edge differs too — so the badge reads the same in greyscale, in print and for a
 * reviewer who cannot tell red from green.
 */
export type OutcomeTone = 'pass' | 'fail' | 'review' | 'missing' | 'none';

export const OUTCOME_TONE: Record<Outcome, OutcomeTone> = {
  PASS: 'pass',
  FAIL: 'fail',
  REVIEW_REQUIRED: 'review',
  NOT_FOUND: 'missing',
  NO_APPLICABLE_RULE: 'none',
};

/** Fill colour for charts and bars, one per outcome (CSS variable, both themes). */
export const OUTCOME_FILL: Record<Outcome, string> = {
  PASS: 'var(--outcome-pass)',
  FAIL: 'var(--outcome-fail)',
  REVIEW_REQUIRED: 'var(--outcome-review)',
  NOT_FOUND: 'var(--outcome-missing)',
  NO_APPLICABLE_RULE: 'var(--muted-foreground)',
};

const outcomeBadgeVariants = cva(
  'inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap [&>svg]:size-3.5 [&>svg]:shrink-0',
  {
    variants: {
      tone: {
        // Filled: needs correction. Solid edge: looks right / needs a decision.
        // Dashed: waiting on a value. Plain: not applicable.
        pass: 'border-outcome-pass-fg/30 bg-outcome-pass-bg text-outcome-pass-fg',
        fail: 'border-outcome-fail-fg bg-outcome-fail-fg text-background',
        review: 'border-outcome-review-fg/60 bg-outcome-review-bg text-outcome-review-fg',
        missing: 'border-dashed border-outcome-missing-fg/60 bg-outcome-missing-bg text-outcome-missing-fg',
        none: 'border-transparent bg-transparent text-muted-foreground',
      },
    },
  },
);

export function OutcomeBadge({
  outcome,
  className,
  label = OUTCOME_LABELS[outcome],
}: {
  outcome: Outcome;
  className?: string;
  /** Override the word only for a deliberate, shorter context (a dense table); default is agreed. */
  label?: string;
}) {
  return (
    <span
      data-slot="outcome-badge"
      data-outcome={outcome}
      className={cn(outcomeBadgeVariants({ tone: OUTCOME_TONE[outcome] }), className)}
    >
      <OutcomeIcon outcome={outcome} size={14} />
      {label}
    </span>
  );
}
