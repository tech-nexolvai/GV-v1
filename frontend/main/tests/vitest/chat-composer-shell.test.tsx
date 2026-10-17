// @vitest-environment jsdom
/**
 * The chat page's claude.ai look (#1471): the composer (Enter / Shift+Enter, placeholders, Send and
 * Stop), the one model button and its menu (keyboard, Opus's locked thinking, thinking off holding
 * effort at High, the money footnote), models that are not available, the disclaimer, the empty
 * chat's centred composer and starters, the "Scroll to bottom" button and a new chat's title fading
 * in. The one sidebar (folding, the phone drawer) is tested through the app in chat-shell.test.tsx.
 * Synthetic data only.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatComposer } from '@/components/chat/chat-composer';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatModel } from '@/lib/chat-api';
import type { ChatChoice } from '@/lib/chat-settings';
import { ChatPage } from '@/pages/ChatPage';
import { chatOut, drivenStream, fakeChatApi, HAIKU, modelsOut, OPUS, QUESTION, SAVED_ANSWER, SET_NAME, SONNET, summary } from './fixtures/chat';

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    // no storage in this environment
  }
});

const MODELS: ChatModel[] = [OPUS, SONNET, HAIKU];
const DEFAULT: ChatChoice = { model: OPUS.id, effort: 'high', thinking: true };

/** The composer on its own, holding its own words and choice like the page does. */
function Composer({ running = false, blocked = null, models = MODELS, onSend = vi.fn(), onStop = vi.fn(), onChoice = vi.fn() }: {
  running?: boolean;
  blocked?: string | null;
  models?: ChatModel[];
  onSend?: (text: string) => void;
  onStop?: () => void;
  onChoice?: (choice: ChatChoice) => void;
}) {
  const [value, setValue] = useState('');
  const [choice, setChoice] = useState<ChatChoice | null>(models.length ? DEFAULT : null);
  return (
    <ChatComposer
      value={value}
      onChange={setValue}
      onSend={() => {
        onSend(value);
        setValue('');
      }}
      onStop={onStop}
      running={running}
      stopping={false}
      blocked={blocked}
      models={models}
      choice={choice}
      onChoice={(next) => {
        onChoice(next);
        setChoice(next);
      }}
      placeholder="How can I help you today?"
      menuFootnote="Up to $5 per answer · $3.10 of $40 used on this set"
    />
  );
}

const box = () => screen.getByRole('textbox', { name: 'Your message' });

function renderPage(api: ChatApi, chatId: string | null = 'c-1') {
  const onOpenChat = vi.fn();
  const chats = document.body.appendChild(document.createElement('div'));
  const view = render(
    <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
      <ChatPage packageId="pkg-1" chatId={chatId} api={api} onOpenChat={onOpenChat} stopGraceMs={0} />
    </ShellSlotsContext.Provider>,
  );
  return { ...view, onOpenChat };
}

