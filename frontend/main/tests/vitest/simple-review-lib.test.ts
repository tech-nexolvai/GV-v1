import { describe, expect, it } from 'vitest';

import { formatRoute, parseRoute } from '@/app/route';
import {
  agreeAction,
  disagreeAction,
  fixChoices,
  orderItems,
  pointsOf,
  ringOf,
  splitNumbers,
  summarize,
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

  it('the final cloud PDF is locked while anything is open, and says why', () => {
    const summary = summarize(data);
    expect(summary.openItems).toHaveLength(6);
    expect(summary.canFinalise).toBe(false);
    expect(summary.finalBlocked).toBe('6 items still need you.');
    expect(summary.nothingNeedsYou).toBe(false);
  });

  it('unlocks the final cloud PDF only when the server and the page agree', () => {
    expect(summarize(allSettled())).toMatchObject({ canFinalise: true, finalBlocked: null, nothingNeedsYou: true });
    const serverSaysNo = allSettled();
    serverSaysNo.header.can_finalise = false;
    serverSaysNo.header.blocked_reason = 'The package is not awaiting review.';
    expect(summarize(serverSaysNo)).toMatchObject({ canFinalise: false, finalBlocked: 'The package is not awaiting review.' });
  });

  it('a record missing from the page locks the final PDF', () => {
    const missing = allSettled();
    missing.unaccounted = [{ kind: 'finding', id: 'find-x' }];
    expect(summarize(missing).canFinalise).toBe(false);
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

  it('splits numbers out of plain words so they can use the number face', () => {
    expect(splitNumbers('39 1/2 in != 41 5/8 in').filter((part) => part.number).map((part) => part.text)).toEqual(['39 1/2 in', '41 5/8 in']);
  });
});
