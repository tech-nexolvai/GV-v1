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
  FAILED_RETRYABLE: 'Failed — Retrying',
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

/** The words for a package state, shared with the shadcn status badge so both say the same thing. */
export function packageStatusLabel(status: string): string {
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
