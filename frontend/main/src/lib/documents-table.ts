import type { PackageSummary } from '@/api/client';
import { setResult, type ResultKind } from '@/lib/set-check';
import { productLabel } from '@/lib/products';

/**
 * The Documents table's arithmetic (#1064), kept out of React so it can be tested on its own. The
 * table shows each set's newest full check (`lib/set-check.ts`); the old pipeline's recorded results
 * and states are no longer shown (a set read only by the chat never moves on there).
 */

/** The old pipeline's recorded results per outcome (the Usage page still counts them by day). */
export type SummaryOutcomes = PackageSummary['outcomes'];

export function resultTotal(outcomes: SummaryOutcomes): number {
  return outcomes.pass + outcomes.fail + outcomes.review + outcomes.not_found + outcomes.no_rule;
}

/** What the drawing set is for, in the product registry's words (#1547); the API sends its id. */
export function productWord(product: string | null): string {
  if (!product) return 'Not set';
  return productLabel(product);
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
 * Review-result order for sorting: what asks the most of the vendor first (Rejected, Revise and
 * resubmit), then what needs GV's person before it can be approved (Review needed), then Approved
 * as noted and Approved, then checks with no result to send, then running, then sets not checked
 * yet.
 */
const RESULT_ORDER: readonly ResultKind[] = ['rejected', 'revise_resubmit', 'review_needed', 'approved_as_noted', 'approved', 'not_for_sending', 'unfinished', 'checking', 'none'];

export function resultRank(row: Parameters<typeof setResult>[0]): number {
  return RESULT_ORDER.indexOf(setResult(row).kind);
}
