/**
 * Small wording and styling helpers the Measurements wizard's parts share (#1124). Kept out of the
 * component files so fast refresh can reload those on their own.
 */

/** "1 drawing", "3 drawings": the number with the word it counts. */
export function countWords(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

/** One decision's choice buttons: the chosen one filled, the others outlined. */
export function choiceVariant(chosen: boolean): 'default' | 'outline' {
  return chosen ? 'default' : 'outline';
}
