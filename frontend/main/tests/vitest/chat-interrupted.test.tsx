// @vitest-environment jsdom
/**
 * An answer a server restart interrupted (#1514): it says so in plain words, says what Continue
 * will do again, and Continue sends the follow-up with the interrupted answer's id (a full check
 * goes on as a full check). Synthetic data only.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ShellSlotsContext } from '@/components/shell/shellSlots';
import { CONTINUE_TEXT, type ChatMessage, type ChatResume } from '@/lib/chat-api';
import { isFullCheck } from '@/lib/chat-full-check';
import { fromSaved, stoppedWords } from '@/lib/chat-messages';
import { ChatPage } from '@/pages/ChatPage';
import { chatOut, drivenStream, fakeChatApi, QUESTION, SAVED_ANSWER } from './fixtures/chat';

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    // no storage in this environment
  }
});

const REDO = 'Continue asks the AI to go on from what it had written and the 2 steps it had done; only the step that was running when the server restarted (and what came after it) is done again.';

function interrupted(resume: ChatResume, change: Partial<ChatMessage> = {}): ChatMessage {
  return {
    ...SAVED_ANSWER,
    id: 'a-cut',
    text: '',
    stopped: 'interrupted',
    cost_usd: '0.42',
    blocks: [{ type: 'text', text: 'Sink S1 differs on page 3.' }, { type: 'resume', resume }],
    resume,
    ...change,
  };
}

function renderChat(messages: ChatMessage[], send = drivenStream().send) {
  const chats = document.body.appendChild(document.createElement('div'));
  render(
    <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
      <ChatPage packageId="pkg-1" chatId="c-1" api={fakeChatApi({ get: vi.fn(async () => chatOut('c-1', messages)), send })} onOpenChat={vi.fn()} onNameChange={vi.fn()} stopGraceMs={0} />
    </ShellSlotsContext.Provider>,
  );
}

describe('an answer a server restart interrupted', () => {
  it('reads as interrupted, keeps its saved work and its resume, in plain words', () => {
    const resume: ChatResume = { mode: 'chat', findings: 0, pages_done: 0, redo: REDO };
    const view = fromSaved({ ...interrupted(resume), resume: undefined });
    expect(stoppedWords(view)).toBe('Interrupted by a server restart');
    // The resume comes from its block when the field is absent; it is never drawn as a block.
    expect(view.resume).toEqual(resume);
    expect(view.blocks).toEqual([{ type: 'text', text: 'Sink S1 differs on page 3.' }]);
  });

  it('shows what Continue redoes, and Continue sends the follow-up with the answer’s id', async () => {
    const stream = drivenStream();
    renderChat([QUESTION, interrupted({ mode: 'chat', findings: 0, pages_done: 0, redo: REDO })], stream.send);
    const answer = await screen.findByRole('article', { name: 'Answer' });
    const stopped = answer.querySelector('[data-slot="chat-stopped"]');
    expect(stopped?.getAttribute('data-stopped')).toBe('interrupted');
    expect(within(answer).getByText('Interrupted by a server restart')).toBeTruthy();
    expect(within(answer).getByText(REDO)).toBeTruthy();
    expect(within(answer).getByText('Sink S1 differs on page 3.')).toBeTruthy();

    const button = await waitFor(() => within(answer).getByRole('button', { name: 'Continue' }));
    await userEvent.setup().click(button);
    const body = stream.send.mock.calls[0][1];
    expect(body.text).toBe(CONTINUE_TEXT);
    expect(body.continue_from).toBe('a-cut');
    // While the continued answer runs, nothing offers Continue again.
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull();
  });

  it('offers Continue only on the newest answer', async () => {
    const resume: ChatResume = { mode: 'chat', findings: 0, pages_done: 0, redo: REDO };
    renderChat([QUESTION, interrupted(resume), { ...QUESTION, id: 'q-2' }, { ...SAVED_ANSWER, id: 'a-2' }]);
    await screen.findAllByRole('article', { name: 'Answer' });
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull();
  });

  it('a continued full check is still a full check', () => {
    expect(isFullCheck({ mode: null }, { mode: null, text: 'Continue the full check · Countertops · High effort' })).toBe(true);
    expect(isFullCheck({ mode: null }, { mode: null, text: CONTINUE_TEXT })).toBe(false);
  });
});

describe('a full check that did not finish (#1821)', () => {
  const FULL_REDO = 'Continue keeps the 12 findings and the 9 pages already compared, compares only pages 14-16, and runs the proof steps on 3 red candidates not proved yet. It has its own fixed price for that work.';

  it('says what is left, and Continue goes on as a full check from the answer’s id', async () => {
    const stream = drivenStream();
    const stopped = interrupted({ mode: 'full_check', findings: 12, pages_done: 9, redo: FULL_REDO }, { stopped: 'cancelled' });
    renderChat([QUESTION, stopped], stream.send);
    const answer = await screen.findByRole('article', { name: 'Answer' });
    expect(answer.querySelector('[data-slot="chat-unfinished"]')).toBeTruthy();
    expect(within(answer).getByText(FULL_REDO)).toBeTruthy();
    const button = await waitFor(() => within(answer).getByRole('button', { name: 'Continue' }));
    await userEvent.setup().click(button);
    const body = stream.send.mock.calls[0][1];
    expect(body.continue_from).toBe('a-cut');
  });

  it('a stopped chat answer with no resume offers no Continue', async () => {
    renderChat([QUESTION, { ...SAVED_ANSWER, id: 'a-stop', stopped: 'cancelled' }]);
    await screen.findByRole('article', { name: 'Answer' });
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull();
  });
});
