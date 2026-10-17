import { useEffect, useId, useState } from 'react';
import { Check, ChevronRight, ExternalLink, ImageOff, Maximize2, X } from 'lucide-react';

import { RoundChip } from '@/components/chat/chat-trust';
import { useMediaQuery } from '@/hooks/use-media-query';
import type { ChatFinding, FindingCategory, FindingDecisionValue, FindingStatus } from '@/lib/chat-api';
import {
  architectPage,
  biggestFirst,
  CATEGORY_WORDS,
  categoriesPresent,
  decisionHelpRows,
  findingLine,
  FINDINGS_NARROW,
  groupByArea,
  itemTitle,
  pagesWords,
  pageWords,
  reviewerWords,
  sourceTag,
  spotInsets,
  STATUS_WORDS,
  type AreaGroup,
  type FindingFilter,
  vendorPage,
} from '@/lib/chat-findings';
import { useReducedMotion } from '@/lib/chat-motion';
import { plainChecks, sumWords, TRUST_WORDS, trustLevelOf } from '@/lib/chat-trust';
import { cn } from '@/lib/utils';

/**
 * The findings of an answer, redesigned in #1522 after the owner's "very bad: needs professional,
 * very cool and classy" and "make it so everyone can understand, from the reviewer's perspective".
 * The reviewer's path (PATH.md step 4): What's wrong, then Your decision, then Right (folded), each
 * grouped by area under a sticky header. A row is one plain line ("Vendor drew 46 in · needs 37 in
 * (architect 3' - 1")"), a quiet confidence meter, a readable A / V picture pair and Agree / Not a
 * problem. A click opens the item's card in place: what each side shows, why, what to tell the
 * vendor, how sure, and the two pictures large with the spot boxed.
 * One vocabulary: Wrong · Your decision · Right · Not compared. Read-only, except the reviewer's own
 * Agree / Not a problem, shown only when the server offers the route.
 */

const DROP_MS = 220;

type RowStatus = FindingStatus | 'not_compared';

const statusOf = (finding: ChatFinding): RowStatus => (finding.status === 'same' && finding.compared === false ? 'not_compared' : finding.status);

const DOT: Record<RowStatus, string> = {
  different: 'bg-outcome-fail',
  check: 'bg-outcome-review',
  same: 'bg-outcome-pass',
  not_compared: 'bg-transparent ring-[1.5px] ring-inset ring-outcome-missing',
};

/** A small coloured dot: colour is never the only signal, the word beside it always says it. */
export function StatusDot({ status, className }: { status: RowStatus; className?: string }) {
  return <span aria-hidden="true" className={cn('inline-block size-2 shrink-0 rounded-full', DOT[status], className)} />;
}

const STATUS_TEXT: Record<RowStatus, string> = {
  different: 'text-outcome-fail-fg font-medium',
  check: 'text-foreground font-medium',
  same: 'text-muted-foreground',
  not_compared: 'text-muted-foreground',
};

/** Wrong / Your decision / Right / Not compared, as a dot and a word (no filled chip). */
export function StatusMark({ finding, className }: { finding: ChatFinding; className?: string }) {
  const status = statusOf(finding);
  return (
    <span data-slot="chat-finding-status" data-status={status} className={cn('inline-flex items-center gap-1.5 text-[13px] whitespace-nowrap', STATUS_TEXT[status], className)}>
      <StatusDot status={status} />
      {status === 'not_compared' ? 'Not compared' : STATUS_WORDS[finding.status]}
    </span>
  );
}

const LEVEL_STEPS = { confirmed: 3, likely: 2, to_check: 1 } as const;

/**
 * How sure, quietly: a 3-step meter (Confirmed 3 · Likely 2 · AI reading only 1) and its word.
 * Grey, never amber: confidence is not a status.
 */
