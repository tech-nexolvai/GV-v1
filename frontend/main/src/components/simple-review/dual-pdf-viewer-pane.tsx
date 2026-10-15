import { useEffect, useEffectEvent, useMemo, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Crosshair, ImageOff, MoveHorizontal, RefreshCw, ZoomIn, ZoomOut } from 'lucide-react';

import { ApiError } from '@/api/client';
import { cn } from '@/lib/utils';
import { boundsOf, svgPoints, zoomAt, type Point, type Size, type View } from '@/lib/drawing-viewer';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { PictureStore, type ArchitectRegion, type ReviewDocument, type ReviewPage } from './dual-pdf-viewer-api';
import {
  decodeSize,
  detailCrop,
  fitWidth,
  maxScale,
  regionRect,
  regionView,
  zoomLimits,
  type Crop,
  type Decoded,
  type Decoder,
  type Side,
} from './dual-pdf-viewer-lib';

/** Used until the pane has reported its size (and in tests, where nothing is laid out). */
const FALLBACK_BOX: Size = { w: 800, h: 560 };
/** How long the view must rest before the sharp close-up of the visible part is decoded. */
const SETTLE_MS = 180;

/** An order from the viewer's keyboard to one pane. `id` changes for every order. */
export interface PaneCommand {
  id: number;
  kind: 'fit' | 'in' | 'out' | 'pan';
  dx?: number;
  dy?: number;
}

/**
 * The item's spot on this page: ringed in the item's colour on the vendor side (neutral once the item
 * is checked), grey and dashed on the architect's.
 */
export interface PaneSpot {
  polygon: Point[];
  label: string;
  emphasis: 'item' | 'checked' | 'reference';
}

/** The page of this file that holds the item: its dot in the page strip stays while turning away. */
export interface PaneMark {
  page: number;
  tone: 'item' | 'neutral';
}

/** The server's code for a page whose picture is not made yet (`app/api/page_pictures.py`). */
const PICTURE_NOT_READY = 'page_picture_not_ready';

/** The architect drawing whose box holds the centre of `focus`, else the first. */
function regionHolding(regions: ArchitectRegion[], focus: Point[] | null): number {
  if (!focus || focus.length === 0) return 0;
  const b = boundsOf(focus);
  const [cx, cy] = [b.x + b.w / 2, b.y + b.h / 2];
  const index = regions.findIndex((entry) => {
    const r = regionRect(entry.bbox);
    return cx >= r.x && cx <= r.x + r.w && cy >= r.y && cy <= r.y + r.h;
  });
  return Math.max(index, 0);
}

const SIDE_WORDS: Record<Side, string> = { vendor: "Vendor's drawing", architect: "Architect's drawing" };

