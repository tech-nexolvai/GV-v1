/**
 * A running full check's live state (#1727, UI redesign step 4), from the server's live events
 * (#1710 / #1718: `check_phase`, `check_drawings`, `check_drawing`, `check_finding`, `check_tally`,
 * `check_stopping`, `check_service`, each `{v: 1, <name>: …}`) and the whole state after a reload
 * (`GET /chats/{id}/live`, `ChatOut.live`). Kept free of React so it is tested on its own.
 *
 * Honest counts (owner decision 1): the server sends a finding only once code DECIDED it, Wrong only
 * when proved; a difference waiting for the blind second look is only a count. This screen never
 * shows anything as Wrong that the server did not.
 */

import type { components } from '@/api/schema';
import { areaWords } from '@/lib/chat-result';

export type LiveState = components['schemas']['ChatLiveStateOut'];
export type LivePhase = components['schemas']['ChatLivePhaseOut'];
export type LiveDrawing = components['schemas']['ChatLiveDrawingOut'];
export type LiveFinding = components['schemas']['ChatLiveFindingOut'];
export type LiveTally = components['schemas']['ChatLiveTallyOut'];
export type LiveStopping = components['schemas']['ChatLiveStoppingOut'];
export type LiveUsual = components['schemas']['ChatLiveUsualOut'];

/** The live events this screen reads (version 1). */
export const LIVE_EVENTS = ['check_phase', 'check_drawings', 'check_drawing', 'check_finding', 'check_tally', 'check_stopping', 'check_service'] as const;
export type LiveEventName = (typeof LIVE_EVENTS)[number];
export const LIVE_VERSION = 1;

export type LiveEvent =
  | { event: 'check_phase'; data: { v: number; phase: LivePhase } }
  | { event: 'check_drawings'; data: { v: number; drawings: LiveDrawing[] } }
  | { event: 'check_drawing'; data: { v: number; drawing: LiveDrawing } }
  | { event: 'check_finding'; data: { v: number; finding: LiveFinding } }
  | { event: 'check_tally'; data: { v: number; tally: LiveTally } }
  | { event: 'check_stopping'; data: { v: number; stopping: LiveStopping } }
  | { event: 'check_service'; data: { v: number; reason: string } };

const EMPTY_TALLY: LiveTally = { wrong: 0, right: 0, rule_note: 0, not_compared: 0, waiting_second_look: 0 };

/** The state before the first event: getting ready since `at` (the card draws at once). */
export function startingLive(at: string | null | undefined): LiveState {
  const now = at ?? new Date().toISOString();
  return {
    v: LIVE_VERSION,
    mode: 'simple',
    started_at: now,
    phase: { phase: 'getting_ready', step: null, done: null, total: null, unit: null, started_at: now, phase_started_at: now, stopping: false },
    drawings: [],
    findings: [],
    tally: EMPTY_TALLY,
    counts: { drawings_done: null, drawings_total: null, second_looks_done: 0, second_looks_total: 0, files_made: 0 },
    stopping: null,
    service: null,
  };
}

/**
 * One live event laid over the state. Idempotent: a replayed event (a reconnect replays the run's
 * events after the snapshot) lands on the same keys. A newer version this screen cannot read is skipped.
 */
export function applyLive(live: LiveState | null | undefined, event: LiveEvent, startedAt?: string): LiveState | null {
  if (!event.data || typeof event.data !== 'object' || event.data.v !== LIVE_VERSION) return live ?? null;
  const state = live ?? startingLive(event.event === 'check_phase' ? event.data.phase.started_at : startedAt);
  switch (event.event) {
    case 'check_phase':
      return { ...state, started_at: event.data.phase.started_at ?? state.started_at, phase: event.data.phase };
    case 'check_drawings':
      return { ...state, drawings: [...event.data.drawings] };
    case 'check_drawing': {
      const drawing = event.data.drawing;
      const at = state.drawings.findIndex((each) => each.id === drawing.id);
      const drawings = at >= 0 ? state.drawings.map((each, n) => (n === at ? drawing : each)) : [...state.drawings, drawing];
      return { ...state, drawings };
    }
    case 'check_finding': {
      const finding = event.data.finding;
      const at = state.findings.findIndex((each) => each.key === finding.key);
      // A finding keeps its first place in the order it arrived; later events update it there.
      const findings = at >= 0 ? state.findings.map((each, n) => (n === at ? finding : each)) : [...state.findings, finding];
      return { ...state, findings };
    }
    case 'check_tally':
      return { ...state, tally: event.data.tally };
    case 'check_stopping':
      return { ...state, stopping: event.data.stopping, phase: { ...state.phase, stopping: true } };
    case 'check_service':
      return { ...state, service: event.data.reason };
    default:
      return state;
  }
}

