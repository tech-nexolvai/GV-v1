import { useEffect, useId, useState } from 'react';
import { ArrowRight, ChevronRight, ChevronsDownUp, ChevronsUpDown, ExternalLink, ImageOff, X } from 'lucide-react';

import { CheckIcons, CheckLegend, CheckList, EvidenceTag, RoundChip, TrustChip } from '@/components/chat/chat-trust';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { useMediaQuery } from '@/hooks/use-media-query';
import type { ChatFinding, FindingCategory, FindingSource, FindingStatus } from '@/lib/chat-api';
import {
  architectPage,
  CATEGORY_WORDS,
  categoriesPresent,
  countWords,
  FEW_ROWS,
  findingCounts,
  FINDINGS_NARROW,
  groupByArea,
  groupCountWords,
  OPEN_GROUPS,
  pagesWords,
  pageWords,
  rowValue,
  sourceTag,
  STATUS_WORDS,
  type AreaGroup,
  type FindingFilter,
  type RowValue,
  type StatusFilter,
  sideLabels,
  vendorPage,
} from '@/lib/chat-findings';
import { useReducedMotion } from '@/lib/chat-motion';
import { matchesWords } from '@/lib/chat-full-check';
import { differenceOf, differenceWords, NOT_COMPARED_WORDS, notCompared, sideValue, sumWords } from '@/lib/chat-trust';
import { cn } from '@/lib/utils';

/**
 * The findings of an answer (#1483, reworked in #1508 after the owner's "clean but still weak").
 * (A full check opens with a summary strip first: `FindingsKpis` in chat-full-check.) The findings are grouped by AREA (the area the item names, else its page), the groups
 * with the most wrong items first, each folding. Every finding is ONE line: page · item · architect
 * → vendor in inches · the difference · status · trust · two evidence crops that are always shown.
 * A value that is a sentence shows a short pill; clicking the row opens its detail in place (full
 * text, why, checks, large crops, "Open viewer"). On a phone the rows become cards.
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

const CHIP = 'inline-flex w-fit shrink-0 items-center gap-1 rounded-full border px-2 py-px text-xs font-medium whitespace-nowrap';

/** Different / Check / Same, with its glyph (colour is only the second signal). */
export function StatusChip({ status, className }: { status: FindingStatus; className?: string }) {
  return (
    <span data-slot="chat-finding-status" data-status={status} className={cn(CHIP, STATUS_CLASS[status] ?? STATUS_CLASS.check, className)}>
      <OutcomeIcon outcome={STATUS_OUTCOME[status] ?? 'REVIEW_REQUIRED'} size={12} />
      {STATUS_WORDS[status] ?? status}
    </span>
  );
}

/** A row's status: a "Same" item that was never really compared says "Not compared" (not seen is not right). */
function RowStatus({ finding }: { finding: ChatFinding }) {
  if (finding.status === 'same' && notCompared(finding)) {
    const why = finding.not_compared_why ? NOT_COMPARED_WORDS[finding.not_compared_why] : null;
    return (
      <span data-slot="chat-finding-status" data-status="not_compared" title={why ? `Not compared: ${why}` : 'Not compared'} className={cn(CHIP, 'border-dashed text-muted-foreground')}>
        <OutcomeIcon outcome="NOT_FOUND" size={12} />
        Not compared
      </span>
    );
  }
  return <StatusChip status={finding.status} />;
}

/** A row's short form of the source tag; the full words are on hover. */
const SOURCE_SHORT: Partial<Record<FindingSource, string>> = {
  brand_prototype: 'prototype',
  rules: 'GV rule',
  vendor_arithmetic: 'own string',
  cross_view: 'other view',
  completeness: 'one side',
};

/**
 * The source tag ("vs brand prototype") when the vendor was not compared with the architect. In a
 * row (`inline`) it is the short form ("prototype") with the full words on hover.
 */
