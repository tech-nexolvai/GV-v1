import { describe, expect, it } from 'vitest';

import { formatRoute, parseRoute } from '@/app/route';
import {
  agreeAction,
  disagreeAction,
  fixChoices,
  orderItems,
  pageWord,
  whereWord,
  pointsOf,
  proposedWall,
  readerWord,
  ringOf,
  splitNumbers,
  summarize,
  leftWords,
  fixValue,
  pagesWord,
  openNoCountertopPages,
} from '@/lib/review-items';
import { allSettled, reviewItems } from './fixtures/review-items';

// Synthetic data only: nothing here comes from a client drawing.

describe('simple review route (#1343)', () => {
  it('opens the simple page at #/review/<id>, and the old tabs at #/review/<id>/advanced', () => {
    expect(parseRoute('#/review/pkg-1')).toEqual({ page: 'review', packageId: 'pkg-1' });
    expect(parseRoute('#/review/pkg-1/advanced')).toEqual({ page: 'review', packageId: 'pkg-1', advanced: true });
    expect(formatRoute({ page: 'review', packageId: 'pkg-1', advanced: true })).toBe('#/review/pkg-1/advanced');
    expect(formatRoute({ page: 'review', packageId: 'pkg-1' })).toBe('#/review/pkg-1');
  });

  it('an unknown tail is the simple page, never the advanced one', () => {
    expect(parseRoute('#/review/pkg-1/other')).toEqual({ page: 'review', packageId: 'pkg-1' });
  });
});

