import type { MeasurementDecisionState } from './measurementDecisionSave.js';

export function DrawingRoleFeedback({ state }: { state?: MeasurementDecisionState }) {
  if (!state) return null;
  if (state.kind === 'error') return <p className="drawing-roles__feedback enter-values__error" role="alert">
    Drawing role save was not confirmed. {state.message} Check the message before choosing again.
  </p>;
  return <p className="drawing-roles__feedback enter-values__hint" role="status">
    {state.kind === 'saving' ? 'Saving this drawing’s role… Waiting for the server.' : 'Drawing role saved.'}
  </p>;
}

export function DrawingRolesLoadState({ error, loading, onRetry }: {
  error: string | null; loading: boolean; onRetry: () => void;
}) {
  if (!error) return loading ? <p className="enter-values__hint" role="status">Loading drawing roles…</p> : null;
  return <div>
    <p className="enter-values__error" role="alert">The drawings on these sheets could not be listed: {error}</p>
    <button type="button" className="btn btn--sm btn--subtle" disabled={loading} onClick={onRetry}>
      {loading ? 'Retrying drawing list…' : 'Retry drawing list'}
    </button>
  </div>;
}
