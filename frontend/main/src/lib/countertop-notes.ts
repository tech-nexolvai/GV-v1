/**
 * Which countertops a held whole-package depth or sink result is about (#1297), kept free of React
 * so it can be tested.
 *
 * On a package with several countertops the server holds those results for a person. Its reason
 * says only how many countertops there are, so a reviewer's decision still carries over while they
 * stay the same; the names are in the result's notes, opening with one of these words (the server
 * holds them the same: `workflow/several_countertops.py`, `tests/workflow/test_several_countertops.py`).
 */
export const COUNTERTOP_NOTE_OPENINGS = [
  'Countertops in this package: ',
  'Countertops with no result of their own for this check: ',
] as const;

/** The notes naming the countertops, in the server's order; empty for any other result. */
export function countertopNotes(finding: { notes?: string[] | null }): string[] {
  return (finding.notes ?? []).filter((note) => COUNTERTOP_NOTE_OPENINGS.some((opening) => note.startsWith(opening)));
}
