import { useEffect, useRef, useState } from 'react';
import { CircleAlert, CircleCheck, CircleX, Download, ExternalLink, FileText, ListX, PencilLine, RefreshCw, RotateCcw, ScanSearch } from 'lucide-react';

import { AccuracyBadge } from '@/components/chat/chat-accuracy';
import { GVSpark } from '@/components/chat/chat-motion';
import { PlanCheck } from '@/components/chat/chat-plan-check';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { chatPaths, type ChatAccuracy, type ChatCoverage, type ChatFile, type ChatFullCheckPlan, type ChatPlanAnswer, type ChatProduct, type ChatReviewAction } from '@/lib/chat-api';
import { coverageCounts, coverageWords, type FindingCounts } from '@/lib/chat-findings';
import { allPicked, progressWords } from '@/lib/chat-full-check';
import { planAnswers, usePlanCheck } from '@/lib/chat-plan-check';
import { fileKind, fileSize, openHref, type ViewMessage } from '@/lib/chat-messages';
import { activeProducts, comingLater, useProductCatalog } from '@/lib/products';
import type { ProofCounts } from '@/lib/chat-proof';
import { cn } from '@/lib/utils';

/**
 * Chat v4 (CONTRACT4-full-check, #1483): the one-click full check's parts. The Countertop / Cabinet
 * chips (in the composer), the "Run full check" button with its small confirm popover, and the
 * large cloud-drawing card a full-check answer leads with. Read-only: nothing changes the review;
 * the cloud drawing is made by code with no clicks (#1581): only Wrong values code proved are
 * clouded, so it is ready to download at once; the reviewer's decisions are optional overrides.
 */

/**
 * The product chips (#1547: the registry's active products, from `GET /products`): multi-select, at
 * least one stays picked. The pick is a strict scope (Anant, 2026-10-18): only the products picked are
 * checked, ruled, counted and drawn. A quiet "All" chip first picks every active product (pressed when
 * all are); with one active product it is left out. The products not ready yet are one greyed chip,
 * "N coming later", naming them.
 */
export function ProductChips({ products, onToggle, onAll, disabled }: { products: readonly ChatProduct[] | null; onToggle: (product: ChatProduct) => void; onAll?: () => void; disabled?: boolean }) {
  const catalog = useProductCatalog();
  const later = comingLater(catalog);
  const active = activeProducts(catalog);
  const every = allPicked(products, catalog);
  const chip = 'inline-flex h-7 shrink-0 items-center rounded-full border px-2.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50 motion-reduce:transition-none';
  return (
    <div role="group" aria-label="Products to check" data-slot="chat-products" className="flex min-w-0 items-center gap-1">
      {onAll && active.length > 1 && (
        <button
          type="button"
          data-slot="chat-products-all"
          aria-pressed={every}
          disabled={disabled}
          onClick={onAll}
          title={`Check every product: ${active.map((product) => product.plural).join(', ')}`}
          className={cn(
            chip,
            every ? 'border-foreground/25 bg-accent text-foreground' : 'border-transparent bg-transparent text-muted-foreground hover:bg-accent hover:text-foreground',
          )}
        >
          All
        </button>
      )}
      {active.map(({ id: product, label, description }) => {
        const on = Boolean(products?.includes(product));
        return (
          <button
            key={product}
            type="button"
            aria-pressed={on}
            disabled={disabled}
            onClick={() => onToggle(product)}
            title={on ? `${description} Checked in this chat.` : `${description} Not checked: press to add it.`}
            className={cn(chip, on ? 'border-foreground bg-foreground text-background' : 'bg-background text-muted-foreground hover:bg-accent hover:text-foreground')}
          >
            {label}
          </button>
        );
      })}
      {later.length > 0 && (
        <span
          data-slot="chat-products-later"
          aria-disabled="true"
          title={`Coming later: ${later.map((product) => product.plural).join(', ')}`}
          className="hidden h-7 shrink-0 cursor-default items-center rounded-full border border-dashed px-2.5 text-xs text-muted-foreground opacity-60 sm:inline-flex"
        >
          {later.length} coming later
        </span>
      )}
    </div>
  );
}

