import { useEffect, useEffectEvent, useState } from 'react';
import { Crosshair, Maximize, X, ZoomIn, ZoomOut } from 'lucide-react';

import { getFindingChain, type CountertopResult } from '@/api/client';
import { useAsync } from '@/api/useAsync';
import { useIsMobile } from '@/hooks/use-mobile';
import { cn } from '@/lib/utils';
import {
  boundsOf,
  fitView,
  focusView,
  noCropsReason,
  pagesOf,
  readingsOf,
  samePage,
  scaleLimits,
  zoomAt,
  type Mark,
  type Reading,
  type Size,
  type StripPage,
  type View,
  type ViewerTarget,
} from '@/lib/drawing-viewer';
import { CountertopStrip } from '@/components/results/CountertopStrip';
import { SplitPageNote } from '@/components/results/split-page-note';
import { DrawnLengthNote } from '@/components/results/drawn-length-note';
import { isSplitPage } from '@/lib/countertop-results';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent } from '@/components/ui/dialog';
import { Separator } from '@/components/ui/separator';
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { pageKey, useBlob, useBlobCache } from './blob-cache';
import { EvidenceCrops, NoCrops } from './evidence-crops';
import { PageCanvas, TonePill } from './page-canvas';
import { PageStrip } from './page-strip';

/** Used until the viewport has reported its size (and in tests, where nothing is laid out). */
const FALLBACK_BOX: Size = { w: 800, h: 560 };

/**
 * "Show on drawing" (#1045): a side sheet on desktop, a full-screen dialog on a phone. `opening`
 * changes each time it is opened, so every opening starts fitted to its own result.
 */
export function DrawingViewerSheet({
  target,
  opening,
  rows,
  projectId,
  packageId,
  onTargetChange,
  onClose,
}: {
  target: ViewerTarget | null;
  opening: number;
  rows: readonly CountertopResult[];
  projectId: string;
  packageId: string;
  onTargetChange: (target: ViewerTarget) => void;
  onClose: () => void;
}) {
  const isMobile = useIsMobile();
  const open = target !== null;
  const content = target && (
    <DrawingViewer key={opening} target={target} rows={rows} projectId={projectId} packageId={packageId} onTargetChange={onTargetChange} />
  );
  if (isMobile) {
    return (
      <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
        <DialogContent
          showCloseButton={false}
          className="top-0 left-0 flex h-dvh max-h-none w-screen max-w-none translate-x-0 translate-y-0 flex-col gap-0 rounded-none border-0 p-0 sm:max-w-none"
        >
          {content}
        </DialogContent>
      </Dialog>
    );
  }
  return (
    <Sheet open={open} onOpenChange={(next) => !next && onClose()}>
      <SheetContent side="right" showCloseButton={false} className="w-full gap-0 p-0 sm:max-w-[min(78rem,96vw)]">
        {content}
      </SheetContent>
    </Sheet>
  );
}

