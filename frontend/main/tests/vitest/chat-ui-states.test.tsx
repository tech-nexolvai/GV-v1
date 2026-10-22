// @vitest-environment jsdom
/**
 * UI redesign step 5 (#1744): the round strip and its groups, a result-shaped wait, a chat link that
 * is not there, losing and getting back the connection, toasts, and the composer while a full check
 * runs. Synthetic data only.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const toast = vi.hoisted(() => vi.fn());
vi.mock('sonner', async (load) => ({ ...(await load<typeof import('sonner')>()), toast }));

import { ApiError } from '@/api/client';
import { FullCheckResult } from '@/components/chat/chat-result';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatEvent, ChatMessage } from '@/lib/chat-api';
import { roundCounts, roundWords } from '@/lib/chat-trust';
import { ChatPage } from '@/pages/ChatPage';
import { chatOut, drivenFollow, fakeChatApi, finding, QUESTION, SAVED_ANSWER } from './fixtures/chat';

afterEach(() => {
  toast.mockReset();
  vi.restoreAllMocks();
});

function renderChat(api: ChatApi, chatId = 'c-1') {
  const chats = document.body.appendChild(document.createElement('div'));
  render(
    <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
      <ChatPage packageId="pkg-1" chatId={chatId} api={api} onOpenChat={vi.fn()} stopGraceMs={0} />
    </ShellSlotsContext.Provider>,
  );
}

const NO_FILES = { cloud: null, report: null, notCompared: [], pkg: null };
const WORK = { drawingsDone: 2, drawingsTotal: 2, pagesTotal: 4, seconds: 60, stopped: false, timings: null };

describe('a later round', () => {
  const findings = [
    finding('a', { item: 'Kitchen: sink base', round_status: 'still_wrong', proof: { group: 'clouded' } }),
    finding('b', { item: 'Kitchen: filler', round_status: 'new_in_later_round', proof: { group: 'clouded' } }),
    finding('c', { item: 'Bar: overall width', status: 'same', round_status: 'fixed_in_later_round', proof: { group: 'right' } }),
  ];

  it('shows the strip (Closed / Previous comment not addressed / New) and groups the Wrong tab the same way', () => {
    render(<FullCheckResult findings={findings} decisionOf={() => null} chatId="c-1" files={NO_FILES} reviewAction={null} packageState={null} work={WORK} pictureUrl={(id) => `/p/${id}`} onOpen={vi.fn()} rounds={roundCounts(findings, null)} />);
    const strip = document.querySelector('[data-slot="chat-result-rounds"]') as HTMLElement;
    expect([...strip.querySelectorAll('[data-round-cell]')].map((cell) => cell.textContent)).toEqual([
      expect.stringMatching(/^Closed1/),
      expect.stringMatching(/^Previous comment not addressed1/),
      expect.stringMatching(/^New1/),
    ]);
    const groups = [...document.querySelectorAll('[data-slot="chat-result-round"]')];
    expect(groups.map((group) => group.getAttribute('data-round'))).toEqual(['not_addressed', 'new', 'closed']);
    expect(within(groups[0] as HTMLElement).getByText('Sink base')).toBeTruthy();
    expect(within(groups[2] as HTMLElement).getByText('Overall width')).toBeTruthy();
  });

  it('says “Unchanged since round 1” as information only, never as a Right (2026-10-22)', () => {
    const unchanged = finding('d', {
      item: 'Bar: overall height', status: 'same', round_status: 'new_in_later_round',
      round_match: { page: 3, round: 1, vendor_round: 2, value: '45 11/16"', unchanged: true, compared: null, words: 'Unchanged since round 1' },
      proof: { group: 'not_compared', not_compared: 'round_only' },
    });
    render(<FullCheckResult findings={[...findings, unchanged]} decisionOf={() => null} chatId="c-1" files={NO_FILES} reviewAction={null} packageState={null} work={WORK} pictureUrl={(id) => `/p/${id}`} onOpen={vi.fn()} rounds={{ ...roundCounts(findings, null)!, unchanged: 1 }} />);
    const strip = document.querySelector('[data-slot="chat-result-rounds"]') as HTMLElement;
    expect(within(strip).getAllByText(/^1$/)).toHaveLength(3); // the three cells, unchanged by it
    fireEvent.click(document.querySelector('[role="tab"][data-tab="not_compared"]') as HTMLElement);
    expect(document.querySelector('[data-slot="chat-result-round-match"]')?.textContent).toBe('Unchanged since round 1');
    expect(roundWords({ fixed: 1, still_wrong: 1, new: 1, unchanged: 1 })).toBe(
      'Closed 1 of 2 round-1 items · 1 previous comment not addressed · 1 new · 1 unchanged since an earlier round (information only)',
    );
  });

  it('a single round has no strip', () => {
    render(<FullCheckResult findings={[finding('a', { proof: { group: 'clouded' } })]} decisionOf={() => null} chatId="c-1" files={NO_FILES} reviewAction={null} packageState={null} work={WORK} pictureUrl={(id) => `/p/${id}`} onOpen={vi.fn()} rounds={null} />);
    expect(document.querySelector('[data-slot="chat-result-rounds"]')).toBeNull();
  });
});

describe('opening a chat', () => {
  it('shows a result-shaped skeleton only after 250 ms', async () => {
    renderChat(fakeChatApi({ get: vi.fn(() => new Promise<never>(() => {})) }));
    const loading = await screen.findByLabelText('Loading the chat');
    expect(loading.getAttribute('aria-busy')).toBe('true');
    expect(loading.getAttribute('data-shown')).toBe('false');
    await waitFor(() => expect(loading.getAttribute('data-shown')).toBe('true'));
  });

  it('a link that is not a chat says it was not found, not that a message could not be sent', async () => {
    renderChat(fakeChatApi({ get: vi.fn(async () => { throw new ApiError(422, { error: 'validation_error', message: 'Synthetic: not a chat id.', request_id: 'req-3' }); }) }), 'not-a-chat');
    expect(await screen.findByText(/This chat was not found/)).toBeTruthy();
    expect(screen.queryByText(/could not be sent/)).toBeNull();
  });
});

describe('the connection', () => {
  it('says when it is lost, and when it is back reads the chat again and says so', async () => {
    const get = vi.fn(async () => chatOut('c-1', [QUESTION, SAVED_ANSWER]));
    renderChat(fakeChatApi({ get }));
    await screen.findByRole('article', { name: 'Answer' });
    const onLine = vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false);
    act(() => { window.dispatchEvent(new Event('offline')); });
    expect(document.querySelector('[data-slot="chat-offline"]')?.textContent).toContain('Connection lost. Reconnecting…');
    onLine.mockReturnValue(true);
    act(() => { window.dispatchEvent(new Event('online')); });
    expect(document.querySelector('[data-slot="chat-offline"]')).toBeNull();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(toast).toHaveBeenCalledWith('Back online.');
  });
});

describe('while a full check runs', () => {
  const asked: ChatMessage = { ...QUESTION, mode: 'full_check', text: 'Full check · All products' };

  it('the composer says to ask in a new chat, and the end of the check is said in a toast', async () => {
    const feed = drivenFollow();
    renderChat(fakeChatApi({ get: vi.fn(async () => chatOut('c-1', [asked], { running: true, running_message_id: 'a-7' })), follow: feed.follow }));
    await waitFor(() => expect(feed.follow).toHaveBeenCalledTimes(1));
    act(() => feed.emit({ event: 'message_start', data: { message_id: 'a-7', model: 'claude-opus-5-5', effort: 'high', thinking: false } } as ChatEvent));
    expect((await screen.findByRole('textbox', { name: 'Your message' })).getAttribute('placeholder')).toBe('The check is running. Ask a question in a new chat…');
    await act(async () => feed.end({ ...SAVED_ANSWER, id: 'a-7', mode: 'full_check' }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('The full check is done.'));
    expect(screen.getByRole('textbox', { name: 'Your message' }).getAttribute('placeholder')).toBe('Ask about these drawings…');
  });
});
