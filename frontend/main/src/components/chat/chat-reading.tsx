import { useEffect, useId, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';

import { Collapse } from '@/components/chat/chat-motion';
import type { ChatReading, ChatReadingGroup, ChatReadingPage, ChatReadingSide } from '@/lib/chat-api';
import { boxOnPicture } from '@/lib/chat-result';
import { cn } from '@/lib/utils';

/**
 * #1862: the reading coverage under "How this was checked": how many of the numbers printed on the
 * drawings code read (one line per side, a quiet grey meter), the main reasons the rest were not
 * read, and a per-page table whose reasons open the page picture with each unread number boxed.
 * Counted by code when the set pack was made; it never changes a finding. Greys only (no outcome
 * colour: an unread number is neither right nor wrong).
 */

const SIDE_WORDS: Record<'vendor' | 'architect', string> = { vendor: 'Vendor’s drawings', architect: 'Architect’s drawings' };

const number = (value: number) => value.toLocaleString('en-US');

function SideLine({ side, counts }: { side: 'vendor' | 'architect'; counts: ChatReadingSide }) {
  const share = counts.printed ? Math.min(1, counts.read / counts.printed) : 1;
  return (
    <li data-slot="chat-reading-side" data-side={side} className="grid min-w-0 gap-1">
      <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3">
        <span>{SIDE_WORDS[side]}</span>
        <span className="num text-muted-foreground">
          <span className="text-foreground">{number(counts.read)}</span> of about {number(counts.printed)} read ·{' '}
          <span className="text-foreground">{counts.percent}%</span>
        </span>
      </div>
      <div aria-hidden="true" className="h-1 w-full overflow-hidden rounded-full bg-muted">
        <span className="block h-full rounded-full bg-foreground/55" style={{ width: `${share * 100}%` }} />
      </div>
    </li>
  );
}

/** The page picture zoomed to one unread number, boxed in grey; arrows step through the group's spots. */
function SpotView({ page, group, src }: { page: number; group: ChatReadingGroup; src: string }) {
  const [at, setAt] = useState(0);
  const [broken, setBroken] = useState(false);
  const [view, setView] = useState(520);
  const frame = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = frame.current;
    if (!node) return;
    const measure = () => setView(node.clientWidth || 520);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    return () => observer?.disconnect();
  }, [broken]);
  const spots = group.spots.filter((spot) => spot.box.length === 4);
  if (!spots.length || broken) return <p className="text-xs text-muted-foreground">The page picture is not available.</p>;
  const spot = spots[Math.min(at, spots.length - 1)];
  const [x0, y0, x1, y1] = spot.box;
  const box = boxOnPicture({ x0, y0, x1, y1 })!;
  const height = 200;
  const x = view / 2 - (box.left + box.width / 2);
  const y = height / 2 - (box.top + box.height / 2);
  return (
    <figure data-slot="chat-reading-spot" className="grid min-w-0 gap-1.5">
      <div ref={frame} className="relative h-[200px] overflow-hidden rounded-lg border bg-white">
        <div style={{ position: 'absolute', left: 0, top: 0, transform: `translate(${x}px, ${y}px)` }}>
          <img src={src} alt={`Page ${page}`} onError={() => setBroken(true)} className="block max-w-none" />
          {spots.map((each, index) => {
            const [a, b, c, d] = each.box;
            const shown = boxOnPicture({ x0: a, y0: b, x1: c, y1: d })!;
            return (
              <span
                key={index}
                aria-hidden="true"
                className={cn('absolute rounded-[3px] border-2', index === at ? 'border-neutral-800 shadow-[0_0_0_3px_rgb(255_255_255/0.9)]' : 'border-neutral-400/70')}
                style={{ left: shown.left - 4, top: shown.top - 4, width: shown.width + 8, height: shown.height + 8 }}
              />
            );
          })}
        </div>
      </div>
      <figcaption className="flex min-w-0 flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span className="min-w-0 break-words">
          p.<span className="num">{page}</span> · {group.words} · {spot.side === 'architect' ? 'architect’s drawing' : spot.side === 'brand' ? 'brand prototype' : 'vendor’s drawing'}
        </span>
        {spots.length > 1 && (
          <span className="inline-flex items-center gap-1">
            <button type="button" aria-label="Previous number" onClick={() => setAt((at + spots.length - 1) % spots.length)} className="inline-flex size-7 items-center justify-center rounded-md border bg-background hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
              <ChevronLeft className="size-3.5" aria-hidden="true" />
            </button>
            <span className="num min-w-[3.5rem] text-center">{at + 1} of {spots.length}</span>
            <button type="button" aria-label="Next number" onClick={() => setAt((at + 1) % spots.length)} className="inline-flex size-7 items-center justify-center rounded-md border bg-background hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
              <ChevronRight className="size-3.5" aria-hidden="true" />
            </button>
          </span>
        )}
      </figcaption>
    </figure>
  );
}

function sideCell(read: number, printed: number) {
  if (!printed) return <span className="text-muted-foreground">—</span>;
  return (
    <>
      <span className="text-foreground">{number(read)}</span>
      <span className="text-muted-foreground"> / {number(printed)}</span>
    </>
  );
}

