/**
 * The numbers a countertop's check recorded (#1388, #1399), for the panel beside the drawings. Pure
 * functions and the live loader only. Every value is the server's own text for a recorded number;
 * nothing here is added up or worked out: a "+" only lists the recorded pieces, and the "= total"
 * is the check's own recorded row total (`pieces_add_up_to`).
 */
import { getCountertopResults, getFindingChain, type CountertopResults, type FindingChain } from '@/api/client';

/** One recorded number, with what it is in plain words. `value` null: the check had none for it, said by `missing` (else "not read"). */
export interface RecordedLine {
  label: string;
  value: string | null;
  missing?: string;
}

/**
 * What the check recorded for one result (#1388), to compare with the drawing. Every value is the
 * server's own text for a recorded number; nothing here is added up or worked out.
 */
export interface RecordedNumbers {
  lines: RecordedLine[];
  /** The check's own comparison sentence, when it is shown from the stored calculation. */
  comparison: string | null;
  /** Why something is not there (the check held the row, recorded no total, or has not run). Never silent. */
  note?: string | null;
}

/** What the panel is about: a wrong row (#1388) or a countertop item to check by hand (#1399). */
export interface RecordedSubject {
  key: string;
  finding_id?: string | null;
  row_id?: string | null;
}

export type RecordedNumbersLoader = (projectId: string, packageId: string, subject: RecordedSubject) => Promise<RecordedNumbers>;

type Countertop = CountertopResults['items'][number];

/** Said when no piece of the row was read one by one (e.g. the drawing gives only "N equal" pieces). */
export const PIECES_NOT_READ = 'not read one by one';
/** Said for a value of a row the check held: it may have been read, but the check recorded none. */
const HELD = 'not recorded';

/** In the place of a missing value: not recorded on a held row, else not read. */
function missingWord(row: Countertop): string {
  return row.hold ? HELD : 'not read';
}

/** A piece's kind, when the reader named one ("unknown" is no name). */
function namedKind(kind: string | null | undefined): string | null {
  return kind && kind !== 'unknown' ? kind : null;
}

/**
 * The pieces as one line: each recorded piece in order, joined by "+", then "= " and the check's own
 * recorded total when there is one. A piece without a recorded value says so in its place.
 */
export function piecesLine(row: Countertop): RecordedLine {
  if (row.pieces.length === 0) return { label: 'Pieces', value: null, missing: 'none recorded' };
  const total = row.pieces_add_up_to?.display ?? null;
  if (row.pieces.every((piece) => !piece.value)) {
    return { label: 'Pieces', value: total === null ? null : `add up to ${total}`, missing: row.hold ? HELD : PIECES_NOT_READ };
  }
  const listed = row.pieces.map((piece) => piece.value?.display ?? missingWord(row)).join(' + ');
  return { label: 'Pieces', value: total === null ? listed : `${listed} = ${total}` };
}

/**
 * A countertop width result, as its row records it: what the vendor wrote, the pieces (and their
 * recorded total), the field cut, what was needed and the difference (the server reads them from the
 * check's recorded inputs and trace).
 */
export function countertopLines(row: Countertop): RecordedLine[] {
  const lines: RecordedLine[] = [{ label: 'The vendor wrote', value: row.printed_overall ? `${row.printed_overall.display} (overall)` : null, missing: missingWord(row) }];
  lines.push(piecesLine(row));
  if (row.pieces.some((piece) => namedKind(piece.kind))) lines.push({ label: 'Pieces are', value: row.pieces.map((piece) => namedKind(piece.kind) ?? 'not named').join(', ') });
  if (row.field_cut_per_end) {
    const count = row.field_cut_count;
    lines.push({ label: 'Field cut', value: count == null ? `${row.field_cut_per_end.display} per end` : `${row.field_cut_per_end.display} at each of ${count} ${count === 1 ? 'end' : 'ends'}` });
  }
  if (row.expected_total) lines.push({ label: 'Needed overall', value: row.expected_total.display });
  // Checked for every wall layout (#1202): each layout's needed overall, as recorded.
  for (const wallCase of row.wall_cases?.cases ?? []) {
    lines.push({ label: `Needed if ${wallCase.words || wallCase.layout}`, value: wallCase.expected_total?.display ?? null });
  }
  if (row.delta) lines.push({ label: 'Difference', value: row.delta.display });
  else if (row.wall_cases?.closest_gap) lines.push({ label: 'Closest difference', value: `${row.wall_cases.closest_gap.display}, whatever the walls` });
  return lines;
}

/** Why the row shows no recorded result or total, in plain words; null when nothing is missing. */
export function countertopNote(row: Countertop): string | null {
  // The hold's reason is the item's own reason, shown above the numbers: said once, not twice.
  if (row.hold) return 'The check held this countertop, so it recorded no numbers to compare. Read them on the drawing.';
  if (!row.finding_id) return 'The checks have not run on this countertop yet, so nothing is recorded.';
  if (!row.pieces_add_up_to && row.pieces.length > 0) return 'The check recorded no total for the pieces.';
  return null;
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
 * The live loader: the countertop result that carries this finding (else this row), else the
 * finding's own stored calculation. Both are recorded values only.
 */
export const liveRecordedNumbers: RecordedNumbersLoader = async (projectId, packageId, subject) => {
  const results = await getCountertopResults(projectId, packageId);
  const row =
    (subject.finding_id ? results.items.find((item) => item.finding_id === subject.finding_id) : undefined) ??
    (subject.row_id ? results.items.find((item) => item.row_id === subject.row_id) : undefined);
  if (row) return { lines: countertopLines(row), comparison: null, note: countertopNote(row) };
  if (subject.finding_id) return chainNumbers(await getFindingChain(projectId, packageId, subject.finding_id));
  return { lines: [], comparison: null, note: 'No check result is recorded for this item.' };
};