export function SourceTag({ finding, inline = false }: { finding: Pick<ChatFinding, 'source'>; inline?: boolean }) {
  const tag = sourceTag(finding);
  if (!tag) return null;
  const short = inline ? SOURCE_SHORT[finding.source] ?? tag : tag;
  return (
    <span
      data-slot="chat-finding-source"
      title={short !== tag ? tag : undefined}
      className={cn('w-fit shrink-0 rounded border border-dashed px-1 text-xs leading-4 whitespace-nowrap text-muted-foreground', inline ? 'inline-block' : 'mt-0.5 block')}
    >
      <span aria-hidden={short !== tag || undefined}>{short}</span>
      {short !== tag && <span className="sr-only">{tag}</span>}
    </span>
  );
}

/**
 * One side's value in full (the row's detail and the evidence viewer): code's inches ("37 in") with
 * the printed form on hover, or the printed words as written, with a quiet "not a number" pill when
 * code could not read a length there.
 */
export function FindingValue({ finding, side }: { finding: ChatFinding; side: 'architect' | 'vendor' }) {
  const value = sideValue(finding, side);
  // A vendor string's sum, written out by code, on the vendor value's tooltip.
  const sum = side === 'vendor' ? sumWords(finding) : null;
  const title = [value.printed ? `As printed: ${value.printed}` : null, sum ? `Code’s sum: ${sum}` : null].filter(Boolean).join('\n');
  return (
    <span data-slot="chat-finding-value" data-side={side} className="inline-flex flex-wrap items-baseline gap-x-1.5">
      <span className={cn('break-words', value.printed ? 'tabular-nums' : null)} title={title || undefined}>{value.shown}</span>
      {value.printed && <span className="text-xs text-muted-foreground">printed {value.printed}</span>}
      {sum && <span className="sr-only">(code’s sum: {sum})</span>}
      {value.notANumber && <span data-slot="chat-not-a-number" className="rounded-full border px-1.5 text-xs leading-4 text-muted-foreground">not a number</span>}
    </span>
  );
}

/**
 * A cross-view finding compares two views of the same side's drawings: its cells say so
 * ("Reference view" / "This view") under the table's Architect / Vendor headings.
 */
export function SideLabel({ finding, side }: { finding: ChatFinding; side: 'architect' | 'vendor' }) {
  if (finding.source !== 'cross_view') return null;
  return <span data-slot="chat-side-label" className="block text-xs text-muted-foreground">{sideLabels(finding)[side]}</span>;
}

/** Code's difference as a chip, "+9 in" (red when the drawings disagree); a quiet dash when code has none. */
export function FindingDifference({ finding }: { finding: ChatFinding }) {
  const difference = differenceOf(finding);
  if (difference === null) return <span data-slot="chat-finding-difference" className="text-muted-foreground/60">—</span>;
  return (
    <span
      data-slot="chat-finding-difference"
      className={cn(
        'inline-flex w-fit items-center rounded-md px-1.5 py-px text-xs font-semibold whitespace-nowrap tabular-nums',
        difference === 0 ? 'bg-muted text-muted-foreground'
          : finding.status === 'different' ? 'bg-outcome-fail-bg text-outcome-fail-fg'
          : 'bg-outcome-review-bg text-outcome-review-fg',
      )}
    >
      {differenceWords(difference)}
    </span>
  );
}

