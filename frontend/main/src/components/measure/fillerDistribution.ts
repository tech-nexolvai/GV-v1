import type { paths } from '../../api/schema';

export type FillerDistributionRequest =
  paths['/api/v1/projects/{project_id}/filler-distribution']['post']['requestBody']['content']['application/json'];
export type FillerDistributionResponse =
  paths['/api/v1/projects/{project_id}/filler-distribution']['post']['responses'][200]['content']['application/json'];

export type DistributionQuantity = {
  key: string;
  source: string;
  semantic_type: string;
  many: boolean;
};

export type DistributionParameter = {
  name: string;
  declared_default: string | null;
  blocked: boolean;
};

export type CabinetTypeName =
  'single_door' | 'double_door' | 'drawer' | 'equipment';

/** The reviewer's classification per cabinet, in the deck's own words. */
export const CABINET_TYPE_LABELS: ReadonlyArray<{
  value: CabinetTypeName;
  label: string;
}> = [
  { value: 'single_door', label: 'Single door' },
  { value: 'double_door', label: 'Double door' },
  { value: 'drawer', label: 'Drawer' },
  { value: 'equipment', label: 'Equipment (width cannot change)' },
];

/**
 * The six cabinet bounds, named exactly as the rulebook parameters and the API field are.
 *
 * None of them has a default here or on the server. CLIENT_FACTS Q21 records that the values are
 * still unsettled, so a bound the rulebook has not supplied must reach the reviewer as a missing
 * input — never as a number this panel chose.
 */
export const CABINET_BOUND_NAMES = [
  'single_door_cab_width_min',
  'single_door_cab_width_max',
  'double_door_cab_width_min',
  'double_door_cab_width_max',
  'drawer_cab_width_min',
  'drawer_cab_width_max',
] as const;

export type CabinetBoundName = (typeof CABINET_BOUND_NAMES)[number];

export type DistributionDraft = {
  cabinetWidths: string[];
  cabinetTypes: CabinetTypeName[];
  fillerWidths: string[];
  fieldWidth: string;
  fillerMin: string;
  fillerMax: string;
  cabinetBounds: Record<CabinetBoundName, string>;
};

export type DistributionBuildResult =
  | { request: FillerDistributionRequest; missing: [] }
  | { request: null; missing: string[] };

function clean(values: string[]): string[] {
  return values.map((value) => value.trim()).filter(Boolean);
}

export function distributionFieldWidthKey(
  quantities: DistributionQuantity[],
): string | null {
  return (
    quantities.find(
      (quantity) =>
        quantity.source === 'USER_INPUT' &&
        quantity.semantic_type === 'field_dimension',
    )?.key ?? null
  );
}

export function buildFillerDistributionRequest(
  draft: DistributionDraft,
): DistributionBuildResult {
  const cabinets = clean(draft.cabinetWidths);
  const fillers = clean(draft.fillerWidths);
  const missing: string[] = [];

  if (cabinets.length === 0) missing.push('architectural cabinet widths');
  // Any number of fillers: slide 12 names a wall on only one side, and the server accepts a run
  // of one. Requiring two here would refuse a layout the client described.
  if (fillers.length === 0) missing.push('architectural filler widths');
  if (!draft.fillerMin.trim()) missing.push('filler minimum');
  if (!draft.fillerMax.trim()) missing.push('filler maximum');
  if (draft.cabinetTypes.length !== cabinets.length) {
    missing.push('a type for every cabinet');
  }
  for (const name of CABINET_BOUND_NAMES) {
    if (!draft.cabinetBounds[name]?.trim())
      missing.push(name.replace(/_/g, ' '));
  }

  if (missing.length > 0) return { request: null, missing };

  return {
    missing: [],
    request: {
      assembly: {
        cabinets: cabinets.map((width, index) => ({
          id: `cabinet-${index + 1}`,
          width,
          type: draft.cabinetTypes[index],
        })),
        fillers: fillers.map((width, index) => ({
          id: fillerName(index, fillers.length),
          width,
        })),
      },
      field_width: draft.fieldWidth.trim() || null,
      filler_min: draft.fillerMin.trim(),
      filler_max: draft.fillerMax.trim(),
      single_door_cab_width_min:
        draft.cabinetBounds.single_door_cab_width_min.trim(),
      single_door_cab_width_max:
        draft.cabinetBounds.single_door_cab_width_max.trim(),
      double_door_cab_width_min:
        draft.cabinetBounds.double_door_cab_width_min.trim(),
      double_door_cab_width_max:
        draft.cabinetBounds.double_door_cab_width_max.trim(),
      drawer_cab_width_min: draft.cabinetBounds.drawer_cab_width_min.trim(),
      drawer_cab_width_max: draft.cabinetBounds.drawer_cab_width_max.trim(),
    },
  };
}

function fillerName(index: number, count: number): string {
  if (count === 1) return 'filler';
  if (index === 0) return 'left filler';
  if (index === count - 1) return 'right filler';
  return `filler ${index + 1}`;
}
