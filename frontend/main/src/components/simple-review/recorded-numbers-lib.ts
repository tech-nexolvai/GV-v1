/**
 * The numbers a wrong result's check recorded (#1388), for the panel beside the drawings. Pure
 * functions and the live loader only. Every value is the server's own text for a recorded number;
 * nothing here is added up or worked out.
 */
import { getCountertopResults, getFindingChain, type CountertopResults, type FindingChain } from '@/api/client';
import type { ReviewFailure } from '@/api/review-items';

/** One recorded number, with what it is in plain words. `value` null: the check had none for it. */
export interface RecordedLine {
  label: string;
  value: string | null;
}

/**
 * What the check recorded for one wrong result (#1388), to compare with the drawing. Every value is
 * the server's own text for a recorded number; nothing here is added up or worked out.
 */
export interface RecordedNumbers {
  lines: RecordedLine[];
  /** The check's own comparison sentence, when it is shown from the stored calculation. */
  comparison: string | null;
}

export type RecordedNumbersLoader = (projectId: string, packageId: string, failure: ReviewFailure) => Promise<RecordedNumbers>;

type Countertop = CountertopResults['items'][number];

/**
 * A countertop width result, as its row records it: the vendor's overall, each piece, the field cut
 * and the needed overall (the server reads them from the check's recorded inputs and trace).
 */
export function countertopLines(row: Countertop): RecordedLine[] {
  const lines: RecordedLine[] = [{ label: 'The vendor wrote', value: row.printed_overall?.display ?? null }];
  for (const piece of row.pieces) {
    lines.push({ label: `Piece ${piece.index + 1}${piece.kind ? `, ${piece.kind}` : ''}`, value: piece.value?.display ?? null });
  }
  if (row.field_cut_per_end) {
    const count = row.field_cut_count;
    lines.push({ label: 'Field cut', value: count == null ? `${row.field_cut_per_end.display} per end` : `${row.field_cut_per_end.display} at each of ${count} ${count === 1 ? 'end' : 'ends'}` });
  }
  if (row.expected_total) lines.push({ label: 'Needed overall', value: row.expected_total.display });
  // Checked for every wall layout (#1202): each layout's needed overall, as recorded.
  for (const wallCase of row.wall_cases?.cases ?? []) {
    lines.push({ label: `Needed if ${wallCase.layout}`, value: wallCase.expected_total?.display ?? null });
  }
  return lines;
}

/** The stored calculation's operands, verbatim; an abstention or unknown trace has none. */
export function chainNumbers(chain: FindingChain): RecordedNumbers {
  const trace = chain.trace;
  if (trace.kind !== 'calculation') return { lines: [], comparison: null };
  return {
    lines: trace.operands.map((operand) => ({ label: operand.name.replaceAll('_', ' '), value: operand.value })),
    comparison: trace.comparison || null,
  };
}

/**
 * The live loader: the countertop result that carries this finding, else the finding's own stored
 * calculation. Both are recorded values only.
 */
export const liveRecordedNumbers: RecordedNumbersLoader = async (projectId, packageId, failure) => {
  const results = await getCountertopResults(projectId, packageId);
  const row = results.items.find((item) => item.finding_id === failure.finding_id);
  if (row) return { lines: countertopLines(row), comparison: null };
  return chainNumbers(await getFindingChain(projectId, packageId, failure.finding_id));
};
