import { useState } from 'react';
import { Plus, Play, Trash2 } from 'lucide-react';
import { createPackage, enterMeasurements, requestChecks } from '../api/client';
import { ApiError } from '../api/client';
import { projectId } from '../api/config';
import './EnterValuesPage.css';

/**
 * The reviewer types the dimensions, and the deterministic engine decides.
 *
 * `CLIENT_FACTS` Q7: *"the reviewer types the values into input fields for that drawing set"*. This
 * is those fields. There is no AI here and no drawing — the reading half of the product is a person,
 * which is the sanctioned arrangement rather than a stand-in: a reviewer's own reading is
 * HUMAN_CONFIRMED, and the evidence gate accepts that.
 *
 * **Nothing on this page does arithmetic on a value.** The strings go to the server exactly as typed
 * and are parsed there by the same code that reads a drawing. JavaScript has no exact rational, and
 * under exact match (Q2) there is no tolerance band to absorb a rounding error — so a number this
 * file converted could already be a different verdict. `api/fractions.ts` makes the same argument for
 * the outbound direction.
 *
 * **A value with no unit is refused by the server, and that refusal is shown rather than smoothed.**
 * `984` with no unit was once recorded as 984 inches — 82 feet — because tokenisation had removed its
 * `mm`. The reviewer is told which field and why.
 */

/** One row of the form. `ruleId` empty means a project parameter rather than a measurement. */
interface Row {
  key: number;
  ruleId: string;
  name: string;
  value: string;
}

let nextKey = 1;

function row(ruleId = '', name = '', value = ''): Row {
  return { key: nextKey++, ruleId, name, value };
}

/**
 * What a countertop-depth check needs, as the starting rows.
 *
 * Prefilled with the *names* the rulebook declares and no values — a starting point a reviewer edits,
 * never a number nobody supplied. `CT-DEPTH-001` compares the shop depth against
 * `cabinet_depth + countertop_overhang`, so those three are what it takes to reach a verdict rather
 * than an abstention.
 */
const STARTING_PARAMETERS = [row('', 'cabinet_depth'), row('', 'countertop_overhang')];
const STARTING_MEASUREMENTS = [row('CT-DEPTH-001', 'countertop_depth')];

