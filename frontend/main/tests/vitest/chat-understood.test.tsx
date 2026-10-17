// @vitest-environment jsdom
/**
 * #1551: the quiet "Understood as" line under a typed question, its Edit form and Resend, the stream
 * event and the saved block, and the chat page sending the correction with the same question.
 * Synthetic data only.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { UnderstoodLine } from '@/components/chat/chat-understood';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatMessage, ChatUnderstood } from '@/lib/chat-api';
import { applyChatEvent, fromSaved, localAnswer } from '@/lib/chat-messages';
import { formOf, pagesText, parsePages, taskEditOf } from '@/lib/chat-understood';
import { ChatPage } from '@/pages/ChatPage';
import { chatOut, drivenStream, fakeChatApi, QUESTION, SAVED_ANSWER } from './fixtures/chat';

const UNDERSTOOD: ChatUnderstood = {
  task: 'Compare outlet heights (architect vs vendor)',
  intent: 'compare',
  scope: { kind: 'pages', pages: [4], area: null, label: 'page 4' },
  products: ['countertop'],
  what_to_compare: ['outlet heights'],
  checks_to_run: ['read_value', 'verify_value'],
  output_shape: 'short',
  assumptions: [],
  question_back: null,
  label: 'Compare outlet heights (architect vs vendor) · page 4 · short answer',
  path: 'model',
  seconds: 0.8,
  cost_usd: '0.0003',
};

describe('the Understood as line', () => {
  it('reads quietly under the question, with Edit', () => {
    render(<UnderstoodLine understood={UNDERSTOOD} onResend={vi.fn()} />);
    const line = document.querySelector('[data-slot="chat-understood"]');
    expect(line?.textContent).toBe('Understood as: Compare outlet heights (architect vs vendor) · page 4 · short answer · Edit');
    expect(line?.className).toContain('text-muted-foreground');
    expect(screen.getByRole('button', { name: /^Edit how the question was understood/ })).toBeTruthy();
  });

  it('has no Edit while an answer is running', () => {
    render(<UnderstoodLine understood={UNDERSTOOD} onResend={vi.fn()} disabled />);
    expect(screen.queryByRole('button', { name: /^Edit/ })).toBeNull();
    expect(document.querySelector('[data-slot="chat-understood"]')?.textContent).toBe(`Understood as: ${UNDERSTOOD.label}`);
  });

  it('Edit opens the form filled in; Resend sends the correction', async () => {
    const onResend = vi.fn();
    const user = userEvent.setup();
    render(<UnderstoodLine understood={UNDERSTOOD} onResend={onResend} />);
    await user.click(screen.getByRole('button', { name: /^Edit/ }));

    const form = screen.getByRole('form', { name: 'Correct how the question was understood' });
    const task = within(form).getByRole('textbox', { name: 'What to check' }) as HTMLInputElement;
    expect(task.value).toBe(UNDERSTOOD.task);
    const pages = within(form).getByRole('textbox', { name: 'Pages' }) as HTMLInputElement;
    expect(pages.value).toBe('4');
    await user.clear(task);
    await user.type(task, 'Compare outlet heights and the backsplash');
    await user.clear(pages);
    await user.type(pages, '4-5, 9');
    await user.selectOptions(within(form).getByRole('combobox', { name: 'Answer' }), 'findings');
    await user.click(within(form).getByRole('button', { name: 'Resend' }));

    expect(onResend).toHaveBeenCalledWith({
      task: 'Compare outlet heights and the backsplash',
      intent: 'compare',
      checks_to_run: ['read_value', 'verify_value'],
      what_to_compare: ['outlet heights'],
      pages: [4, 5, 9],
      output_shape: 'findings',
    });
    expect(screen.queryByRole('form')).toBeNull();
  });

  it('pages it cannot read are said plainly and nothing is sent', async () => {
    const onResend = vi.fn();
    const user = userEvent.setup();
    render(<UnderstoodLine understood={UNDERSTOOD} onResend={onResend} />);
    await user.click(screen.getByRole('button', { name: /^Edit/ }));
    const pages = screen.getByRole('textbox', { name: 'Pages' });
    await user.clear(pages);
    await user.type(pages, 'page four');
    await user.click(screen.getByRole('button', { name: 'Resend' }));
    expect(screen.getByRole('alert').textContent).toContain('Pages look like');
    expect(onResend).not.toHaveBeenCalled();
  });

  it('Cancel and Escape close the form', async () => {
    const user = userEvent.setup();
    render(<UnderstoodLine understood={UNDERSTOOD} onResend={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: /^Edit/ }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('form')).toBeNull();
    await user.click(screen.getByRole('button', { name: /^Edit/ }));
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('form')).toBeNull();
  });

  it('a question back starts the form empty and lets the server pick the intent', () => {
    const asked: ChatUnderstood = {
      ...UNDERSTOOD, task: 'Not clear enough to check yet', intent: 'clarify', scope: { kind: 'whole_set', pages: [], label: 'the whole set' },
      what_to_compare: [], checks_to_run: [], output_shape: 'one_line', question_back: 'What should I check?', label: 'Not clear enough to check yet: one question back to you',
    };
    const form = formOf(asked);
    expect(form).toEqual({ task: '', compare: '', pages: '', shape: 'short' });
    expect(taskEditOf(asked, form)).toBe('Say what to check.');
    expect(taskEditOf(asked, { ...form, task: 'Depths on page 7' })).toEqual({ task: 'Depths on page 7', what_to_compare: [], pages: [], output_shape: 'short' });
  });
});

describe('pages as typed', () => {
  it('reads "4, 7-9" and writes it back', () => {
    expect(parsePages('4, 7-9')).toEqual([4, 7, 8, 9]);
    expect(parsePages('  ')).toEqual([]);
    expect(parsePages('9-7')).toBeNull();
    expect(parsePages('four')).toBeNull();
    expect(pagesText([9, 4, 7, 8])).toBe('4, 7-9');
  });
});

describe('the stream and the saved message', () => {
  it('the understood event sets the line; message_end keeps the stored one', () => {
    let answer = localAnswer('k', { model: 'claude-opus-5-5', effort: 'high', thinking: true });
    answer = applyChatEvent(answer, { event: 'understood', data: { index: 0, understood: UNDERSTOOD } });
    expect(answer.understood?.label).toBe(UNDERSTOOD.label);
    answer = applyChatEvent(answer, { event: 'text_delta', data: { index: 1, text: 'They match.' } });
    const stored: ChatMessage = { ...SAVED_ANSWER, blocks: [{ type: 'understood', understood: UNDERSTOOD }, { type: 'text', text: 'They match.' }] };
    answer = applyChatEvent(answer, { event: 'message_end', data: { message: stored } });
    expect(answer.understood?.path).toBe('model');
    // The block is not drawn among the answer's words and steps.
    expect(answer.blocks.map((block) => block.type)).toEqual(['text']);
  });

  it('a saved answer reads its understood block', () => {
    const saved = fromSaved({ ...SAVED_ANSWER, blocks: [{ type: 'understood', understood: UNDERSTOOD }] });
    expect(saved.understood?.task).toBe(UNDERSTOOD.task);
  });
});

describe('on the chat page', () => {
  it('shows the line under the question and resends the same question with the correction', async () => {
    const answered: ChatMessage = { ...SAVED_ANSWER, understood: UNDERSTOOD };
    const stream = drivenStream();
    const api: ChatApi = fakeChatApi({ get: vi.fn(async (id: string) => chatOut(id, [QUESTION, answered])), send: stream.send });
    const chats = document.body.appendChild(document.createElement('div'));
    render(
      <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
        <ChatPage packageId="pkg-1" chatId="c-1" api={api} onOpenChat={vi.fn()} onNameChange={vi.fn()} stopGraceMs={0} />
      </ShellSlotsContext.Provider>,
    );
    await screen.findByRole('article', { name: 'Answer' });
    const line = document.querySelector('[data-slot="chat-understood"]');
    expect(line?.textContent).toContain(`Understood as: ${UNDERSTOOD.label}`);
    // It sits right under the reviewer's question.
    expect(line?.previousElementSibling?.getAttribute('data-slot')).toBe('chat-user-message');

    const user = userEvent.setup();
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Model:/ })).not.toBeNull());
    await user.click(screen.getByRole('button', { name: /^Edit how the question was understood/ }));
    const pages = screen.getByRole('textbox', { name: 'Pages' });
    await user.clear(pages);
    await user.type(pages, '5');
    await user.click(screen.getByRole('button', { name: 'Resend' }));

    await waitFor(() => expect(stream.send).toHaveBeenCalledTimes(1));
    const body = stream.send.mock.calls[0][1];
    expect(body.text).toBe(QUESTION.text);
    expect(body.task).toEqual({
      task: UNDERSTOOD.task,
      intent: 'compare',
      checks_to_run: ['read_value', 'verify_value'],
      what_to_compare: ['outlet heights'],
      pages: [5],
      output_shape: 'short',
    });
  });
});