export function Confidence({ finding, className }: { finding: ChatFinding; className?: string }) {
  const level = trustLevelOf(finding);
  if (!level) return null;
  const steps = LEVEL_STEPS[level];
  return (
    <span data-slot="chat-confidence" data-level={level} className={cn('inline-flex items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground', className)}>
      <span aria-hidden="true" className="inline-flex items-end gap-[2px]">
        {[5, 8, 11].map((height, at) => (
          <span key={height} className={cn('block w-[3px] rounded-[1px]', at < steps ? 'bg-muted-foreground' : 'bg-border')} style={{ height }} />
        ))}
      </span>
      <span><span className="sr-only">Confidence: </span>{TRUST_WORDS[level]}</span>
    </span>
  );
}

/** The row's one line: "Vendor drew 46 in · needs 37 in (architect 3' - 1") +9 in". */
export function FindingLineText({ finding }: { finding: ChatFinding }) {
  const line = findingLine(finding);
  const value = (part: { text: string; inches: boolean }) => <span className="font-semibold text-foreground tabular-nums">{part.text}</span>;
  return (
    <span data-slot="chat-finding-line" className="line-clamp-2 text-[13px] leading-[1.45] text-muted-foreground">
      {line.verb === 'both' ? <>Both {value(line.vendor)}</> : (
        <>
          Vendor drew {value(line.vendor)}
          <span aria-hidden="true"> · </span>
          <span className="sr-only">, </span>
          {line.verb} {line.reference && value(line.reference)}
          {line.printed && <> ({line.printed})</>}
        </>
      )}
      {line.difference && (
        <span
          data-slot="chat-finding-difference"
          className={cn(
            'ml-1.5 inline-flex h-[18px] items-center rounded-[5px] px-1.5 align-[1px] text-xs font-semibold tabular-nums',
            finding.status === 'different' ? 'bg-outcome-fail-bg text-outcome-fail-fg' : 'bg-muted text-muted-foreground',
          )}
        >
          {line.difference}
        </span>
      )}
    </span>
  );
}

// ── Pictures ────────────────────────────────────────────────

/** An evidence picture, or a calm placeholder when there is none (or it fails to load). */
export function EvidenceThumb({ src, label, className }: { src: string | null; label: string; className?: string }) {
  const [broken, setBroken] = useState<string | null>(null);
  if (!src || broken === src) {
    return (
      <span data-slot="chat-evidence-thumb" data-missing="true" className={cn('grid place-items-center rounded-lg border border-dashed bg-muted/40 px-2 text-center text-xs text-muted-foreground', className)}>
        <ImageOff aria-hidden="true" className="mb-0.5 size-3.5" />
        <span>{label}: no evidence picture</span>
      </span>
    );
  }
  return <img data-slot="chat-evidence-thumb" src={src} alt={`${label} evidence`} loading="lazy" onError={() => setBroken(src)} className={cn('object-contain', className)} />;
}

/** One side of a row's pair: 96 px tall, labelled A or V, larger on hover or focus; a click opens the viewer. */
function PairSide({ src, side, label, onOpen }: { src: string | null; side: 'A' | 'V'; label: string; onOpen: () => void }) {
  const reduced = useReducedMotion();
  if (!src) return <EvidenceThumb src={null} label={label} className="h-24" />;
  return (
    <button
      type="button"
      data-slot="chat-evidence-side"
      data-side={side}
      onClick={(event) => { event.stopPropagation(); onOpen(); }}
      aria-label={`Open the pictures large: ${label}`}
      className="group/pic relative block h-24 min-w-0 rounded-lg border border-border/70 bg-white focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
    >
      <EvidenceThumb src={src} label={label} className="size-full rounded-[7px] p-1" />
      <span aria-hidden="true" className="absolute top-1 left-1 rounded-[4px] bg-black/70 px-1 py-0.5 text-xs leading-none font-semibold text-white">{side}</span>
      {/* Hover / focus zoom: a larger copy beside the pair, never covering the row's words. */}
      <span
        aria-hidden="true"
        className={cn(
          'pointer-events-none absolute top-1/2 z-30 hidden w-[26rem] -translate-y-1/2 rounded-xl bg-white p-1.5 opacity-0 shadow-[0_16px_40px_rgb(0_0_0/0.18),0_0_0_1px_rgb(0_0_0/0.06)] lg:block',
          side === 'A' ? 'right-[calc(200%+1.125rem)]' : 'right-[calc(100%+0.625rem)]',
          'group-hover/pic:opacity-100 group-focus-visible/pic:opacity-100',
          !reduced && 'scale-[0.97] transition-[opacity,transform] duration-200 ease-out group-hover/pic:scale-100 group-focus-visible/pic:scale-100',
        )}
      >
        <img src={src} alt="" className="max-h-80 w-full object-contain" />
      </span>
    </button>
  );
}

