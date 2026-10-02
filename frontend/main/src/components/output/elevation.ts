/**
 * Layout for the cabinet elevation drawing, kept apart from the SVG so the geometry can be tested.
 *
 * **Every width is validated before it is drawn.** The API carries each quantity as an exact
 * numerator/denominator pair of integer strings. A quantity that is not one is refused here, before
 * any conversion, and nothing is drawn: a drawing built from a malformed width would place boxes at
 * `NaN` and still look like a drawing.
 *
 * **A correction is drawn only when it closes.** It needs a PASS outcome, a measured site width, and
 * corrected parts that sum exactly (in integers) to that width. Anything less and only the arch run
 * is drawn, because a "corrected" run that does not add up to the wall would state a layout nobody
 * computed.
 *
 * **Wall order only when it is recorded.** The API lists fillers and cabinets separately. The
 * request sends fillers ordered left then right, so with exactly two fillers the first is the left
 * one and the last is the right one. With one filler, or more than two, the request does not record
 * where they stand, so `positionsKnown` is false and the caller must not draw a wall order.
 */

import type { FillerDistributionResponse } from '../measure/fillerDistribution';

type Quantity = FillerDistributionResponse['design_width'];
type CabinetType = FillerDistributionResponse['cabinets'][number]['type'];

export type ElementKind = 'filler' | 'cabinet' | 'equipment';

export interface ElevationElement {
  id: string;
  kind: ElementKind;
  /** Short code drawn inside the box: F, SD, DD, DR, EQ. */
  code: string;
  /** Full name, for the tooltip and the table. */
  name: string;
  original: { value: number; display: string };
  proposed: { value: number; display: string };
  /** Exact comparison of the two rationals, not a float comparison. */
  changed: boolean;
}

export interface ElevationLayout {
  /** In wall order when `positionsKnown`; otherwise fillers then cabinets, as the API lists them. */
  elements: ElevationElement[];
  archTotal: { value: number; display: string };
  siteTotal: { value: number; display: string } | null;
  /** A corrected run exists and closes to the measured site width. */
  hasProposal: boolean;
  /** Whether the run's order on the wall is recorded. When false, draw no run. */
  positionsKnown: boolean;
  /** Every quantity was an exact integer fraction. When false, nothing may be drawn. */
  valid: boolean;
  unit: 'in' | 'mm';
}

const CABINET_CODES: Record<CabinetType, { code: string; name: string }> = {
  single_door: { code: 'SD', name: 'Single-door cabinet' },
  double_door: { code: 'DD', name: 'Double-door cabinet' },
  drawer: { code: 'DR', name: 'Drawer cabinet' },
  sink_cabinet: { code: 'SK', name: 'Sink cabinet' },
  microwave_cabinet: { code: 'MW', name: 'Microwave cabinet' },
  under_counter_refrigeration_cabinet: { code: 'RF', name: 'Under-counter refrigeration cabinet' },
  equipment: { code: 'EQ', name: 'Equipment cabinet' },
};

const INTEGER = /^-?\d+$/;

/** An exact fraction, or null when the quantity is not one. Nothing is converted before this. */
function exact(quantity: Quantity | null | undefined): { n: bigint; d: bigint } | null {
  if (!quantity || !INTEGER.test(quantity.numerator) || !INTEGER.test(quantity.denominator)) return null;
  const d = BigInt(quantity.denominator);
  if (d === 0n) return null;
  return { n: BigInt(quantity.numerator), d };
}

/** a/b === c/d, decided in integers so 21/1 and 42/2 compare equal and no float drift decides it. */
export function sameQuantity(a: Quantity, b: Quantity): boolean {
  const x = exact(a);
  const y = exact(b);
  if (!x || !y) throw new Error('sameQuantity called with a quantity that is not an exact fraction');
  return x.n * y.d === y.n * x.d;
}

/** Sum of exact fractions, exactly. Only called after every quantity has been validated. */
function exactSum(quantities: readonly Quantity[]): { n: bigint; d: bigint } {
  return quantities.reduce(
    (sum, quantity) => {
      const q = exact(quantity)!;
      return { n: sum.n * q.d + q.n * sum.d, d: sum.d * q.d };
    },
    { n: 0n, d: 1n },
  );
}

