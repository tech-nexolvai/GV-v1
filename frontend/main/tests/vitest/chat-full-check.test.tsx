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
import { confirmWords, effectiveProducts, fullCheckBubble, fullCheckFiles, matchesWords, productsWords, readProducts, saveProducts, toggleProduct } from '@/lib/chat-full-check';
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
  it('picks products: the reviewer’s, else remembered, else the set’s; never none', () => {
    expect(effectiveProducts(null, null, 'cabinet')).toEqual(['cabinet']);
    expect(effectiveProducts(null, ['countertop'], 'cabinet')).toEqual(['countertop']);
    expect(effectiveProducts(['countertop', 'cabinet'], ['countertop'], 'cabinet')).toEqual(['countertop', 'cabinet']);
    expect(effectiveProducts(null, null, null)).toBeNull();
    expect(toggleProduct(['countertop'], 'cabinet')).toEqual(['countertop', 'cabinet']);
    expect(toggleProduct(['countertop', 'cabinet'], 'countertop')).toEqual(['cabinet']);
    expect(toggleProduct(['countertop'], 'countertop')).toEqual(['countertop']);
    expect(toggleProduct(null, 'cabinet')).toEqual(['cabinet']);
    saveProducts(window.localStorage, 'pkg-9', ['cabinet']);
    expect(readProducts(window.localStorage, 'pkg-9')).toEqual(['cabinet']);
    expect(readProducts(window.localStorage, 'pkg-other')).toBeNull();
  });

  it('says the full check plainly', () => {
    expect(productsWords(['cabinet', 'countertop'])).toBe('Countertops + Cabinets');
    expect(fullCheckBubble(['countertop'], 'High')).toBe('Full check · Countertops · High effort');
    expect(confirmWords(['countertop'], 'Opus 5.5', 'High', '$15')).toBe('Full check · Countertops · Opus 5.5 · High · up to $15');
    expect(confirmWords(null, 'Opus 5.5', 'High', null)).toBe('Full check · this set’s product · Opus 5.5 · High');
    expect(matchesWords('architect')).toBe('matches architect');
    expect(matchesWords('brand_prototype')).toBe('matches brand prototype');
  });

  it('finds the cloud drawing and the check report by kind, else by name', () => {
    expect(fullCheckFiles([REPORT, CLOUD])).toEqual({ cloud: CLOUD, report: REPORT, rest: [] });
    const old = { id: 'x', name: 'set-cloud-markup.pdf', size: 1, mime: 'application/pdf', url: '' };
    const csv = { id: 'y', name: 'items.csv', size: 1, mime: 'text/csv', url: '' };
    expect(fullCheckFiles([csv, old])).toEqual({ cloud: old, report: null, rest: [csv] });
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
    finding('w1', { item: 'Sink base width', status: 'different', inches: { architect: '37', vendor: '46', difference: '9' }, architect: '3\'-1"', vendor: '46"' }),
    finding('w2', { item: 'Filler', category: 'cabinet', status: 'different' }),
    finding('k1', { item: 'Island overhang', status: 'check' }),
    finding('r1', { item: 'Vanity top depth', status: 'same', architect: '22"', vendor: '22"' }),
    finding('r2', { item: 'Sink model', category: 'other', status: 'same', source: 'brand_prototype', architect: 'K-1', vendor: 'K-1' }),
  ];

  it('leads with the notes, the trust summary and the cloud drawing, then the tabs, then coverage', async () => {
    const user = userEvent.setup();
    render(
      <AssistantMessage
        message={answer({ findings, files: [REPORT, CLOUD], coverage: { pages: [{ page: 1, status: 'same' }, { page: 3, status: 'different' }] }, trust: { levels: { confirmed: 3, likely: 1, to_check: 1 } } })}
        chatId="c-1"
        models={[OPUS]}
        fullCheck
      />,
    );
    const node = screen.getByRole('article', { name: 'Answer' });
    const order = ['chat-markdown', 'chat-trust', 'chat-cloud-drawing', 'chat-check-report', 'chat-findings', 'chat-coverage']
      .map((slot) => node.querySelector(`[data-slot="${slot}"]`));
    order.forEach((part) => expect(part).toBeTruthy());
    for (let at = 1; at < order.length; at += 1) {
      expect(order[at - 1]!.compareDocumentPosition(order[at]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    // The coverage strip sits at the end, not inside the summary.
    expect(node.querySelector('[data-slot="chat-trust"] [data-slot="chat-coverage"]')).toBeNull();

    const cloud = within(node).getByRole('region', { name: 'Cloud drawing' });
    expect(within(cloud).getByRole('link', { name: 'Open cloud drawing' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-c');
    // The cloud drawing is the marked-up PDF: no separate button, and no plain file card for either PDF.
    expect(within(node).queryByRole('button', { name: 'Download marked-up PDF' })).toBeNull();
    expect(node.querySelector('[data-slot="chat-files"]')).toBeNull();
    expect(within(node).getByRole('button', { name: 'Download Excel (CSV)' })).toBeTruthy();

    const tabs = within(node).getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent)).toEqual(['What’s wrong(2)', 'To check(1)', 'What’s right(2)']);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    const ids = () => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-finding-row"]')].map((row) => row.dataset.id);
    expect(ids()).toEqual(['w1', 'w2']);

    await user.click(tabs[2]);
    expect(ids()).toEqual(['r1', 'r2']);
    expect([...node.querySelectorAll('[data-slot="chat-finding-matches"]')].map((line) => line.textContent)).toEqual(['matches architect', 'matches brand prototype']);

    // Arrow keys move between the tabs.
    await user.keyboard('{ArrowLeft}');
    expect(within(node).getAllByRole('tab')[1].getAttribute('aria-selected')).toBe('true');
    expect(ids()).toEqual(['k1']);
  });

  it('opens on To check when nothing is wrong', () => {
    render(<AssistantMessage message={answer({ findings: findings.filter((item) => item.status !== 'different') })} chatId="c-1" models={[OPUS]} fullCheck />);
    expect(screen.getAllByRole('tab')[1].getAttribute('aria-selected')).toBe('true');
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

  it('product chips start on the set’s product, are remembered for the set, and go with every message', async () => {
    const user = userEvent.setup();
    const stream = drivenStream();
    renderPage(fakeChatApi({ send: stream.send, list: vi.fn(async () => ({ chats: [], package_name: 'Sample', product_type: 'countertop' as const })) }));
    const chips = await screen.findByRole('group', { name: 'Products to check' });
    await waitFor(() => expect(within(chips).getByRole('button', { name: 'Countertop' }).getAttribute('aria-pressed')).toBe('true'));
    expect(within(chips).getByRole('button', { name: 'Cabinet' }).getAttribute('aria-pressed')).toBe('false');
    await user.click(within(chips).getByRole('button', { name: 'Cabinet' }));
    expect(readProducts(window.localStorage, 'pkg-1')).toEqual(['countertop', 'cabinet']);

    await waitFor(() => expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(true));
    await user.type(screen.getByRole('textbox', { name: 'Your message' }), 'Which tops differ?');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(stream.send).toHaveBeenCalled());
    expect(stream.send.mock.calls[0][1]).toEqual({ text: 'Which tops differ?', model: 'claude-opus-5-5', effort: 'high', thinking: true, products: ['countertop', 'cabinet'] });
  });

  it('Run full check confirms what it will use, then sends a full check', async () => {
    const user = userEvent.setup();
    const stream = drivenStream();
    renderPage(fakeChatApi({ send: stream.send, list: vi.fn(async () => ({ chats: [], package_name: 'Sample', product_type: 'countertop' as const })) }), 'c-1');
    const run = await screen.findByRole('button', { name: 'Run full check' });
    await waitFor(() => expect((run as HTMLButtonElement).disabled).toBe(false));
    await user.click(run);
    const summary = await screen.findByText(/^Full check · /, { selector: '[data-slot="chat-full-check-summary"]' });
    expect(summary.textContent).toBe('Full check · Countertops · Opus 5.5 · High · up to $5');
    await user.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() => expect(stream.send).toHaveBeenCalled());
    expect(stream.send.mock.calls[0][1]).toEqual({ text: 'Full check', model: 'claude-opus-5-5', effort: 'high', thinking: true, mode: 'full_check', products: ['countertop'] });
    expect(document.querySelector('[data-slot="chat-user-message"]')?.textContent).toBe('Full check · Countertops · High effort');
  });
});
