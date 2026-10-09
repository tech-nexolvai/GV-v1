import { useEffect, useState } from 'react';
import { ApiError, downloadPagePicture, preparePagePictures } from '../../api/client';
import { projectId } from '../../api/config';
import { PageDrawingView } from './PageDrawingView';

/**
 * The drawing on the left, the form on the right (admin, 2026-10-06).
 *
 * The page being reviewed, vendor layer only, so the reviewer reads the vendor's numbers while
 * filling the form. It shows the page; it decides nothing.
 */
export function PageDrawing({ packageId, pageNumber, className }: { packageId: string; pageNumber: number; className?: string }) {
  const key = `${packageId}:${pageNumber}`;
  /** Bumped to try again while the worker is still rendering the page. */
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState<{ key: string; url?: string; error?: string } | null>(null);
  // Derived, not reset in the effect: a picture for another page is simply not this page's.
  const state = loaded && loaded.key === key ? loaded : null;

  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    downloadPagePicture(projectId(), packageId, pageNumber).then(
      (blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setLoaded({ key: `${packageId}:${pageNumber}`, url });
      },
      (error: unknown) => {
        if (cancelled) return;
        // Not rendered yet: ask the worker (idempotent) and look again shortly, for a while.
        if (error instanceof ApiError && error.status === 404 && attempt < 40) {
          if (attempt === 0) void preparePagePictures(projectId(), packageId).catch(() => undefined);
          window.setTimeout(() => {
            if (!cancelled) setAttempt((count) => count + 1);
          }, 3000);
          return;
        }
        {
          setLoaded({
            key: `${packageId}:${pageNumber}`,
            error: error instanceof Error ? error.message : 'The drawing page could not be loaded.',
          });
        }
      },
    );
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [packageId, pageNumber, attempt]);

  return <PageDrawingView pageNumber={pageNumber} url={state?.url} error={state?.error} className={className} />;
}
