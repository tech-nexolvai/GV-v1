import { Plus, ArrowRight } from 'lucide-react';
import { listPackages, getFindingCounts, listReviewSessions } from '../api/client';
import type { PackageStatus } from '../data/types';
import { projectId } from '../api/config';
import { useAsync } from '../api/useAsync';
import { StatusBadge } from '../components/ui/Badge';
import './PackagesPage.css';

/** A package plus its finding counts — two calls the table shows as one row. */
interface PackageRow {
  id: string;
  vendor: string;
  project: string;
  category: string;
  status: PackageStatus;
  submitted_at: string;
  reviewer: string | null;
  pass_count: number;
  fail_count: number;
  review_count: number;
  /** `NOT_FOUND` plus `NO_APPLICABLE_RULE` — the checks that produced no verdict at all. */
  missing_count: number;
}

/**
 * Fetch the packages, then each one's counts.
 *
 * The counts are a second call per package because the list endpoint does not carry them, and
 * `Promise.all` rather than a loop so one slow package does not hold up the rest. If the number of
 * packages ever grows past a page this wants a batched endpoint instead — noted rather than
 * pre-built, since a page is currently 50.
 */
async function loadRows(): Promise<PackageRow[]> {
  const project = projectId();
  const [page, sessions] = await Promise.all([
    listPackages(project),
    // Every sitting in the project, not just the caller's: this column answers "who has this?", and
    // a list showing only your own would leave somebody else's work looking unclaimed.
    listReviewSessions(project, { mine: false }),
  ]);

  // Keyed by revision, because that is what a sitting is opened against. A package whose drawings
  // were re-uploaded has a new revision, and the previous reviewer's name does not carry over to it.
  const reviewerByRevision = new Map(
    sessions.items.map((item) => [item.package_revision_id, item.reviewer]),
  );

  return Promise.all(
    page.items.map(async (pkg) => {
      const counts = await getFindingCounts(project, pkg.id);
      return {
        id: pkg.id,
        vendor: pkg.vendor ?? '—',
        project: pkg.project_id,
        // No source on the wire yet; shown as absent rather than invented.
        category: '—',
        status: pkg.state as PackageStatus,
        submitted_at: pkg.created_at,
        reviewer: reviewerByRevision.get(pkg.current_revision_id) ?? null,
        pass_count: counts.passed,
        fail_count: counts.failed,
        review_count: counts.review_required,
        missing_count: counts.not_found + counts.no_applicable_rule,
      };
    }),
  );
}

interface PackagesPageProps {
  onOpenReview: (packageId: string) => void;
  onNewPackage: () => void;
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  });
}

export function PackagesPage({ onOpenReview, onNewPackage }: PackagesPageProps) {
  const rows = useAsync(loadRows, []);

  return (
    <div className="packages-page animate-fade-in">
      {/* Page header */}
      <div className="packages-page__header">
        <div>
          <h1 className="packages-page__title">Documents</h1>
          <p className="packages-page__subtitle">
            Review submissions from vendors against the architectural set and rulebook.
          </p>
        </div>
        <button className="btn btn--action" onClick={onNewPackage}>
          <Plus size={14} />
          New Document
        </button>
      </div>

      {/* Table */}
      <div className="packages-table-wrap">
        <table className="packages-table">
          <thead>
            <tr>
              <th>Document ID</th>
              <th>Vendor</th>
              <th>Project</th>
              <th>Category</th>
              <th>Status</th>
              <th>Results</th>
              <th>Submitted</th>
              <th>Reviewer</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.status === 'loading' && (
              <tr>
                <td colSpan={8} className="packages-table__state">Loading documents…</td>
              </tr>
            )}
            {rows.status === 'error' && (
              /* Said out loud, not rendered as an empty table. An empty table here would read as
                 "no documents", and in a review workflow that is the same sentence as "nothing to
                 check" — the one thing a failure must never look like. */
              <tr>
                <td colSpan={8} className="packages-table__state packages-table__state--error">
                  Could not load documents: {rows.error.message}
                </td>
              </tr>
            )}
            {rows.status === 'ready' && rows.data.length === 0 && (
              <tr>
                <td colSpan={8} className="packages-table__state">No documents yet.</td>
              </tr>
            )}
            {(rows.status === 'ready' ? rows.data : []).map((pkg, i) => (
              <tr
                key={pkg.id}
                className="packages-table__row animate-fade-in"
                style={{ animationDelay: `${i * 40}ms` }}
                onClick={() => onOpenReview(pkg.id)}
                tabIndex={0}
                role="button"
                aria-label={`Open review for ${pkg.id}`}
                onKeyDown={e => e.key === 'Enter' && onOpenReview(pkg.id)}
              >
                <td data-label="Package ID">
                  <span className="packages-table__id">{pkg.id}</span>
                </td>
                <td data-label="Vendor">
                  <span className="packages-table__vendor">{pkg.vendor}</span>
                </td>
                <td data-label="Project">
                  <span className="packages-table__project">{pkg.project}</span>
                </td>
                <td data-label="Category">
                  <span className="packages-table__category">{pkg.category}</span>
                </td>
                <td data-label="Status"><StatusBadge status={pkg.status} size="sm" /></td>
                <td data-label="Results">
                  <div className="packages-table__results">
                    {pkg.pass_count > 0 && <span className="packages-table__result packages-table__result--pass">✓ {pkg.pass_count}</span>}
                    {pkg.fail_count > 0 && <span className="packages-table__result packages-table__result--fail">✕ {pkg.fail_count}</span>}
                    {pkg.review_count > 0 && <span className="packages-table__result packages-table__result--review">◎ {pkg.review_count}</span>}
                    {pkg.missing_count > 0 && <span className="packages-table__result packages-table__result--missing">○ {pkg.missing_count}</span>}
                  </div>
                </td>
                <td data-label="Submitted">
                  <span className="packages-table__date">{formatDate(pkg.submitted_at)}</span>
                </td>
                <td data-label="Reviewer">
                  <span className="packages-table__reviewer">{pkg.reviewer ?? '—'}</span>
                </td>
                <td>
                  <ArrowRight size={14} className="packages-table__arrow" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
