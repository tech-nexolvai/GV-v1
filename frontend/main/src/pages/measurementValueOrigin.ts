export type MeasurementValueOrigin = 'empty' | 'saved' | 'proposed' | 'unplaced' | 'confirmed' | 'tagged' | 'typed';

/** The label describes the current editable value, not an older observation on the server. */
export function measurementValueOrigin(input: {
  hasValue: boolean;
  humanEdited: boolean;
  proposed: boolean;
  placementUnverified: boolean;
  qualifications: readonly string[];
  /** The box shows exactly the value the server holds as typed and saved (#1183). */
  showsSaved?: boolean;
}): MeasurementValueOrigin {
  if (!input.hasValue) return 'empty';
  if (input.showsSaved) return 'saved';
  if (input.humanEdited) return 'typed';
  if (input.proposed) return input.placementUnverified ? 'unplaced' : 'proposed';
  if (input.qualifications.includes('exact_vector_tag')) return 'tagged';
  if (input.qualifications.length > 0) return 'confirmed';
  return 'typed';
}
