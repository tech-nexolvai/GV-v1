import { Plus, ArrowRight } from 'lucide-react';
import { MOCK_PACKAGES_LIST } from '../data/mock';
import { StatusBadge } from '../components/ui/Badge';
import './PackagesPage.css';

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
  return (
    <div className="packages-page animate-fade-in">
      {/* Page header */}
      <div className="packages-page__header">
        <div>
          <div className="gv-bar" />
          <h1 className="packages-page__title">Document Packages</h1>
          <p className="packages-page__subtitle">
            Review submissions from vendors against the architectural set and rulebook.
          </p>
        </div>
        <button className="btn btn--action" onClick={onNewPackage}>
          <Plus size={14} />
          New Package
        </button>
      </div>

      {/* Table */}
      <div className="packages-table-wrap">
        <table className="packages-table">
          <thead>
            <tr>
              <th>Package ID</th>
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
            {MOCK_PACKAGES_LIST.map((pkg, i) => (
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
