import { describe, expect, it } from 'vitest';

import { chatEventOf } from '@/lib/chat-api';
import {
  applyLive,
  drawingCounts,
  isMatching,
  progressOf,
  usualWords,
  badgesByDrawing,
  barShare,
  barWords,
  elapsedWords,
  leftWords,
  nowWords,
  phaseTitle,
  provedWrongs,
  secondsLeft,
  stepOf,
  tabTitle,
  type LiveDrawing,
  type LiveEvent,
  type LiveFinding,
  type LivePhase,
  type LiveState,
} from '@/lib/chat-live';
import { applyChatEvent, followedAnswer, shownLive } from '@/lib/chat-messages';

const AT = '2026-10-19T10:00:00.000Z';
const at = (seconds: number) => Date.parse(AT) + seconds * 1000;

const phase = (over: Partial<LivePhase> = {}): LivePhase => ({ phase: 'comparing', step: null, done: 0, total: 39, unit: 'drawings', started_at: AT, phase_started_at: AT, stopping: false, ...over });
const USUAL = { low_minutes: 35, high_minutes: 60, pages: 99, matched: 96 };
const drawing = (id: string, page: number, status: LiveDrawing['status'], area = 'Kitchen'): LiveDrawing => ({ id, page, status, area, round: 1, reason: null });
const finding = (key: string, over: Partial<LiveFinding> = {}): LiveFinding => ({
  key, id: null, outcome: 'wrong', proved_by: 'code', page: 16, area: 'Kitchen', item: 'Kitchen: overall width', vendor: '24', architect: '25', should_be: '25"', product: 'countertops',
  reason: null, needs: null, impact: null, same_as: null, removed: false, previous: null, final: false, ...over,
});
const ev = <E extends LiveEvent['event']>(event: E, data: Record<string, unknown>) => ({ event, data: { v: 1, ...data } }) as LiveEvent;

function run(...events: LiveEvent[]): LiveState {
  let live: LiveState | null = null;
  for (const event of events) live = applyLive(live, event, AT);
  if (!live) throw new Error('no live state');
  return live;
}

describe('the live events reach the screen', () => {
  it('parses every check_* event from the stream (unknown ones are still dropped)', () => {
    for (const name of ['check_phase', 'check_drawings', 'check_drawing', 'check_finding', 'check_tally', 'check_stopping', 'check_service']) {
      expect(chatEventOf({ event: name, data: { v: 1 } })?.event).toBe(name);
    }
    expect(chatEventOf({ event: 'check_something_new', data: { v: 1 } })).toBeNull();
  });

  it('a followed answer gets its live state and learns it is a full check', () => {
    let message = followedAnswer('m1');
    message = applyChatEvent(message, ev('check_phase', { phase: phase({ phase: 'getting_ready', done: null, total: null }) }) as never);
    expect(message.mode).toBe('full_check');
    expect(message.live?.phase.phase).toBe('getting_ready');
  });

  it('after a reload the snapshot stays on screen until the replayed events catch up (never a step back)', () => {
    const seed = run(ev('check_phase', { phase: phase({ phase: 'double_checking', step: 'verifying', done: 7, total: 39 }) }));
    let message = followedAnswer('m1', new Date(), seed);
    expect(shownLive(message)).toBe(seed);
    message = applyChatEvent(message, ev('check_phase', { phase: phase({ phase: 'getting_ready', done: null, total: null }) }) as never);
    message = applyChatEvent(message, ev('check_phase', { phase: phase({ done: 39 }) }) as never);
    expect(shownLive(message)).toBe(seed);
    message = applyChatEvent(message, ev('check_phase', { phase: phase({ phase: 'double_checking', step: 'verifying', done: 7, total: 39 }) }) as never);
    expect(message.liveSeed).toBeNull();
    expect(shownLive(message)?.phase.done).toBe(7);
  });

  it('skips a newer version it cannot read', () => {
    expect(applyLive(null, { event: 'check_phase', data: { v: 2, phase: phase() } } as LiveEvent)).toBeNull();
  });
});