/** One side's value in a ROW: inches, a short printed value, or a pill for a sentence (full text on hover). */
function RowValueCell({ value, side, sum }: { value: RowValue; side: 'architect' | 'vendor'; sum: string | null }) {
  if (value.kind === 'none') return <span data-slot="chat-finding-value" data-side={side} className="text-muted-foreground/60">—</span>;
  if (value.kind === 'pill') {
    return (
      <span data-slot="chat-finding-value" data-side={side} data-pill={value.pill} title={value.text} className="inline-flex w-fit max-w-full rounded-full border bg-background px-1.5 text-xs leading-[18px] text-muted-foreground">
        <span aria-hidden="true">{value.pill}</span>
        <span className="sr-only">{value.text}</span>
      </span>
    );
  }
  const title = [value.kind === 'inches' && value.printed ? `As printed: ${value.printed}` : null, sum ? `Code’s sum: ${sum}` : null].filter(Boolean).join('\n');
  return (
    <span data-slot="chat-finding-value" data-side={side} title={title || (value.kind === 'short' ? value.text : undefined)} className="block min-w-0 truncate tabular-nums">
      {value.text}
      {value.kind === 'inches' && value.printed && <span className="sr-only"> (printed {value.printed})</span>}
    </span>
  );
}

/** Architect → vendor, the arrow lined up from row to row. */
function RowValues({ finding }: { finding: ChatFinding }) {
  const sum = sumWords(finding);
  return (
    <span className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-1.5">
      <RowValueCell value={rowValue(finding, 'architect')} side="architect" sum={null} />
      <ArrowRight aria-hidden="true" className="size-3 text-muted-foreground/70" />
      <RowValueCell value={rowValue(finding, 'vendor')} side="vendor" sum={sum} />
    </span>
  );
}

