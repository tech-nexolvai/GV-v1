import type { FindingCounts } from '../api/client';
import { OUTCOME_LABELS } from '../data/outcomeLabels.js';
import { OutcomeIcon } from '../components/ui/OutcomeIcon.js';

/** Never confuse an unavailable summary with a measured zero. Keep all five outcomes distinct. */
export function DocumentResults({ counts, error }: { counts: FindingCounts | null; error: string | null }) {
  if (!counts) return <span className="packages-table__unavailable" title={error ?? undefined}>Results unavailable</span>;
  if (counts.total === 0) return <span className="packages-table__empty">No findings recorded</span>;
  const entries = [
    ['PASS', counts.passed], ['FAIL', counts.failed], ['REVIEW_REQUIRED', counts.review_required],
    ['NOT_FOUND', counts.not_found], ['NO_APPLICABLE_RULE', counts.no_applicable_rule],
  ] as const;
  return <div className="packages-table__results">
    {entries.filter(([, count]) => count > 0).map(([outcome, count]) => <span key={outcome} className="packages-table__result">
      <OutcomeIcon outcome={outcome} size={12} />
      <span><strong className="document-result__count">{count}</strong> {OUTCOME_LABELS[outcome]}</span>
    </span>)}
  </div>;
}
