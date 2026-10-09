import { useEffect, useRef } from 'react';
import { ImageOff, RefreshCw } from 'lucide-react';

import { cn } from '@/lib/utils';
import { isSplitPage } from '@/lib/countertop-results';
import { boundsOf, svgPoints, zoomAt, type Mark, type Reading, type Size, type Tone, type View, type ViewerTarget } from '@/lib/drawing-viewer';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { BlobState } from './blob-cache';

/** Literal class strings, so Tailwind sees every one. Colour always comes with the pill's glyph and word. */
const TONE_OUTLINE: Record<Tone, string> = {
  pass: 'stroke-outcome-pass-fg fill-outcome-pass-fg/10',
  fail: 'stroke-outcome-fail-fg fill-outcome-fail-fg/10',
  review: 'stroke-outcome-review-fg fill-outcome-review-fg/10',
  missing: 'stroke-outcome-missing-fg fill-outcome-missing-fg/10 [stroke-dasharray:6_4]',
};

const TONE_PILL: Record<Tone, string> = {
  pass: 'border-outcome-pass-fg/40 bg-outcome-pass-bg text-outcome-pass-fg',
  fail: 'border-outcome-fail-fg bg-outcome-fail-fg text-background',
  review: 'border-outcome-review-fg/60 bg-outcome-review-bg text-outcome-review-fg',
  missing: 'border-dashed border-outcome-missing-fg/60 bg-outcome-missing-bg text-outcome-missing-fg',
};

export function TonePill({ target, className }: { target: Pick<ViewerTarget, 'tone' | 'glyph' | 'word'>; className?: string }) {
  return (
    <span className={cn('inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap', TONE_PILL[target.tone], className)}>
      <OutcomeIcon outcome={target.glyph} size={12} />
      {target.word}
    </span>
  );
}

/**
 * The page picture with the result's outline, zoomed and panned by the reviewer (#1045).
 *
 * The outline, the other countertops and the reading spots sit in an SVG whose viewBox is the page's
 * own 0–1 square, laid exactly over the picture; zoom and pan move that one box, so they stay on the
 * drawing at every zoom. Strokes do not scale, so the outline stays crisp.
 */
