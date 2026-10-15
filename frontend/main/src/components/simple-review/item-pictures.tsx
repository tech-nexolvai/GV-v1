import { createContext, useContext, useEffect, useState } from 'react';
import { ImageOff } from 'lucide-react';

import type { PageRef } from '@/api/review-items';
import { cn } from '@/lib/utils';
import { pointsOf, ringOf } from '@/lib/review-items';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';

/** A stored picture the page can show: a whole page of one document, or a close-up path from the API. */
export type PictureSource =
  | { kind: 'page'; page: number; documentVersionId: string }
  | { kind: 'zoom'; path: string };

/** Turns a source into something an <img> can show. The page passes the live one; tests pass their own. */
export type PictureLoader = (source: PictureSource) => Promise<string>;

const PicturesContext = createContext<PictureLoader | null>(null);

export function PicturesProvider({ load, children }: { load: PictureLoader; children: React.ReactNode }) {
  return <PicturesContext.Provider value={load}>{children}</PicturesContext.Provider>;
}

function sourceKey(source: PictureSource | null): string | null {
  if (source === null) return null;
  return source.kind === 'page' ? `page|${source.documentVersionId}|${source.page}` : `zoom|${source.path}`;
}

type PictureState = { status: 'loading' } | { status: 'ready'; url: string } | { status: 'error'; message: string };

/** The picture for a source. State is kept per source, so another item's picture never shows here. */
function usePicture(source: PictureSource | null): [PictureState, () => void] {
  const load = useContext(PicturesContext);
  const key = sourceKey(source);
  const [attempt, setAttempt] = useState(0);
  const [stored, setStored] = useState<{ key: string; attempt: number; state: PictureState } | null>(null);

  useEffect(() => {
    if (source === null || key === null || load === null) return;
    let current = true;
    load(source).then(
      (url) => current && setStored({ key, attempt, state: { status: 'ready', url } }),
      (error: unknown) => current && setStored({ key, attempt, state: { status: 'error', message: error instanceof Error ? error.message : String(error) } }),
    );
    return () => {
      current = false;
    };
    // `source` is described completely by `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt, load]);

  const state: PictureState =
    load === null
      ? { status: 'error', message: 'No pictures on this screen.' }
      : stored && stored.key === key && stored.attempt === attempt
        ? stored.state
        : { status: 'loading' };
  return [state, () => setAttempt((n) => n + 1)];
}

function Unavailable({ children, onRetry }: { children: React.ReactNode; onRetry?: () => void }) {
  return (
    <div className="flex h-full min-h-24 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-4 py-6 text-center text-[13px] leading-5 text-muted-foreground">
      <ImageOff aria-hidden="true" className="size-4" />
      <span>{children}</span>
      {onRetry && (
        <Button size="xs" variant="ghost" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

/**
 * A whole stored page with one spot ringed: an amber ring over a white halo, a little larger than the
 * spot, so the number inside stays readable. The page is never inverted, in either theme.
 */
export function PagePicture({
  pageRef,
  label,
  className,
}: {
  pageRef: PageRef;
  /** What the picture shows, for a screen reader ("Vendor page 3, the number ringed"). */
  label: string;
  className?: string;
}) {
  const [state, retry] = usePicture({ kind: 'page', page: pageRef.page_number, documentVersionId: pageRef.document_version_id });
  const points = pointsOf(pageRef);
  const ring = points ? ringOf(points) : null;
  if (state.status === 'error') return <Unavailable onRetry={retry}>This page's picture could not be loaded: {state.message}</Unavailable>;
  return (
    <div role="img" aria-label={label} className={cn('flex h-full items-center justify-center overflow-hidden rounded-lg border border-border bg-white p-2', className)}>
      {state.status === 'loading' ? (
        <Skeleton className="h-full min-h-32 w-full" />
      ) : (
        <span className="relative inline-block max-h-full">
          <img src={state.url} alt="" draggable={false} className="block max-h-[inherit] w-auto max-w-full select-none" style={{ maxHeight: 'inherit' }} />
          {ring && (
            <svg aria-hidden="true" viewBox="0 0 1 1" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full overflow-visible">
              <ellipse cx={ring.cx} cy={ring.cy} rx={ring.rx} ry={ring.ry} fill="none" stroke="#ffffff" strokeWidth={6} vectorEffect="non-scaling-stroke" />
              <ellipse data-slot="spot-ring" cx={ring.cx} cy={ring.cy} rx={ring.rx} ry={ring.ry} fill="none" stroke="var(--outcome-review)" strokeWidth={2.5} vectorEffect="non-scaling-stroke" />
            </svg>
          )}
        </span>
      )}
    </div>
  );
}

/** The stored close-up of the number, shown at most at its own size: it is never blown up past the source. */
export function ZoomPicture({ path, label }: { path: string; label: string }) {
  const [state, retry] = usePicture({ kind: 'zoom', path });
  if (state.status === 'error') return <Unavailable onRetry={retry}>The close-up could not be loaded: {state.message}</Unavailable>;
  return (
    <div role="img" aria-label={label} className="flex h-full items-center justify-center overflow-hidden rounded-lg border border-border bg-white p-3">
      {state.status === 'loading' ? (
        <Skeleton className="h-full min-h-40 w-full" />
      ) : (
        <img src={state.url} alt="" draggable={false} className="block h-auto max-h-full w-auto max-w-full select-none [image-rendering:auto]" />
      )}
    </div>
  );
}

export { Unavailable as PictureUnavailable };