describe('applyLive', () => {
  it('keeps one tile per drawing id and updates it in place', () => {
    const live = run(
      ev('check_drawings', { drawings: [drawing('p3.v1', 3, 'waiting'), drawing('p6.v1', 6, 'waiting')] }),
      ev('check_drawing', { drawing: drawing('p6.v1', 6, 'comparing') }),
      ev('check_drawing', { drawing: drawing('p6.v1', 6, 'answered') }),
      ev('check_drawing', { drawing: drawing('p6.v1', 6, 'answered') }),
    );
    expect(live.drawings.map((each) => `${each.id}:${each.status}`)).toEqual(['p3.v1:waiting', 'p6.v1:answered']);
  });

  it('keeps a finding where it first arrived and replaces it by key (a replay changes nothing)', () => {
    const first = ev('check_finding', { finding: finding('a', { outcome: 'right' }) });
    const live = run(first, ev('check_finding', { finding: finding('b') }), ev('check_finding', { finding: finding('a', { outcome: 'not_compared' }) }), first);
    expect(live.findings.map((each) => each.key)).toEqual(['a', 'b']);
    expect(live.findings[0].outcome).toBe('right');
  });

  it('marks stopping and the service failure', () => {
    const live = run(ev('check_phase', { phase: phase() }), ev('check_stopping', { stopping: { in_flight: 8, not_started: 20 } }), ev('check_service', { reason: 'The AI service refused the key.' }));
    expect(live.phase.stopping).toBe(true);
    expect(live.stopping).toEqual({ in_flight: 8, not_started: 20 });
    expect(phaseTitle(live)).toBe('The check could not go on');
  });
});

describe('the words', () => {
  it('names the step and the phase', () => {
    expect(stepOf('getting_ready')).toBe(0);
    expect(stepOf('double_checking')).toBe(2);
    expect(stepOf('done')).toBe(4);
    expect(phaseTitle(run(ev('check_phase', { phase: phase() })))).toBe('Comparing drawings');
    expect(phaseTitle(run(ev('check_phase', { phase: phase() }), ev('check_stopping', { stopping: { in_flight: 2, not_started: 1 } })))).toBe('Stopping…');
  });

  it('says which drawing is being compared now, and how many more at the same time', () => {
    const live = run(
      ev('check_phase', { phase: phase() }),
      ev('check_drawings', { drawings: [drawing('p3.v1', 3, 'comparing', 'Bar'), drawing('p47.v1', 47, 'comparing', 'Buffet small'), drawing('p8.v1', 8, 'waiting')] }),
    );
    expect(nowWords(live)).toEqual({ words: 'with the architect’s', page: 47, area: 'Buffet small', more: 1 });
  });

  it('says what stopping does', () => {
    const live = run(ev('check_phase', { phase: phase() }), ev('check_stopping', { stopping: { in_flight: 8, not_started: 20 } }));
    expect(nowWords(live).words).toBe('Finishing the 8 drawings already being compared; nothing new starts');
  });

  it('gives no share for a step with no count (an indeterminate bar), and a share otherwise', () => {
    expect(barShare(run(ev('check_phase', { phase: phase({ phase: 'getting_ready', done: null, total: null }) })))).toBeNull();
    expect(barShare(run(ev('check_phase', { phase: phase({ phase: 'double_checking', step: 'final_checks', done: null, total: null }) })))).toBeNull();
    expect(barShare(run(ev('check_phase', { phase: phase({ phase: 'double_checking', step: 'code_checks', done: null, total: null }) })))).toBeNull();
    expect(barShare(run(ev('check_phase', { phase: phase({ done: 0 }) })))).toBe(5);
    expect(barShare(run(ev('check_phase', { phase: phase({ done: 39 }) })))).toBe(80);
  });

  it('counts the bar in plain words', () => {
    expect(barWords(run(ev('check_phase', { phase: phase({ done: 23 }) })))).toBe('23 of\u00a039 drawings compared');
    expect(barWords(run(ev('check_phase', { phase: phase({ phase: 'double_checking', step: 'second_looks', done: 1, total: 4, unit: 'looks' }) })))).toBe('All drawings compared · second look 1 of\u00a04');
    expect(barWords(run(ev('check_phase', { phase: phase({ phase: 'getting_ready', done: null, total: null }) })))).toBe('');
    // The final checks by code count each check's items (a long code step is never a still bar).
    const counted = run(ev('check_phase', { phase: phase({ phase: 'double_checking', step: 'final_checks', done: 45, total: 125, unit: 'items' }) }));
    expect(barWords(counted)).toBe('All drawings compared · checking 45 of\u00a0125 items');
    expect(barShare(counted)).toBeNull();
    expect(barWords(run(ev('check_phase', { phase: phase({ phase: 'double_checking', step: 'final_checks', done: null, total: null, unit: null }) })))).toBe('All drawings compared · checking by code');
    expect(barWords(run(ev('check_phase', { phase: phase({ phase: 'getting_ready', done: null, total: null, usual: USUAL }) })))).toBe('Usually 35–60 min for 99\u00a0content pages');
  });

  it('gives time left only from the server, or from the pace once 3 drawings are done', () => {
    const early = run(ev('check_phase', { phase: phase({ done: 2 }) }));
    expect(secondsLeft(early, at(60), null)).toBeNull();
    const later = run(ev('check_phase', { phase: phase({ done: 10, total: 40 }) }));
    expect(secondsLeft(later, at(100), null)).toBe(360);
    expect(secondsLeft(later, at(100), 95)).toBe(95);
    expect(leftWords(30)).toBe('under a minute left');
    expect(leftWords(360)).toBe('about 6 min left');
    expect(leftWords(null)).toBeNull();
  });

  it('shows the elapsed clock', () => {
    expect(elapsedWords(run(ev('check_phase', { phase: phase() })), at(161))).toBe('2:41');
  });

  it('lists only proved Wrongs, newest first, never a removed one or a repeat', () => {
    const live = run(
      ev('check_drawings', { drawings: [drawing('p16.v1', 16, 'answered'), drawing('p16.v2', 16, 'answered'), drawing('p3.v1', 3, 'answered')] }),
      ev('check_finding', { finding: finding('a') }),
      ev('check_finding', { finding: finding('b', { outcome: 'right' }) }),
      ev('check_finding', { finding: finding('c', { removed: true }) }),
      ev('check_finding', { finding: finding('d', { same_as: 'a' }) }),
      ev('check_finding', { finding: finding('e', { page: 3, proved_by: 'checked_twice' }) }),
    );
    expect(provedWrongs(live).map((each) => each.key)).toEqual(['e', 'a']);
    expect(Object.fromEntries(badgesByDrawing(live))).toEqual({ 'p16.v2': 1, 'p3.v1': 1 });
  });

  it('puts the count in the tab title while comparing', () => {
    expect(tabTitle(run(ev('check_phase', { phase: phase({ done: 23 }) })))).toBe('(23/39) Checking · GV Review');
    expect(tabTitle(run(ev('check_phase', { phase: phase({ phase: 'making_files', done: 1, total: 5 }) })))).toBe('Checking · GV Review');
    expect(tabTitle(null)).toBe('GV Review');
  });
});

