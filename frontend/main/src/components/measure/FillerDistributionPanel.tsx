import { useMemo, useRef, useState } from 'react';
import { AlertTriangle, Calculator, CheckCircle2 } from 'lucide-react';

import {
  buildFillerDistributionRequest,
  CABINET_BOUND_NAMES,
  CABINET_TYPE_LABELS,
  type CabinetBoundName,
  type CabinetTypeName,
  type DistributionParameter,
  type DistributionQuantity,
  type FillerDistributionRequest,
  type FillerDistributionResponse,
} from './fillerDistribution.js';
import { ElevationDiagram } from '../output/ElevationDiagram.js';
import { buildElevation } from '../output/elevation.js';
import { DistributionProvenance } from './DistributionProvenance.js';
import { distributionInputKey, distributionIsCurrent, type DistributionSnapshot } from './distributionSnapshot.js';


const DESIGN_CABINET_KEY = 'ARCH:cabinet_width';
const DESIGN_FILLER_KEY = 'ARCH:filler_width';

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
  // The reviewer's classification, one per cabinet, indexed by position in the run. Empty until
  // they choose: nothing here guesses a type from a width, and the request is withheld until every
  // cabinet has one.
  const [cabinetTypes, setCabinetTypes] = useState<CabinetTypeName[]>([]);
  const [snapshot, setSnapshot] = useState<DistributionSnapshot | null>(() => initialResult
    ? { result: initialResult, request: null, inputKey: null } : null);
  const result = snapshot?.result ?? null;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);

  const cabinetWidths = useMemo(
    () => valueRun(quantities, singles, runs, DESIGN_CABINET_KEY).filter((width) => width.trim()),
    [quantities, runs, singles],
  );

  const built = useMemo(
    () =>
      buildFillerDistributionRequest({
        cabinetWidths,
        cabinetTypes: cabinetTypes.slice(0, cabinetWidths.length),
        fillerWidths: valueRun(quantities, singles, runs, DESIGN_FILLER_KEY),
        fieldWidth,
        fillerMin: parameterValue(parameters, singles, 'filler_min'),
        fillerMax: parameterValue(parameters, singles, 'filler_max'),
        cabinetBounds: Object.fromEntries(
          CABINET_BOUND_NAMES.map((name) => [name, parameterValue(parameters, singles, name)]),
        ) as Record<CabinetBoundName, string>,
      }),
    [cabinetTypes, cabinetWidths, fieldWidth, parameters, quantities, runs, singles],
  );

  async function calculate() {
    if (built.request === null || pending.current) return;
    const request = built.request;
    const inputKey = distributionInputKey(request);
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      const response = await onCalculate(request);
      setSnapshot({ result: response, request, inputKey });
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : 'The distribution could not be calculated.',
      );
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  return (
    <section
      className="enter-values__section distribution-panel"
      aria-labelledby="distribution-heading"
    >
      <div className="distribution-panel__head">
        <div>
          <h2 id="distribution-heading">Filler distribution</h2>
          <p className="enter-values__hint">
            Preview a distribution using the approved cabinet and filler widths above, the site
            field width, and the limits in Settings. This preview does not save measurements.
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

        {cabinetWidths.map((width, index) => (
          <label
            className="value-field distribution-panel__field"
            htmlFor={`distribution-cabinet-type-${index}`}
            key={`cabinet-${index + 1}`}
          >
            <span className="value-label">
              <span className="value-name">
                Cabinet {index + 1} — {width}
              </span>
              <span className="value-source">reviewer classification</span>
            </span>
            <select
              className="value-input value-input--wide"
              id={`distribution-cabinet-type-${index}`}
              value={cabinetTypes[index] ?? ''}
              onChange={(event) =>
                setCabinetTypes((current) => {
                  const next = [...current];
                  next[index] = event.target.value as CabinetTypeName;
                  return next;
                })
              }
            >
              <option value="">Not classified</option>
              {CABINET_TYPE_LABELS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        ))}
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

      {result && snapshot && (
        <div className="distribution-result" data-outcome={result.outcome}>
          <DistributionProvenance request={snapshot.request}
            current={distributionIsCurrent(snapshot, built.request)} busy={busy} />
          <div className="distribution-result__status" role="status">
            {result.outcome === 'PASS' ? (
              <CheckCircle2 size={15} aria-hidden="true" />
            ) : (
              <AlertTriangle size={15} aria-hidden="true" />
            )}
            <strong>{result.outcome === 'PASS' ? 'Proposal returned' : 'Could not propose'}</strong>
            {result.reviewer_action && <span>{result.reviewer_action}</span>}
          </div>

          {/* The picture first: what changed is found by eye, not by reading. */}
          <ElevationDiagram result={result} showBanner={false} />

          <DistributionTable result={result} />

          {/* Raj's own explanation (slides 5 and 9), composed from the exact figures. */}
          {result.message && <p className="distribution-result__explanation">{result.message}</p>}

          <details className="distribution-result__trace">
            <summary>Calculation trace</summary>
            <p className="distribution-result__calculation">{result.calculation}</p>
          </details>
        </div>
      )}
    </section>
  );
}

/**
 * The same parts as the drawing, in the same wall order when that order is recorded, so a row and
 * a box are found together.
 * A changed row is highlighted; nothing else is coloured.
 */
function DistributionTable({ result }: { result: FillerDistributionResponse }) {
  const { elements, hasProposal, positionsKnown } = buildElevation(result);
  if (elements.length === 0) return null;
  return (
    <table className="distribution-result__table">
      {/* Wall order is only claimed when it is recorded (exactly two fillers). */}
      {!positionsKnown && <caption>In the order returned; wall positions not recorded.</caption>}
      <thead>
        <tr>
          <th scope="col">Part</th>
          <th scope="col">Arch</th>
          {hasProposal && <th scope="col">Corrected</th>}
          {hasProposal && <th scope="col" aria-label="Changed">&nbsp;</th>}
        </tr>
      </thead>
      <tbody>
        {elements.map((element) => {
          const changed = hasProposal && element.changed;
          return (
            <tr key={element.id} data-changed={changed || undefined} data-kind={element.kind}>
              <th scope="row">
                {element.id}
                {element.kind === 'equipment' ? ' (equipment — width fixed)' : ''}
              </th>
              <td className="mono">{element.original.display}</td>
              {hasProposal && (
                <td className="mono distribution-result__proposed">{element.proposed.display}</td>
              )}
              {hasProposal && (
                <td className="distribution-result__mark" aria-label={changed ? 'corrected' : 'kept'}>
                  {changed ? '✕' : '✓'}
                </td>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