export interface FullCheckButtonProps {
  /** "Full check · Countertops · Opus 5.5 · High · up to $15". */
  summary: string;
  /** Runs it, with the reviewer's answers to the plan check (#1569), if any. */
  onRun: (answers?: ChatPlanAnswer[]) => void;
  /** Why it cannot run now, in plain words; null when it can. */
  blocked: string | null;
  /** `primary`: the empty chat's main action. `bar`: the smaller one in the top bar. */
  variant?: 'primary' | 'bar';
  /** #1569: code's plan and its 0-3 questions, read when the popover opens; absent: none asked. */
  loadPlan?: () => Promise<ChatFullCheckPlan>;
  /** The product scope in one sentence: "Only countertops are checked; cabinets are left out." */
  scope?: string | null;
}

/** "Run full check", confirmed in a small popover that names the products, model, effort and cap. */
export function FullCheckButton({ summary, onRun, blocked, variant = 'primary', loadPlan, scope }: FullCheckButtonProps) {
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<Record<string, string>>({});
  const plan = usePlanCheck(loadPlan, open);
  const doubts = plan.status === 'ready' ? plan.plan.doubts : [];
  return (
    <Popover open={open} onOpenChange={(next) => { setOpen(next); if (next) setPicked({}); }}>
      <PopoverTrigger
        disabled={Boolean(blocked)}
        title={blocked ?? 'Check the whole drawing set: Wrong, Right and Not compared, with the cloud drawing'}
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
      <PopoverContent align={variant === 'primary' ? 'center' : 'end'} className="w-[min(24rem,calc(100vw-24px))] gap-3 rounded-xl p-3 font-sans">
        <p data-slot="chat-full-check-summary" className="text-sm font-medium tabular-nums">{summary}</p>
        {scope && <p data-slot="chat-full-check-scope" className="text-xs font-medium text-foreground">{scope}</p>}
        <p className="text-xs text-muted-foreground">
          Checks every page against the architect and GV’s rules, then code draws the cloud drawing and the check report.
        </p>
        <PlanCheck state={plan} picked={picked} onPick={(id, value) => setPicked((now) => ({ ...now, [id]: value }))} />
        <div className="flex justify-end gap-2">
          <button type="button" onClick={() => setOpen(false)} className="h-8 rounded-lg border bg-background px-3 text-sm hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => {
              const answers = planAnswers(doubts, picked);
              setOpen(false);
              if (answers.length) onRun(answers);
              else onRun();
            }}
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
export function CloudPreview({ href, page, total }: { href: string; page: number; total: number | null }) {
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
        .then(({ renderPdfPage }) => renderPdfPage(href, page, 900, { byLabel: true }))
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
      <span className="absolute bottom-2 left-2 rounded-[5px] bg-black/70 px-1.5 py-0.5 text-xs font-medium text-white tabular-nums">
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
  /** Items the reviewer has overridden, of those it could (Wrong + Not compared). */
  decided: number;
  total: number;
  /** Who answered, once every item is answered: "Reviewed by <name>". */
  reviewedBy: string | null;
  /** Make the drawing again from the answers; null when that is not offered. */
  onUpdate: (() => void) | null;
  updating: boolean;
  failed: boolean;
}

