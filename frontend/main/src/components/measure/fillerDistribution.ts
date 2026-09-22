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

export type DistributionDraft = {
  cabinetWidths: string[];
  fillerWidths: string[];
  fieldWidth: string;
  fillerMin: string;
  fillerMax: string;
  adjustableCabinetId: string | null;
};

export type DistributionBuildResult =
  | { request: FillerDistributionRequest; missing: [] }
  | { request: null; missing: string[] };

function clean(values: string[]): string[] {
  return values.map((value) => value.trim()).filter(Boolean);
}

export function distributionFieldWidthKey(quantities: DistributionQuantity[]): string | null {
  return (
    quantities.find(
      (quantity) =>
        quantity.source === 'USER_INPUT' && quantity.semantic_type === 'field_dimension',
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
  if (fillers.length !== 2) missing.push('two architectural filler widths');
  if (!draft.fillerMin.trim()) missing.push('filler minimum');
  if (!draft.fillerMax.trim()) missing.push('filler maximum');

  if (missing.length > 0) return { request: null, missing };

  return {
    missing: [],
    request: {
      assembly: {
        cabinets: cabinets.map((width, index) => ({
          id: `cabinet-${index + 1}`,
          width,
        })),
        fillers: [
          { id: 'left filler', width: fillers[0] },
          { id: 'right filler', width: fillers[1] },
        ],
      },
      field_width: draft.fieldWidth.trim() || null,
      filler_min: draft.fillerMin.trim(),
      filler_max: draft.fillerMax.trim(),
      adjustable_cabinet_id: draft.adjustableCabinetId,
    },
  };
}
