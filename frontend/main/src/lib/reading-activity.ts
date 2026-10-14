/**
 * The live reading activity (#1239): what each AI is doing while a package is read, when its
 * inspector stops it, and when it tries again, in plain words.
 *
 * The API sends codes only (`PackageOut.activity`, #1238): an agent, a page, a state and a reason
 * code. The words a reviewer reads are made here, from those codes, so the screen never shows
 * drawing text and never shows an agent's name in a sentence (Anant, phase 3 decision 1: plain role
 * names, the agent's name only on hover). A code this screen does not know yet falls back to the
 * server's own plain line, so a new kind of stop is never shown as a blank.
 *
 * It decides nothing. "Passed" here means an inspector let the work go on; it is not a PASS result,
 * and nothing on this panel is an outcome.
 */

import { pageWords } from '@/lib/review-stage';

/**
 * One happening, as the API reports it (design §(a)). Mirrors the designed shape until #1238's
 * generated types land; read through `readActivity`, so a missing or odd field never breaks the page.
 */
export interface ReadingActivityItemOut {
  page: number;
  /** The agent's name ("Rekha", "Akshara", …); shown only on hover. */
  agent: string;
  /** A code question id, e.g. `p11:row-choice`; never drawing text. */
  item: string;
  /** `answered`, `passed`, `stopped`, `trying_again`, `try_again_result` or `waiting`. */
  state: string;
  /** Why: `readers-differ`, `no-printed-numbers`, `helped`, `not-helped`, `allowance-used`, … */
  code: string | null;
  /** What changed on a try-again: `tie-break`, `closer-look`, …; null otherwise. */
  change: string | null;
  /** The server's own plain sentence; used only for a code this screen does not know. */
  line: string | null;
  at: string | null;
}

export interface ReadingActivitySummaryOut {
  stopped: number;
  tried_again: number;
  helped: number;
  waiting: number;
}

export interface ReadingActivityOut {
  reading_id: string;
  updated_at: string | null;
  /** The server's line for what is happening now; used when no page count is known. */
  now: string | null;
  /** The newest happenings that are not passes (passes are only counted). */
  items: ReadingActivityItemOut[];
  summary: ReadingActivitySummaryOut | null;
}

/** The role each agent plays, as a reviewer would say it. The agent's name stays on hover. */
const ROLES: Record<string, string> = {
  Bala: 'Item finder',
  Rekha: 'Line finder',
  Akshara: 'Number reader',
  Vastu: 'Architect reader',
  Milan: 'Matcher',
  Kautilya: 'Planner',
  Nina: 'Checker',
  Vani: 'Writer',
};

/** The role on screen ("Line finder"); an agent this screen does not know is an "AI helper". */
export function roleName(agent: string): string {
  return ROLES[agent] ?? 'AI helper';
}

/** The role inside a sentence ("the line finder"). */
function roleInSentence(agent: string): string {
  const role = ROLES[agent];
  return role ? `the ${role.toLowerCase()}` : 'an AI helper';
}

const WAITING = 'Waiting for you.';

/** Why an inspector stopped the work, per agent; null for a code not known here. */
function stopWords(item: ReadingActivityItemOut): string | null {
  const page = `Page ${item.page}`;
  if (item.agent === 'Rekha') {
    if (item.code === 'readers-differ') return `${page}: the two AIs picked different lines.`;
    if (item.code === 'no-printed-numbers') return `${page}: the chosen line has no printed numbers.`;
  }
  if (item.agent === 'Akshara') {
    if (item.code === 'readers-differ') return `${page}: the two AIs read one number differently.`;
    if (item.code === 'unsure') return `${page}: an AI was not sure about one number.`;
    if (item.code === 'unreadable') return `${page}: an AI could not read one number clearly.`;
  }
  return null;
}

/** What a try-again changed, as the end of a sentence; null for a change not known here. */
function changeWords(change: string | null): string | null {
  if (change === 'tie-break' || change === 'closer-look') return 'with a closer look';
  const zoom = change ? /(\d)x/.exec(change) : null;
  if (zoom) return `with a ${zoom[1]}× close-up`;
  return null;
}

/** The outcome of a try-again (or why there was none); null for a code not known here. */
function resultWords(item: ReadingActivityItemOut): string | null {
  const line = item.agent === 'Rekha';
  const number = item.agent === 'Akshara';
  if (item.code === 'helped') {
    if (line) return 'Trying again helped — both AIs now pick the same line, and it has printed numbers.';
    if (number) return 'Trying again helped — both AIs now read the number the same way.';
    return 'Trying again helped — both AIs now agree.';
  }
  if (item.code === 'not-helped') {
    if (line) return "Trying again didn't help — waiting for you to pick the line.";
    if (number) return "Trying again didn't help — waiting for you to check the number.";
    return "Trying again didn't help — waiting for you.";
  }
  if (item.code === 'allowance-used') return `Not tried again — the try-again allowance for this set is used up. ${WAITING}`;
  if (item.code === 'cannot-zoom') return `Not tried again — the number cannot be shown any larger. ${WAITING}`;
  return null;
}

/** The server's plain line, or a generic sentence that still names the page and the role. */
function fallback(item: ReadingActivityItemOut, generic: string): string {
  return item.line?.trim() ? item.line.trim() : generic;
}