// Matching pages comes before the pages' own count within comparing (#1883).
const STEP_ORDER: Record<string, number> = { matching: -1, code_checks: 1, verifying: 2, second_looks: 3, final_checks: 4, stopped: 5 };

/** How far a check is, as one number that only grows: the phase, its step, then its count. */
export function progressOf(live: LiveState): number {
  const { phase, step, done } = live.phase;
  return stepOf(phase) * 1e8 + (step ? STEP_ORDER[step] ?? 0 : 0) * 1e5 + Math.min(99_999, done ?? 0);
}

/**
 * A reopened page draws the server's snapshot at once (`seed`) while the run's events replay from the
 * start (`replay`), so the card never steps back to an earlier phase: the replay takes over once it
 * has caught up with the snapshot.
 */
export function caughtUp(seed: LiveState, replay: LiveState | null): boolean {
  return replay !== null && progressOf(replay) >= progressOf(seed);
}

/** The event names this module reads, for the stream's filter. */
export function isLiveEvent(name: string): name is LiveEventName {
  return (LIVE_EVENTS as readonly string[]).includes(name);
}

// ── Words ──────────────────────────────────────────────────

export const STEPS = ['Read', 'Compare', 'Double-check', 'Package'] as const;

/** Which of the four steps runs: 0 Read … 3 Package; 4 when done. */
export function stepOf(phase: LivePhase['phase']): number {
  return { getting_ready: 0, comparing: 1, double_checking: 2, making_files: 3, done: 4 }[phase] ?? 0;
}

export function phaseTitle(live: LiveState): string {
  if (live.service) return 'The check could not go on';
  if (live.phase.stopping || live.stopping) return 'Stopping…';
  if (isMatching(live)) return 'Matching pages';
  return { getting_ready: 'Getting ready', comparing: 'Comparing drawings', double_checking: 'Double-checking', making_files: 'Making the package', done: 'Full check done' }[live.phase.phase] ?? 'Checking';
}

/** What is being done now, in one plain line (with the page and area when a drawing is being compared). */
export function nowWords(live: LiveState): { words: string; page: number | null; area: string | null; more: number } {
  if (live.stopping) {
    const left = live.stopping.in_flight;
    return { words: left ? `Finishing the ${left} ${left === 1 ? 'drawing' : 'drawings'} already being compared; nothing new starts` : 'Finishing; nothing new starts', page: null, area: null, more: 0 };
  }
  const phase = live.phase;
  if (phase.phase === 'getting_ready') return { words: 'Reading the drawings: whose drawing is where, every printed size and tag', page: null, area: null, more: 0 };
  if (isMatching(live)) return { words: 'Pairing vendor pages with the architect’s; each starts once paired', page: null, area: null, more: 0 };
  if (phase.phase === 'comparing') {
    const running = live.drawings.filter((drawing) => drawing.status === 'comparing');
    const last = running[running.length - 1];
    if (!last) return { words: 'Comparing the vendor’s drawings with the architect’s', page: null, area: null, more: 0 };
    return { words: 'with the architect’s', page: last.page, area: areaWords(last.area), more: running.length - 1 };
  }
  if (phase.phase === 'double_checking') {
    const steps: Partial<Record<NonNullable<LivePhase['step']>, string>> = {
      code_checks: 'GV Review’s own checks of the whole set, by code',
      verifying: 'Checking each answer by code: the printed values, their spots and directions',
      second_looks: 'A second, independent look at each difference',
      final_checks: 'The final checks by code',
      stopped: 'Checking what was compared before the stop',
    };
    const words = steps[phase.step ?? 'code_checks'];
    return { words: words ?? 'Double-checking', page: null, area: null, more: 0 };
  }
  if (phase.phase === 'making_files') return { words: 'Making the cloud drawing, the comment letter and the lists', page: null, area: null, more: 0 };
  return { words: 'Writing the result', page: null, area: null, more: 0 };
}

