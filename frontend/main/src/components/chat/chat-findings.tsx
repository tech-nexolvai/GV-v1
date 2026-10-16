import { useEffect, useState } from 'react';
import { ImageOff, X } from 'lucide-react';

import { CheckIcons, RoundChip, TrustChip } from '@/components/chat/chat-trust';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { useMediaQuery } from '@/hooks/use-media-query';
import type { ChatFinding, FindingCategory, FindingStatus } from '@/lib/chat-api';
import {
  CATEGORY_WORDS,
  categoriesPresent,
  countWords,
  findingCounts,
  FINDINGS_NARROW,
  groupFindings,
  pageWords,
  sourceTag,
  STATUS_WORDS,
  type FindingFilter,
  type StatusFilter,
} from '@/lib/chat-findings';
import { useReducedMotion } from '@/lib/chat-motion';
import { matchesWords } from '@/lib/chat-full-check';
import { differenceOf, differenceWords, sideValue } from '@/lib/chat-trust';
import { cn } from '@/lib/utils';

/**
 * Chat v2 (#1483): an answer's findings as ONE compact table, the reviewer's evidence at a glance.
 * Grouped Countertops / Cabinets / Other; each row is Page · Item · Architect · Vendor · status ·
 * a small "checked on the drawing" tick · the two evidence crops (architect | vendor). A count line
 * and filter chips sit above it. Clicking a row opens the evidence viewer. On a phone the rows
 * become stacked cards. Values are the finding's own, verbatim, in the numbers font, never cut off.
 * Read-only: nothing here changes the review.
 */

const DROP_MS = 220;

// ── Small parts ─────────────────────────────────────────────

const STATUS_OUTCOME = { different: 'FAIL', check: 'REVIEW_REQUIRED', same: 'PASS' } as const;

const STATUS_CLASS: Record<FindingStatus, string> = {
  // Filled: the drawings disagree. Solid edge: a person should look. Light: they agree.
  different: 'border-outcome-fail-fg bg-outcome-fail-fg text-background',
  check: 'border-outcome-review-fg/60 bg-outcome-review-bg text-outcome-review-fg',
  same: 'border-outcome-pass-fg/30 bg-outcome-pass-bg text-outcome-pass-fg',
};

/** Different / Check / Same, with its glyph (colour is only the second signal). */
export function StatusChip({ status, className }: { status: FindingStatus; className?: string }) {
  return (
    <span
      data-slot="chat-finding-status"
      data-status={status}
      className={cn('inline-flex w-fit shrink-0 items-center gap-1 rounded-full border px-2 py-px text-xs font-medium whitespace-nowrap', STATUS_CLASS[status] ?? STATUS_CLASS.check, className)}
    >
      <OutcomeIcon outcome={STATUS_OUTCOME[status] ?? 'REVIEW_REQUIRED'} size={12} />
      {STATUS_WORDS[status] ?? status}
    </span>
  );
}

/** The source tag ("vs brand prototype") when the vendor was not compared with the architect. */
export function SourceTag({ finding }: { finding: Pick<ChatFinding, 'source'> }) {
  const tag = sourceTag(finding);
  if (!tag) return null;
  return (
    <span data-slot="chat-finding-source" className="mt-0.5 block w-fit rounded border border-dashed px-1 text-xs leading-4 text-muted-foreground">
      {tag}
    </span>
  );
}

/**
 * One side's value: code's inches ("37 in") with the printed form on hover, or the printed words as
 * written when code had no number, marked "not a number" when code could not read one.
 */
export function FindingValue({ finding, side }: { finding: ChatFinding; side: 'architect' | 'vendor' }) {
  const value = sideValue(finding, side);
  return (
    <span data-slot="chat-finding-value" data-side={side} className="inline-flex flex-wrap items-baseline gap-x-1.5">
      <span className="num break-words" title={value.printed ? `As printed: ${value.printed}` : undefined}>{value.shown}</span>
      {value.printed && <span className="sr-only">(printed {value.printed})</span>}
      {value.notANumber && <span data-slot="chat-not-a-number" className="text-xs text-muted-foreground">not a number</span>}
    </span>
  );
}

/** Code's difference, "+9 in"; a dash when code has none. */
export function FindingDifference({ finding }: { finding: ChatFinding }) {
  const difference = differenceOf(finding);
  return <span data-slot="chat-finding-difference" className={cn('num whitespace-nowrap', difference === null && 'text-muted-foreground')}>{difference === null ? '—' : differenceWords(difference)}</span>;
}

