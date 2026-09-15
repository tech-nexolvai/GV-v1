import type { Finding } from '../../data/types';

const OUTCOME_LABELS = {
  PASS: 'Pass',
  FAIL: 'Fail',
  REVIEW_REQUIRED: 'Review required',
  NOT_FOUND: 'Not found',
  NO_APPLICABLE_RULE: 'Not applicable',
} as const;

export function findingsTableMarkdown(findings: readonly Finding[]): string {
  if (findings.length === 0) return '';

  const rows = findings.map((finding) => [
    finding.name || finding.check_id,
    readingFor(finding),
    sheetFor(finding),
    comparisonFor(finding),
    OUTCOME_LABELS[finding.outcome],
  ]);

  return [
    '| Check | Reading | Sheet | Compared against | Outcome |',
    '| --- | --- | --- | --- | --- |',
    ...rows.map((row) => `| ${row.map(escapeCell).join(' | ')} |`),
  ].join('\n');
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

function escapeCell(value: string): string {
  return value.replace(/\s+/g, ' ').replaceAll('|', '/').trim() || 'Not recorded';
}