export function DrawingViewer({
  target,
  rows,
  projectId,
  packageId,
  onTargetChange,
  embedded = false,
  extraMarks = [],
  activeMark = null,
}: {
  target: ViewerTarget;
  rows: readonly CountertopResult[];
  projectId: string;
  packageId: string;
  onTargetChange: (target: ViewerTarget) => void;
  /**
   * Inside another screen (the "Needs you" queue, #1050): only the toolbar and the drawing. That
   * screen owns the title, the facts and moving between items, so there is no header, page strip,
   * evidence panel or arrow-key paging, and other countertops are not offered.
   */
  embedded?: boolean;
  /** More grey marks beside the target's own (the spans offered for pairing, #1085). */
  extraMarks?: readonly Mark[];
  /** The mark to stand out. */
  activeMark?: string | null;
}) {
  const cache = useBlobCache();
  const pictureKey = target.page !== null ? pageKey(projectId, packageId, target.page, target.documentVersionId ?? '') : null;
  const [picture, retryPicture] = useBlob(cache, pictureKey);
  const [box, setBox] = useState<Size | null>(null);
  const [frame, setFrame] = useState<{ key: string; natural: Size; view: View } | null>(null);
  const [activeReading, setActiveReading] = useState<string | null>(null);

  // Derived, not reset in an effect: another page's size and zoom are simply not this page's.
  const placed = frame && frame.key === pictureKey ? frame : null;
  const natural = placed?.natural ?? null;
  const view = placed?.view ?? null;
  const area = box ?? FALLBACK_BOX;
  const limits = natural ? scaleLimits(natural, area) : null;
  const fitScale = natural ? fitView(natural, area).scale : null;

  const setView: React.Dispatch<React.SetStateAction<View | null>> = (update) =>
    setFrame((current) => {
      if (!current || current.key !== pictureKey) return current;
      const next = typeof update === 'function' ? update(current.view) : update;
      return next ? { ...current, view: next } : current;
    });

  const chain = useAsync(
    () => (target.findingId ? getFindingChain(projectId, packageId, target.findingId) : Promise.resolve(null)),
    [projectId, packageId, target.findingId],
  );
  const chainData = chain.status === 'ready' ? chain.data : null;
  const readings = chainData ? readingsOf(chainData, target.row) : [];
  const onThisPage = readings.filter((r) => r.page === target.page && (target.documentVersionId === null || r.documentVersionId === target.documentVersionId));

  // Grey marks (#1085) are drawn only on the page they were stored on, and only from a stored outline.
  const marks = [...target.marks, ...extraMarks];
  const marksHere = marks.filter((mark) => mark.outline !== null && samePage(mark, target));

  const pages = pagesOf(rows, target);
  const currentPage = target.page !== null && target.documentVersionId !== null ? `${target.documentVersionId}:${target.page}` : null;
  const others = pages.find((p) => `${p.documentVersionId}:${p.page}` === currentPage)?.targets.filter((t) => t.key !== target.key) ?? [];

  function initialView(size: Size, outline: ViewerTarget['outline']): View {
    return outline ? focusView(boundsOf(outline), size, area, scaleLimits(size, area), 0.5) : fitView(size, area);
  }

  function onLoaded(size: Size) {
    if (pictureKey === null) return;
    setFrame({ key: pictureKey, natural: size, view: initialView(size, target.outline) });
  }

  function zoomBy(factor: number) {
    if (limits) setView((v) => (v ? zoomAt(v, factor, { x: area.w / 2, y: area.h / 2 }, limits) : v));
  }

  function fit() {
    if (natural) setView(fitView(natural, area));
  }

  function findOutline() {
    if (natural && target.outline) setView(initialView(natural, target.outline));
  }

  function select(next: ViewerTarget) {
    setActiveReading(null);
    onTargetChange(next);
    // Same page: move to the new outline now. Another page: its picture's load places it.
    if (samePage(next, target) && natural) setView(initialView(natural, next.outline));
  }

  function pickPage(page: StripPage) {
    if (page.targets[0]) select(page.targets[0]);
  }

  function step(by: number) {
    const index = pages.findIndex((p) => `${p.documentVersionId}:${p.page}` === currentPage);
    const next = pages[index + by];
    if (index >= 0 && next) pickPage(next);
  }

  function pickReading(reading: Reading) {
    setActiveReading(reading.observationId);
    if (natural && limits && reading.spot && reading.page === target.page) setView(focusView(boundsOf(reading.spot), natural, area, limits, 0.3));
  }

  // On the window, not the viewer's own element: a click on the drawing leaves focus on the sheet
  // itself, above this component, and the keys must still work. The viewer is modal while open.
  const onKeyDown = useEffectEvent((event: KeyboardEvent) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target instanceof Element && event.target.closest('input, textarea, select, [contenteditable="true"], [role="slider"]')) return;
    const actions: Record<string, () => void> = {
      '+': () => zoomBy(1.25),
      '=': () => zoomBy(1.25),
      '-': () => zoomBy(0.8),
      _: () => zoomBy(0.8),
      '0': fit,
      f: findOutline,
      F: findOutline,
      ...(embedded ? {} : { ArrowLeft: () => step(-1), ArrowRight: () => step(1) }),
    };
    const action = actions[event.key];
    if (!action) return;
    event.preventDefault();
    action();
  });
  useEffect(() => {
    const listener = (event: KeyboardEvent) => onKeyDown(event);
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
  }, []);

  const zoomPercent = view && fitScale ? Math.round((view.scale / fitScale) * 100) : null;

  return (
    <TooltipProvider delayDuration={250}>
      <div data-tw data-slot="drawing-viewer" className="flex h-full min-h-0 flex-col font-sans text-foreground">
        {!embedded && (
        <header className="flex items-start gap-3 border-b px-4 py-3">
          <div className="min-w-0 flex-1">
            <SheetTitle className="truncate text-base font-semibold">{target.label}</SheetTitle>
            <SheetDescription className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground" asChild>
              <div>
                <TonePill target={target} />
                {target.needsYou && target.glyph !== 'REVIEW_REQUIRED' && (
                  <span className="inline-flex items-center gap-1 font-medium text-outcome-review-fg">
                    <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} /> Needs you
                  </span>
                )}
                <span className="num">{target.page !== null ? `Page ${target.page}` : 'No page'}</span>
                {target.row?.hold && (
                  <span className="max-w-full truncate rounded-full border border-dashed px-2 py-0.5" title={target.row.hold.reason}>
                    {isSplitPage(target.row) ? 'No line chosen' : 'Held'}: {target.row.hold.reason}
                  </span>
                )}
              </div>
            </SheetDescription>
          </div>
          <SheetClose asChild>
            <Button variant="ghost" size="icon-sm" aria-label="Close drawing">
              <X />
            </Button>
          </SheetClose>
        </header>
        )}

        {/* Phone and tablet: one scrolling column, the drawing first at a fixed height. Desktop: drawing | evidence. */}
        <div className={embedded ? 'flex min-h-0 flex-1 flex-col' : 'min-h-0 flex-1 overflow-y-auto lg:grid lg:grid-cols-[minmax(0,1fr)_21rem] lg:overflow-hidden'}>
          <div className={embedded ? 'flex min-h-0 flex-1 flex-col' : 'flex h-[62dvh] min-h-72 flex-col lg:h-full lg:min-h-0'}>
            <div role="toolbar" aria-label="Drawing controls" className="flex shrink-0 items-center gap-1 border-b px-2 py-1">
              <Button size="icon-sm" variant="ghost" aria-label="Zoom out" onClick={() => zoomBy(0.8)} disabled={!view}>
                <ZoomOut />
              </Button>
              <span data-slot="zoom-level" className="num w-12 text-center text-xs" aria-live="polite">
                {zoomPercent !== null ? `${zoomPercent}%` : '—'}
              </span>
              <Button size="icon-sm" variant="ghost" aria-label="Zoom in" onClick={() => zoomBy(1.25)} disabled={!view}>
                <ZoomIn />
              </Button>
              <Separator orientation="vertical" className="mx-1 data-[orientation=vertical]:h-4" />
              <Button size="sm" variant="ghost" onClick={fit} disabled={!view}>
                <Maximize /> Fit
              </Button>
              <Button size="sm" variant="ghost" onClick={findOutline} disabled={!view || !target.outline}>
                <Crosshair /> Find outline
              </Button>
              <Tooltip>
                <TooltipTrigger asChild>
                  <button type="button" className="ml-auto inline-flex size-6 items-center justify-center rounded-full border text-xs text-muted-foreground" aria-label="How to move around">
                    ?
                  </button>
                </TooltipTrigger>
                <TooltipContent className="max-w-72">
                  Drag to move. Wheel or pinch to zoom. Keys: + and − zoom, 0 fits, F finds the outline{embedded ? '' : ', ← and → change page'}. This is the vendor&apos;s drawing layer; markup already baked into it can still show. The outline only marks where this result was read.
                </TooltipContent>
              </Tooltip>
            </div>
            <PageCanvas
              picture={picture}
              onRetry={retryPicture}
              target={target}
              others={embedded ? [] : others}
              marks={marksHere}
              activeMark={activeMark}
              readings={onThisPage}
              activeReading={activeReading}
              natural={natural}
              view={view}
              limits={limits}
              onView={setView}
              onLoaded={onLoaded}
              onBox={setBox}
              onSelect={select}
            />
            {!embedded && <PageStrip pages={pages} current={currentPage} cache={cache} projectId={projectId} packageId={packageId} onPick={pickPage} />}
          </div>

          {!embedded && (
          <aside aria-label="Evidence" data-slot="drawing-evidence" className="flex flex-col gap-5 border-t p-4 lg:min-h-0 lg:overflow-y-auto lg:border-t-0 lg:border-l">
            {/* A split page (#1093) has no line, so nothing is outlined and there is no picture. */}
            {target.row?.hold && isSplitPage(target.row) ? (
              <SplitPageNote hold={target.row.hold} />
            ) : target.row && (
              <section aria-label="Countertop picture" className="flex flex-col gap-2">
                <CountertopStrip row={target.row} showHoldReason={false} />
                {/* A held row's reason, said here unless "No readings used" below already says it. */}
                {target.row.hold && chain.status !== 'loading' && !(readings.length === 0 && chainData?.trace.kind === 'abstention') && (
                  <p data-slot="drawing-hold-reason" className="text-xs text-muted-foreground">{target.row.hold.reason}</p>
                )}
                <DrawnLengthNote row={target.row} />
              </section>
            )}
            <section aria-labelledby="drawing-readings" className="flex flex-col gap-2">
              <h3 id="drawing-readings" className="flex items-baseline gap-2 text-sm font-medium">
                Read from the drawing
                {readings.length > 0 && <span className="num text-xs text-muted-foreground">{readings.length}</span>}
              </h3>
              {target.findingId && chain.status === 'loading' ? (
                <div className="grid grid-cols-2 gap-2" role="status" aria-label="Loading the readings">
                  <Skeleton className="h-24" />
                  <Skeleton className="h-24" />
                </div>
              ) : chain.status === 'error' ? (
                <p className="text-sm text-muted-foreground" role="alert">
                  The readings could not be loaded: {chain.error.message}
                </p>
              ) : readings.length > 0 ? (
                <EvidenceCrops readings={readings} cache={cache} projectId={projectId} packageId={packageId} active={activeReading} onPick={pickReading} />
              ) : (
                <NoCrops {...noCropsReason(chainData, target.row ?? (target.findingId ? null : { hold: null, finding_id: null }))} />
              )}
            </section>
            {target.marks.length > 0 && (
              <section aria-labelledby="drawing-architect" className="flex flex-col gap-2">
                <h3 id="drawing-architect" className="text-sm font-medium">Matches the architect</h3>
                <MarkNotes marks={target.marks} at={target} />
              </section>
            )}
            {others.length > 0 && (
              <section aria-labelledby="drawing-others" className="flex flex-col gap-2">
                <h3 id="drawing-others" className="text-sm font-medium">
                  On this page
                </h3>
                <ul className="flex flex-col gap-1">
                  {others.map((other) => (
                    <li key={other.key}>
                      <button
                        type="button"
                        data-other-button={other.key}
                        onClick={() => select(other)}
                        className="flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left text-sm outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50"
                      >
                        <TonePill target={other} />
                        <span className="min-w-0 flex-1 truncate">{other.label}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </aside>
          )}
        </div>
      </div>
    </TooltipProvider>
  );
}

/**
 * Where each grey mark is (#1085): outlined on this page, on another page, or not outlined because
 * its position was not stored. Said in words, so a missing outline is never mistaken for a match.
 */
export function MarkNotes({ marks, at }: { marks: readonly Mark[]; at: Pick<ViewerTarget, 'page' | 'documentVersionId'> }) {
  return (
    <ul className="flex flex-col gap-1 text-xs" data-slot="mark-notes">
      {marks.map((mark) => (
        <li key={mark.key} className="flex items-start gap-1.5">
          <span aria-hidden="true" className={cn('mt-1 inline-block size-2.5 shrink-0 rounded-sm border', mark.outline ? 'border-neutral-500 bg-neutral-500/20' : 'border-dashed border-muted-foreground')} />
          <span>
            {mark.label}:{' '}
            <span className="text-muted-foreground">
              {mark.outline === null
                ? 'not outlined (its position on the drawing is not stored)'
                : samePage(mark, at)
                  ? 'outlined in grey'
                  : mark.page === at.page
                    ? `on page ${mark.page} of the other drawing`
                    : `on page ${mark.page}, not this page`}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}
