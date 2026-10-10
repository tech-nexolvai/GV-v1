import type { ArchitectCompared, ArchitectMatch, ArchitectMatchStatus, ArchitectResult, ArchitectSpan, ArchitectViewRef, CountertopResult } from '@/api/client';

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
  | 'compared'
  /** Separate architect file (#1168): the reviewer chooses which of the architect's views shows the countertop. */
  | 'choose-view'
  /** Separate architect file: the reviewer chose a view after this result; only a new check run uses it. */
  | 'view-picked'
  /** Separate architect file: the matched view runs into its neighbour, so the reviewer compares by hand. */
  | 'by-hand';

export function architectOf(row: Pick<CountertopResult, 'architect'>): ArchitectResult | null {
  return row.architect ?? null;
}

const ONE_JUDGMENT = new Set(['code', 'both-ais']);

export function architectState(result: ArchitectResult | null): ArchitectState {
  if (!result) return 'none';
  // The view match comes first (#1168): a pick waiting for a run, then a view still to choose, then a
  // matched view that cannot be read. All three are null on a combined-sheet set, which reads as today.
  const match = result.match ?? null;
  if (match?.waits_for_run) return 'view-picked';
  if (match?.status === 'needs_reviewer' && result.needs_decision) return 'choose-view';
  if (match?.status === 'not_separated' && result.needs_decision && result.finding_id) return 'by-hand';
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
 * True when the server's reason already says what it is (`app/api/visual_countertops.py`): a row
 * whose architect check has not run yet ("Not checked yet: …"), or a page where no countertop line
 * was chosen ("Not compared: no countertop line was chosen…", #1093). Said as they are, never as
 * "Not compared: Not checked yet: …" or "Not compared: Not compared: …".
 */
export function reasonSaysItself(result: Pick<ArchitectResult, 'not_compared_reason'>): boolean {
  const reason = result.not_compared_reason ?? '';
  return reason.startsWith('Not checked yet') || reason.startsWith('Not compared');
}

/** True while the row waits for the reviewer's view choice (#1168): open the queue's view picker. */
export function asksForView(result: ArchitectResult | null): boolean {
  return architectState(result) === 'choose-view';
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

// ── The architect's view (#1168: the architect's drawings uploaded as a separate file) ──

export function matchOf(result: ArchitectResult | null | undefined): ArchitectMatch | null {
  return result?.match ?? null;
}

/** Each match state in plain words, short enough for one line. The server's reason says the rest. */
export const MATCH_WORDS: Record<ArchitectMatchStatus, string> = {
  not_matched_yet: 'Not matched with an architect view yet',
  needs_reviewer: "Choose which of the architect's views shows this countertop",
  auto_matched: 'View matched by code and both AIs',
  reviewer_confirmed: 'View chosen by a reviewer',
  carried_over: 'Same view as on the earlier revision',
  none_matches: "No view in the architect's drawings shows this countertop",
  not_separated: "The architect's view is not clearly apart from its neighbour",
  no_candidates: "The architect's file has no views to match",
};

/** The match state in words, or null on a combined-sheet set (no match). */
export function matchWords(result: ArchitectResult | null | undefined): string | null {
  const match = matchOf(result);
  return match ? MATCH_WORDS[match.status] ?? null : null;
}

/**
 * The architect view this row is about, for "Show the architect's view": the one it was compared with,
 * else the matched one (matched but not compared, or not clearly apart). Null on a combined set.
 */
export function viewOf(result: ArchitectResult | null | undefined): ArchitectViewRef | null {
  return result?.compared_with ?? result?.match?.matched_view ?? null;
}

/**
 * The words of the link to the view: the server's own "compared with <file>, page N, view X" when it
 * was compared, else "Matched with <label>". Null when there is no view.
 */
export function viewLinkWords(result: ArchitectResult | null | undefined): string | null {
  if (result?.compared_with) return result.compared_with_text ?? `Compared with ${result.compared_with.label}`;
  const matched = result?.match?.matched_view;
  return matched ? `Matched with ${matched.label}` : null;
}

/** "Sheet A-9 · view 4 · SAMPLE ELEVATION": what the architect printed for a view, in order. */
export function viewHeading(view: Pick<ArchitectViewRef, 'sheet_number' | 'bubble' | 'title'>): string {
  const parts = [
    view.sheet_number ? `Sheet ${view.sheet_number}` : null,
    view.bubble ? `view ${view.bubble}` : null,
    view.title,
  ].filter((part): part is string => Boolean(part));
  return parts.length > 0 ? parts.join(' · ') : 'No sheet or title printed';
}

/** What one AI said, in words: "picked view 2", "said none of them", "was not sure", "gave no answer". */
export function aiPickWords(pick: { answer: string; view_id: string | null }, viewName: (viewId: string) => string | null): string {
  switch (pick.answer) {
    case 'view': return pick.view_id ? `picked ${viewName(pick.view_id) ?? 'a view'}` : 'picked a view';
    case 'none': return 'said none of them';
    case 'unsure': return 'was not sure';
    default: return 'gave no answer';
  }
}

/** How many rows wait for a check run because a reviewer chose their architect view after it. */
export function viewPicksWaiting(rows: readonly Pick<CountertopResult, 'row_id' | 'architect'>[], savedHere: ReadonlySet<string> = new Set()): number {
  return rows.filter((row) => row.architect?.match?.waits_for_run || savedHere.has(row.row_id)).length;
}
