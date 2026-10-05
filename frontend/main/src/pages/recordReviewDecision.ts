import type { Finding } from '../data/types';
import type { DecisionSaveResult, SimpleReviewAction } from '../components/chat/decisionSave';

/** A failed request must not undo another finding's concurrent acknowledged decision. */
export async function recordReviewDecision(
  findingId: string,
  action: SimpleReviewAction,
  persist: () => Promise<unknown>,
  update: (apply: (current: Finding[]) => Finding[]) => void,
): Promise<DecisionSaveResult> {
  try {
    await persist();
    update((current) => current.map((finding) => finding.id === findingId
      ? { ...finding, reviewer_action: action } : finding));
    return { saved: true };
  } catch (error) {
    return { saved: false, error: `That decision was not recorded — ${error instanceof Error ? error.message : String(error)}` };
  }
}
