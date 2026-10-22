import { useId, useState } from 'react';
import { ChevronRight } from 'lucide-react';

import { Collapse } from '@/components/chat/chat-motion';
import type { ChatCoverage, ChatFinding, ChatTrust, ChatUnknown, TrustLevel } from '@/lib/chat-api';
import { coverageCounts, coverageWords } from '@/lib/chat-findings';
import {
  checkCounts,
  checkLines,
  gvChecksOf,
  helperTimingRows,
  ledgerItems,
  ledgerWords,
  levelCounts,
  notOpenedWords,
  ROUND_WORDS,
  roundCounts,
  roundWords,
  TRUST_MEANING,
  TRUST_ORDER,
  TRUST_WORDS,
  timingRows,
  type TimingRow,
  UNKNOWN_WHY_WORDS,
  unknownsWords,
} from '@/lib/chat-trust';
import { cn } from '@/lib/utils';

/**
 * How far the findings can be believed, kept quiet (#1522; PATH step 4.7). Confidence is not a
 * status: on a row it is a small grey meter; the details (how sure the findings are, the pages, the
 * reading ledger, GV Review's own checks, what was not checked) sit in ONE folded "How this was
 * checked" section at the end of a whole-set answer. A narrow answer (a few pages) gets only one
 * plain confidence line. Code's words and counts only, no AI prose.
 */

const LEVEL_BAR: Record<TrustLevel, string> = {
  confirmed: 'bg-foreground/80',
  likely: 'bg-foreground/45',
  to_check: 'bg-foreground/15',
};

/** "How sure: 2 confirmed · 1 AI reading only": the only trust words a narrow answer shows. */
export function ConfidenceLine({ findings, trust }: { findings: readonly ChatFinding[]; trust: ChatTrust | null | undefined }) {
  const levels = levelCounts(findings, trust);
  if (!levels) return null;
  const parts = TRUST_ORDER.filter((level) => levels[level] > 0).map((level) => `${levels[level]} ${TRUST_WORDS[level].toLowerCase().replace('ai ', 'AI ')}`);
  if (!parts.length) return null;
  return (
    <p data-slot="chat-confidence-line" className="text-xs text-muted-foreground tabular-nums" title={TRUST_ORDER.map((level) => `${TRUST_WORDS[level]}: ${TRUST_MEANING[level]}`).join('\n')}>
      How sure: {parts.join(' · ')}
    </p>
  );
}

/**
 * The round chip ("Still wrong") when a set has rounds of comments; nothing for a single round.
 * An item compared with the vendor's own earlier round also says so ("Unchanged since round 1"):
 * information only, never a Right (2026-10-22).
 */
export function RoundChip({ finding }: { finding: Pick<ChatFinding, 'round_status' | 'round_match'> }) {
  const words = finding.round_status ? ROUND_WORDS[finding.round_status] : null;
  const match = finding.round_match?.words || null;
  if (!words && !match) return null;
  return (
    <>
      {words && <RoundStatusChip finding={finding} words={words} />}
      {match && (
        <span data-slot="chat-round-match" title="Information only: the vendor’s own earlier round is never counted as right" className="inline-flex w-fit rounded border border-dashed px-1 text-xs leading-4 text-muted-foreground">
          {match}
        </span>
      )}
    </>
  );
}

function RoundStatusChip({ finding, words }: { finding: Pick<ChatFinding, 'round_status'>; words: string }) {
  return (
    <span
      data-slot="chat-round"
      data-round={finding.round_status ?? undefined}
      className={cn(
        'inline-flex w-fit rounded border px-1 text-xs leading-4',
        finding.round_status === 'still_wrong' ? 'border-outcome-fail-fg/50 text-outcome-fail-fg' : 'text-muted-foreground',
      )}
    >
      {words}
    </span>
  );
}

// ── How this was checked ─────────────────────────────────────

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

