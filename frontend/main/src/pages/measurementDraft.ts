export type MeasurementValueOrigin = 'empty' | 'proposed' | 'unplaced' | 'confirmed' | 'tagged' | 'typed';

/** Labels describe the editable field, not the historical observations that remain on the server. */
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

/** Refreshes may add readings, but a human's draft (including a deliberate blank) has priority. */
export function prefillReadingValues(
  current: readonly string[],
  humanEdited: boolean,
  confirmed: readonly string[],
  proposed: readonly string[],
  many: boolean,
): { values: string[]; origin: 'unchanged' | 'confirmed' | 'proposed' } {
  if (humanEdited || current.some((value) => value.trim())) {
    return { values: [...current], origin: 'unchanged' };
  }
  if (confirmed.length > 0) {
    return { values: many || confirmed.length === 1 ? [...confirmed] : [''], origin: 'confirmed' };
  }
  if (proposed.length > 0 && (many || proposed.length === 1)) {
    return { values: [...proposed], origin: 'proposed' };
  }
  return { values: [...current], origin: 'unchanged' };
}
