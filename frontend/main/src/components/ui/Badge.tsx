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

const STATUS_CONFIG: Record<PackageStatus, { label: string; cls: string }> = {
  CREATED:             { label: 'Created',            cls: 'badge--muted'      },
  UPLOADING:           { label: 'Uploading',          cls: 'badge--processing' },
  UPLOADED:            { label: 'Uploaded',           cls: 'badge--processing' },
  INGESTING:           { label: 'Ingesting',          cls: 'badge--processing' },
  EXTRACTING:          { label: 'Extracting',         cls: 'badge--processing' },
  MATCHING:            { label: 'Matching',           cls: 'badge--processing' },
  VALIDATING_EVIDENCE: { label: 'Validating',         cls: 'badge--processing' },
  RUNNING_CHECKS:      { label: 'Running Checks',     cls: 'badge--processing' },
  GENERATING_OUTPUTS:  { label: 'Generating Outputs', cls: 'badge--processing' },
  AWAITING_REVIEW:     { label: 'Awaiting Review',    cls: 'badge--review'     },
  APPROVED:            { label: 'Approved',           cls: 'badge--pass'       },
  CHANGES_REQUESTED:   { label: 'Changes Requested',  cls: 'badge--fail'       },
  FAILED_RETRYABLE:    { label: 'Failed — Retrying',  cls: 'badge--processing' },
  FAILED_PERMANENT:    { label: 'Failed',             cls: 'badge--fail'       },
  NEEDS_INPUT:         { label: 'Needs Input',        cls: 'badge--review'     },
  CANCELLED:           { label: 'Cancelled',          cls: 'badge--muted'      },
  SUPERSEDED:          { label: 'Superseded',         cls: 'badge--muted'      },
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

// ── StatusBadge ──────────────────────────────────────────────
interface StatusBadgeProps {
  /**
   * Widened to `string` because that is what the API actually sends.
   *
   * `data/types.ts` says so directly: the package `state` is published as a bare `string` with no
   * named schema behind it, so `PackageStatus` is the one list in this app that can drift without
   * the compiler noticing. Requiring the union here did not prevent that drift — it only forced
   * every caller to assert its way past it, and `STATUS_CONFIG[unknown]` is `undefined`, so the
   * next state the server adds would have taken the badge down with `cfg.label` on undefined.
   */
  status: PackageStatus | string;
  size?: 'sm' | 'md';
}

export function StatusBadge({ status, size = 'md' }: StatusBadgeProps) {
  const cfg = STATUS_CONFIG[status as PackageStatus];
  // An unrecognised state shows its own name in a neutral badge. A reviewer seeing
  // "Awaiting Second Look" they cannot find in the docs is a far better outcome than a blank
  // screen, and it makes the drift visible instead of fatal.
  const label = cfg?.label ?? humanise(status);
  const cls = cfg?.cls ?? 'badge--muted';

  return (
    <span
      className={`badge ${cls}`}
      style={size === 'sm' ? { fontSize: 'var(--text-xs)', padding: '1px 6px' } : undefined}
    >
      {label}
    </span>
  );
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
