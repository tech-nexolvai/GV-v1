export type DecisionFeedback =
  | { kind: 'saving' }
  | { kind: 'saved' }
  | { kind: 'error'; message: string };

/** One in-flight request per item. Other items can still be decided independently. */
export function createDecisionSaver() {
  const pending = new Set<string>();
  return async (
    key: string,
    submit: () => Promise<unknown>,
    update: (key: string, feedback: DecisionFeedback) => void,
    onSaved: () => void,
  ): Promise<void> => {
    if (pending.has(key)) return;
    pending.add(key);
    update(key, { kind: 'saving' });
    try {
      await submit();
      update(key, { kind: 'saved' });
      onSaved();
    } catch (caught) {
      update(key, { kind: 'error', message: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      pending.delete(key);
    }
  };
}

export function feedbackText(feedback: DecisionFeedback | undefined): string | null {
  if (!feedback) return null;
  if (feedback.kind === 'saving') return 'Saving this decision…';
  if (feedback.kind === 'saved') return 'Decision saved.';
  return `Decision not saved — ${feedback.message}`;
}
