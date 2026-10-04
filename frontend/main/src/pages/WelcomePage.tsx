/**
 * The start screen: start a new review, or pick up a recent one.
 *
 * Shaped like a chat product's empty conversation — one line of heading and the composer in the
 * middle — except that the first "message" of a review is the drawings, so the composer is the
 * start-a-review form (`NewReviewForm`).
 *
 * History worth keeping: this screen once opened `sess-001`, a package id in no database, and later
 * offered a chat box that silently asked about whichever package happened to be newest. Both are
 * gone. A question belongs to a review, and the review screen is where it is asked.
 */

import { ArrowUpRight } from 'lucide-react';
import { NewReviewForm } from '../components/upload/NewReviewForm';
import { StatusBadge } from '../components/ui/Badge';
import { listPackages } from '../api/client';
import { projectId } from '../api/config';
import { useAsync } from '../api/useAsync';
import './WelcomePage.css';

interface WelcomePageProps {
  onCreated: (packageId: string) => void;
  onOpenReview: (packageId: string) => void;
}

/** A few recent reviews to return to. The sidebar has the full list. */
const RECENT_LIMIT = 3;

export function WelcomePage({ onCreated, onOpenReview }: WelcomePageProps) {
  const packages = useAsync(() => listPackages(projectId(), { limit: RECENT_LIMIT }), []);
  const recent = packages.status === 'ready' ? packages.data.items : [];

  return (
    <div className="welcome">
      <div className="welcome__inner">
        <h2 className="welcome__heading">Start a review</h2>
        <p className="welcome__sub">
          Add the architect&rsquo;s drawings and the vendor&rsquo;s shop drawings. GV Review reads
          both, checks every dimension against the rulebook, and shows you what needs a decision.
        </p>

        <NewReviewForm onCreated={onCreated} />

        {/* Failure is said out loud: "no reviews" on a screen that could not reach the server
            reads as an empty project. */}
        {packages.status === 'error' && (
          <p className="welcome__error" role="alert">
            Recent reviews could not be loaded — {packages.error.message}
          </p>
        )}

        {recent.length > 0 && (
          <section className="welcome__recent" aria-labelledby="welcome-recent">
            <h3 id="welcome-recent" className="welcome__recent-label">Continue a review</h3>
            <ul className="welcome__recent-list">
              {recent.map((pkg) => (
                <li key={pkg.id}>
                  <button
                    type="button"
                    className="welcome__recent-item"
                    onClick={() => onOpenReview(pkg.id)}
                  >
                    <span className="welcome__recent-vendor">{pkg.vendor ?? 'Untitled document set'}</span>
                    <span className="welcome__recent-meta">
                      <StatusBadge status={pkg.state} size="sm" />
                      <span className="welcome__recent-date">
                        {new Date(pkg.created_at).toLocaleDateString(undefined, {
                          day: 'numeric',
                          month: 'short',
                        })}
                      </span>
                      <ArrowUpRight size={14} aria-hidden="true" className="welcome__recent-arrow" />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
