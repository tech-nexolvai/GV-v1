// @vitest-environment jsdom
/**
 * The drawer of a card that checks the vendor's own numbers (2026-10-31, the owner's approved
 * mock-up): the right-hand card adds the printed parts up against the printed overall (each number
 * close up when code placed it, its value alone when not) instead of showing the vendor's picture a
 * second time; the left card boxes each number apart. A card with no design picture says why on one
 * reason line, chosen from the check's own coverage notes; the "not in the upload" chip only when
 * the upload truly has no design file. Cards that compare two drawings are unchanged. Made-up values.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatCoverage, ChatFinding, CoveragePage, PageWhere } from '@/lib/chat-api';
import { chatPaths } from '@/lib/chat-api';
import { designFileInUpload, ownNumbers, ownSum, printedInches, reasonLine, unionBox } from '@/lib/chat-drawer';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

const FILE = 'Made-up vendor set.pdf';
const box = (x0: number, y0: number, x1: number, y1: number) => ({ page: 4, x0, y0, x1, y1, file: FILE });

/** A made-up height row: 3" + 50" + 3" = 56" against a printed 54" overall, a likely problem. */
const ROW = finding('s1', {
  page: 4, item: 'Section view B: height string', source: 'vendor_arithmetic', created_by: 'page_reviewer', arch_picture_id: null, status: 'different',
  vendor: '54"', architect: '56"', parts: ['3"', '50"', '3"'], parts_sum: '56', printed_total: '54"', vendor_box: box(180, 280, 215, 330),
  why: 'The row adds up to 56", not the printed 54" overall. (page review)',
  proof: { group: 'likely_problem', reason: 'The row adds up to 56", not the printed 54" overall.', likely_problem: { kind: 'size', words: 'Please check and confirm: the row adds up to 56", not the printed 54" overall.', reason: 'The row adds up to 56", not the printed 54" overall.', sure: 'sure', by: 'page_reviewer' } },
});

const SPOTS: Pick<ChatFinding, 'part_boxes' | 'total_box'> = {
  part_boxes: [
    { text: '3"', inches: '3', box: box(186, 100, 198, 112), turned: true },
    { text: '50"', inches: '50', box: box(186, 300, 198, 316), turned: true },
    { text: '3"', inches: '3', box: null },
  ],
  total_box: { text: '54"', inches: '54', box: box(204, 300, 208, 312), turned: true },
};

/** A proven Wrong against the architect, with both crops: unchanged. */
const PAIR = finding('w1', {
  page: 5, item: 'Sample unit C: sink base width', vendor: '46"', architect: '37"', vs_page: 9, arch_box: { page: 9, x0: 300, y0: 200, x1: 340, y1: 214, file: 'Made-up design set.pdf' },
  vendor_box: { page: 5, x0: 10, y0: 10, x1: 40, y1: 20, file: FILE }, proof: { group: 'clouded', should_be: '37" (architect, page 9)' },
});

const UNSURE: CoveragePage = { page: 4, status: 'not_comparable', note: 'Not compared with the architect: pairing unsure (…)', pairing_unsure: true, pairing_unsure_why: 'architect page 12 or architect page 15?', pairing_unsure_pages: [{ page: 12 }, { page: 15 }] };
const NOT_FOUND: CoveragePage = { page: 6, status: 'not_comparable', drawing_needed: true, drawing_needed_why: 'no drawing of the same item was found in the architect’s file' };
const TAG: CoveragePage = { page: 7, status: 'not_comparable', drawing_needed: true, drawing_needed_why: 'the architect shows it only as a tag on a plan' };
const NO_SIZES: CoveragePage = { page: 8, status: 'not_comparable', drawing_needed: true, drawing_needed_why: 'the architect shows this item without sizes' };
const NO_FILE: CoveragePage = { page: 4, status: 'not_comparable', drawing_needed: true, drawing_needed_why: 'the architect’s file has no drawing to pair it with' };

function answer(findings: ChatFinding[], coverage: ChatCoverage): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-31T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Done.' }], files: [], tasks: null, cost_usd: '4.10', stopped: null, error: null, status: 'done', findings, coverage,
  };
}

