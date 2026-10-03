import type { MeasurementDecisionState } from './measurementDecisionSave.js';

export function CountertopRunFeedback({ state }: { state?: MeasurementDecisionState }) {
  if (!state) return null;
  if (state.kind === 'error') return <p className="drawing-parts__feedback enter-values__error" role="alert">
    Run decision was not confirmed. {state.message} Your ticked parts are kept. Check the message before trying again.
  </p>;
  return <p className="drawing-parts__feedback enter-values__hint" role="status">
    {state.kind === 'saving' ? 'Saving this countertop’s run… Waiting for the server.' : 'Run decision saved.'}
  </p>;
}

export function CountertopRunsLoadState({ error, loading, hasDrawings, onRetry }: {
  error: string | null; loading: boolean; hasDrawings: boolean; onRetry: () => void;
}) {
  if (error) return <div className="enter-values__error">
    <p role="alert">The runs under each countertop could not be listed: {error}</p>
    {hasDrawings && <p>The previous list is still shown. Refresh it before making another run decision.</p>}
    <button className="btn btn--sm btn--subtle" type="button" disabled={loading} onClick={onRetry}>
      {loading ? 'Retrying countertop runs…' : 'Retry countertop runs'}
    </button>
  </div>;
  return loading && !hasDrawings ? <p className="enter-values__hint" role="status">Loading parts under each countertop…</p> : null;
}
