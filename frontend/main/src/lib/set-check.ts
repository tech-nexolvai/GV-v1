/**
 * A drawing set's newest full check, as the Documents table and the sidebar say it.
 *
 * The results live in the chat's one-click full check now, not in the old pipeline's package state,
 * check runs and findings (a set read only by the chat stays "Uploading" there for ever). The server
 * sends, per set, its newest full check (review result, counts, when it ran, its chat), whether one
 * runs now and where its set pack is (`drawings`). Kept free of React so it is tested on its own.
 */

import type { components } from '@/api/schema';
import { OUTCOME_WORDS } from '@/lib/words';

export type LatestFullCheck = components['schemas']['LatestFullCheckOut'];
export type FullCheckCounts = components['schemas']['FullCheckCountsOut'];
export type DrawingsState = components['schemas']['DrawingsState'];

/** What both lists carry for a set (`packages` and `packages-summary`). */
export interface SetCheck {
  full_check?: LatestFullCheck | null;
  checking?: boolean;
  drawings?: DrawingsState | null;
}

/** The review result's look: one per AWI word, plus the cases with no result. */
export type ResultKind =
  | 'revise_resubmit'
  | 'rejected'
  | 'approved_as_noted'
  | 'approved'
  | 'review_needed'
  | 'not_for_sending'
  | 'unfinished'
  | 'checking'
  | 'none';

export interface SetResult {
  kind: ResultKind;
  /** The short words on the screen ("Revise and resubmit", "Check incomplete", "Not checked yet"). */
  words: string;
  /** The full words where they were shortened ("Check incomplete: not for sending"); else null. */
  title: string | null;
}

/** The newest full check's result in words; "Not checked yet" before the first. */
export function setResult(set: SetCheck): SetResult {
  if (set.checking) return { kind: 'checking', words: 'Full check running…', title: null };
  const check = set.full_check;
  if (!check) return { kind: 'none', words: 'Not checked yet', title: null };
  const result = check.review_result;
  if (!result) return { kind: 'unfinished', words: 'Check did not finish', title: stoppedWords(check.stopped) };
  if (result.action === 'not_for_sending') return { kind: 'not_for_sending', words: 'Check incomplete', title: result.words };
  return { kind: result.action, words: result.words, title: null };
}

const STOPPED: Record<NonNullable<LatestFullCheck['stopped']>, string> = {
  cost_cap: 'It stopped at its price limit.',
  cancelled: 'It was stopped.',
  error: 'It stopped after a problem.',
  turn_limit: 'It stopped at its step limit.',
  interrupted: 'The server restarted while it ran.',
};

function stoppedWords(stopped: LatestFullCheck['stopped']): string | null {
  return stopped ? STOPPED[stopped] : null;
}

/** Where the set's drawings are for the full check (the set pack), or null when nothing started. */
export function drawingsWords(drawings: DrawingsState | null | undefined): string | null {
  if (drawings === 'reading') return 'Reading the drawings…';
  if (drawings === 'read') return 'Drawings read';
  if (drawings === 'failed') return 'Could not read the drawings';
  return null;
}

export type CountKind = 'wrong' | 'right' | 'rule_note' | 'not_compared';

export const COUNT_WORDS: Record<CountKind, string> = {
  wrong: OUTCOME_WORDS.wrong,
  right: OUTCOME_WORDS.right,
  // #1839: the owner's words; shown only when there is one, like the result screen's tile.
  rule_note: 'Ask the architect',
  not_compared: OUTCOME_WORDS.notCompared,
};

export interface CountPart {
  kind: CountKind;
  count: number;
  words: string;
}

/** The result screen's tiles, in their order; Ask the architect only when there is one. */
export function countParts(counts: FullCheckCounts): CountPart[] {
  const parts: CountPart[] = [
    { kind: 'wrong', count: counts.wrong, words: COUNT_WORDS.wrong },
    { kind: 'right', count: counts.right, words: COUNT_WORDS.right },
    { kind: 'rule_note', count: counts.ask_architect, words: COUNT_WORDS.rule_note },
    { kind: 'not_compared', count: counts.not_compared, words: COUNT_WORDS.not_compared },
  ];
  return parts.filter((part) => part.kind !== 'rule_note' || part.count > 0);
}

/** "5 Wrong, 12 Right, 30 Not compared": the counts for a screen reader and a title. */
export function countSentence(counts: FullCheckCounts): string {
  return countParts(counts).map((part) => `${part.count} ${part.words}`).join(', ');
}

/**
 * The sidebar's grey line for a set (#1712: plain words, no pill), first match wins: a check running,
 * the drawings being read, the newest check's result (with its Wrong count when there are any), the
 * drawings failing, then "Drawings read · Not checked yet" or "Not checked yet". `wrong` is shown
 * after the words as "· 5 wrong", its number in Plex Mono; `toDecide` (Review needed only: the items
 * Not compared a person decides before it can be approved) as "· 52 to decide".
 */
export function sidebarLine(set: SetCheck): { words: string; wrong: number | null; toDecide: number | null } {
  if (set.checking) return { words: 'Full check running…', wrong: null, toDecide: null };
  if (set.drawings === 'reading') return { words: 'Reading the drawings…', wrong: null, toDecide: null };
  if (set.full_check) {
    const { wrong, not_compared: notCompared } = set.full_check.counts;
    const result = setResult(set);
    return { words: result.words, wrong: wrong > 0 ? wrong : null, toDecide: result.kind === 'review_needed' ? notCompared : null };
  }
  if (set.drawings === 'failed') return { words: 'Could not read the drawings', wrong: null, toDecide: null };
  return { words: set.drawings === 'read' ? 'Drawings read · Not checked yet' : 'Not checked yet', wrong: null, toDecide: null };
}

/** The sidebar line as one string ("Revise and resubmit · 5 wrong", "Review needed · 52 to decide"). */
export function sidebarWords(set: SetCheck): string {
  const line = sidebarLine(set);
  const wrong = line.wrong === null ? '' : ` · ${line.wrong} ${OUTCOME_WORDS.wrong.toLowerCase()}`;
  const toDecide = line.toDecide === null ? '' : ` · ${line.toDecide} to decide`;
  return `${line.words}${wrong}${toDecide}`;
}

/** "21 Oct 2026, 14:02": when the check ended, in the reviewer's own time zone. */
export function ranWords(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}
