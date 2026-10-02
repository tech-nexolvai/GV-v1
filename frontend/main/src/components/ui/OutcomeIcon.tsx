import { AlertCircle, CheckCircle2, CircleDashed, MinusCircle, XCircle } from 'lucide-react';
import type { Outcome } from '../../data/types';

// Presentation only: the stored outcome and its shared wording remain authoritative.
const ICONS = {
  PASS: CheckCircle2,
  FAIL: XCircle,
  REVIEW_REQUIRED: AlertCircle,
  NOT_FOUND: CircleDashed,
  NO_APPLICABLE_RULE: MinusCircle,
} as const;

/** Decorative companion to a visible outcome label, never a replacement for that label. */
export function OutcomeIcon({ outcome, size = 14, className }: {
  outcome: Outcome;
  size?: number;
  className?: string;
}) {
  const Icon = ICONS[outcome];
  return <Icon size={size} className={className} data-outcome-icon={outcome} aria-hidden="true" focusable="false" style={{ flexShrink: 0 }} />;
}
