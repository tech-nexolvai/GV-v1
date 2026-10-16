// @vitest-environment jsdom
/**
 * The chat page (#1464) against a fake chat API: opening a set's newest chat, the empty chat, a
 * saved chat shown exactly after a reload, a message streaming in, Stop, the composer's model /
 * effort / thinking rules, and models that are not available. Synthetic data only.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import { CHOICE_KEY } from '@/lib/chat-settings';
import { ChatPage } from '@/pages/ChatPage';
import type { ChatApi } from '@/lib/chat-api';
import { chatOut, drivenStream, fakeChatApi, modelsOut, QUESTION, SAVED_ANSWER, SET_NAME, summary } from './fixtures/chat';

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
  render(<ChatPage packageId="pkg-1" chatId={chatId} api={api} onOpenChat={onOpenChat} onNameChange={onNameChange} stopGraceMs={0} {...props} />);
  return { onOpenChat, onNameChange };
}

const composer = () => screen.getByRole('textbox', { name: 'Your message' });
const sendButton = () => screen.getByRole('button', { name: 'Send' });

describe('opening a drawing set’s chats', () => {
  it('#/chat/<set> opens the newest chat in place of the address', async () => {
    const api = fakeChatApi({ list: vi.fn(async () => ({ chats: [summary('c-new', { title: 'Newest' }), summary('c-old')] })) });
    const { onOpenChat } = renderChat(api, null);
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith('c-new', { replace: true }));
    expect(api.create).not.toHaveBeenCalled();
  });

  it('a set with no chats gets one new empty chat', async () => {
    const api = fakeChatApi({ list: vi.fn(async () => ({ chats: [] })) });
    const { onOpenChat } = renderChat(api, null);
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith('c-new', { replace: true }));
    expect(api.create).toHaveBeenCalledTimes(1);
  });

  it('lists the set’s chats as links, the open one marked, with New chat on top and the review list', async () => {
    const api = fakeChatApi({ list: vi.fn(async () => ({ chats: [summary('c-1', { title: 'Which items are wrong?', turns: 1, last_message_at: new Date().toISOString() }), summary('c-2')] })) });
    renderChat(api);
    const nav = await screen.findByRole('navigation', { name: 'Chats in this drawing set' });
    const links = await within(nav).findAllByRole('link');
    expect(links.map((link) => link.textContent)).toEqual(['Which items are wrong?just now', 'New chat' + links[1].querySelector('time')?.textContent, 'Review list']);
    expect(links[0].getAttribute('href')).toBe('#/chat/pkg-1/c-1');
    expect(links[0].getAttribute('aria-current')).toBe('page');
    expect(links[2].getAttribute('href')).toBe('#/review/pkg-1');
    expect(within(nav).getAllByRole('button')[0].textContent).toBe('New chat');
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
      'Summarise every countertop and cabinet issue',
      'Make me a marked-up PDF of the problems',
    ]);
    await waitFor(() => expect(starters[2].hasAttribute('disabled')).toBe(false));
    await userEvent.setup().click(starters[2]);
    expect(stream.send).toHaveBeenCalledWith('c-1', { text: 'Make me a marked-up PDF of the problems', model: 'claude-opus-5-5', effort: 'high', thinking: true }, expect.any(Function), expect.any(AbortSignal));
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

  it('says why a saved answer stopped, in plain words', async () => {
    const stopped = [
      { ...SAVED_ANSWER, id: 'a-1', stopped: 'cost_cap' as const },
      { ...SAVED_ANSWER, id: 'a-2', stopped: 'error' as const, error: 'The model provider did not answer' },
      { ...SAVED_ANSWER, id: 'a-3', stopped: 'turn_limit' as const },
      { ...SAVED_ANSWER, id: 'a-4', stopped: 'cancelled' as const },
    ];
    renderChat(fakeChatApi({ get: vi.fn(async () => chatOut('c-1', stopped)) }));
    await screen.findAllByRole('article', { name: 'Answer' });
    expect([...document.querySelectorAll('[data-slot="chat-stopped"]')].map((node) => node.textContent)).toEqual([
      'Stopped: this answer reached its $5 limit',
      'Something went wrong. Try again.',
      'Stopped after the maximum number of steps',
      'Stopped',
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
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();

    await act(async () => stream.end({
      id: 'a-9', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'Two items differ.', model: 'claude-opus-5-5', effort: 'high', thinking: true, cost_usd: '0.64',
      blocks: [{ type: 'thinking', text: 'Reading the vendor pages.', seconds: 12 }, { type: 'step', step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', command: 'ls', output: 'vendor.pdf', ok: true } }, { type: 'text', text: 'Two items differ.' }],
      files: [{ id: 'f-9', name: 'issues.csv', size: 900, mime: 'text/csv', url: '/api/v1/chats/c-1/files/f-9' }],
    }));
    expect(answer.querySelector('[data-slot="chat-meta"]')?.textContent).toBe('Claude Opus 5.5 · High effort · $0.64');
    expect(answer.querySelector('[data-streaming="true"]')).toBeNull();
    expect(answer.querySelector('[data-slot="chat-working"]')).toBeNull();
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('Answer ready');
  });

  it('Stop asks the server to stop, stops reading, and the answer says it was stopped', async () => {
    const { api, stream, user } = await ready();
    await user.type(composer(), 'Summarise{Enter}');
    act(() => stream.emit({ event: 'message_start', data: { message_id: 'a-9', model: 'claude-opus-5-5', effort: 'high', thinking: true } }, { event: 'text_delta', data: { index: 0, text: 'So far' } }));
    await user.click(screen.getByRole('button', { name: 'Stop' }));
    expect(api.cancel).toHaveBeenCalledWith('c-1');
    await waitFor(() => expect(document.querySelector('[data-slot="chat-stopped"]')?.textContent).toBe('Stopped'));
    // The stream's signal was aborted, and the composer is back.
    expect((stream.send.mock.calls[0][3] as AbortSignal).aborted).toBe(true);
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy();
    expect(screen.getByText('So far')).toBeTruthy();
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

  it('a chat still answering elsewhere shows it, offers Stop, and reads it again until it is saved', async () => {
    const get = vi.fn()
      .mockResolvedValueOnce(chatOut('c-1', [QUESTION], { running: true }))
      .mockResolvedValue(chatOut('c-1', [QUESTION, SAVED_ANSWER], { running: false }));
    const api = fakeChatApi({ get });
    renderChat(api, 'c-1', { runningPollMs: 10 });
    expect(await screen.findByText(/Still answering the last message/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();
    await screen.findByRole('article', { name: 'Answer' });
    expect(screen.queryByText(/Still answering the last message/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy();
  });
});

describe('model, effort and thinking in the composer', () => {
  async function opened() {
    renderChat(fakeChatApi());
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    return { user: userEvent.setup(), model: await screen.findByRole('button', { name: 'Model: Claude Opus 5.5' }) };
  }

  it('Opus 5.5 is the default, at High effort, and its Thinking is locked on and says why', async () => {
    await opened();
    expect(screen.getByRole('button', { name: 'Effort: High' })).toBeTruthy();
    const thinking = screen.getByRole('button', { name: /^Thinking/ });
    expect(thinking.getAttribute('aria-pressed')).toBe('true');
    expect(thinking.getAttribute('aria-disabled')).toBe('true');
    expect(thinking.getAttribute('aria-describedby') && document.getElementById(thinking.getAttribute('aria-describedby')!)?.textContent).toBe('Opus 5.5 always thinks');
    await userEvent.setup().click(thinking);
    expect(thinking.getAttribute('aria-pressed')).toBe('true');
  });

  it('with Sonnet and thinking off, efforts above High cannot be picked; the choice is remembered', async () => {
    const { user, model } = await opened();
    await user.click(model);
    await user.click(await screen.findByRole('menuitemradio', { name: /Claude Sonnet 5.5/ }));
    const thinking = await screen.findByRole('button', { name: /^Thinking/ });
    expect(thinking.getAttribute('aria-disabled')).toBeNull();
    await user.click(thinking);
    expect(thinking.getAttribute('aria-pressed')).toBe('false');

    await user.click(screen.getByRole('button', { name: 'Effort: High' }));
    const items = await screen.findAllByRole('menuitemradio');
    const state = Object.fromEntries(items.map((item) => [item.textContent?.split('Needs')[0], item.getAttribute('aria-disabled') === 'true']));
    expect(state).toEqual({ Low: false, Medium: false, High: false, 'Extra high': true, Max: true });
    await user.click(items[0]);
    expect(screen.getByRole('button', { name: 'Effort: Low' })).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem(CHOICE_KEY) ?? '{}')).toEqual({ model: 'claude-sonnet-5-5', effort: 'low', thinking: false });
  });

  it('a remembered choice comes back, and is what a message is sent with', async () => {
    window.localStorage.setItem(CHOICE_KEY, JSON.stringify({ model: 'claude-sonnet-5-5', effort: 'max', thinking: true }));
    const stream = drivenStream();
    renderChat(fakeChatApi({ send: stream.send }));
    expect(await screen.findByRole('button', { name: 'Model: Claude Sonnet 5.5' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Effort: Max' })).toBeTruthy();
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    await userEvent.setup().type(composer(), 'Hi{Enter}');
    expect(stream.send.mock.calls[0][1]).toEqual({ text: 'Hi', model: 'claude-sonnet-5-5', effort: 'max', thinking: true });
  });
});