/** A tiny evidence crop, or a quiet placeholder when there is none (or it fails to load). */
export function EvidenceThumb({ src, label }: { src: string | null; label: string }) {
  const [broken, setBroken] = useState<string | null>(null);
  if (!src || broken === src) {
    return (
      <span
        data-slot="chat-evidence-thumb"
        data-missing="true"
        title={`${label}: no evidence picture`}
        className="grid h-9 w-12 shrink-0 place-items-center rounded-md border border-dashed text-muted-foreground/70"
      >
        <ImageOff aria-hidden="true" className="size-3.5" />
        <span className="sr-only">{label}: no evidence picture</span>
      </span>
    );
  }
  return (
    <img
      data-slot="chat-evidence-thumb"
      src={src}
      alt={`${label} evidence`}
      loading="lazy"
      onError={() => setBroken(src)}
      className="h-9 w-12 shrink-0 rounded-md border bg-white object-cover"
    />
  );
}

function Thumbs({ finding, pictureUrl }: { finding: ChatFinding; pictureUrl: (pictureId: string) => string }) {
  return (
    <span className="flex items-center gap-1">
      <EvidenceThumb src={finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null} label="Architect" />
      <EvidenceThumb src={finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null} label="Vendor" />
    </span>
  );
}

function Chip({ pressed, onClick, children, count }: { pressed: boolean; onClick: () => void; children: React.ReactNode; count?: number }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        'inline-flex h-7 items-center gap-1 rounded-full border px-2.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none',
        pressed ? 'border-foreground bg-foreground text-background' : 'bg-background text-muted-foreground hover:bg-accent hover:text-foreground',
      )}
    >
      {children}
      {count !== undefined && <span className="num opacity-80">{count}</span>}
    </button>
  );
}

// ── Rows that leave ─────────────────────────────────────────

/**
 * Findings that were just dropped, kept for a moment so their row fades out instead of vanishing.
 * Under reduced motion they go at once.
 */
function useLeaving(findings: readonly ChatFinding[]): ChatFinding[] {
  const reduced = useReducedMotion();
  const [previous, setPrevious] = useState(findings);
  const [leaving, setLeaving] = useState<ChatFinding[]>([]);
  if (previous !== findings) {
    // Worked out while rendering (React's "adjust state when a prop changes"), not in an effect.
    const ids = new Set(findings.map((finding) => finding.id));
    const gone = reduced ? [] : previous.filter((finding) => !ids.has(finding.id));
    setPrevious(findings);
    setLeaving([...leaving.filter((finding) => !ids.has(finding.id)), ...gone]);
  }
  useEffect(() => {
    if (!leaving.length) return;
    const timer = window.setTimeout(() => setLeaving([]), DROP_MS);
    return () => window.clearTimeout(timer);
  }, [leaving]);
  return leaving;
}

// ── The table ───────────────────────────────────────────────

export interface FindingsTableProps {
  findings: readonly ChatFinding[];
  filter: FindingFilter;
  onFilter: (filter: FindingFilter) => void;
  /** Open the evidence viewer on this finding. */
  onOpen: (finding: ChatFinding) => void;
  pictureUrl: (pictureId: string) => string;
  /**
   * Chat v4 full check: the status chips become three tabs, What's wrong · To check · What's right
   * (the parent picks the opening tab, never "all").
   */
  tabs?: boolean;
}

const TABS = [
  { status: 'different', words: 'What’s wrong' },
  { status: 'check', words: 'To check' },
  { status: 'same', words: 'What’s right' },
] as const;

/** Under a right finding's vendor value: what it matched ("matches architect"). */
function Matches({ finding }: { finding: ChatFinding }) {
  if (finding.status !== 'same') return null;
  return <span data-slot="chat-finding-matches" className="mt-0.5 block text-xs text-outcome-pass-fg">{matchesWords(finding.source)}</span>;
}

