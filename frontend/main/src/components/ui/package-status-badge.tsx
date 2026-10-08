import { CheckCircle2, CircleDashed, Clock, MinusCircle, PenLine, XCircle } from 'lucide-react';

import { cn } from '@/lib/utils';
import { packageStatusLabel } from '@/components/ui/StatusBadge';

type Tone = 'working' | 'review' | 'approved' | 'failed' | 'quiet';

const TONE: Record<string, Tone> = {
  UPLOADING: 'working', UPLOADED: 'working', INGESTING: 'working', EXTRACTING: 'working', MATCHING: 'working',
  VALIDATING_EVIDENCE: 'working', RUNNING_CHECKS: 'working', GENERATING_OUTPUTS: 'working', FAILED_RETRYABLE: 'working',
  AWAITING_REVIEW: 'review', NEEDS_INPUT: 'review', CHANGES_REQUESTED: 'failed',
  APPROVED: 'approved', FAILED_PERMANENT: 'failed',
  CREATED: 'quiet', CANCELLED: 'quiet', SUPERSEDED: 'quiet',
};

const ICON = { working: Clock, review: PenLine, approved: CheckCircle2, failed: XCircle, quiet: MinusCircle } as const;

const TONE_CLASS: Record<Tone, string> = {
  working: 'border-border bg-muted text-muted-foreground',
  review: 'border-outcome-review-fg/50 bg-outcome-review-bg text-outcome-review-fg',
  approved: 'border-outcome-pass-fg/40 bg-outcome-pass-bg text-outcome-pass-fg',
  failed: 'border-outcome-fail-fg/60 bg-outcome-fail-bg text-outcome-fail-fg',
  quiet: 'border-dashed border-border text-muted-foreground',
};

/**
 * A package's lifecycle state: the same words as the legacy StatusBadge, with an icon so the colour
 * never carries the meaning alone. An unknown state still renders, by its own name.
 */
export function PackageStatusBadge({ status, className }: { status: string; className?: string }) {
  const tone = TONE[status] ?? 'quiet';
  const Icon = TONE[status] ? ICON[tone] : CircleDashed;
  return (
    <span
      data-slot="package-status-badge"
      data-status={status}
      className={cn(
        'inline-flex w-fit shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 font-sans text-xs font-medium whitespace-nowrap',
        TONE_CLASS[tone],
        className,
      )}
    >
      <Icon className="size-3.5" aria-hidden="true" />
      {packageStatusLabel(status)}
    </span>
  );
}
