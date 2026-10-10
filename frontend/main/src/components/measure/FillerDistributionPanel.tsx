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
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { Caution, INPUT_CLASS, LoadError, SELECT_CLASS, StepSection } from './wizard-ui.js';


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
    <StepSection
      id="distribution-heading"
      slot="distribution-panel"
      className="rounded-xl border bg-card p-4"
      title={
        <span className="flex items-center gap-2">
          <Calculator className="size-4" aria-hidden="true" /> Filler distribution
        </span>
      }
      line="Uses the widths above and the limits in Settings. This preview does not save measurements."
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor="distribution-field-width" className="flex flex-wrap items-baseline gap-x-2 text-sm">
            <span className="font-medium">Site field width</span>
            <span className="text-xs text-muted-foreground">measured on site</span>
          </label>
          <input
            className={`${INPUT_CLASS} num`}
            id="distribution-field-width"
            placeholder={'96 1/2" or 2451 mm'}
            value={fieldWidth}
            onChange={(event) => onFieldWidthChange(event.target.value)}
          />
        </div>

        {cabinetWidths.map((width, index) => (
          <div className="flex flex-col gap-1" key={`cabinet-${index + 1}`}>
            <label htmlFor={`distribution-cabinet-type-${index}`} className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="font-medium">
                Cabinet <span className="num">{index + 1}</span> — <span className="num">{width}</span>
              </span>
              <span className="text-xs text-muted-foreground">reviewer classification</span>
            </label>
            <select
              className={SELECT_CLASS}
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
          </div>
        ))}
      </div>

      {built.missing.length > 0 && <Caution>Enter {built.missing.join(', ')} above before asking for a distribution.</Caution>}

      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        disabled={busy || built.request === null}
        onClick={() => void calculate()}
      >
        <Calculator aria-hidden="true" />
        {busy ? 'Calculating…' : 'Calculate distribution'}
      </Button>

      {error && <LoadError>{error}</LoadError>}

      {result && snapshot && (
        <div className="flex flex-col gap-3 border-t pt-3" data-outcome={result.outcome}>
          <DistributionProvenance request={snapshot.request}
            current={distributionIsCurrent(snapshot, built.request)} busy={busy} />
          <p
            className={cn(
              'flex flex-wrap items-center gap-1.5 text-sm',
              result.outcome === 'PASS' ? 'text-outcome-pass-fg' : 'text-outcome-review-fg',
            )}
            role="status"
          >
            {result.outcome === 'PASS' ? (
              <CheckCircle2 className="size-4" aria-hidden="true" />
            ) : (
              <AlertTriangle className="size-4" aria-hidden="true" />
            )}
            <strong className="font-medium">{result.outcome === 'PASS' ? 'Proposal returned' : 'Could not propose'}</strong>
            {result.reviewer_action && <span className="text-foreground">{result.reviewer_action}</span>}
          </p>

          {/* The picture first: what changed is found by eye, not by reading. A legacy drawing, so
              it keeps its own element styles (data-legacy). */}
          <div data-legacy className="min-w-0 overflow-x-auto">
            <ElevationDiagram result={result} showBanner={false} />
          </div>

          <DistributionTable result={result} />

          {/* The client lead's own explanation (slides 5 and 9), composed from the exact figures. */}
          {result.message && <p className="text-sm">{result.message}</p>}

          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer py-2">Calculation trace</summary>
            <p className="num whitespace-pre-wrap break-words">{result.calculation}</p>
          </details>
        </div>
      )}
    </StepSection>
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
    <div className="min-w-0 overflow-x-auto">
      <table className="w-full text-left text-sm">
        {/* Wall order is only claimed when it is recorded (exactly two fillers). */}
        {!positionsKnown && <caption className="pb-1 text-left text-xs text-muted-foreground">In the order returned; wall positions not recorded.</caption>}
        <thead className="text-xs text-muted-foreground">
          <tr className="border-b">
            <th scope="col" className="py-1.5 pr-3 font-normal">Part</th>
            <th scope="col" className="py-1.5 pr-3 text-right font-normal">Arch</th>
            {hasProposal && <th scope="col" className="py-1.5 pr-3 text-right font-normal">Corrected</th>}
            {hasProposal && <th scope="col" className="py-1.5 font-normal" aria-label="Changed">&nbsp;</th>}
          </tr>
        </thead>
        <tbody>
          {elements.map((element) => {
            const changed = hasProposal && element.changed;
            return (
              <tr key={element.id} data-changed={changed || undefined} data-kind={element.kind} className="border-b last:border-0 data-[changed=true]:bg-muted/60">
                <th scope="row" className="py-1.5 pr-3 font-normal">
                  {element.id}
                  {element.kind === 'equipment' ? ' (equipment — width fixed)' : ''}
                </th>
                <td className="num py-1.5 pr-3 text-right">{element.original.display}</td>
                {hasProposal && (
                  <td className="num py-1.5 pr-3 text-right font-medium">{element.proposed.display}</td>
                )}
                {hasProposal && (
                  <td className="py-1.5 text-center" aria-label={changed ? 'corrected' : 'kept'}>
                    {changed ? '✕' : '✓'}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
