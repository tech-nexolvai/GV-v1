export type PositionedProposal = { value: string; position?: number | null };

/** Place sparse drawing proposals into the exact row positions they came from. */
export function proposalValuesAtPositions(
  values: readonly PositionedProposal[],
  expectedCount: number,
): string[] {
  return Array.from({ length: expectedCount }, (_, position) =>
    values.find((reading, index) => (reading.position ?? index) === position)?.value ?? '',
  );
}

/** A sparse proposal can be shown, but it is not a complete list to confirm as one field. */
export function proposalCoversEveryPosition(
  values: readonly PositionedProposal[],
  expectedCount: number,
): boolean {
  if (values.length !== expectedCount) return false;
  const positions = new Set(values.map((reading, index) => reading.position ?? index));
  return positions.size === expectedCount && Array.from({ length: expectedCount }, (_, i) => i).every((i) => positions.has(i));
}
