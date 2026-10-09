/**
 * Reading the countertop results (#1035) for the dashboard (#1039). Pure functions only.
 *
 * **Numbers are never computed here.** Every value shown comes from the API's exact
 * `{numerator, denominator, display}`; the numerator is read only to tell a zero, negative or
 * positive difference apart for its colour and sign — never to do arithmetic.
 *
 * **A reviewer's decision is never shown as the check's result.** A row's bucket is what the record
 * says: still needs someone → "needs-you"; otherwise the recorded outcome (PASS, FAIL, or "not
 * checkable" when the automatic check could not decide). Who decided is a separate column.
 */
import type { CountertopResult, ExactValue } from '@/api/client';
import type { Finding } from '@/data/types';
import { architectState } from './architect';

export type Bucket = 'needs-you' | 'fail' | 'pass' | 'not-checkable';
export type Filter = 'all' | 'needs-you' | 'fail' | 'pass' | 'held' | 'automatic';

type RowFacts = Pick<CountertopResult, 'needs_decision' | 'outcome' | 'architect'>;

/** The hold the API puts on a page the two AIs picked different countertop lines on (#1093). */
export const ROW_CHOICE_SPLIT = 'row-choice-split';

/**
 * A page the two AIs split on (#1093): no line was chosen, so nothing on it was read. It is a held
 * item that needs the reviewer, but it has no pieces, no picture, no outline and no countertop card.
 */
export function isSplitPage(row: Pick<CountertopResult, 'hold'>): boolean {
  return row.hold?.code === ROW_CHOICE_SPLIT;
}

/**
 * A countertop has two checks (#1085): its own width, and whether it matches the architect. It needs
 * the reviewer when either does; it is FAIL when either recorded a FAIL; it is PASS only when every
 * recorded result passed (an architect line that compared nothing adds no result).
 */
export function rowNeedsYou(row: RowFacts): boolean {
  return row.needs_decision || Boolean(row.architect?.needs_decision);
}

export function rowHasFail(row: RowFacts): boolean {
  return row.outcome === 'FAIL' || row.architect?.outcome === 'FAIL';
}

export function rowAllPass(row: RowFacts): boolean {
  const architect = row.architect?.outcome;
  return row.outcome === 'PASS' && (architect === null || architect === undefined || architect === 'PASS');
}

export function bucketOf(row: RowFacts): Bucket {
  if (rowNeedsYou(row)) return 'needs-you';
  if (rowHasFail(row)) return 'fail';
  if (rowAllPass(row)) return 'pass';
  return 'not-checkable';
}

const BUCKET_ORDER: Record<Bucket, number> = { 'needs-you': 0, fail: 1, pass: 2, 'not-checkable': 3 };

/** Needs you → FAIL → PASS → not checkable, then page, then label. Returns a new array. */
export function sortRows<T extends Pick<CountertopResult, 'needs_decision' | 'outcome' | 'architect' | 'page_number' | 'label'>>(rows: readonly T[]): T[] {
  return [...rows].sort(
    (a, b) =>
      BUCKET_ORDER[bucketOf(a)] - BUCKET_ORDER[bucketOf(b)] ||
      a.page_number - b.page_number ||
      a.label.localeCompare(b.label),
  );
}

/**
 * A row by its recorded result (#1056): FAIL and PASS are what the check recorded, whether or not the
 * reviewer still has to decide; the rest is either waiting for a decision or decided "not checkable".
 * The donut and the FAIL / PASS filters use this, so "FAIL" means the same everywhere. `bucketOf`
 * (needs you first) still orders the table and the queue.
 */
export function resultBucket(row: RowFacts): Bucket {
  if (rowHasFail(row)) return 'fail';
  if (rowAllPass(row)) return 'pass';
  return rowNeedsYou(row) ? 'needs-you' : 'not-checkable';
}

/** The donut's words: its undecided slice holds only rows without a PASS or FAIL. */
export const RESULT_LABEL: Record<Bucket, string> = {
  'needs-you': 'Needs your decision',
  fail: 'FAIL',
  pass: 'PASS',
  'not-checkable': 'Not checkable',
};

