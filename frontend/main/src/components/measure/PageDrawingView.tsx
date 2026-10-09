import { cn } from '@/lib/utils';

/** What the drawing pane shows for one page: loading, an error, or the picture. No fetching here. */
export function PageDrawingView({
  pageNumber,
  url,
  error,
  className,
}: {
  pageNumber: number;
  url?: string;
  error?: string;
  className?: string;
}) {
  return (
    <aside
      data-slot="page-drawing"
      className={cn('flex min-w-0 flex-col gap-2 rounded-xl border bg-card p-3', className)}
      aria-label={`Drawing, page ${pageNumber}`}
    >
      <p className="text-xs text-muted-foreground">
        Drawing · page <span className="num">{pageNumber}</span>
      </p>
      {url === undefined && error === undefined && (
        <p className="flex aspect-[4/3] items-center justify-center rounded-lg bg-muted text-xs text-muted-foreground">Loading the drawing…</p>
      )}
      {error !== undefined && (
        <p className="text-xs text-outcome-fail-fg" role="alert">
          The drawing could not be shown. {error}
        </p>
      )}
      {url !== undefined && (
        <a href={url} target="_blank" rel="noreferrer" title="Open the page full size">
          <img className="w-full rounded-lg border bg-white object-contain" src={url} alt={`Drawing page ${pageNumber}`} />
        </a>
      )}
    </aside>
  );
}
