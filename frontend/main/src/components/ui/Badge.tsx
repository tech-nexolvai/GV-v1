import type { Outcome, PackageStatus } from '../../data/types';
import '../../design/components.css';

// ── Outcome → display config ─────────────────────────────────
//
// **These say what happened to the check, not what the engine called it.**
//
// `NOT_FOUND` was rendered as "NOT FOUND", which a reviewer reads as *the evidence was not found*
// — beside an evidence panel that may be showing a crop perfectly well. It means something else
// entirely: the check could not run because a value it needs has not been supplied. The narration
// under the same card already says "Waiting on a value"; the badge was the one place still
// speaking the engine's vocabulary, and the two meanings of "not found" collided on one screen.
//
// `NO_APPLICABLE_RULE` had the same problem in the other direction: "N/A RULE" reads like the rule
// is broken, when it means the rule deliberately does not apply to this package.
//
// The engine's own names are unchanged — `Outcome` is still the stored vocabulary, and the rule id
// is always shown beside the badge. Only the word a person reads is different.
const OUTCOME_CONFIG: Record<Outcome, { label: string; cls: string; dot: string }> = {
  PASS:               { label: 'PASS',          cls: 'badge--pass',    dot: '●' },
  FAIL:               { label: 'FAIL',          cls: 'badge--fail',    dot: '✕' },
  REVIEW_REQUIRED:    { label: 'NEEDS REVIEW',  cls: 'badge--review',  dot: '◎' },
  NOT_FOUND:          { label: 'NEEDS A VALUE', cls: 'badge--missing', dot: '–' },
  NO_APPLICABLE_RULE: { label: "DOESN'T APPLY", cls: 'badge--none',    dot: '—' },
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
      style={size === 'sm' ? { fontSize: '10px', padding: '1px 6px' } : undefined}
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
