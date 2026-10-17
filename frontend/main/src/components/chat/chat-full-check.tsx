import { useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, FileText, RefreshCw, ScanSearch } from 'lucide-react';

import { AccuracyBadge } from '@/components/chat/chat-accuracy';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { chatPaths, type ChatAccuracy, type ChatCoverage, type ChatFile, type ChatProduct } from '@/lib/chat-api';
import { coverageCounts, coverageWords, type FindingCounts } from '@/lib/chat-findings';
import { PRODUCT_WORDS, PRODUCTS, progressWords } from '@/lib/chat-full-check';
import { fileKind, fileSize, type ViewMessage } from '@/lib/chat-messages';
import { cn } from '@/lib/utils';

/**
 * Chat v4 (CONTRACT4-full-check, #1483): the one-click full check's parts. The Countertop / Cabinet
 * chips (in the composer), the "Run full check" button with its small confirm popover, and the
 * large cloud-drawing card a full-check answer leads with. Read-only: nothing changes the review;
 * the cloud drawing is an AI markup draft made by code, not GV's signed review.
 */

/** Countertop / Cabinet: multi-select, at least one stays picked. None shown picked = the set's own product. */
export function ProductChips({ products, onToggle, disabled }: { products: readonly ChatProduct[] | null; onToggle: (product: ChatProduct) => void; disabled?: boolean }) {
  return (
    <div role="group" aria-label="Products to check" data-slot="chat-products" className="flex min-w-0 items-center gap-1">
      {PRODUCTS.map((product) => {
        const on = Boolean(products?.includes(product));
        return (
          <button
            key={product}
            type="button"
            aria-pressed={on}
            disabled={disabled}
            onClick={() => onToggle(product)}
            title={products ? undefined : 'Not picked yet: the drawing set’s own product is used'}
            className={cn(
              'inline-flex h-7 items-center rounded-full border px-2.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50 motion-reduce:transition-none',
              on ? 'border-foreground bg-foreground text-background' : 'bg-background text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            {PRODUCT_WORDS[product]}
          </button>
        );
      })}
    </div>
  );
}

export interface FullCheckButtonProps {
  /** "Full check · Countertops · Opus 5.5 · High · up to $15". */
  summary: string;
  onRun: () => void;
  /** Why it cannot run now, in plain words; null when it can. */
  blocked: string | null;
  /** `primary`: the empty chat's main action. `bar`: the smaller one in the top bar. */
  variant?: 'primary' | 'bar';
}

/** "Run full check", confirmed in a small popover that names the products, model, effort and cap. */
export function FullCheckButton({ summary, onRun, blocked, variant = 'primary' }: FullCheckButtonProps) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        disabled={Boolean(blocked)}
        title={blocked ?? 'Check the whole drawing set: what is wrong, what to check, what is right'}
        data-slot="chat-full-check"
        data-variant={variant}
        className={cn(
          'inline-flex shrink-0 items-center gap-1.5 font-medium transition-[opacity,background-color] focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-default disabled:opacity-50 motion-reduce:transition-none',
          variant === 'primary'
            ? 'h-10 rounded-xl bg-primary px-4 text-sm text-primary-foreground shadow-sm hover:opacity-90'
            : 'h-8 rounded-lg border bg-background px-2.5 text-sm hover:bg-accent',
        )}
      >
        <ScanSearch className="size-4" aria-hidden="true" />
        Run full check
      </PopoverTrigger>
      <PopoverContent align={variant === 'primary' ? 'center' : 'end'} className="w-[min(22rem,calc(100vw-24px))] gap-3 rounded-xl p-3 font-sans">
        <p data-slot="chat-full-check-summary" className="text-sm font-medium tabular-nums">{summary}</p>
        <p className="text-xs text-muted-foreground">
          Checks every page against the architect and GV’s rules, then code draws the cloud drawing and the check report.
        </p>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={() => setOpen(false)} className="h-8 rounded-lg border bg-background px-3 text-sm hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => { setOpen(false); onRun(); }}
            className="h-8 rounded-lg bg-primary px-3 text-sm font-medium text-primary-foreground hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
          >
            Run
          </button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/**
 * The cloud drawing's page preview: the page with the first wrong item, drawn from the PDF once the
 * card is on screen (pdf.js loads only then). Until it is drawn, or when it cannot be, a calm icon.
 */
function CloudPreview({ href, page, total }: { href: string; page: number; total: number | null }) {
  const [state, setState] = useState<{ src: string } | 'icon'>('icon');
  const frame = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const node = frame.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    let cancelled = false;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      import('@/api/pdf')
        .then(({ renderPdfPage }) => renderPdfPage(href, page, 900))
        .then((src) => { if (!cancelled) setState({ src }); })
        .catch(() => { /* The plain icon stays: a preview is a nicety, the file is the output. */ });
    }, { rootMargin: '200px' });
    observer.observe(node);
    return () => { cancelled = true; observer.disconnect(); };
  }, [href, page]);
  return (
    <span ref={frame} data-slot="chat-cloud-preview" data-state={state === 'icon' ? 'icon' : 'page'} className="relative grid h-56 w-full place-items-center overflow-hidden rounded-lg bg-white shadow-[0_1px_3px_rgb(0_0_0/0.12),0_0_0_1px_rgb(0_0_0/0.05)] sm:h-72">
      {state === 'icon' ? (
        <svg aria-hidden="true" viewBox="0 0 48 32" className="h-10 w-16 text-outcome-fail/70" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
          <path d="M10 26c-4 0-6-3-5-6-3-2-2-7 2-7 0-4 5-6 8-3 2-4 8-4 10 0 3-3 9-1 9 3 4 0 6 4 4 7 2 3 0 6-4 6z" />
        </svg>
      ) : (
        <img src={state.src} alt={`Cloud drawing, page ${page}`} className="size-full object-cover object-[50%_88%]" />
      )}
      <span className="absolute bottom-2 left-2 rounded-[5px] bg-black/70 px-1.5 py-0.5 text-[11px] font-medium text-white tabular-nums">
        Page {page}{total ? ` of ${total}` : ''}
      </span>
    </span>
  );
}

