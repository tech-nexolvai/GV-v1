// @vitest-environment jsdom
/**
 * Fewer decisions (#1554): an item that could not be compared says why (#1619: never a question), an item GV Review settled
 * says how, the same item on other pages is named, and the folded "Settled automatically" group
 * lists every settled item. The resolver block arrives live as an event too. Synthetic data only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ResultNumbers } from '@/components/chat/chat-full-check';
import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatFinding, ChatResolver } from '@/lib/chat-api';
import { applyChatEvent, evidenceOf, type ViewMessage } from '@/lib/chat-messages';
import { alsoOnWords, findingLine, type FindingCounts } from '@/lib/chat-findings';
import { finding, OPUS } from './fixtures/chat';

const RESOLVER: ChatResolver = {
  before: { wrong: 1, decision: 4, right: 0 },
  after: { wrong: 2, decision: 1, right: 0 },
  settled: [
    { finding_id: 'f3', page: 5, item: 'End filler width', outcome: 'wrong', method: 'gv_limits', how: 'GV’s filler limits are 1" to 2"; the vendor draws 5" fillers.' },
    { finding_id: 'f4', page: 6, item: 'Counter width', outcome: 'merged', method: 'same_item', how: 'the same item as f2 on p.7.', into: 'f2' },
    { finding_id: 'f5', page: 8, item: 'Material tags', outcome: 'not_a_difference', method: 'other_trades_tags', how: 'the architect tags only wall tile here.' },
  ],
  kept: { architect_prints_nothing: 1 },
  reads: 0,
  cost_usd: '0.00',
};

const FINDINGS: ChatFinding[] = [
  finding('f1', { page: 4, item: 'Outlet height', architect: '50"', vendor: '51 1/2"' }),
  finding('f2', {
    page: 7,
    item: 'Counter width',
    status: 'check',
    architect: 'no width printed',
    vendor: '80"',
    why: 'nothing to compare',
    question: 'The architect prints no size for the counter width: is the vendor’s 80" right?',
    keep_reason: 'architect_prints_nothing',
    also_on: [{ finding_id: 'f4', page: 6, round: 1, vendor: '80"' }],
  }),
  finding('f3', {
    page: 5,
    item: 'End filler width',
    vendor: '5"',
    settled: { outcome: 'wrong', method: 'gv_limits', how: 'GV’s filler limits are 1" to 2"; the vendor draws 5" fillers.' },
  }),
];

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Two items differ.' }], files: [], tasks: null, cost_usd: '1.00', stopped: null, error: null, status: 'done', ...change,
  };
}

describe('fewer decisions (#1554)', () => {
  it('shows why, how an item was settled, its other pages and the folded settled group', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS, resolver: RESOLVER })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const node = screen.getByRole('article', { name: 'Answer' });
    const question = node.querySelector('[data-slot="chat-finding-question"]');
    // #1619: no question to the reviewer: why it could not be compared.
    expect(question?.textContent).toBe('Why: nothing to compare');
    expect(node.textContent).not.toContain('is the vendor’s 80" right?');
    expect(node.querySelector('[data-slot="chat-finding-settled"]')?.textContent).toBe('Settled by GV Review: GV’s filler limits are 1" to 2"; the vendor draws 5" fillers.');
    expect(node.querySelector('[data-slot="chat-finding-also"]')?.textContent).toBe('Also on p.6');
    const group = node.querySelector<HTMLElement>('[data-slot="chat-findings-settled"]')!;
    const toggle = within(group).getByRole('button', { name: /Settled automatically\s*3/ });
    expect(toggle.textContent).toContain('4 open became 1');
    expect(group.querySelectorAll('[data-slot="chat-settled-row"]')).toHaveLength(0);
    await user.click(toggle);
    const settledRows = [...group.querySelectorAll<HTMLElement>('[data-slot="chat-settled-row"]')];
    expect(settledRows.map((row) => row.dataset.outcome)).toEqual(['wrong', 'merged', 'not_a_difference']);
    expect(settledRows[2].textContent).toContain('Not a difference');
    expect(settledRows[2].textContent).toContain('the architect tags only wall tile here.');
  });

  it('opens the card with the settled line and the same item elsewhere, never a question', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS, resolver: RESOLVER })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const node = screen.getByRole('article', { name: 'Answer' });
    // A short answer opens its few cards at first; else a click opens it.
    if (!node.querySelector('[data-slot="chat-finding-card-also"]')) await user.click(within(node).getByRole('button', { name: 'Counter width' }));
    expect(node.textContent).not.toContain('is the vendor’s 80" right?');
    expect(node.querySelector('[data-slot="chat-finding-card-also"]')?.textContent).toBe('p.6 (round 1): vendor 80"');
  });

  it('reads the resolver block from a saved answer and from the live event', () => {
    expect(evidenceOf([{ type: 'resolver', resolver: RESOLVER }]).resolver).toEqual(RESOLVER);
    const live = applyChatEvent(answer({ status: 'running' }), { event: 'resolver', data: { resolver: RESOLVER } });
    expect(live.resolver).toEqual(RESOLVER);
    expect(alsoOnWords({ also_on: [{ finding_id: 'a', page: 3 }, { finding_id: 'b', page: 3 }, { finding_id: 'c', page: 9 }] })).toBe('Also on p.3, p.9');
    expect(alsoOnWords({ also_on: null })).toBe('');
  });

  it('lets the stat card labels wrap on a phone instead of cutting "Nothing to compare" short', () => {
    render(<ResultNumbers counts={{ different: 2, check: 1, same: 0, not_compared: 0, total: 3 } as FindingCounts} coverage={null} accuracy={null} onPick={() => {}} />);
    const labels = [...document.querySelectorAll<HTMLElement>('[data-slot="chat-kpi-label"]')];
    expect(labels.map((label) => label.textContent)).toEqual(['Wrong', 'Right', 'Nothing to compare']);
    for (const label of labels) {
      expect(label.querySelector('.truncate')).toBeNull();
      expect(label.className).toContain('min-h-[2.5em]');
    }
  });

  it('reads the vendor’s own string as a sum, never "needs nothing"', () => {
    const string = finding('s1', {
      page: 9, item: 'Front height string', source: 'vendor_arithmetic', architect: 'n/a', vendor: '80',
      parts: ['4', '10 1/8', '66'], printed_total: '80', parts_sum: '80.125', inches: { architect: null, vendor: '80', difference: null },
    });
    expect(findingLine(string)).toMatchObject({ vendorVerb: 'prints', verb: 'parts add up to', reference: { text: '80 1/8 in' }, difference: '−1/8 in' });
    render(<AssistantMessage message={answer({ findings: [string] })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const line = document.querySelector('[data-slot="chat-finding-line"]')?.textContent ?? '';
    expect(line).toContain('Vendor prints 80 in');
    expect(line).toContain('parts add up to 80 1/8 in');
    expect(line).not.toContain('needs');
  });

  it('shows the same counts in the headline, the cards, the sections and the settled group', () => {
    // #1619: the server's groups: the two Wrong items code proved, one Not compared.
    const placed = FINDINGS.map((f) => ({ ...f, proof: { group: f.status === 'check' ? 'not_compared' as const : 'clouded' as const, label: 'Not reviewed: not shown on the architect’s drawings', reason: 'architect gives no size or information for it' } }));
    render(<AssistantMessage message={answer({ findings: placed, resolver: RESOLVER, mode: 'full_check', products: ['countertop'] })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    const wrong = FINDINGS.filter((f) => f.status === 'different').length;
    const decide = FINDINGS.filter((f) => f.status === 'check').length;
    const headline = node.querySelector('[data-slot="chat-summary-headline"]')?.textContent ?? '';
    expect(headline).toContain(`${wrong} wrong`);
    expect(headline).toContain(`${decide} with nothing to compare`);
    const card = (kind: string) => node.querySelector(`[data-kpi="${kind}"]`)?.textContent ?? '';
    expect(card('different')).toContain(String(wrong));
    expect(card('check')).toContain(String(decide));
    expect(within(node).getByRole('heading', { name: new RegExp(`^What’s wrong\\s*${wrong}$`) })).toBeTruthy();
    expect(within(node).getByRole('button', { name: new RegExp(`^Nothing to compare\\s*${decide}`) })).toBeTruthy();
    // The settled group counts the final list too, whatever the block's own numbers say.
    const stale = { ...RESOLVER, before: { wrong: 0, decision: 99, right: 0 }, after: { wrong: 0, decision: 42, right: 0 } };
    render(<AssistantMessage message={answer({ key: 'b', id: 'msg-2', findings: FINDINGS, resolver: stale })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const groups = [...document.querySelectorAll('[data-slot="chat-findings-settled"] button')].map((b) => b.textContent ?? '');
    for (const text of groups) expect(text).toContain(`${decide + RESOLVER.settled.length} open became ${decide}`);
  });
});