export function matchesFilter(row: CountertopResult, filter: Filter): boolean {
  if (filter === 'all') return true;
  if (filter === 'held') return row.hold !== null;
  if (filter === 'automatic') return row.outcome === 'PASS' || row.outcome === 'FAIL';
  if (filter === 'needs-you') return rowNeedsYou(row);
  return resultBucket(row) === filter;
}

export interface Kpis {
  countertops: number;
  /** Decided by the checks themselves: a recorded PASS or FAIL. */
  automatic: number;
  needsYou: number;
  pass: number;
  fail: number;
  held: number;
}

export function kpis(rows: readonly CountertopResult[]): Kpis {
  return {
    countertops: rows.length,
    automatic: rows.filter((r) => r.outcome === 'PASS' || r.outcome === 'FAIL').length,
    needsYou: rows.filter(rowNeedsYou).length,
    pass: rows.filter(rowAllPass).length,
    fail: rows.filter(rowHasFail).length,
    held: rows.filter((r) => r.hold !== null).length,
  };
}

/** The donut's slices, by recorded result; they add up to the number of countertops. */
export function bucketCounts(rows: readonly CountertopResult[]): Record<Bucket, number> {
  const counts: Record<Bucket, number> = { 'needs-you': 0, fail: 0, pass: 0, 'not-checkable': 0 };
  for (const row of rows) counts[resultBucket(row)] += 1;
  return counts;
}

/** Recorded FAILs the reviewer still has to decide: shown beside the donut's FAIL count. */
export function failsNeedingYou(rows: readonly CountertopResult[]): number {
  return rows.filter((row) => rowHasFail(row) && rowNeedsYou(row)).length;
}

/** What a sign-off covers, countertop by countertop (#1064). The five parts add up to the rows. */
export interface SignOffSummary {
  /** No decision needed and none recorded: the checks settled it ("needed no decision"). */
  byChecks: number;
  /** A reviewer's decision, other than "not checkable". */
  byYou: number;
  /** A check that could not decide, dismissed by a reviewer as not checkable. */
  notCheckable: number;
  /** Still waiting for a decision (the server's `needs_decision`) on a recorded result. */
  needYou: number;
  /**
   * A row with no recorded result (no finding). The server lists it as needing a decision, but it is
   * not a finding, so it does not block sign-off and is not part of what sign-off approves.
   */
  noResult: number;
}

export function signOffSummary(rows: readonly CountertopResult[]): SignOffSummary {
  const summary: SignOffSummary = { byChecks: 0, byYou: 0, notCheckable: 0, needYou: 0, noResult: 0 };
  for (const row of rows) {
    if (row.finding_id === null) summary.noResult += 1;
    else if (row.needs_decision) summary.needYou += 1;
    else if (row.reviewer_decision === null) summary.byChecks += 1;
    else if (decisionWords(row.reviewer_decision.action, row.outcome) === 'Not checkable') summary.notCheckable += 1;
    else summary.byYou += 1;
  }
  return summary;
}

/**
 * The one "not compared" reason every countertop shares (#1126), or null. When every row with an
 * architect result is "not compared" for the same reason, the screen says it once above the table
 * instead of under each row. Any row that was compared, waits for a pairing or has another reason
 * keeps the per-row lines. The reason is the API's own text, never reworded here.
 */
export function sharedNotComparedReason(rows: readonly Pick<CountertopResult, 'architect'>[]): string | null {
  let shared: string | null = null;
  for (const row of rows) {
    const result = row.architect ?? null;
    const state = architectState(result);
    if (state === 'none') continue;
    if (state !== 'not-compared' || !result?.not_compared_reason) return null;
    if (shared === null) shared = result.not_compared_reason;
    else if (shared !== result.not_compared_reason) return null;
  }
  return shared;
}

/** The filter a reviewer lands on: what needs them, when anything does. */
export function defaultFilter(rows: readonly CountertopResult[]): Filter {
  return rows.some(rowNeedsYou) ? 'needs-you' : 'all';
}

/** The sign of an exact value, read from its numerator (never computed). */
export function signOf(value: ExactValue): -1 | 0 | 1 {
  const n = value.numerator.trim();
  if (/^-?0+$/.test(n)) return 0;
  return n.startsWith('-') ? -1 : 1;
}