describe('the composer', () => {
  it('Enter sends, Shift+Enter starts a new line, and an empty box cannot be sent', async () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const user = userEvent.setup();
    expect(screen.getByRole('button', { name: 'Send message' }).hasAttribute('disabled')).toBe(true);
    await user.click(box());
    await user.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();
    await user.type(box(), 'One{Shift>}{Enter}{/Shift}two');
    expect(box()).toHaveProperty('value', 'One\ntwo');
    expect(screen.getByRole('button', { name: 'Send message' }).hasAttribute('disabled')).toBe(false);
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith('One\ntwo');
    expect(box()).toHaveProperty('value', '');
  });

  it('while a message runs, Send becomes Stop ("Stop response")', async () => {
    const onStop = vi.fn();
    const { rerender } = render(<Composer onStop={onStop} />);
    expect(screen.queryByRole('button', { name: 'Stop response' })).toBeNull();
    rerender(<Composer running onStop={onStop} />);
    expect(screen.queryByRole('button', { name: 'Send message' })).toBeNull();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Stop response' }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('one model button, its menu driven by the keyboard: arrows move, Enter picks, Esc closes', async () => {
    const onChoice = vi.fn();
    render(<Composer onChoice={onChoice} />);
    const user = userEvent.setup();
    const button = screen.getByRole('button', { name: 'Model: Claude Opus 5.5' });
    expect(button.textContent).toBe('Opus 5.5');

    button.focus();
    await user.keyboard('{Enter}');
    const menu = await screen.findByRole('menu');
    expect(within(menu).getAllByRole('menuitemradio').map((row) => row.textContent)).toEqual([
      'Opus 5.5Best for most review work',
      'Sonnet 5.5Smart, efficient model for everyday use',
      'Haiku 5.5Fastest model for daily tasks',
    ]);
    expect(within(menu).getByRole('menuitemradio', { name: /Opus 5.5/ }).getAttribute('aria-checked')).toBe('true');
    expect(menu.querySelector('[data-slot="chat-model-footnote"]')?.textContent).toBe('Up to $5 per answer · $3.10 of $40 used on this set');

    await waitFor(() => expect(document.activeElement?.textContent).toContain('Opus 5.5'));
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement?.textContent).toContain('Sonnet 5.5');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(onChoice).toHaveBeenLastCalledWith({ model: SONNET.id, effort: 'high', thinking: true });
    expect(screen.getByRole('button', { name: 'Model: Claude Sonnet 5.5' })).toBeTruthy();

    // Esc closes the menu and gives focus back to the button.
    screen.getByRole('button', { name: 'Model: Claude Sonnet 5.5' }).focus();
    await user.keyboard('{Enter}');
    await screen.findByRole('menu');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Model: Claude Sonnet 5.5' }));
  });

  it('Opus always thinks; turning thinking off on Sonnet holds effort at High and says why above it', async () => {
    const onChoice = vi.fn();
    render(<Composer onChoice={onChoice} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Model: Claude Opus 5.5' }));
    const locked = await screen.findByRole('menuitemcheckbox', { name: /Extended thinking/ });
    expect(locked.getAttribute('aria-checked')).toBe('true');
    expect(locked.getAttribute('aria-disabled')).toBe('true');
    expect(locked.textContent).toContain('Opus 5.5 always thinks');

    await user.click(screen.getByRole('menuitemradio', { name: /Sonnet 5.5/ }));
    await user.click(screen.getByRole('button', { name: 'Model: Claude Sonnet 5.5' }));
    const thinking = await screen.findByRole('menuitemcheckbox', { name: /Extended thinking/ });
    expect(thinking.textContent).toContain('Think longer for complex tasks');
    await user.click(thinking);
    // The switch keeps the menu open, like claude.ai's.
    expect(thinking.getAttribute('aria-checked')).toBe('false');
    expect(screen.getByRole('menu')).toBeTruthy();
    expect(onChoice).toHaveBeenLastCalledWith({ model: SONNET.id, effort: 'high', thinking: false });

    screen.getByRole('menuitem', { name: /Effort/ }).focus();
    await user.keyboard('{ArrowRight}');
    const effort = await screen.findByRole('group', { name: 'Effort' });
    const rows = within(effort).getAllByRole('menuitemradio');
    expect(rows.map((row) => row.getAttribute('aria-disabled') === 'true')).toEqual([false, false, false, true, true]);
    expect(rows[4].textContent).toBe('MaxNeeds extended thinking (up to High without it)');
    expect(rows[2].getAttribute('aria-checked')).toBe('true');
  });

  it('no models: no model button, and Send stays off', async () => {
    render(<Composer models={[]} blocked="The assistant is not available on this server." />);
    await userEvent.setup().type(box(), 'Hello');
    expect(screen.queryByRole('button', { name: /^Model:/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Send message' }).hasAttribute('disabled')).toBe(true);
  });
});

describe('the page around it', () => {
  it('an empty chat centres the composer under the greeting, with the starters and the disclaimer; the first message moves it down', async () => {
    const stream = drivenStream();
    renderPage(fakeChatApi({ send: stream.send }));
    expect(await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeTruthy();
    const dock = document.querySelector('[data-slot="chat-dock"]') as HTMLElement;
    expect(dock.getAttribute('data-centred')).toBe('true');
    expect(box().getAttribute('placeholder')).toBe('How can I help you today?');
    expect(screen.getByText('Claude can make mistakes. Please double-check responses.')).toBeTruthy();
    expect(screen.queryByText(/check before acting/)).toBeNull();
    expect((document.querySelector('[data-slot="chat-thread"]') as HTMLElement).hidden).toBe(true);

    const starters = within(screen.getByRole('list', { name: 'Suggested questions' })).getAllByRole('button');
    await waitFor(() => expect(starters[0].hasAttribute('disabled')).toBe(false));
    await userEvent.setup().click(starters[0]);
    expect(stream.send.mock.calls[0][1].text).toBe('Which items are wrong comparing the vendor and architect drawings?');
    expect(dock.getAttribute('data-centred')).toBeNull();
    expect((document.querySelector('[data-slot="chat-thread"]') as HTMLElement).hidden).toBe(false);
    expect(screen.queryByRole('list', { name: 'Suggested questions' })).toBeNull();
    expect(box().getAttribute('placeholder')).toBe('Reply to Claude…');
    expect(screen.getByRole('button', { name: 'Stop response' })).toBeTruthy();
  });

  it('a chat with messages says "Reply to Claude…"; models not available say why and Send stays off', async () => {
    renderPage(fakeChatApi({
      get: vi.fn(async () => chatOut('c-1', [QUESTION, SAVED_ANSWER])),
      models: vi.fn(async () => modelsOut({ available: false, reason: 'No model key is set on this server.', models: [] })),
    }));
    await screen.findByRole('article', { name: 'Answer' });
    expect(box().getAttribute('placeholder')).toBe('Reply to Claude…');
    expect((await screen.findByRole('alert')).textContent).toContain('No model key is set on this server.');
    await userEvent.setup().type(box(), 'Hello');
    expect(screen.getByRole('button', { name: 'Send message' }).hasAttribute('disabled')).toBe(true);
  });

  it('"Scroll to bottom" shows only away from the bottom, and takes you there', async () => {
    renderPage(fakeChatApi({ get: vi.fn(async () => chatOut('c-1', [QUESTION, SAVED_ANSWER])) }));
    await screen.findByRole('article', { name: 'Answer' });
    expect(screen.queryByRole('button', { name: 'Scroll to bottom' })).toBeNull();

    const thread = document.querySelector('[data-slot="chat-thread"]') as HTMLElement;
    Object.defineProperty(thread, 'scrollHeight', { configurable: true, value: 3000 });
    Object.defineProperty(thread, 'clientHeight', { configurable: true, value: 600 });
    thread.scrollTo = vi.fn();
    thread.scrollTop = 200;
    fireEvent.scroll(thread);
    const down = await screen.findByRole('button', { name: 'Scroll to bottom' });
    await userEvent.setup().click(down);
    expect(thread.scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: 3000 }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Scroll to bottom' })).toBeNull());
  });

  it('a new chat’s title fades in once its first message names it', async () => {
    const stream = drivenStream();
    const list = vi.fn()
      .mockResolvedValueOnce({ chats: [summary('c-1')] })
      .mockResolvedValue({ chats: [summary('c-1', { title: 'Which items are wrong', turns: 1 })] });
    renderPage(fakeChatApi({ send: stream.send, list }));
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send message' })).toBeTruthy());
    await userEvent.setup().type(box(), 'Which items are wrong{Enter}');
    await act(async () => stream.end({ ...SAVED_ANSWER, id: 'a-9' }));
    const link = await screen.findByRole('link', { name: 'Which items are wrong' });
    expect(link.querySelector('[data-fresh="true"]')).not.toBeNull();
  });
});