function TimingTable({ rows, caption, slot, head }: { rows: TimingRow[]; caption: string; slot: string; head: string }) {
  return (
    <div className="max-w-full overflow-x-auto rounded-lg border">
      <table data-slot={slot} className="w-full border-collapse text-xs tabular-nums">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className="px-2.5 py-1.5 text-left font-medium">{head}</th>
            <th scope="col" className="px-2.5 py-1.5 text-right font-medium">Time</th>
            <th scope="col" className="px-2.5 py-1.5 text-right font-medium">Cost</th>
            <th scope="col" className="px-2.5 py-1.5 text-right font-medium">Calls</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} data-phase={row.key} className={cn('border-t align-top', row.key === 'total' && 'font-medium')}>
              <th scope="row" className="px-2.5 py-1 text-left font-[inherit] break-words">{row.label}</th>
              <td className="px-2.5 py-1 text-right whitespace-nowrap">{row.time}</td>
              <td className="px-2.5 py-1 text-right whitespace-nowrap">{row.cost}</td>
              <td className="px-2.5 py-1 text-right">{row.calls ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Where the answer's time and money went (#1512): one row per phase, then one per helper. */
function TimingsTable({ trust }: { trust: ChatTrust | null | undefined }) {
  const rows = timingRows(trust?.timings);
  if (!rows) return null;
  const helpers = helperTimingRows(trust?.timings);
  return (
    <div data-slot="chat-trust-timings" className="mt-2 grid min-w-0 gap-2">
      <p className="text-xs font-medium text-muted-foreground">Time and cost by phase (phases that ran together overlap)</p>
      <TimingTable rows={rows} caption="Time and cost by phase" slot="chat-trust-timings-phases" head="Phase" />
      {helpers.length > 0 && <TimingTable rows={helpers} caption="Time and cost by helper" slot="chat-trust-timings-helpers" head="Helper" />}
    </div>
  );
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
            {unknown.page != null && <span className="text-muted-foreground tabular-nums">p.{unknown.page}</span>}
            <span className="font-medium break-words">{unknown.what}</span>
            <span className="text-xs text-muted-foreground">{UNKNOWN_WHY_WORDS[unknown.why] ?? unknown.why}</span>
            <span className="min-w-0 text-xs break-words text-muted-foreground">needs: {unknown.needs}</span>
          </li>
        ))}
      </ul>
    </Disclosure>
  );
}

export interface HowCheckedProps {
  findings: readonly ChatFinding[];
  trust: ChatTrust | null | undefined;
  coverage: ChatCoverage | null | undefined;
  /** The page grid (coverage strip): whole-set answers only. */
  coverageStrip?: React.ReactNode;
  /** What the answer did not check (code's records). */
  unknowns?: readonly ChatUnknown[] | null;
}

