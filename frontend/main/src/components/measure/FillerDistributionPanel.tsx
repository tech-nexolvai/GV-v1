import { useMemo, useState } from 'react';
import { AlertTriangle, Calculator, CheckCircle2 } from 'lucide-react';

import {
  buildFillerDistributionRequest,
  type DistributionParameter,
  type DistributionQuantity,
  type FillerDistributionRequest,
  type FillerDistributionResponse,
} from './fillerDistribution.js';
import type { components } from '../../api/schema';

type QuantityOut = components['schemas']['app__schemas__distribution__QuantityOut'];

const DESIGN_CABINET_KEY = 'ARCH:cabinet_width';
const DESIGN_FILLER_KEY = 'ARCH:filler_width';

function quantityDisplay(quantity: QuantityOut | null | undefined): string {
  return quantity?.display ?? 'not supplied';
}

function valueRun(
  quantities: DistributionQuantity[],
  singles: Record<string, string>,
  runs: Record<string, string[]>,
  key: string,
): string[] {
  const quantity = quantities.find((item) => item.key === key);
  if (!quantity) return [];
  return quantity.many ? (runs[key] ?? []) : [singles[key] ?? ''];
}

function parameterValue(
  parameters: DistributionParameter[],
  singles: Record<string, string>,
  name: string,
): string {
  const typed = (singles[name] ?? '').trim();
  if (typed) return typed;
  const parameter = parameters.find((item) => item.name === name && !item.blocked);
  return parameter?.declared_default ?? '';
}

export function FillerDistributionPanel({
  quantities,
  parameters,
  singles,
  runs,
  fieldWidth,
  onFieldWidthChange,
  onCalculate,
  initialResult = null,
}: {
  quantities: DistributionQuantity[];
  parameters: DistributionParameter[];
  singles: Record<string, string>;
  runs: Record<string, string[]>;
  fieldWidth: string;
  onFieldWidthChange: (value: string) => void;
  onCalculate: (request: FillerDistributionRequest) => Promise<FillerDistributionResponse>;
  initialResult?: FillerDistributionResponse | null;
}) {
  const [selectedCabinet, setSelectedCabinet] = useState('');
  const [result, setResult] = useState<FillerDistributionResponse | null>(initialResult);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const built = useMemo(
    () =>
      buildFillerDistributionRequest({
        cabinetWidths: valueRun(quantities, singles, runs, DESIGN_CABINET_KEY),
        fillerWidths: valueRun(quantities, singles, runs, DESIGN_FILLER_KEY),
        fieldWidth,
        fillerMin: parameterValue(parameters, singles, 'filler_min'),
        fillerMax: parameterValue(parameters, singles, 'filler_max'),
        adjustableCabinetId: selectedCabinet || null,
      }),
    [fieldWidth, parameters, quantities, runs, selectedCabinet, singles],
  );

  async function calculate() {
    if (built.request === null) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await onCalculate(built.request));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The distribution could not be calculated.');
    } finally {
      setBusy(false);
    }
  }

  const cabinetOptions = built.request?.assembly.cabinets ?? [];

  return (
    <section className="enter-values__section distribution-panel" aria-labelledby="distribution-heading">
      <div className="distribution-panel__head">
        <div>
          <h2 id="distribution-heading">Filler distribution</h2>
          <p className="enter-values__hint">
            Enter the site field width and choose the cabinet a reviewer allows to move. The
            proposal below is returned by the backend calculation.
          </p>
        </div>
        <Calculator size={18} aria-hidden="true" />
      </div>

      <div className="distribution-panel__grid">
        <label className="value-field distribution-panel__field" htmlFor="distribution-field-width">
          <span className="value-label">
            <span className="value-name">Site field width</span>
            <span className="value-source">measured on site</span>
          </span>
          <input
            className="value-input value-input--wide"
            id="distribution-field-width"
            placeholder={'96 1/2" or 2451 mm'}
            value={fieldWidth}
            onChange={(event) => onFieldWidthChange(event.target.value)}
          />
        </label>

        <label className="value-field distribution-panel__field" htmlFor="distribution-cabinet">
          <span className="value-label">
            <span className="value-name">Adjustable cabinet</span>
            <span className="value-source">reviewer choice</span>
          </span>
          <select
            className="value-input value-input--wide"
            id="distribution-cabinet"
            value={selectedCabinet}
            onChange={(event) => setSelectedCabinet(event.target.value)}
          >
            <option value="">No cabinet selected</option>
            {cabinetOptions.map((cabinet, index) => (
              <option key={cabinet.id} value={cabinet.id}>
                Cabinet {index + 1} — {cabinet.width}
              </option>
            ))}
          </select>
        </label>
      </div>

      {built.missing.length > 0 && (
        <p className="distribution-panel__missing" role="status">
          <AlertTriangle size={14} aria-hidden="true" />
          Enter {built.missing.join(', ')} above before asking for a distribution.
        </p>
      )}

      <button
        type="button"
        className="value-primary"
        disabled={busy || built.request === null}
        onClick={() => void calculate()}
      >
        <Calculator size={14} aria-hidden="true" />
        {busy ? 'Calculating…' : 'Calculate distribution'}
      </button>

      {error && (
        <div className="enter-values__error" role="alert">
          {error}
        </div>
      )}

      {result && (
        <div className="distribution-result" data-outcome={result.outcome}>
          <div className="distribution-result__message" role="status">
            {result.outcome === 'PASS' ? (
              <CheckCircle2 size={15} aria-hidden="true" />
            ) : (
              <AlertTriangle size={15} aria-hidden="true" />
            )}
            <strong>{result.outcome === 'PASS' ? 'Proposal returned' : 'Could not propose'}</strong>
            <span>{result.message}</span>
          </div>

          <dl className="distribution-result__facts">
            <div>
              <dt>Design width</dt>
              <dd>{quantityDisplay(result.design_width)}</dd>
            </div>
            <div>
              <dt>Site difference</dt>
              <dd>{quantityDisplay(result.site_difference)}</dd>
            </div>
            <div>
              <dt>Selected cabinet</dt>
              <dd>{result.selected_adjustable_cabinet_id ?? 'none selected'}</dd>
            </div>
          </dl>

          <table className="distribution-result__table">
            <thead>
              <tr>
                <th scope="col">Part</th>
                <th scope="col">Original</th>
                <th scope="col">Proposed</th>
              </tr>
            </thead>
            <tbody>
              {result.fillers.map((filler) => (
                <tr key={filler.id}>
                  <th scope="row">{filler.id}</th>
                  <td>{quantityDisplay(filler.original)}</td>
                  <td>{quantityDisplay(filler.proposed)}</td>
                </tr>
              ))}
              {result.cabinets.map((cabinet) => (
                <tr key={cabinet.id} data-adjustable={cabinet.adjustable}>
                  <th scope="row">
                    {cabinet.id}
                    {cabinet.adjustable ? ' (adjusted)' : ''}
                  </th>
                  <td>{quantityDisplay(cabinet.original)}</td>
                  <td>{quantityDisplay(cabinet.proposed)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="distribution-result__calculation">{result.calculation}</p>
        </div>
      )}
    </section>
  );
}
