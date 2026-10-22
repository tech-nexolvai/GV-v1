/**
 * The start screen: start a new review, or pick up a recent one.
 *
 * The page header is the one every supporting page uses (#1125); the form under it is the
 * start-a-review form (`NewReviewForm`), and a short list of recent reviews follows in the Documents
 * table's style.
 *
 * History worth keeping: this screen once opened `sess-001`, a package id in no database, and later
 * offered a chat box that silently asked about whichever package happened to be newest. Both are
 * gone. A question belongs to a review, and the review screen is where it is asked.
 */

import { ArrowRight } from 'lucide-react';
import { listPackages } from '@/api/client';
import { projectId } from '@/api/config';
import { useAsync } from '@/api/useAsync';
import { NewReviewForm } from '@/components/upload/NewReviewForm';
import { PageFrame } from '@/components/ui/PageFrame';
import { sidebarWords } from '@/lib/set-check';

interface WelcomePageProps {
  /** An upload finished: the app opens the set's chat (#1464). */
  onCreated: (packageId: string) => void;
  /** A half-finished upload's saved set, opened to inspect. Without it, the same as `onCreated`. */
  onOpenSaved?: (packageId: string) => void;
  onOpenReview: (packageId: string) => void;
}

/** A few recent reviews to return to. The sidebar has the full list. */
const RECENT_LIMIT = 3;
const UNTITLED = 'Untitled document set';

function formatDay(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

export function WelcomePage({ onCreated, onOpenSaved, onOpenReview }: WelcomePageProps) {
  const packages = useAsync(() => listPackages(projectId(), { limit: RECENT_LIMIT }), []);
  const recent = packages.status === 'ready' ? packages.data.items : [];

  return (
    <PageFrame
      narrow
      title="Start a review"
      description="Add the architect's drawings and the vendor's shop drawings to check them."
    >
      <div className="flex flex-col gap-8">
        <NewReviewForm onCreated={onCreated} onOpenSaved={onOpenSaved} />

        {/* Failure is said out loud: "no reviews" on a screen that could not reach the server
            reads as an empty project. */}
        {packages.status === 'error' && (
          <p className="text-sm" role="alert">
            Recent reviews could not be loaded: {packages.error.message}
          </p>
        )}

        {recent.length > 0 && (
          <section className="flex flex-col gap-2" aria-labelledby="welcome-recent">
            <h2 id="welcome-recent" className="text-base font-semibold">Continue a review</h2>
            <ul className="divide-y overflow-hidden rounded-lg border">
              {recent.map((pkg) => {
                const vendor = pkg.vendor ?? UNTITLED;
                return (
                  <li key={pkg.id}>
                    <button
                      type="button"
                      className="flex w-full items-center gap-3 px-3 py-2.5 text-left outline-none hover:bg-muted/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:ring-inset"
                      onClick={() => onOpenReview(pkg.id)}
                    >
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-sm font-medium" title={vendor}>{vendor}</span>
                        <span className="text-xs text-muted-foreground">
                          {/* #1744: "Round" (the vendor's submission), as everywhere else. */}
                          Round <span className="num">{pkg.current_revision_number}</span>
                          {' · '}
                          <time className="num" dateTime={pkg.created_at}>{formatDay(pkg.created_at)}</time>
                        </span>
                      </span>
                      {/* #1744: a set's state is grey words, never an outcome colour; the newest full check's, as in the sidebar. */}
                      <span data-slot="set-status" className="shrink-0 text-xs text-muted-foreground max-sm:hidden">{sidebarWords(pkg)}</span>
                      <ArrowRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        )}
      </div>
    </PageFrame>
  );
}
