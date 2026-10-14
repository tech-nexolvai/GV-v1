/**
 * The live reading activity (#1239): what each AI is doing while a package is read, when its
 * inspector stops it, and when it tries again, in plain words.
 *
 * **The server owns every sentence** (#1238): each item carries `line` and `role` (a plain role,
 * never an agent's name), and the activity carries `now` and `summary.sentence`. The screen shows
 * them word for word and adds only the role tag (the agent's name on hover, Anant's phase 3 decision
 * 1), a glyph by state, the list of what stopped after a reading, and a link to Decisions. It writes
 * no sentences of its own: an empty sentence shows the item's code plainly.
 *
 * It decides nothing, and nothing on this panel is an outcome.
 */

import type { components } from '@/api/schema';
import { pageWords } from '@/lib/review-stage';

/** One happening (`PackageOut.activity.items[]`, #1238), as the API's generated types say. */
export type ReadingActivityItemOut = components['schemas']['ReadingActivityItemOut'];
/** The reading's counts and its one summary sentence. */
export type ReadingActivitySummaryOut = components['schemas']['ReadingActivitySummaryOut'];
/** `PackageOut.activity`: null before the running attempt's first event, and on the package list. */
export type ReadingActivityOut = components['schemas']['ReadingActivityOut'];

/** The row's tag: the server's role without its article, capitalised ("Number reader"). Never the agent. */
export function roleTag(item: Pick<ReadingActivityItemOut, 'role'>): string {
  const words = item.role.trim().replace(/^(the|an?) /i, '');
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'AI helper';
}

/** One happening as the reviewer reads it: the server's sentence; if empty, the page and the code. */
export function activityLine(item: Pick<ReadingActivityItemOut, 'line' | 'page' | 'code'>): string {
  return item.line.trim() || `Page ${item.page}: ${item.code}`;
}

/** The row's glyph, by state: amber needs-you, the turning arrow, or a plain tick. */
export type ActivityGlyph = 'needs-you' | 'trying-again' | 'done';

export function activityGlyph(item: Pick<ReadingActivityItemOut, 'state'>): ActivityGlyph {
  if (item.state === 'stopped' || item.state === 'waiting') return 'needs-you';
  if (item.state === 'trying_again') return 'trying-again';
  return 'done';
}

/** An item that waits for a person: stopped and never tried, or tried and not helped. */
export function waitsForYou(item: Pick<ReadingActivityItemOut, 'state'>): boolean {
  return item.state === 'stopped' || item.state === 'waiting';
}

/**
 * The panel's first line while reading: the server's `now`, unless it only repeats a line shown
 * below it (any row, #1240 review); then "Reading page 12 of 40…" from the worker's progress line,
 * else just "Reading…".
 */
export function readingHeadline(
  progress: { pagesDone: number | null; pagesTotal: number | null } | null | undefined,
  now: string | null | undefined,
  shownLines: readonly string[] = [],
): string {
  const said = now?.trim();
  if (said && !shownLines.some((line) => line.trim() === said)) return said;
  const pages = progress ? pageWords(progress) : null;
  return pages?.startsWith('page ') ? `Reading ${pages}…` : 'Reading…';
}

/**
 * The line after a reading: the server's `summary.sentence` ("From the reading, nothing waits for
 * you.", "Tried again on 2 items: 1 now agrees; 1 item waits for you.", or that the reading did not
 * finish); if that is empty, `now`. Null when the server said nothing.
 */
export function afterReadingSentence(activity: Pick<ReadingActivityOut, 'summary' | 'now'>): string | null {
  return activity.summary.sentence.trim() || activity.now.trim() || null;
}

/**
 * What stopped, for the list after a finished reading: every item the API returned that waits for a
 * person, in its order, and how many wait in all (`summary.waiting`). The API returns at most 20
 * items, so `shown` can be fewer than `total`; the screen says so.
 */
export function whatStopped(activity: Pick<ReadingActivityOut, 'items' | 'summary'>): {
  items: ReadingActivityItemOut[];
  total: number;
} {
  const items = activity.items.filter(waitsForYou);
  return { items, total: Math.max(activity.summary.waiting, items.length) };
}