describe('simple review rules', () => {
  const data = reviewItems();
  const item = (key: string) => data.items.find((entry) => entry.key === key)!;

  it('a wall choice pre-selects the readers\' proposal; with none, nothing is picked (#1358)', () => {
    expect(proposedWall(item('wall:row-7'))).toBe('back_left_right');
    expect(proposedWall({ wall_choices: item('wall:row-7').wall_choices!.map((choice) => ({ ...choice, proposed: false })) })).toBeNull();
    expect(proposedWall(item('number:cand-101'))).toBeNull();
  });

  it('the PDF\'s own text is named as such beside the AIs\' readings', () => {
    expect(readerWord('pdf-text-layer')).toBe('PDF text');
    expect(readerWord('MODEL_A')).toBe('AI 1');
  });

  it('C agrees with the AI and F disagrees, for every kind of item', () => {
    expect([agreeAction(item('number:cand-101')), disagreeAction(item('number:cand-101'))]).toEqual(['confirm', 'fix']);
    expect([agreeAction(item('countertop:find-5')), disagreeAction(item('countertop:find-5'))]).toEqual(['checked_right', 'checked_wrong']);
    expect([agreeAction(item('page:rec-9')), disagreeAction(item('page:rec-9'))]).toEqual(['confirm_no_countertop', 'there_is_a_countertop']);
    expect([agreeAction(item('number:cand-140')), disagreeAction(item('number:cand-140'))]).toEqual(['confirm', 'not_this']);
    expect([agreeAction(item('wall:row-7')), disagreeAction(item('wall:row-7'))]).toEqual(['choose_wall', null]);
  });

  it('a done or re-checking item offers no answer', () => {
    expect(agreeAction(item('number:cand-090'))).toBeNull();
    expect(disagreeAction(item('number:cand-120'))).toBeNull();
  });

  it('Fix offers the other AI readings, once each, and nothing to type', () => {
    expect(fixChoices(item('number:cand-101'))).toEqual([{ text: '96 3/4"', readers: ['AI 2'] }]);
    // Both AIs read the guess: there is no other reading to pick.
    expect(fixChoices(item('number:cand-102'))).toEqual([]);
  });

  it('orders open first, then re-checking, then done, by page', () => {
    expect(orderItems(data.items).map((entry) => `${entry.status}:${entry.page}`)).toEqual([
      'open:3', 'open:3', 'open:5', 'open:6', 'open:7', 'open:9', 'rechecking:4', 'done:2',
    ]);
  });

  it('the final cloud PDF is locked while the server says so, in its own words (#1368)', () => {
    const summary = summarize(data);
    expect(summary.openItems).toHaveLength(6);
    expect(summary.canFinalise).toBe(false);
    expect(summary.finalBlocked).toBe('6 items still need you.');
    expect(summary.nothingNeedsYou).toBe(false);
  });

  it('readiness is the server\'s answer, never worked out on the screen (#1368)', () => {
    expect(summarize(allSettled())).toMatchObject({ canFinalise: true, finalBlocked: null, nothingNeedsYou: true });
    const serverSaysNo = allSettled();
    serverSaysNo.header.can_finalise = false;
    serverSaysNo.header.blocked_reason = 'The package is not awaiting review.';
    expect(summarize(serverSaysNo)).toMatchObject({ canFinalise: false, finalBlocked: 'The package is not awaiting review.' });
    // Items still open on the screen do not lock what the server unlocked, nor the other way round.
    const serverSaysYes = reviewItems((draft) => { draft.header.can_finalise = true; draft.header.blocked_reason = null; });
    expect(summarize(serverSaysYes)).toMatchObject({ canFinalise: true, finalBlocked: null });
    const noWords = allSettled();
    noWords.header.can_finalise = false;
    noWords.header.blocked_reason = null;
    expect(summarize(noWords).finalBlocked).toBe('The server has not unlocked the final cloud PDF yet.');
  });

  it('says what is left in plain words with the header\'s own counts, only those above zero (#1394)', () => {
    expect(leftWords(data.header)).toEqual(['6 items need you', '6 not checked', '1 item re-checking']);
    expect(leftWords({ open: 9, not_checked: 54, rechecking: 0 })).toEqual(['9 items need you', '54 not checked']);
    expect(leftWords({ open: 1, not_checked: 0, rechecking: 0 })).toEqual(['1 item needs you']);
    // The lists are never counted again: the header's numbers are the ones printed.
    const listsDisagree = reviewItems((draft) => { draft.header.open = 9; });
    expect(leftWords(listsDisagree.header)[0]).toBe('9 items need you');
    expect(leftWords(allSettled().header)).toEqual([]);
  });

  it('a Fix typed as a bare number is in inches; anything else is sent as typed (#1368)', () => {
    expect(fixValue(' 96 3/4 ')).toBe('96 3/4"');
    expect(fixValue('42.5')).toBe('42.5"');
    expect(fixValue('3/4')).toBe('3/4"');
    expect(fixValue('96   1/2')).toBe('96 1/2"');
    expect(fixValue('96 1/2"')).toBe('96 1/2"');
    expect(fixValue("8' 1/2\"")).toBe("8' 1/2\"");
    expect(fixValue('1200 mm')).toBe('1200 mm');
    expect(fixValue('')).toBe('');
  });

  it('names pages "p.2, p.4 and p.5" and picks the open no-countertop pages (#1368)', () => {
    expect(pagesWord([2, 4, 5])).toBe('p.2, p.4 and p.5');
    expect(pagesWord([9])).toBe('p.9');
    expect(openNoCountertopPages(data.items).map((entry) => entry.key)).toEqual(['page:rec-9']);
  });

  it('rings a spot a little larger than the spot, never smaller than 3% of the page', () => {
    const points = pointsOf(item('number:cand-101').vendor_ref)!;
    const ring = ringOf(points);
    expect(ring.cx).toBeCloseTo(0.48);
    expect(ring.cy).toBeCloseTo(0.325);
    expect(ring.rx).toBeGreaterThan(0.1);
    expect(ringOf([[0.5, 0.5], [0.501, 0.5], [0.501, 0.501]]).rx).toBe(0.03);
    expect(pointsOf({ polygon: [['0.x', '0.x'], ['1', '1'], ['0', '1']] })).toBeNull();
  });

  it('never says "p.null": a missing page or place has plain words', () => {
    expect(pageWord(null)).toBe('');
    expect(whereWord({ countertop: null, row_label: null, page: null })).toBe('Place not recorded');
    expect(whereWord({ page: 4 })).toBe('Page 4');
  });

  it('splits numbers out of plain words so they can use the number face', () => {
    expect(splitNumbers('39 1/2 in != 41 5/8 in').filter((part) => part.number).map((part) => part.text)).toEqual(['39 1/2 in', '41 5/8 in']);
  });
});
