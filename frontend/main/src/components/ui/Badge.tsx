import type { Outcome, PackageStatus } from '../../data/mock';
import '../../design/components.css';

// ── Outcome → display config ─────────────────────────────────
const OUTCOME_CONFIG: Record<Outcome, { label: string; cls: string; dot: string }> = {
  PASS:               { label: 'PASS',        cls: 'badge--pass',    dot: '●' },
  FAIL:               { label: 'FAIL',        cls: 'badge--fail',    dot: '✕' },
  REVIEW_REQUIRED:    { label: 'REVIEW',      cls: 'badge--review',  dot: '◎' },
  NOT_FOUND:          { label: 'NOT FOUND',   cls: 'badge--missing', dot: '–' },
  NO_APPLICABLE_RULE: { label: 'N/A RULE',    cls: 'badge--none',    dot: '—' },
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
      style={size === 'sm' ? { fontSize: '10px', padding: '1px 6px' } : undefined}
    >
      <span aria-hidden="true">{cfg.dot}</span>
      {cfg.label}
    </span>
  );
}

// ── StatusBadge ──────────────────────────────────────────────
interface StatusBadgeProps {
  status: PackageStatus;
  size?: 'sm' | 'md';
}

export function StatusBadge({ status, size = 'md' }: StatusBadgeProps) {
  const cfg = STATUS_CONFIG[status];
  return (
    <span
      className={`badge ${cfg.cls}`}
      style={size === 'sm' ? { fontSize: '10px', padding: '1px 6px' } : undefined}
    >
      {cfg.label}
    </span>
  );
}

// ── SeverityDot ──────────────────────────────────────────────
import type { Severity } from '../../data/mock';

const SEVERITY_COLOR: Record<Severity, string> = {
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
