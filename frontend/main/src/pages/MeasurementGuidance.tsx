import { AlertTriangle } from 'lucide-react';

/** Presentation only: never edits, confirms, or qualifies a reading. */
export function MeasurementGuidance({ rulesPublished }: { rulesPublished: number }) {
  return (
    <header className="enter-values__head">
      <h1>Review measurements</h1>
      <p className="enter-values__instruction">
        Check suggested values against their drawing crops. Fill missing values from the drawings,
        then choose <strong>Run checks</strong>.
      </p>
      <p>
        Include the unit: <code>25 1/2&quot;</code> or <code>648 mm</code>. Values without units are refused.
      </p>
      <details className="measure-guidance">
        <summary>How these values are used</summary>
        <p>
          The {rulesPublished} published rules determine the fields shown here. The server parses
          values exactly; the deterministic rules decide the results, not the AI.
        </p>
        <p>
          <strong>Save values</strong> saves the form without running checks. <strong>Run checks</strong>
          {' '}saves first and queues the checks only if saving succeeds. Missing or uncertain inputs
          can leave a check undecided.
        </p>
      </details>
    </header>
  );
}

export function StoredProposalGuidance({ count, unverifiedCount }: { count: number; unverifiedCount: number }) {
  return (
    <>
      <p>
        {count} field{count === 1 ? '' : 's'} below {count === 1 ? 'has' : 'have'} an AI suggestion.
        Check each crop and value, correct anything wrong, then save. A suggestion is not a confirmed measurement.
      </p>
      {unverifiedCount > 0 && (
        <p className="measure-fill__caveat">
          <AlertTriangle size={13} aria-hidden="true" />
          <span>
            Placement is unverified for {unverifiedCount} field{unverifiedCount === 1 ? '' : 's'}.
            The geometry check did not establish which dimension each number belongs to.
            Inspect the crop before keeping the suggestion.
          </span>
        </p>
      )}
    </>
  );
}