/**
 * A full check's main output (PATH step 4.3): the cloud drawing as one big card, its page preview
 * on the left, Open and Download on the right; the check report as a quiet line inside it.
 */
export interface CloudReview {
  /** Items the reviewer has answered, of those needing an answer (Wrong + Your decision). */
  decided: number;
  total: number;
  /** Who answered, once every item is answered: "Reviewed by <name>". */
  reviewedBy: string | null;
  /** Make the drawing again from the answers; null when that is not offered. */
  onUpdate: (() => void) | null;
  updating: boolean;
  failed: boolean;
}

export function FullCheckFiles({ cloud, report, chatId, page = 1, totalPages = null, counts = null, review = null }: {
  cloud: ChatFile | null;
  report: ChatFile | null;
  chatId: string;
  /** The page the preview shows (the first wrong item's). */
  page?: number;
  totalPages?: number | null;
  counts?: Pick<FindingCounts, 'different' | 'check'> | null;
  /** #1525: the reviewer's answers so far, and "Update cloud drawing". */
  review?: CloudReview | null;
}) {
  if (!cloud && !report) return null;
  const href = (file: ChatFile) => file.url || chatPaths.file(chatId, file.id);
  const reportLine = report && (
    <div data-slot="chat-check-report" className="flex min-w-0 items-center gap-3 border-t pt-3 text-[13px]">
      <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate">
        <span className="font-medium">Check report</span>
        <span className="text-muted-foreground" title={report.name}> · {fileSize(report.size)}</span>
      </span>
      <a href={href(report)} target="_blank" rel="noopener noreferrer" aria-label={`Open ${report.name}`} className="rounded-md px-1.5 py-1 font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">Open</a>
      <a href={href(report)} download={report.name} aria-label={`Download ${report.name}`} className="rounded-md px-1.5 py-1 font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">Download</a>
    </div>
  );
  if (!cloud) return <div className="rounded-xl border bg-card px-4 py-3">{reportLine}</div>;
  return (
    <section data-slot="chat-cloud-drawing" aria-label="Cloud drawing" className="chat-rise grid overflow-hidden rounded-2xl border bg-card shadow-[0_1px_2px_rgb(0_0_0/0.04)] md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <a href={href(cloud)} target="_blank" rel="noopener noreferrer" aria-label="Open the cloud drawing" className="grid border-b bg-muted/40 p-4 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset md:border-r md:border-b-0">
        <CloudPreview href={href(cloud)} page={page} total={totalPages} />
      </a>
      <div className="grid content-center gap-3 p-5 md:p-6">
        <div className="grid gap-1.5">
          <p className="text-xs font-medium text-muted-foreground">Main output</p>
          <p className="text-[17px] font-semibold tracking-[-0.01em]">Cloud drawing</p>
          <p className="text-[13.5px] text-muted-foreground">
            The vendor’s drawing with a cloud at every item: what is wrong and what it should be. Draft AI markup, not GV’s signed review.
          </p>
          {counts && (counts.different > 0 || counts.check > 0) && (
            <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground tabular-nums">
              {counts.different > 0 && <span className="inline-flex items-center gap-1.5"><span aria-hidden="true" className="size-2 rounded-full bg-outcome-fail" />{counts.different} wrong</span>}
              {counts.check > 0 && <span className="inline-flex items-center gap-1.5"><span aria-hidden="true" className="size-2 rounded-full bg-outcome-review" />{counts.check} to decide</span>}
            </p>
          )}
          <p className="text-xs text-muted-foreground"><span className="break-all">{cloud.name}</span> · {[fileKind(cloud), fileSize(cloud.size)].filter(Boolean).join(' · ')}</p>
        </div>
        {review && review.total > 0 && (
          <div data-slot="chat-cloud-review" className="grid gap-1.5">
            <p className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
              <span className="font-medium tabular-nums">{review.decided} of {review.total} decided</span>
              {review.reviewedBy && <span className="text-muted-foreground"><span aria-hidden="true">· </span>Reviewed by {review.reviewedBy}</span>}
            </p>
            <span aria-hidden="true" className="flex h-1 w-full max-w-xs overflow-hidden rounded-full bg-muted">
              <span className="h-full rounded-full bg-foreground/70 transition-[width] duration-300 motion-reduce:transition-none" style={{ width: `${(review.decided / review.total) * 100}%` }} />
            </span>
            {review.decided === 0 && <p className="text-xs text-muted-foreground">Agree or dismiss each item below; the drawing then clouds only what you agreed with.</p>}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          {review?.onUpdate && review.decided > 0 && (
            <button
              type="button"
              data-slot="chat-cloud-update"
              onClick={review.onUpdate}
              disabled={review.updating}
              aria-busy={review.updating}
              className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary px-3.5 text-sm font-medium text-primary-foreground hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-progress disabled:opacity-70"
            >
              <RefreshCw className={cn('size-4', review.updating && 'animate-spin motion-reduce:animate-none')} aria-hidden="true" />
              {review.updating ? 'Updating the cloud drawing…' : 'Update cloud drawing'}
            </button>
          )}
          <a
            href={href(cloud)}
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              'inline-flex h-9 items-center gap-1.5 rounded-lg px-3.5 text-sm font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none',
              review?.onUpdate && review.decided > 0 ? 'border bg-background hover:bg-accent' : 'bg-primary text-primary-foreground hover:opacity-90',
            )}
          >
            <ExternalLink className="size-4" aria-hidden="true" />
            Open cloud drawing
          </a>
          <a href={href(cloud)} download={cloud.name} aria-label={`Download ${cloud.name}`} className="inline-flex h-9 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <Download className="size-4" aria-hidden="true" />
            Download
          </a>
        </div>
        {review?.failed && <p role="alert" className="text-sm text-muted-foreground">The cloud drawing could not be made again right now. Try again.</p>}
        {reportLine}
      </div>
    </section>
  );
}