const coverage = (...pages: CoveragePage[]): ChatCoverage => ({ pages: [{ page: 5, status: 'different', pair_page: 9 }, ...pages], counts: null });
const pagePicture = (page: number, where?: PageWhere) => chatPaths.planPage('p', page, where);

function open(findings: ChatFinding[], cover: ChatCoverage, id: string) {
  render(<AssistantMessage message={answer(findings, cover)} chatId="c-1" models={[OPUS]} fullCheck pagePicture={pagePicture} focusFindingId={id} />);
  return screen.getByRole('dialog');
}

const sides = (panel: HTMLElement) => [...panel.querySelectorAll<HTMLElement>('[data-slot="chat-evidence-side"]')];

describe('the vendor’s own numbers, without React', () => {
  it('knows a card that checks the vendor’s own numbers from one that compares two drawings', () => {
    expect(ownNumbers(ROW)).toBe(true);
    expect(ownNumbers({ ...ROW, source: 'architect', created_by: 'consistency', parts: null })).toBe(true);
    expect(ownNumbers({ ...ROW, source: 'gv_review', created_by: 'page_reviewer' })).toBe(true);
    expect(ownNumbers({ ...ROW, source: 'gv_review', created_by: 'page_reviewer', parts: null })).toBe(false);
    expect(ownNumbers({ ...ROW, source: 'cross_view' })).toBe(false);
    expect(ownNumbers(PAIR)).toBe(false);
    expect(ownNumbers({ ...ROW, arch_picture_id: 'a-1' })).toBe(false);
  });

  it('reads printed sizes for the sum only, never guessing', () => {
    expect(printedInches('2 1/2"')).toBe(2.5);
    expect(printedInches('70 1/2')).toBe(70.5);
    expect(printedInches('3/4″')).toBe(0.75);
    expect(printedInches('6′-0″')).toBe(72);
    expect(printedInches(`3' - 1 1/2"`)).toBe(37.5);
    expect(printedInches('about 3')).toBeNull();
    expect(printedInches('')).toBeNull();
  });

  it('adds the parts up against the overall, with each number’s spot when code placed it', () => {
    const sum = ownSum({ ...ROW, ...SPOTS })!;
    expect(sum.parts.map((part) => [part.value, Boolean(part.box), part.turned])).toEqual([['3″', true, true], ['50″', true, true], ['3″', false, false]]);
    expect(sum.sum).toBe('56″');
    expect(sum.total).toMatchObject({ value: '54″', turned: true });
    expect(sum.difference).toBe(2);
    // No spots: the values alone; no code sum given: the printed parts added.
    const bare = ownSum({ ...ROW, parts_sum: null, parts: ['3', '50 1/2', '3'] })!;
    expect(bare.parts.every((part) => part.box === null)).toBe(true);
    expect(bare.parts.map((part) => part.value)).toEqual(['3″', '50 1/2″', '3″']);
    expect(bare.sum).toBe('56 1/2″');
    expect(bare.difference).toBe(2.5);
    expect(ownSum({ ...ROW, parts: [] })).toBeNull();
    expect(ownSum({ ...ROW, printed_total: null })).toBeNull();
    expect(unionBox([box(186, 100, 198, 112), null, box(204, 300, 208, 312)])).toEqual({ page: 4, file: FILE, x0: 186, y0: 100, x1: 208, y1: 312 });
    expect(unionBox([box(1, 1, 2, 2), { ...box(1, 1, 2, 2), page: 5 }])).toBeNull();
  });

  it('knows whether the upload holds a design file from what the result says', () => {
    expect(designFileInUpload(coverage())).toBe(true);
    expect(designFileInUpload({ pages: [UNSURE] })).toBe(true);
    expect(designFileInUpload({ pages: [NOT_FOUND] })).toBe(true);
    expect(designFileInUpload({ pages: [NO_FILE] })).toBe(false);
    expect(designFileInUpload({ pages: [NO_FILE] }, [PAIR])).toBe(true);
    expect(designFileInUpload(null)).toBe(false);
  });

  it('picks each of the five reasons from the check’s own notes', () => {
    const none = reasonLine(ROW, { designFile: false, page: NO_FILE, inRequest: true })!;
    expect([none.tag, none.lead, none.rest, none.act]).toEqual(['NO DESIGN FILE', 'No design drawings in this upload.', 'This page is in the drawings request to GV.', 'Send the architect’s or designer’s drawings of this item.']);
    const unsure = reasonLine(ROW, { designFile: true, page: UNSURE })!;
    expect([unsure.tag, unsure.lead, unsure.rest, unsure.act]).toEqual(['NOT CONFIRMED', 'The design set is uploaded, but the page for this item wasn’t confirmed', '(design page 12 or 15?).', 'A person confirms which design page shows it.']);
    const tag = reasonLine(ROW, { designFile: true, page: TAG })!;
    expect([tag.key, tag.lead, tag.rest]).toEqual(['tag_only', 'The design shows this item only as a tag on a plan', ': no elevation and no sizes to compare.']);
    // Code's own words name the tag and its plan page.
    const named = reasonLine({ ...ROW, proof: { group: 'not_compared', reason: 'the architect shows this item only as a tag on a plan (U-7 on its page 19, no callout beside it), with no sizes' } }, { designFile: true, page: TAG })!;
    expect(named.lead).toBe('The design shows U-7 only as a tag on plan p19');
    const sizes = reasonLine(ROW, { designFile: true, page: NO_SIZES })!;
    expect([sizes.key, sizes.lead, sizes.rest]).toEqual(['no_size', 'The design shows this item but prints no size here', ', so it is not compared (drawings are never scaled).']);
    const page = reasonLine({ ...ROW, reference_page: { page: 23 }, proof: { group: 'not_compared', not_compared: 'nothing_to_compare' } }, { designFile: true, page: null })!;
    expect([page.key, page.lead]).toEqual(['no_size', 'Design page 23 shows this item but prints no size here']);
    const missing = reasonLine(ROW, { designFile: true, page: NOT_FOUND, inRequest: false })!;
    expect([missing.tag, missing.lead, missing.rest]).toEqual(['NOT IN DESIGN', 'No drawing of this item was found in the design set.', 'This page is left out of the drawings request to GV.']);
    // A page whose design page is known: the card just checks the vendor's own numbers.
    expect(reasonLine(ROW, { designFile: true, page: { page: 4, status: 'different', pair_page: 12 } })).toBeNull();
  });
});

