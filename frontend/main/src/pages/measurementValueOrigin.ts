export type MeasurementValueOrigin = 'empty' | 'proposed' | 'unplaced' | 'confirmed' | 'tagged' | 'typed';

/** The label describes the current editable value, not an older observation on the server. */
export function measurementValueOrigin(input: {
  hasValue: boolean;
  humanEdited: boolean;
  proposed: boolean;
  placementUnverified: boolean;
  qualifications: readonly string[];
}): MeasurementValueOrigin {
  if (!input.hasValue) return 'empty';
  if (input.humanEdited) return 'typed';
  if (input.proposed) return input.placementUnverified ? 'unplaced' : 'proposed';
  if (input.qualifications.includes('exact_vector_tag')) return 'tagged';
  if (input.qualifications.length > 0) return 'confirmed';
  return 'typed';
}