// ── The three numbers (PATH step 4.2) ───────────────────────

const NUMBERS = [
  { status: 'different', words: 'Wrong', hint: 'Send back to the vendor', dot: 'bg-outcome-fail' },
  { status: 'check', words: 'Your decision', hint: 'Agree or dismiss each', dot: 'bg-outcome-review' },
  { status: 'same', words: 'Right', hint: 'Matches the drawings', dot: 'bg-outcome-pass' },
] as const;

export interface ResultNumbersProps {
  counts: FindingCounts;
  coverage: ChatCoverage | null;
  accuracy: ChatAccuracy | null | undefined;
  /** Jump to that section of the findings. */
  onPick: (status: 'different' | 'check' | 'same') => void;
}

/**
 * Wrong · Your decision · Right, plus pages checked and the Accuracy % (each shown once on the
 * screen). A number jumps to its section below.
 */
export function ResultNumbers({ counts, coverage, accuracy, onPick }: ResultNumbersProps) {
  const pages = coverage ? coverageCounts(coverage) : null;
  return (
    <section data-slot="chat-kpis" aria-label="The result in numbers" className="chat-rise grid grid-cols-3 gap-2.5 md:grid-cols-[repeat(3,minmax(0,1fr))_minmax(0,1.1fr)]">
      {NUMBERS.map((number) => {
        const count = counts[number.status];
        return (
          <button
            key={number.status}
            type="button"
            data-kpi={number.status}
            disabled={count === 0}
            onClick={() => onPick(number.status)}
            aria-label={`${number.words}: ${count}. Go to them`}
            className="grid min-w-0 gap-1 rounded-xl border bg-card px-3 py-3 text-left shadow-[0_1px_2px_rgb(0_0_0/0.04)] transition-colors hover:border-foreground/25 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:cursor-default disabled:hover:border-border motion-reduce:transition-none sm:px-4 sm:py-3.5"
          >
            <span className="flex min-w-0 items-center gap-1.5 text-[13px] font-medium text-muted-foreground">
              <span aria-hidden="true" className={cn('size-2 shrink-0 rounded-full', number.dot)} />
              <span className="truncate">{number.words}</span>
            </span>
            <span className={cn('text-2xl leading-8 font-semibold tracking-[-0.03em] tabular-nums sm:text-[30px] sm:leading-9', count === 0 && 'text-muted-foreground')}>{count}</span>
            <span className="hidden text-xs text-muted-foreground sm:block">{number.hint}</span>
          </button>
        );
      })}
      <div data-slot="chat-facts" className="col-span-3 grid content-center gap-2.5 rounded-xl border bg-muted/30 px-4 py-3 md:col-span-1">
        <div data-kpi="pages" className="flex items-center justify-between gap-2 text-[13px] text-muted-foreground" title={pages ? coverageWords(pages) : undefined}>
          <span>Pages checked</span>
          {pages ? (
            <span className="text-[15px] font-semibold text-foreground tabular-nums">{pages.total - pages.not_opened}/{pages.total}</span>
          ) : <span>—</span>}
          {pages && <span className="sr-only">{coverageWords(pages)}</span>}
        </div>
        <div data-kpi="accuracy" className="flex items-center justify-between gap-2 text-[13px] text-muted-foreground">
          <span>Accuracy</span>
          {accuracy ? <AccuracyBadge accuracy={accuracy} bare /> : <span>—</span>}
        </div>
      </div>
    </section>
  );
}

/** PATH step 3: one calm line while a full check runs ("Checking page 12 of 60 · 8 wrong so far · about 6 min left"). */
export function FullCheckProgress({ message }: { message: Pick<ViewMessage, 'created_at' | 'findings' | 'coverage' | 'progress'> }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <p data-slot="chat-progress" role="status" className="flex min-w-0 items-center gap-2.5 rounded-xl border bg-card px-4 py-3 text-sm font-medium tabular-nums shadow-[0_1px_2px_rgb(0_0_0/0.04)]">
      <span aria-hidden="true" className="relative flex size-2 shrink-0">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-foreground/40 motion-reduce:animate-none" />
        <span className="relative inline-flex size-2 rounded-full bg-foreground" />
      </span>
      <span className="min-w-0">{progressWords(message, now)}</span>
    </p>
  );
}
