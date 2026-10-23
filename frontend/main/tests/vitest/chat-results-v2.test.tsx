// @vitest-environment jsdom
/**
 * Results v2 (2026-10-23, the owner's approved design, built on top of #1717's screen): six tiles
 * (Wrong · Likely problem · Ask the architect · Drawing needed · Right · Not compared), a plain top
 * line, "Ask GV for N drawings" (a draft to copy, never sent), no "Run the check again" when only
 * architect drawings are missing, a "Biggest first | By page" list, and the wider drawer: two
 * pictures, "How we know", chips; its ‹ › follow the open tile. Made-up values only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatCoverage, ChatFile, ChatFinding, ChatReviewAction } from '@/lib/chat-api';
import { drawerChips, howWeKnow, ringOnCrop, ringOnPage, vendorSide } from '@/lib/chat-drawer';
import { byPage, drawingNeededItems, drawingNeededPages, drawingsRequestText, headline, missingDrawingsOnly, pageCounts, pageHeading } from '@/lib/chat-result';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

const CLOUD: ChatFile = { id: 'f-c', kind: 'cloud_drawing', name: 'vendor-clouded.pdf', size: 2_400_000, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-c' };
const PKG: ChatFile = { id: 'f-z', kind: 'package', name: 'set - GV review package.zip', size: 4_000_000, mime: 'application/zip', url: '/api/v1/chats/c-1/files/f-z' };

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-23T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Done.' }], files: [CLOUD], tasks: null, cost_usd: '4.10', stopped: null, error: null, status: 'done', ...change,
  };
}

const UNPAIRED = 'no drawing of the same item was found in the architect’s file';

/** Six vendor pages: 1-2 compared with the architect, 3-4 with no architect drawing, 5 a title page, 6 not reached. */
const COVERAGE: ChatCoverage = {
  pages: [
    { page: 1, status: 'same' },
    { page: 2, status: 'different' },
    { page: 3, status: 'not_comparable', note: 'Not compared with the architect: …', drawing_needed: true, drawing_needed_why: UNPAIRED },
    { page: 4, status: 'not_comparable', note: 'Not compared with the architect: …', drawing_needed: true, drawing_needed_why: UNPAIRED },
    { page: 5, status: 'not_comparable', note: 'no vendor drawing to compare' },
    { page: 6, status: 'not_opened' },
  ],
  counts: { total: 6, same: 1, different: 1, not_comparable: 3, not_opened: 1 },
};

const FINDINGS: ChatFinding[] = [
  // #1887's shape: code's geometry names the wrong part (the 14″ line is drawn 11 5/8″); the 18″ overall is right.
  finding('w1', { page: 3, item: 'Sample unit A: height string', source: 'vendor_arithmetic', created_by: 'consistency', arch_picture_id: null, parts: ['2 3/8"', '14"', '4"'], parts_sum: '20.375', printed_total: '18"', vendor: '14"', architect: '11 5/8"', wrong_part: { text: '14"', should_be: '11.625', drawn: '11 5/8"', overall: '18"' }, sum_text: '2 3/8" + 14" + 4" = 20 3/8" ≠ 18" printed (off 2 3/8")', proof: { group: 'clouded', should_be: '11 5/8" (the overall less the other parts)', proof: 'Code measured the string on the drawing' }, impact: { score: 80, rank: 1, why: '' } }),
  finding('w2', { page: 1, item: 'Sample unit B: overall width', proof: { group: 'clouded', should_be: '24"' }, impact: { score: 50, rank: 2, why: '' } }),
  finding('l1', { page: 2, item: 'Sample unit C: drawer count', status: 'check', vendor: '3 drawers', architect: '4 drawers', proof: { group: 'likely_problem', likely_problem: { kind: 'count', words: 'Please check and confirm: the vendor draws 3 drawers; the architect shows 4.', reason: 'the architect’s elevation shows four drawer fronts', sure: 'likely', by: 'whole_drawing_look' } } }),
  finding('r1', { page: 1, item: 'Sample unit B: depth', status: 'same', architect: '22"', vendor: '22"', proof: { group: 'right' } }),
  finding('k1', { page: 4, item: 'Sample unit D: toe kick', status: 'check', proof: { group: 'not_compared', not_compared: 'nothing_to_compare', reason: 'architect gives no size for it', settle: 'the architect’s size' } }),
];

