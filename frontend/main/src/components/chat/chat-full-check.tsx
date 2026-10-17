import { useState } from 'react';
import { Download, ExternalLink, FileText, ScanSearch } from 'lucide-react';

import { AccuracyBadge } from '@/components/chat/chat-accuracy';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { chatPaths, type ChatAccuracy, type ChatCoverage, type ChatFile, type ChatProduct } from '@/lib/chat-api';
import { coverageCounts, coverageWords, type FindingCounts, type StatusFilter } from '@/lib/chat-findings';
import { PRODUCT_WORDS, PRODUCTS } from '@/lib/chat-full-check';
import { fileKind, fileSize } from '@/lib/chat-messages';
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

/** A full check's main output, first and large: the cloud drawing (Open / Download), and the check report beside it. */
export function FullCheckFiles({ cloud, report, chatId }: { cloud: ChatFile | null; report: ChatFile | null; chatId: string }) {
  if (!cloud && !report) return null;
  const href = (file: ChatFile) => file.url || chatPaths.file(chatId, file.id);
  return (
    <div data-slot="chat-full-check-files" className="grid gap-2">
      {cloud && (
        <section data-slot="chat-cloud-drawing" aria-label="Cloud drawing" className="chat-rise grid gap-3 rounded-2xl border bg-card p-4 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center">
          <span aria-hidden="true" className="grid size-16 place-items-center rounded-xl border bg-outcome-fail-bg text-outcome-fail-fg">
            <svg viewBox="0 0 48 32" className="h-8 w-12" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinejoin="round">
              <path d="M10 26c-4 0-6-3-5-6-3-2-2-7 2-7 0-4 5-6 8-3 2-4 8-4 10 0 3-3 9-1 9 3 4 0 6 4 4 7 2 3 0 6-4 6z" />
            </svg>
          </span>
          <div className="grid min-w-0 gap-1.5">
            <p className="text-base font-semibold">Cloud drawing</p>
            <p className="text-sm break-words text-muted-foreground">
              The vendor’s drawing with a red cloud at each wrong value and what it needs to be; amber clouds to check. AI markup: draft, not GV’s signed review.
            </p>
            <p className="text-xs text-muted-foreground"><span className="break-all">{cloud.name}</span> · {[fileKind(cloud), fileSize(cloud.size)].filter(Boolean).join(' · ')}</p>
            <div className="flex flex-wrap gap-2 pt-0.5">
              <a href={href(cloud)} target="_blank" rel="noopener noreferrer" className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary px-3.5 text-sm font-medium text-primary-foreground hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none">
                <ExternalLink className="size-4" aria-hidden="true" />
                Open cloud drawing
              </a>
              <a href={href(cloud)} download={cloud.name} aria-label={`Download ${cloud.name}`} className="inline-flex h-9 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
                <Download className="size-4" aria-hidden="true" />
                Download
              </a>
            </div>
          </div>
        </section>
      )}
      {report && (
        <div data-slot="chat-check-report" className="flex min-w-0 items-center gap-3 rounded-xl border bg-card px-3 py-2.5">
          <span aria-hidden="true" className="grid size-10 shrink-0 place-items-center rounded-lg border bg-muted text-muted-foreground">
            <FileText className="size-5" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-medium">Check report</span>
            <span className="block truncate text-xs text-muted-foreground" title={report.name}>{report.name} · {fileSize(report.size)}</span>
          </span>
          <a href={href(report)} target="_blank" rel="noopener noreferrer" aria-label={`Open ${report.name}`} className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-2.5 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <ExternalLink className="size-4" aria-hidden="true" />
            <span className="hidden min-[420px]:inline">Open</span>
          </a>
          <a href={href(report)} download={report.name} aria-label={`Download ${report.name}`} className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-2.5 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <Download className="size-4" aria-hidden="true" />
            <span className="hidden min-[420px]:inline">Download</span>
          </a>
        </div>
      )}
    </div>
  );
}

// ── The summary strip (full check) ──────────────────────────

const KPI_TONE = {
  different: 'text-outcome-fail-fg',
  check: 'text-outcome-review-fg',
  same: 'text-outcome-pass-fg',
  not_compared: 'text-muted-foreground',
} as const;

const KPI_OUTCOME = { different: 'FAIL', check: 'REVIEW_REQUIRED', same: 'PASS', not_compared: 'NOT_FOUND' } as const;

const KPIS = [
  { status: 'different', words: 'Wrong' },
  { status: 'check', words: 'To check' },
  { status: 'same', words: 'Right' },
  { status: 'not_compared', words: 'Not compared' },
] as const;

const TILE = 'grid min-w-0 content-between gap-0.5 rounded-xl border bg-card px-3 py-2 text-left';

export interface FindingsKpisProps {
  counts: FindingCounts;
  coverage: ChatCoverage | null;
  accuracy: ChatAccuracy | null | undefined;
  /** The status the findings show now (its tile is marked). */
  status: StatusFilter;
  onPick: (status: StatusFilter) => void;
}

/**
 * A full check's summary, first (#1508): Wrong · To check · Right · Not compared · Pages checked ·
 * Accuracy. A count tile picks that tab of the findings below.
 */
export function FindingsKpis({ counts, coverage, accuracy, status, onPick }: FindingsKpisProps) {
  const pages = coverage ? coverageCounts(coverage) : null;
  return (
    <section data-slot="chat-kpis" aria-label="Summary" className="chat-rise grid grid-cols-3 gap-2 sm:grid-cols-6">
      {KPIS.map((kpi) => {
        const count = counts[kpi.status];
        return (
          <button
            key={kpi.status}
            type="button"
            data-kpi={kpi.status}
            aria-pressed={status === kpi.status}
            disabled={count === 0}
            onClick={() => onPick(kpi.status)}
            aria-label={`${kpi.words}: ${count}. Show them`}
            className={cn(
              TILE,
              'transition-colors hover:border-foreground/25 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:cursor-default disabled:hover:border-border motion-reduce:transition-none',
              status === kpi.status && 'border-foreground/40 bg-muted/60',
            )}
          >
            <span className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
              <OutcomeIcon outcome={KPI_OUTCOME[kpi.status]} size={12} className={KPI_TONE[kpi.status]} />
              <span className="min-w-0 leading-4">{kpi.words}</span>
            </span>
            <span className={cn('text-xl leading-7 font-semibold tabular-nums', count === 0 ? 'text-muted-foreground' : KPI_TONE[kpi.status])}>{count}</span>
          </button>
        );
      })}
      <div data-kpi="pages" className={TILE} title={pages ? coverageWords(pages) : 'No page coverage was recorded'}>
        <span className="text-xs leading-4 text-muted-foreground"><span className="sm:hidden">Pages</span><span className="hidden sm:inline">Pages checked</span></span>
        <span className="text-xl leading-7 font-semibold tabular-nums">
          {pages ? <>{pages.total - pages.not_opened}<span className="text-sm font-normal text-muted-foreground">/{pages.total}</span></> : <span className="text-muted-foreground">—</span>}
        </span>
        {pages && <span className="sr-only">{coverageWords(pages)}</span>}
      </div>
      <div data-kpi="accuracy" className={TILE}>
        <span className="text-xs leading-4 text-muted-foreground">Accuracy</span>
        <span className="flex h-7 items-center">{accuracy ? <AccuracyBadge accuracy={accuracy} bare /> : <span className="text-xl font-semibold text-muted-foreground">—</span>}</span>
      </div>
    </section>
  );
}
