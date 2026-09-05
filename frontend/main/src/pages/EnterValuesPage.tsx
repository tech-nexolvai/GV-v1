import { useEffect, useState } from 'react';
import { Plus, Play, Trash2, AlertTriangle } from 'lucide-react';
import {
  ApiError,
  createPackage,
  enterMeasurements,
  getRequiredInputs,
  requestChecks,
} from '../api/client';
import { projectId } from '../api/config';
import './EnterValuesPage.css';

/**
 * The reviewer types the dimensions, and the deterministic engine decides.
 *
 * `CLIENT_FACTS` Q7: *"the reviewer types the values into input fields for that drawing set"*. There
 * is no AI here and no drawing — the reading half of the product is a person, which is the sanctioned
 * arrangement rather than a stand-in: a reviewer's own reading is HUMAN_CONFIRMED, and the evidence
 * gate accepts that.
 *
 * **Every field comes from the server, and that is what makes the form complete.** A list of fields
 * written here would be right today and silently wrong the first time a rule gained an input — the
 * check would then abstain for a reason the reviewer could not act on, indistinguishable from a
 * genuine missing dimension. `GET .../required-inputs` derives the fields from the published rules,
 * so a rule that gains an input gains a field.
 *
 * **Nothing on this page does arithmetic on a value.** The strings go to the server exactly as typed
 * and are parsed there by the same code that reads a drawing. JavaScript has no exact rational, and
 * under exact match (Q2) there is no tolerance band to absorb a rounding error, so a number this file
 * converted could already be a different verdict.
 */

type Quantity = {
  key: string;
  semantic_type: string;
  source: string;
  many: boolean;
  consumers: { rule_id?: string; input_name?: string }[];
};
type Parameter = {
  name: string;
  scope: string;
  rule_ids: string[];
  declared_default: string | null;
  blocked: boolean;
};
type Discriminator = { name: string; rule_ids: string[]; choices: string[] };
/** One entry on the wire. Exactly one of `value` or `values`, which the server also enforces. */
type MeasurementEntry = {
  rule_id: string;
  name: string;
  value?: string;
  values?: string[];
};
type Needed = {
  quantities: Quantity[];
  parameters: Parameter[];
  discriminators: Discriminator[];
  rules_published: number;
};

/** Which sheet a measurement is read from, in the words a reviewer uses. */
const SOURCE_LABEL: Record<string, string> = {
  SHOP: 'shop drawing',
  ARCH: 'architectural drawing',
  USER_INPUT: 'measured on site',
  PRODUCT_SPEC: 'product specification',
};

