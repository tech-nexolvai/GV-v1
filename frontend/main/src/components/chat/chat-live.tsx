/**
 * The live full check card (#1727, UI redesign step 4; design spec 4.3): what the check is doing now,
 * the vendor's drawings moving from waiting to done, and only what code has PROVED so far. Read from
 * the server's live events (`lib/chat-live.ts`); nothing here is guessed or counted on the screen.
 */

import { memo, useEffect, useRef, useState } from 'react';
import { Check, CircleAlert, CircleDashed, DoorOpen, FileCheck2, FileText, ListMinus, Mail, Package, Sheet as SheetIcon, Square, X } from 'lucide-react';

import { OutcomeGlyph } from '@/components/chat/chat-result';
import { GVSpark, Shimmer } from '@/components/chat/chat-motion';
import { Button } from '@/components/ui/button';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import type { ChatFile } from '@/lib/chat-api';
import {
  badgesByDrawing,
  barShare,
  barWords,
  elapsedWords,
  leftWords,
  nowWords,
  phaseTitle,
  provedWrongs,
  secondsLeft,
  STEPS,
  stepOf,
  tallyOf,
  usualWords,
  type LiveDrawing,
  type LiveFinding,
  type LiveState,
} from '@/lib/chat-live';
import { useReducedMotion } from '@/lib/chat-motion';
import { areaWords, itemParts, shouldBeValue, vendorValue } from '@/lib/chat-result';
import { cn } from '@/lib/utils';

export interface LiveCheckProps {
  live: LiveState;
  /** The server's time-left estimate (the `progress` event), when it sent one. */
  eta?: number | null;
  /** The files made so far (the last phase shows them as they arrive). */
  files?: readonly ChatFile[];
  /** "Sonnet 5.5": the model's short name, for the footer. */
  model?: string | null;
  /** "$2.13": the cost so far. */
  cost?: string | null;
  /** The vendor's page picture (the set pack's, 100 dpi), for the drawing tiles. */
  pagePicture?: ((page: number) => string) | null;
  /** Stop the check (after the reviewer confirmed it); left out when it cannot be stopped from here. */
  onStop?: () => void;
  /** Stop was pressed here and the server has not said so yet. */
  stopping?: boolean;
  /** The Stop confirmation is open (the card's Stop or the composer's). */
  askStop?: boolean;
  onAskStop?: (open: boolean) => void;
}

