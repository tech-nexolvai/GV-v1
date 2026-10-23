// @vitest-environment jsdom
/**
 * Chat v4/v5 (#1483): the product chips (default from the set, remembered per set, sent as
 * `products`), the one-click full check (confirm popover, `mode: "full_check"`, the user bubble),
 * a full-check answer's order (notes → trust → cloud drawing first → What's wrong / To check /
 * What's right → coverage), the accuracy badge (colours, claims table, n/a) and evidence chips for
 * stated numbers. Synthetic data only.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import { AssistantMessage, UserMessage } from '@/components/chat/chat-message';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatFile, RemadeFiles } from '@/lib/chat-api';
import { accuracyTone, accuracyWords } from '@/lib/chat-accuracy';
import { allPicked, checkedWords, confirmWords, effectiveProducts, fullCheckBubble, fullCheckFiles, isFullCheck, lastProducts, matchesWords, productsWords, progressWords, scopeSentence, toggleProduct } from '@/lib/chat-full-check';
import { applyChatEvent, localAnswer, type ViewMessage } from '@/lib/chat-messages';
import { ChatPage } from '@/pages/ChatPage';
import { drivenStream, fakeChatApi, finding, OPUS } from './fixtures/chat';

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    // no storage in this environment
  }
});

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Two values are wrong; one to check.' }], files: [], tasks: null, cost_usd: '4.10', stopped: null, error: null, status: 'done', ...change,
  };
}

const CLOUD: ChatFile = { id: 'f-c', kind: 'cloud_drawing', name: 'vendor-clouded.pdf', size: 2_400_000, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-c' };
const REPORT: ChatFile = { id: 'f-r', kind: 'check_report', name: 'check-report.pdf', size: 90_000, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-r' };

describe('products and the full check, without React', () => {
  it('picks products: the reviewer’s, else what the chat last asked with, else all; never none', () => {
    // All by default (2026-10-18): a set's stated product is not a scope.
    expect(effectiveProducts(null, null)).toEqual(['countertop', 'cabinet']);
    expect(effectiveProducts(null, ['cabinet'])).toEqual(['cabinet']);
    expect(effectiveProducts(['countertop'], ['cabinet'])).toEqual(['countertop']);
    expect(effectiveProducts(null, ['lighting', 'cabinet'])).toEqual(['cabinet']); // not pickable now
    expect(effectiveProducts(null, null, [])).toBeNull(); // the products not loaded yet
    expect(lastProducts([{ products: ['countertop'] }, { products: null }])).toEqual(['countertop']);
    expect(lastProducts([{ products: null }])).toBeNull();
    expect(allPicked(['cabinet', 'countertop'])).toBe(true);
    expect(allPicked(['cabinet'])).toBe(false);
    expect(checkedWords(['countertop'])).toBe('Checked: Countertops');
    expect(checkedWords(['countertop', 'cabinet'])).toBe('Checked: All products');
    expect(scopeSentence(['countertop'])).toBe('Only countertops are checked; cabinets are left out.');
    expect(scopeSentence(['cabinet', 'countertop'])).toBe('Checks all products (countertops, cabinets).');
    expect(toggleProduct(['countertop'], 'cabinet')).toEqual(['countertop', 'cabinet']);
    expect(toggleProduct(['countertop', 'cabinet'], 'countertop')).toEqual(['cabinet']);
    expect(toggleProduct(['countertop'], 'countertop')).toEqual(['countertop']);
    expect(toggleProduct(null, 'cabinet')).toEqual(['cabinet']);
  });

  it('says the full check plainly', () => {
    expect(productsWords(['cabinet', 'countertop'])).toBe('Countertops + Cabinets');
    expect(fullCheckBubble(['countertop'], 'High')).toBe('Full check · Countertops · High effort');
    expect(confirmWords(['countertop'], 'Opus 5.5', 'High', '$15')).toBe('Full check · Countertops · Opus 5.5 · High · up to $15');
    expect(confirmWords(null, 'Opus 5.5', 'High', null)).toBe('Full check · this set’s product · Opus 5.5 · High');
    expect(matchesWords('architect')).toBe('matches architect');
    expect(matchesWords('brand_prototype')).toBe('matches brand prototype');
  });

  it('finds the cloud drawing and the check report by kind, else by name; the newest of each', () => {
    expect(fullCheckFiles([REPORT, CLOUD])).toEqual({ cloud: CLOUD, report: REPORT, notCompared: [], pkg: null, rest: [] });
    // #1619: the Not-compared list, as a PDF and a workbook, in the card (not as plain file cards).
    const listPdf: ChatFile = { id: 'f-n', name: 'Set - Not compared.pdf', size: 10, mime: 'application/pdf', url: '/f-n' };
    const listXlsx: ChatFile = { id: 'f-x', name: 'Set - Not compared.xlsx', size: 10, mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', url: '/f-x' };
    expect(fullCheckFiles([CLOUD, REPORT, listPdf, listXlsx])).toEqual({ cloud: CLOUD, report: REPORT, notCompared: [listPdf, listXlsx], pkg: null, rest: [] });
    const cloud2 = { ...CLOUD, id: 'f-c2' };
    const report2 = { ...REPORT, id: 'f-r2' };
    expect(fullCheckFiles([CLOUD, REPORT, cloud2, report2])).toEqual({ cloud: cloud2, report: report2, notCompared: [], pkg: null, rest: [] });
    const old = { id: 'x', name: 'set-cloud-markup.pdf', size: 1, mime: 'application/pdf', url: '' };
    const csv = { id: 'y', name: 'items.csv', size: 1, mime: 'text/csv', url: '' };
    expect(fullCheckFiles([csv, old])).toEqual({ cloud: old, report: null, notCompared: [], pkg: null, rest: [csv] });
  });

  it('reads cites and the accuracy from the stream', () => {
    let message = localAnswer('k', { model: 'claude-opus-5-5', effort: 'high', thinking: true });
    message = applyChatEvent(message, { event: 'cite', data: { cite: { id: 'c1', page: 23, value: '25 1/2"' } } });
    message = applyChatEvent(message, { event: 'cite', data: { cite: { id: 'c1', page: 23, value: '25 1/2"', printed: 'match' } } });
    message = applyChatEvent(message, { event: 'accuracy', data: { accuracy: { score: 92 } } });
    expect(message.cites).toEqual([{ id: 'c1', page: 23, value: '25 1/2"', printed: 'match' }]);
    expect(message.accuracy?.score).toBe(92);
  });

  it('colours the accuracy the owner’s way', () => {
    expect([100, 80, 79, 60, 59, 0].map(accuracyTone)).toEqual(['green', 'green', 'orange', 'orange', 'yellow', 'yellow']);
    expect(accuracyTone(null)).toBe('none');
    expect(accuracyWords({ score: 91.6 })).toBe('Accuracy 92%');
    expect(accuracyWords({ score: null })).toBe('Accuracy: n/a (nothing to check)');
  });
});

describe('a full-check answer', () => {
  const findings = [
    // #1619: the server's groups: two Wrongs code proved, one with nothing to compare, two right.
    finding('w1', { item: 'Sink base width', status: 'different', inches: { architect: '37', vendor: '46', difference: '9' }, architect: '3\'-1"', vendor: '46"', proof: { group: 'clouded' } }),
    finding('w2', { item: 'Filler', category: 'cabinet', status: 'different', proof: { group: 'clouded' } }),
    finding('k1', { item: 'Island overhang', status: 'check', proof: { group: 'not_compared', rule: 'R7', not_compared: 'nothing_to_compare', label: 'Not reviewed: not shown on the architect’s drawings', reason: 'architect gives no size or information for it', settle: 'the architect’s size or note for the island overhang' } }),
    finding('r1', { item: 'Vanity top depth', status: 'same', architect: '22"', vendor: '22"', proof: { group: 'right' } }),
    finding('r2', { item: 'Sink model', category: 'other', status: 'same', source: 'brand_prototype', architect: 'K-1', vendor: 'K-1', proof: { group: 'right' } }),
  ];

  const rows = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-row"]')].map((row) => row.dataset.id);
  const tiles = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-tiles"] [role="tab"]')].map((tile) => `${tile.dataset.tab}:${tile.textContent}`);

  it('reads summary first: the work line, the review result and files, then three tiles that are the tabs, then How this was checked (#1717)', async () => {
    const user = userEvent.setup();
    render(
      <AssistantMessage
        message={answer({ findings, files: [REPORT, CLOUD], coverage: { pages: [{ page: 1, status: 'same' }, { page: 3, status: 'different' }] }, trust: { levels: { confirmed: 3, likely: 1, to_check: 1 } }, accuracy: { score: 84, claims: 5, breakdown: [] }, products: ['countertop', 'cabinet'] })}
        chatId="c-1"
        models={[OPUS]}
        fullCheck
      />,
    );
    const node = screen.getByRole('article', { name: 'Answer' });
    const order = ['chat-work-line', 'chat-result-head', 'chat-result-tiles', 'chat-result-list', 'chat-trust'].map((slot) => node.querySelector(`[data-slot="${slot}"]`));
    order.forEach((part) => expect(part).toBeTruthy());
    for (let at = 1; at < order.length; at += 1) {
      expect(order[at - 1]!.compareDocumentPosition(order[at]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    // One place per number: the tiles hold the counts; no headline, stat cards or counted section heads.
    // Results v2 (2026-10-23): six tiles, always in the same place; glyph + word + count + hint.
    expect(tiles(node)).toEqual([
      'wrong:Wrong2Proven. Red on the cloud drawing',
      'likely_problem:!Likely problem0Found, not proven. Vendor asked to confirm',
      'rule_note:?Ask the architect0The architect’s own size needs a yes',
      'drawing_needed:Drawing needed0Not in the upload. Ask GV',
      'right:Right2Match the architect or GV’s rules',
      'not_compared:Not compared1Each with its reason',
    ]);
    expect(node.querySelector('[data-slot="chat-summary-headline"], [data-slot="chat-kpis"], [data-slot="chat-cloud-drawing"], [data-slot="chat-package"]')).toBeNull();
    // The accuracy once, in the footer.
    expect(node.querySelectorAll('[data-slot="chat-accuracy"]')).toHaveLength(1);
    // The answer's own words wait under "Summary as text".
    expect(within(node).queryByText('Two values are wrong; one to check.')).toBeNull();
    await user.click(within(node).getByRole('button', { name: 'Summary as text' }));
    expect(within(node).getByText('Two values are wrong; one to check.')).toBeTruthy();
    // The page coverage lives only in "How this was checked" (folded).
    expect(node.querySelectorAll('[data-slot="chat-coverage"]')).toHaveLength(0);
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    expect(node.querySelector('[data-slot="chat-trust"] [data-slot="chat-coverage"]')).toBeTruthy();
    expect(node.textContent).not.toMatch(/To check|Ready to send|Nothing to compare/);

    // The head: the cloud drawing (open shows the PDF, #1684) and its download as the main action; the files in one line.
    const head = within(node).getByRole('region', { name: 'Review result' });
    expect(within(head).getByRole('link', { name: 'Open the cloud drawing' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-c?inline=true');
    const download = within(head).getByRole('link', { name: 'Download vendor-clouded.pdf' });
    expect(download.getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-c');
    expect(download.className).toContain('bg-primary');
    const filesLine = within(head).getByRole('button', { name: /^2 files: cloud drawing, check report$/ });
    await user.click(filesLine);
    expect(within(head).getByRole('link', { name: 'Open check-report.pdf' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-r?inline=true');
    expect(within(head).getByRole('link', { name: 'Download check-report.pdf' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-r');
    // No second copy of the outputs: no marked-up PDF button, no CSV button, no plain file cards.
    expect(within(node).queryByRole('button', { name: /Download marked-up PDF|Download Excel/ })).toBeNull();
    expect(node.querySelector('[data-slot="chat-files"]')).toBeNull();

    // The tabs: Wrong open first; each row says what was measured and the two values, with no decision buttons on it.
    expect(rows(node)).toEqual(['w1', 'w2']);
    const first = node.querySelector<HTMLElement>('[data-id="w1"]')!;
    expect(first.querySelector('[data-slot="chat-result-values"]')?.textContent).toBe('Vendor 46″Should be 3′-1″9″ over');
    expect(within(node).queryByRole('button', { name: /^(?:Agree|Not a problem)$/ })).toBeNull();
    await user.click(within(node).getByRole('tab', { name: /^Right/ }));
    expect(rows(node)).toEqual(['r1', 'r2']);
    await user.click(within(node).getByRole('tab', { name: /^Not compared/ }));
    expect(rows(node)).toEqual(['k1']);
    expect(node.querySelector('[data-slot="chat-result-reason"]')?.textContent).toContain('Architect gives no size or information');
    expect(node.querySelector('[data-id="k1"]')?.textContent).toContain('Needs the architect’s size or note for the island overhang');
    expect(node.textContent).not.toMatch(/Your call|your decision|vendor to confirm|check before sending/i);
  });

  it('never labels the vendor’s own string or span “Architect”: its evidence is code’s sum (#1581)', () => {
    const own = [
      finding('s1', { item: 'Run height string', status: 'different', source: 'vendor_arithmetic', parts: ['40"', '40 1/8"'], parts_sum: '80.125', printed_total: '80"', vendor: '80"', proof: { group: 'clouded' } }),
      finding('s2', { item: 'Base: same span printed twice', status: 'different', source: 'cross_view', reference_side: 'vendor', created_by: 'consistency', proof: { group: 'clouded' } }),
    ];
    render(<AssistantMessage message={answer({ findings: own, files: [CLOUD] })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(rows(node)).toEqual(['s1', 's2']);
    const evidence = node.querySelector<HTMLElement>('[data-id="s1"] [data-slot="chat-result-evidence"]')!;
    expect(evidence.textContent).toContain('40″ + 40 1/8″= 80 1/8″Added up by code');
    expect(evidence.textContent).not.toMatch(/Architect/);
    for (const row of node.querySelectorAll('[data-slot="chat-result-row"]')) expect(row.textContent).not.toMatch(/Architect/);
  });

  it('leads with the download when there is no review result yet, and no decision on any row (#1581, owner decision 3)', () => {
    const proved = [
      finding('w1', { item: 'Sink base width', status: 'different', proof: { group: 'clouded', should_be: '37" (architect)', proof: 'Printed at both spots, read by code' } }),
      finding('w2', { item: 'Filler', status: 'different', proof: { group: 'not_compared', not_compared: 'unreadable', label: 'Illegible', reason: 'could not read the vendor’s value on page 2' } }),
      finding('k1', { item: 'Island overhang', status: 'check', proof: { group: 'not_compared', not_compared: 'nothing_to_compare', label: 'Not reviewed: not shown on the architect’s drawings', reason: 'architect gives no size or information for it' } }),
      finding('r1', { item: 'Vanity top depth', status: 'same', proof: { group: 'right' } }),
    ];
    render(<AssistantMessage message={answer({ findings: proved, files: [CLOUD, REPORT] })} chatId="c-1" models={[OPUS]} fullCheck decide={vi.fn()} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(tiles(node).map((tile) => tile.replace(/[A-Z][^0-9]*$/, ''))).toEqual(['wrong:Wrong1', 'likely_problem:!Likely problem0', 'rule_note:?Ask the architect0', 'drawing_needed:Drawing needed0', 'right:Right1', 'not_compared:Not compared2']);
    const head = within(node).getByRole('region', { name: 'Review result' });
    expect(head.textContent).not.toMatch(/draft|signed review/i);
    expect(head.querySelector('a')?.getAttribute('aria-label')).toBe('Open the cloud drawing');
    expect(head.querySelector('[data-slot="chat-cloud-download"]')?.className).toContain('bg-primary');
    expect(head.querySelector('[data-slot="chat-cloud-update"]')).toBeNull();
    expect(within(node).queryByRole('button', { name: /^(?:Agree|Not a problem|Make it Wrong)$/ })).toBeNull();
  });

  it('an unfinished check: a dashed draft stamp, never "send"; Run the check again leads (#1684, #1717)', () => {
    const action = { action: 'not_for_sending' as const, rule: 'S0' as const, words: 'Check incomplete: not for sending', reason: 'The check did not finish: 39 of 39 drawings could not be compared.', corrections: 1, fabrication: 1, note: null, next_step: '', required_by: 'GV: finish the check before sending', disclaimer: '' };
    const PKG: ChatFile = { id: 'f-z', kind: 'package', name: 'set - GV review package.zip', size: 4_000_000, mime: 'application/zip', url: '/api/v1/chats/c-1/files/f-z' };
    const retry = vi.fn();
    render(<AssistantMessage message={answer({ findings, files: [CLOUD, REPORT, PKG], review_action: action })} chatId="c-1" models={[OPUS]} fullCheck onRetry={retry} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    const head = within(node).getByRole('region', { name: 'Review result' });
    const stamp = head.querySelector<HTMLElement>('[data-slot="chat-review-action"]')!;
    expect(stamp.textContent).toBe('Check incomplete · not for sending');
    expect(stamp.className).toContain('border-dashed');
    expect(head.querySelector('[data-slot="chat-review-sentence"]')?.textContent).toBe('The check did not finish: 39 of 39 drawings could not be compared. This package is a draft: do not send it yet.');
    expect(head.textContent).not.toMatch(/Ready to send|Send the package/);
    const actions = [...head.querySelectorAll<HTMLElement>('[data-slot="chat-result-run-again"], [data-slot="chat-package-download"]')].map((el) => el.textContent);
    expect(actions).toEqual(['Run the check again', 'Download draft']);
    within(head).getByRole('button', { name: 'Run the check again' }).click();
    expect(retry).toHaveBeenCalled();
  });

  it('a finished check: AWI’s words on a solid stamp, and the package download leads (#1717)', () => {
    const action = { action: 'revise_resubmit' as const, rule: 'S3' as const, words: 'Revise and resubmit', reason: '1 of 2 corrections change fabrication.', corrections: 2, fabrication: 1, note: null, next_step: 'Correct the drawings and send them again.', required_by: 'Vendor: revise and resubmit', disclaimer: '' };
    const PKG: ChatFile = { id: 'f-z', kind: 'package', name: 'set - GV review package.zip', size: 4_000_000, mime: 'application/zip', url: '/api/v1/chats/c-1/files/f-z' };
    render(<AssistantMessage message={answer({ findings, files: [CLOUD, REPORT, PKG], review_action: action, package_state: { state: {}, numbers: { w1: 'C-04' } } })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    const head = within(node).getByRole('region', { name: 'Review result' });
    expect(head.querySelector('[data-slot="chat-review-action"]')?.textContent).toBe('Revise and resubmit');
    expect(head.querySelector('[data-slot="chat-review-sentence"]')?.textContent).toBe('1 of the wrong values changes fabrication. Correct the drawings and send them again.');
    const download = head.querySelector<HTMLElement>('[data-slot="chat-package-download"]')!;
    expect(download.getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-z');
    expect(download.className).toContain('bg-primary');
    // The comment number the letter gives it, on its row.
    expect(node.querySelector('[data-id="w1"] [data-slot="chat-result-number"]')?.textContent).toBe('C-04');
  });

  it('counts each group once, on its tile: a "same" not looked at on both sides is Not compared (#1684)', () => {
    const mixed = [...findings, finding('r3', { item: 'Toe kick height', status: 'same', compared: false, proof: { group: 'not_compared', not_compared: 'not_seen', label: 'Not reviewed: not looked at on both drawings', reason: 'only the vendor’s side was read' } })];
    render(<AssistantMessage message={answer({ findings: mixed, files: [CLOUD] })} chatId="c-1" models={[OPUS]} fullCheck decide={vi.fn()} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(tiles(node).map((tile) => tile.replace(/[A-Z][^0-9]*$/, ''))).toEqual(['wrong:Wrong2', 'likely_problem:!Likely problem0', 'rule_note:?Ask the architect0', 'drawing_needed:Drawing needed0', 'right:Right2', 'not_compared:Not compared2']);
  });

  it('is still a full check after a reload, when the saved question has no mode (#1508)', () => {
    expect(isFullCheck({ mode: null }, { mode: null, text: 'Full check · Countertops and Cabinets · High effort' })).toBe(true);
    expect(isFullCheck({ mode: null }, { mode: null, text: 'Full check' })).toBe(true);
    expect(isFullCheck({ mode: null }, { mode: null, text: 'Full check the sink, please' })).toBe(false);
    expect(isFullCheck({ mode: null }, { mode: 'chat', text: 'Full check' })).toBe(false);
    expect(isFullCheck({ mode: null }, null)).toBe(false);
    // Its "AI markup" PDF, saved before files had kinds, is its cloud drawing.
    const markup: ChatFile = { id: 'f-m', name: 'Set - AI markup.pdf', size: 10, mime: 'application/pdf', url: '/f-m' };
    expect(fullCheckFiles([markup]).cloud).toBe(markup);
  });

  it('opens on Not compared when nothing is wrong, and says so on the Wrong tab', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: findings.filter((item) => item.status !== 'different') })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(within(node).getByRole('tab', { name: /^Not compared/ }).getAttribute('aria-selected')).toBe('true');
    await user.click(within(node).getByRole('tab', { name: /^Wrong/ }));
    expect(node.querySelector('[data-slot="chat-result-list"]')?.textContent).toBe('Nothing proved wrong.');
  });

  it('says a stop once, on the work line, and keeps a summary GV Review wrote under "Summary as text"', async () => {
    const user = userEvent.setup();
    const blocks: ViewMessage['blocks'] = [{ type: 'text', text: 'Checked 30 of 60 pages.\n\n*Summary written by GV Review: the AI did not finish its own.*', by: 'code' }];
    render(<AssistantMessage message={answer({ findings, blocks, stopped: 'cancelled' })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(node.querySelector('[data-slot="chat-work-line"]')?.textContent).toMatch(/stopped before the end/);
    expect(node.textContent?.match(/stopped before the end/gi)).toHaveLength(1);
    await user.click(within(node).getByRole('button', { name: 'Summary as text' }));
    expect(within(node).getByText('Checked 30 of 60 pages.')).toBeTruthy();
    expect(node.textContent).not.toMatch(/Summary written by GV Review/);
  });

  it('shows one live progress line while it runs (PATH step 3)', () => {
    const progress = { phase: 'checking' as const, pages_done: 11, pages_total: 60, wrong_so_far: 8, decision_so_far: 3, right_so_far: 4, eta_seconds: 330 };
    render(<AssistantMessage message={answer({ status: 'running', findings, progress })} chatId="c-1" models={[OPUS]} fullCheck />);
    expect(screen.getByRole('status').textContent).toBe('Checking page 12 of 60 · 8 wrong so far · about 6 min left');
    expect(document.querySelector('[data-slot="chat-kpis"], [data-slot="chat-findings"]')).toBeNull();
    // From the stream, too.
    const live = applyChatEvent(localAnswer('k', { model: OPUS.id, effort: 'high', thinking: true }), { event: 'progress', data: { progress } });
    expect(live.progress).toEqual(progress);
    expect(progressWords({ created_at: '2026-10-17T10:00:00Z', findings: [], coverage: null, progress: { ...progress, phase: 'reading', pages_done: 0, eta_seconds: null } })).toBe('Reading the drawings…');
    expect(progressWords({ created_at: '2026-10-17T10:00:00Z', findings: [], coverage: null, progress: { ...progress, eta_seconds: 20 } })).toBe('Checking page 12 of 60 · 8 wrong so far · under a minute left');
  });

  it('#1874: names the roster step, and on a big set the areas done of all', () => {
    const progress = { phase: 'checking' as const, pages_done: 40, pages_total: 188, wrong_so_far: 3, decision_so_far: 0, right_so_far: 1, eta_seconds: 840 };
    const at = (more: object) => progressWords({ created_at: '2026-10-17T10:00:00Z', findings: [], coverage: null, progress: { ...progress, ...more } });
    expect(at({ step: 'areas', areas_done: 2, areas_total: 5 })).toBe('Comparing the areas · 2 of 5 done · 3 wrong so far · about 14 min left');
    expect(at({ step: 'lead_review', areas_total: null })).toBe('Lead review · 3 wrong so far · about 14 min left');
    expect(at({ step: 'advocate', eta_seconds: null })).toBe('Second look at the red marks · 3 wrong so far');
    expect(at({ step: null })).toBe('Checking page 41 of 188 · 3 wrong so far · about 14 min left');
  });

  it('says how far the drawings are read while the check waits for them (#1872)', () => {
    const progress = { phase: 'reading' as const, pages_done: 0, pages_total: 0, wrong_so_far: 0, decision_so_far: 0, right_so_far: 0, eta_seconds: null, reading_done: 40, reading_total: 188 };
    expect(progressWords({ created_at: '2026-10-22T10:00:00Z', findings: [], coverage: null, progress })).toBe('Reading the drawings: 40 of 188 pages');
  });

  it('counts drawings in the simple full check (#1665)', () => {
    const progress = { phase: 'checking' as const, pages_done: 3, pages_total: 60, wrong_so_far: 2, decision_so_far: 0, right_so_far: 1, eta_seconds: 400, drawings_done: 11, drawings_total: 36 };
    expect(progressWords({ created_at: '2026-10-17T10:00:00Z', findings: [], coverage: null, progress })).toBe('Comparing drawing 12 of 36 · 2 wrong so far · about 7 min left');
    expect(progressWords({ created_at: '2026-10-17T10:00:00Z', findings: [], coverage: null, progress: { ...progress, phase: 'reading', pages_done: 0, drawings_done: 0, eta_seconds: null } })).toBe('Comparing drawing 1 of 36 · 2 wrong so far');
  });

  it('offers clear overrides in the evidence panel and makes the draft again by itself (#1803)', async () => {
    const user = userEvent.setup();
    const decide = vi.fn(async (_message: string, _finding: string, decision: string, extra?: { note?: string | null; should_be?: string | null }) => ({ decision, by: 'anant', at: '2026-10-17T11:00:00Z', ...(extra?.should_be ? { should_be: extra.should_be } : {}), note: extra?.note || null }));
    const NEW_CLOUD: ChatFile = { ...CLOUD, id: 'f-c2', name: 'vendor-clouded-reviewed.pdf', url: '/api/v1/chats/c-1/files/f-c2' };
    const remake = vi.fn(async () => NEW_CLOUD);
    const reload = vi.fn();
    render(<AssistantMessage message={answer({ findings, files: [CLOUD, REPORT] })} chatId="c-1" models={[OPUS]} fullCheck decide={decide} remakeCloud={remake} onReload={reload} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(node.querySelector('[data-slot="chat-cloud-update"]')).toBeNull();
    // A Wrong item: "Not a mistake: remove the red mark", with an optional reason; it becomes Right (reviewer).
    await user.click(within(node).getByRole('button', { name: /^Filler, page \d+\. Open the evidence$/ }));
    const panel = await screen.findByRole('dialog');
    expect(within(panel).queryByRole('button', { name: /It’s a mistake/ })).toBeNull();
    expect(within(panel).getByText(/The red cloud and its comment come off/)).toBeTruthy();
    await user.click(within(panel).getByRole('button', { name: 'Not a mistake: remove the red mark' }));
    await user.type(within(panel).getByRole('textbox', { name: /Reason/ }), 'Field fit');
    await user.click(within(panel).getByRole('button', { name: 'Remove the red mark' }));
    expect(decide).toHaveBeenCalledWith('msg-1', 'w2', 'not_a_problem', expect.objectContaining({ note: 'Field fit' }));
    await waitFor(() => expect(tiles(node)[0]).toMatch(/^wrong:Wrong1/));
    expect(tiles(node)[4]).toMatch(/^right:Right3/);
    expect(within(screen.getByRole('dialog')).getByText('Right (reviewer).')).toBeTruthy();
    // The draft is made again by itself, a moment later.
    await waitFor(() => expect(remake).toHaveBeenCalledWith('msg-1'), { timeout: 3000 });
    await waitFor(() => expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-draft"]')?.getAttribute('data-state')).toBe('updated'));
    expect(reload).toHaveBeenCalled();
    await user.keyboard('{Escape}');
    await user.click(within(node).getByRole('tab', { name: /^Right/ }));
    expect(node.querySelector('[data-slot="chat-result-list"]')?.textContent).toContain('Right (your call)');
    expect(node.querySelector('[data-slot="chat-package-line"]')?.textContent).toMatch(/^Package updated with your 1 decision ·/);

    // A Not-compared item: "It's a mistake: add a red mark" asks what it should be, filled in with the architect's value.
    await user.click(within(node).getByRole('tab', { name: /^Not compared/ }));
    await user.click(within(node).getByRole('button', { name: /^Island overhang, page \d+\. Open the evidence$/ }));
    const second = await screen.findByRole('dialog');
    expect(within(second).queryByRole('button', { name: /remove the red mark/ })).toBeNull();
    expect(within(second).getByRole('button', { name: 'It’s fine: mark Right' })).toBeTruthy();
    await user.click(within(second).getByRole('button', { name: 'It’s a mistake: add a red mark' }));
    const value = within(second).getByRole('textbox', { name: 'Should be' });
    expect((value as HTMLInputElement).value).toBe('24"');
    await user.clear(value);
    expect(within(second).getByRole('button', { name: 'Add the red mark' }).hasAttribute('disabled')).toBe(true);
    await user.type(value, '12"');
    await user.click(within(second).getByRole('button', { name: 'Add the red mark' }));
    expect(decide).toHaveBeenCalledWith('msg-1', 'k1', 'agree', expect.objectContaining({ should_be: '12"' }));
    await waitFor(() => expect(tiles(node)[0]).toMatch(/^wrong:Wrong2/));
    expect([...screen.getByRole('dialog').querySelectorAll('[data-slot="chat-evidence-side"] figcaption')].map((caption) => caption.textContent)).toContain('Should be12″');
    expect(within(screen.getByRole('dialog')).getByText('Wrong (reviewer).')).toBeTruthy();
    await waitFor(() => expect(remake).toHaveBeenCalledTimes(2), { timeout: 3000 });

    // Undo: the call is taken back; the item is Not compared again and the package is made again.
    expect(within(screen.getByRole('dialog')).getByText(/Takes your call back/)).toBeTruthy();
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Undo your call' }));
    expect(decide).toHaveBeenLastCalledWith('msg-1', 'k1', 'undo');
    await waitFor(() => expect(tiles(node)[0]).toMatch(/^wrong:Wrong1/));
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'It’s a mistake: add a red mark' })).toBeTruthy();
    await waitFor(() => expect(remake).toHaveBeenCalledTimes(3), { timeout: 3000 });
  }, 20_000);

  it('never hands out the old package: no download until it is made again with every decision (#1803)', async () => {
    const user = userEvent.setup();
    const PKG: ChatFile = { id: 'f-z', kind: 'package', name: 'set - GV review package.zip', size: 4_000_000, mime: 'application/zip', url: '/api/v1/chats/c-1/files/f-z' };
    let saved: (value: unknown) => void = () => {};
    const decide = vi.fn((_message: string, _finding: string, decision: string) => new Promise((resolve) => { saved = () => resolve({ decision, by: 'anant', at: '2026-10-17T11:00:00Z' }); }));
    let finish: (file: RemadeFiles) => void = () => {};
    const remake = vi.fn(() => new Promise<RemadeFiles>((resolve) => { finish = resolve; }));
    render(<AssistantMessage message={answer({ findings, files: [CLOUD, REPORT, PKG] })} chatId="c-1" models={[OPUS]} fullCheck decide={decide} remakeCloud={remake} onReload={vi.fn()} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(node.querySelector('[data-slot="chat-package-download"]')).not.toBeNull();
    await user.click(within(node).getByRole('button', { name: /^Filler, page \d+\. Open the evidence$/ }));
    const panel = await screen.findByRole('dialog');
    await user.click(within(panel).getByRole('button', { name: 'Not a mistake: remove the red mark' }));
    await user.click(within(panel).getByRole('button', { name: 'Remove the red mark' }));
    // From the click on (not yet saved), the old package is not offered anywhere.
    expect(node.querySelector('[data-slot="chat-package-download"]')).toBeNull();
    saved(null);
    await waitFor(() => expect(node.querySelector('[data-slot="chat-package-download"]')).toBeNull());
    expect(node.querySelector('[data-slot="chat-result-files"]')).toBeNull();
    expect(within(screen.getByRole('dialog')).queryByRole('link', { name: /Download package/ })).toBeNull();
    await waitFor(() => expect(remake).toHaveBeenCalledTimes(1), { timeout: 3000 });
    expect(node.querySelector('[data-slot="chat-package-line"]')?.textContent).toBe('Making the package again with your 1 decision…');
    // The server's answer: the new drawing, and every file made with it (the new package among them).
    const NEW_CLOUD: ChatFile = { ...CLOUD, id: 'f-c2', url: '/api/v1/chats/c-1/files/f-c2' };
    const NEW_PKG: ChatFile = { ...PKG, id: 'f-z2', url: '/api/v1/chats/c-1/files/f-z2' };
    finish({ ...NEW_CLOUD, made: [NEW_CLOUD, { ...REPORT, id: 'f-r2' }, NEW_PKG] });
    await waitFor(() => expect(node.querySelector('[data-slot="chat-package-line"]')?.getAttribute('data-state')).toBe('updated'));
    expect(node.querySelector('[data-slot="chat-package-line"]')?.textContent).toMatch(/^Package updated with your 1 decision/);
    // The new package at once (the chat is not read again here), never the old one.
    expect(node.querySelector('[data-slot="chat-package-download"]')?.getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-z2');
    expect(within(screen.getByRole('dialog')).getByRole('link', { name: /Download package/ })).toBeTruthy();
    // "Re-make the package" makes it again on request.
    await user.keyboard('{Escape}');
    await user.click(within(node).getByRole('button', { name: 'Re-make the package' }));
    await waitFor(() => expect(remake).toHaveBeenCalledTimes(2), { timeout: 3000 });
  }, 20_000);

  it('says the server’s own words when the package cannot be made again, and offers no old one (#1803)', async () => {
    const PKG: ChatFile = { id: 'f-z', kind: 'package', name: 'set - GV review package.zip', size: 4_000_000, mime: 'application/zip', url: '/api/v1/chats/c-1/files/f-z' };
    const decided = findings.map((item) => (item.id === 'w2' ? { ...item, decision: { decision: 'not_a_problem' as const, by: 'anant', at: '2026-10-17T11:00:00Z' } } : item));
    const remake = vi.fn(async () => { throw new ApiError(409, { error: 'list_not_remade', message: 'This check’s Not-compared list cannot be made again with your decisions. Run the check again.', request_id: 'r-1' }); });
    render(<AssistantMessage message={answer({ findings: decided, files: [CLOUD, REPORT, PKG], package_outdated: true })} chatId="c-1" models={[OPUS]} fullCheck decide={vi.fn()} remakeCloud={remake} onReload={vi.fn()} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await waitFor(() => expect(node.querySelector('[data-slot="chat-package-line"]')?.getAttribute('data-state')).toBe('failed'), { timeout: 3000 });
    expect(node.querySelector('[data-slot="chat-package-line"]')?.textContent).toMatch(/Run the check again\.$/);
    expect(node.querySelector('[data-slot="chat-package-download"]')).toBeNull();
  }, 20_000);

  it('makes the package again by itself when the server says it is older than a decision (#1803)', async () => {
    const PKG: ChatFile = { id: 'f-z', kind: 'package', name: 'set - GV review package.zip', size: 4_000_000, mime: 'application/zip', url: '/api/v1/chats/c-1/files/f-z' };
    const decided = findings.map((item) => (item.id === 'w2' ? { ...item, decision: { decision: 'not_a_problem' as const, by: 'anant', at: '2026-10-17T11:00:00Z' } } : item));
    const remake = vi.fn(() => new Promise<ChatFile>(() => {}));
    render(<AssistantMessage message={answer({ findings: decided, files: [CLOUD, REPORT, PKG], package_outdated: true })} chatId="c-1" models={[OPUS]} fullCheck decide={vi.fn()} remakeCloud={remake} onReload={vi.fn()} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(node.querySelector('[data-slot="chat-package-download"]')).toBeNull();
    await waitFor(() => expect(remake).toHaveBeenCalledWith('msg-1'), { timeout: 3000 });
    expect(node.querySelector('[data-slot="chat-package-download"]')).toBeNull();
  }, 20_000);

  it('the evidence panel shows both values large and the spot boxed on the vendor’s page picture (#1717)', async () => {
    const user = userEvent.setup();
    const boxed = findings.map((item) => (item.id === 'w1' ? { ...item, vendor_box: { page: 4, x0: 72, y0: 144, x1: 108, y1: 151.2, file: 'set.pdf' }, arch_box: { page: 4, x0: 300, y0: 144, x1: 330, y1: 152, file: 'set.pdf' }, proof: { group: 'clouded' as const, should_be: '37" (architect 3\'-1")', proof: 'Printed at both spots, read by code' } } : item));
    render(<AssistantMessage message={answer({ findings: boxed, files: [CLOUD], package_state: { state: {}, numbers: { w1: 'C-02' } } })} chatId="c-1" models={[OPUS]} fullCheck pagePicture={(page) => `/api/v1/packages/pkg-1/plan-pages/${page}`} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('button', { name: /^C-02: Sink base width, page \d+\. Open the evidence$/ }));
    const panel = await screen.findByRole('dialog');
    expect(panel.querySelector('[data-slot="chat-evidence-number"]')?.textContent).toBe('C-02');
    // Results v2: the two big pictures side by side (the evidence crops); "Whole page" shows the vendor's page picture.
    expect([...panel.querySelectorAll('[data-slot="chat-evidence-side"] figcaption')].map((caption) => caption.textContent)).toEqual(['Vendor shows46″', 'Should be37″']);
    const sides = () => [...panel.querySelectorAll<HTMLElement>('[data-slot="chat-evidence-side"]')];
    expect(sides().map((side) => side.querySelector('img')?.getAttribute('src'))).toEqual(['/api/v1/chats/c-1/pictures/vend-w1', '/api/v1/chats/c-1/pictures/arch-w1']);
    await user.click(within(sides()[0]).getByRole('button', { name: 'Whole page' }));
    expect(sides()[0].querySelector('img')?.getAttribute('src')).toBe('/api/v1/packages/pkg-1/plan-pages/4');
    expect(within(sides()[0]).getByRole('button', { name: 'Back to the spot' })).toBeTruthy();
    expect(within(panel).getByRole('region', { name: 'How we know' }).textContent).toContain('Printed at both spots, read by code.');
    expect(panel.querySelector('[data-slot="chat-evidence-kind"]')?.textContent).toBe('Wrong · proven');
    expect(within(panel).getByRole('link', { name: /Open on the cloud drawing/ }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-c?inline=true');
  });

  it('takes an override back when the server refuses it', async () => {
    const user = userEvent.setup();
    const decide = vi.fn(async () => { throw new Error('409'); });
    const remake = vi.fn();
    render(<AssistantMessage message={answer({ findings, files: [CLOUD] })} chatId="c-1" models={[OPUS]} fullCheck decide={decide} remakeCloud={remake} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('button', { name: /^Sink base width, page \d+\. Open the evidence$/ }));
    const panel = await screen.findByRole('dialog');
    await user.click(within(panel).getByRole('button', { name: 'Not a mistake: remove the red mark' }));
    await user.click(within(panel).getByRole('button', { name: 'Remove the red mark' }));
    // Said in the panel itself: the page behind an open panel is hidden.
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent).toBe('Your answer was not saved. Try again.'));
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Not a mistake: remove the red mark' })).toBeTruthy();
    expect(tiles(node)[0]).toMatch(/^wrong:Wrong2/);
    expect(remake).not.toHaveBeenCalled();
  });

  it('the question’s bubble names the full check, its products and effort', () => {
    render(<UserMessage message={{ ...answer(), role: 'user', text: 'Full check', mode: 'full_check', products: ['countertop'] }} />);
    expect(document.querySelector('[data-slot="chat-user-message"]')?.textContent).toBe('Full check · Countertops · High effort');
  });
});

describe('accuracy and evidence on every answer', () => {
  it('closes the answer with a coloured badge whose click shows the claims and points', async () => {
    const user = userEvent.setup();
    render(
      <AssistantMessage
        message={answer({ accuracy: { score: 72, claims: 2, breakdown: [{ claim: 'Sink base width 46"', how: 'Printed at the spot; second read agrees', points: 1 }, { claim: 'Filler 2"', how: 'To check', points: 0.4 }] } })}
        chatId="c-1"
        models={[OPUS]}
      />,
    );
    const badge = screen.getByRole('button', { name: /^Accuracy 72%/ });
    expect(badge.getAttribute('data-tone')).toBe('orange');
    // It is the last thing in the answer.
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(node.querySelector('[data-slot="chat-footer"]')?.contains(badge)).toBe(true);
    await user.click(badge);
    const table = await screen.findByRole('table');
    expect(within(table).getAllByRole('row').map((row) => row.textContent)).toEqual([
      'ClaimHow it was checkedPoints',
      'Sink base width 46"Printed at the spot; second read agrees1',
      'Filler 2"To check0.4',
    ]);
  });

  it('says n/a in grey when there was nothing to check, and older answers show no badge', () => {
    const { unmount } = render(<AssistantMessage message={answer({ accuracy: { score: null } })} chatId="c-1" models={[OPUS]} />);
    expect(screen.getByRole('button', { name: /Accuracy: n\/a/ }).getAttribute('data-tone')).toBe('none');
    unmount();
    render(<AssistantMessage message={answer()} chatId="c-1" models={[OPUS]} />);
    expect(screen.queryByRole('button', { name: /Accuracy/ })).toBeNull();
  });

  it('shows each stated number’s evidence chip, opening its crop', async () => {
    const user = userEvent.setup();
    render(
      <AssistantMessage
        message={answer({ blocks: [{ type: 'text', text: 'The vanity top is 25 1/2" deep.' }], cites: [{ id: 'c1', page: 23, value: '25 1/2"', picture_id: 'pic-23', printed: 'match' }] })}
        chatId="c-1"
        models={[OPUS]}
      />,
    );
    const chip = screen.getByRole('button', { name: 'Page 23: 25 1/2". Open the evidence' });
    expect(chip.textContent).toBe('p.2325 1/2"✓');
    await user.click(chip);
    const viewer = await screen.findByRole('dialog');
    expect(within(viewer).getByRole('img', { name: 'Page 23' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/pic-23');
    expect(within(viewer).getByText('Printed at this spot: checked by code.')).toBeTruthy();
  });
});

describe('the chat page', () => {
  function renderPage(api: ChatApi, chatId: string | null = null) {
    const chats = document.body.appendChild(document.createElement('div'));
    render(
      <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
        <ChatPage packageId="pkg-1" chatId={chatId} api={api} onOpenChat={vi.fn()} stopGraceMs={0} />
      </ShellSlotsContext.Provider>,
    );
  }

  it('product chips start on All (even for a set stated "countertop"), a pick narrows what is sent, and a new chat starts on All again', async () => {
    const user = userEvent.setup();
    const stream = drivenStream();
    renderPage(fakeChatApi({ send: stream.send, list: vi.fn(async () => ({ chats: [], package_name: 'Sample', product_type: 'countertop' as const })) }));
    const chips = await screen.findByRole('group', { name: 'Products to check' });
    await waitFor(() => expect(within(chips).getByRole('button', { name: 'All' }).getAttribute('aria-pressed')).toBe('true'));
    expect(within(chips).getByRole('button', { name: 'Countertop' }).getAttribute('aria-pressed')).toBe('true');
    expect(within(chips).getByRole('button', { name: 'Cabinet' }).getAttribute('aria-pressed')).toBe('true');
    await user.click(within(chips).getByRole('button', { name: 'Cabinet' }));
    expect(within(chips).getByRole('button', { name: 'All' }).getAttribute('aria-pressed')).toBe('false');

    await waitFor(() => expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(true));
    await user.type(screen.getByRole('textbox', { name: 'Your message' }), 'Which tops differ?');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(stream.send).toHaveBeenCalled());
    expect(stream.send.mock.calls[0][1]).toEqual({ text: 'Which tops differ?', model: 'claude-opus-5-5', effort: 'high', thinking: true, products: ['countertop'] });
  });

  it('Run full check confirms what it will use, then sends a full check', async () => {
    const user = userEvent.setup();
    const stream = drivenStream();
    renderPage(fakeChatApi({ send: stream.send, list: vi.fn(async () => ({ chats: [], package_name: 'Sample', product_type: 'countertop' as const })) }), 'c-1');
    const run = await screen.findByRole('button', { name: 'Run full check' });
    await waitFor(() => expect((run as HTMLButtonElement).disabled).toBe(false));
    await user.click(run);
    const summary = await screen.findByText(/^Full check · /, { selector: '[data-slot="chat-full-check-summary"]' });
    // All by default, even for a set stated "countertop" (2026-10-18).
    // #1823: no per-answer cap here; a full check's price is its own line.
    expect(summary.textContent).toBe('Full check · Countertops + Cabinets · Opus 5.5 · High');
    expect(screen.getByText('Checks all products (countertops, cabinets).')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() => expect(stream.send).toHaveBeenCalled());
    expect(stream.send.mock.calls[0][1]).toEqual({ text: 'Full check', model: 'claude-opus-5-5', effort: 'high', thinking: true, mode: 'full_check', products: ['countertop', 'cabinet'] });
    expect(document.querySelector('[data-slot="chat-user-message"]')?.textContent).toBe('Full check · Countertops + Cabinets · High effort');
  });
});
