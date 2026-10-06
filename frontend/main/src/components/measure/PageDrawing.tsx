import { useEffect, useState } from 'react';
import { downloadPagePicture } from '../../api/client';
import { projectId } from '../../api/config';
import { PageDrawingView } from './PageDrawingView';

/**
 * The drawing on the left, the form on the right (admin, 2026-10-06).
 *
 * The page being reviewed, vendor layer only, so the reviewer reads the vendor's numbers while
 * filling the form. It shows the page; it decides nothing.
 */
export function PageDrawing({ packageId, pageNumber }: { packageId: string; pageNumber: number }) {
  const key = `${packageId}:${pageNumber}`;
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
        if (!cancelled) {
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
  }, [packageId, pageNumber]);

  return <PageDrawingView pageNumber={pageNumber} url={state?.url} error={state?.error} />;
}
