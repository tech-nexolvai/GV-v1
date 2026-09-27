/**
 * Layout for the cabinet elevation drawing, kept apart from the SVG so the geometry can be tested.
 *
 * **Every width here is the API's own exact value.** The response carries each quantity as a
 * numerator/denominator pair; nothing in this file rounds, estimates or invents a dimension. The
 * only arithmetic is placing boxes on a screen.
 *
 * **Wall order, not response order.** The API lists fillers and cabinets as two separate arrays.
 * Raj's slides draw the run as it stands on the wall: left filler, the cabinets, right filler. The
 * request sends fillers ordered left then right, so with two fillers the first is the left one and
 * the last is the right one. With one filler the side is not recorded, so it is drawn first and
 * `sideKnown` is false; the caller must not label it "left".
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
  elements: ElevationElement[];
  archTotal: { value: number; display: string };
  siteTotal: { value: number; display: string } | null;
  /** Whether a corrected run exists to draw. Only a PASS proposal is drawn; see `buildElevation`. */
  hasProposal: boolean;
  /** False when a single filler's side is unknown, so the drawing does not claim "left". */
  sideKnown: boolean;
  unit: 'in' | 'mm';
}

const CABINET_CODES: Record<CabinetType, { code: string; name: string }> = {
  single_door: { code: 'SD', name: 'Single-door cabinet' },
  double_door: { code: 'DD', name: 'Double-door cabinet' },
  drawer: { code: 'DR', name: 'Drawer cabinet' },
  equipment: { code: 'EQ', name: 'Equipment cabinet' },
};

function numeric(quantity: Quantity): number {
  return Number(quantity.numerator) / Number(quantity.denominator);
}

/** a/b === c/d, decided in integers so 21/1 and 42/2 compare equal and no float drift decides it. */
export function sameQuantity(a: Quantity, b: Quantity): boolean {
  try {
    return (
      BigInt(a.numerator) * BigInt(b.denominator) === BigInt(b.numerator) * BigInt(a.denominator)
    );
  } catch {
    // A numerator that is not an integer string is a contract break upstream. Treat it as changed
    // so the drawing highlights it rather than silently calling it unchanged.
    return false;
  }
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

/**
 * Orders the run as it stands on the wall and marks what the correction changed.
 *
 * A proposal is only drawn when the outcome is PASS. A REVIEW_REQUIRED result is the "cannot be
 * resolved, RFI to architect" case: drawing a corrected run there would show widths the engine
 * declined to produce.
 */
export function buildElevation(result: FillerDistributionResponse): ElevationLayout {
  const fillers = result.fillers.map((filler) =>
    toElement(filler.id, 'filler', 'F', fillerName(filler.id), filler.original, filler.proposed),
  );
  const cabinets = result.cabinets.map((cabinet, index) => {
    const meta = CABINET_CODES[cabinet.type];
    const kind: ElementKind = cabinet.adjustable ? 'cabinet' : 'equipment';
    return toElement(
      cabinet.id,
      kind,
      meta.code,
      `${meta.name} ${index + 1}`,
      cabinet.original,
      cabinet.proposed,
    );
  });

  let elements: ElevationElement[];
  let sideKnown = true;
  if (fillers.length === 2) {
    elements = [fillers[0], ...cabinets, fillers[1]];
  } else if (fillers.length === 1) {
    elements = [fillers[0], ...cabinets];
    sideKnown = false;
  } else if (fillers.length === 0) {
    elements = cabinets;
  } else {
    // More than two fillers: their positions between cabinets are not in the request.
    elements = [fillers[0], ...cabinets, ...fillers.slice(1)];
    sideKnown = false;
  }

  const hasProposal = result.outcome === 'PASS';
  const proposedTotal = elements.reduce((sum, element) => sum + element.proposed.value, 0);
  const field = result.field_dimension?.value ?? null;

  return {
    elements,
    archTotal: { value: numeric(result.design_width), display: result.design_width.display },
    siteTotal: hasProposal
      ? { value: proposedTotal, display: field?.display ?? formatTotal(proposedTotal, result.design_width.unit) }
      : null,
    hasProposal,
    sideKnown,
    unit: result.design_width.unit,
  };
}

function fillerName(id: string): string {
  const trimmed = id.trim();
  if (!trimmed) return 'Filler';
  return trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
}

function formatTotal(value: number, unit: 'in' | 'mm'): string {
  const rounded = Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, '');
  return unit === 'in' ? `${rounded}"` : `${rounded} mm`;
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
 * Both strips share one scale (units per pixel) so a shorter site run is visibly shorter. No
 * minimum width is imposed: a filler that is 2" of 90" is drawn 2/90 of the run, because a
 * minimum would misstate proportions on a drawing whose purpose is to show proportions. Labels
 * that do not fit are moved below the strip by the renderer instead.
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
