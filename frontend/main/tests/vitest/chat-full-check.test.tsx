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

import { AssistantMessage, UserMessage } from '@/components/chat/chat-message';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatFile } from '@/lib/chat-api';
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

  it('follows the reviewer’s path: summary, the numbers, the cloud drawing, What’s wrong, Right and Nothing to compare (folded), How this was checked', async () => {
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
    const order = ['chat-summary', 'chat-kpis', 'chat-cloud-drawing', 'chat-findings-different', 'chat-findings-same', 'chat-findings-check', 'chat-trust']
      .map((slot) => node.querySelector(`[data-slot="${slot}"]`));
    order.forEach((part) => expect(part).toBeTruthy());
    for (let at = 1; at < order.length; at += 1) {
      expect(order[at - 1]!.compareDocumentPosition(order[at]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    // 1. The summary: a code headline over the answer's own words.
    const summary = within(node).getByRole('region', { name: 'Summary' });
    expect(summary.querySelector('[data-slot="chat-summary-headline"]')?.textContent).toBe('2 wrong on the vendor’s drawings, 2 right, 1 with nothing to compare.');
    expect(within(summary).getByText('Two values are wrong; one to check.')).toBeTruthy();
    expect(within(summary).getByText('Full check · Checked: All products')).toBeTruthy();

    // 2. The three numbers, pages and accuracy: each once on the screen.
    const kpis = node.querySelector<HTMLElement>('[data-slot="chat-kpis"]')!;
    expect([...kpis.querySelectorAll<HTMLElement>('[data-kpi]')].map((tile) => `${tile.dataset.kpi}:${tile.textContent}`)).toEqual([
      'different:Wrong2Proved by code, clouded red', 'same:Right2Matches the architect', 'check:Nothing to compare1Listed apart, with the reason',
      'pages:Pages checked2/22 of 2 pages compared · 0 not comparable · 0 not opened', 'accuracy:Accuracy84%',
    ]);
    expect(node.querySelectorAll('[data-slot="chat-accuracy"]')).toHaveLength(1);
    expect(node.querySelector('[data-slot="chat-footer"] [data-slot="chat-accuracy"]')).toBeNull();
    // The page coverage lives only in "How this was checked" (folded).
    expect(node.querySelectorAll('[data-slot="chat-coverage"]')).toHaveLength(0);
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    expect(node.querySelectorAll('[data-slot="chat-coverage"]')).toHaveLength(1);
    expect(node.querySelector('[data-slot="chat-trust"] [data-slot="chat-coverage"]')).toBeTruthy();
    expect(node.textContent).not.toMatch(/To check/);

    // 3. The cloud drawing, with the check report inside its card.
    const cloud = within(node).getByRole('region', { name: 'Cloud drawing' });
    expect(within(cloud).getByRole('link', { name: 'Open cloud drawing' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-c');
    expect(cloud.querySelector('[data-slot="chat-check-report"]')).toBeTruthy();
    // The cloud drawing is the marked-up PDF: no separate button, and no plain file card for either PDF.
    expect(within(node).queryByRole('button', { name: 'Download marked-up PDF' })).toBeNull();
    expect(node.querySelector('[data-slot="chat-files"]')).toBeNull();
    expect(within(node).getByRole('button', { name: 'Download Excel (CSV)' })).toBeTruthy();

    // 4-6. What's wrong open; Right and Nothing to compare folded. A Not-compared row says why, in
    // the trade's words and ours, and what is needed: never a question to anyone.
    const ids = () => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-finding-row"]')].map((row) => row.dataset.id);
    expect(ids()).toEqual(['w1', 'w2']);
    await user.click(within(node).getByRole('button', { name: /^Right\s*2/ }));
    expect(ids()).toEqual(['w1', 'w2', 'r1', 'r2']);
    await user.click(within(node).getByRole('button', { name: /^Nothing to compare\s*1/ }));
    expect(ids()).toEqual(['w1', 'w2', 'r1', 'r2', 'k1']);
    expect(node.querySelector('[data-id="k1"] [data-slot="chat-finding-question"]')?.textContent).toBe('Not reviewed: not shown on the architect’s drawings: architect gives no size or information for itNeeded: the architect’s size or note for the island overhang');
    expect(node.textContent).not.toMatch(/Your call|your decision|vendor to confirm|check before sending/i);

    // A number jumps to its section.
    const scrolled: string[] = [];
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.textContent ?? ''); } as typeof Element.prototype.scrollIntoView;
    await user.click(within(kpis).getByRole('button', { name: 'Nothing to compare: 1. Go to them' }));
    expect(scrolled[0]).toMatch(/Nothing to compare/);
  });

  it('never labels the vendor’s own string or span “Architect” (#1581)', () => {
    const own = [
      finding('s1', { item: 'Run height string', status: 'different', source: 'vendor_arithmetic', parts: ['40"', '40 1/8"'], printed_total: '80"', proof: { group: 'clouded' } }),
      finding('s2', { item: 'Base: same span printed twice', status: 'different', source: 'cross_view', reference_side: 'vendor', created_by: 'consistency', proof: { group: 'clouded' } }),
    ];
    render(<AssistantMessage message={answer({ findings: own, files: [CLOUD] })} chatId="c-1" models={[OPUS]} fullCheck />);
    const rows = [...screen.getByRole('article', { name: 'Answer' }).querySelectorAll<HTMLElement>('[data-slot="chat-finding-row"]')];
    expect(rows).toHaveLength(2);
    const labels = (row: HTMLElement) => [...row.querySelectorAll('[data-slot="chat-evidence-side"]')].map((side) => `${side.getAttribute('data-side')} ${side.getAttribute('aria-label')}`);
    expect(labels(rows[0])).toEqual(['V Open the pictures large: Vendor: the parts', 'V Open the pictures large: Vendor: the printed total']);
    expect(labels(rows[1])[0]).toBe('V Open the pictures large: Vendor: the same span, elsewhere');
    for (const row of rows) expect(labels(row).join(' ')).not.toMatch(/Architect/);
  });

  it('leads with a drawing ready to download, counted by what code proved, decisions optional (#1581)', () => {
    const proved = [
      finding('w1', { item: 'Sink base width', status: 'different', proof: { group: 'clouded', should_be: '37" (architect)', proof: 'Printed at both spots, read by code' } }),
      finding('w2', { item: 'Filler', status: 'different', proof: { group: 'not_compared', label: 'Illegible', reason: 'could not read the vendor’s value on page 2' } }),
      finding('k1', { item: 'Island overhang', status: 'check', proof: { group: 'not_compared', label: 'Not reviewed: not shown on the architect’s drawings', reason: 'architect gives no size or information for it' } }),
      finding('r1', { item: 'Vanity top depth', status: 'same', proof: { group: 'right' } }),
    ];
    render(<AssistantMessage message={answer({ findings: proved, files: [CLOUD, REPORT] })} chatId="c-1" models={[OPUS]} fullCheck decide={vi.fn()} />);
    const cloud = within(screen.getByRole('article', { name: 'Answer' })).getByRole('region', { name: 'Cloud drawing' });
    expect(cloud.textContent).not.toMatch(/draft|signed review/i);
    expect(cloud.querySelector('[data-slot="chat-cloud-counts"]')?.textContent).toBe('1 clouded, proved by code2 with nothing to compare');
    // Download is the first and main action, before any decision.
    const actions = [...cloud.querySelectorAll<HTMLElement>('a, button')].map((el) => el.getAttribute('aria-label') ?? el.textContent);
    expect(actions[1]).toBe('Download vendor-clouded.pdf');
    expect(cloud.querySelector('[data-slot="chat-cloud-download"]')?.className).toContain('bg-primary');
    expect(cloud.querySelector('[data-slot="chat-cloud-update"]')).toBeNull();
    expect(cloud.querySelector('[data-slot="chat-cloud-review"]')?.textContent).toContain('0 of 3 overridden · optional');
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

  it('says "Nothing proved wrong" when nothing is wrong, and counts what had nothing to compare', () => {
    render(<AssistantMessage message={answer({ findings: findings.filter((item) => item.status !== 'different') })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(node.querySelector('[data-slot="chat-summary-headline"]')?.textContent).toBe('Nothing proved wrong on the vendor’s drawings, 2 right, 1 with nothing to compare.');
    expect(node.querySelector('[data-slot="chat-findings-different"]')).toBeNull();
    expect(within(node).getByRole('button', { name: 'Wrong: 0. Go to them' }).hasAttribute('disabled')).toBe(true);
  });

  it('says a stop once, quietly, in the summary, and shows a summary GV Review wrote with one small note', () => {
    const blocks: ViewMessage['blocks'] = [{ type: 'text', text: 'Checked 30 of 60 pages.\n\n*Summary written by GV Review: the AI did not finish its own.*', by: 'code' }];
    render(<AssistantMessage message={answer({ findings, blocks, stopped: 'cancelled' })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(within(node).getByText('Checked 30 of 60 pages.')).toBeTruthy();
    expect(node.querySelectorAll('[data-slot="chat-summary-by-code"]')).toHaveLength(1);
    expect(node.textContent?.match(/Summary written by GV Review/g)).toHaveLength(1);
    expect([...node.querySelectorAll('[data-slot="chat-stopped"]')].map((line) => line.textContent)).toEqual(['You stopped this check before the end, so some items may be missing.']);
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

  it('counts drawings in the simple full check (#1665)', () => {
    const progress = { phase: 'checking' as const, pages_done: 3, pages_total: 60, wrong_so_far: 2, decision_so_far: 0, right_so_far: 1, eta_seconds: 400, drawings_done: 11, drawings_total: 36 };
    expect(progressWords({ created_at: '2026-10-17T10:00:00Z', findings: [], coverage: null, progress })).toBe('Comparing drawing 12 of 36 · 2 wrong so far · about 7 min left');
    expect(progressWords({ created_at: '2026-10-17T10:00:00Z', findings: [], coverage: null, progress: { ...progress, phase: 'reading', pages_done: 0, drawings_done: 0, eta_seconds: null } })).toBe('Comparing drawing 1 of 36 · 2 wrong so far');
  });

  it('counts the reviewer’s answers and makes the cloud drawing again from them (#1525)', async () => {
    const user = userEvent.setup();
    const decide = vi.fn(async (_message: string, _finding: string, decision: string) => ({ decision, by: 'anant', at: '2026-10-17T11:00:00Z' }));
    const NEW_CLOUD: ChatFile = { ...CLOUD, id: 'f-c2', name: 'vendor-clouded-reviewed.pdf', url: '/api/v1/chats/c-1/files/f-c2' };
    const remake = vi.fn(async () => NEW_CLOUD);
    const reload = vi.fn();
    const decided = findings.map((item) => (item.id === 'w1' ? { ...item, decision: { decision: 'agree' as const, by: 'anant' } } : item));
    render(
      <AssistantMessage message={answer({ findings: decided, files: [CLOUD, REPORT] })} chatId="c-1" models={[OPUS]} fullCheck
        decide={decide} remakeCloud={remake} onReload={reload} />,
    );
    const node = screen.getByRole('article', { name: 'Answer' });
    const review = await waitFor(() => { const found = node.querySelector<HTMLElement>('[data-slot="chat-cloud-review"]'); expect(found).toBeTruthy(); return found!; });
    expect(review.textContent).toContain('1 of 3 overridden');
    // Optimistic: the count moves at once.
    await user.click(within(node.querySelector<HTMLElement>('[data-id="w2"]')!).getByRole('button', { name: 'Agree' }));
    // A Not-compared item offers only "Make it Wrong" (#1619): the Agree route, so it is clouded.
    await user.click(within(node).getByRole('button', { name: /^Nothing to compare\s*1/ }));
    const open = node.querySelector<HTMLElement>('[data-id="k1"]')!;
    expect(within(open).queryByRole('button', { name: 'Not a problem' })).toBeNull();
    await user.click(within(open).getByRole('button', { name: 'Make it Wrong' }));
    await waitFor(() => expect(node.querySelector('[data-slot="chat-cloud-review"]')?.textContent).toContain('3 of 3 overridden · reviewed by anant'));
    expect(decide).toHaveBeenCalledWith('msg-1', 'k1', 'agree');
    // Made Wrong, it moves to What's wrong, as the drawing will cloud it.
    expect(node.querySelector('[data-slot="chat-findings-different"] [data-id="k1"]')).toBeTruthy();

    await user.click(within(node).getByRole('button', { name: 'Update cloud drawing' }));
    expect(remake).toHaveBeenCalledWith('msg-1');
    await waitFor(() => expect(within(node).getByRole('link', { name: 'Open cloud drawing' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-c2'));
    expect(reload).toHaveBeenCalled();
  });

  it('takes an answer back when the server refuses it', async () => {
    const user = userEvent.setup();
    const decide = vi.fn(async () => { throw new Error('409'); });
    render(<AssistantMessage message={answer({ findings, files: [CLOUD] })} chatId="c-1" models={[OPUS]} fullCheck decide={decide} remakeCloud={vi.fn()} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    const agree = await within(node.querySelector<HTMLElement>('[data-id="w1"]')!).findByRole('button', { name: 'Agree' });
    await user.click(agree);
    await waitFor(() => expect(within(node).getByRole('alert').textContent).toBe('Your answer was not saved. Try again.'));
    expect(agree.getAttribute('aria-pressed')).toBe('false');
    expect(node.querySelector('[data-slot="chat-cloud-review"]')?.textContent).toContain('0 of 3 overridden');
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
    expect(summary.textContent).toBe('Full check · Countertops + Cabinets · Opus 5.5 · High · up to $5');
    expect(screen.getByText('Checks all products (countertops, cabinets).')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() => expect(stream.send).toHaveBeenCalled());
    expect(stream.send.mock.calls[0][1]).toEqual({ text: 'Full check', model: 'claude-opus-5-5', effort: 'high', thinking: true, mode: 'full_check', products: ['countertop', 'cabinet'] });
    expect(document.querySelector('[data-slot="chat-user-message"]')?.textContent).toBe('Full check · Countertops + Cabinets · High effort');
  });
});
