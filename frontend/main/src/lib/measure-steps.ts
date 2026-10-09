/**
 * The Measurements wizard's four steps (#1061) and how each counts its work, kept free of React.
 *
 * A step's count is the sum of what its sections already report as still to do — the same rules
 * the sections print today ("N still to decide") — so the step bar and the page can never disagree.
 */

export type MeasureStep = 'drawings' | 'runs' | 'values' | 'settings';

export interface StepCount {
  done: number;
  total: number;
}

export const MEASURE_STEPS: readonly { id: MeasureStep; target: string; number: string; label: string }[] = [
  { id: 'drawings', target: 'measure-drawings', number: '1', label: 'Drawings & parts' },
  { id: 'runs', target: 'measure-runs', number: '2', label: 'Countertops' },
  { id: 'values', target: 'measure-values', number: '3', label: 'Values' },
  { id: 'settings', target: 'measure-settings', number: '4', label: 'Settings & run checks' },
];

/** A count from a total and the number still open. */
export function countOf(total: number, open: number): StepCount {
  return { done: Math.max(0, total - open), total };
}

/** A step's count from its sections'; null while none of them has reported. */
export function sumCounts(parts: readonly (StepCount | null | undefined)[]): StepCount | null {
  const known = parts.filter((part): part is StepCount => part !== null && part !== undefined);
  if (known.length === 0) return null;
  return known.reduce((sum, part) => ({ done: sum.done + part.done, total: sum.total + part.total }), { done: 0, total: 0 });
}

/** Done: there was work and all of it is done. A step with nothing to do is not "done", it is empty. */
export function isComplete(count: StepCount | null): boolean {
  return count !== null && count.total > 0 && count.done === count.total;
}

export function stepAfter(step: MeasureStep): MeasureStep | null {
  const index = MEASURE_STEPS.findIndex((s) => s.id === step);
  return MEASURE_STEPS[index + 1]?.id ?? null;
}

export function stepBefore(step: MeasureStep): MeasureStep | null {
  const index = MEASURE_STEPS.findIndex((s) => s.id === step);
  return index > 0 ? MEASURE_STEPS[index - 1].id : null;
}

/** Equal counts, so a section reporting the same numbers again changes nothing. */
export function sameCount(a: StepCount | null | undefined, b: StepCount | null | undefined): boolean {
  return (a ?? null) === (b ?? null) || (a !== null && a !== undefined && b !== null && b !== undefined && a.done === b.done && a.total === b.total);
}

/**
 * A step's count in words (#1124): "6 of 9 done", "All 3 done", never a bare "6/9".
 */
export function stepCountWords(count: StepCount | null): string {
  if (count === null) return 'Counting…';
  if (count.total === 0) return 'Nothing to do';
  if (isComplete(count)) return `All ${count.total} done`;
  return `${count.done} of ${count.total} done`;
}

/**
 * What one section of a step has shown so far (#1124). An explicit flag, so "nothing to do here" is
 * never guessed from a count: a section still loading, or one whose request failed, also counts 0.
 */
export type SectionState = 'loading' | 'empty' | 'shown' | 'error';

/** A step is empty only when every one of its sections has loaded, found nothing, and not failed. */
export function stepIsEmpty(states: readonly (SectionState | undefined)[]): boolean {
  return states.length > 0 && states.every((state) => state === 'empty');
}
