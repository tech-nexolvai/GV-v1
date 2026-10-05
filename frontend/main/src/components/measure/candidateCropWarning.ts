/** Plain-language display state for the stored crop mark check. */
export function candidateCropWarning(
  showsGvMark: boolean | null | undefined,
): string | null {
  if (showsGvMark === true) {
    return "This picture shows GV's own coloured marks; check the vendor's drawing itself.";
  }
  if (showsGvMark === false) return null;
  return 'This picture has not been checked for GV marks.';
}
