import type { MeasurementDecisionState as PartDecisionState } from './measurementDecisionSave.js';

export function PartDecisionFeedback({ state }: { state?: PartDecisionState }) {
  if (!state) return null;
  if (state.kind === 'error') return <p className="drawing-parts__feedback enter-values__error" role="alert">
    Save was not confirmed. {state.message} Your entered code has been kept. Review the message before trying again.
  </p>;
  return <p className="drawing-parts__feedback enter-values__hint" role="status">
    {state.kind === 'saving' ? 'Saving this decision… Waiting for the server.' : 'Decision saved.'}
  </p>;
}
