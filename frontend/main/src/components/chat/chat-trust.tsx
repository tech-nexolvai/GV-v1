import { useId, useState } from 'react';
import { BadgeCheck, Calculator, ChevronRight, Eye, Scale, ScanText, ShieldAlert, ShieldCheck, ShieldHalf } from 'lucide-react';

import { Collapse } from '@/components/chat/chat-motion';
import type { ChatCoverage, ChatFinding, ChatTrust, ChatUnknown, TrustLevel } from '@/lib/chat-api';
import { coverageCounts, coverageWords } from '@/lib/chat-findings';
import {
  checkCounts,
  checkLines,
  EVIDENCE_WORDS,
  evidenceKindOf,
  evidenceWords,
  findingChecks,
  levelCounts,
  notOpenedWords,
  ROUND_WORDS,
  roundCounts,
  roundWords,
  TRUST_MEANING,
  TRUST_ORDER,
  TRUST_WORDS,
  trustLevelOf,
  trustReason,
  UNKNOWN_WHY_WORDS,
  unknownsWords,
  type CheckResult,
  type CheckState,
} from '@/lib/chat-trust';
import { cn } from '@/lib/utils';

/**
 * Chat v3 (CONTRACT3-trust, #1483): TRUST, shown. Why each finding can be believed: the checks code
 * ran on it (printed at the spot, a second independent read, the lead's own check on the drawing, a
 * GV rule or the vendor's arithmetic) and the level code gave it (Confirmed / Likely / To check).
 * Above the table, a summary: a stacked bar by level, the coverage line, how many of GV Review's own
 * recorded differences the answer includes (the left-out ones listed), and how it was checked.
 * Calm and factual: code's words and counts only, no AI prose.
 */

// ── Level chip ──────────────────────────────────────────────

const LEVEL_CLASS: Record<TrustLevel, string> = {
  confirmed: 'border-outcome-pass-fg/40 bg-outcome-pass-bg text-outcome-pass-fg',
  likely: 'border-border bg-muted text-foreground',
  to_check: 'border-outcome-review-fg/60 bg-outcome-review-bg text-outcome-review-fg',
};

const LEVEL_ICON = { confirmed: ShieldCheck, likely: ShieldHalf, to_check: ShieldAlert } as const;

const LEVEL_BAR: Record<TrustLevel, string> = {
  confirmed: 'bg-outcome-pass',
  likely: 'bg-foreground/45',
  to_check: 'bg-outcome-review',
};

/** Confirmed / Likely / To check, with its glyph; the tooltip says what the level means (and why it dropped). */
export function TrustChip({ finding }: { finding: ChatFinding }) {
  const level = trustLevelOf(finding);
  if (!level) {
    return <span data-slot="chat-trust-level" data-level="none" title="The checks have not run yet" className="text-xs text-muted-foreground">Not rated</span>;
  }
  const Icon = LEVEL_ICON[level];
  const reason = trustReason(finding);
  const evidence = evidenceWords(finding);
  const title = (reason ? `${TRUST_WORDS[level]}: ${reason}` : `${TRUST_WORDS[level]}: ${TRUST_MEANING[level]}`) + (evidence ? ` · Evidence: ${evidence}` : '');
  return (
    <span
      data-slot="chat-trust-level"
      data-level={level}
      title={title}
      className={cn('inline-flex w-fit shrink-0 items-center gap-1 rounded-full border px-2 py-px text-xs font-medium whitespace-nowrap', LEVEL_CLASS[level])}
    >
      <Icon aria-hidden="true" className="size-3" />
      {TRUST_WORDS[level]}
      {reason && <span className="sr-only">: {reason}</span>}
    </span>
  );
}

/** What backs it, in a few words: "printed on the drawing" / "worked out by code" / "AI reading only". */
export function EvidenceTag({ finding }: { finding: ChatFinding }) {
  const kind = evidenceKindOf(finding);
  if (!kind) return null;
  return (
    <span
      data-slot="chat-evidence-kind"
      data-kind={kind}
      title={evidenceWords(finding) ?? undefined}
      className={cn('w-fit text-xs whitespace-nowrap', kind === 'inferred' ? 'text-outcome-review-fg' : 'text-muted-foreground')}
    >
      {EVIDENCE_WORDS[kind]}
    </span>
  );
}

// ── Check icons ─────────────────────────────────────────────

const CHECK_ICON = { printed: ScanText, second_read: Eye, verified: BadgeCheck, rule: Scale } as const;

/** The state's mark beside the icon, so it reads without colour: ✓ passed, ✗ disagreed, ? unsure, – not run. */
const STATE_MARK: Record<CheckState, string> = { pass: '✓', fail: '✗', unsure: '?', not_run: '–' };