/** The bar: a share of the whole check (0-100), or null for a step with no count (an indeterminate bar). */
export function barShare(live: LiveState): number | null {
  const { phase, done, total } = live.phase;
  const part = total ? Math.min(1, Math.max(0, (done ?? 0) / total)) : null;
  switch (phase) {
    case 'getting_ready':
      return null;
    case 'comparing':
      // Matching pages fills only the bar's first few percent: the pages' own count starts at 5.
      if (live.phase.step === 'matching') return part === null ? null : 2 + part * 3;
      return part === null ? null : 5 + part * 75;
    case 'double_checking':
      return part === null ? null : 80 + part * 15;
    case 'making_files':
      return part === null ? 95 : 95 + part * 5;
    default:
      return 100;
  }
}

/** The words under the bar, left: what is counted now. */
export function barWords(live: LiveState): string {
  const { phase, step, done, total, unit } = live.phase;
  if (phase === 'getting_ready') return usualSentence(live.phase.usual) ?? '';
  // "of" and its total never part across lines (a no-break space).
  if (phase === 'comparing' && step === 'matching' && total) return `${done ?? 0} of\u00a0${total} drawings matched to the architect’s`;
  if (phase === 'comparing' && total) return `${done ?? 0} of\u00a0${total} ${unit === 'pages' ? 'pages' : 'drawings'} compared`;
  if (phase === 'double_checking') {
    if (step === 'second_looks' && total) return `All drawings compared · second look ${done ?? 0} of\u00a0${total}`;
    if (step === 'verifying' && total) return `All drawings compared · checked by code ${done ?? 0} of\u00a0${total}`;
    return 'All drawings compared · checking by code';
  }
  if (phase === 'making_files') return total ? `Making the files · ${done ?? 0} of\u00a0${total}` : 'Making the files';
  return '';
}

/**
 * Time left, from the server's estimate when it has one, else from the pace so far while comparing
 * (with a minute for the rest). Null before three drawings are done: the bar's words say the usual
 * time instead (a range up front, never a jumpy countdown).
 */
export function secondsLeft(live: LiveState, now: number, eta: number | null | undefined): number | null {
  const { phase, done, total, phase_started_at } = live.phase;
  // While matching, the pace of the matcher says nothing of the check's own time.
  if (phase === 'comparing' && (live.phase.step === 'matching' || (done ?? 0) < 3)) return null;
  if (eta != null && Number.isFinite(eta)) return Math.max(0, eta);
  if (phase !== 'comparing' || !total || !done) return null;
  const since = (now - Date.parse(phase_started_at)) / 1000;
  if (!Number.isFinite(since) || since <= 0) return null;
  return Math.round((since / done) * (total - done) + 60);
}

export function leftWords(seconds: number | null): string | null {
  if (seconds === null) return null;
  if (seconds < 50) return 'under a minute left';
  if (seconds < 100) return 'about 1 min left';
  return `about ${Math.round(seconds / 60)} min left`;
}

/**
 * While matching pages, or comparing before 3 drawings are done: the usual time for this set (the
 * server's range from measured runs, by its content pages and whether they are matched across
 * files) instead of an estimate. While matching it names the content pages (the card shows no page
 * count then); while comparing the bar already counts the pages. No range from the server: no time
 * at all, never a wrong one (#1883).
 */
export function usualWords(live: LiveState): string | null {
  const { phase, step, done, usual } = live.phase;
  if (phase !== 'comparing' || (step !== 'matching' && (done ?? 0) >= 3)) return null;
  const range = usualRange(usual);
  if (range === null || !usual) return null;
  return step === 'matching' ? `usually ${range} min for ${contentPages(usual.pages)}` : `usually ${range} min`;
}