/** One happening in plain words (design §(c)). */
export function activityLine(item: ReadingActivityItemOut): string {
  const page = `Page ${item.page}`;
  const role = roleInSentence(item.agent);
  switch (item.state) {
    case 'answered':
      return `${page}: ${role} answered.`;
    case 'passed':
      if (item.agent === 'Rekha') return `${page}: countertop line found.`;
      if (item.agent === 'Akshara') return `${page}: numbers read and agreed.`;
      return fallback(item, `${page}: ${role} passed its check.`);
    case 'stopped':
      return stopWords(item) ?? fallback(item, `${page}: ${role} stopped on one item. ${WAITING}`);
    case 'trying_again': {
      const how = changeWords(item.change);
      if (item.agent === 'Rekha') {
        return how ? `Couldn't settle the countertop line on page ${item.page} — trying again ${how}.` : fallback(item, `Couldn't settle the countertop line on page ${item.page} — trying again.`);
      }
      if (item.agent === 'Akshara') {
        return how ? `Trying again on a number on page ${item.page} ${how}.` : fallback(item, `Trying again on a number on page ${item.page}.`);
      }
      return how ? `Trying again on page ${item.page} ${how}.` : fallback(item, `Trying again on page ${item.page}.`);
    }
    case 'try_again_result':
      return resultWords(item) ?? fallback(item, `${page}: ${role} tried again.`);
    case 'waiting': {
      const result = resultWords(item);
      if (result) return result;
      const stop = stopWords(item);
      return stop ? `${stop} ${WAITING}` : fallback(item, `${page}: ${role} stopped on one item. ${WAITING}`);
    }
    default:
      return fallback(item, `${page}: ${role} is working.`);
  }
}

/** Whether a happening leaves something for the reviewer (drawn with the amber needs-you glyph). */
export function needsYou(item: ReadingActivityItemOut): boolean {
  if (item.state === 'stopped' || item.state === 'waiting') return true;
  return item.state === 'try_again_result' && item.code !== 'helped';
}

/**
 * The panel's first line while reading: "Reading page 12 of 40…" from the worker's progress line;
 * without a page count, the server's own line; else just "Reading…".
 */
export function readingHeadline(
  progress: { pagesDone: number | null; pagesTotal: number | null } | null | undefined,
  now: string | null | undefined,
): string {
  const pages = progress ? pageWords(progress) : null;
  if (pages?.startsWith('page ')) return `Reading ${pages}…`;
  const said = now?.trim().replace(/[.…]+$/, '');
  return said ? `${said}…` : 'Reading…';
}

/** The summary after reading, as its parts: the two counts are links to Decisions. */
export interface SummaryParts {
  lead: string;
  /** "3 now agree"; null when none did. */
  agreed: string | null;
  /** "2 wait for you"; null when none do. */
  waiting: string | null;
}

/**
 * "Tried again on 5 items: 3 now agree, 2 wait for you." — or, with no try-again, "Reading
 * finished: 4 items wait for you." Null when nothing stopped (nothing to say).
 */
export function summaryParts(summary: ReadingActivitySummaryOut | null | undefined): SummaryParts | null {
  if (!summary) return null;
  if (summary.tried_again > 0) {
    const tried = summary.tried_again;
    const helped = Math.min(Math.max(summary.helped, 0), tried);
    const notHelped = tried - helped;
    return {
      lead: `Tried again on ${tried} ${tried === 1 ? 'item' : 'items'}: `,
      agreed: helped > 0 ? `${helped} now ${helped === 1 ? 'agrees' : 'agree'}` : null,
      waiting: notHelped > 0 ? `${notHelped} ${notHelped === 1 ? 'waits' : 'wait'} for you` : null,
    };
  }
  if (summary.waiting > 0) {
    return { lead: 'Reading finished: ', agreed: null, waiting: `${summary.waiting} ${summary.waiting === 1 ? 'item waits' : 'items wait'} for you` };
  }
  return null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/**
 * `PackageOut.activity`, read tolerantly: absent, null or a shape this screen does not know is no
 * activity (the panel then shows nothing); a broken item is dropped rather than drawn wrong.
 */
export function readActivity(value: unknown): ReadingActivityOut | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.reading_id !== 'string' || !Array.isArray(raw.items)) return null;
  const items: ReadingActivityItemOut[] = [];
  for (const entry of raw.items as unknown[]) {
    if (!entry || typeof entry !== 'object') continue;
    const it = entry as Record<string, unknown>;
    if (typeof it.page !== 'number' || typeof it.agent !== 'string' || typeof it.state !== 'string') continue;
    items.push({
      page: it.page,
      agent: it.agent,
      item: text(it.item) ?? '',
      state: it.state,
      code: text(it.code),
      change: text(it.change),
      line: text(it.line),
      at: text(it.at),
    });
  }
  const s = raw.summary && typeof raw.summary === 'object' ? (raw.summary as Record<string, unknown>) : null;
  return {
    reading_id: raw.reading_id,
    updated_at: text(raw.updated_at),
    now: text(raw.now),
    items,
    summary: s ? { stopped: count(s.stopped), tried_again: count(s.tried_again), helped: count(s.helped), waiting: count(s.waiting) } : null,
  };
}
