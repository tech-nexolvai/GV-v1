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
  notConfirmedGroups,
  unansweredWords,
} from '@/lib/review-items';
import { allSettled, optionalOpen, reviewItems } from './fixtures/review-items';

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
    // Every question is optional (owner, 2026-10-16): "6 optional", never "6 items need you".
    expect(leftWords(data.header)).toEqual(['6 optional', '6 not checked', '1 item re-checking']);
    expect(leftWords({ open: 9, not_checked: 54, rechecking: 0 })).toEqual(['9 optional', '54 not checked']);
    expect(leftWords({ open: 1, not_checked: 0, rechecking: 0 })).toEqual(['1 optional']);
    // The lists are never counted again: the header's numbers are the ones printed.
    const listsDisagree = reviewItems((draft) => { draft.header.open = 9; });
    expect(leftWords(listsDisagree.header)[0]).toBe('9 optional');
    expect(leftWords(allSettled().header)).toEqual([]);
    expect(leftWords(data.header).join(' ')).not.toMatch(/need you|needs you/);
  });

  it('says how many unanswered items the final PDF prints as not confirmed, singular and plural', () => {
    expect(unansweredWords(3)).toBe('Ready to create the final cloud PDF — 3 items left unanswered will be printed as not confirmed');
    expect(unansweredWords(1)).toBe('Ready to create the final cloud PDF — 1 item left unanswered will be printed as not confirmed');
  });

  it('groups what will be printed as not confirmed by kind, in plain words, with open failures last', () => {
    // The fixture's open items: 2 numbers, 1 depth, 1 countertop by hand, 1 wall, 1 page; 2 open failures.
    expect(notConfirmedGroups(optionalOpen())).toEqual([
      { label: 'Numbers to confirm', count: 2 },
      { label: 'Depths to confirm', count: 1 },
      { label: 'Countertops to check by hand', count: 1 },
      { label: 'Wall choices', count: 1 },
      { label: 'Pages to decide', count: 1 },
      { label: 'Clouded as wrong, not agreed by a person', count: 2 },
    ]);
    // Done and re-checking items are never counted; both cabinet kinds share one line; an unknown kind has words too.
    const mixed = notConfirmedGroups({
      items: [
        { kind: 'cabinet_kinds', status: 'open' },
        { kind: 'cabinet_run', status: 'open' },
        { kind: 'architect', status: 'open' },
        { kind: 'architect', status: 'done' },
        { kind: 'number', status: 'rechecking' },
        { kind: 'other_result', status: 'open' },
        { kind: 'made_up_kind' as never, status: 'open' },
      ],
      failures: [{ status: 'done' }],
    });
    expect(mixed).toEqual([
      { label: 'Architect questions', count: 1 },
      { label: 'Cabinet marks', count: 2 },
      { label: 'Other results to check', count: 1 },
      { label: 'Other questions', count: 1 },
    ]);
    expect(notConfirmedGroups(allSettled())).toEqual([]);
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

describe('answerWord: "Checked by hand: wrong" (QA 3)', () => {
  it('says WRONG and who, from the mark, else the usual words', async () => {
    const { answerWord } = await import('@/lib/review-items');
    expect(answerWord({ decided_by: 'REVIEWER', checked_wrong_by_hand: true })).toBe('Checked by hand: WRONG by you');
    expect(answerWord({ decided_by: 'Sample Checker', checked_wrong_by_hand: true })).toBe('Checked by hand: WRONG by Sample Checker');
    expect(answerWord({ decided_by: null, checked_wrong_by_hand: true })).toBe('Checked by hand: WRONG');
    expect(answerWord({ decided_by: 'REVIEWER', checked_wrong_by_hand: false })).toBe('Checked by you');
    // Older answers without the field read as before.
    expect(answerWord({ decided_by: 'AI' })).toBe('Confirmed by AI');
  });
});
