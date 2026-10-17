// @vitest-environment jsdom
/**
 * #1512: where an answer's time and money went, under "How this was checked": one row per phase
 * code timed (seconds and cost), the whole answer, then one row per helper. Synthetic data only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatTrust } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { helperTimingRows, secondsWords, timingRows } from '@/lib/chat-trust';
import { finding, OPUS } from './fixtures/chat';

const TIMINGS: NonNullable<ChatTrust['timings']> = {
  total_seconds: 372.4,
  cost_usd: '4.12',
  phases: [
    { phase: 'set_pack', label: 'Waiting for the set pack’s page map', seconds: 0.2, cost_usd: '0.00', calls: 0 },
    { phase: 'helpers', label: 'Helpers', seconds: 201.6, cost_usd: '2.80', calls: 93 },
    { phase: 'lead_verification', label: 'Lead checks and notes', seconds: 96, cost_usd: '0.95', calls: 4 },
    { phase: 'critic', label: 'Critic', seconds: 31.3, cost_usd: '0.21', calls: 4 },
  ],
  helpers: [
    { number: 1, description: 'Pages 3-7 · 1. Area one', pages: [3, 4, 5, 6, 7], calls: 9, seconds: 188, cost_usd: '0.31', stopped: null },
    { number: 2, description: 'Pages 8-12 · 2. Area two', pages: [8, 9, 10, 11, 12], calls: 12, seconds: 201.6, cost_usd: '0.35', stopped: 'turn_limit' },
  ],
};

function answer(trust: ChatTrust): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Two items differ.' }], files: [], tasks: null, cost_usd: '4.12', stopped: null, error: null, status: 'done',
    findings: [finding('f1', { trust_level: 'likely' })], trust,
  };
}

describe('timings in words', () => {
  it('says seconds and minutes plainly', () => {
    expect(secondsWords(0.44)).toBe('0.4 s');
    expect(secondsWords(38.2)).toBe('38 s');
    expect(secondsWords(252)).toBe('4 min 12 s');
    expect(secondsWords(120)).toBe('2 min');
  });

  it('lists each phase, then the whole answer; each helper with why it stopped', () => {
    const rows = timingRows(TIMINGS)!;
    expect(rows.map((row) => [row.label, row.time, row.cost, row.calls])).toEqual([
      ['Waiting for the set pack’s page map', '0.2 s', '$0.00', 0],
      ['Helpers', '3 min 22 s', '$2.80', 93],
      ['Lead checks and notes', '1 min 36 s', '$0.95', 4],
      ['Critic', '31 s', '$0.21', 4],
      ['Whole answer', '6 min 12 s', '$4.12', null],
    ]);
    expect(helperTimingRows(TIMINGS).map((row) => row.label)).toEqual([
      'Helper 1: Pages 3-7 · 1. Area one',
      'Helper 2: Pages 8-12 · 2. Area two (stopped: turn limit)',
    ]);
    expect(timingRows(null)).toBeNull();
  });
});

describe('the table under "How this was checked"', () => {
  it('shows the phases and the helpers once opened, and nothing on an older answer', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ timings: TIMINGS })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));

    const phases = within(node).getByRole('table', { name: 'Time and cost by phase' });
    const rows = within(phases).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.textContent)).toEqual([
      'Waiting for the set pack’s page map0.2 s$0.000',
      'Helpers3 min 22 s$2.8093',
      'Lead checks and notes1 min 36 s$0.954',
      'Critic31 s$0.214',
      'Whole answer6 min 12 s$4.12',
    ]);
    const helpers = within(node).getByRole('table', { name: 'Time and cost by helper' });
    expect(within(helpers).getAllByRole('row')).toHaveLength(3);
    expect(within(helpers).getByText('Helper 2: Pages 8-12 · 2. Area two (stopped: turn limit)')).toBeTruthy();
  });

  it('is left out when the answer has no timings', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({})} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    expect(node.querySelector('[data-slot="chat-trust-timings"]')).toBeNull();
  });
});
