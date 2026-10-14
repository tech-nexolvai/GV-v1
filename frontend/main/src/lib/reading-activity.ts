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
 * One happening, as `PackageOut.activity.items[]` reports it (the shape #1242 posted). Mirrors it
 * until the generated types land; read through `readActivity`, so a missing or odd field never
 * breaks the page.
 */
export interface ReadingActivityItemOut {
  page: number;
  /** The agent's name ("Rekha", "Akshara", …); shown only on hover. */
  agent: string;
  /** The agent's role in plain words ("the number reader"): the row's tag. */
  role: string | null;
  /** A code question id, e.g. `p11:slot0:0`; never drawing text. */
  item: string;
  /** `answered`, `stopped`, `trying_again`, `helped` or `waiting`: the row's glyph. */
  state: string;
  /** Why: `readers-differ`, `no-printed-numbers`, `not-helped`, `allowance-used`, `cannot-zoom`, … */
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
  /** Inspector passes: counted, never listed. */
  passed: number;
  stopped: number;
  tried_again: number;
  helped: number;
  /** Every item waiting for a person (stopped and not tried, or tried and not helped). */
  waiting: number;
  /** The server's summary sentence, e.g. "Tried again on 2 items: 1 now agree; 1 item waits for you." */
  sentence: string | null;
}

export interface ReadingActivityOut {
  reading_id: string;
  updated_at: string | null;
  /** The newest line while reading; the summary sentence once the reading has ended. */
  now: string | null;
  /** The newest 20 happenings, newest first; passes are only counted. */
  items: ReadingActivityItemOut[];
  summary: ReadingActivitySummaryOut | null;
}

/** The role each agent plays, for an item whose `role` is missing. The agent's name stays on hover. */
const ROLES: Record<string, string> = {
  Bala: 'the item finder',
  Rekha: 'the line finder',
  Akshara: 'the number reader',
  Vastu: 'the architect reader',
  Milan: 'the matcher',
  Kautilya: 'the planner',
  Nina: 'the checker',
  Vani: 'the writer',
};

/** The role inside a sentence ("the number reader"): the server's, else the one this screen knows. */
function roleWords(item: Pick<ReadingActivityItemOut, 'agent' | 'role'>): string {
  return item.role?.trim() || ROLES[item.agent] || 'an AI helper';
}

/** The row's tag ("Number reader"): the role without its article, capitalised. Never the agent. */
export function roleTag(item: Pick<ReadingActivityItemOut, 'agent' | 'role'>): string {
  const words = roleWords(item).replace(/^(the|an?) /i, '');
  return words.charAt(0).toUpperCase() + words.slice(1);
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

/** Why an item waits after a try-again (or without one); null for a code not known here. */
function waitingWords(item: ReadingActivityItemOut): string | null {
  if (item.code === 'not-helped') {
    if (item.agent === 'Rekha') return "Trying again didn't help — waiting for you to pick the line.";
    if (item.agent === 'Akshara') return "Trying again didn't help — waiting for you to check the number.";
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
  const role = roleWords(item);
  switch (item.state) {
    case 'answered':
      return `${page}: ${role} answered.`;
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
    case 'helped':
      if (item.agent === 'Rekha') return 'Trying again helped — both AIs now pick the same line, and it has printed numbers.';
      if (item.agent === 'Akshara') return 'Trying again helped — both AIs now read the number the same way.';
      return 'Trying again helped — both AIs now agree.';
    case 'waiting': {
      const waits = waitingWords(item);
      if (waits) return waits;
      const stop = stopWords(item);
      return stop ? `${stop} ${WAITING}` : `${page}: ${role} stopped on one item. ${WAITING}`;
    }
    default:
      return `${page}: ${role} is working.`;
  }
}

/** The row's glyph, by state: amber needs-you, the turning arrow, or a plain tick. */
export type ActivityGlyph = 'needs-you' | 'trying-again' | 'done';

export function activityGlyph(item: Pick<ReadingActivityItemOut, 'state'>): ActivityGlyph {
  if (item.state === 'stopped' || item.state === 'waiting') return 'needs-you';
  if (item.state === 'trying_again') return 'trying-again';
  return 'done';
}

/**
 * The panel's first line while reading: the server's `now`; without it, "Reading page 12 of 40…"
 * from the worker's progress line; else just "Reading…". When `now` only repeats the newest
 * item's line (shown just below it), the page count leads instead, so nothing is said twice
 * (orchestrator, 2026-10-14).
 */
export function readingHeadline(
  progress: { pagesDone: number | null; pagesTotal: number | null } | null | undefined,
  now: string | null | undefined,
  newestLine?: string | null,
): string {
  const said = now?.trim();
  if (said && said !== newestLine?.trim()) return said;
  const pages = progress ? pageWords(progress) : null;
  return pages?.startsWith('page ') ? `Reading ${pages}…` : 'Reading…';
}

/**
 * The line after reading: the server's `summary.sentence`; if that is empty, the server's `now`
 * (orchestrator, 2026-10-14); without either, "Tried again on 5 items: 3 now agree; 4 items wait
 * for you." or "Reading finished: 4 items wait for you." from the counts, with `waiting` used as it
 * is (every item waiting for a person). Null when there is nothing to say.
 */
export function summarySentence(summary: ReadingActivitySummaryOut | null | undefined, now?: string | null): string | null {
  const said = summary?.sentence?.trim() || now?.trim();
  if (said) return said;
  if (!summary) return null;
  const { tried_again: tried, helped, waiting } = summary;
  const waits = waiting > 0 ? `${waiting} ${waiting === 1 ? 'item waits' : 'items wait'} for you` : null;
  if (tried > 0) {
    const parts = [helped > 0 ? `${helped} now agree` : null, waits].filter(Boolean);
    return `Tried again on ${tried} ${tried === 1 ? 'item' : 'items'}${parts.length ? `: ${parts.join('; ')}` : ''}.`;
  }
  return waits ? `Reading finished: ${waits}.` : null;
}

/**
 * A count a reviewer can act on, as #1242 writes it: "3 now agree", "1 item waits for you",
 * "2 items wait for you" (also "N now agrees" and "N wait for you", so the fallback never breaks it).
 */
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
      role: text(it.role),
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
    summary: s ? { passed: count(s.passed), stopped: count(s.stopped), tried_again: count(s.tried_again), helped: count(s.helped), waiting: count(s.waiting), sentence: text(s.sentence) } : null,
  };
}
