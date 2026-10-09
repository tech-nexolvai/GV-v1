import { useId, type ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * The page header and reading width every supporting page shares (#1125): one `<h1>`, one short line
 * under it, and the page's actions on the right (under the title on a phone).
 *
 * The title is the page's only `<h1>`: the top bar names the screen with a heading only on an open
 * review, where that is the page's `<h1>` and the review's panels use `<h2>` (`AppShell`).
 */
export function PageFrame({ title, description, actions, narrow = false, className, children }: {
  title: string;
  description: ReactNode;
  actions?: ReactNode;
  /** A form-width column (Start a review) instead of the table width. */
  narrow?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const titleId = useId();
  return (
    <section
      data-tw
      data-slot="page-frame"
      aria-labelledby={titleId}
      className={cn('min-h-0 min-w-0 flex-1 overflow-auto overscroll-contain bg-background font-sans text-foreground', className)}
    >
      <div className={cn('mx-auto flex w-full flex-col gap-6 px-4 pt-6 pb-16 sm:px-6 sm:pt-8', narrow ? 'max-w-2xl' : 'max-w-[70rem]')}>
        <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <div className="flex min-w-0 flex-[1_1_18rem] flex-col gap-1">
            <h1 id={titleId} className="text-2xl font-semibold tracking-tight">{title}</h1>
            <p className="max-w-prose text-sm text-muted-foreground [overflow-wrap:anywhere]">{description}</p>
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
        <div className="min-w-0">{children}</div>
      </div>
    </section>
  );
}

/** A loading line in the page body. */
export function PageLoading({ children }: { children: ReactNode }) {
  return <p className="py-6 text-sm text-muted-foreground" role="status">{children}</p>;
}

/** Failure remains distinct from an empty result; retry repeats the same request. */
export function PageLoadError({ title, message, onRetry }: {
  title: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div
      data-tw
      data-slot="page-load-error"
      role="alert"
      className="flex flex-col items-start gap-2 rounded-lg border border-l-4 border-l-destructive bg-card p-4 font-sans text-sm [overflow-wrap:anywhere]"
    >
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="text-muted-foreground">{message}</p>
      <Button type="button" variant="outline" size="sm" onClick={onRetry}>
        <RefreshCw aria-hidden="true" /> Try again
      </Button>
    </div>
  );
}