export function DualPane({
  side,
  doc,
  pageNumber,
  count,
  combined,
  spot,
  focus = null,
  mark,
  store,
  decoder,
  command,
  linkedNote,
  onStep,
  onActive,
  onRefresh,
  onPickPage,
}: {
  side: Side;
  doc: ReviewDocument | null;
  pageNumber: number;
  count: number;
  /** The architect side of a combined sheet: the vendor's page, zoomed to its architect drawing. */
  combined: boolean;
  spot: PaneSpot | null;
  /** Combined sheet: where the item is on this page, so the pane opens on the architect drawing holding it. */
  focus?: Point[] | null;
  mark: PaneMark | null;
  store: PictureStore;
  decoder: Decoder;
  command: PaneCommand | null;
  linkedNote?: string | null;
  onStep: (delta: number) => void;
  onActive: () => void;
  onRefresh: () => void;
  onPickPage: (page: number) => void;
}) {
  const words = SIDE_WORDS[side];
  const page = doc?.pages.find((entry) => entry.page_number === pageNumber) ?? null;
  const regions = combined ? (page?.architect_regions ?? []) : [];
  const [regionIndex, setRegionIndex] = useState(() => regionHolding(regions, focus));
  const region: ArchitectRegion | null = regions[Math.min(regionIndex, regions.length - 1)] ?? null;
  const pageKey = doc ? `${doc.document_version_id}:${pageNumber}` : null;
  // A new page starts at the architect drawing holding the item, else its first (derived, not reset in an effect).
  const [regionFor, setRegionFor] = useState(pageKey);
  if (regionFor !== pageKey) {
    setRegionFor(pageKey);
    setRegionIndex(regionHolding(regions, focus));
  }
  const showPicture = !combined || region !== null;
  const title = combined ? `${words} (same sheet)` : words;

  return (
    <section
      aria-label={title}
      data-side={side}
      className="flex min-h-0 min-w-0 flex-1 flex-col"
      onFocusCapture={onActive}
      onPointerDownCapture={onActive}
    >
      <header className={cn('flex h-12 shrink-0 items-center gap-2 border-b border-border px-3', side === 'vendor' ? 'md:pr-16' : 'md:pl-16')}>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-[13px] leading-4 font-semibold">{title}</h3>
          <p className="truncate text-[12px] leading-4 text-muted-foreground" title={doc?.file_name ?? undefined}>
            {doc?.file_name ?? 'No file'}
            {page?.sheet_number && (
              <>
                {', sheet '}
                <span className="num">{page.sheet_number}</span>
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-0.5">
          <Button size="icon-sm" variant="ghost" aria-label="Previous page" disabled={!doc || pageNumber <= 1} onClick={() => onStep(-1)}>
            <ChevronLeft />
          </Button>
          <span data-testid="page-counter" className="num min-w-[4.5rem] text-center text-[13px] tabular-nums" aria-live="polite">
            {doc ? `${pageNumber} / ${count}` : '–'}
          </span>
          <Button size="icon-sm" variant="ghost" aria-label="Next page" disabled={!doc || pageNumber >= count} onClick={() => onStep(1)}>
            <ChevronRight />
          </Button>
        </div>
      </header>

      {combined && regions.length > 0 && region && <RegionBar regions={regions} index={regionIndex} onPick={setRegionIndex} />}
      {linkedNote && <p className="border-b border-border bg-muted/40 px-3 py-1.5 text-[12px] text-muted-foreground">{linkedNote}</p>}

      {!doc ? (
        <PaneNote icon={<ImageOff className="size-5" aria-hidden="true" />} line={side === 'vendor' ? 'No vendor drawing in this set' : "No architect's drawing in this set"} why="Upload it on the set's page to see it here." />
      ) : !showPicture ? (
        <NoRegion pageNumber={pageNumber} doc={doc} onPickPage={onPickPage} />
      ) : (
        <PageView
          key={pageKey}
          words={words}
          doc={doc}
          page={page}
          pageNumber={pageNumber}
          count={count}
          region={region}
          spot={spot}
          store={store}
          decoder={decoder}
          command={command}
          onRefresh={onRefresh}
        />
      )}

      {!combined && doc && <PageStrip side={side} doc={doc} count={count} current={pageNumber} mark={mark} onPick={onPickPage} />}
    </section>
  );
}

/** Which architect drawing of a combined sheet is shown, and whether its role is only a suggestion. */
function RegionBar({ regions, index, onPick }: { regions: ArchitectRegion[]; index: number; onPick: (index: number) => void }) {
  const region = regions[Math.min(index, regions.length - 1)];
  return (
    <div className="flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-1.5 text-[12px]">
      {region.role_confirmed ? (
        <span className="text-muted-foreground">The architect's drawing on this sheet</span>
      ) : (
        <span className="flex items-center gap-1.5 text-foreground">
          <span className="inline-block size-2.5 rounded-[2px] border border-dashed border-foreground/70" aria-hidden="true" />
          <span>
            <strong className="font-medium">Suggestion, not confirmed.</strong>{' '}
            {region.heading ? <>The sheet heading reads “{region.heading}”.</> : 'Only the sheet layout suggests it.'}
          </span>
        </span>
      )}
      {regions.length > 1 && (
        <span className="ml-auto flex items-center gap-1" role="group" aria-label="Architect drawings on this sheet">
          {regions.map((entry, i) => (
            <button
              key={entry.view_id}
              type="button"
              aria-pressed={i === index}
              aria-label={`Drawing ${i + 1} of ${regions.length}${entry.role_confirmed ? '' : ', a suggestion'}`}
              onClick={() => onPick(i)}
              className={cn(
                'num h-6 min-w-6 rounded-md border px-1.5 text-[12px] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50',
                i === index ? 'border-foreground bg-foreground text-background' : 'border-border hover:border-foreground/40',
                !entry.role_confirmed && 'border-dashed',
              )}
            >
              {i + 1}
            </button>
          ))}
        </span>
      )}
    </div>
  );
}

function NoRegion({ pageNumber, doc, onPickPage }: { pageNumber: number; doc: ReviewDocument; onPickPage: (page: number) => void }) {
  const withOne = doc.pages.filter((entry) => entry.architect_regions.length > 0).map((entry) => entry.page_number);
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 bg-muted/40 p-6 text-center" role="status">
      <ImageOff className="size-5 text-muted-foreground" aria-hidden="true" />
      <p className="text-sm font-medium">No architect's drawing is marked on page <span className="num">{pageNumber}</span>.</p>
      {withOne.length > 0 ? (
        <div className="flex flex-wrap items-center justify-center gap-1.5 text-[13px] text-muted-foreground">
          <span>Pages that have one:</span>
          {withOne.map((n) => (
            <Button key={n} size="xs" variant="outline" className="num" aria-label={`Go to page ${n}`} onClick={() => onPickPage(n)}>
              {n}
            </Button>
          ))}
        </div>
      ) : (
        <p className="max-w-sm text-[13px] text-muted-foreground">No page of this set has an architect's drawing marked yet.</p>
      )}
    </div>
  );
}

type PictureState =
  | { status: 'loading' }
  | { status: 'ready'; blob: Blob }
  | { status: 'not-ready' }
  | { status: 'error'; error: string };

/**
 * One page: fetched once, decoded at the size it is drawn (never above the stored size), with a sharp
 * close-up of only the visible part decoded once zoomed in. Everything is freed when the page leaves.
 * Mounted per page (`key`), so a new page always starts clean and fitted.
 */
function PageView({
  words,
  doc,
  page,
  pageNumber,
  count,
  region,
  spot,
  store,
  decoder,
  command,
  onRefresh,
}: {
  words: string;
  doc: ReviewDocument;
  page: ReviewPage | null;
  pageNumber: number;
  count: number;
  region: ArchitectRegion | null;
  spot: PaneSpot | null;
  store: PictureStore;
  decoder: Decoder;
  command: PaneCommand | null;
  onRefresh: () => void;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<Size>(FALLBACK_BOX);
  /** The reviewer's own view; null while the page is fitted (it then follows the pane's size). */
  const [moved, setMoved] = useState<View | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [picture, setPicture] = useState<PictureState>({ status: 'loading' });
  const [base, setBase] = useState<Decoded | null>(null);
  const [detail, setDetail] = useState<{ crop: Crop; decoded: Decoded } | null>(null);
  const detailRef = useRef<{ crop: Crop; decoded: Decoded } | null>(null);
  const dpr = typeof window === 'undefined' ? 1 : window.devicePixelRatio || 1;
  const max = maxScale(dpr);
  const stored = page?.picture_status === 'ready' && page.picture ? page.picture : null;
  const natural: Size | null = stored ? { w: stored.width_px, h: stored.height_px } : null;
  const limits = natural ? zoomLimits(natural, box, max) : null;
  const dvid = doc.document_version_id;

  // The pane's size, for fitting.
  useEffect(() => {
    const element = viewport.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setBox((now) => (now.w === width && now.h === height ? now : { w: width, h: height }));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Fitted: to the width, or (a combined sheet's architect side) to the architect's drawing.
  const regionId = region?.view_id ?? null;
  const fitted = useMemo<View | null>(() => {
    if (!natural) return null;
    if (region) return regionView(regionRect(region.bbox), natural, box, zoomLimits(natural, box, max));
    return fitWidth(natural, box, max);
  }, [natural?.w, natural?.h, box, max, regionId]); // eslint-disable-line react-hooks/exhaustive-deps
  const view = moved ?? fitted;

  // Another architect drawing starts fitted to it (derived during render, not reset in an effect).
  const [fitFor, setFitFor] = useState(regionId);
  if (fitFor !== regionId) {
    setFitFor(regionId);
    setMoved(null);
  }

  const move = (change: (now: View) => View) => setMoved((now) => (now ?? fitted ? change((now ?? fitted)!) : now));
  const zoomBy = (factor: number) => limits && move((now) => zoomAt(now, factor, { x: box.w / 2, y: box.h / 2 }, limits));
  const fit = () => setMoved(null);

  // The keyboard's orders, each applied once.
  const [seenCommand, setSeenCommand] = useState(command?.id ?? null);
  if (command && command.id !== seenCommand) {
    setSeenCommand(command.id);
    if (command.kind === 'fit') setMoved(null);
    else if (view && limits) {
      setMoved(command.kind === 'pan' ? { ...view, x: view.x + (command.dx ?? 0), y: view.y + (command.dy ?? 0) } : zoomAt(view, command.kind === 'in' ? 1.25 : 0.8, { x: box.w / 2, y: box.h / 2 }, limits));
    }
  }

  // Fetch the stored picture (shared with the other pane), and let it go when the page leaves.
  useEffect(() => {
    if (!stored) return;
    let live = true;
    store.acquire(dvid, pageNumber).then(
      (blob) => live && setPicture({ status: 'ready', blob }),
      (error: unknown) => {
        if (!live) return;
        if (error instanceof ApiError && error.status === 404 && error.code === PICTURE_NOT_READY) setPicture({ status: 'not-ready' });
        else setPicture({ status: 'error', error: error instanceof Error ? error.message : String(error) });
      },
    );
    return () => {
      live = false;
      store.release(dvid, pageNumber);
    };
  }, [store, dvid, pageNumber, attempt, stored?.url]); // eslint-disable-line react-hooks/exhaustive-deps

  // The whole page, decoded once at the fitted size (sharp enough at fit; the close-up does the rest).
  const fitScale = natural ? fitWidth(natural, box, max).scale : 0;
  const baseSize = natural ? decodeSize(natural, fitScale, dpr) : null;
  const blob = picture.status === 'ready' ? picture.blob : null;
  useEffect(() => {
    if (!blob || !baseSize) return;
    let live = true;
    let made: Decoded | null = null;
    decoder(blob, null, baseSize).then(
      (decoded) => {
        if (!live) return decoded.close();
        made = decoded;
        setBase(decoded);
      },
      (error: unknown) => live && setPicture({ status: 'error', error: error instanceof Error ? error.message : String(error) }),
    );
    return () => {
      live = false;
      made?.close();
    };
  }, [blob, baseSize?.w, baseSize?.h, decoder]); // eslint-disable-line react-hooks/exhaustive-deps

  // Zoomed in: once the view rests, decode only the visible part at the screen's size.
  useEffect(() => {
    if (!blob || !natural || !view || !baseSize) return;
    // A browser that cannot decode part of a picture would stretch the whole page into the crop (#1362).
    if (decoder.canDecodeParts && !decoder.canDecodeParts()) return;
    let live = true;
    const timer = window.setTimeout(() => {
      const crop = detailCrop(natural, view, box, dpr, baseSize);
      const now = detailRef.current;
      if (!crop) {
        now?.decoded.close();
        detailRef.current = null;
        setDetail(null);
        return;
      }
      if (now && now.crop.x <= crop.x && now.crop.y <= crop.y && now.crop.x + now.crop.w >= crop.x + crop.w && now.crop.y + now.crop.h >= crop.y + crop.h && now.crop.size.w / now.crop.w >= crop.size.w / crop.w) return;
      decoder(blob, { x: crop.x, y: crop.y, w: crop.w, h: crop.h }, crop.size).then(
        (decoded) => {
          if (!live) return decoded.close();
          detailRef.current?.decoded.close();
          detailRef.current = { crop, decoded };
          setDetail(detailRef.current);
        },
        // The close-up only sharpens; without it the whole-page picture still shows.
        () => undefined,
      );
    }, SETTLE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [blob, view, box, decoder]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => {
    detailRef.current?.decoded.close();
    detailRef.current = null;
  }, []);

  // Wheel zoom at the cursor; a native listener, because React's is passive.
  const onWheel = useEffectEvent((event: WheelEvent) => {
    const element = viewport.current;
    if (!element || !limits) return;
    event.preventDefault();
    const rect = element.getBoundingClientRect();
    const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0015));
    move((now) => zoomAt(now, factor, { x: event.clientX - rect.left, y: event.clientY - rect.top }, limits));
  });
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const listener = (event: WheelEvent) => onWheel(event);
    element.addEventListener('wheel', listener, { passive: false });
    return () => element.removeEventListener('wheel', listener);
  }, []);

  const drag = useRef<{ id: number; x: number; y: number } | null>(null);
  const retry = () => {
    setPicture({ status: 'loading' });
    setBase(null);
    setAttempt((n) => n + 1);
  };

  const ready = picture.status === 'ready' && base && natural && view;
  const ring = spot ? padded(boundsOf(spot.polygon), 0.012) : null;
  const zoomWord = view ? `${Math.max(1, Math.round(view.scale * dpr * 100))}%` : '–';

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={viewport}
        role="group"
        tabIndex={0}
        aria-label={`${words}, page ${pageNumber} of ${count}. Drag or use the arrow keys with Shift to move; + and − zoom; W fits the width.`}
        data-slot="dual-viewport"
        className="relative min-h-0 flex-1 cursor-grab touch-none overflow-hidden bg-muted/60 outline-none select-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:ring-inset active:cursor-grabbing"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerMove={(event) => {
          const start = drag.current;
          if (!start || start.id !== event.pointerId) return;
          const dx = event.clientX - start.x;
          const dy = event.clientY - start.y;
          drag.current = { ...start, x: event.clientX, y: event.clientY };
          move((now) => ({ ...now, x: now.x + dx, y: now.y + dy }));
        }}
        onPointerUp={() => (drag.current = null)}
        onPointerCancel={() => (drag.current = null)}
      >
        {ready && (
          <div
            role="img"
            aria-label={`${words}, page ${pageNumber}${spot ? `, ${spot.label} ringed` : ''}`}
            data-theme="light"
            className="absolute top-0 left-0 bg-white shadow-[0_1px_3px_rgb(0_0_0/0.12)]"
            style={{ width: natural.w * view.scale, height: natural.h * view.scale, transform: `translate(${view.x}px, ${view.y}px)` }}
          >
            <Layer decoded={base} className="absolute inset-0 size-full" />
            {detail && (
              <Layer
                decoded={detail.decoded}
                className="absolute"
                style={{ left: `${(detail.crop.x / natural.w) * 100}%`, top: `${(detail.crop.y / natural.h) * 100}%`, width: `${(detail.crop.w / natural.w) * 100}%`, height: `${(detail.crop.h / natural.h) * 100}%` }}
              />
            )}
            <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full overflow-visible" aria-hidden="true">
              {page?.architect_regions.map((entry) => {
                const shown = region?.view_id === entry.view_id;
                if (!region) return null;
                return (
                  <polygon
                    key={entry.view_id}
                    data-testid={shown ? 'architect-region' : 'architect-region-other'}
                    data-confirmed={String(entry.role_confirmed)}
                    points={svgPoints(entry.polygon.map(([x, y]) => [Number(x), Number(y)] as Point))}
                    vectorEffect="non-scaling-stroke"
                    strokeWidth={shown ? 2 : 1}
                    className={cn('fill-none', shown ? 'stroke-neutral-800' : 'stroke-neutral-400', !entry.role_confirmed && '[stroke-dasharray:7_5]')}
                  />
                );
              })}
              {spot && ring && (
                <g data-testid="item-spot" data-emphasis={spot.emphasis} aria-label={`${spot.label}, ringed`}>
                  <rect x={ring.x} y={ring.y} width={ring.w} height={ring.h} vectorEffect="non-scaling-stroke" strokeWidth={7} className="fill-none stroke-white/90" />
                  <rect
                    x={ring.x}
                    y={ring.y}
                    width={ring.w}
                    height={ring.h}
                    vectorEffect="non-scaling-stroke"
                    strokeWidth={spot.emphasis === 'reference' ? 1.75 : 2.5}
                    className={RING[spot.emphasis]}
                  />
                  <polygon points={svgPoints(spot.polygon)} vectorEffect="non-scaling-stroke" strokeWidth={1} className={spot.emphasis === 'item' ? 'fill-none stroke-outcome-review-fg/70' : 'fill-none stroke-neutral-700/60'} />
                </g>
              )}
            </svg>
            {spot && ring && (
              <span
                className={cn(
                  'pointer-events-none absolute max-w-64 truncate rounded-md px-1.5 py-0.5 text-[12px] font-medium whitespace-nowrap shadow-sm',
                  spot.emphasis === 'item' ? 'bg-outcome-review-fg text-white' : spot.emphasis === 'checked' ? 'bg-neutral-700 text-white' : 'border border-neutral-300 bg-white text-neutral-800',
                )}
                style={{ left: `${ring.x * 100}%`, top: `${ring.y * 100}%`, transform: 'translate(0, calc(-100% - 4px))' }}
                aria-hidden="true"
              >
                {spot.label}
              </span>
            )}
          </div>
        )}

        {page === null && (
          <PaneNote
            icon={<ImageOff className="size-5" aria-hidden="true" />}
            line={`Page ${pageNumber} is still being read`}
            why="Its picture comes once the page is read."
            action={{ label: 'Check again', onClick: onRefresh }}
          />
        )}
        {page?.picture_status === 'not_ready' && (
          <PaneNote
            icon={<ImageOff className="size-5" aria-hidden="true" />}
            line="Picture not made yet"
            why="Page pictures are made in the background after upload. This viewer never starts that."
            action={{ label: 'Check again', onClick: onRefresh }}
          />
        )}
        {page?.picture_status === 'render_failed' && (
          <PaneNote icon={<ImageOff className="size-5" aria-hidden="true" />} line={`Page ${pageNumber} could not be read`} why="No picture will be made for it. Open the PDF file itself to look at this page." />
        )}
        {stored && picture.status === 'not-ready' && (
          <PaneNote icon={<ImageOff className="size-5" aria-hidden="true" />} line="Picture not made yet" why="The server has no picture for this page yet." action={{ label: 'Check again', onClick: retry }} />
        )}
        {stored && picture.status === 'error' && (
          <PaneNote
            role="alert"
            icon={<ImageOff className="size-5" aria-hidden="true" />}
            line={`The picture of page ${pageNumber} could not be loaded`}
            why={picture.error}
            action={{ label: 'Retry', onClick: retry }}
          />
        )}
        {stored && (picture.status === 'loading' || (picture.status === 'ready' && !base)) && (
          <div className="absolute inset-0 flex items-start justify-center p-4" role="status">
            <Skeleton className="w-full" style={{ aspectRatio: `${stored.width_px} / ${stored.height_px}` }} />
            <span className="sr-only">Loading page {pageNumber}…</span>
          </div>
        )}
      </div>

      {stored && (
        <div className="absolute right-2 bottom-2 flex items-center gap-0.5 rounded-lg border border-border bg-background/95 p-0.5 shadow-sm">
          {spot && (
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={`Zoom to ${spot.label}`}
              title="Zoom to the spot"
              disabled={!natural || !limits}
              onClick={() => {
                if (!natural || !limits || !ring) return;
                setMoved(regionView(ring, natural, box, { min: Math.max(limits.min, fitted?.scale ?? limits.min), max: limits.max }, 0.85));
              }}
            >
              <Crosshair />
            </Button>
          )}
          <Button size="icon-sm" variant="ghost" aria-label="Zoom out" onClick={() => zoomBy(0.8)}>
            <ZoomOut />
          </Button>
          <span data-testid="zoom-level" className="num w-11 text-center text-[12px] text-muted-foreground tabular-nums" title="Share of the stored picture's full detail">
            {zoomWord}
          </span>
          <Button size="icon-sm" variant="ghost" aria-label="Zoom in" onClick={() => zoomBy(1.25)}>
            <ZoomIn />
          </Button>
          <Button size="icon-sm" variant="ghost" aria-label={region ? "Fit the architect's drawing" : 'Fit width'} onClick={fit}>
            <MoveHorizontal />
          </Button>
        </div>
      )}
    </div>
  );
}

const RING: Record<PaneSpot['emphasis'], string> = {
  item: 'fill-outcome-review/10 stroke-outcome-review-fg',
  // Already checked: nothing to act on, so no outcome colour.
  checked: 'fill-neutral-900/[0.04] stroke-neutral-700',
  reference: 'fill-neutral-900/[0.06] stroke-neutral-700 [stroke-dasharray:6_4]',
};

function padded(rect: { x: number; y: number; w: number; h: number }, pad: number) {
  return { x: rect.x - pad, y: rect.y - pad, w: rect.w + 2 * pad, h: rect.h + 2 * pad };
}

/** A decoded bitmap on a canvas, or (without `createImageBitmap`) the picture's object URL. */
function Layer({ decoded, className, style }: { decoded: Decoded; className?: string; style?: React.CSSProperties }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (typeof decoded.source === 'string') return;
    const element = canvas.current;
    const context = element?.getContext('2d');
    if (!element || !context) return;
    context.drawImage(decoded.source, 0, 0);
  }, [decoded]);
  if (typeof decoded.source === 'string') return <img src={decoded.source} alt="" draggable={false} className={cn('block', className)} style={style} />;
  return <canvas ref={canvas} width={decoded.width} height={decoded.height} aria-hidden="true" className={cn('block', className)} style={style} />;
}