function LiveCheckCard({ live: fromServer, eta = null, files = [], model = null, cost = null, pagePicture = null, onStop, stopping = false, askStop = false, onAskStop }: LiveCheckProps) {
  const reduced = useReducedMotion();
  // #1744: Stop pressed here says "Stopping…" at once, before the server's own word arrives.
  const live = stopping && !fromServer.stopping
    ? { ...fromServer, stopping: { in_flight: fromServer.drawings.filter((drawing) => drawing.status === 'comparing').length, not_started: 0 } }
    : fromServer;
  const { now, left } = useClock(live, eta);
  const isStopping = stopping || live.phase.stopping || Boolean(live.stopping);
  const service = live.service ?? null;
  const share = barShare(live);
  const step = stepOf(live.phase.phase);
  const comparing = live.drawings.filter((drawing) => drawing.status === 'comparing').length;
  const stopRef = useRef<HTMLButtonElement>(null);

  // The composer's Stop opens the same question here: the card's button is brought into view first.
  useEffect(() => {
    if (askStop) stopRef.current?.scrollIntoView?.({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' });
  }, [askStop, reduced]);

  const tally = tallyOf(live);
  const waiting = tally.waiting_second_look;
  const leftText = isStopping || service ? null : leftWords(left) ?? usualWords(live);

  return (
    <section data-slot="chat-live" data-phase={live.phase.phase} aria-label="Full check in progress" className="chat-wide min-w-0 overflow-hidden rounded-xl border bg-card text-card-foreground shadow-xs">
      {/* Header: what it is doing, the clock, Stop */}
      <div className="flex min-w-0 items-start gap-3 px-5 pt-[18px] max-sm:px-4 max-sm:pt-4">
        <GVSpark state={service ? 'still' : 'working'} size={20} className="mt-0.5 shrink-0" label="GV Review is working" />
        <div className="min-w-0 flex-1">
          <h3 data-slot="chat-live-title" className="text-base leading-6 font-semibold tracking-[-0.01em]">{phaseTitle(live)}</h3>
          <p data-slot="chat-live-now" aria-live="polite" className="min-h-[22px] truncate text-sm leading-[22px] text-muted-foreground max-sm:line-clamp-2 max-sm:whitespace-normal">
            {!service && <NowLine live={live} active={!isStopping} />}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          <span data-slot="chat-live-elapsed" title="Time since the check started" className="num text-[13px] text-muted-foreground">{elapsedWords(live, now)}</span>
          {onStop && !service && (
            <Popover open={askStop && !isStopping} onOpenChange={(open) => onAskStop?.(open)}>
              <PopoverAnchor asChild>
                <Button ref={stopRef} type="button" variant="outline" size="sm" disabled={isStopping} onClick={() => onAskStop?.(true)} data-slot="chat-live-stop">
                  <Square className="size-3.5" aria-hidden="true" />
                  Stop
                </Button>
              </PopoverAnchor>
              <PopoverContent align="end" sideOffset={8} data-tw data-slot="chat-live-stop-confirm" className="chat-brand w-80 font-sans text-sm antialiased">
                <h4 className="font-semibold">Stop the check?</h4>
                <p className="mt-1.5 text-[13px] leading-5 text-muted-foreground">
                  {comparing > 0 ? <>The <span className="num">{comparing}</span> {comparing === 1 ? 'drawing' : 'drawings'} being compared now will finish and be kept.</> : 'What was compared so far is kept.'}{' '}
                  Nothing new starts. The result will say <span className="font-medium text-foreground">“Check incomplete: not for sending”</span>.
                </p>
                <div className="mt-3 flex justify-end gap-2">
                  <Button type="button" variant="outline" size="sm" onClick={() => onAskStop?.(false)}>Keep checking</Button>
                  <Button
                    type="button"
                    size="sm"
                    autoFocus
                    className="bg-foreground text-background hover:bg-foreground/90"
                    onClick={() => {
                      onAskStop?.(false);
                      onStop();
                    }}
                  >
                    Stop check
                  </Button>
                </div>
              </PopoverContent>
            </Popover>
          )}
        </div>
      </div>

      {service ? (
        <div data-slot="chat-live-service" role="alert" className="mx-5 mt-4 flex items-start gap-2.5 rounded-lg border bg-muted/40 px-3.5 py-3 text-[13.5px] max-sm:mx-4">
          <CircleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0">
            <span className="font-medium">The AI service stopped every call:</span> {service} What was compared before is kept in the result.
          </span>
        </div>
      ) : (
        <>
          {/* Stepper */}
          <ol data-slot="chat-live-steps" className="flex min-w-0 list-none items-center gap-2.5 px-5 pt-3.5 text-[13px] text-muted-foreground/70 max-sm:gap-1.5 max-sm:px-4 max-sm:pt-3">
            {STEPS.map((name, at) => {
              const state = at < step ? 'done' : at === step ? 'run' : 'wait';
              return (
                <li key={name} data-state={state} aria-current={state === 'run' ? 'step' : undefined} className={cn('inline-flex items-center gap-[7px] whitespace-nowrap', state === 'done' && 'text-muted-foreground', state === 'run' && 'font-medium text-foreground')}>
                  {at > 0 && <span aria-hidden="true" className="mr-[3px] h-px w-7 bg-border max-sm:w-3.5" />}
                  <span aria-hidden="true" className={cn('relative grid size-4 shrink-0 place-items-center rounded-full border-[1.5px] border-border', state === 'done' && 'border-foreground bg-foreground text-background', state === 'run' && 'chat-live-ring')}>
                    {state === 'done' && <Check className="size-2.5" strokeWidth={3} />}
                  </span>
                  <span className={cn(state !== 'run' && 'max-sm:sr-only')}>{name}</span>
                  {state === 'done' && <span className="sr-only">(done)</span>}
                </li>
              );
            })}
          </ol>

          {/* Bar */}
          <div className="px-5 pt-4 max-sm:px-4 max-sm:pt-3.5">
            <div
              data-slot="chat-live-bar"
              role="progressbar"
              aria-label="Full check progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={share === null ? undefined : Math.round(share)}
              aria-valuetext={barWords(live) || undefined}
              data-indeterminate={share === null ? 'true' : 'false'}
              className="chat-live-bar relative h-1.5 overflow-hidden rounded-full bg-muted"
            >
              <i style={share === null ? undefined : { width: `${share.toFixed(1)}%` }} />
            </div>
            <div className="mt-2 flex min-w-0 justify-between gap-3 text-[13px] text-muted-foreground">
              <span data-slot="chat-live-count" className="min-w-0">{isStopping ? 'Stopping: nothing new starts' : <CountWords words={barWords(live)} />}</span>
              <span data-slot="chat-live-left" className="shrink-0 transition-opacity duration-150 motion-reduce:transition-none">{leftText}</span>
            </div>
          </div>
        </>
      )}

      {/* The vendor's drawings */}
      {live.drawings.length > 0 && (
        <>
          <div className="flex min-w-0 items-baseline justify-between gap-3 px-5 pt-5 pb-2.5 max-sm:px-4 max-sm:pt-[18px] max-sm:pb-2">
            <h4 className="text-[13px] font-semibold">The vendor’s drawings</h4>
            <div aria-hidden="true" className="flex gap-3.5 text-xs text-muted-foreground max-md:hidden">
              <span className="inline-flex items-center gap-1.5"><i className="h-[9px] w-3 rounded-[2px] border bg-muted/60" />Waiting</span>
              <span className="inline-flex items-center gap-1.5"><i className="h-[9px] w-3 rounded-[2px] border-[1.5px] border-foreground bg-background" />Being compared</span>
              <span className="inline-flex items-center gap-1.5"><i className="h-[9px] w-3 rounded-[2px] border border-muted-foreground/60 bg-background" />Done</span>
              <span className="inline-flex items-center gap-1.5"><OutcomeGlyph kind="wrong" className="size-3 [&_svg]:size-2" />Wrong found</span>
            </div>
          </div>
          <Tiles drawings={live.drawings} badges={badgesByDrawing(live)} stopped={isStopping} pagePicture={pagePicture} />
        </>
      )}

      {/* Files made (the last phase) */}
      {(live.phase.phase === 'making_files' || live.phase.phase === 'done') && files.length > 0 && (
        <ul data-slot="chat-live-files" aria-label="Files made" className="flex list-none flex-wrap gap-2 px-5 pt-3.5 max-sm:px-4">
          {files.map((file) => {
            const Icon = fileIcon(file);
            return (
              <li key={file.id} className="chat-rise inline-flex h-8 items-center gap-2 rounded-lg border bg-background pr-3 pl-2.5 text-[13px]">
                <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
                {fileWords(file)}
              </li>
            );
          })}
        </ul>
      )}

      {/* Found so far: the tally, then the proved Wrongs, newest first */}
      <div className="mt-5 flex min-w-0 items-center justify-between gap-3 border-t px-5 pt-5 pb-2 max-sm:flex-col max-sm:items-start max-sm:gap-1.5 max-sm:px-4 max-sm:pt-[18px]">
        <h4 className="text-[13px] font-semibold">Found so far</h4>
        <div data-slot="chat-live-tally" className="flex items-center gap-3.5 text-[13px] text-muted-foreground">
          <span data-count="wrong" className="inline-flex items-center gap-1.5"><OutcomeGlyph kind="wrong" className="size-4 [&_svg]:size-2.5" /><Rolling value={tally.wrong} reduced={reduced} /> Wrong</span>
          <span data-count="right" className="inline-flex items-center gap-1.5"><OutcomeGlyph kind="right" className="size-4 [&_svg]:size-2.5" /><Rolling value={tally.right} reduced={reduced} /> Right</span>
          {/* #1839: decided, never red: the architect's size breaks a GV rule (shown once there is one). */}
          {(tally.rule_note ?? 0) > 0 && <span data-count="rule_note" className="inline-flex items-center gap-1.5"><OutcomeGlyph kind="rule_note" className="size-4" /><Rolling value={tally.rule_note ?? 0} reduced={reduced} /> Ask the architect</span>}
          <span data-count="not_compared" className="inline-flex items-center gap-1.5"><OutcomeGlyph kind="not_compared" className="size-4 [&_svg]:size-2.5" /><Rolling value={tally.not_compared} reduced={reduced} /> Not compared</span>
        </div>
      </div>
      <Feed live={live} />
      {waiting > 0 && (
        <p data-slot="chat-live-waiting" className="flex items-center gap-2.5 px-5 py-2.5 text-[13px] text-muted-foreground max-sm:px-4">
          <GVSpark state={isStopping || service ? 'still' : 'thinking'} size={16} className="shrink-0" />
          <span>
            <span className="num font-semibold text-foreground">{waiting}</span> possible {waiting === 1 ? 'difference' : 'differences'} waiting for a second look. {waiting === 1 ? 'It becomes' : 'They become'} Wrong only if it agrees.
          </span>
        </p>
      )}

      {/* Footer */}
      <div data-slot="chat-live-foot" className="mt-2 flex min-w-0 items-center justify-between gap-3 border-t bg-muted/40 px-5 py-3 text-[12.5px] text-muted-foreground max-sm:flex-col max-sm:items-start max-sm:gap-1.5 max-sm:px-4">
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <DoorOpen className="size-3.5 shrink-0" aria-hidden="true" />
          You can leave this page. The check keeps going, and the sidebar shows how far it is.
        </span>
        {(model || cost) && (
          <span className="shrink-0">
            {model}
            {model && cost && ' · '}
            {cost && <><span className="num">{cost}</span> so far</>}
          </span>
        )}
      </div>
    </section>
  );
}

/**
 * Drawn again only when what it shows changes: a reopened page replays hundreds of events while the
 * snapshot is shown, and the page's own callbacks are new on every render.
 */
export const LiveCheck = memo(LiveCheckCard, (before, after) =>
  before.live === after.live &&
  before.eta === after.eta &&
  before.files === after.files &&
  before.model === after.model &&
  before.cost === after.cost &&
  before.stopping === after.stopping &&
  before.askStop === after.askStop &&
  Boolean(before.onStop) === Boolean(after.onStop) &&
  Boolean(before.pagePicture) === Boolean(after.pagePicture));

// ── Parts ─────────────────────────────────────────────────

/** "Comparing p.47 · Buffet small with the architect’s · 7 more at the same time", with the decided shimmer. */
function NowLine({ live, active }: { live: LiveState; active: boolean }) {
  const now = nowWords(live);
  // #1741 review: a neutral light on this line (red means Wrong; maroon is the logo and the star only).
  if (now.page === null) return <Shimmer active={active} tone="ink">{now.words}</Shimmer>;
  return (
    <Shimmer active={active} tone="ink">
      Comparing <span className="num text-[13px]">p.{now.page}</span>
      {now.area && <> · {now.area}</>} {now.words}
      {now.more > 0 && <span className="text-muted-foreground/70"> · {now.more} more at the same time</span>}
    </Shimmer>
  );
}

/** The bar's words with every number in the numbers' face ("23 of 39 drawings compared"). */
function CountWords({ words }: { words: string }) {
  const parts = words.split(/(\d+(?:–\d+)?)/);
  return <>{parts.map((part, at) => (at % 2 === 1 ? <span key={at} className={cn('num', at === 1 && 'font-medium text-foreground')}>{part}</span> : part))}</>;
}

function Tiles({ drawings, badges, stopped, pagePicture }: { drawings: readonly LiveDrawing[]; badges: Map<string, number>; stopped: boolean; pagePicture: ((page: number) => string) | null }) {
  return (
    <ul data-slot="chat-live-drawings" aria-label="The vendor’s drawings" className="grid list-none grid-cols-8 gap-[5px] px-5 max-sm:px-4 sm:grid-cols-[repeat(13,minmax(0,1fr))] sm:gap-[7px]">
      {drawings.map((drawing) => {
        // After Stop, a drawing that never started will not be compared.
        const status = stopped && drawing.status === 'waiting' ? 'skipped' : drawing.status;
        const wrong = badges.get(drawing.id) ?? 0;
        const words = [`p.${drawing.page}`, areaWords(drawing.area), drawing.round ? `round ${drawing.round}` : null, TILE_WORDS[status], drawing.reason, wrong ? `${wrong} Wrong` : null].filter(Boolean).join(' · ');
        return (
          <li key={drawing.id} data-slot="chat-live-drawing" data-status={status} title={words} aria-label={words} className="chat-live-tile relative flex min-w-0 flex-col gap-1">
            <div
              className={cn(
                'chat-live-thumb relative aspect-[1.414] overflow-hidden rounded-md border bg-muted/60 transition-[border-color,box-shadow] duration-200',
                status === 'comparing' && 'border-foreground shadow-[0_0_0_1px_var(--foreground)]',
                (status === 'failed' || status === 'skipped') && 'border-dashed border-muted-foreground/50',
              )}
            >
              {pagePicture && <Thumb src={pagePicture(drawing.page)} status={status} />}
              {status === 'answered' && !wrong && (
                <span aria-hidden="true" className="chat-live-pop absolute right-1 bottom-1 grid size-3.5 place-items-center rounded-full bg-foreground text-background max-sm:hidden">
                  <Check className="size-[9px]" strokeWidth={3} />
                </span>
              )}
            </div>
            <span aria-hidden="true" className={cn('num truncate text-xs leading-[14px] text-muted-foreground/70', status === 'comparing' && 'text-foreground', status === 'answered' && 'text-muted-foreground')}>p.{drawing.page}</span>
            {wrong > 0 && (
              <span aria-hidden="true" data-slot="chat-live-badge" className="chat-live-pop num absolute -top-1.5 -right-1.5 inline-flex h-[18px] min-w-[18px] items-center justify-center gap-px rounded-full bg-outcome-fail px-1 text-xs font-semibold text-white shadow-[0_0_0_2px_var(--card)]">
                <X className="size-2.5" strokeWidth={3} />
                {wrong}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

const TILE_WORDS: Record<LiveDrawing['status'], string> = {
  waiting: 'waiting',
  comparing: 'being compared',
  answered: 'done',
  failed: 'could not be compared',
  skipped: 'not compared',
};

/** A page picture, faded while waiting, grey while compared, in full once done; nothing if it fails. */
function Thumb({ src, status }: { src: string; status: LiveDrawing['status'] }) {
  const [broken, setBroken] = useState(false);
  if (broken) return null;
  return (
    <img
      src={src}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setBroken(true)}
      className={cn(
        // A page is paper: white behind its lines (some pictures are transparent), in both themes.
        'size-full bg-white object-cover transition-[opacity,filter] duration-400 motion-reduce:transition-none',
        status === 'answered' ? 'opacity-100' : status === 'comparing' ? 'opacity-85 grayscale' : status === 'waiting' ? 'opacity-[0.28] grayscale contrast-90' : 'opacity-20 grayscale',
      )}
    />
  );
}

function Feed({ live }: { live: LiveState }) {
  const rows = provedWrongs(live);
  // Rows already there when the card first drew (a reload) do not flash; only new ones do.
  const [first] = useState(() => new Set(rows.map((row) => row.key)));
  if (!rows.length) {
    return (
      <p data-slot="chat-live-feed-empty" className="flex items-center gap-2.5 px-5 py-2.5 text-[13px] text-muted-foreground max-sm:px-4">
        <CircleDashed className="size-4 shrink-0" aria-hidden="true" />
        Wrong items appear here as soon as they are proved.
      </p>
    );
  }
  return (
    <ul data-slot="chat-live-feed" aria-label="Wrong so far, newest first" className="flex list-none flex-col px-3 pb-1.5 max-sm:px-2">
      {rows.map((row) => <FeedRow key={row.key} finding={row} fresh={!first.has(row.key)} />)}
    </ul>
  );
}

function FeedRow({ finding, fresh }: { finding: LiveFinding; fresh: boolean }) {
  const parts = itemParts(finding.item || '');
  const area = parts.area ?? areaWords(finding.area);
  const vendor = vendorValue({ vendor: finding.vendor ?? '' }).main;
  const should = shouldBeValue({ proof: finding.should_be ? ({ should_be: finding.should_be } as never) : null, architect: finding.architect ?? null })?.main ?? null;
  const by = finding.proved_by === 'checked_twice' ? 'checked twice' : 'proved by code';
  const page = finding.page != null ? `p.${finding.page}` : null;
  // A value in words ("not on the vendor's sheets") reads in the words' face; numbers in the numbers'.
  const face = (value: string) => (/\d/.test(value) ? 'num' : 'font-sans');
  return (
    <li data-slot="chat-live-row" className={cn('flex min-w-0 items-center gap-3 rounded-lg px-2 py-[9px] text-[13.5px] max-sm:flex-wrap max-sm:items-start max-sm:gap-x-2.5 max-sm:gap-y-1', fresh && 'chat-live-row')}>
      <OutcomeGlyph kind="wrong" className="max-sm:mt-0.5" />
      <span className="sr-only">Wrong:</span>
      <span className="num w-9 shrink-0 text-xs text-muted-foreground max-sm:hidden">{page}</span>
      <span className="min-w-0 flex-1 truncate max-sm:basis-[calc(100%-1.75rem)] max-sm:whitespace-normal">
        {parts.thing}
        {area && <span className="text-[13px] text-muted-foreground"> · {area}</span>}
        {page && <span className="num text-xs text-muted-foreground sm:hidden"> · {page}</span>}
      </span>
      {(vendor || should) && (
        <span className="flex shrink-0 items-center gap-2 text-[13px] whitespace-nowrap max-sm:ml-7">
          {vendor && <span className={cn('font-medium text-outcome-fail-fg', face(vendor))}>{vendor}</span>}
          {vendor && should && <span aria-hidden="true" className="text-muted-foreground/70">→</span>}
          {vendor && should && <span className="sr-only">should be</span>}
          {should && <span className={cn('font-medium text-outcome-pass-fg', face(should))}>{should}</span>}
        </span>
      )}
      <span className="w-[104px] shrink-0 text-right text-xs whitespace-nowrap text-muted-foreground/70 max-sm:hidden">{by}</span>
    </li>
  );
}

/** A count that rolls from its old value to the new one (420 ms); instant under reduced motion. */
function Rolling({ value, reduced }: { value: number; reduced: boolean }) {
  const [shown, setShown] = useState(value);
  const shownNow = useRef(value);
  useEffect(() => {
    shownNow.current = shown;
  });
  useEffect(() => {
    const from = shownNow.current;
    if (from === value) return;
    let frame = 0;
    if (reduced) {
      frame = requestAnimationFrame(() => setShown(value));
      return () => cancelAnimationFrame(frame);
    }
    const start = performance.now();
    const step = (time: number) => {
      const share = Math.min(1, Math.max(0, (time - start) / 420));
      setShown(share >= 1 ? value : Math.round(from + (value - from) * (1 - (1 - share) ** 3)));
      if (share < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value, reduced]);
  return (
    <span data-value={value} className="num font-semibold text-foreground">
      <span aria-hidden="true">{shown}</span>
      <span className="sr-only">{value}</span>
    </span>
  );
}

/** The elapsed clock (every second) and time left (at most every 5 s, so it never flickers). */
function useClock(live: LiveState, eta: number | null) {
  const latest = useRef({ live, eta });
  useEffect(() => {
    latest.current = { live, eta };
  });
  const [now, setNow] = useState(() => Date.now());
  const [left, setLeft] = useState<{ value: number | null; at: number }>(() => ({ value: secondsLeft(live, Date.now(), eta), at: Date.now() }));
  useEffect(() => {
    const timer = window.setInterval(() => {
      const time = Date.now();
      setNow(time);
      const value = secondsLeft(latest.current.live, time, latest.current.eta);
      setLeft((was) => ((was.value === null) !== (value === null) || time - was.at >= 5000 ? { value, at: time } : was));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);
  return { now, left: left.value };
}

function fileIcon(file: ChatFile) {
  if (file.kind === 'cloud_drawing') return FileText;
  if (file.kind === 'check_report') return FileCheck2;
  if (file.kind === 'package') return Package;
  if (/letter/i.test(file.name)) return Mail;
  if (/not.compared/i.test(file.name)) return ListMinus;
  if (/\.(xlsx|csv)$/i.test(file.name)) return SheetIcon;
  return FileText;
}

function fileWords(file: ChatFile): string {
  return file.name.replace(/\.(pdf|xlsx|csv|zip)$/i, '');
}
