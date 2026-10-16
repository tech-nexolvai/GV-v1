// @vitest-environment jsdom
/**
 * #1498: the third read and the critic in the screen's words: the evidence viewer's check lines,
 * the trust reason, and the words code builds from `reads` / `checks.adjudication` / `critic`.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatFinding } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { allChecks, criticCheck, findingChecks, readsWords, trustReason } from '@/lib/chat-trust';
import { finding, OPUS } from './fixtures/chat';

function answer(findings: ChatFinding[]): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'One item differs.' }], files: [], tasks: null, cost_usd: '1.72', stopped: null, error: null, status: 'done', findings,
  };
}

const READS = [
  { role: 'first' as const, model: 'lead', value_arch: null, value_vendor: '46"', sure: null },
  { role: 'second' as const, model: 'haiku', value_arch: null, value_vendor: '40"', sure: true },
  { role: 'third' as const, model: 'sonnet', value_arch: null, value_vendor: '46"', sure: true, dpi: 300, turned: false },
];

const settled = finding('a1', {
  item: 'Sink base width', architect: '3\'-1"', vendor: '46"', trust_level: 'likely',
  checks: { printed_vendor: 'no_text', second_read: 'disagrees', adjudication: 'agrees_after_adjudication', lead_checked: true },
  reads: READS,
});

const objected = finding('c1', {
  item: 'Upper outlet height', architect: '72"', vendor: '73 1/2"', trust_level: 'to_check',
  checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees', critic: 'objects' },
  critic: { result: 'objects', note: 'The vendor value is GV\'s red note.', model: 'sonnet' },
});

describe('the third read in words', () => {
  it('names each read and the majority', () => {
    expect(readsWords(settled)).toBe('Second read: 40" · Third read (Sonnet, 300 dpi): 46" → agrees');
    const [, read] = findingChecks(settled);
    expect(read.state).toBe('pass');
    expect(read.words).toBe('Second read: 40" · Third read (Sonnet, 300 dpi): 46" → agrees');
  });

  it('shows a turned copy, both sides, a read that was not sure, and a tie that leaves the disagreement', () => {
    const tie = finding('t1', {
      checks: { second_read: 'disagrees', adjudication: 'unsure' },
      reads: [
        { role: 'second', model: 'haiku', value_arch: '37"', value_vendor: '40"', sure: true },
        { role: 'third', model: 'sonnet', value_arch: null, value_vendor: null, sure: false, dpi: 240, turned: true },
      ],
    });
    expect(readsWords(tie)).toBe('Second read: 37" / 40" · Third read (Sonnet, 240 dpi, turned): nothing read (not sure) → unsure');
    expect(findingChecks(tie)[1].state).toBe('fail');
    const against = finding('d1', { checks: { second_read: 'unsure', adjudication: 'disagrees' }, reads: READS });
    expect(findingChecks(against)[1].state).toBe('fail');
    expect(readsWords(finding('n1', { checks: { second_read: 'agrees' } }))).toBeNull();
  });
});

describe('the critic in words', () => {
  it('objects with its note, passes, or is left out when it did not look', () => {
    expect(criticCheck(objected)).toMatchObject({ key: 'critic', state: 'fail', words: 'Critic: The vendor value is GV\'s red note.' });
    expect(trustReason(objected)).toBe('Critic: The vendor value is GV\'s red note.');
    const passed = finding('p1', { checks: { critic: 'passed' } });
    expect(criticCheck(passed)?.state).toBe('pass');
    expect(criticCheck(finding('n1', { checks: { critic: 'not_run' } }))?.words).toBe('Critic: not run (limit or cap)');
    expect(criticCheck(finding('x1', { checks: { second_read: 'agrees' } }))).toBeNull();
    // The table's four check icons stay four; the critic is one more line in the viewer.
    expect(findingChecks(objected)).toHaveLength(4);
    expect(allChecks(objected).map((check) => check.key)).toEqual(['printed', 'second_read', 'verified', 'rule', 'critic']);
  });
});

describe('the evidence viewer', () => {
  it('shows the reads and the critic under the crops', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer([settled, objected])} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const node = screen.getByRole('article', { name: 'Answer' });

    await user.click(within(node).getByRole('button', { name: 'Sink base width' }));
    let viewer = await screen.findByRole('dialog');
    expect(within(viewer).getByText('Second read: 40" · Third read (Sonnet, 300 dpi): 46" → agrees')).toBeTruthy();
    expect(viewer.querySelector('[data-check="critic"]')).toBeNull();

    await user.keyboard('{ArrowRight}');
    viewer = screen.getByRole('dialog');
    const critic = viewer.querySelector<HTMLElement>('[data-slot="chat-evidence-checks"] [data-check="critic"]');
    expect(critic?.dataset.state).toBe('fail');
    expect(critic?.textContent).toContain('Critic: The vendor value is GV\'s red note.');
  });
});