const STATE_CLASS: Record<CheckState, string> = {
  pass: 'text-foreground',
  fail: 'text-outcome-fail-fg',
  unsure: 'text-outcome-review-fg',
  not_run: 'text-muted-foreground/55',
};

function CheckIcon({ check }: { check: CheckResult }) {
  const Icon = check.key === 'rule' && check.name === 'Arithmetic' ? Calculator : CHECK_ICON[check.key];
  return (
    <li
      data-slot="chat-check"
      data-check={check.key}
      data-state={check.state}
      title={check.words}
      className={cn('inline-flex items-center gap-px', STATE_CLASS[check.state])}
    >
      <Icon aria-hidden="true" className="size-3.5" />
      <span aria-hidden="true" className="text-xs leading-none font-semibold">{STATE_MARK[check.state]}</span>
      <span className="sr-only">{check.words}</span>
    </li>
  );
}

/** Printed · 2nd read · Checked on drawing · Rule/Arithmetic, each with what passed or which disagreed. */
export function CheckIcons({ finding }: { finding: ChatFinding }) {
  return (
    <ul data-slot="chat-checks" aria-label="Checks" className="flex items-center gap-1.5">
      {findingChecks(finding).map((check) => <CheckIcon key={check.key} check={check} />)}
    </ul>
  );
}

/** The checks as words, for the evidence viewer (under the crops). */
export function CheckList({ finding, only }: { finding: ChatFinding; only?: readonly CheckResult['key'][] }) {
  const checks = findingChecks(finding).filter((check) => !only || only.includes(check.key));
  return (
    <ul data-slot="chat-check-list" aria-label="Checks" className="grid gap-1 text-[13px]">
      {checks.map((check) => (
        <li key={check.key} data-check={check.key} data-state={check.state} className="flex min-w-0 items-start gap-1.5">
          <span aria-hidden="true" className={cn('num w-3 shrink-0 text-center font-semibold', STATE_CLASS[check.state])}>{STATE_MARK[check.state]}</span>
          <span className={cn('min-w-0 break-words', check.state === 'not_run' ? 'text-muted-foreground' : 'text-foreground')}>{check.words}</span>
        </li>
      ))}
    </ul>
  );
}

/** The round chip ("Still wrong") when a set has rounds of comments; nothing for a single round. */
export function RoundChip({ finding }: { finding: Pick<ChatFinding, 'round_status'> }) {
  const words = finding.round_status ? ROUND_WORDS[finding.round_status] : null;
  if (!words) return null;
  return (
    <span
      data-slot="chat-round"
      data-round={finding.round_status ?? undefined}
      className={cn(
        'mt-0.5 inline-flex w-fit rounded border px-1 text-xs leading-4',
        finding.round_status === 'still_wrong' ? 'border-outcome-fail-fg/50 text-outcome-fail-fg' : 'text-muted-foreground',
      )}
    >
      {words}
    </span>
  );
}

// ── The summary above the table ─────────────────────────────

function Disclosure({ label, children, slot }: { label: React.ReactNode; children: React.ReactNode; slot: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div data-slot={slot} className="min-w-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-md py-0.5 text-left text-xs text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <span className="min-w-0 tabular-nums">{label}</span>
        <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
      </button>
      <Collapse open={open} id={id}>
        <div className="pt-1 pb-0.5 pl-1">{children}</div>
      </Collapse>
    </div>
  );
}

export interface TrustSummaryProps {
  findings: readonly ChatFinding[];
  trust: ChatTrust | null | undefined;
  coverage: ChatCoverage | null | undefined;
  /** The interactive coverage strip, shown in place of the plain coverage line when given. */
  coverageStrip?: React.ReactNode;
  /** What the answer did not check (code's records), listed under the card. */
  unknowns?: readonly ChatUnknown[] | null;
}

/** Under the trust card: "3 not checked", each with why and what is needed to settle it. */
export function UnknownsList({ unknowns }: { unknowns: readonly ChatUnknown[] }) {
  const words = unknownsWords(unknowns);
  if (!words) return null;
  return (
    <Disclosure slot="chat-trust-unknowns" label={<><span className="text-foreground">{words}</span>: why · what’s needed</>}>
      <ul data-slot="chat-unknowns" aria-label="Not checked" className="grid gap-1 text-[13px]">
        {unknowns.map((unknown) => (
          <li key={unknown.id} data-why={unknown.why} className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            {unknown.page != null && <span className="num text-muted-foreground">p.{unknown.page}</span>}
            <span className="font-medium break-words">{unknown.what}</span>
            <span className="text-xs text-outcome-review-fg">{UNKNOWN_WHY_WORDS[unknown.why] ?? unknown.why}</span>
            <span className="min-w-0 text-xs break-words text-muted-foreground">needs: {unknown.needs}</span>
          </li>
        ))}
      </ul>
    </Disclosure>
  );
}

