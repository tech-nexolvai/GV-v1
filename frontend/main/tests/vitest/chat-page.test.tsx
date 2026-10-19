// @vitest-environment jsdom
/**
 * The chat page (#1464) against a fake chat API: a new chat made on its first message (#1479), the
 * empty chat, a saved chat shown exactly after a reload, a message streaming in, Stop, an answer still
 * running followed live after a reload (#1479), the composer's model / effort / thinking rules, and
 * models that are not available. Synthetic data only.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { CHOICE_KEY } from '@/lib/chat-settings';
import { ChatPage } from '@/pages/ChatPage';
import type { ChatApi } from '@/lib/chat-api';
import { chatOut, drivenFollow, drivenStream, fakeChatApi, modelsOut, QUESTION, SAVED_ANSWER, SET_NAME, summary } from './fixtures/chat';

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    // no storage in this environment
  }
});
afterEach(() => vi.useRealTimers());

function renderChat(api: ChatApi, chatId: string | null = 'c-1', props: Partial<React.ComponentProps<typeof ChatPage>> = {}) {
  const onOpenChat = vi.fn();
  const onNameChange = vi.fn();
  // The place under the open set in the app's sidebar, where the page lists the set's chats.
  const chats = document.body.appendChild(document.createElement('div'));
  render(
    <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
      <ChatPage packageId="pkg-1" chatId={chatId} api={api} onOpenChat={onOpenChat} onNameChange={onNameChange} stopGraceMs={0} {...props} />
    </ShellSlotsContext.Provider>,
  );
  return { onOpenChat, onNameChange };
}

const composer = () => screen.getByRole('textbox', { name: 'Your message' });
const sendButton = () => screen.getByRole('button', { name: 'Send message' });

describe('opening a drawing set’s chats', () => {
  it('#/chat/<set> shows the empty composer and makes no chat (like claude.ai)', async () => {
    const api = fakeChatApi({ list: vi.fn(async () => ({ chats: [summary('c-old', { title: 'Older', turns: 1 })], package_name: SET_NAME })) });
    const { onOpenChat } = renderChat(api, null);
    const greeting = await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    // #1538: at most two balanced lines, centred; a long name is cut with "…", the whole question on hover.
    expect(greeting.className).toContain('line-clamp-2');
    expect(greeting.className).toContain('text-balance');
    expect(greeting.getAttribute('title')).toBe(`What do you want to know about ${SET_NAME}?`);
    expect(greeting.querySelector('[data-slot="gv-spark"]')).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Model:/ })).not.toBeNull());
    expect(composer()).toBeTruthy();
    expect(api.create).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
    expect(onOpenChat).not.toHaveBeenCalled();
  });

  it('the first send makes the chat, moves the address to it, then sends to it', async () => {
    const stream = drivenStream();
    const api = fakeChatApi({ send: stream.send, list: vi.fn(async () => ({ chats: [], package_name: SET_NAME })) });
    const { onOpenChat } = renderChat(api, null);
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Model:/ })).not.toBeNull());
    await userEvent.setup().type(composer(), 'How many pages?{Enter}');
    await waitFor(() => expect(stream.send).toHaveBeenCalledTimes(1));
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.create.mock.invocationCallOrder[0]).toBeLessThan(stream.send.mock.invocationCallOrder[0]);
    expect(stream.send.mock.calls[0][0]).toBe('c-new');
    expect(stream.send.mock.calls[0][1].text).toBe('How many pages?');
    expect(onOpenChat).toHaveBeenCalledWith('c-new', { replace: true });
  });

  it('New chat goes to the empty composer instead of making a chat', async () => {
    const api = fakeChatApi({ get: vi.fn(async (id: string) => chatOut(id, [QUESTION, SAVED_ANSWER])) });
    const { onOpenChat } = renderChat(api);
    const nav = await screen.findByRole('navigation', { name: 'Chats in this drawing set' });
    await screen.findByRole('article', { name: 'Answer' });
    await userEvent.setup().click(within(nav).getByRole('button', { name: 'New chat' }));
    expect(onOpenChat).toHaveBeenCalledWith(null);
    expect(api.create).not.toHaveBeenCalled();
  });

  it('lists the set’s chats as links (in the sidebar), the open one marked, with New chat on top', async () => {
    const api = fakeChatApi({ list: vi.fn(async () => ({ chats: [summary('c-1', { title: 'Which items are wrong?', turns: 1, last_message_at: new Date().toISOString() }), summary('c-2')] })) });
    renderChat(api);
    const nav = await screen.findByRole('navigation', { name: 'Chats in this drawing set' });
    const links = await within(nav).findAllByRole('link');
    expect(links.map((link) => link.textContent)).toEqual(['Which items are wrong?', 'New chat']);
    expect(links[0].getAttribute('href')).toBe('#/chat/pkg-1/c-1');
    expect(links[0].getAttribute('aria-current')).toBe('page');
    expect(within(nav).getAllByRole('button').map((button) => button.textContent)).toEqual(['New chat']);
  });
});

describe('a link to a drawing set that is not there (#1685)', () => {
  it('says the drawing set was not found, with a way back, and offers no full check, composer or model error', async () => {
    const missing = () => { throw new ApiError(404, { error: 'not_found', message: 'Not found', request_id: 'r' }); };
    const api = fakeChatApi({ list: vi.fn(async () => missing()), models: vi.fn(async () => missing()) });
    renderChat(api, null);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('This drawing set was not found. It may have been opened from an old link.');
    expect(within(alert).getByRole('link', { name: 'Start a review' }).getAttribute('href')).toBe('#/');
    expect(screen.queryByRole('button', { name: 'Run full check' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Your message' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/models could not be loaded/);
  });

  it('a list that fails for another reason keeps the chat usable', async () => {
    const api = fakeChatApi({ list: vi.fn(async () => { throw new ApiError(503, { error: 'unavailable', message: 'Busy', request_id: 'r' }); }) });
    renderChat(api, null);
    await waitFor(() => expect(api.list).toHaveBeenCalled());
    expect(await screen.findByRole('textbox', { name: 'Your message' })).toBeTruthy();
    expect(screen.queryByText(/drawing set was not found/)).toBeNull();
  });
});

describe('an empty chat', () => {
  it('asks what you want to know about the set, offers three starters, and a starter sends with the default choice', async () => {
    const stream = drivenStream();
    const api = fakeChatApi({ send: stream.send });
    const { onNameChange } = renderChat(api);
    expect(await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeTruthy();
    expect(onNameChange).toHaveBeenCalledWith(SET_NAME);
    const starters = within(screen.getByRole('list', { name: 'Suggested questions' })).getAllByRole('button');
    expect(starters.map((button) => button.textContent)).toEqual([
      'Which items are wrong comparing the vendor and architect drawings?',
      'Summarise every issue, product by product',
      'Make me a marked-up PDF of the problems',
    ]);
    await waitFor(() => expect(starters[2].hasAttribute('disabled')).toBe(false));
    await userEvent.setup().click(starters[2]);
    // A set with no product stated is checked for every active product, always named (a strict scope).
    expect(stream.send).toHaveBeenCalledWith('c-1', { text: 'Make me a marked-up PDF of the problems', model: 'claude-opus-5-5', effort: 'high', thinking: true, products: ['countertop', 'cabinet'] }, expect.any(Function), expect.any(AbortSignal));
    expect(screen.getByText('Make me a marked-up PDF of the problems', { selector: '[data-slot="chat-user-message"]' })).toBeTruthy();
  });

  it('models not available: the reason is said plainly and sending is off', async () => {
    const api = fakeChatApi({ models: vi.fn(async () => modelsOut({ available: false, reason: 'No model key is set on this server.', models: [] })) });
    renderChat(api);
    expect((await screen.findByRole('alert')).textContent).toContain('No model key is set on this server.');
    const user = userEvent.setup();
    await user.type(composer(), 'Hello{Enter}');
    expect(api.send).not.toHaveBeenCalled();
    expect(sendButton().hasAttribute('disabled')).toBe(true);
    for (const starter of within(screen.getByRole('list', { name: 'Suggested questions' })).getAllByRole('button')) expect(starter.hasAttribute('disabled')).toBe(true);
  });

  it('models that could not be loaded say so, with Try again', async () => {
    const models = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValue(modelsOut());
    renderChat(fakeChatApi({ models }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('The models could not be loaded: The assistant could not be reached.');
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
});

describe('a saved chat, after a reload', () => {
  it('shows every block in order, folded the way a finished answer is', async () => {
    renderChat(fakeChatApi({ get: vi.fn(async () => chatOut('c-1', [QUESTION, SAVED_ANSWER])) }));
    const answer = await screen.findByRole('article', { name: 'Answer' });
    expect(screen.getByText('Which items are wrong?', { selector: '[data-slot="chat-user-message"]' })).toBeTruthy();

    // Thinking and every tool fold into ONE row that sums them up, claude.ai's way; opening it shows a timeline.
    const user = userEvent.setup();
    const row = within(answer).getByRole('button', { name: 'Thought for 12s, ran a command, viewed a picture, ran an agent' });
    expect(row.getAttribute('aria-expanded')).toBe('false');
    await user.click(row);
    expect(row.getAttribute('aria-expanded')).toBe('true');
    expect(within(answer).getByText('two sets').tagName).toBe('STRONG');
    const kinds = [...answer.querySelectorAll('[data-slot="chat-timeline"] > [data-slot="chat-entry"]')].map((entry) => entry.getAttribute('data-kind'));
    expect(kinds).toEqual(['thinking', 'step', 'step', 'tasks', 'agents']);

    // A step opens to its command and output, a picture step to its picture, a helper to its steps and report.
    await user.click(within(answer).getByRole('button', { name: 'List the PDFs' }));
    expect(within(answer).getByText('ls /mnt/user-data/uploads', { selector: 'pre' })).toBeTruthy();
    await user.click(within(answer).getByRole('button', { name: 'Look at page 3' }));
    expect(within(answer).getByRole('img', { name: 'Picture: Look at page 3' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/pic-3');
    await user.click(within(answer).getByRole('button', { name: /Pages 1-4/ }));
    expect(within(answer).getByText('Page 2 differs by 1 inch.')).toBeTruthy();

    // The task list sits inside the timeline; then the markdown answer (a heading and a table), the file and the meta line.
    const plan = answer.querySelector('[data-slot="chat-timeline"] [data-slot="chat-tasks"]');
    expect(plan?.textContent).toContain('1 of 2 done');
    expect(within(answer).getByRole('heading', { name: 'Result' })).toBeTruthy();
    expect(within(answer).getByRole('columnheader', { name: 'Architect' })).toBeTruthy();
    expect(within(answer).getByRole('cell', { name: '26' })).toBeTruthy();
    const download = within(answer).getByRole('link', { name: 'Download markup.pdf' });
    expect(download.getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-1');
    expect(download.getAttribute('download')).toBe('markup.pdf');
    expect(within(answer).getByText('PDF · 1.2 MB')).toBeTruthy();
    expect(answer.querySelector('[data-slot="chat-meta"]')?.textContent).toBe('Claude Opus 5.5 · High effort · $1.72');

    // Blocks keep their order: the activity row (with the task list after it), the words, then the file and footer.
    const order = [...answer.children].map((child) => child.getAttribute('data-slot') ?? child.firstElementChild?.getAttribute('data-slot'));
    expect(order).toEqual(['chat-activity', 'chat-markdown', 'chat-files', 'chat-footer']);
  });

  it('Retry under an answer asks its question again as a new message', async () => {
    const stream = drivenStream();
    renderChat(fakeChatApi({ get: vi.fn(async () => chatOut('c-1', [QUESTION, SAVED_ANSWER])), send: stream.send }));
    const answer = await screen.findByRole('article', { name: 'Answer' });
    const retry = await waitFor(() => within(answer).getByRole('button', { name: 'Retry' }));
    await userEvent.setup().click(retry);
    expect(stream.send.mock.calls[0][1].text).toBe('Which items are wrong?');
    expect(screen.getAllByText('Which items are wrong?', { selector: '[data-slot="chat-user-message"]' })).toHaveLength(2);
    // While the new answer runs, no answer offers Retry.
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('says why a saved answer stopped, in plain words', async () => {
    const stopped = [
      { ...SAVED_ANSWER, id: 'a-1', stopped: 'cost_cap' as const },
      { ...SAVED_ANSWER, id: 'a-2', stopped: 'error' as const, error: 'The model provider did not answer' },
      { ...SAVED_ANSWER, id: 'a-3', stopped: 'turn_limit' as const },
      { ...SAVED_ANSWER, id: 'a-4', stopped: 'cancelled' as const },
      { ...SAVED_ANSWER, id: 'a-5', stopped: 'cost_cap' as const, cap_hit: 'package' as const, message_cap_usd: '5.00', package_cap_usd: '40.00' },
      { ...SAVED_ANSWER, id: 'a-6', stopped: 'cost_cap' as const, cap_hit: 'message' as const, message_cap_usd: '3.00', package_cap_usd: '40.00' },
    ];
    renderChat(fakeChatApi({ get: vi.fn(async () => chatOut('c-1', stopped)) }));
    await screen.findAllByRole('article', { name: 'Answer' });
    expect([...document.querySelectorAll('[data-slot="chat-stopped"]')].map((node) => node.textContent)).toEqual([
      // Saved without its cap: today's $5.00 setting is not named (#1483).
      'Stopped: this answer reached its cost limit',
      // #1683: the server's own plain reason, not a generic line.
      'The model provider did not answer',
      'Stopped after the maximum number of steps',
      'Stopped',
      'Stopped: this drawing set has used its $40 AI budget',
      'Stopped: this answer reached its $3 limit',
    ]);
  });
});

describe('a message streaming in', () => {
  async function ready(stream = drivenStream(), change: Partial<ChatApi> = {}) {
    const api = fakeChatApi({ send: stream.send, ...change });
    renderChat(api);
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Model:/ })).not.toBeNull());
    return { api, stream, user: userEvent.setup() };
  }

  it('Enter sends, Shift+Enter starts a new line', async () => {
    const { stream, user } = await ready();
    await user.type(composer(), 'First line{Shift>}{Enter}{/Shift}second line');
    expect(composer()).toHaveProperty('value', 'First line\nsecond line');
    expect(stream.send).not.toHaveBeenCalled();
    await user.keyboard('{Enter}');
    expect(stream.send.mock.calls[0][1].text).toBe('First line\nsecond line');
    expect(composer()).toHaveProperty('value', '');
  });

  it('shows thinking folded and shimmering while it streams, then one row for the work, words, a file and the footer in order', async () => {
    const { stream, user } = await ready();
    await user.type(composer(), 'Which items differ?{Enter}');
    const answer = screen.getByRole('article', { name: 'Answer' });
    expect(within(answer).getByText('Sending your message')).toBeTruthy();

    act(() => stream.emit(
      { event: 'message_start', data: { message_id: 'a-9', model: 'claude-opus-5-5', effort: 'high', thinking: true } },
      { event: 'thinking_start', data: { index: 0 } },
      { event: 'thinking_delta', data: { index: 0, text: 'Reading the ' } },
      { event: 'thinking_delta', data: { index: 0, text: 'vendor pages.' } },
    ));
    // Thinking stays folded, its newest line shimmering.
    const thinking = within(answer).getByRole('button', { name: 'Reading the vendor pages.' });
    expect(thinking.getAttribute('aria-expanded')).toBe('false');
    expect(thinking.querySelector('[data-slot="chat-shimmer"]')?.getAttribute('data-active')).toBe('true');
    expect(answer.querySelector('[data-slot="chat-working"]')).not.toBeNull();

    // A step joins the thinking in one row, which says what runs now.
    act(() => stream.emit(
      { event: 'thinking_end', data: { index: 0, seconds: 12 } },
      { event: 'step_start', data: { index: 1, step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', ok: null } } },
    ));
    const toggle = () => answer.querySelector('[data-slot="chat-activity-toggle"]');
    expect(toggle()?.textContent).toBe('List the PDFs');
    expect(toggle()?.querySelector('[data-slot="chat-shimmer"]')?.getAttribute('data-active')).toBe('true');

    act(() => stream.emit(
      { event: 'step_end', data: { index: 1, step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', command: 'ls', output: 'vendor.pdf', ok: true } } },
      { event: 'text_delta', data: { index: 2, text: 'Two items ' } },
      { event: 'text_delta', data: { index: 2, text: 'differ.' } },
      { event: 'file', data: { file: { id: 'f-9', name: 'issues.csv', size: 900, mime: 'text/csv', url: '/api/v1/chats/c-1/files/f-9' } } },
      { event: 'usage', data: { cost_usd: '0.42' } },
    ));
    // Once words follow, the row sums up the work.
    expect(toggle()?.textContent).toBe('Thought for 12s, ran a command');
    expect(within(answer).getByText('Two items differ.')).toBeTruthy();
    expect(answer.querySelector('[data-slot="chat-markdown"][data-streaming="true"]')).not.toBeNull();
    expect(answer.querySelector('[data-slot="chat-working"]')).not.toBeNull();
    expect(within(answer).getByRole('link', { name: 'Download issues.csv' })).toBeTruthy();
    expect(answer.querySelector('[data-slot="chat-footer"]')).toBeNull();
    expect(screen.getByRole('button', { name: 'Stop response' })).toBeTruthy();

    await act(async () => stream.end({
      id: 'a-9', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'Two items differ.', model: 'claude-opus-5-5', effort: 'high', thinking: true, cost_usd: '0.64',
      blocks: [{ type: 'thinking', text: 'Reading the vendor pages.', seconds: 12 }, { type: 'step', step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', command: 'ls', output: 'vendor.pdf', ok: true } }, { type: 'text', text: 'Two items differ.' }],
      files: [{ id: 'f-9', name: 'issues.csv', size: 900, mime: 'text/csv', url: '/api/v1/chats/c-1/files/f-9' }],
    }));
    expect(answer.querySelector('[data-slot="chat-meta"]')?.textContent).toBe('Claude Opus 5.5 · High effort · $0.64');
    expect(answer.querySelector('[data-streaming="true"]')).toBeNull();
    expect(answer.querySelector('[data-slot="chat-working"]')).toBeNull();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('Answer ready');
  });

  it('Stop asks the server to stop, stops reading, and the answer says it was stopped', async () => {
    // The chat read again after Stop (#1683): the server saved the stopped answer with what it had.
    // That read is held here, so both moments are seen in order, whatever the machine's speed.
    const stopped = { ...SAVED_ANSWER, id: 'a-9', text: 'So far', blocks: [{ type: 'text' as const, text: 'So far' }], files: [], stopped: 'cancelled' as const };
    let reread: (chat: ReturnType<typeof chatOut>) => void = () => {};
    let reads = 0;
    const get = vi.fn((id: string) => {
      reads += 1;
      return reads > 1 ? new Promise<ReturnType<typeof chatOut>>((done) => { reread = done; }) : Promise.resolve(chatOut(id));
    });
    const { api, stream, user } = await ready(drivenStream(), { get });
    await user.type(composer(), 'Summarise{Enter}');
    act(() => stream.emit({ event: 'message_start', data: { message_id: 'a-9', model: 'claude-opus-5-5', effort: 'high', thinking: true } }, { event: 'text_delta', data: { index: 0, text: 'So far' } }));
    await user.click(screen.getByRole('button', { name: 'Stop response' }));
    expect(api.cancel).toHaveBeenCalledWith('c-1');
    await waitFor(() => expect(document.querySelector('[data-slot="chat-stopped"]')?.textContent).toBe('Stopped'));
    // The stream's signal was aborted; until the saved answer is read back, Stop stays "Stopping…".
    expect((stream.send.mock.calls[0][3] as AbortSignal).aborted).toBe(true);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Stop response' }).getAttribute('title')).toBe('Stopping…');
    expect(screen.queryByRole('button', { name: 'Send message' })).toBeNull();
    // The saved answer is back: the composer is too.
    await act(async () => reread(chatOut('c-1', [{ ...QUESTION, text: 'Summarise' }, stopped])));
    expect(await screen.findByRole('button', { name: 'Send message' })).toBeTruthy();
    expect(screen.getByText('So far')).toBeTruthy();
    expect(document.querySelector('[data-slot="chat-stopped"]')?.textContent).toBe('Stopped');
  });

  it('Stop while the server is still finishing (a full check keeps its paid answers): the chat is followed again until the saved answer, files and all', async () => {
    // #1683: after Stop the server lets the calls in flight finish, verifies and makes the files,
    // which takes longer than the grace. The screen reads the chat again and follows it.
    const feed = drivenFollow();
    const get = vi.fn(async (id: string) => (get.mock.calls.length > 1 ? chatOut(id, [{ ...QUESTION, text: 'Summarise' }], { running: true, running_message_id: 'a-9' }) : chatOut(id)));
    const { api, stream, user } = await ready(drivenStream(), { get, follow: feed.follow });
    await user.type(composer(), 'Summarise{Enter}');
    act(() => stream.emit({ event: 'message_start', data: { message_id: 'a-9', model: 'claude-opus-5-5', effort: 'high', thinking: true } }, { event: 'text_delta', data: { index: 0, text: 'Half an answer' } }));
    await user.click(screen.getByRole('button', { name: 'Stop response' }));
    expect(api.cancel).toHaveBeenCalledWith('c-1');
    await waitFor(() => expect(feed.follow).toHaveBeenCalledTimes(1));
    expect(feed.follow.mock.calls[0].slice(0, 2)).toEqual(['c-1', 'a-9']);
    // Still stopping: Stop cannot be pressed again, and nothing claims the answer is finished.
    const stop = screen.getByRole('button', { name: 'Stop response' });
    expect(stop.hasAttribute('disabled')).toBe(true);
    expect(stop.getAttribute('title')).toBe('Stopping…');
    expect(api.cancel).toHaveBeenCalledTimes(1);

    await act(async () => feed.end({ ...SAVED_ANSWER, id: 'a-9', stopped: 'cancelled' }));
    // The saved answer replaces the half one, with its files.
    expect(screen.queryByText('Half an answer')).toBeNull();
    expect(screen.getByRole('link', { name: 'Download markup.pdf' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeTruthy();
  });

  it('Stop when the server has already saved the stopped answer: the saved one replaces the half one', async () => {
    const saved = { ...SAVED_ANSWER, id: 'a-9', stopped: 'cancelled' as const };
    const get = vi.fn(async (id: string) => (get.mock.calls.length > 1 ? chatOut(id, [{ ...QUESTION, text: 'Summarise' }, saved]) : chatOut(id)));
    const follow = vi.fn();
    const { stream, user } = await ready(drivenStream(), { get, follow });
    await user.type(composer(), 'Summarise{Enter}');
    act(() => stream.emit({ event: 'message_start', data: { message_id: 'a-9', model: 'claude-opus-5-5', effort: 'high', thinking: true } }, { event: 'text_delta', data: { index: 0, text: 'Half an answer' } }));
    await user.click(screen.getByRole('button', { name: 'Stop response' }));
    await waitFor(() => expect(screen.getByRole('link', { name: 'Download markup.pdf' })).toBeTruthy());
    expect(screen.queryByText('Half an answer')).toBeNull();
    expect(follow).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeTruthy();
  });

  it('a refused send (another message running) gives the question back with the reason', async () => {
    const stream = drivenStream();
    const { user } = await ready(stream);
    await user.type(composer(), 'Again{Enter}');
    await act(async () => stream.fail(new ApiError(409, { error: 'chat_busy', message: 'busy', request_id: 'r' })));
    expect((await screen.findByRole('alert')).textContent).toContain('Another message is still running in this chat.');
    expect(composer()).toHaveProperty('value', 'Again');
    expect(document.querySelector('[data-slot="chat-user-message"]')).toBeNull();
  });

  it('a stream cut off midway keeps what came and offers to reload the chat', async () => {
    const stream = drivenStream();
    const { api, user } = await ready(stream);
    await user.type(composer(), 'Go{Enter}');
    act(() => stream.emit({ event: 'text_delta', data: { index: 0, text: 'Partial' } }));
    await act(async () => stream.fail(new ApiError(502, { error: 'incomplete_stream', message: 'The answer stopped before it was complete. Reload the chat to see what was saved.', request_id: 'r' })));
    expect(document.querySelector('[data-slot="chat-failed"]')?.textContent).toContain('The answer stopped before it was complete.');
    expect(screen.getByText('Partial')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Reload chat' }));
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  });
});

describe('an answer still running when its chat is opened (a reload, another tab)', () => {
  const running = () => chatOut('c-1', [QUESTION], { running: true, running_message_id: 'a-7' });

  it('follows it live: the events so far, then each new one, shown like an answer sent from here, with Stop', async () => {
    const feed = drivenFollow();
    const api = fakeChatApi({ get: vi.fn(async () => running()), follow: feed.follow });
    renderChat(api);
    await waitFor(() => expect(feed.follow).toHaveBeenCalledTimes(1));
    expect(feed.follow.mock.calls[0].slice(0, 2)).toEqual(['c-1', 'a-7']);
    expect(screen.queryByText(/started before this page opened/)).toBeNull();
    const answer = await screen.findByRole('article', { name: 'Answer' });

    // The replay: thinking so far (one merged delta), shimmering, folded.
    act(() => feed.emit(
      { event: 'message_start', data: { message_id: 'a-7', model: 'claude-opus-5-5', effort: 'high', thinking: true } },
      { event: 'thinking_start', data: { index: 0 } },
      { event: 'thinking_delta', data: { index: 0, text: 'Reading the vendor pages.' } },
    ));
    const thinking = within(answer).getByRole('button', { name: 'Reading the vendor pages.' });
    expect(thinking.querySelector('[data-slot="chat-shimmer"]')?.getAttribute('data-active')).toBe('true');
    expect(screen.getByRole('button', { name: 'Stop response' })).toBeTruthy();

    // Then live: a step, and the words streaming in.
    act(() => feed.emit(
      { event: 'thinking_end', data: { index: 0, seconds: 9 } },
      { event: 'step_start', data: { index: 1, step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', ok: null } } },
      { event: 'step_end', data: { index: 1, step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', command: 'ls', output: 'vendor.pdf', ok: true } } },
      { event: 'text_delta', data: { index: 2, text: 'Two items ' } },
      { event: 'text_delta', data: { index: 2, text: 'differ.' } },
    ));
    expect(within(answer).getByText('Two items differ.')).toBeTruthy();
    expect(answer.querySelector('[data-slot="chat-markdown"][data-streaming="true"]')).not.toBeNull();

    await act(async () => feed.end({ ...SAVED_ANSWER, id: 'a-7' }));
    expect(answer.querySelector('[data-streaming="true"]')).toBeNull();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('Answer ready');
  });

  it('Stop on a followed answer asks the server to stop it', async () => {
    const feed = drivenFollow();
    // Read again after Stop (#1683): by then the server has saved the stopped answer.
    const get = vi.fn(async () => (get.mock.calls.length > 1 ? chatOut('c-1', [QUESTION, { ...SAVED_ANSWER, id: 'a-7', text: 'So far', blocks: [{ type: 'text' as const, text: 'So far' }], files: [], stopped: 'cancelled' as const }]) : running()));
    const api = fakeChatApi({ get, follow: feed.follow });
    renderChat(api);
    await waitFor(() => expect(feed.follow).toHaveBeenCalledTimes(1));
    act(() => feed.emit({ event: 'text_delta', data: { index: 0, text: 'So far' } }));
    await userEvent.setup().click(screen.getByRole('button', { name: 'Stop response' }));
    expect(api.cancel).toHaveBeenCalledWith('c-1');
    await waitFor(() => expect(document.querySelector('[data-slot="chat-stopped"]')?.textContent).toBe('Stopped'));
  });

  it('Stop on a followed answer the server is still finishing: it is followed again, Stop stays pressed, then the saved answer shows', async () => {
    // #1683: opened after a reload, stopped, and the server still finishing (a full check's graceful stop).
    const first = drivenFollow();
    const second = drivenFollow();
    let calls = 0;
    const follow = vi.fn<ChatApi['follow']>((...args) => {
      calls += 1;
      return calls > 1 ? second.follow(...args) : first.follow(...args);
    });
    const api = fakeChatApi({ get: vi.fn(async () => running()), follow });
    renderChat(api);
    await waitFor(() => expect(follow).toHaveBeenCalledTimes(1));
    act(() => first.emit({ event: 'text_delta', data: { index: 0, text: 'Half an answer' } }));
    await userEvent.setup().click(screen.getByRole('button', { name: 'Stop response' }));
    expect(api.cancel).toHaveBeenCalledWith('c-1');
    await waitFor(() => expect(follow).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Stop response' }).getAttribute('title')).toBe('Stopping…'));
    expect(screen.getByRole('status').textContent).toBe('Stopping');
    await act(async () => second.end({ ...SAVED_ANSWER, id: 'a-7', stopped: 'cancelled' }));
    expect(screen.getByRole('link', { name: 'Download markup.pdf' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeTruthy();
  });

  it('leaving the chat only stops reading: the server is not asked to stop', async () => {
    const feed = drivenFollow();
    const api = fakeChatApi({ get: vi.fn(async (id: string) => (id === 'c-1' ? running() : chatOut(id))), follow: feed.follow });
    const onOpenChat = vi.fn();
    const chats = document.body.appendChild(document.createElement('div'));
    const page = (chatId: string) => (
      <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
        <ChatPage packageId="pkg-1" chatId={chatId} api={api} onOpenChat={onOpenChat} stopGraceMs={0} />
      </ShellSlotsContext.Provider>
    );
    const { rerender } = render(page('c-1'));
    await waitFor(() => expect(feed.follow).toHaveBeenCalledTimes(1));
    rerender(page('c-2'));
    expect((feed.follow.mock.calls[0][3] as AbortSignal).aborted).toBe(true);
    expect(api.cancel).not.toHaveBeenCalled();
    // Coming back follows it again.
    rerender(page('c-1'));
    await waitFor(() => expect(feed.follow).toHaveBeenCalledTimes(2));
  });
});

describe('model, effort and thinking in the composer', () => {
  async function opened() {
    renderChat(fakeChatApi());
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    return { user: userEvent.setup(), model: await screen.findByRole('button', { name: 'Model: Claude Opus 5.5' }) };
  }

  it('Opus 5.5 is the default, at High effort, and its Extended thinking is locked on and says why', async () => {
    const { user, model } = await opened();
    expect(model.textContent).toBe('Opus 5.5');
    await user.click(model);
    const thinking = await screen.findByRole('menuitemcheckbox', { name: /Extended thinking/ });
    expect(thinking.getAttribute('aria-checked')).toBe('true');
    expect(thinking.getAttribute('aria-disabled')).toBe('true');
    expect(thinking.textContent).toContain('Opus 5.5 always thinks');
    expect(screen.getByRole('menuitem', { name: /Effort/ }).textContent).toContain('High');
  });

  it('with Sonnet and thinking off, efforts above High cannot be picked; the choice is remembered', async () => {
    const { user, model } = await opened();
    await user.click(model);
    await user.click(await screen.findByRole('menuitemradio', { name: /Sonnet 5.5/ }));
    await user.click(await screen.findByRole('button', { name: 'Model: Claude Sonnet 5.5' }));
    const thinking = await screen.findByRole('menuitemcheckbox', { name: /Extended thinking/ });
    expect(thinking.getAttribute('aria-disabled')).toBeNull();
    await user.click(thinking);
    expect(thinking.getAttribute('aria-checked')).toBe('false');

    // Effort opens in place with the keyboard: Enter on its row, then Enter picks.
    const effortRow = screen.getByRole('menuitem', { name: /Effort/ });
    effortRow.focus();
    await user.keyboard('{Enter}');
    expect(effortRow.getAttribute('aria-expanded')).toBe('true');
    const effort = await screen.findByRole('group', { name: 'Effort' });
    await user.keyboard('{ArrowDown}');
    const items = within(effort).getAllByRole('menuitemradio');
    const state = Object.fromEntries(items.map((item) => [item.textContent?.split('Needs')[0], item.getAttribute('aria-disabled') === 'true']));
    expect(state).toEqual({ Low: false, Medium: false, High: false, 'Extra high': true, Max: true });
    expect(items[3].textContent).toContain('Needs extended thinking (up to High without it)');
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(JSON.parse(window.localStorage.getItem(CHOICE_KEY) ?? '{}')).toEqual({ model: 'claude-sonnet-5-5', effort: 'low', thinking: false });
  });

  it('a remembered choice comes back, and is what a message is sent with', async () => {
    window.localStorage.setItem(CHOICE_KEY, JSON.stringify({ model: 'claude-sonnet-5-5', effort: 'max', thinking: true }));
    const stream = drivenStream();
    renderChat(fakeChatApi({ send: stream.send }));
    expect(await screen.findByRole('button', { name: 'Model: Claude Sonnet 5.5' })).toBeTruthy();
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    await userEvent.setup().type(composer(), 'Hi{Enter}');
    expect(stream.send.mock.calls[0][1]).toEqual({ text: 'Hi', model: 'claude-sonnet-5-5', effort: 'max', thinking: true, products: ['countertop', 'cabinet'] });
  });
});

describe('the drawing set’s spend under the composer', () => {
  it('asks for the models with the set, so the spend is this set’s chats (not always $0.00)', async () => {
    const api = fakeChatApi({ models: vi.fn(async () => modelsOut({ package_spent_usd: '4.27', package_cap_usd: '40.00' })) });
    renderChat(api);
    await waitFor(() => expect(api.models).toHaveBeenCalledWith('pkg-1'));
    expect(api.models.mock.calls.every((call: unknown[]) => call[0] === 'pkg-1')).toBe(true);
  });
});

describe('the set’s chat list keeps up', () => {
  it('a first message lists its chat, titled by the question, as soon as it starts (not when it ends)', async () => {
    const stream = drivenStream();
    const list = vi.fn()
      .mockResolvedValueOnce({ chats: [summary('c-1')] })
      .mockResolvedValue({ chats: [summary('c-1', { title: 'How many pages?', turns: 1, last_message_at: new Date().toISOString() })] });
    const api = fakeChatApi({ send: stream.send, list });
    renderChat(api);
    expect(await screen.findByRole('button', { name: 'Model: Claude Opus 5.5' })).toBeTruthy();
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    await userEvent.setup().type(composer(), 'How many pages?{Enter}');
    expect(stream.send).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);

    act(() => stream.emit({ event: 'message_start', data: { message_id: 'a-9', model: 'claude-opus-5-5', effort: 'high', thinking: true } }));

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    const nav = screen.getByRole('navigation', { name: 'Chats in this drawing set' });
    expect(await within(nav).findByText('How many pages?')).toBeTruthy();
  });

  it('a chat started elsewhere is listed when it is opened', async () => {
    const list = vi.fn()
      .mockResolvedValueOnce({ chats: [summary('c-old')] })
      .mockResolvedValue({ chats: [summary('c-1', { title: 'Started by a script', turns: 1 }), summary('c-old')] });
    const api = fakeChatApi({ list, get: vi.fn(async (id: string) => chatOut(id, [QUESTION], { running: true })) });
    renderChat(api);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    const nav = screen.getByRole('navigation', { name: 'Chats in this drawing set' });
    expect(await within(nav).findByText('Started by a script')).toBeTruthy();
  });
});