function EvidencePair({ finding, pictureUrl, onOpen }: { finding: ChatFinding; pictureUrl: (pictureId: string) => string; onOpen: () => void }) {
  return (
    <span data-slot="chat-finding-thumbs" className="grid w-full max-w-80 grid-cols-2 gap-2 lg:w-64">
      <PairSide side="A" label="Architect" src={finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null} onOpen={onOpen} />
      <PairSide side="V" label="Vendor" src={finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null} onOpen={onOpen} />
    </span>
  );
}

/** A large picture in the card, with the spot boxed when code knows where it is; a click zooms (viewer). */
function LargePicture({ src, label, page, box, tone, onOpen }: {
  src: string | null;
  label: string;
  page: number;
  box: ChatFinding['arch_box'];
  tone: RowStatus;
  onOpen: () => void;
}) {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const insets = size ? spotInsets(box, size) : null;
  return (
    <figure className="grid min-w-0 gap-1.5">
      <figcaption className="flex items-baseline gap-1.5 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{label}</span>
        <span className="tabular-nums">page {page}</span>
      </figcaption>
      {src ? (
        <button
          type="button"
          onClick={onOpen}
          aria-label={`Zoom: ${label}, page ${page}`}
          className="relative block h-60 cursor-zoom-in overflow-hidden rounded-lg border bg-white focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <img
            src={src}
            alt={`${label} drawing, page ${page}`}
            onLoad={(event) => setSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
            className="size-full object-contain"
          />
          {insets && (
            <span
              data-slot="chat-finding-spot"
              aria-hidden="true"
              style={insets}
              className={cn('pointer-events-none absolute rounded-[3px] border-2', tone === 'different' ? 'border-outcome-fail shadow-[0_0_0_3px_rgb(220_38_38/0.15)]' : 'border-outcome-review shadow-[0_0_0_3px_rgb(217_119_6/0.15)]')}
            />
          )}
        </button>
      ) : <EvidenceThumb src={null} label={label} className="h-60 rounded-lg" />}
    </figure>
  );
}

// ── The reviewer's answer per item (PATH step 5) ────────────

export interface DecisionProps {
  /** The reviewer's answer on this finding (saved, or just given). */
  decision: FindingDecisionValue | null;
  onDecide: (decision: FindingDecisionValue) => void;
  busy?: boolean;
}

/** ✓ Agree / ✗ Not a problem: one click each, real buttons (keyboard: Tab, Enter or Space). */
function DecisionButtons({ decision, onDecide, busy }: DecisionProps) {
  const base = 'inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-[12.5px] font-medium whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-60 motion-reduce:transition-none';
  return (
    <span role="group" aria-label="Your answer" data-slot="chat-finding-decision" data-decision={decision ?? undefined} className="flex flex-wrap gap-1.5" onClick={(event) => event.stopPropagation()}>
      <button
        type="button"
        aria-pressed={decision === 'agree'}
        disabled={busy}
        onClick={() => onDecide('agree')}
        className={cn(base, decision === 'agree' ? 'border-foreground bg-foreground text-background' : 'bg-background text-muted-foreground hover:border-foreground/25 hover:text-foreground')}
      >
        <Check aria-hidden="true" className="size-3.5" />
        Agree
      </button>
      <button
        type="button"
        aria-pressed={decision === 'not_a_problem'}
        disabled={busy}
        onClick={() => onDecide('not_a_problem')}
        className={cn(base, decision === 'not_a_problem' ? 'border-foreground bg-foreground text-background' : 'bg-background text-muted-foreground hover:border-foreground/25 hover:text-foreground')}
      >
        <X aria-hidden="true" className="size-3.5" />
        Not a problem
      </button>
    </span>
  );
}

// ── The item's card (owner: "from the reviewer's perspective") ─

/** Everything about one finding, in the reviewer's words, with the two pictures large. */
export function FindingCard({ finding, pictureUrl, onOpen, fullPageHref }: {
  finding: ChatFinding;
  pictureUrl: (pictureId: string) => string;
  onOpen: () => void;
  fullPageHref?: string | null;
}) {
  const words = reviewerWords(finding);
  const status = statusOf(finding);
  const checks = plainChecks(finding);
  const tag = sourceTag(finding);
  const sum = sumWords(finding);
  const help = decisionHelpRows(finding);
  return (
    <div data-slot="chat-finding-card" className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.3fr)]">
      <div className="grid min-w-0 content-start gap-3.5">
        <p className="text-[15px] leading-snug font-semibold break-words">
          {finding.item}
          <span className="font-normal text-muted-foreground tabular-nums"> · page {pageWords(finding)}</span>
        </p>
        <dl className="grid gap-3 text-[13.5px] leading-relaxed">
          <div><dt className="text-xs font-medium text-muted-foreground">{words.referenceLabel}</dt><dd className="break-words">{words.reference}</dd></div>
          <div><dt className="text-xs font-medium text-muted-foreground">{words.vendorLabel}</dt><dd className="break-words">{words.vendor}</dd></div>
          {sum && <div><dt className="text-xs font-medium text-muted-foreground">Code’s sum</dt><dd data-slot="chat-finding-sum" className="break-words tabular-nums">{sum}</dd></div>}
          <div><dt className="text-xs font-medium text-muted-foreground">{words.whyLabel}</dt><dd data-slot="chat-finding-why" className="break-words">{words.why}</dd></div>
          {help.length > 0 && (
            <div data-slot="chat-finding-decision-help" className="grid gap-2 border-l-2 border-border pl-3">
              {help.map((row) => (
                <div key={row.label}><dt className="text-xs font-medium text-muted-foreground">{row.label}</dt><dd className="break-words">{row.text}</dd></div>
              ))}
            </div>
          )}
          {words.tell && <div><dt className="text-xs font-medium text-muted-foreground">What to tell the vendor</dt><dd data-slot="chat-finding-tell" className="font-medium break-words">{words.tell}</dd></div>}
        </dl>
        <p data-slot="chat-finding-sure" className="flex items-baseline gap-2 text-[12.5px] leading-relaxed text-muted-foreground">
          <span><span className="font-medium text-foreground">How sure: {trustLevelOf(finding) ? TRUST_WORDS[trustLevelOf(finding)!] : 'not rated yet'}.</span> {words.sure}</span>
        </p>
        {(checks.length > 0 || tag) && (
          <details data-slot="chat-finding-how" className="group/how text-[12.5px] text-muted-foreground">
            <summary className="inline-flex cursor-pointer list-none items-center gap-1 rounded-sm hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none [&::-webkit-details-marker]:hidden">
              <ChevronRight aria-hidden="true" className="size-3.5 transition-transform duration-150 group-open/how:rotate-90 motion-reduce:transition-none" />
              How we know
            </summary>
            <ul className="mt-1.5 grid gap-1 pl-[18px]">
              {tag && <li>Compared {tag.replace(/^vs /, 'with the ')}.</li>}
              {checks.map((line) => <li key={line}>{line}</li>)}
            </ul>
          </details>
        )}
      </div>
      <div className="grid min-w-0 content-start gap-3">
        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <LargePicture src={finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null} label={finding.source === 'cross_view' ? 'Reference view' : 'Architect'} page={architectPage(finding)} box={finding.arch_box} tone={status} onOpen={onOpen} />
          <LargePicture src={finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null} label={finding.source === 'cross_view' ? 'This view' : 'Vendor'} page={vendorPage(finding)} box={finding.vendor_box} tone={status} onOpen={onOpen} />
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" onClick={onOpen} className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-[13px] font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <Maximize2 aria-hidden="true" className="size-3.5" />
            Open viewer
          </button>
          {fullPageHref && (
            <a href={fullPageHref} className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-[13px] font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
              <ExternalLink aria-hidden="true" className="size-3.5" />
              Open full page
            </a>
          )}
        </div>
      </div>
    </div>
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

// ── One row ─────────────────────────────────────────────────

interface RowProps {
  finding: ChatFinding;
  area: string | null;
  open: boolean;
  leaving: boolean;
  onToggle: () => void;
  onOpen: () => void;
  pictureUrl: (pictureId: string) => string;
  fullPageHref?: string | null;
  decision?: DecisionProps | null;
}

function FindingRow({ finding, area, open, leaving, onToggle, onOpen, pictureUrl, fullPageHref, decision }: RowProps) {
  const detailId = useId();
  const tag = sourceTag(finding);
  const title = itemTitle(finding.item, area);
  return (
    <li
      data-slot="chat-finding-row"
      data-id={finding.id}
      data-status={statusOf(finding)}
      data-open={open ? 'true' : undefined}
      data-leaving={leaving ? 'true' : undefined}
      className={cn('border-b border-border/60 last:border-b-0', leaving && 'chat-leave pointer-events-none')}
    >
      <div
        onClick={leaving ? undefined : onToggle}
        className={cn(
          'grid cursor-pointer gap-3 px-4 py-4 transition-colors hover:bg-muted/40 motion-reduce:transition-none lg:grid-cols-[2.5rem_minmax(0,1fr)_16rem_auto] lg:items-center lg:gap-4',
          open && 'bg-muted/40',
        )}
      >
        <span className="hidden text-[12.5px] font-medium text-muted-foreground tabular-nums lg:block">p.{finding.page}</span>
        <span className="grid min-w-0 gap-1">
          <span className="flex min-w-0 items-baseline gap-2">
            <button
              type="button"
              aria-expanded={open}
              aria-controls={detailId}
              onClick={(event) => { event.stopPropagation(); onToggle(); }}
              title={finding.item}
              className="min-w-0 truncate rounded-sm text-left text-[13.5px] font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              <span className="sr-only">{finding.item}</span>
              <span aria-hidden="true">{title}</span>
            </button>
            <span className="shrink-0 text-xs text-muted-foreground tabular-nums lg:hidden">p.{pageWords(finding)}</span>
          </span>
          <FindingLineText finding={finding} />
          {finding.status === 'check' && finding.why?.trim() && (
            <span data-slot="chat-finding-question" className="line-clamp-2 text-[13px] leading-[1.45]">
              <span className="font-medium text-outcome-review-fg">Your call: </span>
              {finding.why}
            </span>
          )}
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {statusOf(finding) === 'not_compared' && <StatusMark finding={finding} />}
            <Confidence finding={finding} />
            <RoundChip finding={finding} />
            {finding.updated_in && (
              <span data-slot="chat-finding-updated" className="text-xs text-muted-foreground">
                Updated in a later message
              </span>
            )}
            {tag && <span data-slot="chat-finding-source" className="text-xs text-muted-foreground">{tag.replace(/^vs /, 'Compared with the ')}</span>}
            {finding.vs_page != null && finding.vs_page !== finding.page && <span className="hidden text-xs text-muted-foreground tabular-nums lg:inline">vs page {finding.vs_page}</span>}
          </span>
        </span>
        <EvidencePair finding={finding} pictureUrl={pictureUrl} onOpen={onOpen} />
        <span className="flex lg:justify-end">{decision && <DecisionButtons {...decision} />}</span>
      </div>
      {open && !leaving && (
        <div id={detailId} data-slot="chat-finding-detail" data-id={finding.id} className="border-t border-border/60 bg-muted/30 px-4 py-6 lg:pl-[4.5rem]">
          <FindingCard finding={finding} pictureUrl={pictureUrl} onOpen={onOpen} fullPageHref={fullPageHref} />
        </div>
      )}
    </li>
  );
}

// ── A list of findings, grouped by area ─────────────────────

export interface FindingsListProps {
  findings: readonly ChatFinding[];
  /** Which groups start open: all of them, or the first few. */
  openGroups?: 'all' | number;
  /** Rows whose card starts open (a narrow answer's few items). */
  openRowsAtFirst?: boolean;
  onOpen: (finding: ChatFinding) => void;
  pictureUrl: (pictureId: string) => string;
  fullPageHref?: string | null;
  /** The reviewer's answer on each finding; null hides the buttons (no route yet, or a Right list). */
  decisionFor?: ((finding: ChatFinding) => DecisionProps) | null;
  /** What the list is, for screen readers ("What’s wrong"). */
  label: string;
}

export function FindingsList({ findings, openGroups = 'all', openRowsAtFirst = false, onOpen, pictureUrl, fullPageHref, decisionFor, label }: FindingsListProps) {
  const baseId = useId();
  const leaving = useLeaving(findings);
  const leavingIds = new Set(leaving.map((finding) => finding.id));
  const groups = groupByArea([...findings, ...leaving], { status: 'any', category: null, page: null });
  const [closed, setClosed] = useState<ReadonlySet<string>>(() => new Set());
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set());
  const [rows, setRows] = useState<ReadonlySet<string>>(() => (openRowsAtFirst ? new Set(findings.map((finding) => finding.id)) : new Set()));
  const isOpen = (group: AreaGroup, index: number) => !closed.has(group.key) && (opened.has(group.key) || openGroups === 'all' || index < openGroups);
  const toggleGroup = (group: AreaGroup, index: number) => {
    const open = isOpen(group, index);
    setClosed((now) => { const next = new Set(now); if (open) next.add(group.key); else next.delete(group.key); return next; });
    setOpened((now) => { const next = new Set(now); if (open) next.delete(group.key); else next.add(group.key); return next; });
  };
  const toggleRow = (id: string) => setRows((now) => { const next = new Set(now); if (next.has(id)) next.delete(id); else next.add(id); return next; });

  return (
    <div data-slot="chat-findings-list" aria-label={label} role="region" className="rounded-xl border border-border/70 bg-card">
      {groups.map((group, index) => {
        const open = isOpen(group, index);
        const listId = `${baseId}-group-${index}`;
        const pages = group.byPage ? null : pagesWords(group.pages).replace(/^p/, 'p.');
        return (
          <section key={group.key} data-slot="chat-findings-group" data-area={group.key} data-open={open ? 'true' : 'false'} aria-label={group.name} className="border-t first:border-t-0">
            <button
              type="button"
              aria-expanded={open}
              aria-controls={listId}
              onClick={() => toggleGroup(group, index)}
              data-slot="chat-findings-group-toggle"
              className={cn(
                'sticky top-0 z-20 flex h-10 w-full min-w-0 items-center gap-2 border-b border-border/60 bg-muted/90 px-4 text-left text-[13px] backdrop-blur-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset supports-[backdrop-filter]:bg-muted/75',
                index === 0 && 'rounded-t-xl',
                !open && index === groups.length - 1 && 'rounded-b-xl border-b-0',
              )}
            >
              <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 text-muted-foreground transition-transform duration-150 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
              <span className="min-w-0 truncate font-semibold text-foreground" title={group.name}>{group.name}</span>
              {pages && <span className="shrink-0 text-muted-foreground tabular-nums">{pages}</span>}
              <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">{group.findings.length}</span>
            </button>
            {open && (
              <ul id={listId} className="grid">
                {group.findings.map((finding) => (
                  <FindingRow
                    key={finding.id}
                    finding={finding}
                    area={group.byPage ? null : group.name}
                    open={rows.has(finding.id)}
                    leaving={leavingIds.has(finding.id)}
                    onToggle={() => toggleRow(finding.id)}
                    onOpen={() => onOpen(finding)}
                    pictureUrl={pictureUrl}
                    fullPageHref={fullPageHref}
                    decision={decisionFor && finding.status !== 'same' ? decisionFor(finding) : null}
                  />
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}

/**
 * The biggest Wrong items first (#1569): code's impact order (checked before AI reading only, then
 * what changes fabrication and the size of the difference), under a quiet "Biggest first" note.
 * The same rows as below; the list by area then holds the rest.
 */
function BiggestFirst({ findings, onOpen, pictureUrl, fullPageHref, decisionFor }: Omit<FindingsListProps, 'label' | 'openGroups' | 'openRowsAtFirst'>) {
  const [rows, setRows] = useState<ReadonlySet<string>>(() => new Set());
  const toggleRow = (id: string) => setRows((now) => { const next = new Set(now); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  return (
    <div data-slot="chat-findings-biggest" role="region" aria-label="Biggest first" className="rounded-xl border border-border/70 bg-card">
      <p className="flex min-h-10 flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-border/60 px-4 py-2 text-[13px] text-muted-foreground">
        <span className="font-semibold whitespace-nowrap text-foreground">Biggest first</span>
        <span>what changes fabrication most, the most certain first</span>
      </p>
      <ul className="grid">
        {findings.map((finding) => (
          <FindingRow
            key={finding.id}
            finding={finding}
            area={null}
            open={rows.has(finding.id)}
            leaving={false}
            onToggle={() => toggleRow(finding.id)}
            onOpen={() => onOpen(finding)}
            pictureUrl={pictureUrl}
            fullPageHref={fullPageHref}
            decision={decisionFor ? decisionFor(finding) : null}
          />
        ))}
      </ul>
    </div>
  );
}

// ── The sections (PATH step 4.4 to 4.6) ─────────────────────

export interface FindingsSectionsProps {
  findings: readonly ChatFinding[];
  filter: FindingFilter;
  onFilter: (filter: FindingFilter) => void;
  onOpen: (finding: ChatFinding) => void;
  pictureUrl: (pictureId: string) => string;
  fullPageHref?: string | null;
  decisionFor?: ((finding: ChatFinding) => DecisionProps) | null;
  /** A narrow answer (a few pages): its few cards start open. */
  narrow?: boolean;
  /** Anchors for the summary's numbers. */
  ids?: Partial<Record<'different' | 'check' | 'same', string>>;
}

const SECTIONS = [
  { status: 'different', words: 'What’s wrong', hint: null },
  { status: 'check', words: 'Your decision', hint: 'The AI could not settle these on its own.' },
] as const;

function SectionHead({ status, words, count, hint, children, id }: { status: RowStatus; words: string; count: number; hint?: string | null; children?: React.ReactNode; id?: string }) {
  return (
    <div className="mb-3 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
      <h3 id={id} className="flex items-center gap-2 text-base font-semibold tracking-[-0.01em]">
        <StatusDot status={status} />
        {words}
        <span className="font-medium text-muted-foreground tabular-nums">{count}</span>
      </h3>
      {hint && <span className="text-[13px] text-muted-foreground">{hint}</span>}
      {children}
    </div>
  );
}

function Folded({ status, words, hint, count, children, id }: { status: RowStatus; words: string; hint: string; count: number; children: React.ReactNode; id?: string }) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  return (
    <section data-slot={`chat-findings-${status}`} aria-labelledby={id} className="min-w-0">
      <button
        type="button"
        id={id}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
        className="flex w-full min-w-0 flex-wrap items-center gap-x-2 gap-y-1 rounded-md py-1 text-left focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <ChevronRight aria-hidden="true" className={cn('size-4 shrink-0 text-muted-foreground transition-transform duration-150 motion-reduce:transition-none', open && 'rotate-90')} />
        <StatusDot status={status} />
        <span className="text-base font-semibold">{words}</span>
        <span className="font-medium text-muted-foreground tabular-nums">{count}</span>
        <span className="text-[13px] text-muted-foreground">{hint}</span>
      </button>
      {open && <div id={bodyId} className="mt-3">{children}</div>}
    </section>
  );
}

/** What's wrong · Your decision · Right (folded) · Not compared (folded), each grouped by area. */
export function FindingsSections({ findings, filter, onFilter, onOpen, pictureUrl, fullPageHref, decisionFor, narrow = false, ids }: FindingsSectionsProps) {
  const isNarrowScreen = useMediaQuery(FINDINGS_NARROW);
  const categories = categoriesPresent(findings);
  const shown = findings.filter((finding) => (filter.category === null || (finding.category ?? 'other') === filter.category) && (filter.page === null || finding.page === filter.page || finding.vs_page === filter.page));
  const of = (status: RowStatus) => shown.filter((finding) => statusOf(finding) === status);
  const setCategory = (category: FindingCategory | null) => onFilter({ ...filter, category });
  const firstHead = SECTIONS.find((section) => of(section.status).length > 0)?.status ?? null;
  const filters = (categories.length > 1 || filter.page !== null) && !isNarrowScreen ? (
    <div className="ml-auto flex flex-wrap items-center gap-2">
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
      {categories.length > 1 && (
        <div role="group" aria-label="Filter findings" className="inline-flex rounded-lg border bg-muted/50 p-0.5">
          {[null, ...categories].map((category) => (
            <button
              key={category ?? 'all'}
              type="button"
              aria-pressed={filter.category === category}
              onClick={() => setCategory(category)}
              className={cn(
                'h-[26px] rounded-md px-2.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none',
                filter.category === category ? 'bg-card text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.08),0_0_0_1px_var(--border)]' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {category ? CATEGORY_WORDS[category] : 'All'}
            </button>
          ))}
        </div>
      )}
    </div>
  ) : null;

  const right = of('same');
  const unseen = of('not_compared');
  const nothing = SECTIONS.every((section) => of(section.status).length === 0);

  return (
    <section data-slot="chat-findings" aria-label="Findings" className="chat-rise grid min-w-0 gap-7">
      {nothing && (
        <div className="grid gap-3">
          {filters && <div className="flex">{filters}</div>}
          <p data-slot="chat-findings-empty" className="rounded-xl border border-dashed px-4 py-5 text-center text-sm text-muted-foreground">
            {findings.length === 0 ? 'No findings yet.' : shown.length === 0 ? 'No findings match this filter.' : 'Nothing wrong and nothing to decide.'}
          </p>
        </div>
      )}
      {SECTIONS.map((section) => {
        const list = of(section.status);
        if (!list.length) return null;
        // #1569: the biggest Wrong items first (only when there are more than a handful).
        const biggest = section.status === 'different' && list.length > 3 ? biggestFirst(list) : [];
        const top = new Set(biggest.map((finding) => finding.id));
        const rest = list.filter((finding) => !top.has(finding.id));
        return (
          <section key={section.status} data-slot={`chat-findings-${section.status}`} aria-labelledby={ids?.[section.status]} className="min-w-0 scroll-mt-4">
            <SectionHead status={section.status} words={section.words} count={list.length} hint={section.hint} id={ids?.[section.status]}>
              {section.status === firstHead && filters}
            </SectionHead>
            {biggest.length > 0 && (
              <div className={cn(rest.length > 0 && 'mb-3')}>
                <BiggestFirst findings={biggest} onOpen={onOpen} pictureUrl={pictureUrl} fullPageHref={fullPageHref} decisionFor={decisionFor} />
              </div>
            )}
            {rest.length > 0 && <FindingsList
              findings={rest}
              label={section.words}
              openGroups={section.status === 'different' ? 'all' : 3}
              openRowsAtFirst={narrow && of('different').length + of('check').length <= 3}
              onOpen={onOpen}
              pictureUrl={pictureUrl}
              fullPageHref={fullPageHref}
              decisionFor={decisionFor}
            />}
          </section>
        );
      })}
      {right.length > 0 && (
        <Folded status="same" words="Right" count={right.length} hint="Matches the architect, the prototype or GV’s rules" id={ids?.same}>
          <FindingsList findings={right} label="Right" onOpen={onOpen} pictureUrl={pictureUrl} fullPageHref={fullPageHref} />
        </Folded>
      )}
      {unseen.length > 0 && (
        <Folded status="not_compared" words="Not compared" count={unseen.length} hint="Not looked at on both sides, so not counted as right">
          <FindingsList findings={unseen} label="Not compared" onOpen={onOpen} pictureUrl={pictureUrl} fullPageHref={fullPageHref} />
        </Folded>
      )}
    </section>
  );
}
