import type { FindingCounts } from '../api/client';

const processingStates = new Set([
  'UPLOADING', 'UPLOADED', 'INGESTING', 'EXTRACTING', 'MATCHING',
  'VALIDATING_EVIDENCE', 'RUNNING_CHECKS', 'GENERATING_OUTPUTS', 'FAILED_RETRYABLE',
]);

/** Navigation advice only: never derives a verdict, approval, or completeness from counts. */
export function documentGuidance(state: string, counts: FindingCounts | null): string {
  if (state === 'APPROVED') return 'Open review to view the approved review and its available reports.';
  if (state === 'CANCELLED' || state === 'SUPERSEDED') return 'Open review to inspect this historical record. Its status does not describe an active review.';
  if (state === 'FAILED_PERMANENT') return 'Open review to inspect the processing failure. Missing results do not mean the drawing passed.';
  if (processingStates.has(state)) return 'Open review to check processing progress. Any recorded results do not mean processing has finished.';
  if (!counts) return 'Open review to inspect its status and findings, or retry the unavailable details above. No outcome can be inferred from this missing summary.';
  if (state === 'CREATED') return 'Open review to check uploads and confirm drawing values before running checks.';
  if (!['AWAITING_REVIEW', 'NEEDS_INPUT', 'CHANGES_REQUESTED'].includes(state)) return 'Open review to inspect the recorded status, findings, and available evidence.';
  if (counts.total === 0) return 'Open review to check progress and required inputs in Measurements. No findings recorded is not a pass.';

  const tasks: string[] = [];
  if (counts.failed > 0) tasks.push('inspect failed checks and their evidence');
  if (counts.review_required > 0) tasks.push('resolve checks needing a reviewer decision');
  if (counts.not_found > 0 || state === 'NEEDS_INPUT') tasks.push('confirm missing inputs in Measurements');
  if (tasks.length) {
    const list = tasks.length === 1 ? tasks[0] : `${tasks.slice(0, -1).join(', ')} and ${tasks.at(-1)}`;
    return `Open review to ${list}.`;
  }
  return 'Open review to inspect each recorded check and its evidence. These counts alone do not approve the drawing.';
}
