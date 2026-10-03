import type { FillerDistributionRequest, FillerDistributionResponse } from './fillerDistribution.js';

export type DistributionSnapshot = {
  result: FillerDistributionResponse;
  request: FillerDistributionRequest | null;
  inputKey: string | null;
};

/** Compare submitted text and classification only. No numeric parsing or local recalculation. */
export function distributionInputKey(request: FillerDistributionRequest | null): string | null {
  return request === null ? null : JSON.stringify(request);
}

export function distributionIsCurrent(snapshot: Pick<DistributionSnapshot, 'inputKey'>, request: FillerDistributionRequest | null): boolean {
  return snapshot.inputKey !== null && snapshot.inputKey === distributionInputKey(request);
}
