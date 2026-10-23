import { useState } from 'react';
import { Check, CircleAlert, Info, LoaderCircle, OctagonX } from 'lucide-react';

import { NumText } from '@/components/simple-review/words';
import type { ChatPreflight, ChatPreflightLine } from '@/lib/chat-api';
import { preflightMeta, preflightOrder, supervisorWords } from '@/lib/chat-preflight';
import { cn } from '@/lib/utils';

/**
 * The pre-flight health report (code, $0): what GV Review found in the upload before any money is
 * spent, each line with whether the check goes on and who acts. Greys only: outcome colours belong
 * to findings, and nothing here is a finding. Only a swapped, broken or other-project file stops.
 */

const ICONS = {
  stop: OctagonX,
  warn: CircleAlert,
  note: Info,
  ok: Check,
} as const;

function LineRow({ line }: { line: ChatPreflightLine }) {
  const Icon = ICONS[line.level];
  const meta = preflightMeta(line);
  return (
    <li data-slot="chat-preflight-line" data-id={line.id} data-level={line.level} className="flex min-w-0 gap-2">
      <Icon aria-hidden="true" className={cn('mt-[3px] size-3.5 shrink-0', line.level === 'warn' || line.level === 'stop' ? 'text-foreground' : 'text-muted-foreground')} />
      <div className="grid min-w-0 gap-0.5">
        <p className={cn('text-[13px] leading-snug break-words', line.level === 'stop' && 'font-medium')}>
          <NumText text={line.words} />
          {line.estimate && (
            <span data-slot="chat-preflight-estimate" className="ml-1.5 inline-flex h-[18px] items-center rounded-full border px-1.5 align-[1px] font-mono text-xs text-muted-foreground">
              estimate
            </span>
          )}
        </p>
        {meta && <p data-slot="chat-preflight-meta" className="text-xs leading-snug text-muted-foreground">{meta}</p>}
      </div>
    </li>
  );
}

export interface PreflightReportProps {
  preflight: ChatPreflight;
  /** Lines shown before "Show n more". */
  most?: number;
  /** The heading over the list; null: none. */
  heading?: string | null;
}

/** The report as a short list: the stops in a quiet box first, then the rest, the most important first. */
export function PreflightReport({ preflight, most = 6, heading = 'Before you run · free' }: PreflightReportProps) {
  const [all, setAll] = useState(false);
  const ordered = preflightOrder(preflight.lines);
  const stops = ordered.filter((line) => line.level === 'stop');
  const rest = ordered.filter((line) => line.level !== 'stop');
  const room = Math.max(0, most - stops.length);
  const shown = all ? rest : rest.slice(0, room);
  const hidden = rest.length - shown.length;
  return (
    <div data-slot="chat-preflight" className={cn('grid gap-2', heading && 'mt-1 border-t pt-3')}>
      {heading && <p className="text-xs font-medium text-muted-foreground">{heading}</p>}
      {stops.length > 0 && (
        <ul aria-label="Stops the check" className="grid gap-2 rounded-lg border bg-muted/50 p-2.5">
          {stops.map((line) => <LineRow key={`${line.id}-${line.words}`} line={line} />)}
        </ul>
      )}
      {preflight.state === 'working' && (
        <p data-slot="chat-preflight-working" role="status" className="flex items-center gap-2 text-xs text-muted-foreground">
          <LoaderCircle aria-hidden="true" className="size-3.5 shrink-0 animate-spin motion-reduce:animate-none" />
          Working out which pages match the architect’s (free)…
        </p>
      )}
      {shown.length > 0 && (
        <ul aria-label="What was found" className="grid gap-1.5">
          {shown.map((line) => <LineRow key={`${line.id}-${line.words}`} line={line} />)}
        </ul>
      )}
      {hidden > 0 && (
        <button
          type="button"
          data-slot="chat-preflight-more"
          onClick={() => setAll(true)}
          className="justify-self-start rounded text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          Show <span className="num">{hidden}</span> more
        </button>
      )}
    </div>
  );
}

export interface PreflightPausedProps {
  preflight: ChatPreflight;
  /** Runs the same full check again with Run anyway; absent while another answer runs. */
  onRunAnyway?: (() => void) | null;
}

/** A full check that paused before comparing anything: why, and Run anyway / Cancel. */
export function PreflightPaused({ preflight, onRunAnyway = null }: PreflightPausedProps) {
  const [cancelled, setCancelled] = useState(false);
  const [open, setOpen] = useState(false);
  const stops = preflight.lines.filter((line) => line.level === 'stop');
  const look = supervisorWords(preflight.supervisor);
  return (
    <section data-slot="chat-preflight-paused" aria-label="Check paused" className="chat-wide grid min-w-0 max-w-[40rem] gap-3 rounded-xl border bg-card p-4">
      <div className="flex items-start gap-2.5">
        <OctagonX aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-foreground" />
        <div className="grid min-w-0 gap-1">
          <h3 className="text-sm font-medium">Paused before comparing anything</h3>
          <p className="text-[13px] text-muted-foreground">Nothing was spent on comparing. Fix the upload and run again, or run it as it is.</p>
        </div>
      </div>
      <ul aria-label="Why it paused" className="grid gap-2 rounded-lg border bg-muted/50 p-2.5">
        {stops.map((line) => <LineRow key={`${line.id}-${line.words}`} line={line} />)}
        {preflight.supervisor?.verdict === 'stop' && look && (
          <li data-slot="chat-preflight-supervisor" className="flex min-w-0 gap-2">
            <CircleAlert aria-hidden="true" className="mt-[3px] size-3.5 shrink-0 text-foreground" />
            <p className="text-[13px] leading-snug font-medium break-words">{look}</p>
          </li>
        )}
      </ul>
      {preflight.lines.length > stops.length && (
        <div className="grid gap-2">
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
            className="justify-self-start rounded text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            {open ? 'Hide' : 'Show'} what GV Review found (<span className="num">{preflight.lines.length - stops.length}</span>)
          </button>
          {open && <PreflightReport preflight={{ ...preflight, lines: preflight.lines.filter((line) => line.level !== 'stop') }} heading={null} most={50} />}
        </div>
      )}
      {!cancelled && onRunAnyway && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            data-slot="chat-preflight-run-anyway"
            onClick={onRunAnyway}
            className="h-8 rounded-lg bg-primary px-3 text-sm font-medium text-primary-foreground hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
          >
            Run anyway
          </button>
          <button
            type="button"
            data-slot="chat-preflight-cancel"
            onClick={() => setCancelled(true)}
            className="h-8 rounded-lg border bg-background px-3 text-sm hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            Cancel
          </button>
        </div>
      )}
    </section>
  );
}
