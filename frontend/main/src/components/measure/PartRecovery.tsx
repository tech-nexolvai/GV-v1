export function PartsLoadState({ error, loading, hasDrawings, onRetry }: {
  error: string | null; loading: boolean; hasDrawings: boolean; onRetry: () => void;
}) {
  if (!error) return loading ? <p className="enter-values__hint" role="status">Loading the parts of these drawings…</p> : null;
  return <div className="enter-values__error">
    <p role="alert">The parts of these drawings could not be listed: {error}</p>
    {hasDrawings && <p>The last loaded list remains below. Retry to see the latest recorded decisions.</p>}
    <button type="button" className="value-secondary" disabled={loading} onClick={onRetry}>
      {loading ? 'Loading parts…' : 'Retry parts list'}
    </button>
  </div>;
}

export type PartImageState = { url: string } | { error: true } | null;

/** A stored code crop, not an invented view of the whole part. Retry never decides a part. */
export function PartPicture({ state, position, page, onRetry, onError }: {
  state: PartImageState; position: number; page: number; onRetry: () => void; onError: () => void;
}) {
  if (state && 'error' in state) return <div>
    <p className="drawing-parts__no-picture" role="alert">The stored code crop for part {position} on page {page} could not be loaded.</p>
    <button type="button" className="value-secondary" onClick={onRetry}>Retry part {position} crop</button>
  </div>;
  if (!state) return <p className="drawing-parts__no-picture" role="status">Loading the picture…</p>;
  return <img className="drawing-parts__crop" src={state.url} onError={onError}
    alt={`Where the code is printed on part ${position}, page ${page}`} />;
}
