import './ThinkingStream.css';

/** Pending until the server sends recorded facts; no timer can claim an unreported stage. */
export function ThinkingStream() {
  return (
    <div className="thinking" role="status" aria-live="polite">
      <span className="thinking__pulse" aria-hidden="true" />
      <span>Waiting for recorded findings…</span>
    </div>
  );
}
