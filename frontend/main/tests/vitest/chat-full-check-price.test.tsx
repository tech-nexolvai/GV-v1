// @vitest-environment jsdom
/**
 * #1823: the full check's price before it runs. It grows with the set: a base plus a price per
 * content page of the products picked, at least a minimum, never above the set's budget left; it
 * is the full check's own cap, so the per-answer cap is not shown for it. Made-up numbers only.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { FullCheckButton } from '@/components/chat/chat-full-check';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatFullCheckPlan } from '@/lib/chat-api';
import { fullCheckPrice, fullCheckPriceWords, priceRuleShort, priceRuleWords } from '@/lib/chat-full-check';
import { ChatPage } from '@/pages/ChatPage';
import { fakeChatApi, modelsOut } from './fixtures/chat';

beforeEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    // no storage in this environment
  }
});

const RULE = { full_check_base_usd: '2.00', full_check_per_page_usd: '0.35', full_check_min_usd: '5.00' };
const PRICED: ChatFullCheckPlan = { state: 'ready', summary: '44 pages · 18 pairs · round 1', doubts: [], more: 0, price_usd: '16.00', price_pages: 40 };

describe('the price words', () => {
  it('says the rule the server states, and nothing when it does not', () => {
    expect(priceRuleWords(RULE)).toBe('$2 + $0.35 a content page, at least $5');
    expect(priceRuleShort(RULE)).toBe('$2 + $0.35/page');
    expect(priceRuleWords({})).toBeNull();
    expect(priceRuleShort(null)).toBeNull();
  });

  it('says the price once code has counted the pages, else the rule', () => {
    const rule = priceRuleWords(RULE);
    expect(fullCheckPriceWords(PRICED, rule)).toBe('Fixed price $16 · 40 content pages');
    expect(fullCheckPriceWords({ price_usd: '12.85', price_pages: 1 }, rule)).toBe('Fixed price $12.85 · 1 content page');
    expect(fullCheckPriceWords({ price_usd: null, price_pages: null }, rule)).toBe('Fixed price: $2 + $0.35 a content page, at least $5');
    expect(fullCheckPriceWords(null, null)).toBeNull();
    expect(fullCheckPrice(PRICED, rule)).toEqual({ price: '$16', detail: '40 content pages' });
  });
});

describe('the price on screen', () => {
  it('the Run popover says the rule, then the price for the pages counted', async () => {
    const user = userEvent.setup();
    let answer: (plan: ChatFullCheckPlan) => void = () => {};
    const loadPlan = vi.fn(() => new Promise<ChatFullCheckPlan>((done) => { answer = done; }));
    render(<FullCheckButton summary="Full check · Countertops · Opus 5.5 · High" onRun={vi.fn()} blocked={null} loadPlan={loadPlan} priceRule={priceRuleWords(RULE)} />);

    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    const line = () => document.querySelector('[data-slot="chat-full-check-price"]');
    expect(line()?.textContent).toBe('Fixed price:$2 + $0.35 a content page, at least $5');
    answer(PRICED);
    await waitFor(() => expect(line()?.textContent).toBe('Fixed price$16· 40 content pages'));
    // The amount is a number: the numbers' face.
    expect(line()?.querySelector('.font-mono')?.textContent).toBe('$16');
    expect(screen.getByText('Full check · Countertops · Opus 5.5 · High')).toBeTruthy();
  });

  it('the chat page asks the plan for the products picked and shows no per-answer cap for the full check', async () => {
    const user = userEvent.setup();
    const plan = vi.fn(async () => PRICED);
    const api: ChatApi = fakeChatApi({
      models: vi.fn(async () => modelsOut({ message_cap_usd: '15.00', ...RULE })) as never,
      list: vi.fn(async () => ({ chats: [], package_name: 'Sample' })) as never,
      plan: plan as never,
    });
    const chats = document.body.appendChild(document.createElement('div'));
    render(
      <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
        <ChatPage packageId="pkg-1" chatId={null} api={api} onOpenChat={vi.fn()} stopGraceMs={0} />
      </ShellSlotsContext.Provider>,
    );
    const run = await screen.findByRole('button', { name: 'Run full check' });
    await waitFor(() => expect((run as HTMLButtonElement).disabled).toBe(false));
    await user.click(run);

    await waitFor(() => expect(plan).toHaveBeenCalledWith('pkg-1', ['countertop', 'cabinet']));
    await waitFor(() => expect(document.querySelector('[data-slot="chat-full-check-price"]')?.textContent).toBe('Fixed price$16· 40 content pages'));
    const summary = document.querySelector('[data-slot="chat-full-check-summary"]');
    expect(summary?.textContent).not.toContain('$15');
    expect(summary?.textContent).not.toContain('up to');
  });
});