function PaneNote({ icon, line, why, action, role = 'status' }: { icon: React.ReactNode; line: string; why: string; action?: { label: string; onClick: () => void }; role?: 'status' | 'alert' }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-6 text-center" role={role}>
      <span className="text-muted-foreground">{icon}</span>
      <p className="text-sm font-medium">{line}</p>
      <p className="max-w-sm text-[13px] text-muted-foreground">{why}</p>
      {action && (
        <Button size="sm" variant="outline" className="mt-1" onClick={action.onClick}>
          <RefreshCw /> {action.label}
        </Button>
      )}
    </div>
  );
}

/**
 * Every page of one file as a numbered strip. No pictures: a thumbnail of a 17-megapixel page would
 * cost a full download and decode, so the strip shows the page and sheet numbers and each page's state.
 */
function PageStrip({ side, doc, count, current, mark, onPick }: { side: Side; doc: ReviewDocument; count: number; current: number; mark: PaneMark | null; onPick: (page: number) => void }) {
  const spotPage = mark?.page ?? null;
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    list.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [current]);
  const byNumber = new Map(doc.pages.map((entry) => [entry.page_number, entry]));
  return (
    <nav aria-label={side === 'vendor' ? 'Vendor pages' : 'Architect pages'} className="shrink-0 border-t border-border">
      <ol ref={list} className="flex gap-1 overflow-x-auto px-2 py-1.5">
        {Array.from({ length: count }, (_, i) => i + 1).map((n) => {
          const entry = byNumber.get(n) ?? null;
          const state = entry === null ? 'still being read' : entry.picture_status === 'not_ready' ? 'picture not made yet' : entry.picture_status === 'render_failed' ? 'could not be read' : null;
          const words = [`Page ${n}`, entry?.sheet_number ? `sheet ${entry.sheet_number}` : null, state, n === spotPage ? 'has the item' : null, entry && entry.architect_regions.length > 0 ? "has an architect's drawing" : null]
            .filter(Boolean)
            .join(', ');
          return (
            <li key={n}>
              <button
                type="button"
                aria-label={words}
                aria-current={n === current ? 'page' : undefined}
                title={words}
                onClick={() => onPick(n)}
                className={cn(
                  'relative flex h-10 min-w-11 flex-col items-center justify-center rounded-md border px-1.5 leading-none outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50',
                  n === current ? 'border-foreground bg-foreground text-background' : 'border-transparent text-foreground hover:border-border hover:bg-muted',
                  state && n !== current && 'text-muted-foreground',
                )}
              >
                <span className="num text-[12px] font-medium">{n}</span>
                {entry?.sheet_number && <span className={cn('num mt-0.5 max-w-16 truncate text-[12px]', n === current ? 'text-background/70' : 'text-muted-foreground')}>{entry.sheet_number}</span>}
                {n === spotPage && <span className={cn('absolute top-0.5 right-0.5 size-1.5 rounded-full', mark?.tone === 'item' ? 'bg-outcome-review' : 'bg-neutral-500')} aria-hidden="true" />}
                {state && <span className="absolute inset-x-1.5 bottom-0.5 border-b border-dashed border-current opacity-60" aria-hidden="true" />}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