/**
 * A difference as a reviewer reads it: the API's own display with a true minus sign, and a plus on
 * an overrun so "+1/2"" and "1/2"" can never be confused. Null (held, unchecked) is "—".
 */
export function formatDelta(delta: ExactValue | null): { text: string; sign: -1 | 0 | 1 | null } {
  if (delta === null) return { text: '—', sign: null };
  const sign = signOf(delta);
  const display = delta.display.trim();
  if (sign < 0) return { text: `−${display.replace(/^-/, '')}`, sign };
  if (sign > 0) return { text: display.startsWith('+') ? display : `+${display}`, sign };
  return { text: display, sign };
}

/** Which walls a layout has, for the glyph; null when the layout is not established. */
export function wallsOf(config: string | null): { back: boolean; left: boolean; right: boolean } | null {
  if (config === 'back_left_right') return { back: true, left: true, right: true };
  if (config === 'back_only') return { back: true, left: false, right: false };
  if (config === 'island') return { back: false, left: false, right: false };
  // The rulebook publishes only these three layouts (rules/rulebook/ct_width_001.yaml); anything
  // else is shown as not established rather than drawn from a guess.
  return null;
}

/** Where the wall layout came from, in one word. */
export const WALL_SOURCE_WORD: Record<CountertopResult['wall_layout']['source'], string> = {
  'drawing clues': 'drawing',
  'both readers': 'AIs',
  reviewer: 'reviewer',
  'between panels': 'panels',
  'not established': 'not set',
};

/** The same, said in full for a tooltip and screen readers. */
export const WALL_SOURCE_TITLE: Record<CountertopResult['wall_layout']['source'], string> = {
  'drawing clues': 'Read from the vendor drawing’s clues',
  'both readers': 'Both AI readers agreed',
  reviewer: 'Chosen by a reviewer',
  'between panels': 'Stone between side panels',
  'not established': 'Not established — choose it on the countertop card',
};

/** "Ana Lima" → "AL"; "reviewer@x" → "RE". */
export function initials(actor: string): string {
  const words = actor.replace(/@.*/, '').split(/[\s._-]+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return (words[0] ?? '?').slice(0, 2).toUpperCase();
}

/** "confirm" / "dismiss" / … in the words the decision buttons use. */
export function decisionWords(action: string, outcome: CountertopResult['outcome']): string {
  const abstention = outcome === 'REVIEW_REQUIRED' || outcome === 'NOT_FOUND' || outcome === null;
  if (action === 'confirm') return abstention ? 'Checked: OK' : 'Confirmed';
  if (action === 'dismiss') return abstention ? 'Not checkable' : 'Dismissed';
  if (action === 'correct') return 'Corrected';
  if (action === 'except') return 'Exception';
  return action;
}

/** Abstentions still waiting on someone: the only findings the bulk "not checkable" may touch. */
export function bulkEligible(findings: readonly Finding[], blocking: ReadonlySet<string>): Finding[] {
  return findings.filter((f) => blocking.has(f.id) && (f.outcome === 'NOT_FOUND' || f.outcome === 'REVIEW_REQUIRED'));
}

/**
 * An exact value as a number, for ordering rows only — never shown, never used in a result. A
 * missing value sorts last.
 */
export function sortValue(value: ExactValue | null): number {
  if (value === null) return Number.POSITIVE_INFINITY;
  return Number(value.numerator) / Number(value.denominator);
}

/**
 * Record the same decision on several findings, one call each, in order (#1039 bulk "not
 * checkable"). A failure is collected, never thrown, so one refused finding cannot hide whether
 * the others were saved.
 */
export async function recordEach(
  ids: readonly string[],
  record: (id: string) => Promise<void>,
): Promise<{ saved: number; failed: { id: string; error: string }[] }> {
  const result = { saved: 0, failed: [] as { id: string; error: string }[] };
  for (const id of ids) {
    try {
      await record(id);
      result.saved += 1;
    } catch (error) {
      result.failed.push({ id, error: error instanceof Error ? error.message : String(error) });
    }
  }
  return result;
}
