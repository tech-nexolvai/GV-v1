import type { ArchitectCompared, ArchitectResult, ArchitectSpan, CountertopResult } from '@/api/client';

/**
 * The vendor-vs-architect check on screen (#1085), kept free of React so it can be tested alone.
 *
 * Every number here is the API's own text (`vendor_display`, `architect_display`, `delta_display`);
 * nothing is recomputed. Whether a row needs the reviewer is the server's `needs_decision`.
 */

export type ArchitectState =
  /** The API sent no architect result for this row (an older run, or the check is not published). */
  | 'none'
  /** Nothing comparable on the architect's drawing: no finding, nothing to click (decided 2026-10-09). */
  | 'not-compared'
  /** Compared, but the pairing rests on one judgment: the reviewer confirms it (decided 2026-10-09). */
  | 'confirm'
  /** Waiting for a pairing nobody has made yet (no automatic pairing at all). */
  | 'unpaired'
  /** A recorded result: PASS / FAIL from two judgments or a reviewer's pairing, or another abstention. */
  | 'compared';

export function architectOf(row: Pick<CountertopResult, 'architect'>): ArchitectResult | null {
  return row.architect ?? null;
}

const ONE_JUDGMENT = new Set(['code', 'both-ais']);

export function architectState(result: ArchitectResult | null): ArchitectState {
  if (!result) return 'none';
  if (result.finding_id === null || result.finding_id === undefined || result.outcome === null || result.outcome === undefined) {
    return result.not_compared_reason ? 'not-compared' : 'none';
  }
  if (result.outcome === 'REVIEW_REQUIRED' && result.needs_decision) {
    if (result.pairing_source && ONE_JUDGMENT.has(result.pairing_source)) return 'confirm';
    if (!result.pairing_source || result.pairing_source === 'none') return 'unpaired';
  }
  return 'compared';
}

/**
 * The server's own words for a row whose architect check has not run yet
 * (`app/api/visual_countertops.py`): said as they are, not as "Not compared: Not checked yet: …".
 */
export function notCheckedYet(result: Pick<ArchitectResult, 'not_compared_reason'>): boolean {
  return (result.not_compared_reason ?? '').startsWith('Not checked yet');
}

/** True while the pairing waits for the reviewer: its numbers are not a result yet, so no verdict colour. */
export function awaitsPairing(result: ArchitectResult | null): boolean {
  const state = architectState(result);
  return state === 'confirm' || state === 'unpaired';
}

/** "Confirm the pairing: only code matched these" / "…only the AIs matched these". */
export function confirmWords(result: Pick<ArchitectResult, 'pairing_source'>): string {
  return result.pairing_source === 'both-ais'
    ? 'Confirm the pairing: only the AIs matched these'
    : 'Confirm the pairing: only code matched these';
}

/** Who paired the row, in words for small print. Never "AI decided". */
export function pairedByWords(result: Pick<ArchitectResult, 'pairing_judgments'>): string | null {
  switch (result.pairing_judgments) {
    case 'code and both AIs': return 'Paired by code and both AIs';
    case 'code only': return 'Paired by code only';
    case 'both AIs only': return 'Paired by both AIs only';
    case 'reviewer': return 'Paired by a reviewer';
    default: return null;
  }
}

/** The pair a single line shows: the overall when it was compared, else the first compared pair. */
export function headlinePair(result: ArchitectResult): ArchitectCompared | null {
  return result.compared.find((pair) => pair.kind === 'overall') ?? result.compared[0] ?? null;
}

/** "Overall" or "Piece 2", as the row counts its pieces (from 1). */
export function pairLabel(pair: Pick<ArchitectCompared, 'kind' | 'vendor_piece'>): string {
  return pair.kind === 'overall' ? 'Overall' : `Piece ${pair.vendor_piece ?? '?'}`;
}

/** The architect finding ids on these rows (null ones left out): they belong to their countertops. */
export function architectFindingIds(rows: readonly Pick<CountertopResult, 'architect'>[]): Set<string> {
  const ids = new Set<string>();
  for (const row of rows) {
    const id = row.architect?.finding_id;
    if (id) ids.add(id);
  }
  return ids;
}

// ── The reviewer's pairing ──────────────────────────────────

/** A pair being built in the picker: one architect span with the overall or a run of pieces. */
export interface DraftPair {
  candidateId: string;
  kind: 'overall' | 'piece';
  /** 0-based piece positions, next to each other; empty for the overall. */
  pieces: number[];
}

/**
 * Why a drafted pairing cannot be sent yet, in the server's own words where it has them
 * (`workflow/architect_pairing_records.record_reviewer_pairing`); null when it can be. The server
 * checks again and its refusal is shown as it comes.
 */
export function draftProblem(pairs: readonly DraftPair[], spans: readonly ArchitectSpan[]): string | null {
  if (pairs.length === 0) return 'Choose at least one pair, or say that nothing is comparable.';
  const offered = new Map(spans.map((span) => [span.candidate_id, span]));
  const usedSpans = new Set<string>();
  const usedPieces = new Set<number>();
  let overalls = 0;
  for (const pair of pairs) {
    const span = offered.get(pair.candidateId);
    if (!span || !span.can_pair) return span?.refusal ?? 'That architect dimension cannot be paired.';
    if (usedSpans.has(pair.candidateId)) return 'Each architect dimension can be paired only once.';
    usedSpans.add(pair.candidateId);
    if (pair.kind === 'overall') {
      overalls += 1;
      continue;
    }
    if (pair.pieces.length === 0) return "Choose one or more of this row's own pieces.";
    // The server accepts a run of pieces, but V1 compares one piece or the overall only, so a run
    // would never be compared (workflow/architect_row_plan.py): one piece per dimension here.
    if (pair.pieces.length > 1) return 'Pair one piece with each dimension: a run of pieces is not compared yet.';
    const sorted = [...pair.pieces].sort((a, b) => a - b);
    if (sorted.some((piece, index) => index > 0 && piece !== sorted[index - 1] + 1)) {
      return 'Pieces paired with one architect dimension must be next to each other.';
    }
    for (const piece of sorted) {
      if (usedPieces.has(piece)) return 'Each vendor piece can be paired only once.';
      usedPieces.add(piece);
    }
  }
  if (overalls > 1) return 'Only one architect dimension can pair with the overall.';
  return null;
}

/** The request body for a drafted pairing, pieces in order. */
export function draftBody(pairs: readonly DraftPair[]): { kind: 'overall' | 'piece'; architect_candidate_id: string; vendor_slot_indices: number[] }[] {
  return pairs.map((pair) => ({
    kind: pair.kind,
    architect_candidate_id: pair.candidateId,
    vendor_slot_indices: pair.kind === 'overall' ? [] : [...pair.pieces].sort((a, b) => a - b),
  }));
}

/** "Pieces 2–3" / "Piece 2" / "The overall", for a pair's vendor side (positions are 0-based). */
export function vendorSideWords(kind: string, pieces: readonly number[]): string {
  if (kind === 'overall') return 'The overall';
  const sorted = [...pieces].sort((a, b) => a - b);
  if (sorted.length === 0) return 'No piece';
  if (sorted.length === 1) return `Piece ${sorted[0] + 1}`;
  return `Pieces ${sorted[0] + 1}–${sorted[sorted.length - 1] + 1}`;
}
