/**
 * The full check's result screen, its words and groups (#1717). Made-up values only.
 */
import { describe, expect, it } from 'vitest';

import type { ChatFinding, ChatReviewAction } from '@/lib/chat-api';
import {
  boxOnPicture,
  clockWords,
  commentNumber,
  differenceWords,
  filesLine,
  itemParts,
  neededWords,
  notComparedGroups,
  resultGroups,
  shouldBeValue,
  vendorValue,
  verdictOf,
  workLine,
  workSteps,
} from '@/lib/chat-result';

const finding = (id: string, extra: Partial<ChatFinding> = {}): ChatFinding =>
  ({ id, page: 4, item: `Sample unit ${id}: overall width`, architect: '36"', vendor: '35"', status: 'different', why: '', verified: false, source: 'architect', ...extra }) as ChatFinding;

const action = (extra: Partial<ChatReviewAction> = {}): ChatReviewAction => ({
  action: 'revise_resubmit', rule: 'S3', words: 'Revise and resubmit', reason: '2 of 5 corrections change fabrication.', corrections: 5, fabrication: 2,
  next_step: 'Correct the drawings and send them again.', required_by: 'Vendor: revise and resubmit', disclaimer: 'Disclaimer.', ...extra,
});

describe('the result’s groups (#1717)', () => {
  it('puts each item where the cloud drawing puts it: Wrong biggest first, Right, Not compared by reason', () => {
    const groups = resultGroups([
      finding('a', { proof: { group: 'clouded' }, impact: { score: 40, rank: 3, why: '' } }),
      finding('b', { proof: { group: 'clouded' }, impact: { score: 90, rank: 1, why: '' } }),
      finding('c', { status: 'same', proof: { group: 'right' } }),
      finding('d', { proof: { group: 'not_compared', not_compared: 'unreadable' } }),
      finding('e', { proof: { group: 'not_compared', not_compared: 'field_verify' } }),
      finding('f', { proof: { group: 'clouded' } }),
    ]);
    expect(groups.wrong.map((each) => each.id)).toEqual(['b', 'a', 'f']);
    expect(groups.right.map((each) => each.id)).toEqual(['c']);
    // Plan change #8: "could not read" stays a question; a field-verify note has nothing to compare
    // and leaves the work list for the collapsed list at the bottom.
    expect(groups.notCompared.map((each) => each.id)).toEqual(['d']);
    expect(groups.aside.map((each) => each.id)).toEqual(['e']);
  });

  it('lays the reviewer’s Not a problem over the server’s group: listed apart, not counted as Wrong', () => {
    const groups = resultGroups([finding('a', { proof: { group: 'clouded' } })], () => ({ decision: 'not_a_problem' }) as never);
    expect(groups.wrong).toEqual([]);
    expect(groups.dismissed.map((each) => each.id)).toEqual(['a']);
  });

  it('groups Not compared under the list’s headings, each saying what is needed', () => {
    const groups = notComparedGroups([
      finding('x', { proof: { group: 'not_compared', not_compared: 'no_overall', settle: 'the architect’s printed overall' } }),
      finding('y', { status: 'same', compared: false }),
    ]);
    expect(groups.map((group) => [group.words, group.findings.length])).toEqual([
      ['Architect gives only the parts, no overall', 1],
      ['Not looked at on both drawings', 1],
    ]);
    expect(neededWords(groups[0].findings[0])).toBe('the architect’s printed overall');
    expect(neededWords(finding('z', { proof: { group: 'not_compared', not_compared: 'unreadable' } }))).toBe('a legible value on the drawing, or the next round');
  });
});

describe('a row’s words (#1717)', () => {
  it('splits the area from what was measured, never cutting it', () => {
    expect(itemParts('Sample kitchen: the left wall to outlet 2 of the row at 48" (centre)')).toEqual({ area: 'Sample kitchen', thing: 'The left wall to outlet 2 of the row at 48″ (centre)' });
    expect(itemParts('Countertop row on page 9')).toEqual({ area: null, thing: 'Countertop row on page 9' });
  });

  it('says the vendor’s value in inches first, with its millimetres apart', () => {
    expect(vendorValue({ vendor: '229 [9]' })).toEqual({ main: '9″', note: '229 mm' });
    expect(vendorValue({ vendor: '35 1/2"' })).toEqual({ main: '35 1/2″', note: null });
  });

  it('says what it should be, and where that comes from', () => {
    expect(shouldBeValue({ proof: { group: 'clouded', should_be: '12" (architect 1\' - 0")' }, architect: '1\' - 0"' })).toEqual({ main: '12″', note: 'architect 1′ - 0″' });
    expect(shouldBeValue({ proof: null, architect: '36"' })).toEqual({ main: '36″', note: null });
  });

  it('says the difference in words', () => {
    expect(differenceWords({ inches: { architect: '12', vendor: '9', difference: '-3' } })).toBe('3″ short');
    expect(differenceWords({ inches: { architect: '24', vendor: '24.125', difference: '0.125' } })).toBe('1/8″ over');
    expect(differenceWords({ inches: null })).toBeNull();
  });

  it('gives a Wrong item its comment number once the package is made', () => {
    expect(commentNumber({ id: 'F1' }, { state: {}, numbers: { F1: 'C-07' } })).toBe('C-07');
    expect(commentNumber({ id: 'F1' }, null)).toBeNull();
  });
});

