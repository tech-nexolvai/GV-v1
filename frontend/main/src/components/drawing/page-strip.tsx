import { useEffect, useRef, useState } from 'react';

import { cn } from '@/lib/utils';
import type { StripPage } from '@/lib/drawing-viewer';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { pageKey, useBlob, type BlobCache } from './blob-cache';

/**
 * Small pictures of the pages that have countertops (#1045), each with its countertops' outcome
 * glyphs. Picking one shows that page's first countertop. Pictures load once the strip is visible.
 */
export function PageStrip({
  pages,
  current,
  cache,
  projectId,
  packageId,
  onPick,
}: {
  pages: StripPage[];
  current: string | null;
  cache: BlobCache;
  projectId: string;
  packageId: string;
  onPick: (page: StripPage) => void;
}) {
  if (pages.length < 2) return null;
  return (
    <nav aria-label="Pages" data-slot="page-strip" className="shrink-0 border-t bg-background">
      <ol className="flex gap-2 overflow-x-auto p-2">
        {pages.map((page) => {
          const key = `${page.documentVersionId}:${page.page}`;
          return (
            <li key={key}>
              <Thumb page={page} selected={current === key} cache={cache} projectId={projectId} packageId={packageId} onPick={onPick} />
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function Thumb({ page, selected, cache, projectId, packageId, onPick }: { page: StripPage; selected: boolean; cache: BlobCache; projectId: string; packageId: string; onPick: (page: StripPage) => void }) {
  const [visible, setVisible] = useState(typeof IntersectionObserver === 'undefined');
  const box = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const element = box.current;
    if (visible || !element) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) setVisible(true);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [visible]);
  const [state] = useBlob(cache, visible ? pageKey(projectId, packageId, page.page, page.documentVersionId) : null);
  const shown = page.targets.slice(0, 3);
  const words = page.targets.map((t) => `${t.label}: ${t.word}`).join('; ');

  return (
    <button
      ref={box}
      type="button"
      data-page={page.page}
      aria-current={selected ? 'page' : undefined}
      aria-label={`Page ${page.page}: ${words}`}
      title={`Page ${page.page}`}
      onClick={() => onPick(page)}
      className={cn(
        'flex w-20 flex-col items-stretch gap-1 rounded-md border p-1 text-left outline-none hover:border-foreground/40 focus-visible:ring-[3px] focus-visible:ring-ring/50',
        selected && 'border-foreground ring-1 ring-foreground',
      )}
    >
      <span className="flex aspect-[1.414] items-center justify-center overflow-hidden rounded-sm bg-muted">
        {state.status === 'ready' ? <img src={state.url} alt="" className="size-full bg-white object-contain" /> : <span className="num text-xs text-muted-foreground">{page.page}</span>}
      </span>
      <span className="flex items-center justify-between gap-1">
        <span className="num text-xs font-medium">p{page.page}</span>
        <span className="flex items-center gap-0.5" aria-hidden="true">
          {shown.map((t) => (
            <OutcomeIcon key={t.key} outcome={t.glyph} size={11} className={GLYPH_COLOUR[t.tone]} />
          ))}
          {page.targets.length > shown.length && <span className="num text-xs text-muted-foreground">+{page.targets.length - shown.length}</span>}
        </span>
      </span>
    </button>
  );
}

const GLYPH_COLOUR = {
  pass: 'text-outcome-pass-fg',
  fail: 'text-outcome-fail-fg',
  review: 'text-outcome-review-fg',
  missing: 'text-outcome-missing-fg',
} as const;