/** One folded section at the end of a whole-set answer: everything about how it was checked. */
export function HowChecked({ findings, trust, coverage, coverageStrip, unknowns }: HowCheckedProps) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const levels = levelCounts(findings, trust);
  const rated = levels ? levels.confirmed + levels.likely + levels.to_check : 0;
  const counts = checkCounts(findings, trust);
  const rounds = roundCounts(findings, trust);
  const gv = gvChecksOf(trust);
  const leftOut = gv?.leftOut ?? [];
  const ledger = ledgerWords(trust?.ledger);
  const ledgerNamed = ledgerItems(trust?.ledger);
  const notOpened = notOpenedWords(coverage);
  const notChecked = [...(trust?.not_checked ?? [])];
  if (notOpened && !notChecked.some((line) => line.toLowerCase().includes('not opened'))) notChecked.push(notOpened);
  const heading = 'text-xs font-medium text-muted-foreground';

  return (
    <section data-slot="chat-trust" aria-label="How this was checked" className="min-w-0 border-t pt-3">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
        className="flex w-full min-w-0 items-center gap-2 rounded-md py-1 text-left text-sm font-medium text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <ChevronRight aria-hidden="true" className={cn('size-4 shrink-0 transition-transform duration-150 motion-reduce:transition-none', open && 'rotate-90')} />
        How this was checked
      </button>
      <Collapse open={open} id={bodyId}>
        <div className="mt-2 grid gap-4 rounded-xl border bg-card p-4 text-[13px]">
          {levels && rated > 0 && (
            <div className="grid gap-1.5">
              <p className={heading}>How sure the findings are</p>
              <div data-slot="chat-trust-bar" aria-hidden="true" className="flex h-1.5 w-full max-w-md overflow-hidden rounded-full bg-muted">
                {TRUST_ORDER.map((level) => levels[level] > 0 && (
                  <span key={level} data-level={level} className={cn('h-full', LEVEL_BAR[level])} style={{ width: `${(levels[level] / rated) * 100}%` }} />
                ))}
              </div>
              <ul data-slot="chat-trust-levels" aria-label="Findings by confidence" className="flex flex-wrap gap-x-4 gap-y-1">
                {TRUST_ORDER.map((level) => (
                  <li key={level} data-level={level} title={TRUST_MEANING[level]} className="inline-flex items-center gap-1.5">
                    <span aria-hidden="true" className={cn('inline-block size-2.5 rounded-sm', LEVEL_BAR[level])} />
                    <span>{TRUST_WORDS[level]}</span>
                    <span className="font-medium tabular-nums">{levels[level]}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {(coverageStrip || coverage || rounds) && (
            <div className="grid gap-1">
              <p className={heading}>Pages</p>
              {coverageStrip ?? (coverage && <p data-slot="chat-trust-coverage" className="tabular-nums">{coverageWords(coverageCounts(coverage))}</p>)}
              {rounds && <p data-slot="chat-trust-rounds" className="tabular-nums">{roundWords(rounds)}</p>}
            </div>
          )}

          {(ledger || gv || trust?.evidence) && (
            <div className="grid gap-1 text-muted-foreground">
              <p className={heading}>Checked by code</p>
              {trust?.evidence && (
                <p data-slot="chat-trust-evidence" className="tabular-nums">
                  <span className="text-foreground">{trust.evidence.observed ?? 0}</span> printed on the drawing ·{' '}
                  <span className="text-foreground">{trust.evidence.derived ?? 0}</span> worked out by code ·{' '}
                  <span className="text-foreground">{trust.evidence.inferred ?? 0}</span> AI reading only
                  {trust.not_compared ? <> · <span className="text-foreground">{trust.not_compared}</span> not compared</> : null}
                </p>
              )}
              {ledger && (ledgerNamed.length ? (
                <Disclosure slot="chat-trust-ledger" label={<>Read and checked by code: <span className="text-foreground">{ledger}</span></>}>
                  <ul data-slot="chat-trust-ledger-items" className="grid gap-1">
                    {ledgerNamed.map((group) => <li key={group.label} className="min-w-0 break-words"><span className="text-muted-foreground">{group.label}: </span>{group.names.join(', ')}</li>)}
                  </ul>
                </Disclosure>
              ) : <p data-slot="chat-trust-ledger" className="tabular-nums">Read and checked by code: <span className="text-foreground">{ledger}</span></p>)}
              {gv && (leftOut.length ? (
                <Disclosure slot="chat-trust-gv" label={<>Matches GV Review’s own checks: <span className="text-foreground">{gv.matched} of {gv.total}</span> · {leftOut.length} left out</>}>
                  <ul data-slot="chat-trust-left-out" aria-label="Left out of this answer" className="grid gap-1">
                    {leftOut.map((item, at) => (
                      <li key={at} className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                        {item.page != null && <span className="text-muted-foreground tabular-nums">p.{item.page}</span>}
                        <span className="font-medium break-words">{item.item}</span>
                        {item.values && <span className="break-words tabular-nums">{item.values}</span>}
                        {item.source && <span className="text-xs text-muted-foreground">{item.source}</span>}
                      </li>
                    ))}
                  </ul>
                </Disclosure>
              ) : <p data-slot="chat-trust-gv" className="tabular-nums">Matches GV Review’s own checks: <span className="text-foreground">{gv.matched} of {gv.total}</span></p>)}
            </div>
          )}

          {unknowns && unknowns.length > 0 && <UnknownsList unknowns={unknowns} />}

          <div className="grid gap-1">
            <p className={heading}>The checks</p>
            <ul data-slot="chat-trust-checks" className="grid gap-1">
              {checkLines(counts).map((line) => <li key={line} className="tabular-nums">{line}</li>)}
              {notChecked.map((line) => <li key={line} className="text-muted-foreground">Not checked: {line}</li>)}
            </ul>
            <TimingsTable trust={trust} />
          </div>
        </div>
      </Collapse>
    </section>
  );
}
