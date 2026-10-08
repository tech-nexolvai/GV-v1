import { reviewStage, type NextAction } from '../lib/review-stage.js';
import type { DocumentRow } from './documentRows';

/**
 * The next step for a Documents card: the same words the review's header button uses (#1034).
 *
 * Navigation only. It never decides a result: a card says "Sign off" only when the server's own
 * readiness answer says nothing blocks it, and a package whose results could not be loaded says
 * so instead of pretending it has none.
 */
export function documentNextStep(row: Pick<DocumentRow, 'document' | 'counts' | 'readiness' | 'exports'>): NextAction {
  const stage = reviewStage({
    state: row.document.state,
    findingsTotal: row.counts ? row.counts.total : null,
    readiness: row.readiness
      ? { blockingFindings: row.readiness.blocking_findings, canApprove: row.readiness.can_approve, reason: row.readiness.reason }
      : null,
    exports: row.exports ?? null,
  });
  // Results that did not load are not "no results": do not send the reviewer to run checks on that.
  if (row.counts === null && stage.next.kind === 'run-checks') {
    return { kind: 'none', label: 'Results unavailable', disabled: true, reason: 'The recorded results could not be loaded. Open the review to see them.' };
  }
  return stage.next;
}
