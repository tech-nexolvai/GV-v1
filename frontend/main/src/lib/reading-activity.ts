/**
 * The live reading activity (#1239): what each AI is doing while a package is read, when its
 * inspector stops it, and when it tries again, in plain words.
 *
 * **The server owns every sentence** (#1238, orchestrator 2026-10-14): each item carries `line` (a
 * plain sentence with role names, never an agent's name), and the activity carries `now` and
 * `summary.sentence`. The screen shows them word for word and adds only the role tag (the agent's
 * name on hover, Anant's phase 3 decision 1), a glyph by state and the links to Decisions. The
 * wording table below is a fallback for a sentence that is missing, so nothing is ever blank.
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
  /**
   * What changed on a try-again (fixed codes): the number reader `closer-look-2x`,
   * `closer-look-3x-upright`, `tighter-crop`; the line finder `tie-break-disputed-lines`,
   * `other-numbered-lines`, `each-line-alone`. Null otherwise.
   */
  change: string | null;
  /** Which try this is, 1 to 3 (Anant: up to 3 tries per item); null when not a try-again. */
  attempt: number | null;
  /** The server's plain sentence for this happening: what the screen shows. */
  line: string | null;
  at: string | null;
}

export interface ReadingActivitySummaryOut {
  stopped: number;
  tried_again: number;
  helped: number;
  /** Every item waiting for a person (stopped and not tried, or tried and not helped). */
  waiting: number;
  /** The server's summary sentence, e.g. "Tried again on 5 items: 3 now agree, 4 wait for you." */
  sentence: string | null;
}

export interface ReadingActivityOut {
  reading_id: string;
  updated_at: string | null;
  /** The server's sentence for what is happening now ("Reading page 12 of 40…"). */
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

/** What a try-again changed, as the end of a sentence (the fixed change codes); null otherwise. */
const CHANGE_WORDS: Record<string, string> = {
  'closer-look-2x': 'with a 2× close-up',
  'closer-look-3x-upright': 'with a 3× close-up, turned upright',
  'tighter-crop': 'with a tighter crop',
  'tie-break-disputed-lines': 'with a closer look at the disputed lines',
  'other-numbered-lines': 'by showing the other numbered lines',
  'each-line-alone': 'by showing each line alone',
};

function changeWords(change: string | null): string | null {
  return change ? (CHANGE_WORDS[change] ?? null) : null;
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

/** One happening as the reviewer reads it: the server's sentence, else the fallback wording. */
export function activityLine(item: ReadingActivityItemOut): string {
  const line = item.line?.trim();
  return line ? line : fallbackLine(item);
}

/** The screen's own words for a happening whose sentence is missing (design §(c)). */
export function fallbackLine(item: ReadingActivityItemOut): string {
  const page = `Page ${item.page}`;
  const role = roleInSentence(item.agent);
  switch (item.state) {
    case 'answered':
      return `${page}: ${role} answered.`;
    case 'passed':
      if (item.agent === 'Rekha') return `${page}: countertop line found.`;
      if (item.agent === 'Akshara') return `${page}: numbers read and agreed.`;
      return `${page}: ${role} passed its check.`;
    case 'stopped':
      return stopWords(item) ?? `${page}: ${role} stopped on one item. ${WAITING}`;
    case 'trying_again': {
      const how = changeWords(item.change);
      if (item.agent === 'Rekha') {
        return how ? `Couldn't settle the countertop line on page ${item.page} — trying again ${how}.` : `Couldn't settle the countertop line on page ${item.page} — trying again.`;
      }
      if (item.agent === 'Akshara') {
        return how ? `Trying again on a number on page ${item.page} ${how}.` : `Trying again on a number on page ${item.page}.`;
      }
      return how ? `Trying again on page ${item.page} ${how}.` : `Trying again on page ${item.page}.`;
    }
    case 'try_again_result':
      return resultWords(item) ?? `${page}: ${role} tried again.`;
    case 'waiting': {
      const result = resultWords(item);
      if (result) return result;
      const stop = stopWords(item);
      return stop ? `${stop} ${WAITING}` : `${page}: ${role} stopped on one item. ${WAITING}`;
    }
    default:
      return `${page}: ${role} is working.`;
  }
}

/** Whether a happening leaves something for the reviewer (drawn with the amber needs-you glyph). */
export function needsYou(item: ReadingActivityItemOut): boolean {
  if (item.state === 'stopped' || item.state === 'waiting') return true;
  return item.state === 'try_again_result' && item.code !== 'helped';
}

/**
 * The panel's first line while reading: the server's `now`; without it, "Reading page 12 of 40…"
 * from the worker's progress line; else just "Reading…".
 */
export function readingHeadline(
  progress: { pagesDone: number | null; pagesTotal: number | null } | null | undefined,
  now: string | null | undefined,
): string {
  const said = now?.trim();
  if (said) return said;
  const pages = progress ? pageWords(progress) : null;
  return pages?.startsWith('page ') ? `Reading ${pages}…` : 'Reading…';
}

/**
 * The line after reading: the server's `summary.sentence`; without it, "Tried again on 5 items: 3
 * now agree, 4 wait for you." or "Reading finished: 4 items wait for you." from the counts, with
 * `waiting` used as it is (every item waiting for a person). Null when there is nothing to say.
 */
export function summarySentence(summary: ReadingActivitySummaryOut | null | undefined): string | null {
  if (!summary) return null;
  const said = summary.sentence?.trim();
  if (said) return said;
  const { tried_again: tried, helped, waiting } = summary;
  const waits = (n: number, items: boolean) => `${n} ${items ? (n === 1 ? 'item ' : 'items ') : ''}${n === 1 ? 'waits' : 'wait'} for you`;
  if (tried > 0) {
    const parts = [helped > 0 ? `${helped} now ${helped === 1 ? 'agrees' : 'agree'}` : null, waiting > 0 ? waits(waiting, false) : null].filter(Boolean);
    return `Tried again on ${tried} ${tried === 1 ? 'item' : 'items'}${parts.length ? `: ${parts.join(', ')}` : ''}.`;
  }
  return waiting > 0 ? `Reading finished: ${waits(waiting, true)}.` : null;
}

/** A count a reviewer can act on: "3 now agree", "4 wait for you", "1 item waits for you". */
const DECISION_COUNT = /(\d+ (?:items? )?(?:now agrees?|waits? for you))/;

/** A sentence cut into plain text and the counts that link to Decisions. */
export function linkedParts(sentence: string): { text: string; link: boolean }[] {
  // Split on a capturing group: the counts land at the odd positions.
  return sentence
    .split(DECISION_COUNT)
    .map((part, index) => ({ text: part, link: index % 2 === 1 }))
    .filter((part) => part.text !== '');
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
      attempt: typeof it.attempt === 'number' && Number.isFinite(it.attempt) ? it.attempt : null,
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
    summary: s ? { stopped: count(s.stopped), tried_again: count(s.tried_again), helped: count(s.helped), waiting: count(s.waiting), sentence: text(s.sentence) } : null,
  };
}