const TITLES = new Map([
  [3, { page: 3, title: '3. Sample unit A', project: 'Made-up Hotel', sheet: 'SD-3' }],
  [4, { page: 4, title: 'Sample unit D', project: 'Made-up Hotel', sheet: null }],
]);

const MISSING: ChatReviewAction = {
  action: 'not_for_sending', rule: 'S0', words: 'Check incomplete: not for sending',
  reason: 'The check did not finish: 2 vendor pages have no architect drawing to compare with (p3-4).',
  corrections: 2, fabrication: 1, note: null, next_step: '', required_by: 'GV: finish the check before sending', disclaimer: '',
};

const tiles = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-tiles"] [role="tab"]')].map((tile) => `${tile.dataset.tab}:${tile.querySelector('[data-slot="chat-result-count"]')?.textContent}`);
const rows = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-row"]')].map((row) => row.dataset.id);

describe('results v2, without React', () => {
  it('reads the pages with no architect drawing from the coverage, and counts the pages once', () => {
    expect(drawingNeededPages(COVERAGE)).toEqual([{ page: 3, why: UNPAIRED }, { page: 4, why: UNPAIRED }]);
    expect(pageCounts(COVERAGE)).toEqual({ total: 6, compared: 2, needed: 2, other: 2 });
    expect(pageCounts(null)).toBeNull();
  });

  it('says the top line plainly, each number the tiles’ or the pages’', () => {
    expect(headline({ wrong: 3, likely: 0, pages: { total: 99, compared: 54, needed: 45, other: 0 } }))
      .toBe('3 proven mistakes for the vendor to fix. 54 of 99 pages checked against the architect; 45 pages have no architect drawing in this upload.');
    expect(headline({ wrong: 1, likely: 2, pages: pageCounts(COVERAGE) }))
      .toBe('1 proven mistake for the vendor to fix, and 2 likely problems for the vendor to confirm. 2 of 6 pages checked against the architect; 2 pages have no architect drawing in this upload; 2 pages not compared.');
    expect(headline({ wrong: 0, likely: 0, pages: null })).toBe('No proven mistakes.');
  });

  it('knows when only missing architect drawings keep the check unfinished', () => {
    expect(missingDrawingsOnly(MISSING, 2)).toBe(true);
    expect(missingDrawingsOnly(MISSING, 0)).toBe(false);
    expect(missingDrawingsOnly({ ...MISSING, reason: 'The check did not finish: 2 vendor pages have no architect drawing to compare with (p3-4); 3 vendor pages were not compared (p7-9).' }, 2)).toBe(false);
    expect(missingDrawingsOnly({ ...MISSING, reason: 'The check did not finish: it stopped at its fixed price.' }, 2)).toBe(false);
    expect(missingDrawingsOnly({ ...MISSING, action: 'revise_resubmit' }, 2)).toBe(false);
  });

  it('writes the drawings request as a draft listing the items and their vendor pages', () => {
    const text = drawingsRequestText([3, 4], TITLES);
    expect(text).toContain('- Vendor page 3 · Sample unit A · Made-up Hotel (sheet SD-3)');
    expect(text).toContain('- Vendor page 4 · Sample unit D · Made-up Hotel');
    expect(text).toContain('these 2 items');
    expect(text).not.toMatch(/\bAI\b|helper|Claude|GV Review/);
  });

  it('heads a page with the pack’s title, else its items’ area; Drawing-needed pages become items', () => {
    expect(pageHeading(3, TITLES)).toEqual({ page: 3, title: 'Sample unit A', project: 'Made-up Hotel' });
    expect(pageHeading(1, TITLES, [FINDINGS[1]])).toEqual({ page: 1, title: 'Sample unit B', project: null });
    const items = drawingNeededItems(drawingNeededPages(COVERAGE), TITLES);
    expect(items.map((item) => [item.id, item.page, item.item])).toEqual([
      ['drawing-needed:3', 3, 'Sample unit A: no architect drawing in this upload'],
      ['drawing-needed:4', 4, 'Sample unit D: no architect drawing in this upload'],
    ]);
    expect(byPage(FINDINGS).map((group) => [group.page, group.findings.map((each) => each.id)])).toEqual([[1, ['w2', 'r1']], [2, ['l1']], [3, ['w1']], [4, ['k1']]]);
  });

  it('rings the spot on a crop only when the crop’s shape matches the padded spot', () => {
    const box = { page: 3, x0: 100, y0: 100, x1: 152, y1: 112 };
    // 52 x 12 pt padded by 24 pt: 100 x 60.
    expect(ringOnCrop(box, 1000, 600)).toEqual({ left: 0.24, top: 0.4, width: 0.52, height: 0.2 });
    // Cut at a page edge: another shape, so no guessed ring.
    expect(ringOnCrop(box, 800, 600)).toBeNull();
    const page = ringOnPage({ page: 1, x0: 72, y0: 144, x1: 108, y1: 151.2 }, 1000, 2000)!;
    expect(Object.values(page).map((value) => Math.round(value * 10_000) / 10_000)).toEqual([0.1, 0.1, 0.05, 0.005]);
  });

  it('says how we know in plain steps, never an internal name', () => {
    expect(howWeKnow(FINDINGS[0], 'clouded', { noArchitectDrawing: true })).toEqual([
      'The parts add up to 20 3/8″, but the overall says 18″.',
      'The 18″ line is drawn 18″ long, so the overall is right.',
      'The 14″ line is drawn only 11 5/8″ long, so the 14″ is the mistake.',
    ]);
    // An older string with no part named by the drawing keeps the sum against the overall.
    const older = { ...FINDINGS[0], wrong_part: null, vendor: '18"', architect: '20 3/8"', proof: { group: 'clouded' as const, should_be: '20 3/8" (sum of the vendor’s own parts)' } };
    expect(howWeKnow(older, 'clouded')[0]).toBe('2 3/8″ + 14″ + 4″ = 20 3/8″, but the overall says 18″.');
    const likely = howWeKnow(FINDINGS[2], 'likely_problem');
    expect(likely).toEqual([
      'The vendor draws 3 drawers; the architect shows 4.',
      'The architect’s elevation shows four drawer fronts.',
      'Code could not prove it on the drawings, so it is not a red mark: the vendor is asked to check and confirm.',
    ]);
    const internal = howWeKnow({ ...FINDINGS[1], proof: { group: 'clouded', proof: 'A helper AI found it and the critic passed it' } }, 'clouded');
    expect(internal.join(' ')).not.toMatch(/helper|critic|\bAI\b/);
    expect(vendorSide(FINDINGS[0], 'clouded')).toEqual({ label: 'Vendor shows', value: '14″', tone: 'wrong' });
    expect(drawerChips({ ...FINDINGS[0], also_on: [{ finding_id: 'x', page: 44 }, { finding_id: 'y', page: 3 }] }, { number: 'C-01', noArchitectDrawing: true })).toEqual([
      'Also on page 44',
      'In the comment letter as C-01',
      'Architect drawing: not in the upload · in the GV request',
    ]);
  });
});

