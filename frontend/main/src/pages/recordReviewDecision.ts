import type { Finding } from '../data/types';
import type { DecisionSaveResult, SimpleReviewAction } from '../components/chat/decisionSave';

/** Update only the acknowledged finding against the latest state. A failed write changes nothing. */
export async function recordReviewDecision(
  findingId: string,
  action: SimpleReviewAction,
  persist: () => Promise<unknown>,
  update: (apply: (current: Finding[]) => Finding[]) => void,
): Promise<DecisionSaveResult> {
  try {
    await persist();
    update(current => current.map(finding => finding.id === findingId
      ? { ...finding, reviewer_action: action } : finding));
    return { saved: true };
  } catch (error) {
    return { saved: false, error: `That decision was not recorded — ${error instanceof Error ? error.message : String(error)}` };
  }
}
