/** A resolved request is not necessarily a saved decision (the parent can refuse a correction). */
export type DecisionSaveResult = { saved: true } | { saved: false; error: string };
export type SimpleReviewAction = 'confirm' | 'dismiss';

/** One in-flight submission per mounted finding card. No retries or payload transformations. */
export function createDecisionSaver() {
  let saving = false;
  return async (
    submit: () => Promise<DecisionSaveResult>,
    ui: { busy: (value: boolean) => void; error: (message: string | null) => void; saved: () => void },
  ): Promise<void> => {
    if (saving) return;
    saving = true;
    ui.busy(true);
    ui.error(null);
    try {
      const result = await submit();
      if (result.saved) ui.saved();
      else ui.error(result.error);
    } catch (error) {
      ui.error(`Save was not confirmed — ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      saving = false;
      ui.busy(false);
    }
  };
}
