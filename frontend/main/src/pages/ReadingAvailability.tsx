export function ReadingAvailability({ error, retrying, onRetry }: {
  error: string;
  retrying: boolean;
  onRetry: () => void;
}) {
  return (
    <section className="enter-values__error" aria-label="Drawing readings unavailable">
      <p role="alert"><strong>Drawing readings could not be loaded.</strong> {error}</p>
      <p>The required fields and recorded confirmations are still available. Enter missing values
        from the drawings, or retry loading the readings. An unavailable list does not mean no dimensions were read.</p>
      <button type="button" className="value-secondary" disabled={retrying} onClick={onRetry}>
        {retrying ? 'Loading readings…' : 'Retry readings'}
      </button>
    </section>
  );
}