/** A small evidence crop, or a quiet placeholder when there is none (or it fails to load). */
export function EvidenceThumb({ src, label, size = 'row' }: { src: string | null; label: string; size?: 'row' | 'large' }) {
  const [broken, setBroken] = useState<string | null>(null);
  const box = size === 'large' ? 'h-44 w-full' : 'h-10 w-14';
  if (!src || broken === src) {
    return (
      <span
        data-slot="chat-evidence-thumb"
        data-missing="true"
        title={`${label}: no evidence picture`}
        className={cn('grid shrink-0 place-items-center rounded-md border border-dashed text-muted-foreground/70', box)}
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
      className={cn('shrink-0 rounded-md border bg-white', box, size === 'large' ? 'object-contain' : 'object-cover')}
    />
  );
}

/** The two crops of a row. With `onOpen` each opens the evidence viewer. */
function Thumbs({ finding, pictureUrl, onOpen }: { finding: ChatFinding; pictureUrl: (pictureId: string) => string; onOpen?: () => void }) {
  const labels = sideLabels(finding);
  const sides = [
    { key: 'architect', label: labels.architect, src: finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null },
    { key: 'vendor', label: labels.vendor, src: finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null },
  ];
  return (
    <span data-slot="chat-finding-thumbs" className="flex shrink-0 items-center gap-1">
      {sides.map((side) => (onOpen && side.src ? (
        <button
          key={side.key}
          type="button"
          onClick={(event) => { event.stopPropagation(); onOpen(); }}
          title={`Open the evidence: ${side.label}`}
          className="rounded-md transition-[filter] hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none dark:hover:brightness-110"
        >
          <EvidenceThumb src={side.src} label={side.label} />
        </button>
      ) : <EvidenceThumb key={side.key} src={side.src} label={side.label} />))}
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
      {count !== undefined && <span className="tabular-nums opacity-80">{count}</span>}
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

// ── A row's detail ──────────────────────────────────────────

/** Under a right finding's vendor value: what it matched ("matches architect"), or why it was not compared. */
function Matches({ finding }: { finding: ChatFinding }) {
  if (finding.status !== 'same') return null;
  if (notCompared(finding)) {
    const why = finding.not_compared_why ? NOT_COMPARED_WORDS[finding.not_compared_why] : null;
    return <span data-slot="chat-finding-not-compared" className="mt-0.5 block text-xs text-muted-foreground">{why ? `not compared: ${why}` : 'not compared'}</span>;
  }
  return <span data-slot="chat-finding-matches" className="mt-0.5 block text-xs text-outcome-pass-fg">{matchesWords(finding.source)}</span>;
}

/** Everything about one finding, in place: full values, why, checks, large crops and "Open viewer". */
function FindingDetail({ finding, pictureUrl, onOpen, title = true }: { finding: ChatFinding; pictureUrl: (pictureId: string) => string; onOpen: () => void; title?: boolean }) {
  const labels = sideLabels(finding);
  const sum = sumWords(finding);
  const crops = [
    { key: 'architect', label: labels.architect, page: architectPage(finding), src: finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null },
    { key: 'vendor', label: labels.vendor, page: vendorPage(finding), src: finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null },
  ];
  return (
    <div data-slot="chat-finding-detail-body" className="grid min-w-0 gap-4 text-[13px] leading-relaxed md:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
      <div className="grid min-w-0 content-start gap-3">
        {/* A card already shows the whole item; a row cuts it short. */}
        {title && <p className="text-sm font-medium break-words">{finding.item}</p>}
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5">
          <dt className="text-muted-foreground">{labels.architect}</dt>
          <dd className="min-w-0"><FindingValue finding={finding} side="architect" /><SourceTag finding={finding} /></dd>
          <dt className="text-muted-foreground">{labels.vendor}</dt>
          <dd className="min-w-0"><FindingValue finding={finding} side="vendor" /><Matches finding={finding} /></dd>
          {differenceOf(finding) !== null && <><dt className="text-muted-foreground">Difference</dt><dd><FindingDifference finding={finding} /></dd></>}
          {sum && <><dt className="text-muted-foreground">Code’s sum</dt><dd className="tabular-nums break-words">{sum}</dd></>}
          <dt className="text-muted-foreground">Page</dt>
          <dd className="tabular-nums">{pageWords(finding)}</dd>
          {finding.evidence_kind && <><dt className="text-muted-foreground">Evidence</dt><dd><EvidenceTag finding={finding} /></dd></>}
        </dl>
        {finding.why?.trim() && <p data-slot="chat-finding-why" className="break-words text-foreground/90">{finding.why}</p>}
        <div className="grid gap-1">
          <p className="text-xs font-medium text-muted-foreground">Checks</p>
          <CheckList finding={finding} />
        </div>
      </div>
      <div className="grid min-w-0 content-start gap-2">
        <div className="grid grid-cols-2 gap-2">
          {crops.map((crop) => (
            <figure key={crop.key} className="grid min-w-0 gap-1">
              <figcaption className="flex items-baseline gap-1.5 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{crop.label}</span>
                <span className="tabular-nums">page {crop.page}</span>
              </figcaption>
              {crop.src ? (
                <button type="button" onClick={onOpen} title="Open in the viewer" className="rounded-md focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
                  <EvidenceThumb src={crop.src} label={crop.label} size="large" />
                </button>
              ) : <EvidenceThumb src={null} label={crop.label} size="large" />}
            </figure>
          ))}
        </div>
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex h-8 w-fit items-center gap-1.5 justify-self-end rounded-lg border bg-background px-3 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <ExternalLink className="size-3.5" aria-hidden="true" />
          Open viewer
        </button>
      </div>
    </div>
  );
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
  { status: 'not_compared', words: 'Not compared' },
] as const;

/** Which groups are open: the default (the first three, or all when there are few rows), all, or a picked set. */
type OpenGroups = 'default' | 'all' | ReadonlySet<string>;

function GroupHeader({ group, open, onToggle, controls }: { group: AreaGroup; open: boolean; onToggle: () => void; controls: string }) {
  const counts = groupCountWords(group.counts);
  const pages = group.byPage ? null : pagesWords(group.pages);
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      data-slot="chat-findings-group-toggle"
      className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left text-xs focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset"
    >
      <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 text-muted-foreground transition-transform duration-150 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
      <span className="min-w-0 truncate text-[13px] font-semibold text-foreground" title={group.name}>{group.name}</span>
      {pages && <span className="shrink-0 text-muted-foreground tabular-nums">· {pages}</span>}
      {counts && (
        <span className="flex min-w-0 shrink-0 items-center gap-1.5 tabular-nums">
          <span aria-hidden="true" className="text-muted-foreground">·</span>
          {group.counts.different > 0 && <span className="font-medium text-outcome-fail-fg">{group.counts.different} wrong</span>}
          {group.counts.check > 0 && <span className="text-outcome-review-fg">{group.counts.check} to check</span>}
          {group.counts.same > 0 && <span className="text-outcome-pass-fg">{group.counts.same} right</span>}
          {group.counts.not_compared > 0 && <span className="text-muted-foreground">{group.counts.not_compared} not compared</span>}
        </span>
      )}
    </button>
  );
}

export function FindingsTable({ findings, filter, onFilter, onOpen, pictureUrl, tabs = false }: FindingsTableProps) {
  const narrow = useMediaQuery(FINDINGS_NARROW);
  const baseId = useId();
  const leaving = useLeaving(findings);
  const [openGroups, setOpenGroups] = useState<OpenGroups>('default');
  const [openRows, setOpenRows] = useState<ReadonlySet<string>>(() => new Set());
  const counts = findingCounts(findings);
  const summary = countWords(counts);
  // Only cross-view findings shown: the column headings name the views themselves.
  const allCrossView = findings.length > 0 && findings.every((finding) => finding.source === 'cross_view');
  const leavingIds = new Set(leaving.map((finding) => finding.id));
  const groups = groupByArea([...findings, ...leaving], filter);
  const shownRows = groups.reduce((total, group) => total + group.findings.length, 0);
  const categories = categoriesPresent(findings);
  const issues = counts.different + counts.check;

  // The first three groups open, or all when there are few rows or one page is picked.
  const openByDefault = (index: number) => shownRows <= FEW_ROWS || filter.page !== null || index < OPEN_GROUPS;
  const isOpen = (group: AreaGroup, index: number) => (openGroups === 'all' ? true : openGroups === 'default' ? openByDefault(index) : openGroups.has(group.key));
  const allOpen = groups.every((group, index) => isOpen(group, index));
  const toggleGroup = (group: AreaGroup) => {
    const now = new Set(groups.filter((each, index) => isOpen(each, index)).map((each) => each.key));
    if (now.has(group.key)) now.delete(group.key);
    else now.add(group.key);
    setOpenGroups(now);
  };
  const toggleRow = (id: string) => setOpenRows((now) => {
    const next = new Set(now);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });

  const setStatus = (status: StatusFilter) => onFilter({ ...filter, status });
  // "Not compared" shows only when an answer has such items (not seen is not right).
  const tabsShown = TABS.filter((tab) => tab.status !== 'not_compared' || counts.not_compared > 0 || filter.status === 'not_compared');
  const setCategory = (category: FindingCategory) => onFilter({ ...filter, category: filter.category === category ? null : category });
  const columns = 7;

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
              const at = tabsShown.findIndex((tab) => tab.status === filter.status);
              const next = tabsShown[(at + (event.key === 'ArrowRight' ? 1 : tabsShown.length - 1)) % tabsShown.length];
              setStatus(next.status);
              event.currentTarget.querySelector<HTMLElement>(`[data-tab="${next.status}"]`)?.focus();
            }}
          >
            {tabsShown.map((tab) => {
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
          {!tabs && counts.not_compared > 0 && <Chip pressed={filter.status === 'not_compared'} onClick={() => setStatus('not_compared')} count={counts.not_compared}>Not compared</Chip>}
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
              <span className="tabular-nums">Page {filter.page}</span>
              <X aria-hidden="true" className="size-3.5" />
            </button>
          )}
        </div>
        {groups.length > 1 && (
          <button
            type="button"
            data-slot="chat-findings-expand"
            onClick={() => setOpenGroups(allOpen ? new Set() : 'all')}
            className="ml-auto inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            {allOpen ? <ChevronsDownUp aria-hidden="true" className="size-3.5" /> : <ChevronsUpDown aria-hidden="true" className="size-3.5" />}
            {allOpen ? 'Collapse all' : 'Expand all'}
          </button>
        )}
      </div>

      {groups.length === 0 ? (
        <p data-slot="chat-findings-empty" className="rounded-lg border border-dashed px-3 py-2.5 text-sm text-muted-foreground">
          {findings.length ? 'No findings match this filter.' : 'No findings yet.'}
        </p>
      ) : narrow ? (
        <div data-slot="chat-findings-cards" className="grid gap-2">
          {groups.map((group, index) => {
            const open = isOpen(group, index);
            const listId = `${baseId}-group-${index}`;
            return (
              <section key={group.key} data-slot="chat-findings-group" data-area={group.key} data-open={open ? 'true' : 'false'} aria-label={group.name} className="grid gap-1.5 rounded-xl border bg-muted/20">
                <GroupHeader group={group} open={open} onToggle={() => toggleGroup(group)} controls={listId} />
                {open && (
                  <ul id={listId} className="grid gap-1.5 px-1.5 pb-1.5">
                    {group.findings.map((finding) => {
                      const expanded = openRows.has(finding.id);
                      const detailId = `${baseId}-detail-${finding.id}`;
                      return (
                        <li key={finding.id} data-slot="chat-finding-row" data-id={finding.id} data-leaving={leavingIds.has(finding.id) ? 'true' : undefined} className={cn('rounded-lg border bg-card', leavingIds.has(finding.id) && 'chat-leave')}>
                          <button
                            type="button"
                            aria-expanded={expanded}
                            aria-controls={detailId}
                            onClick={() => toggleRow(finding.id)}
                            aria-label={`${finding.item}: show the details`}
                            className="flex w-full min-w-0 items-start gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                          >
                            <span className="grid min-w-0 flex-1 gap-1">
                              <span className="flex min-w-0 items-center gap-1.5">
                                <RowStatus finding={finding} />
                                <span className="text-xs text-muted-foreground tabular-nums">p.{pageWords(finding)}</span>
                              </span>
                              <span className="line-clamp-2 font-medium break-words">{finding.item}</span>
                              <span className="flex min-w-0 items-center gap-2 text-[13px]">
                                <span className="min-w-0 flex-1"><RowValues finding={finding} /></span>
                                <FindingDifference finding={finding} />
                              </span>
                              <span className="flex flex-wrap items-center gap-2"><TrustChip finding={finding} /><CheckIcons finding={finding} /><RoundChip finding={finding} /></span>
                            </span>
                            <Thumbs finding={finding} pictureUrl={pictureUrl} />
                          </button>
                          {expanded && (
                            <div id={detailId} data-slot="chat-finding-detail" className="border-t px-2.5 py-3">
                              <FindingDetail finding={finding} pictureUrl={pictureUrl} onOpen={() => onOpen(finding)} title={false} />
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            );
          })}
          <CheckLegend />
        </div>
      ) : (
        <div className="grid gap-2 rounded-xl border bg-card shadow-[0_1px_2px_rgb(0_0_0/0.03)]">
          <div className="max-w-full overflow-x-auto rounded-t-xl">
            <table data-slot="chat-findings-table" className="w-full min-w-[54rem] table-fixed border-collapse text-[13px] leading-snug">
              <caption className="sr-only">Findings: {summary}</caption>
              <colgroup>
                <col className="w-[4.25rem]" />
                <col />
                <col className="w-[12.5rem]" />
                <col className="w-[5.5rem]" />
                <col className="w-[6.75rem]" />
                <col className="w-[7.75rem]" />
                <col className="w-[8rem]" />
              </colgroup>
              <thead className="bg-muted/50 text-xs text-muted-foreground">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left font-medium">Page</th>
                  <th scope="col" className="px-2 py-2 text-left font-medium">Item</th>
                  <th scope="col" className="px-2 py-2 text-left font-medium">{allCrossView ? 'Reference view → This view' : 'Architect → Vendor'}</th>
                  <th scope="col" className="px-2 py-2 text-left font-medium">Difference</th>
                  <th scope="col" className="px-2 py-2 text-left font-medium">Status</th>
                  <th scope="col" className="px-2 py-2 text-left font-medium">Trust</th>
                  <th scope="col" className="px-3 py-2 text-left font-medium">Evidence</th>
                </tr>
              </thead>
              {groups.map((group, index) => {
                const open = isOpen(group, index);
                const bodyId = `${baseId}-group-${index}`;
                return (
                  <tbody key={group.key} id={bodyId} data-slot="chat-findings-group" data-area={group.key} data-open={open ? 'true' : 'false'}>
                    <tr>
                      <th scope="rowgroup" colSpan={columns} className="border-t bg-muted/30 p-0 text-left font-normal">
                        <GroupHeader group={group} open={open} onToggle={() => toggleGroup(group)} controls={bodyId} />
                      </th>
                    </tr>
                    {open && group.findings.map((finding) => {
                      const gone = leavingIds.has(finding.id);
                      const expanded = openRows.has(finding.id);
                      const detailId = `${baseId}-detail-${finding.id}`;
                      return [
                        <tr
                          key={finding.id}
                          data-slot="chat-finding-row"
                          data-id={finding.id}
                          data-status={finding.status}
                          data-leaving={gone ? 'true' : undefined}
                          data-open={expanded ? 'true' : undefined}
                          onClick={gone ? undefined : () => toggleRow(finding.id)}
                          className={cn(
                            'h-[3.25rem] cursor-pointer border-t align-middle transition-colors hover:bg-muted/40 motion-reduce:transition-none',
                            expanded && 'bg-muted/30',
                            gone && 'chat-leave pointer-events-none',
                          )}
                        >
                          <td className="px-3 py-1.5 whitespace-nowrap text-muted-foreground tabular-nums">{pageWords(finding)}</td>
                          <th scope="row" className="px-2 py-1.5 text-left font-normal">
                            <span className="flex min-w-0 items-center gap-1.5">
                              <button
                                type="button"
                                aria-expanded={expanded}
                                aria-controls={detailId}
                                onClick={(event) => { event.stopPropagation(); toggleRow(finding.id); }}
                                title={finding.item}
                                className="min-w-0 truncate rounded-sm text-left font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                              >
                                {finding.item}
                              </button>
                              <SourceTag finding={finding} inline />
                              <RoundChip finding={finding} />
                            </span>
                          </th>
                          <td className="px-2 py-1.5"><RowValues finding={finding} /></td>
                          <td className="px-2 py-1.5"><FindingDifference finding={finding} /></td>
                          <td className="px-2 py-1.5"><RowStatus finding={finding} /></td>
                          <td className="px-2 py-1.5"><span className="grid justify-items-start gap-1"><TrustChip finding={finding} /><CheckIcons finding={finding} /></span></td>
                          <td className="px-3 py-1.5"><Thumbs finding={finding} pictureUrl={pictureUrl} onOpen={() => onOpen(finding)} /></td>
                        </tr>,
                        expanded && !gone ? (
                          <tr key={`${finding.id}-detail`} id={detailId} data-slot="chat-finding-detail" data-id={finding.id} className="bg-muted/20">
                            <td colSpan={columns} className="px-3 pt-2 pb-4">
                              <FindingDetail finding={finding} pictureUrl={pictureUrl} onOpen={() => onOpen(finding)} />
                            </td>
                          </tr>
                        ) : null,
                      ];
                    })}
                  </tbody>
                );
              })}
            </table>
          </div>
          <div className="border-t px-3 py-2"><CheckLegend /></div>
        </div>
      )}
    </section>
  );
}
