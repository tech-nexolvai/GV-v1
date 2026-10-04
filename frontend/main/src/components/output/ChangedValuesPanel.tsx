import type { components } from '../../api/schema';

type ChangedValues = components['schemas']['ChangedValuesOut'];

interface Props {
  value: ChangedValues | null;
  state: 'loading' | 'error' | 'ready';
  currentRevisionId: string | null;
}

/** A presentation of the pinned check-run report, never the newest settings. */
export function ChangedValuesPanel({ value, state, currentRevisionId }: Props) {
  let content: React.ReactNode;
  if (state === 'loading') {
    content = <p>Loading recorded check-run values…</p>;
  } else if (state === 'error' || value === null) {
    content = <p role="alert">The recorded check-run values could not be loaded.</p>;
  } else if (currentRevisionId === null || value.revision_id !== currentRevisionId) {
    content = <p>Check-run values belong to another revision. Refresh this review.</p>;
  } else if (value.message !== null) {
    content = <p>{value.message}</p>;
  } else {
    content = <>
      {value.company_standards_displaced.length ? (
        <ul>{value.company_standards_displaced.map(item => <li key={item}>{item}</li>)}</ul>
      ) : <p>None</p>}
      <h3>Required values not set</h3>
      {value.outstanding.length ? (
        <ul>{value.outstanding.map(item => <li key={item}>{item}</li>)}</ul>
      ) : <p>None</p>}
    </>;
  }
  return <section className="review-page__changed-values" aria-label="Project values for this check run">
    <h2>Project values that differ from GV standards</h2>
    {content}
  </section>;
}