/** Screen geometry only, after validation. Never used to decide what changed. */
function numeric(quantity: Quantity): number {
  return Number(quantity.numerator) / Number(quantity.denominator);
}

function toElement(
  id: string,
  kind: ElementKind,
  code: string,
  name: string,
  original: Quantity,
  proposed: Quantity,
): ElevationElement {
  return {
    id,
    kind,
    code,
    name,
    original: { value: numeric(original), display: original.display },
    proposed: { value: numeric(proposed), display: proposed.display },
    changed: !sameQuantity(original, proposed),
  };
}

export function buildElevation(result: FillerDistributionResponse): ElevationLayout {
  const unit = result.design_width.unit;
  const field = result.field_dimension?.value ?? null;
  const quantities: Array<Quantity | null | undefined> = [
    result.design_width,
    ...result.fillers.flatMap((filler) => [filler.original, filler.proposed]),
    ...result.cabinets.flatMap((cabinet) => [cabinet.original, cabinet.proposed]),
  ];
  if (field) quantities.push(field);

  if (quantities.some((quantity) => exact(quantity) === null)) {
    return {
      elements: [],
      archTotal: { value: 0, display: result.design_width.display },
      siteTotal: null,
      hasProposal: false,
      positionsKnown: false,
      valid: false,
      unit,
    };
  }

  const fillers = result.fillers.map((filler) =>
    toElement(filler.id, 'filler', 'F', fillerName(filler.id), filler.original, filler.proposed),
  );
  const cabinets = result.cabinets.map((cabinet, index) => {
    const meta = CABINET_CODES[cabinet.type];
    const kind: ElementKind = cabinet.adjustable ? 'cabinet' : 'equipment';
    return toElement(cabinet.id, kind, meta.code, `${meta.name} ${index + 1}`, cabinet.original, cabinet.proposed);
  });

  const positionsKnown = fillers.length === 2 || fillers.length === 0;
  const elements = fillers.length === 2 ? [fillers[0], ...cabinets, fillers[1]] : [...fillers, ...cabinets];

  // The correction must close: the corrected parts sum exactly to the measured site width.
  const proposed = [
    ...result.fillers.map((filler) => filler.proposed),
    ...result.cabinets.map((cabinet) => cabinet.proposed),
  ];
  const sum = exactSum(proposed);
  const fieldExact = field ? exact(field) : null;
  const closes = fieldExact !== null && sum.n * fieldExact.d === fieldExact.n * sum.d;
  const hasProposal = result.outcome === 'PASS' && closes;

  return {
    elements,
    archTotal: { value: numeric(result.design_width), display: result.design_width.display },
    siteTotal: hasProposal && field ? { value: numeric(field), display: field.display } : null,
    hasProposal,
    positionsKnown,
    valid: true,
    unit,
  };
}

function fillerName(id: string): string {
  const trimmed = id.trim();
  if (!trimmed) return 'Filler';
  return trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
}

/** Geometry for one strip, in SVG user units. Pure, so a test can assert on it. */
export interface Segment {
  element: ElevationElement;
  x: number;
  width: number;
  value: number;
  display: string;
}

/**
 * Places each element at true scale.
 *
 * Both strips share one scale so a shorter site run is visibly shorter. No minimum width is
 * imposed: a filler that is 2" of 90" is drawn 2/90 of the run, because a minimum would misstate
 * proportions on a drawing whose purpose is to show proportions. Labels that do not fit are moved
 * below the strip by the renderer instead.
 */
export function placeSegments(
  elements: ElevationElement[],
  which: 'original' | 'proposed',
  startX: number,
  pxPerUnit: number,
): Segment[] {
  let x = startX;
  return elements.map((element) => {
    const { value, display } = element[which];
    const width = value * pxPerUnit;
    const segment = { element, x, width, value, display };
    x += width;
    return segment;
  });
}

/** Approximate rendered width of a mono label at 12px. Used only to decide where a label goes. */
export function monoLabelWidth(text: string, fontSize = 12): number {
  return text.length * fontSize * 0.6;
}