/** "Usually 35–65 min for 99 content pages", before the check counts anything. */
export function usualSentence(usual: LiveUsual | null | undefined): string | null {
  const range = usualRange(usual);
  return range === null || !usual ? null : `Usually ${range} min for ${contentPages(usual.pages)}`;
}

function usualRange(usual: LiveUsual | null | undefined): string | null {
  if (!usual || usual.pages <= 0 || usual.high_minutes <= 0) return null;
  return usual.low_minutes === usual.high_minutes ? `${usual.high_minutes}` : `${usual.low_minutes}–${usual.high_minutes}`;
}

/** "99 content pages" (a no-break space keeps the number with its words). */
function contentPages(pages: number): string {
  return `${pages}\u00a0content ${pages === 1 ? 'page' : 'pages'}`;
}

/** The AI page matcher is pairing the vendor's pages with a separate architect's file. */
export function isMatching(live: LiveState): boolean {
  return live.phase.phase === 'comparing' && live.phase.step === 'matching';
}

/** Drawings compared so far and in all (skipped ones left out), for the sidebar row. */
export function drawingCounts(live: LiveState): { done: number | null; total: number | null } {
  // The same number as the card's bar and the tab ("one place per number": never two counts).
  if (live.phase.phase === 'comparing' && !isMatching(live) && live.phase.total) return { done: live.phase.done ?? 0, total: live.phase.total };
  if (!live.drawings.length) return { done: live.counts.drawings_done ?? null, total: live.counts.drawings_total ?? null };
  const counted = live.drawings.filter((drawing) => drawing.status !== 'skipped');
  return { done: counted.filter((drawing) => drawing.status === 'answered' || drawing.status === 'failed').length, total: counted.length };
}

/** "2:41": time since the check started. */
export function elapsedWords(live: LiveState, now: number): string {
  const seconds = Math.max(0, Math.floor((now - Date.parse(live.started_at)) / 1000));
  if (!Number.isFinite(seconds)) return '0:00';
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

/** The proved Wrongs to show, newest first: removed ones and repeats of the same item left out. */
export function provedWrongs(live: LiveState): LiveFinding[] {
  return live.findings.filter((finding) => finding.outcome === 'wrong' && !finding.removed && !finding.same_as).reverse();
}

/**
 * "Found so far" counted from the decided findings themselves, so the tally always equals the list
 * under it (one number per thing): the server's `check_tally` can come a few seconds after the
 * findings it counts. Removed items and repeats of the same item are not counted; the differences
 * waiting for the second look are only ever the server's count.
 */
export function tallyOf(live: LiveState): LiveTally {
  const counted = live.findings.filter((finding) => !finding.removed && !finding.same_as);
  const count = (outcome: LiveFinding['outcome']) => counted.filter((finding) => finding.outcome === outcome).length;
  return { wrong: count('wrong'), right: count('right'), rule_note: count('rule_note'), not_compared: count('not_compared'), waiting_second_look: live.tally.waiting_second_look };
}

/** How many proved Wrongs each drawing tile carries: on the newest tile of the finding's page. */
export function badgesByDrawing(live: LiveState): Map<string, number> {
  const badges = new Map<string, number>();
  for (const finding of provedWrongs(live)) {
    if (finding.page == null) continue;
    const tiles = live.drawings.filter((drawing) => drawing.page === finding.page);
    const tile = tiles[tiles.length - 1];
    if (tile) badges.set(tile.id, (badges.get(tile.id) ?? 0) + 1);
  }
  return badges;
}

/** The tab title while a check runs: "(23/39) Checking · GV Review". */
export function tabTitle(live: LiveState | null, base = 'GV Review'): string {
  if (!live) return base;
  const { phase, done, total } = live.phase;
  if (phase === 'comparing' && live.phase.step === 'matching') return `Matching pages · ${base}`;
  if (phase === 'comparing' && total) return `(${done ?? 0}/${total}) Checking · ${base}`;
  return `Checking · ${base}`;
}
