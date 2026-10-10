import { useState } from 'react';
import { Crosshair, Maximize } from 'lucide-react';

import { cn } from '@/lib/utils';
import { boundsOf, fitView, focusView, scaleLimits, type Mark, type Point, type Size, type View, type ViewerTarget } from '@/lib/drawing-viewer';
import { Button } from '@/components/ui/button';
import { TooltipProvider } from '@/components/ui/tooltip';
import { pageKey, useBlob, useBlobCache, type BlobCache } from './blob-cache';
import { PageCanvas } from './page-canvas';

/** Used until the pane has reported its size (and in tests, where nothing is laid out). */
const FALLBACK_BOX: Size = { w: 640, h: 420 };

/**
 * One drawing page in its own pane, framing one stored region (#1168): the architect's view beside the
 * vendor's countertop, in the drawing viewer's second pane and in the view picker. The region is drawn
 * in grey from its stored 0–1 polygon only (a reference, not a result); without one the whole page is
 * shown and the pane says so. Drag and wheel move and zoom it, as the main viewer does.
 */
export function FramedPage({
  projectId,
  packageId,
  page,
  documentVersionId,
  region,
  heading,
  regionLabel,
  pictureAlt,
  cache,
  className,
  slot = 'framed-page',
}: {
  projectId: string;
  packageId: string;
  page: number;
  documentVersionId: string;
  region: Point[] | null;
  /** One short line above the picture, e.g. "Architect's drawing · arch.pdf, page 2, view 3". */
  heading: React.ReactNode;
  /** The region's name on hover. */
  regionLabel: string;
  pictureAlt: string;
  /** The viewer's own picture cache, so a page shown twice is downloaded once. */
  cache?: BlobCache;
  className?: string;
  slot?: string;
}) {
  const own = useBlobCache();
  const pictures = cache ?? own;
  const key = pageKey(projectId, packageId, page, documentVersionId);
  const [picture, retry] = useBlob(pictures, key);
  const [box, setBox] = useState<Size | null>(null);
  const [frame, setFrame] = useState<{ key: string; natural: Size; view: View } | null>(null);
  const placed = frame && frame.key === key ? frame : null;
  const natural = placed?.natural ?? null;
  const view = placed?.view ?? null;
  const area = box ?? FALLBACK_BOX;
  const limits = natural ? scaleLimits(natural, area) : null;

  const setView: React.Dispatch<React.SetStateAction<View | null>> = (update) =>
    setFrame((current) => {
      if (!current || current.key !== key) return current;
      const next = typeof update === 'function' ? update(current.view) : update;
      return next ? { ...current, view: next } : current;
    });

  function framed(size: Size): View {
    return region ? focusView(boundsOf(region), size, area, scaleLimits(size, area), 0.85) : fitView(size, area);
  }

  // The pane shows a page, not a result: no outcome outline and no pill, only the grey region.
  const target: ViewerTarget = {
    key: `page:${documentVersionId}:${page}`,
    label: regionLabel,
    page,
    documentVersionId,
    outline: null,
    findingId: null,
    tone: 'missing',
    glyph: 'NOT_FOUND',
    word: '',
    needsYou: false,
    row: null,
    marks: [],
  };
  const marks: Mark[] = region ? [{ key: 'region', label: regionLabel, page, documentVersionId, outline: region }] : [];

  return (
    <TooltipProvider delayDuration={250}>
    <section data-slot={slot} aria-label={regionLabel} className={cn('flex min-h-0 flex-1 flex-col', className)}>
      <div className="flex shrink-0 flex-wrap items-center gap-1 border-b px-2 py-1 text-xs">
        <span className="min-w-0 flex-1 truncate font-medium" title={typeof heading === 'string' ? heading : undefined}>{heading}</span>
        <Button size="sm" variant="ghost" onClick={() => natural && setView(fitView(natural, area))} disabled={!view}>
          <Maximize /> Fit
        </Button>
        <Button size="sm" variant="ghost" onClick={() => natural && setView(framed(natural))} disabled={!view || !region}>
          <Crosshair /> Find view
        </Button>
      </div>
      <PageCanvas
        picture={picture}
        onRetry={retry}
        target={target}
        others={[]}
        marks={marks}
        activeMark="region"
        readings={[]}
        activeReading={null}
        natural={natural}
        view={view}
        limits={limits}
        onView={setView}
        onLoaded={(size) => setFrame({ key, natural: size, view: framed(size) })}
        onBox={setBox}
        onSelect={() => undefined}
        pictureAlt={pictureAlt}
        outlineNote={false}
      />
      {!region && (
        <p data-slot="no-region" className="shrink-0 border-t px-2 py-1 text-xs text-muted-foreground">
          The view&apos;s frame is not stored: the whole page is shown.
        </p>
      )}
    </section>
    </TooltipProvider>
  );
}