export function EnterValuesPage({ onDone }: { onDone?: (packageId: string) => void }) {
  const [needed, setNeeded] = useState<Needed | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [vendor, setVendor] = useState('');
  const [packageId, setPackageId] = useState<string | null>(null);
  /** Single-valued quantities and parameters, keyed by quantity key or parameter name. */
  const [singles, setSingles] = useState<Record<string, string>>({});
  /** Many-valued quantities, in layout order. */
  const [runs, setRuns] = useState<Record<string, string[]>>({});
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [stored, setStored] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<string | null>(null);

  // The package is created on first save, so the fields have to be fetchable before one exists. A
  // throwaway package purely to read the rulebook would litter the project with empties.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const bootstrap = packageId ?? (await createPackage(projectId(), null)).id;
        const fields = await getRequiredInputs(projectId(), bootstrap);
        if (cancelled) return;
        setPackageId(bootstrap);
        setNeeded(fields as unknown as Needed);
        setRuns(
          Object.fromEntries(
            (fields as unknown as Needed).quantities.filter((q) => q.many).map((q) => [q.key, ['']]),
          ),
        );
      } catch (caught) {
        if (!cancelled) {
          setLoadError(caught instanceof ApiError ? caught.message : String(caught));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // Once. Re-fetching on every keystroke would replace the reviewer's rows mid-edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setRun = (key: string, index: number, value: string) =>
    setRuns((prior) => ({
      ...prior,
      [key]: (prior[key] ?? ['']).map((v, i) => (i === index ? value : v)),
    }));

  async function onSave() {
    if (!packageId || !needed) return;
    setBusy(true);
    setError(null);
    setAccepted(null);
    try {
      // **One typed value fans out to every rule input it feeds.** The mapping is the server's, taken
      // from `consumers` — a reviewer measures the front offset once, and three rules receive it.
      const measurements = needed.quantities.flatMap<MeasurementEntry>((quantity) => {
        const consumers = quantity.consumers.filter((c) => c.rule_id && c.input_name);
        if (quantity.many) {
          const values = (runs[quantity.key] ?? []).map((v) => v.trim()).filter(Boolean);
          if (!values.length) return [];
          return consumers.map((c) => ({
            rule_id: c.rule_id as string,
            name: c.input_name as string,
            values,
          }));
        }
        const value = (singles[quantity.key] ?? '').trim();
        if (!value) return [];
        return consumers.map((c) => ({
          rule_id: c.rule_id as string,
          name: c.input_name as string,
          value,
        }));
      });

      const parameters = needed.parameters
        .filter((p) => !p.blocked && (singles[p.name] ?? '').trim())
        .map((p) => ({
          name: p.name,
          value: (singles[p.name] ?? '').trim(),
          scope: (p.scope === 'run' ? 'run' : 'project') as 'run' | 'project',
        }));

      const result = await enterMeasurements(projectId(), packageId, { parameters, measurements });
      // Echo the parse, not the input: `25.5"` and `25 1/2"` are the same value and a reviewer should
      // see that the system agrees — which is also how a mistyped unit becomes visible.
      setStored([
        ...result.parameters.map((v) => `${v.name} = ${v.numerator}/${v.denominator} ${v.unit}`),
        ...result.measurements.map((v) => `${v.name} = ${v.numerator}/${v.denominator} ${v.unit}`),
        ...(result.lists ?? []).map(
          (l) => `${l.name} = [${l.values.map((v) => `${v.numerator}/${v.denominator}`).join(', ')}]`,
        ),
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
      const response = await requestChecks(projectId(), packageId, choices);
      setAccepted(response.accepted_id);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return (
      <div className="enter-values">
        <div className="enter-values__error" role="alert">
          {loadError}
        </div>
      </div>
    );
  }
  if (!needed) {
    return (
      <div className="enter-values">
        <p className="enter-values__hint">Reading what the rulebook needs…</p>
      </div>
    );
  }

  return (
    <div className="enter-values">
      <header className="enter-values__head">
        <h1>Enter measurements</h1>
        <p>
          Type each value with its unit — <code>25 1/2&quot;</code> or <code>648 mm</code>. Values are
          parsed exactly and compared by the rule engine; nothing is rounded and nothing is inferred.
          A value with no unit is refused rather than guessed at.
        </p>
        <p className="enter-values__hint">
          These fields come from the {needed.rules_published} published rules, so a check can only
          fail to decide for a reason you can see — never because a field was missing.
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
        />
      </section>

      <section className="enter-values__section">
        <h2>Measurements</h2>
        <p className="enter-values__hint">
          Each one is read once, even where several checks use it. The sheet to read it from is named
          beside the field.
        </p>
        {needed.quantities.map((quantity) => (
          <div className="value-field" key={quantity.key}>
            <label className="value-label" htmlFor={`q-${quantity.key}`}>
              {quantity.semantic_type}
              <span className="value-source">{SOURCE_LABEL[quantity.source] ?? quantity.source}</span>
              <span className="value-feeds">
                {quantity.consumers.map((c) => c.rule_id).join(', ')}
              </span>
            </label>
            {quantity.many ? (
              <>
                {(runs[quantity.key] ?? ['']).map((value, index) => (
                  <div className="value-row" key={index}>
                    <input
                      className="value-input"
                      id={index === 0 ? `q-${quantity.key}` : undefined}
                      aria-label={`${quantity.semantic_type}, item ${index + 1}, left to right`}
                      placeholder={'24"'}
                      value={value}
                      onChange={(e) => setRun(quantity.key, index, e.target.value)}
                    />
                    <button
                      type="button"
                      className="value-remove"
                      aria-label={`Remove item ${index + 1} from ${quantity.semantic_type}`}
                      onClick={() =>
                        setRuns((prior) => ({
                          ...prior,
                          [quantity.key]: (prior[quantity.key] ?? []).filter((_, i) => i !== index),
                        }))
                      }
                    >
                      <Trash2 size={14} aria-hidden="true" />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="value-add"
                  onClick={() =>
                    setRuns((prior) => ({ ...prior, [quantity.key]: [...(prior[quantity.key] ?? []), ''] }))
                  }
                >
                  <Plus size={14} aria-hidden="true" /> Add another
                </button>
                <p className="enter-values__hint enter-values__hint--tight">
                  In order, left to right — two runs are compared position by position.
                </p>
              </>
            ) : (
              <input
                className="value-input value-input--wide"
                id={`q-${quantity.key}`}
                placeholder={'25 1/2" or 648 mm'}
                value={singles[quantity.key] ?? ''}
                onChange={(e) =>
                  setSingles((prior) => ({ ...prior, [quantity.key]: e.target.value }))
                }
              />
            )}
          </div>
        ))}
      </section>

      <section className="enter-values__section">
        <h2>Settings</h2>
        <p className="enter-values__hint">
          Values for this job rather than dimensions off a drawing. Where the rulebook suggests one it
          is shown — a rule author&apos;s stand-in, not a number the client has confirmed.
        </p>
        {needed.parameters.map((parameter) => (
          <div className="value-field" key={parameter.name}>
            <label className="value-label" htmlFor={`p-${parameter.name}`}>
              {parameter.name}
              <span className="value-source">
                {parameter.scope === 'run' ? 'this review only' : 'this project'}
              </span>
              <span className="value-feeds">{parameter.rule_ids.join(', ')}</span>
            </label>
            {parameter.blocked ? (
              <p className="value-blocked" role="note">
                <AlertTriangle size={14} aria-hidden="true" /> Waiting on the vendor. This check will
                report that it could not decide, which is the correct answer until the value arrives —
                it is not a field to fill in.
              </p>
            ) : (
              <>
                <input
                  className="value-input value-input--wide"
                  id={`p-${parameter.name}`}
                  placeholder={parameter.declared_default ?? '24"'}
                  value={singles[parameter.name] ?? ''}
                  onChange={(e) =>
                    setSingles((prior) => ({ ...prior, [parameter.name]: e.target.value }))
                  }
                />
                {parameter.declared_default && (
                  <p className="enter-values__hint enter-values__hint--tight">
                    The rulebook suggests <code>{parameter.declared_default}</code>. Leave blank to use
                    it, or type the value this job actually uses.
                  </p>
                )}
              </>
            )}
          </div>
        ))}
      </section>

      {needed.discriminators.length > 0 && (
        <section className="enter-values__section">
          <h2>Layout</h2>
          <p className="enter-values__hint">
            What the drawing shows. A check whose layout nobody states cannot choose which version of
            itself applies, and reports that instead of a verdict.
          </p>
          {needed.discriminators.map((discriminator) => (
            <div className="value-field" key={discriminator.name}>
              <label className="value-label" htmlFor={`d-${discriminator.name}`}>
                {discriminator.name}
                <span className="value-feeds">{discriminator.rule_ids.join(', ')}</span>
              </label>
              <select
                className="value-input value-input--wide"
                id={`d-${discriminator.name}`}
                value={choices[discriminator.name] ?? ''}
                onChange={(e) =>
                  setChoices((prior) => ({ ...prior, [discriminator.name]: e.target.value }))
                }
              >
                <option value="">Not stated</option>
                {discriminator.choices.map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </section>
      )}

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
          Save values
        </button>
        <button type="button" className="value-primary" onClick={onRunChecks} disabled={busy}>
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
