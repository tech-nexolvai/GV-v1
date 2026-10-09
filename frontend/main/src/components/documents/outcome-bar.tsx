import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { OUTCOME_FILL } from '@/components/ui/outcome-badge';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import type { Outcome } from '@/data/types';
import { outcomeSegments, outcomeSentence, resultTotal, type SummaryOutcomes } from '@/lib/documents-table';

/**
 * One review's recorded results as a stacked bar (#1064). The bar is the at-a-glance part; the counts
 * under it carry each outcome's own shape, and the words are in the bar's accessible name, in each
 * count's title and in the legend above the table, so colour never carries the meaning alone.
 */
export function OutcomeBar({ outcomes }: { outcomes: SummaryOutcomes }) {
  const total = resultTotal(outcomes);
  if (total === 0) {
    // Zero is not "all clear": nothing has been checked yet.
    return (
      <span data-slot="outcome-bar" data-empty="" className="text-xs text-muted-foreground">
        No results yet
      </span>
    );
  }
  const segments = outcomeSegments(outcomes);
  return (
    <div data-slot="outcome-bar" className="flex min-w-32 flex-col gap-1">
      <div
        role="img"
        aria-label={outcomeSentence(outcomes)}
        className="flex h-2 w-full gap-px overflow-hidden rounded-full bg-muted"
      >
        {segments.map((segment) => (
          <span
            key={segment.outcome}
            data-outcome={segment.outcome}
            className="h-full"
            style={{ width: `${(segment.count / total) * 100}%`, background: OUTCOME_FILL[segment.outcome] }}
          />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-muted-foreground" aria-hidden="true">
        {segments.map((segment) => (
          <li key={segment.outcome} className="inline-flex items-center gap-0.5" title={`${segment.count} ${segment.label}`}>
            <OutcomeIcon outcome={segment.outcome} size={12} />
            <span className="num">{segment.count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const LEGEND: readonly Outcome[] = ['PASS', 'FAIL', 'REVIEW_REQUIRED', 'NOT_FOUND', 'NO_APPLICABLE_RULE'];

/** The words for the bar's shapes, once above the table. */
export function OutcomeLegend() {
  return (
    <ul
      data-slot="outcome-legend"
      aria-label="Result shapes"
      className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground"
    >
      {LEGEND.map((outcome) => (
        <li key={outcome} className="inline-flex items-center gap-1">
          <span className="inline-block size-2 rounded-full" style={{ background: OUTCOME_FILL[outcome] }} aria-hidden="true" />
          <OutcomeIcon outcome={outcome} size={12} />
          {OUTCOME_LABELS[outcome]}
        </li>
      ))}
    </ul>
  );
}
