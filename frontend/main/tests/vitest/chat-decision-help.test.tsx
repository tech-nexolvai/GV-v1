// @vitest-environment jsdom
/**
 * #1545: a "Your decision" item's reasoning on its card, under the why line: What it measures /
 * Our best estimate / Likely / What to confirm, in plain words. Synthetic data only.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatFinding } from '@/lib/chat-api';
import { decisionHelpRows } from '@/lib/chat-findings';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'One item needs your decision.' }], files: [], tasks: null, cost_usd: '1.00', stopped: null, error: null, status: 'done', ...change,
  };
}

const HELPED: ChatFinding = finding('d1', {
  item: 'Run length', status: 'check', architect: 'not dimensioned', vendor: '191"', why: 'The architect prints no overall length.',
  decision_help: {
    measures: 'From the wall to the far end of the run, fillers included',
    estimate: { value_in: 185.53, plus_minus_in: 1, method: 'outlet string added up, and scaled against two printed dims' },
    likely: 'probably fine',
    confirm: 'Confirm the fillers are scribed on site',
  },
});

describe('decision help', () => {
  it('turns the help into four plain lines, the estimate to the nearest 1/16 in', () => {
    expect(decisionHelpRows(HELPED)).toEqual([
      { label: 'What it measures', text: 'From the wall to the far end of the run, fillers included' },
      { label: 'Our best estimate', text: 'About 185 1/2 in (± 1 in): outlet string added up, and scaled against two printed dims' },
      { label: 'Likely', text: 'Probably fine' },
      { label: 'What to confirm', text: 'Confirm the fillers are scribed on site' },
    ]);
    expect(decisionHelpRows({ decision_help: { ...HELPED.decision_help!, estimate: null } })[1]).toEqual({ label: 'Our best estimate', text: 'None: nothing to measure it against.' });
    expect(decisionHelpRows(finding('plain'))).toEqual([]);
  });

  it('shows it on the card right under the why line, and nothing for an item without it', () => {
    render(<AssistantMessage message={answer({ findings: [HELPED, finding('w1', { item: 'Sink base width' })] })} chatId="c-1" models={[OPUS]} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    const card = node.querySelector<HTMLElement>('[data-slot="chat-finding-detail"][data-id="d1"]')!;
    const help = card.querySelector<HTMLElement>('[data-slot="chat-finding-decision-help"]')!;
    expect([...help.querySelectorAll('dt')].map((term) => term.textContent)).toEqual(['What it measures', 'Our best estimate', 'Likely', 'What to confirm']);
    expect(help.textContent).toContain('About 185 1/2 in (± 1 in)');
    // Right after the why line.
    expect(card.querySelector('[data-slot="chat-finding-why"]')?.parentElement?.nextElementSibling).toBe(help);
    const wrong = node.querySelector<HTMLElement>('[data-slot="chat-finding-detail"][data-id="w1"]');
    expect(wrong?.querySelector('[data-slot="chat-finding-decision-help"]') ?? null).toBeNull();
  });
});
