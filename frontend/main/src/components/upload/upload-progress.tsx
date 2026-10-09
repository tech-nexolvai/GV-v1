import { Check, FileText, Loader2 } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Progress } from '@/components/ui/progress';
import { percentOf, type FileProgress, type Slot, type UploadView } from '@/lib/upload-progress';

const STEPS = [
  { id: 'upload', label: 'Upload' },
  { id: 'reading', label: 'AI reading' },
  { id: 'ready', label: 'Ready' },
] as const;

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The phase in words; the percentage alone uses the number face. */
function PhaseWords({ file, percent }: { file: FileProgress; percent: number }) {
  if (file.phase === 'sending') return <>Sending <span className="num">{percent}%</span></>;
  return <>{phaseWords(file, percent)}</>;
}

function phaseWords(file: FileProgress, percent: number): string {
  switch (file.phase) {
    case 'waiting': return 'Waiting';
    case 'preparing': return 'Preparing…';
    case 'sending': return `Sending ${percent}%`;
    case 'confirming': return 'Checking it arrived intact…';
    case 'done': return 'Uploaded';
    case 'combined': return 'Same file as the shop drawings: sent once';
  }
}

/**
 * The start screen while a review is being created (#1064): where the three steps stand, one bar per
 * drawing with its real share of bytes sent, and one line on what happens next. The steps come from
 * `createPackage`'s own progress reports (see `lib/upload-progress.ts`).
 */
export function UploadProgressPanel({
  view,
  files,
}: {
  view: UploadView;
  files: readonly { slot: Slot; title: string; name: string; size: number }[];
}) {
  const current = view.stage === 'upload' ? 0 : 1;
  return (
    <div data-tw data-slot="upload-progress" className="flex flex-col gap-4 font-sans">
      <ol className="flex items-center gap-2 text-sm" aria-label="Review set-up">
        {STEPS.map((step, index) => {
          const done = index < current;
          const active = index === current;
          return (
            <li key={step.id} className="flex items-center gap-2" aria-current={active ? 'step' : undefined}>
              {index > 0 && <span className={cn('h-px w-6 bg-border', done || active ? 'bg-foreground/40' : '')} aria-hidden="true" />}
              <span
                className={cn(
                  'num flex size-6 items-center justify-center rounded-full border text-xs',
                  done && 'border-foreground bg-foreground text-background',
                  active && 'border-foreground',
                )}
                aria-hidden="true"
              >
                {done ? <Check className="size-3.5" /> : active ? <Loader2 className="size-3.5 animate-spin" /> : index + 1}
              </span>
              <span className={cn(active ? 'font-medium' : 'text-muted-foreground')}>
                {step.label}
                <span className="sr-only">{done ? ', done' : active ? ', in progress' : ', not started'}</span>
              </span>
            </li>
          );
        })}
      </ol>

      <ul className="flex flex-col gap-3" aria-label="Drawings">
        {files.map((file) => {
          const progress = view.files[file.slot];
          // The combined slot follows the file that is actually being sent.
          const shown = progress.phase === 'combined' ? view.files.shop : progress;
          const percent = percentOf(shown);
          const done = shown.phase === 'done';
          return (
            <li key={file.slot} className="flex flex-col gap-1.5" data-phase={progress.phase}>
              <div className="flex items-center gap-2 text-sm">
                <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="font-medium">{file.title}</span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground" title={file.name}>
                  {file.name} · <span className="num">{formatSize(file.size)}</span>
                </span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {done && <Check className="mr-0.5 inline size-3.5" aria-hidden="true" />}
                  <PhaseWords file={progress} percent={percent} />
                </span>
              </div>
              <Progress value={percent} aria-label={`${file.title}: ${phaseWords(progress, percent)}`} />
            </li>
          );
        })}
      </ul>

      <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
        <span className="sr-only">{view.step}. </span>
        {view.stage === 'upload'
          ? 'Keep this page open until both drawings are uploaded. AI reading then takes a few minutes.'
          : 'Both drawings are in. AI reading takes a few minutes; the review opens now and shows its progress.'}
      </p>
    </div>
  );
}
