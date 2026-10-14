import { OUTCOME_LABELS } from '../../data/outcomeLabels.js';
import type { Outcome, PackageStatus } from '../../data/types';
import { OutcomeIcon } from './OutcomeIcon.js';
import '../../design/components.css';

// ── Outcome → display config ─────────────────────────────────
//
// The colour and the dot are this file's business; the **word** is not. It comes from
// `OUTCOME_LABELS`, which the backend narration is also written against, so the badge and the
// sentence underneath it cannot say two different things about the same finding.
const OUTCOME_CONFIG: Record<Outcome, { cls: string }> = {
  PASS:               { cls: 'badge--pass' },
  FAIL:               { cls: 'badge--fail' },
  REVIEW_REQUIRED:    { cls: 'badge--review' },
  NOT_FOUND:          { cls: 'badge--missing' },
  NO_APPLICABLE_RULE: { cls: 'badge--none' },
};

const STATUS_LABELS: Record<PackageStatus, string> = {
  CREATED: 'Created',
  UPLOADING: 'Uploading',
  UPLOADED: 'Uploaded',
  INGESTING: 'Ingesting',
  EXTRACTING: 'Extracting',
  MATCHING: 'Matching',
  VALIDATING_EVIDENCE: 'Validating',
  RUNNING_CHECKS: 'Running Checks',
  GENERATING_OUTPUTS: 'Generating Outputs',
  AWAITING_REVIEW: 'Awaiting Review',
  APPROVED: 'Approved',
  CHANGES_REQUESTED: 'Changes Requested',
  // Neutral words, true for every cause (#1240 review): a reading or a check run that failed and
  // will run again, and an out-of-credit stop that waits for a person. While a failed reading
  // really restarts (`reading.retrying`), the badge says `RESTARTING_STATUS_WORDS` instead (#1249).
  FAILED_RETRYABLE: 'Stopped after a problem',
  FAILED_PERMANENT: 'Failed',
  NEEDS_INPUT: 'Needs Input',
  CANCELLED: 'Cancelled',
  SUPERSEDED: 'Superseded',
};

// ── OutcomeBadge ─────────────────────────────────────────────
interface OutcomeBadgeProps {
  outcome: Outcome;
  size?: 'sm' | 'md';
}

export function OutcomeBadge({ outcome, size = 'md' }: OutcomeBadgeProps) {
  const cfg = OUTCOME_CONFIG[outcome];
  return (
    <span
      className={`badge ${cfg.cls}`}
      style={size === 'sm' ? { fontSize: 'var(--text-xs)', padding: '1px 6px' } : undefined}
    >
      <OutcomeIcon outcome={outcome} size={12} />
      {OUTCOME_LABELS[outcome]}
    </span>
  );
}

// ── Package state words ──────────────────────────────────────
// The legacy StatusBadge that wore these is gone (#1125); the shadcn PackageStatusBadge uses them.
// The state is a bare `string` on the wire (`data/types.ts`), so an unknown one is shown by its own
// name rather than taking the badge down.

/**
 * The badge's words for a FAILED_RETRYABLE package while its failed reading is really starting
 * again (`reading.retrying`, #1249), so the badge agrees with the Reading step's "Starting the
 * reading again after a problem" (`RESTARTING_WORDS`). "Trying again" stays the AI's own (#1239).
 */
export const RESTARTING_STATUS_WORDS = 'Restarting after a problem';

/**
 * The words for a package state, shared with the shadcn status badge so both say the same thing.
 * `restarting` is the package's `reading.retrying`: only a FAILED_RETRYABLE package whose reading
 * restarts says so; every other cause keeps the neutral words.
 */
export function packageStatusLabel(status: string, { restarting = false }: { restarting?: boolean } = {}): string {
  if (restarting && status === 'FAILED_RETRYABLE') return RESTARTING_STATUS_WORDS;
  return STATUS_LABELS[status as PackageStatus] ?? humanise(status);
}

/** `AWAITING_REVIEW` → `Awaiting Review`, for a state this file has never heard of. */
function humanise(value: string): string {
  return value
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

// ── SeverityDot ──────────────────────────────────────────────
import type { Severity } from '../../data/types';

const SEVERITY_COLOR: Record<Severity, string> = {
  FLAG:     'var(--status-review)',
  CRITICAL: 'var(--status-fail)',
  MAJOR:    'var(--status-review)',
  MINOR:    'var(--status-missing)',
  ADVISORY: 'var(--text-faint)',
};

interface SeverityDotProps { severity: Severity }

export function SeverityDot({ severity }: SeverityDotProps) {
  return (
    <span
      aria-label={severity}
      data-tooltip={severity}
      style={{
        display: 'inline-block',
        width: '6px',
        height: '6px',
        borderRadius: '50%',
        background: SEVERITY_COLOR[severity],
        flexShrink: 0,
      }}
    />
  );
}