export function TrustSummary({ findings, trust, coverage, coverageStrip, unknowns }: TrustSummaryProps) {
  const levels = levelCounts(findings, trust);
  const rated = levels ? levels.confirmed + levels.likely + levels.to_check : 0;
  const counts = checkCounts(findings, trust);
  const rounds = roundCounts(findings, trust);
  const gv = trust?.gv_checks ?? null;
  const leftOut = gv?.left_out ?? [];
  const notOpened = notOpenedWords(coverage);
  const notChecked = [...(trust?.not_checked ?? [])];
  if (notOpened && !notChecked.some((line) => line.toLowerCase().includes('not opened'))) notChecked.push(notOpened);

  return (
    <section data-slot="chat-trust" aria-label="Why this can be trusted" className="chat-rise grid min-w-0 gap-3 rounded-2xl border bg-card px-4 py-3.5 shadow-[0_1px_2px_rgb(0_0_0/0.03)]">
      <p aria-hidden="true" className="text-xs font-medium tracking-wide text-muted-foreground">Why you can trust this</p>
      {levels && rated > 0 && (
        <div className="grid gap-1.5">
          <div data-slot="chat-trust-bar" aria-hidden="true" className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
            {TRUST_ORDER.map((level) => levels[level] > 0 && (
              <span key={level} data-level={level} className={cn('h-full', LEVEL_BAR[level])} style={{ width: `${(levels[level] / rated) * 100}%` }} />
            ))}
          </div>
          <ul data-slot="chat-trust-levels" aria-label="Findings by trust" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            {TRUST_ORDER.map((level) => (
              <li key={level} data-level={level} title={TRUST_MEANING[level]} className="inline-flex items-center gap-1.5">
                <span aria-hidden="true" className={cn('inline-block size-2.5 rounded-sm', LEVEL_BAR[level])} />
                <span>{TRUST_WORDS[level]}</span>
                <span className="num font-medium">{levels[level]}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid gap-0.5 text-xs text-muted-foreground">
        {coverageStrip ?? (coverage && <p data-slot="chat-trust-coverage" className="tabular-nums">{coverageWords(coverageCounts(coverage))}</p>)}
        {rounds && <p data-slot="chat-trust-rounds" className="text-foreground tabular-nums">{roundWords(rounds)}</p>}
      </div>

      {gv && (
        leftOut.length ? (
          <Disclosure slot="chat-trust-gv" label={<>Matches GV Review’s own checks: <span className="text-foreground">{gv.matched} of {gv.total}</span> · {leftOut.length} left out</>}>
            <ul data-slot="chat-trust-left-out" aria-label="Left out of this answer" className="grid gap-1 text-[13px]">
              {leftOut.map((item, at) => (
                <li key={at} className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                  {item.page != null && <span className="num text-muted-foreground">p.{item.page}</span>}
                  <span className="font-medium break-words">{item.item}</span>
                  {item.values && <span className="num break-words">{item.values}</span>}
                  {item.source && <span className="text-xs text-muted-foreground">{item.source}</span>}
                </li>
              ))}
            </ul>
          </Disclosure>
        ) : (
          <p data-slot="chat-trust-gv" className="text-xs tabular-nums text-muted-foreground">
            Matches GV Review’s own checks: <span className="text-foreground">{gv.matched} of {gv.total}</span>
          </p>
        )
      )}

      {trust?.evidence && (
        <p data-slot="chat-trust-evidence" className="text-xs tabular-nums text-muted-foreground">
          Evidence: <span className="text-foreground">{trust.evidence.observed ?? 0}</span> printed on the drawing ·{' '}
          <span className="text-foreground">{trust.evidence.derived ?? 0}</span> worked out by code ·{' '}
          <span className="text-foreground">{trust.evidence.inferred ?? 0}</span> AI reading only
          {trust.not_compared ? <> · <span className="text-foreground">{trust.not_compared}</span> not compared</> : null}
        </p>
      )}

      {unknowns && unknowns.length > 0 && <UnknownsList unknowns={unknowns} />}

      <Disclosure slot="chat-trust-how" label="How this was checked">
        <ul data-slot="chat-trust-checks" className="grid gap-1 text-[13px]">
          {checkLines(counts).map((line) => <li key={line} className="tabular-nums">{line}</li>)}
          {notChecked.map((line) => <li key={line} className="text-muted-foreground">Not checked: {line}</li>)}
        </ul>
      </Disclosure>
    </section>
  );
}