describe('results v2 on the screen', () => {
  it('shows six tiles; Likely problems leave Not compared; Drawing needed counts the pages with no architect drawing', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS, coverage: COVERAGE })} chatId="c-1" models={[OPUS]} fullCheck pageTitles={TITLES} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(tiles(node)).toEqual(['wrong:2', 'likely_problem:1', 'rule_note:0', 'drawing_needed:2', 'right:1', 'not_compared:1']);
    expect(node.querySelector('[data-slot="chat-result-headline"]')?.textContent).toBe('2 proven mistakes for the vendor to fix, and 1 likely problem for the vendor to confirm. 2 of 6 pages checked against the architect; 2 pages have no architect drawing in this upload; 2 pages not compared.');
    // Each count says its unit: pages on the card, items or pages on the tiles.
    const hints = [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-tiles"] [role="tab"]')].map((tile) => tile.lastElementChild?.textContent);
    expect(hints[3]).toBe('2 pages not in the upload. Ask GV');
    expect(hints[5]).toBe('1 item, with its reason');
    // The wrong part, not the right overall: "Vendor 14″ → Should be 11 5/8″ · the 18″ overall is right".
    expect(node.querySelector('[data-id="w1"] [data-slot="chat-result-values"]')?.textContent).toBe('Vendor 14″Should be 11 5/8″the 18″ overall is right');
    // The work line never says "checked": the top line does.
    expect(node.querySelector('[data-slot="chat-work-line"]')?.textContent).not.toMatch(/Checked/);

    await user.click(within(node).getByRole('tab', { name: /Likely problem/ }));
    expect(rows(node)).toEqual(['l1']);
    const likely = node.querySelector<HTMLElement>('[data-id="l1"]')!;
    expect(likely.dataset.kind).toBe('likely_problem');
    expect(likely.textContent).toContain('Please check and confirm: the vendor draws 3 drawers; the architect shows 4.');
    // Never solid red: no comment number, a dashed mark.
    expect(likely.querySelector('[data-slot="chat-result-number"]')).toBeNull();
    expect(likely.querySelector('[data-slot="chat-result-likely-mark"]')?.className).toContain('border-dashed');

    await user.click(within(node).getByRole('tab', { name: /Drawing needed/ }));
    expect(rows(node)).toEqual(['drawing-needed:3', 'drawing-needed:4']);
    expect(node.querySelector('[data-id="drawing-needed:3"]')?.textContent).toContain('Page 3 · Sample unit A');
  });

  it('lists by page under "Page N · title · project", with a tag where the upload has no architect drawing', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS, coverage: COVERAGE })} chatId="c-1" models={[OPUS]} fullCheck pageTitles={TITLES} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    // Biggest first (the default): the string on page 3 leads.
    expect(rows(node)).toEqual(['w1', 'w2']);
    await user.click(within(node).getByRole('button', { name: 'By page' }));
    expect(rows(node)).toEqual(['w2', 'w1']);
    const heads = [...node.querySelectorAll('[data-slot="chat-result-page-head"]')].map((head) => head.textContent);
    expect(heads).toEqual(['Page 1 · Sample unit B1', 'Page 3 · Sample unit A · Made-up Hotelno architect drawing1']);
  });

  it('asks GV for the missing drawings with a draft to copy, and never offers "Run the check again" for them', async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    render(<AssistantMessage message={answer({ findings: FINDINGS, coverage: COVERAGE, files: [CLOUD, PKG], review_action: MISSING })} chatId="c-1" models={[OPUS]} fullCheck pageTitles={TITLES} onRetry={retry} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    const head = within(node).getByRole('region', { name: 'Review result' });
    expect(within(head).queryByRole('button', { name: 'Run the check again' })).toBeNull();
    expect(head.querySelector('[data-slot="chat-review-sentence"]')?.textContent).toBe('This package is a draft until the architect’s drawings are added: ask GV for them, then run the check again.');
    const ask = within(head).getByRole('button', { name: 'Ask GV for 2 drawings' });
    // An action, not an outcome: the neutral dark button, never red or maroon.
    expect(ask.className).toContain('bg-foreground');
    expect(ask.className).not.toMatch(/bg-primary|outcome-fail/);
    await user.click(ask);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('A draft for you to edit and send yourself. Nothing is sent from here.')).toBeTruthy();
    const text = (within(dialog).getByRole('textbox', { name: 'The request' }) as HTMLTextAreaElement).value;
    expect(text).toContain('- Vendor page 3 · Sample unit A · Made-up Hotel (sheet SD-3)');
    expect(within(dialog).queryByRole('button', { name: /^Send/ })).toBeNull();
    expect(within(dialog).getByRole('link', { name: /Download \.txt/ }).getAttribute('href')).toMatch(/^data:text\/plain/);
  });

  it('keeps "Run the check again" when pages were not reached as well', () => {
    const both = { ...MISSING, reason: 'The check did not finish: 2 vendor pages have no architect drawing to compare with (p3-4); 1 vendor page was not compared (p6).' };
    render(<AssistantMessage message={answer({ findings: FINDINGS, coverage: COVERAGE, files: [CLOUD, PKG], review_action: both })} chatId="c-1" models={[OPUS]} fullCheck onRetry={vi.fn()} />);
    const head = within(screen.getByRole('article', { name: 'Answer' })).getByRole('region', { name: 'Review result' });
    expect(within(head).getByRole('button', { name: 'Run the check again' })).toBeTruthy();
    expect(within(head).getByRole('button', { name: 'Ask GV for 2 drawings' })).toBeTruthy();
  });

  it('opens the wider drawer: two sides, how we know, chips; ‹ › step through the open tile only', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS, coverage: COVERAGE, package_state: { state: {}, numbers: { w1: 'C-01', w2: 'C-02' } } })} chatId="c-1" models={[OPUS]} fullCheck pageTitles={TITLES} pagePicture={(page) => `/api/v1/packages/p/plan-pages/${page}`} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('button', { name: /^C-01: Height string, page 3\. Open the evidence$/ }));
    const panel = await screen.findByRole('dialog');
    expect(panel.dataset.results).toBe('true');
    expect(panel.querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('1 of 2 items');
    const sides = [...panel.querySelectorAll<HTMLElement>('[data-slot="chat-evidence-side"]')];
    expect(sides.map((side) => side.querySelector('figcaption')?.textContent)).toEqual(['Vendor shows14″', 'Should be11 5/8″']);
    // The vendor's own string: the should-be is measured on the vendor's own drawing, never "No evidence picture".
    expect(sides[1].textContent).toContain('Measured on the vendor’s own drawing: this line is drawn 11 5/8″ long');
    expect(sides[1].querySelector('img')?.getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/vend-w1');
    expect(panel.textContent).not.toContain('No evidence picture');
    const how = within(panel).getByRole('region', { name: 'How we know' });
    expect(within(how).getAllByRole('listitem').map((step) => step.textContent)).toEqual([
      'The parts add up to 20 3/8″, but the overall says 18″.',
      'The 18″ line is drawn 18″ long, so the overall is right.',
      'The 14″ line is drawn only 11 5/8″ long, so the 14″ is the mistake.',
    ]);
    expect(how.querySelector('[data-slot="chat-evidence-tell"]')?.textContent).toBe('Change the 14″ part to 11 5/8″: the 18″ overall is right.');
    expect([...panel.querySelectorAll('[data-slot="chat-evidence-chips"] li')].map((chip) => chip.textContent)).toEqual(['In the comment letter as C-01', 'Architect drawing: not in the upload · in the GV request']);
    await user.keyboard('j');
    expect(panel.querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('2 of 2 items');
    expect(within(panel).getByRole('heading', { level: 2 }).textContent).toMatch(/^Overall width/);
  });

  it('a Drawing-needed page opens the same drawer; it can be left out of the request and put back', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS, coverage: COVERAGE, files: [CLOUD, PKG], review_action: MISSING })} chatId="c-1" models={[OPUS]} fullCheck pageTitles={TITLES} pagePicture={(page) => `/api/v1/packages/p/plan-pages/${page}`} decide={vi.fn()} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('tab', { name: /Drawing needed/ }));
    await user.click(within(node).getByRole('button', { name: /^Page 3, Sample unit A: no architect drawing in this upload\. Open it$/ }));
    const panel = await screen.findByRole('dialog');
    expect(panel.querySelector('[data-slot="chat-evidence-kind"]')?.textContent).toBe('Drawing needed');
    expect(panel.textContent).toContain('No architect drawing of this item in the upload.');
    // Nothing is decided on a page: no "Your call".
    expect(within(panel).queryByRole('region', { name: 'Your call' })).toBeNull();
    expect(panel.querySelector('[data-slot="chat-evidence-request"]')?.textContent).toContain('In the drawings request to GV');
    await user.click(within(panel).getByRole('button', { name: 'Leave it out' }));
    await user.click(within(panel).getByRole('button', { name: 'Add to the drawings request' }));
    await user.click(within(panel).getByRole('button', { name: 'Leave it out' }));
    await user.keyboard('{Escape}');
    expect(within(within(node).getByRole('region', { name: 'Review result' })).getByRole('button', { name: 'Ask GV for 1 drawing' })).toBeTruthy();
  });
});