export function PageCanvas({
  picture,
  onRetry,
  target,
  others,
  marks = [],
  activeMark = null,
  readings,
  activeReading,
  natural,
  view,
  limits,
  onView,
  onLoaded,
  onBox,
  onSelect,
}: {
  picture: BlobState;
  onRetry: () => void;
  target: ViewerTarget;
  others: ViewerTarget[];
  /** Grey marks on this page (the architect's dimensions, #1085), drawn from stored locations only. */
  marks?: Mark[];
  /** The mark to stand out (a span the reviewer is looking at in the pairing picker). */
  activeMark?: string | null;
  readings: Reading[];
  activeReading: string | null;
  natural: Size | null;
  view: View | null;
  limits: { min: number; max: number } | null;
  onView: React.Dispatch<React.SetStateAction<View | null>>;
  onLoaded: (size: Size) => void;
  onBox: (size: Size) => void;
  onSelect: (target: ViewerTarget) => void;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const gesture = useRef<{ pointers: Map<number, { x: number; y: number }>; dragging: boolean; suppressClick: boolean; start: { x: number; y: number } | null }>({
    pointers: new Map(),
    dragging: false,
    suppressClick: false,
    start: null,
  });

  // The viewport's size, for fitting and centring.
  useEffect(() => {
    const element = viewport.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => onBox({ w: entry.contentRect.width, h: entry.contentRect.height }));
    observer.observe(element);
    return () => observer.disconnect();
  }, [onBox]);

  // Wheel zoom at the cursor. A native listener, because React's is passive and cannot stop the page scrolling.
  useEffect(() => {
    const element = viewport.current;
    if (!element || !limits) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      // Trackpad pinches arrive as ctrl+wheel with small deltas.
      const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0015));
      onView((current) => (current ? zoomAt(current, factor, { x: event.clientX - rect.left, y: event.clientY - rect.top }, limits) : current));
    };
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => element.removeEventListener('wheel', onWheel);
  }, [limits, onView]);

  function local(event: React.PointerEvent) {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function onPointerDown(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    const g = gesture.current;
    g.pointers.set(event.pointerId, local(event));
    g.start = local(event);
    g.dragging = false;
  }

  function onPointerMove(event: React.PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    const previous = g.pointers.get(event.pointerId);
    if (!previous || !limits) return;
    const point = local(event);
    if (g.pointers.size >= 2) {
      // Pinch: zoom by the change in finger distance, about their midpoint, and follow the midpoint.
      const [a, b] = [...g.pointers.entries()].map(([id, p]) => (id === event.pointerId ? point : p));
      const [pa, pb] = [...g.pointers.values()];
      const before = Math.hypot(pa.x - pb.x, pa.y - pb.y);
      const after = Math.hypot(a.x - b.x, a.y - b.y);
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const midBefore = { x: (pa.x + pb.x) / 2, y: (pa.y + pb.y) / 2 };
      if (before > 0) {
        onView((current) => {
          if (!current) return current;
          const zoomed = zoomAt(current, after / before, mid, limits);
          return { ...zoomed, x: zoomed.x + mid.x - midBefore.x, y: zoomed.y + mid.y - midBefore.y };
        });
      }
      g.dragging = true;
      g.pointers.set(event.pointerId, point);
      return;
    }
    if (!g.dragging && g.start && Math.hypot(point.x - g.start.x, point.y - g.start.y) > 4) {
      // Only now capture, so a plain click still reaches another countertop's outline.
      g.dragging = true;
      event.currentTarget.setPointerCapture?.(event.pointerId);
    }
    if (g.dragging) {
      const dx = point.x - previous.x;
      const dy = point.y - previous.y;
      onView((current) => (current ? { ...current, x: current.x + dx, y: current.y + dy } : current));
    }
    g.pointers.set(event.pointerId, point);
  }

  function onPointerEnd(event: React.PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    g.pointers.delete(event.pointerId);
    if (g.dragging && g.pointers.size === 0) {
      g.suppressClick = true;
      g.dragging = false;
      window.setTimeout(() => {
        g.suppressClick = false;
      }, 0);
    }
  }

  const page = target.page;
  const ready = picture.status === 'ready';
  const placed = ready && natural && view;
  const bounds = target.outline ? boundsOf(target.outline) : null;

  return (
    <div
      ref={viewport}
      data-slot="drawing-canvas"
      className="relative min-h-0 flex-1 cursor-grab touch-none overflow-hidden bg-muted/50 select-none active:cursor-grabbing"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerEnd}
      onPointerCancel={onPointerEnd}
      onClickCapture={(event) => {
        if (gesture.current.suppressClick) {
          event.stopPropagation();
          event.preventDefault();
        }
      }}
    >
      {ready && (
        <div
          data-slot="drawing-page"
          // The paper is white in both themes, so what is drawn on it uses the light colours.
          data-theme="light"
          className="absolute top-0 left-0 shadow-sm"
          style={
            placed
              ? { width: natural.w * view.scale, height: natural.h * view.scale, transform: `translate(${view.x}px, ${view.y}px)` }
              : { visibility: 'hidden' }
          }
        >
          <img
            key={picture.url}
            src={picture.url}
            alt={`Vendor drawing, page ${page}`}
            draggable={false}
            className="block size-full bg-white"
            onLoad={(event) => onLoaded({ w: event.currentTarget.naturalWidth, h: event.currentTarget.naturalHeight })}
          />
          <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full overflow-visible" aria-hidden={!target.outline} role={target.outline ? 'img' : undefined} aria-label={target.outline ? `Outline of ${target.label} on page ${page}` : undefined}>
            {others.map((other) =>
              other.outline ? (
                <polygon
                  key={other.key}
                  data-other={other.key}
                  points={svgPoints(other.outline)}
                  vectorEffect="non-scaling-stroke"
                  strokeWidth={1.5}
                  className="pointer-events-auto cursor-pointer fill-transparent stroke-muted-foreground/70 [stroke-dasharray:5_4] hover:fill-foreground/5 hover:stroke-foreground"
                  onClick={() => onSelect(other)}
                >
                  <title>{`${other.label} — ${other.word}. Click to show it.`}</title>
                </polygon>
              ) : null,
            )}
            {readings.map((reading) =>
              reading.spot ? (
                <polygon
                  key={reading.observationId}
                  data-reading={reading.observationId}
                  points={svgPoints(reading.spot)}
                  vectorEffect="non-scaling-stroke"
                  strokeWidth={activeReading === reading.observationId ? 2.5 : 1.5}
                  className={cn(
                    // Each number the result used is highlighted where it was read (decided 2026-10-08). Grey,
                    // not a colour: only outcomes are coloured (decided 2026-10-08).
                    activeReading === reading.observationId ? 'fill-neutral-900/20 stroke-neutral-900' : 'fill-neutral-900/[0.07] stroke-neutral-900/60',
                  )}
                >
                  <title>{`${reading.label}: ${reading.value}`}</title>
                </polygon>
              ) : null,
            )}
            {marks.map((mark) =>
              mark.outline ? (
                <polygon
                  key={mark.key}
                  data-mark={mark.key}
                  points={svgPoints(mark.outline)}
                  vectorEffect="non-scaling-stroke"
                  strokeWidth={activeMark === mark.key ? 2.5 : 1.5}
                  // Grey, not an outcome colour: the architect's dimension is a reference, not a result.
                  className={activeMark === mark.key ? 'fill-neutral-900/15 stroke-neutral-900' : 'fill-neutral-500/10 stroke-neutral-500'}
                >
                  <title>{mark.label}</title>
                </polygon>
              ) : null,
            )}
            {target.outline && (
              <>
                <polygon points={svgPoints(target.outline)} vectorEffect="non-scaling-stroke" strokeWidth={5} className="fill-none stroke-background/90" />
                <polygon data-outline={target.key} points={svgPoints(target.outline)} vectorEffect="non-scaling-stroke" strokeWidth={2.25} className={TONE_OUTLINE[target.tone]} />
              </>
            )}
          </svg>
          {bounds && placed && (
            <span
              data-slot="drawing-pin"
              className="pointer-events-none absolute"
              style={{ left: `${bounds.x * 100}%`, top: `${bounds.y * 100}%`, transform: 'translate(0, calc(-100% - 6px))' }}
            >
              <TonePill target={target} className="shadow-sm" />
            </span>
          )}
        </div>
      )}

      {picture.status === 'loading' && page !== null && (
        <div className="absolute inset-0 flex items-center justify-center p-6" role="status">
          <Skeleton className="aspect-[1.414] w-full max-w-xl" />
          <span className="sr-only">Loading page {page}…</span>
        </div>
      )}
      {(ready && !placed) && <span className="sr-only" role="status">Placing page {page}…</span>}
      {picture.status === 'not-ready' && (
        <CanvasNote icon={<ImageOff className="size-5" aria-hidden="true" />} line={`Picture of page ${page} not ready`} why="Page pictures are made in the background after upload. This screen never starts that; try again in a moment." onRetry={onRetry} />
      )}
      {picture.status === 'error' && (
        <CanvasNote icon={<ImageOff className="size-5" aria-hidden="true" />} line="The page picture could not be loaded" why={picture.error} onRetry={onRetry} role="alert" />
      )}
      {page === null && <CanvasNote icon={<ImageOff className="size-5" aria-hidden="true" />} line="No stored location" why="This result has no recorded page or outline, so nothing is drawn." />}
      {placed && !target.outline && (
        <span data-slot="no-outline" className="absolute top-3 left-3 rounded-full border bg-background/95 px-2 py-0.5 text-xs text-muted-foreground shadow-sm">
          {/* A split page (#1093) has no line at all; anything else had one whose place was not stored. */}
          {target.row && isSplitPage(target.row) ? 'No line chosen: nothing is outlined' : 'Outline not stored: nothing is drawn'}
        </span>
      )}
    </div>
  );
}

function CanvasNote({ icon, line, why, onRetry, role = 'status' }: { icon: React.ReactNode; line: string; why: string; onRetry?: () => void; role?: 'status' | 'alert' }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center" role={role} data-slot="drawing-note">
      <span className="text-muted-foreground">{icon}</span>
      <p className="flex items-center gap-1.5 text-sm font-medium">
        {line}
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" className="inline-flex size-5 items-center justify-center rounded-full border text-xs text-muted-foreground" aria-label="Why?">
              ?
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-64">{why}</TooltipContent>
        </Tooltip>
      </p>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          <RefreshCw /> Try again
        </Button>
      )}
    </div>
  );
}
