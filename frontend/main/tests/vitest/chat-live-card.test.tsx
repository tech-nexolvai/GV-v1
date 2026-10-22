// @vitest-environment jsdom
/**
 * The live full check card (#1727): the phase, the drawings moving, only proved Wrongs in the feed,
 * Stop asked first (from the card or the composer), the card drawn at once after a reload, the tab
 * title and the sidebar row while it runs. Synthetic data only.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChatList } from '@/components/chat/chat-list';
import { LiveCheck } from '@/components/chat/chat-live';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatEvent, ChatMessage } from '@/lib/chat-api';
import type { LiveDrawing, LiveFinding, LivePhase, LiveState } from '@/lib/chat-live';
import { ChatPage } from '@/pages/ChatPage';
import { chatOut, drivenFollow, fakeChatApi, QUESTION, SAVED_ANSWER, summary } from './fixtures/chat';

const AT = new Date(Date.now() - 161_000).toISOString();
const phase = (over: Partial<LivePhase> = {}): LivePhase => ({ phase: 'comparing', step: null, done: 23, total: 39, unit: 'drawings', started_at: AT, phase_started_at: AT, stopping: false, ...over });
const drawing = (id: string, page: number, status: LiveDrawing['status'], area = 'Kitchenette'): LiveDrawing => ({ id, page, status, area, round: 1, reason: null });
const finding = (key: string, over: Partial<LiveFinding> = {}): LiveFinding => ({
  key, id: null, outcome: 'wrong', proved_by: 'code', page: 16, area: 'Kitchenette', item: 'Kitchenette: overall width', vendor: '24', architect: null, should_be: '25"', product: 'countertops',
  reason: null, needs: null, impact: null, same_as: null, removed: false, previous: null, final: false, ...over,
});

function liveState(over: Partial<LiveState> = {}): LiveState {
  return {
    v: 1,
    mode: 'simple',
    started_at: AT,
    phase: phase(),
    drawings: [drawing('p16.v1', 16, 'answered'), drawing('p23.v1', 23, 'comparing', 'Buffet small'), drawing('p24.v1', 24, 'comparing'), drawing('p27.v1', 27, 'waiting')],
    findings: [finding('a'), finding('b', { outcome: 'right' }), finding('c', { removed: true }), finding('d', { same_as: 'a' })],
    tally: { wrong: 1, right: 1, rule_note: 0, not_compared: 0, waiting_second_look: 2 },
    counts: { drawings_done: 23, drawings_total: 39, second_looks_done: 0, second_looks_total: 0, files_made: 0 },
    stopping: null,
    service: null,
    ...over,
  };
}

afterEach(() => {
  document.title = '';
});

describe('the live card', () => {
  it('shows the phase, what runs now, the steps, the count, the drawings and only the proved Wrongs', () => {
    render(<LiveCheck live={liveState()} model="Sonnet 5.5" cost="$2.13" pagePicture={(page) => `/pages/${page}.png`} />);
    const card = screen.getByRole('region', { name: 'Full check in progress' });
    expect(within(card).getByRole('heading', { name: 'Comparing drawings' })).toBeTruthy();
    expect(card.querySelector('[data-slot="chat-live-now"]')?.textContent).toBe('Comparing p.24 · Kitchenette with the architect’s · 1 more at the same time');
    expect(card.querySelector('[aria-current="step"]')?.textContent).toContain('Compare');
    expect(card.querySelector('[data-slot="chat-live-count"]')?.textContent).toBe('23 of\u00a039 drawings compared');
    const bar = within(card).getByRole('progressbar', { name: 'Full check progress' });
    expect(bar.getAttribute('data-indeterminate')).toBe('false');
    expect(card.querySelector('[data-slot="chat-live-elapsed"]')?.textContent).toMatch(/^2:4\d$/);

    const tiles = [...card.querySelectorAll('[data-slot="chat-live-drawing"]')];
    expect(tiles.map((tile) => tile.getAttribute('data-status'))).toEqual(['answered', 'comparing', 'comparing', 'waiting']);
    expect(tiles[0].getAttribute('aria-label')).toBe('p.16 · Kitchenette · round 1 · done · 1 Wrong');
    expect(tiles[0].querySelector('[data-slot="chat-live-badge"]')?.textContent).toBe('1');
    expect(tiles[1].querySelector('img')?.getAttribute('src')).toBe('/pages/23.png');

    // The feed: the one proved Wrong (not the Right, the removed one or the repeat), red → green.
    const rows = card.querySelectorAll('[data-slot="chat-live-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('Overall width');
    expect(rows[0].textContent).toContain('24');
    expect(rows[0].textContent).toContain('25″');
    expect(rows[0].textContent).toContain('proved by code');
    expect(card.querySelector('[data-count="wrong"] [data-value]')?.getAttribute('data-value')).toBe('1');
    expect(card.querySelector('[data-slot="chat-live-waiting"]')?.textContent).toBe('2 possible differences waiting for a second look. They become Wrong only if it agrees.');
    expect(card.querySelector('[data-slot="chat-live-foot"]')?.textContent).toContain('Sonnet 5.5 · $2.13 so far');
  });

  it('the tally always equals the list: N proved Wrongs shown, N in the tally, even before the server\'s own tally comes', () => {
    const findings = [finding('a'), finding('b', { page: 23 }), finding('c', { page: 24 }), finding('d', { outcome: 'right' }), finding('e', { outcome: 'not_compared' }), finding('f', { same_as: 'a' }), finding('g', { removed: true })];
    render(<LiveCheck live={liveState({ findings, tally: { wrong: 0, right: 0, rule_note: 0, not_compared: 0, waiting_second_look: 1 } })} />);
    const shown = document.querySelectorAll('[data-slot="chat-live-row"]').length;
    expect(shown).toBe(3);
    const value = (kind: string) => document.querySelector(`[data-count="${kind}"] [data-value]`)?.getAttribute('data-value');
    expect(value('wrong')).toBe(String(shown));
    expect(value('right')).toBe('1');
    expect(value('not_compared')).toBe('1');
    expect(document.querySelector('[data-slot="chat-live-waiting"]')?.textContent).toMatch(/^1 possible difference/);
  });

  it('the now line is in the text\'s own greys (no red, no maroon) and areas lose their list numbers', () => {
    render(<LiveCheck live={liveState({ drawings: [drawing('p24.v1', 24, 'comparing', '2.Standard Kitchenette with cooktop'), drawing('p3.v1', 3, 'answered', 'B. Bar')] })} />);
    const now = document.querySelector('[data-slot="chat-live-now"]') as HTMLElement;
    expect(now.textContent).toBe('Comparing p.24 · Standard Kitchenette with cooktop with the architect’s');
    expect(now.querySelector('[data-slot="chat-shimmer"]')?.getAttribute('data-tone')).toBe('ink');
    expect(document.querySelector('[data-status="answered"]')?.getAttribute('aria-label')).toBe('p.3 · Bar · round 1 · done');
  });

  it('a step with no count (the final code checks) has an indeterminate bar and no time left', () => {
    render(<LiveCheck live={liveState({ phase: phase({ phase: 'double_checking', step: 'final_checks', done: null, total: null }) })} eta={120} />);
    expect(screen.getByRole('progressbar').getAttribute('data-indeterminate')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Double-checking' })).toBeTruthy();
  });

  it('before any Wrong is proved the feed says where they will appear', () => {
    render(<LiveCheck live={liveState({ findings: [], tally: { wrong: 0, right: 0, rule_note: 0, not_compared: 0, waiting_second_look: 0 } })} />);
    expect(document.querySelector('[data-slot="chat-live-feed-empty"]')?.textContent).toBe('Wrong items appear here as soon as they are proved.');
    expect(document.querySelector('[data-slot="chat-live-waiting"]')).toBeNull();
  });

  it('Stop asks first; Keep checking leaves it running, Stop check stops it', async () => {
    const onStop = vi.fn();
    function Card() {
      const [ask, setAsk] = useState(false);
      return <LiveCheck live={liveState()} onStop={onStop} askStop={ask} onAskStop={setAsk} />;
    }
    render(<Card />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Stop' }));
    expect(await screen.findByText('Stop the check?')).toBeTruthy();
    expect(document.querySelector('[data-slot="chat-live-stop-confirm"]')?.textContent).toContain('The 2 drawings being compared now will finish and be kept.');
    await user.click(screen.getByRole('button', { name: 'Keep checking' }));
    await waitFor(() => expect(screen.queryByText('Stop the check?')).toBeNull());
    expect(onStop).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Stop' }));
    await user.click(await screen.findByRole('button', { name: 'Stop check' }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('while stopping: the title, the now line and the drawings that will not start', () => {
    render(<LiveCheck live={liveState({ stopping: { in_flight: 2, not_started: 1 }, phase: phase({ stopping: true }) })} onStop={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Stopping…' })).toBeTruthy();
    expect(document.querySelector('[data-slot="chat-live-now"]')?.textContent).toBe('Finishing the 2 drawings already being compared; nothing new starts');
    expect(document.querySelector('[data-status="skipped"]')).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Stop' }).hasAttribute('disabled')).toBe(true);
  });

  it('Stop pressed here says "Stopping…" at once, before the server says so', () => {
    render(<LiveCheck live={liveState()} onStop={vi.fn()} stopping />);
    expect(screen.getByRole('heading', { name: 'Stopping…' })).toBeTruthy();
    expect(document.querySelector('[data-slot="chat-live-now"]')?.textContent).toBe('Finishing the 2 drawings already being compared; nothing new starts');
  });

  it('when the AI service stops every call it says so plainly, with no Stop', () => {
    render(<LiveCheck live={liveState({ service: 'The AI service refused the key.' })} onStop={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'The check could not go on' })).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('The AI service refused the key.');
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });
});

describe('the sidebar row of a running check', () => {
  it('shows the working star and how many drawings are compared', () => {
    const chats = [summary('c-1', { title: 'Full check · All products', running: true, mode: 'full_check', drawings_done: 10, drawings_total: 39 }), summary('c-2', { title: 'Fillers on p.8' })];
    render(<ChatList packageId="pkg-1" activeChatId="c-1" state={{ status: 'ready', chats }} creating={false} onNewChat={vi.fn()} live={{ chatId: 'c-1', running: true, done: 23, total: 39 }} />);
    const rows = document.querySelectorAll('[data-slot="chat-list-running"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toBe('23/39');
    expect(screen.getByRole('link', { name: /Full check, .*running: 23 of 39 drawings compared/ })).toBeTruthy();
  });

  it('the open chat follows this page: a check that ended here is not shown running from an older list', () => {
    const chats = [summary('c-1', { title: 'Full check · All products', running: true, mode: 'full_check', drawings_done: 9, drawings_total: 39 })];
    render(<ChatList packageId="pkg-1" activeChatId="c-1" state={{ status: 'ready', chats }} creating={false} onNewChat={vi.fn()} live={{ chatId: 'c-1', running: false, done: null, total: null }} />);
    expect(document.querySelector('[data-slot="chat-list-running"]')).toBeNull();
    expect(document.querySelector('[data-slot="chat-list-when"]')).not.toBeNull();
  });
});

describe('a full check running when its chat is opened', () => {
  const asked: ChatMessage = { ...QUESTION, mode: 'full_check', text: 'Full check · All products' };

  function renderChat(api: ChatApi) {
    const chats = document.body.appendChild(document.createElement('div'));
    render(
      <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
        <ChatPage packageId="pkg-1" chatId="c-1" api={api} onOpenChat={vi.fn()} stopGraceMs={0} />
      </ShellSlotsContext.Provider>,
    );
  }

  it('draws the live card at once, follows its events, and the tab says how far it is', async () => {
    const feed = drivenFollow();
    const api = fakeChatApi({ get: vi.fn(async () => chatOut('c-1', [asked], { running: true, running_message_id: 'a-7', live: liveState() })), follow: feed.follow });
    renderChat(api);
    expect(await screen.findByRole('region', { name: 'Full check in progress' })).toBeTruthy();
    expect(document.title).toBe('(23/39) Checking · GV Review');

    // The replay from the start (the snapshot stays on screen meanwhile), then one step further.
    const seed = liveState();
    act(() => feed.emit(
      { event: 'check_phase', data: { v: 1, phase: phase({ phase: 'getting_ready', done: null, total: null }) } } as ChatEvent,
      { event: 'check_drawings', data: { v: 1, drawings: seed.drawings } } as ChatEvent,
      { event: 'check_phase', data: { v: 1, phase: phase({ done: 0 }) } } as ChatEvent,
    ));
    expect(document.querySelector('[data-slot="chat-live-count"]')?.textContent).toBe('23 of\u00a039 drawings compared');
    expect(document.querySelectorAll('[data-slot="chat-live-row"]')).toHaveLength(1);
    act(() => feed.emit(
      ...seed.findings.map((each) => ({ event: 'check_finding', data: { v: 1, finding: each } }) as ChatEvent),
      { event: 'check_tally', data: { v: 1, tally: seed.tally } } as ChatEvent,
      { event: 'check_phase', data: { v: 1, phase: phase({ done: 24 }) } } as ChatEvent,
      { event: 'check_finding', data: { v: 1, finding: finding('e', { page: 23, item: 'Buffet small: depth', vendor: '22', should_be: '24"' }) } } as ChatEvent,
    ));
    expect(document.querySelector('[data-slot="chat-live-count"]')?.textContent).toBe('24 of\u00a039 drawings compared');
    expect(document.querySelectorAll('[data-slot="chat-live-row"]')).toHaveLength(2);
    expect(document.title).toBe('(24/39) Checking · GV Review');

    await act(async () => feed.end({ ...SAVED_ANSWER, id: 'a-7', mode: 'full_check' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Full check in progress' })).toBeNull());
    expect(document.title).not.toContain('Checking');
  });

  it('the composer’s Stop asks first, and only Stop check asks the server to stop', async () => {
    const feed = drivenFollow();
    const api = fakeChatApi({ get: vi.fn(async () => chatOut('c-1', [asked], { running: true, running_message_id: 'a-7', live: liveState() })), follow: feed.follow });
    renderChat(api);
    await screen.findByRole('region', { name: 'Full check in progress' });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Stop response' }));
    expect(await screen.findByText('Stop the check?')).toBeTruthy();
    expect(api.cancel).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Stop check' }));
    expect(api.cancel).toHaveBeenCalledWith('c-1');
  });
});

describe('the live card while pages are matched across files (#1883)', () => {
  const usual = { low_minutes: 35, high_minutes: 65, pages: 99, matched: 96 };

  it('shows the matching step with its count and the usual time for this set', () => {
    const live = liveState({ mode: 'roster', drawings: [], findings: [], phase: phase({ step: 'matching', done: 74, total: 96, unit: 'drawings', usual }) });
    render(<LiveCheck live={live} />);
    const card = screen.getByRole('region', { name: 'Full check in progress' });
    expect(within(card).getByRole('heading', { name: 'Matching pages' })).toBeTruthy();
    expect(card.querySelector('[aria-current="step"]')?.textContent).toContain('Compare');
    expect(card.querySelector('[data-slot="chat-live-count"]')?.textContent).toBe('74 of 96 drawings matched to the architect’s');
    expect(card.querySelector('[data-slot="chat-live-left"]')?.textContent).toBe('usually 35–65 min for 99\u00a0content pages');
  });

  it('says no time when the server gives no range for the set', () => {
    const live = liveState({ mode: 'roster', drawings: [], findings: [], phase: phase({ step: 'matching', done: 3, total: 96, unit: 'drawings' }) });
    render(<LiveCheck live={live} />);
    expect(document.querySelector('[data-slot="chat-live-left"]')?.textContent).toBe('');
  });
});
