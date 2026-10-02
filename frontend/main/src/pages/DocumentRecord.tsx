import type { DocumentRow } from './documentRows';

/** Keep identifiers available without letting them obscure the review's vendor and results. */
export function DocumentRecord({ row }: { row: DocumentRow }) {
  const pkg = row.document;
  return <div className="packages-table__record">
    <strong className="packages-table__vendor">{pkg.vendor ?? 'Untitled document set'}</strong>
    <span className="packages-table__revision">Revision {pkg.current_revision_number}</span>
  </div>;
}

/** Secondary metadata stays below the actionable review summary, even when expanded. */
export function DocumentDetails({ row, reviewerUnavailable }: { row: DocumentRow; reviewerUnavailable: boolean }) {
  const pkg = row.document;
  return (
    <details className="packages-table__details">
      <summary>Technical record details<span className="sr-only"> for {pkg.vendor ?? 'document'} ({pkg.id})</span></summary>
      <dl>
        <dt>Document ID</dt><dd>{pkg.id}</dd>
        <dt>Project ID</dt><dd>{pkg.project_id}</dd>
        <dt>Revision ID</dt><dd>{pkg.current_revision_id}</dd>
        <dt>Reviewer</dt><dd>{row.reviewer ?? (reviewerUnavailable ? 'Unavailable' : 'Not listed')}</dd>
        <dt>Category</dt><dd>Not provided</dd>
      </dl>
    </details>
  );
}
