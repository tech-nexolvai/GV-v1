export type MeasurementDecisionState =
  | { kind: 'saving' }
  | { kind: 'saved' }
  | { kind: 'error'; message: string };

/** Each drawing, part or add form owns its request. No retries or payload edits. */
export function createMeasurementDecisionSaver() {
  const pending = new Set<string>();
  return async (key: string, submit: () => Promise<unknown>, ui: {
    state: (key: string, state: MeasurementDecisionState) => void;
    saved: () => void;
  }): Promise<void> => {
    if (pending.has(key)) return;
    pending.add(key);
    ui.state(key, { kind: 'saving' });
    try {
      await submit();
      ui.state(key, { kind: 'saved' });
      ui.saved();
    } catch (caught) {
      ui.state(key, { kind: 'error', message: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      pending.delete(key);
    }
  };
}