function PageRow({ row, pagePicture }: { row: ChatReadingPage; pagePicture: ((page: number) => string) | null }) {
  const [open, setOpen] = useState<string | null>(null);
  const shown = row.not_read.find((group) => group.reason === open) ?? null;
  const canShow = Boolean(pagePicture && row.viewable);
  return (
    <li data-slot="chat-reading-page" data-page={row.page} className="grid min-w-0 gap-2 border-t px-2.5 py-2 first:border-t-0">
      <div className="grid min-w-0 grid-cols-[3rem_1fr_1fr] items-baseline gap-x-2 sm:grid-cols-[3.5rem_6.5rem_6.5rem_1fr]">
        <span className="num text-muted-foreground">p.{row.page}</span>
        <span className="num">{sideCell(row.vendor_read, row.vendor_printed)}</span>
        <span className="num">{sideCell(row.architect_read, row.architect_printed)}</span>
        <div className="col-span-3 mt-1 flex min-w-0 flex-wrap gap-1.5 sm:col-span-1 sm:mt-0">
          {row.not_read.length === 0 && <span className="text-muted-foreground">{row.strokes_counted ? 'All read' : 'Pen strokes not counted'}</span>}
          {row.not_read.map((group) =>
            canShow ? (
              <button
                key={group.reason}
                type="button"
                aria-pressed={open === group.reason}
                aria-label={`${group.words}, ${group.count}: show on the page`}
                onClick={() => setOpen(open === group.reason ? null : group.reason)}
                data-reason={group.reason}
                className={cn(
                  'inline-flex max-w-full items-center gap-1 rounded-md border px-1.5 py-0.5 text-left text-xs hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                  open === group.reason ? 'border-foreground/40 bg-accent text-foreground' : 'bg-background text-muted-foreground',
                )}
              >
                <span className="min-w-0 break-words">{group.words}</span>
                <span className="num text-foreground">{group.count}</span>
              </button>
            ) : (
              <span key={group.reason} data-reason={group.reason} className="inline-flex max-w-full items-center gap-1 rounded-md border bg-background px-1.5 py-0.5 text-xs text-muted-foreground">
                <span className="min-w-0 break-words">{group.words}</span>
                <span className="num text-foreground">{group.count}</span>
              </span>
            ),
          )}
        </div>
      </div>
      {shown && pagePicture && <SpotView key={shown.reason} page={row.page} group={shown} src={pagePicture(row.page)} />}
    </li>
  );
}

/** The reading coverage section of "How this was checked" (#1862). */
export function ReadingCoverage({ reading, pagePicture = null }: { reading: ChatReading; pagePicture?: ((page: number) => string) | null }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const sides = (['vendor', 'architect'] as const).filter((side) => reading[side]);
  const reasons = reading.lines.find((line) => line.startsWith('Not read on'));
  const notCounted = reading.lines.find((line) => line.startsWith('Pen-stroke numbers not counted'));
  const unread = reading.pages.filter((row) => row.not_read.length > 0);
  if (!sides.length) return null;
  return (
    <div data-slot="chat-reading" className="grid min-w-0 gap-1.5">
      <p className="text-xs font-medium text-muted-foreground">Numbers read</p>
      <ul className="grid max-w-md gap-2">
        {sides.map((side) => <SideLine key={side} side={side} counts={reading[side]!} />)}
      </ul>
      {reasons && <p data-slot="chat-reading-reasons" className="min-w-0 break-words text-muted-foreground">{reasons}</p>}
      {notCounted && <p data-slot="chat-reading-not-counted" className="min-w-0 break-words text-muted-foreground">{notCounted}</p>}
      {reading.pages.length > 0 && (
        <div className="min-w-0">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={id}
            onClick={() => setOpen(!open)}
            className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-md py-0.5 text-left text-xs text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <span className="min-w-0">
              By page: <span className="num text-foreground">{unread.length}</span> of <span className="num">{reading.pages.length}</span> pages have numbers not read
            </span>
            <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
          </button>
          <Collapse open={open} id={id}>
            <div className="pt-1.5">
              <div className="rounded-lg border text-xs">
                <div aria-hidden="true" className="grid grid-cols-[3rem_1fr_1fr] gap-x-2 rounded-t-lg bg-muted/50 px-2.5 py-1.5 font-medium text-muted-foreground sm:grid-cols-[3.5rem_6.5rem_6.5rem_1fr]">
                  <span>Page</span>
                  <span>Vendor</span>
                  <span>Architect</span>
                  <span className="hidden sm:block">Not read, why{pagePicture ? ' (open to see it)' : ''}</span>
                </div>
                <ul data-slot="chat-reading-pages" aria-label="Numbers read by page" className="grid min-w-0">
                  {reading.pages.map((row) => <PageRow key={`${row.file ?? ''}:${row.page}`} row={row} pagePicture={pagePicture} />)}
                </ul>
              </div>
            </div>
          </Collapse>
        </div>
      )}
    </div>
  );
}