export function EnterValuesPage({ onDone }: { onDone?: (packageId: string) => void }) {
  const [vendor, setVendor] = useState('');
  const [parameters, setParameters] = useState<Row[]>(STARTING_PARAMETERS);
  const [measurements, setMeasurements] = useState<Row[]>(STARTING_MEASUREMENTS);
  const [packageId, setPackageId] = useState<string | null>(null);
  const [stored, setStored] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<string | null>(null);

  const filled = (rows: Row[]) => rows.filter((r) => r.name.trim() && r.value.trim());

  async function onSave() {
    setBusy(true);
    setError(null);
    setAccepted(null);
    try {
      // Created on first save rather than up front, so a reviewer who opens the page and leaves does
      // not litter the project with empty packages.
      const id = packageId ?? (await createPackage(projectId(), vendor.trim() || null)).id;
      setPackageId(id);

      const result = await enterMeasurements(projectId(), id, {
        parameters: filled(parameters).map((r) => ({ name: r.name.trim(), value: r.value.trim() })),
        measurements: filled(measurements).map((r) => ({
          rule_id: r.ruleId.trim(),
          name: r.name.trim(),
          value: r.value.trim(),
        })),
      });

      // Echo the *parse*, not the input. `25.5"` and `25 1/2"` are the same value and a reviewer
      // should be able to see that the system agrees — which is also how a typo in the unit shows up.
      setStored([
        ...result.parameters.map((v) => `${v.name} = ${v.numerator}/${v.denominator} ${v.unit}`),
        ...result.measurements.map((v) => `${v.name} = ${v.numerator}/${v.denominator} ${v.unit}`),
      ]);
    } catch (caught) {
      // The server's own message, verbatim. It names the field and says what to do about it; a
      // rewritten "invalid input" would lose both.
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function onRunChecks() {
    if (!packageId) return;
    setBusy(true);
    setError(null);
    try {
      const response = await requestChecks(projectId(), packageId);
      setAccepted(response.accepted_id);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const editor = (
    rows: Row[],
    setRows: (rows: Row[]) => void,
    { withRule }: { withRule: boolean },
  ) => (
    <>
      {rows.map((r, index) => (
        <div className="value-row" key={r.key}>
          {withRule && (
            <input
              className="value-input value-input--rule"
              aria-label={`Check for row ${index + 1}`}
              placeholder="CT-DEPTH-001"
              value={r.ruleId}
              onChange={(e) =>
                setRows(rows.map((x) => (x.key === r.key ? { ...x, ruleId: e.target.value } : x)))
              }
            />
          )}
          <input
            className="value-input"
            aria-label={`Name for row ${index + 1}`}
            placeholder="countertop_depth"
            value={r.name}
            onChange={(e) =>
              setRows(rows.map((x) => (x.key === r.key ? { ...x, name: e.target.value } : x)))
            }
          />
          <input
            className="value-input value-input--value"
            aria-label={`Value for row ${index + 1}, with its unit`}
            placeholder={'25 1/2" or 648 mm'}
            value={r.value}
            onChange={(e) =>
              setRows(rows.map((x) => (x.key === r.key ? { ...x, value: e.target.value } : x)))
            }
          />
          <button
            type="button"
            className="value-remove"
            aria-label={`Remove row ${index + 1}`}
            onClick={() => setRows(rows.filter((x) => x.key !== r.key))}
          >
            <Trash2 size={14} aria-hidden="true" />
          </button>
        </div>
      ))}
      <button type="button" className="value-add" onClick={() => setRows([...rows, row()])}>
        <Plus size={14} aria-hidden="true" /> Add a row
      </button>
    </>
  );

  return (
    <div className="enter-values">
      <header className="enter-values__head">
        <h1>Enter measurements</h1>
        <p>
          Type each value with its unit — <code>25 1/2&quot;</code> or <code>648 mm</code>. The
          numbers are parsed exactly and compared by the rule engine; nothing is rounded and nothing
          is inferred. A value with no unit is refused rather than guessed at.
        </p>
      </header>

      <section className="enter-values__section">
        <h2>Package</h2>
        <input
          className="value-input value-input--wide"
          aria-label="Vendor"
          placeholder="Vendor (optional)"
          value={vendor}
          onChange={(e) => setVendor(e.target.value)}
          disabled={packageId !== null}
        />
        {packageId && <p className="enter-values__note">Package {packageId}</p>}
      </section>

      <section className="enter-values__section">
        <h2>Project parameters</h2>
        <p className="enter-values__hint">
          Settings for this job — a cabinet depth, an overhang. Recorded against the project, with you
          as the person who supplied them.
        </p>
        {editor(parameters, setParameters, { withRule: false })}
      </section>

      <section className="enter-values__section">
        <h2>Measurements</h2>
        <p className="enter-values__hint">
          Dimensions you read off the drawing, and which check each one belongs to. Recorded against
          this review only, because a measurement is true of the day it was taken.
        </p>
        {editor(measurements, setMeasurements, { withRule: true })}
      </section>

      {error && (
        <div className="enter-values__error" role="alert">
          {error}
        </div>
      )}

      {stored.length > 0 && (
        <section className="enter-values__section" aria-live="polite">
          <h2>Stored, as the system read them</h2>
          <ul className="enter-values__stored">
            {stored.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      )}

      <footer className="enter-values__actions">
        <button type="button" className="value-primary" onClick={onSave} disabled={busy}>
          {packageId ? 'Save values' : 'Create package and save'}
        </button>
        <button
          type="button"
          className="value-primary"
          onClick={onRunChecks}
          disabled={busy || !packageId}
        >
          <Play size={14} aria-hidden="true" /> Run checks
        </button>
        {packageId && onDone && (
          <button type="button" className="value-secondary" onClick={() => onDone(packageId)}>
            See findings
          </button>
        )}
      </footer>

      {accepted && (
        <p className="enter-values__note" role="status">
          Request recorded ({accepted.slice(0, 8)}). <strong>Nothing has run yet</strong> — the API
          accepts the request and a worker does the work, so the findings appear once it has. Run{' '}
          <code>python scripts/drain_outbox.py</code> if no worker is running.
        </p>
      )}
    </div>
  );
}
