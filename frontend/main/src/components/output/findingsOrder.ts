/**
 * The order findings are listed in: by what they ask of the reviewer.
 *
 * A module of its own so the table component file exports only components (fast refresh).
 */

import type { Finding, Outcome } from '../../data/types';

const ORDER: Record<Outcome, number> = {
  FAIL: 0,
  REVIEW_REQUIRED: 1,
  NOT_FOUND: 2,
  PASS: 3,
  NO_APPLICABLE_RULE: 4,
};

export function sortFindings(findings: readonly Finding[]): Finding[] {
  // A copy, and a stable sort: findings of the same outcome keep the engine's order.
  return findings
    .map((finding, index) => ({ finding, index }))
    .sort((a, b) => ORDER[a.finding.outcome] - ORDER[b.finding.outcome] || a.index - b.index)
    .map(({ finding }) => finding);
}