describe('the vendor’s own numbers on the screen', () => {
  it('adds the numbers up on the right, each close up where code placed it, and boxes them apart on the left', () => {
    const panel = open([{ ...ROW, ...SPOTS }, PAIR], coverage(UNSURE), 's1');
    const [vendor, own] = sides(panel);
    expect(own.dataset.own).toBe('sum');
    expect(own.querySelector('figcaption')?.textContent).toBe('The vendor’s own numbersoff 2″');
    expect(own.textContent).toContain('As printed on Section view B, added by code');
    const numbers = [...own.querySelectorAll<HTMLElement>('[data-slot="chat-evidence-number"]')];
    expect(numbers.map((one) => [one.dataset.tone, one.dataset.picture ?? 'text', one.textContent])).toEqual([
      ['part', 'true', '3″'], ['part', 'true', '50″'], ['part', 'text', '3″'], ['total', 'true', '54″'],
    ]);
    // The close-ups are cut from the vendor's page picture (never the vendor's crop again); turned ones read upright.
    const closeUps = [...own.querySelectorAll<HTMLElement>('[data-slot="chat-evidence-closeup"]')];
    expect(closeUps.every((one) => one.dataset.turned === 'true')).toBe(true);
    expect(own.querySelector('img')?.getAttribute('src')).toBe(pagePicture(4, { file: FILE }));
    expect(own.querySelector('img')?.getAttribute('alt')).toBe('Printed 3″');
    expect(own.querySelector('[data-slot="chat-evidence-sum-parts"]')?.textContent).toBe('3″+50″+3″=56″');
    expect(own.querySelector('[data-slot="chat-evidence-sum-overall"]')?.textContent).toBe('Printed overall54″difference 2″');
    expect(own.textContent).toContain('Code added the numbers printed on the page. Never scaled.');
    // The left card opens the vendor's page onto the numbers, with a legend for the two kinds of box.
    expect(vendor.querySelector('img')?.getAttribute('src')).toBe(pagePicture(4, { file: FILE }));
    expect(vendor.querySelector('[data-slot="chat-evidence-mark-legend"]')?.textContent).toBe('parts addedprinted overall');
    // Pairing unsure on this page: the reason line, not the "not in the upload" chip.
    const reason = panel.querySelector<HTMLElement>('[data-slot="chat-evidence-reason"]')!;
    expect(reason.dataset.reason).toBe('not_confirmed');
    expect(reason.textContent).toBe('NOT CONFIRMEDThe design set is uploaded, but the page for this item wasn’t confirmed (design page 12 or 15?).A person confirms which design page shows it.');
    expect(panel.textContent).not.toContain('not in the upload');
  });

  it('shows the values alone in the same chips when code placed none, and keeps the old crop on the left', () => {
    const panel = open([ROW, PAIR], coverage(UNSURE), 's1');
    const [vendor, own] = sides(panel);
    expect(own.querySelector('img')).toBeNull();
    expect([...own.querySelectorAll<HTMLElement>('[data-slot="chat-evidence-number"]')].map((one) => one.dataset.picture ?? 'text')).toEqual(['text', 'text', 'text', 'text']);
    expect(vendor.querySelector('img')?.getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/vend-s1');
    expect(vendor.querySelector('[data-slot="chat-evidence-mark-legend"]')).toBeNull();
  });

  it('a check of the vendor’s own drawing with no numbers shows its own words, not a second picture', () => {
    const words = finding('c1', { page: 4, item: 'Sample unit A: base run', source: 'vendor_arithmetic', created_by: 'consistency', arch_picture_id: null, parts: null, printed_total: null, why: 'the base run prints 48" on the plan and 47" on the elevation', proof: { group: 'likely_problem' } });
    const panel = open([words], coverage(UNSURE), 'c1');
    const own = sides(panel)[1];
    expect(own.dataset.own).toBe('words');
    expect(own.querySelector('img')).toBeNull();
    expect(own.querySelector('[data-slot="chat-evidence-own-why"]')?.textContent).toBe('The base run prints 48″ on the plan and 47″ on the elevation.');
  });

  it('says "not in the upload" only when the upload has no design file', () => {
    const panel = open([ROW], { pages: [NO_FILE], counts: null }, 's1');
    expect(panel.querySelector<HTMLElement>('[data-slot="chat-evidence-reason"]')?.dataset.reason).toBe('no_design_file');
    expect(panel.querySelector('[data-slot="chat-evidence-reason"]')?.textContent).toBe('NO DESIGN FILENo design drawings in this upload. This page is in the drawings request to GV.Send the architect’s or designer’s drawings of this item.');
    expect([...panel.querySelectorAll('[data-slot="chat-evidence-chips"] li')].map((chip) => chip.textContent)).toContain('Architect drawing: not in the upload · in the GV request');
  });

  it('keeps a card that compares two drawings as it was: two pictures, no sum, no reason line', () => {
    const panel = open([ROW, PAIR], coverage(UNSURE), 'w1');
    const [vendor, architect] = sides(panel);
    expect(architect.dataset.own).toBeUndefined();
    expect(vendor.querySelector('img')?.getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/vend-w1');
    expect(architect.querySelector('img')?.getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/arch-w1');
    expect(architect.querySelector('figcaption')?.textContent).toBe('Should be37″');
    expect(panel.querySelector('[data-slot="chat-evidence-reason"]')).toBeNull();
    expect(panel.querySelector('[data-slot="chat-evidence-sum"]')).toBeNull();
  });
});