describe('the review result (#1717)', () => {
  it('a finished check: AWI’s words, and a sentence with only what no count says', () => {
    expect(verdictOf(action())).toEqual({ stamp: 'Revise and resubmit', draft: false, pending: false, sentence: '2 of the wrong values change fabrication. Correct the drawings and send them again.' });
    expect(verdictOf(action({ fabrication: 0, action: 'approved_as_noted', words: 'Approved as noted' }))?.sentence).toBe('Correct the drawings and send them again.');
  });

  it('an unfinished check: a dashed stamp and a draft package, never "send"', () => {
    const verdict = verdictOf(action({ action: 'not_for_sending', rule: 'S0', words: 'Check incomplete: not for sending', reason: 'The check did not finish: it was stopped before the end.' }));
    expect(verdict).toEqual({ stamp: 'Check incomplete · not for sending', draft: true, pending: false, sentence: 'The check did not finish: it was stopped before the end. This package is a draft: do not send it yet.' });
    expect(verdict?.sentence).not.toMatch(/\bsend (?:it|the)\b(?! yet)/);
    expect(verdictOf(null)).toBeNull();
  });

  it('review needed: a pending stamp, the count a person decides, never "approved" or "send"', () => {
    const verdict = verdictOf(action({
      action: 'review_needed',
      rule: 'RN',
      words: 'Review needed',
      vendor_words: 'Under review',
      fabrication: 0,
      corrections: 0,
      not_compared: 5,
      items: 6,
      reason: '5 items need a person before this can be approved: 5 of 6 items (83.3%) were not compared, more than the 10% allowed.',
      next_step: 'Under review: GV will confirm the remaining items and then send its review result. Please wait for it before fabrication.',
    }));
    expect(verdict).toEqual({
      stamp: 'Review needed',
      draft: false,
      pending: true,
      sentence: '5 items need a person before this can be approved: 5 of 6 items (83.3%) were not compared, more than the 10% allowed. Decide them below; until then the package tells the vendor “Under review”.',
    });
    expect(verdict?.stamp).not.toMatch(/approved/i);
    expect(verdict?.sentence).not.toMatch(/\bsend (?:it|the)\b/);
  });
});

describe('the work line (#1717)', () => {
  it('says what the check did, the time last', () => {
    expect(workLine({ drawingsDone: 12, drawingsTotal: 12, pagesTotal: 20, filesMade: 5, seconds: 151, stopped: false })).toEqual({ words: 'Looked at 12 drawings on 20 pages · 5 files made', time: '2:31' });
    expect(workLine({ drawingsDone: 8, drawingsTotal: 12, pagesTotal: 20, filesMade: 0, seconds: null, stopped: true })).toEqual({ words: 'Looked at 8 of 12 drawings on 20 pages · stopped before the end', time: null });
    expect(clockWords(3725)).toBe('1:02:05');
  });

  it('lists the steps that took time, in plain words, in the order they happen', () => {
    expect(workSteps({ total_seconds: 100, cost_usd: '1.00', phases: [
      { phase: 'cloud_drawing', label: 'Cloud drawing', seconds: 2, cost_usd: '0.00', calls: 0 },
      { phase: 'report', label: 'Check report', seconds: 0.1, cost_usd: '0.00', calls: 0 },
      { phase: 'compare', label: 'Compare', seconds: 90, cost_usd: '1.00', calls: 12 },
    ], helpers: [] } as never)).toEqual([
      { words: 'Compared each vendor drawing with the architect’s', time: '1:30' },
      { words: 'Made the cloud drawing', time: '0:02' },
    ]);
  });

  it('names the package’s files in one line', () => {
    const file = { id: '1', name: 'x', size: 1, mime: 'application/pdf', url: '' };
    expect(filesLine({ cloud: file, report: file, notCompared: [file], pkg: file })).toEqual({ count: 5, words: 'cloud drawing, comment letter, issue register, Not-compared list, check report' });
  });

  it('puts a box from PDF points onto the 100 dpi page picture', () => {
    const box = boxOnPicture({ x0: 72, y0: 144, x1: 108, y1: 151.2 })!;
    expect(Object.fromEntries(Object.entries(box).map(([key, value]) => [key, Math.round(value * 1000) / 1000]))).toEqual({ left: 100, top: 200, width: 50, height: 10 });
    expect(boxOnPicture(null)).toBeNull();
  });
});

describe('areaWords (#1741 review)', () => {
  it('drops the drawing list\'s own numbering, and keeps names that only start like it', async () => {
    const { areaWords, itemParts } = await import('@/lib/chat-result');
    expect(areaWords('2.Standard Kitchenette')).toBe('Standard Kitchenette');
    expect(areaWords('2. Coffee station')).toBe('Coffee station');
    expect(areaWords('B. Bar')).toBe('Bar');
    expect(areaWords('12) Reception desk')).toBe('Reception desk');
    expect(areaWords('B.O.H. corridor')).toBe('B.O.H. corridor');
    expect(areaWords('3.5 m wall')).toBe('3.5 m wall');
    expect(areaWords('Bar')).toBe('Bar');
    expect(areaWords('  ')).toBeNull();
    expect(itemParts('1. Connection Centre: overall width').area).toBe('Connection Centre');
  });
});
