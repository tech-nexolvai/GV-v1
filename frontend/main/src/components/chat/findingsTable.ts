import { OUTCOME_LABELS } from '../../data/outcomeLabels.js';
import type { Finding } from '../../data/types';

// Re-exported rather than redefined. This file used to carry its own spelling of the same five
// outcomes ('Pass', 'Not found'), which is how the table came to disagree with both the badge
// beside it and the narration above it.
export { OUTCOME_LABELS };

/**
 * The cells of one finding's row, as the `FindingsTable` component shows them: what a finding read,
 * where, and against what.
 */
export interface FindingCells {
  check: string;
  reading: string;
  sheet: string;
  comparison: string;
  outcome: string;
}

export function findingCells(finding: Finding): FindingCells {
  return {
    check: finding.name || finding.check_id,
    reading: readingFor(finding),
    sheet: sheetFor(finding),
    comparison: comparisonFor(finding),
    outcome: OUTCOME_LABELS[finding.outcome],
  };
}

function readingFor(finding: Finding): string {
  return (
    operandValues(finding, 'SHOP') ||
    finding.found ||
    valueFromTraceSource(finding, 'SHOP') ||
    'Not recorded'
  );
}

function comparisonFor(finding: Finding): string {
  return (
    operandValues(finding, 'ARCH') ||
    finding.expected ||
    valueFromTraceSource(finding, 'ARCH') ||
    // The engine's stored exact comparison is safe to display verbatim. Never split it to
    // manufacture an approved-side operand the finding did not record.
    (finding.trace?.kind === 'calculation' ? finding.trace.comparison : '') ||
    'Not recorded'
  );
}

function sheetFor(finding: Finding): string {
  if (finding.shop_evidence) return `Shop p.${finding.shop_evidence.page}`;
  if (finding.arch_evidence) return `Arch p.${finding.arch_evidence.page}`;
  return 'Not recorded';
}

function operandValues(finding: Finding, role: 'ARCH' | 'SHOP'): string {
  const values = finding.recorded_operands
    ?.filter((operand) => operand.source === role || operand.documentRole === role)
    .map((operand) => operand.value)
    .filter(Boolean) ?? [];
  return unique(values).join('; ');
}

function valueFromTraceSource(finding: Finding, role: 'ARCH' | 'SHOP'): string {
  const values = finding.trace?.operands
    .filter((operand) => operand.source === role)
    .map((operand) => operand.value)
    .filter(Boolean) ?? [];
  return unique(values).join('; ');
}

function unique(values: readonly string[]): string[] {
  return Array.from(new Set(values));
}
