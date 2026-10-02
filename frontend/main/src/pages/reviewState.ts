import type { ChatMessage, Finding } from '../data/types';

/** Count reviewed items against the same set of findings that requires reviewer action. */
export function reviewActionCounts(findings: readonly Finding[]) {
  const required = findings.filter((finding) => finding.outcome !== 'PASS' && finding.outcome !== 'NO_APPLICABLE_RULE');
  return { total: required.length, reviewed: required.filter((finding) => finding.reviewer_action !== null).length };
}

const WORKING_STATES = new Set([
  'UPLOADED', 'INGESTING', 'EXTRACTING', 'MATCHING', 'VALIDATING_EVIDENCE',
  'RUNNING_CHECKS', 'GENERATING_OUTPUTS',
]);
const FAILED_STATES = new Set(['FAILED_RETRYABLE', 'FAILED_PERMANENT', 'CANCELLED', 'SUPERSEDED']);

export function isReviewWorking(state: string): boolean {
  return WORKING_STATES.has(state);
}

/** A 202 receipt is not completion. Observe fresh findings or a real terminal transition. */
export function checksFinished(
  state: string,
  previousFindingIds: readonly string[],
  findings: readonly Pick<Finding, 'id'>[],
  sawWorking: boolean,
): boolean {
  if (FAILED_STATES.has(state)) return true;
  if (isReviewWorking(state)) return false;
  const previous = new Set(previousFindingIds);
  return findings.some((finding) => !previous.has(finding.id)) ||
    (sawWorking && ['AWAITING_REVIEW', 'NEEDS_INPUT', 'APPROVED'].includes(state));
}

export function reviewOverview(
  packageId: string,
  state: string,
  findings: readonly Finding[],
  timestamp: string,
): ChatMessage {
  const content = findings.length > 0
    ? `These are the ${findings.length} recorded findings for this review. Open a row to inspect its values and evidence, then record your decision.`
    : isReviewWorking(state)
      ? 'The drawings are being processed. Open Measurements to inspect the available readings while the review updates.'
      : FAILED_STATES.has(state)
        ? 'This review needs attention. No findings are available. Inspect the package status before continuing.'
        : 'No findings are available yet. Open Measurements to confirm the drawing readings, fill any missing values, and run the checks.';
  return {
    id: `overview-${packageId}`,
    role: 'assistant',
    content,
    timestamp,
    findings: [...findings],
  };
}
