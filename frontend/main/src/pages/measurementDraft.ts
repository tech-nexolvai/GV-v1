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
