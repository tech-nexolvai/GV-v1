import { Check, Download, FilePen, FileSpreadsheet, FileText, Loader2, RefreshCw, X } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import type { DownloadState, ReportFormat } from '@/components/output/reportDownload';

export type ExportStatus = 'not_requested' | 'preparing' | 'ready' | 'failed';

const FILES: readonly { format: ReportFormat; title: string; what: string; button: string; Icon: LucideIcon }[] = [
  { format: 'pdf', title: 'Findings PDF', what: 'Every result with its reasons and the sign-off.', button: 'Download PDF', Icon: FileText },
  { format: 'workbook', title: 'Workbook', what: 'The same results, values and decisions as a spreadsheet.', button: 'Download workbook', Icon: FileSpreadsheet },
  { format: 'redline', title: 'Drawing redline', what: 'The drawings marked where each located result was read.', button: 'Download redline', Icon: FilePen },
];

const FORMAT_WORDS: Record<ReportFormat, string> = { pdf: 'findings PDF', workbook: 'workbook', redline: 'drawing redline' };

type StepState = 'done' | 'current' | 'blocked' | 'upcoming';

/** Requested → Preparing → Ready, from the server's signed-export status. */
function exportSteps(status: ExportStatus | null): [StepState, StepState, StepState] {
  switch (status) {
    case 'not_requested': return ['current', 'upcoming', 'upcoming'];
    case 'preparing': return ['done', 'current', 'upcoming'];
    case 'failed': return ['done', 'blocked', 'upcoming'];
    case 'ready': return ['done', 'done', 'done'];
    default: return ['upcoming', 'upcoming', 'upcoming'];
  }
}

const STEP_WORDS: Record<StepState, string> = { done: 'done', current: 'in progress', blocked: 'stopped', upcoming: 'not started' };

function StepMark({ state, index }: { state: StepState; index: number }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'num flex size-6 items-center justify-center rounded-full border text-xs',
        state === 'done' && 'border-foreground bg-foreground text-background',
        state === 'current' && 'border-foreground',
        state === 'blocked' && 'border-destructive text-destructive',
      )}
    >
      {state === 'done' ? <Check className="size-3.5" /> : state === 'blocked' ? <X className="size-3.5" /> : state === 'current' ? <Loader2 className="size-3.5 animate-spin" /> : index + 1}
    </span>
  );
}

/**
 * The Report step (#1064), after sign-off: where the signed files stand (requested → preparing →
 * ready, from `signed-exports`), then one card per file. Downloads are offered only when the server
 * says the files are ready; before that, nothing that looks like a report can be taken away.
 *
 * Presentational: the review page owns the status, its polling and the download handlers.
 */
export function ReportPanel({
  status,
  error,
  prepareError = null,
  requesting,
  download,
  onPrepare,
  onCheck,
  onDownload,
}: {
  /** The server's answer; null while it is being asked (or when asking failed). */
  status: ExportStatus | null;
  /** Why asking for the status failed, if it did. */
  error: string | null;
  /** Why "Prepare signed files" failed, if it did (the button stays, to try again). */
  prepareError?: string | null;
  /** True while "Prepare signed files" is being sent. */
  requesting: boolean;
  download: DownloadState;
  onPrepare: () => void;
  onCheck: () => void;
  onDownload: (format: ReportFormat) => void;
}) {
  const steps = exportSteps(status);
  const downloading = download.status === 'loading';
  return (
    <section
      data-tw
      data-slot="report-panel"
      aria-labelledby="report-panel-title"
      className="mx-4 mt-4 flex flex-col gap-3 rounded-xl border bg-card p-4 font-sans text-card-foreground sm:mx-6"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="report-panel-title" tabIndex={-1} className="text-base font-semibold focus-visible:outline-none">Signed report</h2>
        <ol className="flex flex-wrap items-center gap-2 text-sm" aria-label="Signed files progress">
          {(['Requested', 'Preparing', 'Ready'] as const).map((label, index) => (
            <li key={label} className="flex items-center gap-1.5" data-state={steps[index]} aria-current={steps[index] === 'current' ? 'step' : undefined}>
              {index > 0 && <span className="h-px w-4 bg-border" aria-hidden="true" />}
              <StepMark state={steps[index]} index={index} />
              <span className={cn(steps[index] === 'current' || steps[index] === 'blocked' ? 'font-medium' : 'text-muted-foreground')}>
                {label}<span className="sr-only">, {STEP_WORDS[steps[index]]}</span>
              </span>
            </li>
          ))}
        </ol>
      </div>

      {status !== 'ready' && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {/* Two live regions, each keeping its role: a role changed in place may not be announced. */}
          <div className="min-w-0 flex-1">
            <p role="alert" className="text-destructive">
              {error !== null
                ? `Could not check the signed files: ${error}`
                : status === 'failed'
                  ? 'The signed files could not be prepared, and nothing was published. Asking again here does not retry it: an admin needs to retry the export, then check again.'
                  : prepareError !== null
                    ? `Could not request the signed files: ${prepareError}`
                    : ''}
            </p>
            <p role="status">
              {error !== null || status === 'failed'
                ? ''
                : status === 'not_requested'
                  ? 'The signed files have not been requested for this sign-off yet.'
                  : status === 'preparing'
                    ? 'Preparing the signed files. This page checks again every few seconds.'
                    : 'Checking the signed files…'}
            </p>
          </div>
          {status === 'not_requested' && error === null && (
            <Button type="button" size="sm" onClick={onPrepare} disabled={requesting}>
              {requesting ? <><Loader2 className="animate-spin" aria-hidden="true" /> Requesting…</> : 'Prepare signed files'}
            </Button>
          )}
          <Button type="button" size="sm" variant="outline" onClick={onCheck}>
            <RefreshCw aria-hidden="true" /> Check again
          </Button>
        </div>
      )}

      {status === 'ready' && (
        <ul className="grid gap-3 sm:grid-cols-3" aria-label="Signed files">
          {FILES.map(({ format, title, what, button, Icon }) => {
            const active = download.status === 'loading' && download.format === format;
            return (
              <li key={format} className="flex flex-col gap-2 rounded-lg border p-3" data-format={format}>
                <div className="flex items-center gap-2">
                  <Icon className="size-5 text-muted-foreground" aria-hidden="true" />
                  <span className="font-medium">{title}</span>
                </div>
                <p className="flex-1 text-xs text-muted-foreground">{what}</p>
                <Button type="button" size="sm" variant="outline" disabled={downloading} onClick={() => onDownload(format)}>
                  {active ? <Loader2 className="animate-spin" aria-hidden="true" /> : <Download aria-hidden="true" />}
                  {active ? 'Downloading…' : button}
                </Button>
              </li>
            );
          })}
        </ul>
      )}

      {download.status === 'loading' && <p className="text-sm" role="status">Downloading the {FORMAT_WORDS[download.format]}…</p>}
      {download.status === 'started' && <p className="text-sm" role="status">Download started. Check your browser downloads.</p>}
      {download.status === 'error' && (
        <p className="text-sm text-destructive" role="alert">The {FORMAT_WORDS[download.format]} could not be downloaded: {download.message}</p>
      )}
    </section>
  );
}
