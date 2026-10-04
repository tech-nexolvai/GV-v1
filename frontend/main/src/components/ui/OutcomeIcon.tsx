import { AlertCircle, CheckCircle2, CircleDashed, MinusCircle, XCircle } from 'lucide-react';
import type { Outcome } from '../../data/types';

/** One shape for each recorded outcome, consistent in badges, tables and cards. */
const SHAPES = {
  PASS: CheckCircle2,
  FAIL: XCircle,
  REVIEW_REQUIRED: AlertCircle,
  NOT_FOUND: CircleDashed,
  NO_APPLICABLE_RULE: MinusCircle,
} satisfies Record<Outcome, typeof CheckCircle2>;

export function OutcomeIcon({ outcome, size = 13, className }: {
  outcome: Outcome;
  size?: number;
  className?: string;
}) {
  const Shape = SHAPES[outcome];
  return <Shape size={size} className={className} aria-hidden="true" />;
}