describe('matching pages first (#1883)', () => {
  const matching = (done: number, over: Partial<LivePhase> = {}) => ev('check_phase', { phase: phase({ step: 'matching', done, total: 96, unit: 'drawings', ...over }) });

  it('names the matching step with its count, and what happens meanwhile', () => {
    const live = run(matching(74));
    expect(isMatching(live)).toBe(true);
    expect(phaseTitle(live)).toBe('Matching pages');
    expect(barWords(live)).toBe('74 of\u00a096 drawings matched to the architect’s');
    expect(nowWords(live).words).toBe('Pairing vendor pages with the architect’s; each starts once paired');
    expect(tabTitle(live)).toBe('Matching pages · GV Review');
  });

  it('keeps the bar in its first few percent and never steps back when comparing starts', () => {
    expect(barShare(run(matching(0)))).toBe(2);
    expect(barShare(run(matching(96)))).toBe(5);
    const pages = run(ev('check_phase', { phase: phase({ done: 0, total: 109, unit: 'pages' }) }));
    expect(barShare(pages)).toBe(5);
    expect(progressOf(run(matching(96)))).toBeLessThan(progressOf(pages));
  });

  it('gives no pace-based time while matching, and the usual range for this set instead', () => {
    const live = run(matching(50, { usual: USUAL }));
    expect(secondsLeft(live, at(600), null)).toBeNull();
    expect(usualWords(live)).toBe('usually 35–60 min for 99\u00a0content pages');
    // Comparing, before 3 pages: the range (the bar counts the pages); after: the estimate.
    expect(usualWords(run(ev('check_phase', { phase: phase({ done: 1, total: 109, unit: 'pages', usual: USUAL }) })))).toBe('usually 35–60 min');
    expect(usualWords(run(ev('check_phase', { phase: phase({ done: 5, total: 109, unit: 'pages', usual: USUAL }) })))).toBeNull();
  });

  it('says no time rather than a wrong one when the server gives no range', () => {
    expect(usualWords(run(matching(10)))).toBeNull();
    expect(usualWords(run(ev('check_phase', { phase: phase({ done: 0 }) })))).toBeNull();
  });

  it('never counts the matcher’s drawings as drawings compared in the sidebar', () => {
    const live = run(matching(74), ev('check_drawings', { drawings: [drawing('p3.v1', 3, 'answered'), drawing('p4.v1', 4, 'waiting')] }));
    expect(drawingCounts(live)).toEqual({ done: 1, total: 2 });
  });
});