export function FullCheckFiles({ cloud, report, notCompared = [], chatId, page = 1, totalPages = null, proof = null, review = null, packaged = false, unsent = false }: {
  cloud: ChatFile | null;
  report: ChatFile | null;
  /** #1619: the Not-compared list, as a PDF and a workbook. */
  notCompared?: readonly ChatFile[];
  chatId: string;
  /** The page the preview shows (the biggest clouded item's, the vendor's own page number). */
  page?: number;
  totalPages?: number | null;
  /** #1581, #1619: the drawing's own groups (clouded / right / not compared), as its cover counts them. */
  proof?: ProofCounts | null;
  /** #1525: the reviewer's answers so far, and "Update cloud drawing" (optional overrides since #1581). */
  review?: CloudReview | null;
  /** #1622: a send-ready package holds the drawing too; its download is then the one primary action. */
  packaged?: boolean;
  /** #1684: the review result is "not for sending" (the check did not finish): never "Ready to send". */
  unsent?: boolean;
}) {
  if (!cloud && !report && !notCompared.length) return null;
  const href = (file: ChatFile) => file.url || chatPaths.file(chatId, file.id);
  const reportLine = report && (
    <div data-slot="chat-check-report" className="flex min-w-0 items-center gap-3 border-t pt-3 text-[13px]">
      <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate">
        <span className="font-medium">Check report</span>
        <span className="text-muted-foreground" title={report.name}> · {fileSize(report.size)}</span>
      </span>
      <a href={openHref(report, href(report))} target="_blank" rel="noopener noreferrer" aria-label={`Open ${report.name}`} className="rounded-md px-1.5 py-1 font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">Open</a>
      <a href={href(report)} download={report.name} aria-label={`Download ${report.name}`} className="rounded-md px-1.5 py-1 font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">Download</a>
    </div>
  );
  const listLine = notCompared.length > 0 && (
    <div data-slot="chat-not-compared-files" className="flex min-w-0 items-center gap-3 border-t pt-3 text-[13px]">
      <ListX className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate">
        <span className="font-medium">Not-compared list</span>
        <span className="text-muted-foreground"> · why each item could not be compared, and what is needed</span>
      </span>
      {notCompared.map((file) => (
        <a key={file.id} href={href(file)} download={file.name} aria-label={`Download ${file.name}`} className="rounded-md px-1.5 py-1 font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
          {/\.pdf$/i.test(file.name) ? 'PDF' : /\.csv$/i.test(file.name) ? 'CSV' : 'Excel'}
        </a>
      ))}
    </div>
  );
  if (!cloud) return <div className="grid gap-3 rounded-xl border bg-card px-4 py-3">{reportLine}{listLine}</div>;
  return (
    <section data-slot="chat-cloud-drawing" aria-label="Cloud drawing" className="chat-rise grid overflow-hidden rounded-xl border border-border/70 bg-card md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <a href={openHref(cloud, href(cloud))} target="_blank" rel="noopener noreferrer" aria-label="Open the cloud drawing" className="grid border-b border-border/60 bg-muted/40 p-4 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset md:border-r md:border-b-0">
        <CloudPreview href={href(cloud)} page={page} total={totalPages} />
      </a>
      <div className="grid content-center gap-4 p-6">
        <div className="grid gap-1.5">
          <p className="text-xs font-medium text-muted-foreground">Main output</p>
          <p className="text-[17px] font-semibold tracking-[-0.01em]">Cloud drawing</p>
          <p className="text-[13.5px] text-muted-foreground">
            {unsent ? 'Kept for you to look at. The check did not finish, so do not send it yet. It has a red cloud' : 'Ready to send: a red cloud'} at each wrong value code proved, with what it should be in green and the proof for each. The architect’s drawings govern; what could not be compared is listed apart.
          </p>
          {proof ? (
            <p data-slot="chat-cloud-counts" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground tabular-nums">
              <span className="inline-flex items-center gap-1.5"><span aria-hidden="true" className="size-2 rounded-full bg-outcome-fail" /><span className="font-medium text-foreground">{proof.clouded}</span> clouded, proved by code</span>
              {proof.not_compared > 0 && <span className="inline-flex items-center gap-1.5"><span aria-hidden="true" className="size-2 rounded-full ring-[1.5px] ring-inset ring-muted-foreground/60" />{proof.not_compared} not compared</span>}
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground"><span className="break-all">{cloud.name}</span> · {[fileKind(cloud), fileSize(cloud.size)].filter(Boolean).join(' · ')}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <a
            href={href(cloud)}
            download={cloud.name}
            aria-label={`Download ${cloud.name}`}
            data-slot="chat-cloud-download"
            className={cn(
              'inline-flex h-9 items-center gap-1.5 rounded-lg px-3.5 text-sm font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none',
              packaged ? 'border bg-background hover:bg-accent' : 'bg-primary text-primary-foreground hover:opacity-90',
            )}
          >
            <Download className="size-4" aria-hidden="true" />
            Download
          </a>
          <a href={openHref(cloud, href(cloud))} target="_blank" rel="noopener noreferrer" aria-label="Open cloud drawing" className="inline-flex h-9 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <ExternalLink className="size-4" aria-hidden="true" />
            Open
          </a>
          {review?.onUpdate && review.decided > 0 && (
            <button
              type="button"
              data-slot="chat-cloud-update"
              onClick={review.onUpdate}
              disabled={review.updating}
              aria-busy={review.updating}
              className="inline-flex h-9 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:cursor-progress disabled:opacity-70"
            >
              <RefreshCw className={cn('size-4', review.updating && 'animate-spin motion-reduce:animate-none')} aria-hidden="true" />
              {review.updating ? 'Updating the cloud drawing…' : 'Update cloud drawing'}
            </button>
          )}
        </div>
        {review && review.total > 0 && (
          <p data-slot="chat-cloud-review" className="text-xs text-muted-foreground tabular-nums">
            {review.decided} of {review.total} overridden{review.reviewedBy ? ` · reviewed by ${review.reviewedBy}` : ''} · optional: Agree, Not a problem or Make it Wrong below changes the drawing
          </p>
        )}
        {review?.failed && <p role="alert" className="text-sm text-muted-foreground">The cloud drawing could not be made again right now. Try again.</p>}
        {reportLine}
        {listLine}
      </div>
    </section>
  );
}

// ── The send-ready package (#1622, right after the cloud drawing) ─

const ACTION_LOOK: Record<ChatReviewAction['action'], { Icon: typeof RotateCcw; tone: string }> = {
  revise_resubmit: { Icon: RotateCcw, tone: 'text-outcome-fail-fg' },
  rejected: { Icon: CircleX, tone: 'text-outcome-fail-fg' },
  approved_as_noted: { Icon: PencilLine, tone: 'text-foreground' },
  approved: { Icon: CircleCheck, tone: 'text-outcome-pass-fg' },
  not_for_sending: { Icon: CircleAlert, tone: 'text-muted-foreground' },
};

/**
 * One download for the vendor: the GV cloud drawing, the comment letter (the same comment numbers as
 * the clouds, C-01…), the issue register, the Not-compared list (when made) and the check report,
 * all made by code from the final list. The review result leads, decided by code (`review_action`,
 * AWI's words); the letter and the cloud drawing's cover say the same. A check that did not finish
 * says so plainly: the package is kept, but it is not for sending.
 */
export function PackageCard({ pkg, chatId, action, hasReport = true, hasNotCompared = false }: {
  pkg: ChatFile;
  chatId: string;
  action: ChatReviewAction | null;
  hasReport?: boolean;
  hasNotCompared?: boolean;
}) {
  const href = pkg.url || chatPaths.file(chatId, pkg.id);
  const look = action ? ACTION_LOOK[action.action] : null;
  const corrections = action ? `${action.corrections} ${action.corrections === 1 ? 'comment' : 'comments'}, numbered like the clouds` : 'numbered like the clouds';
  const unsent = action?.action === 'not_for_sending';
  const inside = [
    { name: 'GV cloud drawing', note: 'PDF' },
    { name: 'Comment letter', note: corrections },
    { name: 'Issue register', note: 'XLSX · one row per item' },
    ...(hasNotCompared ? [{ name: 'Not-compared list', note: 'PDF and XLSX' }] : []),
    ...(hasReport ? [{ name: 'Check report', note: 'PDF' }] : []),
  ];
  return (
    <section data-slot="chat-package" aria-label="Send-ready package" className="chat-rise grid gap-5 rounded-xl border border-border/70 bg-card p-5 sm:p-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="grid min-w-0 gap-1">
          <p className="text-xs font-medium text-muted-foreground">Send to the vendor</p>
          <p className="text-[17px] font-semibold tracking-[-0.01em]">Send-ready package</p>
          <p className="text-[13.5px] text-muted-foreground">{unsent ? 'Kept for you to look at. The check did not finish, so do not send it yet.' : 'Everything the vendor needs in one file, made by code from the final list. Nothing to edit.'}</p>
        </div>
        <a
          href={href}
          download={pkg.name}
          aria-label={`Download package: ${pkg.name}`}
          data-slot="chat-package-download"
          className={cn(
            'inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-lg px-3.5 text-sm font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none',
            unsent ? 'border bg-background hover:bg-accent' : 'bg-primary text-primary-foreground hover:opacity-90',
          )}
        >
          <Download className="size-4" aria-hidden="true" />
          Download package (.zip)
        </a>
      </div>
      {action && look && (
        <div data-slot="chat-review-action" data-action={action.action} role={unsent ? 'alert' : undefined} className="grid gap-1 rounded-lg bg-muted/50 px-4 py-3">
          <p className="text-xs font-medium text-muted-foreground">Review result</p>
          <p className={cn('inline-flex items-center gap-2 text-[15px] font-semibold', look.tone)}>
            <look.Icon className="size-4 shrink-0" aria-hidden="true" />
            {action.words}
          </p>
          <p className="text-[13px] text-muted-foreground">{[action.reason, action.note].filter(Boolean).join(' ')}</p>
          <p className="text-[13px]"><span className="text-muted-foreground">Action required by: </span>{action.required_by}</p>
        </div>
      )}
      <div className="grid gap-2">
        <p className="text-xs font-medium text-muted-foreground">In the package</p>
        <ul data-slot="chat-package-contents" className="grid gap-x-6 gap-y-1.5 text-[13px] sm:grid-cols-2">
          {inside.map((part) => (
            <li key={part.name} className="flex min-w-0 items-baseline gap-2">
              <FileText className="size-3.5 shrink-0 translate-y-0.5 text-muted-foreground" aria-hidden="true" />
              <span className="min-w-0"><span className="font-medium">{part.name}</span><span className="text-muted-foreground"> · {part.note}</span></span>
            </li>
          ))}
        </ul>
        <p className="text-xs text-muted-foreground"><span className="break-all">{pkg.name}</span> · {fileSize(pkg.size)}</p>
      </div>
    </section>
  );
}

// ── The three numbers (PATH step 4.2) ───────────────────────

const NUMBERS = [
  { status: 'different', words: 'Wrong', hint: 'Proved by code, clouded red', dot: 'bg-outcome-fail' },
  { status: 'same', words: 'Right', hint: 'Matches the architect', dot: 'bg-outcome-pass' },
  { status: 'check', words: 'Not compared', hint: 'Each with its reason and what is needed', dot: 'bg-transparent ring-[1.5px] ring-inset ring-muted-foreground/60' },
] as const;

export interface ResultNumbersProps {
  counts: FindingCounts;
  coverage: ChatCoverage | null;
  accuracy: ChatAccuracy | null | undefined;
  /** Jump to that section of the findings. */
  onPick: (status: 'different' | 'check' | 'same') => void;
}

/**
 * Wrong · Right · Nothing to compare (#1619: as the cloud drawing counts them), plus pages checked
 * and the Accuracy % (each shown once on the screen). A number jumps to its section below.
 */
export function ResultNumbers({ counts, coverage, accuracy, onPick }: ResultNumbersProps) {
  const pages = coverage ? coverageCounts(coverage) : null;
  return (
    <section data-slot="chat-kpis" aria-label="The result in numbers" className="chat-rise grid grid-cols-3 gap-2 md:grid-cols-[repeat(3,minmax(0,1fr))_minmax(0,1.1fr)]">
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
            className="grid min-w-0 gap-1 rounded-xl bg-muted/50 p-3 text-left transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:cursor-default disabled:hover:bg-muted/50 motion-reduce:transition-none sm:p-4"
          >
            {/* The label wraps instead of cutting "Nothing to compare" short on a phone; two lines are kept
                for every card there, so the three numbers stay on one level. */}
            <span data-slot="chat-kpi-label" className="flex min-h-[2.5em] min-w-0 items-start gap-1.5 text-[13px] leading-[1.25] font-medium text-muted-foreground sm:min-h-0 sm:items-center">
              <span aria-hidden="true" className={cn('mt-[0.3em] size-2 shrink-0 rounded-full sm:mt-0', number.dot)} />
              <span className="min-w-0 break-words">{number.words}</span>
            </span>
            <span className={cn('text-2xl leading-8 font-semibold tracking-[-0.03em] tabular-nums sm:text-[30px] sm:leading-9', count === 0 && 'text-muted-foreground')}>{count}</span>
            <span className="hidden text-xs text-muted-foreground sm:block">{number.hint}</span>
          </button>
        );
      })}
      <div data-slot="chat-facts" className="col-span-3 grid content-center gap-2 rounded-xl bg-muted/50 p-4 md:col-span-1">
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
    <p data-slot="chat-progress" role="status" className="flex min-h-12 min-w-0 items-center gap-3 rounded-xl bg-muted/50 px-4 text-sm font-medium tabular-nums">
      <GVSpark state={message.progress?.phase === 'writing' ? 'streaming' : 'working'} size={16} />
      <span className="min-w-0">{progressWords(message, now)}</span>
    </p>
  );
}