export function FindingsTable({ findings, filter, onFilter, onOpen, pictureUrl, tabs = false }: FindingsTableProps) {
  const narrow = useMediaQuery(FINDINGS_NARROW);
  const leaving = useLeaving(findings);
  const counts = findingCounts(findings);
  const summary = countWords(counts);
  const leavingIds = new Set(leaving.map((finding) => finding.id));
  const groups = groupFindings([...findings, ...leaving], filter);
  const categories = categoriesPresent(findings);
  const issues = counts.different + counts.check;
  // Code's difference column, once code has read any finding's numbers.
  const showDifference = findings.some((finding) => finding.inches);

  const setStatus = (status: StatusFilter) => onFilter({ ...filter, status });
  const setCategory = (category: FindingCategory) => onFilter({ ...filter, category: filter.category === category ? null : category });

  return (
    <section data-slot="chat-findings" aria-label="Findings" className="chat-rise grid min-w-0 gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
        {tabs ? (
          <div
            role="tablist"
            aria-label="Findings"
            data-slot="chat-findings-tabs"
            className="flex max-w-full min-w-0 flex-wrap gap-1 rounded-xl bg-muted p-1"
            onKeyDown={(event) => {
              if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
              const at = TABS.findIndex((tab) => tab.status === filter.status);
              const next = TABS[(at + (event.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
              setStatus(next.status);
              event.currentTarget.querySelector<HTMLElement>(`[data-tab="${next.status}"]`)?.focus();
            }}
          >
            {TABS.map((tab) => {
              const selected = filter.status === tab.status;
              return (
                <button
                  key={tab.status}
                  type="button"
                  role="tab"
                  data-tab={tab.status}
                  aria-selected={selected}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => setStatus(tab.status)}
                  className={cn(
                    'inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none',
                    selected ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  {tab.words}
                  <span className="tabular-nums opacity-80">({counts[tab.status]})</span>
                </button>
              );
            })}
          </div>
        ) : (
          <p data-slot="chat-findings-count" aria-live="polite" className="text-sm font-medium text-foreground tabular-nums">{summary}</p>
        )}
        <div role="group" aria-label="Filter findings" className="flex flex-wrap items-center gap-1.5">
          {!tabs && <Chip pressed={filter.status === 'all'} onClick={() => setStatus('all')} count={issues}>All</Chip>}
          {!tabs && <Chip pressed={filter.status === 'different'} onClick={() => setStatus('different')} count={counts.different}>Different</Chip>}
          {!tabs && <Chip pressed={filter.status === 'check'} onClick={() => setStatus('check')} count={counts.check}>Check</Chip>}
          {!tabs && counts.same > 0 && <Chip pressed={filter.status === 'same'} onClick={() => setStatus('same')} count={counts.same}>Same</Chip>}
          {!tabs && categories.length > 1 && <span aria-hidden="true" className="mx-0.5 h-4 w-px bg-border" />}
          {categories.length > 1 && categories.map((category) => (
            <Chip key={category} pressed={filter.category === category} onClick={() => setCategory(category)}>{CATEGORY_WORDS[category]}</Chip>
          ))}
          {filter.page !== null && (
            <button
              type="button"
              data-slot="chat-findings-page-filter"
              onClick={() => onFilter({ ...filter, page: null })}
              aria-label={`Showing page ${filter.page} only. Show all pages`}
              className="inline-flex h-7 items-center gap-1 rounded-full border border-foreground px-2.5 text-xs font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              <span className="num">Page {filter.page}</span>
              <X aria-hidden="true" className="size-3.5" />
            </button>
          )}
        </div>
      </div>

      {groups.length === 0 ? (
        <p data-slot="chat-findings-empty" className="rounded-lg border border-dashed px-3 py-2.5 text-sm text-muted-foreground">
          {findings.length ? 'No findings match this filter.' : 'No findings yet.'}
        </p>
      ) : narrow ? (
        <div data-slot="chat-findings-cards" className="grid gap-3">
          {groups.map((group) => (
            <section key={group.category} aria-label={CATEGORY_WORDS[group.category]} className="grid gap-1.5">
              <h4 className="text-xs font-semibold text-muted-foreground">
                {CATEGORY_WORDS[group.category]} <span className="num font-normal">{group.findings.length}</span>
              </h4>
              <ul className="grid gap-1.5">
                {group.findings.map((finding) => (
                  <li key={finding.id} data-slot="chat-finding-row" data-id={finding.id} data-leaving={leavingIds.has(finding.id) ? 'true' : undefined} className={cn(leavingIds.has(finding.id) && 'chat-leave')}>
                    <button
                      type="button"
                      onClick={() => onOpen(finding)}
                      aria-label={`${finding.item}: open the evidence`}
                      className="grid w-full min-w-0 gap-1.5 rounded-lg border bg-card px-3 py-2 text-left text-sm transition-colors hover:border-foreground/25 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none"
                    >
                      <span className="flex min-w-0 items-start justify-between gap-2">
                        <span className="grid min-w-0">
                          <span className="font-medium break-words">{finding.item}</span>
                          <RoundChip finding={finding} />
                        </span>
                        <StatusChip status={finding.status} />
                      </span>
                      <span className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[13px]">
                        <span className="text-muted-foreground">Page</span>
                        <span className="num">{pageWords(finding)}</span>
                        <span className="text-muted-foreground">Architect</span>
                        <span className="min-w-0"><FindingValue finding={finding} side="architect" /><SourceTag finding={finding} /></span>
                        <span className="text-muted-foreground">Vendor</span>
                        <span className="min-w-0"><FindingValue finding={finding} side="vendor" /><Matches finding={finding} /></span>
                        {showDifference && <span className="text-muted-foreground">Difference</span>}
                        {showDifference && <FindingDifference finding={finding} />}
                        <span className="text-muted-foreground">Trust</span>
                        <span className="flex flex-wrap items-center gap-2"><TrustChip finding={finding} /><CheckIcons finding={finding} /></span>
                      </span>
                      <Thumbs finding={finding} pictureUrl={pictureUrl} />
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      ) : (
        <div className="max-w-full overflow-x-auto rounded-xl border bg-card shadow-[0_1px_2px_rgb(0_0_0/0.03)]">
          <table data-slot="chat-findings-table" className="w-full border-collapse text-[13px] leading-snug">
            <caption className="sr-only">Findings: {summary}</caption>
            <thead className="bg-muted/50 text-xs text-muted-foreground">
              <tr>
                <th scope="col" className="px-2.5 py-2 text-left font-medium whitespace-nowrap">Page</th>
                <th scope="col" className="min-w-36 px-2.5 py-2 text-left font-medium">Item</th>
                <th scope="col" className="px-2.5 py-2 text-left font-medium">Architect</th>
                <th scope="col" className="px-2.5 py-2 text-left font-medium">Vendor</th>
                <th scope="col" className="px-2.5 py-2 text-left font-medium">Status</th>
                <th scope="col" className="px-2.5 py-2 text-left font-medium">Trust</th>
                <th scope="col" className="px-2.5 py-2 text-left font-medium">Evidence</th>
              </tr>
            </thead>
            {groups.map((group) => (
              <tbody key={group.category} data-slot="chat-findings-group" data-category={group.category}>
                <tr>
                  <th scope="rowgroup" colSpan={7} className="border-t bg-muted/25 px-2.5 py-1.5 text-left text-xs font-semibold text-muted-foreground">
                    {CATEGORY_WORDS[group.category]} <span className="num font-normal">{group.findings.length}</span>
                  </th>
                </tr>
                {group.findings.map((finding) => {
                  const gone = leavingIds.has(finding.id);
                  return (
                    <tr
                      key={finding.id}
                      data-slot="chat-finding-row"
                      data-id={finding.id}
                      data-leaving={gone ? 'true' : undefined}
                      onClick={gone ? undefined : () => onOpen(finding)}
                      className={cn('cursor-pointer border-t align-top transition-colors hover:bg-muted/40 motion-reduce:transition-none', gone && 'chat-leave pointer-events-none')}
                    >
                      <td className="num px-2.5 py-2 whitespace-nowrap">{pageWords(finding)}</td>
                      <th scope="row" className="px-2.5 py-2 text-left font-normal">
                        <button
                          type="button"
                          onClick={(event) => { event.stopPropagation(); onOpen(finding); }}
                          title="Open the evidence"
                          className="rounded-sm text-left font-medium break-words hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                        >
                          {finding.item}
                        </button>
                        <RoundChip finding={finding} />
                      </th>
                      <td className="px-2.5 py-2"><FindingValue finding={finding} side="architect" /><SourceTag finding={finding} /></td>
                      <td className="px-2.5 py-2">
                        <FindingValue finding={finding} side="vendor" />
                        <Matches finding={finding} />
                        {showDifference && differenceOf(finding) !== null && <span className="mt-0.5 block text-xs font-medium"><FindingDifference finding={finding} /></span>}
                      </td>
                      <td className="px-2.5 py-2"><StatusChip status={finding.status} /></td>
                      <td className="px-2.5 py-2"><span className="grid gap-1"><TrustChip finding={finding} /><CheckIcons finding={finding} /></span></td>
                      <td className="px-2.5 py-1.5"><Thumbs finding={finding} pictureUrl={pictureUrl} /></td>
                    </tr>
                  );
                })}
              </tbody>
            ))}
          </table>
        </div>
      )}
    </section>
  );
}
