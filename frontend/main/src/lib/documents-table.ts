import type { PackageSummary } from '@/api/client';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import type { Outcome } from '@/data/types';

/**
 * The Documents table's arithmetic (#1064), kept out of React so it can be tested on its own.
 *
 * The bar shows **recorded results**: what each check found when it ran. A reviewer's decision does
 * not change a recorded result, so "Needs your decision" here counts the checks that could not decide
 * on their own, decided since or not. What still needs a decision is the server's own
 * `needs_decision`, shown in its own column (the same split as the Results donut, #1056).
 */
export type SummaryOutcomes = PackageSummary['outcomes'];

const OUTCOME_KEYS: readonly { key: keyof SummaryOutcomes; outcome: Outcome }[] = [
  { key: 'pass', outcome: 'PASS' },
  { key: 'fail', outcome: 'FAIL' },
  { key: 'review', outcome: 'REVIEW_REQUIRED' },
  { key: 'not_found', outcome: 'NOT_FOUND' },
  { key: 'no_rule', outcome: 'NO_APPLICABLE_RULE' },
];

export interface OutcomeSegment {
  outcome: Outcome;
  count: number;
  label: string;
}

/** The non-zero recorded results, always in the same order (looks right first). */
export function outcomeSegments(outcomes: SummaryOutcomes): OutcomeSegment[] {
  return OUTCOME_KEYS.filter(({ key }) => outcomes[key] > 0).map(({ key, outcome }) => ({
    outcome,
    count: outcomes[key],
    label: OUTCOME_LABELS[outcome],
  }));
}

export function resultTotal(outcomes: SummaryOutcomes): number {
  return OUTCOME_KEYS.reduce((sum, { key }) => sum + outcomes[key], 0);
}

/** "12 recorded results: 8 Looks right, 2 Needs correction, …", or the zero case in words. */
export function outcomeSentence(outcomes: SummaryOutcomes): string {
  const total = resultTotal(outcomes);
  if (total === 0) return 'No recorded results yet';
  const parts = outcomeSegments(outcomes).map((segment) => `${segment.count} ${segment.label}`);
  return `${total} recorded ${total === 1 ? 'result' : 'results'}: ${parts.join(', ')}`;
}

/** What the drawing set is for, in words; the API sends the vocabulary value. */
export function productWord(product: string | null): string {
  if (!product) return 'Not set';
  return product.charAt(0).toUpperCase() + product.slice(1);
}

/** A case-insensitive match on the vendor or the product, after trimming the query. */
export function matchesSearch(row: Pick<PackageSummary, 'vendor' | 'product_type'>, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === '') return true;
  return [row.vendor ?? 'Untitled document set', productWord(row.product_type)].some((text) =>
    text.toLowerCase().includes(needle),
  );
}

/**
 * Status order for sorting: the way a review moves (the backend's `PackageState` order), so "sort by
 * status" groups reading, then waiting for a reviewer, then signed off, then stopped.
 */
const STATUS_ORDER = [
  'CREATED', 'UPLOADING', 'UPLOADED', 'INGESTING', 'EXTRACTING', 'MATCHING', 'VALIDATING_EVIDENCE',
  'RUNNING_CHECKS', 'GENERATING_OUTPUTS', 'NEEDS_INPUT', 'AWAITING_REVIEW', 'CHANGES_REQUESTED',
  'APPROVED', 'FAILED_RETRYABLE', 'FAILED_PERMANENT', 'CANCELLED', 'SUPERSEDED',
];

export function statusRank(state: string): number {
  const index = STATUS_ORDER.indexOf(state);
  return index === -1 ? STATUS_ORDER.length : index;
}
