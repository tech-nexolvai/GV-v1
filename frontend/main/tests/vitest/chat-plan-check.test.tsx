// @vitest-environment jsdom
/**
 * #1569: the plan check in the Run popover (0-3 one-click questions where code is unsure of the
 * page map, sent as `plan_answers`; nothing asked when it is sure) and What's wrong biggest first
 * (code's impact rank, ordering only). Synthetic data only.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { FullCheckButton } from '@/components/chat/chat-full-check';
import { AssistantMessage } from '@/components/chat/chat-message';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatFinding, ChatFullCheckPlan } from '@/lib/chat-api';
import { biggestFirst, groupByArea } from '@/lib/chat-findings';
import type { ViewMessage } from '@/lib/chat-messages';
import { planAnswers, usePlanCheck, type PlanState } from '@/lib/chat-plan-check';
import { ChatPage } from '@/pages/ChatPage';
import { drivenStream, fakeChatApi, finding, OPUS } from './fixtures/chat';

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    // no storage in this environment
  }
});

const UNSURE: ChatFullCheckPlan = {
  state: 'ready',
  summary: '60 pages · 22 pairs · rounds 1-2 · 3 general pages',
  doubts: [
    {
      id: 'prototype:f0:p28:h1',
      kind: 'prototype',
      page: 28,
      question: 'Page 28: is the lower drawing the brand prototype or the architect’s? No other architect’s drawing of this area was found.',
      options: [{ value: 'brand_prototype', label: 'Brand prototype' }, { value: 'architect', label: 'Architect’s drawing' }],
      picture_url: '/api/v1/packages/pkg-1/plan-pages/28',
      box: [0.1, 0.5, 0.9, 0.95],
    },
    {
      id: 'pair:f0:p41',
      kind: 'pair',
      page: 41,
      question: 'Page 41: no architect’s drawing was found for it. Compare it with page 40, or skip it?',
      options: [{ value: 'pair:40', label: 'Compare with page 40' }, { value: 'skip', label: 'Skip it' }],
      picture_url: null,
      box: null,
    },
  ],
  more: 1,
};

const SURE: ChatFullCheckPlan = { state: 'ready', summary: '12 pages · 6 pairs · round 1', doubts: [], more: 0 };

describe('the plan check before a full check', () => {
  it('asks only where code is unsure, with the page marked, and runs with the answers picked', async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    const loadPlan = vi.fn(async () => UNSURE);
    render(<FullCheckButton summary="Full check · Countertops · Opus 5.5 · High · up to $15" onRun={onRun} blocked={null} loadPlan={loadPlan} />);
    expect(loadPlan).not.toHaveBeenCalled(); // read only when the popover opens

    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    expect(await screen.findByText('Plan: 60 pages · 22 pairs · rounds 1-2 · 3 general pages')).toBeTruthy();
    const questions = screen.getByRole('list', { name: 'Before it runs' });
    expect(within(questions).getAllByRole('listitem')).toHaveLength(2);
    const picture = within(questions).getByRole('img', { name: 'Page 28' }) as HTMLImageElement;
    expect(picture.getAttribute('src')).toBe('/api/v1/packages/pkg-1/plan-pages/28');
    const box = document.querySelector<HTMLElement>('[data-slot="chat-plan-box"]')!;
    expect(box.style.left).toBe('10%');
    expect(box.style.top).toBe('50%');
    expect(screen.getByText(/Optional: skip these and GV Review takes the first answer itself, here and in 1 more place/)).toBeTruthy();

    const architect = screen.getByRole('button', { name: 'Architect’s drawing' });
    await user.click(architect);
    expect(architect.getAttribute('aria-pressed')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'Run' }));
    expect(onRun).toHaveBeenCalledWith([{ id: 'prototype:f0:p28:h1', answer: 'architect' }]);
  });

  it('with nothing to ask shows the plan in one line and runs straight on', async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<FullCheckButton summary="Full check" onRun={onRun} blocked={null} loadPlan={async () => SURE} />);
    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    expect(await screen.findByText('Plan: 12 pages · 6 pairs · round 1')).toBeTruthy();
    expect(screen.queryByRole('list', { name: 'Before it runs' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Run' }));
    expect(onRun).toHaveBeenCalledWith();
  });

  it('asks again while the set’s pages are being read, then shows the questions', async () => {
    const answers: ChatFullCheckPlan[] = [{ state: 'building', summary: null, doubts: [], more: 0 }, UNSURE];
    const load = vi.fn(async () => answers.shift() ?? UNSURE);
    const seen: PlanState[] = [];
    function Probe() {
      seen.push(usePlanCheck(load, true, 5));
      return null;
    }
    render(<Probe />);
    await waitFor(() => expect(seen.at(-1)?.status).toBe('ready'));
    expect(load).toHaveBeenCalledTimes(2);
    expect(seen.some((state) => state.status === 'loading')).toBe(true);
    expect(planAnswers(UNSURE.doubts, { 'pair:f0:p41': 'skip', other: 'x' })).toEqual([{ id: 'pair:f0:p41', answer: 'skip' }]);
  });

  it('a failed read leaves the full check as it was: no questions', async () => {
    const seen: PlanState[] = [];
    function Probe() {
      seen.push(usePlanCheck(async () => { throw new Error('offline'); }, true));
      return null;
    }
    render(<Probe />);
    await waitFor(() => expect(seen.at(-1)?.status).toBe('unavailable'));
  });

  it('the chat page sends the answers with the full check', async () => {
    const user = userEvent.setup();
    const stream = drivenStream();
    const plan = vi.fn(async () => UNSURE);
    const chats = document.body.appendChild(document.createElement('div'));
    render(
      <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
        <ChatPage packageId="pkg-1" chatId="c-1" api={fakeChatApi({ send: stream.send, plan, list: vi.fn(async () => ({ chats: [], package_name: 'Sample', product_type: 'countertop' as const })) })} onOpenChat={vi.fn()} stopGraceMs={0} />
      </ShellSlotsContext.Provider>,
    );
    const run = await screen.findByRole('button', { name: 'Run full check' });
    await waitFor(() => expect((run as HTMLButtonElement).disabled).toBe(false));
    await user.click(run);
    await user.click(await screen.findByRole('button', { name: 'Skip it' }));
    await user.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() => expect(stream.send).toHaveBeenCalled());
    expect(plan).toHaveBeenCalledWith('pkg-1', ['countertop', 'cabinet']); // with the products picked (#1823)
    expect(stream.send.mock.calls[0][1]).toEqual({
      text: 'Full check', model: 'claude-opus-5-5', effort: 'high', thinking: true, mode: 'full_check', products: ['countertop', 'cabinet'],
      plan_answers: [{ id: 'pair:f0:p41', answer: 'skip' }],
    });
  });
});

// ── What's wrong, biggest first ─────────────────────────────

function ranked(id: string, rank: number, change: Partial<ChatFinding> = {}): ChatFinding {
  return finding(id, { page: 10 + rank, item: `Area ${rank % 2 ? 'A' : 'B'} top width ${id}`, impact: { score: 100 - rank, rank, why: 'changes fabrication (overall size)' }, ...change });
}

function answer(findings: ChatFinding[]): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Seven items differ.' }], files: [], tasks: null, cost_usd: '1.72', stopped: null, error: null, status: 'done', findings,
  };
}

describe('What’s wrong, biggest first (#1569)', () => {
  it('shows the five biggest first under a quiet note, then the rest by area in impact order', () => {
    // Reported in page order; code ranked them 7, 3, 1, 6, 2, 5, 4.
    const findings = [7, 3, 1, 6, 2, 5, 4].map((rank, n) => ranked(`f${n + 1}`, rank));
    render(<AssistantMessage message={answer(findings)} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const node = screen.getByRole('article', { name: 'Answer' });

    const biggest = within(node).getByRole('region', { name: 'Biggest first' });
    expect(within(biggest).getByText('Biggest first')).toBeTruthy();
    const ids = [...biggest.querySelectorAll<HTMLElement>('[data-slot="chat-finding-row"]')].map((row) => row.dataset.id);
    expect(ids).toEqual(['f3', 'f5', 'f2', 'f7', 'f6']);
    const rest = node.querySelector<HTMLElement>('[data-slot="chat-findings-different"] [data-slot="chat-findings-list"]')!;
    expect([...rest.querySelectorAll<HTMLElement>('[data-slot="chat-finding-row"]')].map((row) => row.dataset.id)).toEqual(['f4', 'f1']);
    // Still one status and one count: Wrong 7.
    expect(within(node).getByRole('heading', { name: /^Wrong\s*7$/ })).toBeTruthy();
  });

  it('without impact (an older answer, or while it streams) keeps the list as it was', () => {
    const findings = [1, 2, 3, 4, 5].map((rank) => finding(`g${rank}`, { page: rank, item: `Top ${rank} width` }));
    render(<AssistantMessage message={answer(findings)} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    expect(screen.queryByRole('region', { name: 'Biggest first' })).toBeNull();
    expect(biggestFirst(findings)).toEqual([]);
  });

  it('orders the area groups by their biggest item', () => {
    const findings = [ranked('a', 4, { item: 'Pantry top width' }), ranked('b', 1, { item: 'Buffet top width' }), ranked('c', 2, { item: 'Pantry overall' })];
    expect(groupByArea(findings).map((group) => [group.name, group.findings.map((f) => f.id)])).toEqual([
      ['Buffet', ['b']],
      ['Pantry', ['c', 'a']],
    ]);
  });
});

