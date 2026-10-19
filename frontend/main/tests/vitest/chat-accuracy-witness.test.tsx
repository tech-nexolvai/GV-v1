// @vitest-environment jsdom
/**
 * Chat accuracy rules on the screen: evidence-kind labels and tooltips, "Not compared" kept apart
 * from "What's right" (not seen is not right), the unknowns listed under the trust card, the
 * `unknowns` block and event, and the accuracy popover's calibration line. Synthetic data only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatFinding, ChatUnknown } from '@/lib/chat-api';
import { excludedWords } from '@/lib/chat-accuracy';
import { findingCounts, groupFindings } from '@/lib/chat-findings';
import { applyChatEvent, fromSaved, localAnswer, type ViewMessage } from '@/lib/chat-messages';
import { evidenceWords, findingsCsv, levelCounts, unknownsWords } from '@/lib/chat-trust';
import { finding, OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Checked.' }], files: [], tasks: null, cost_usd: '1.00', stopped: null, error: null, status: 'done', ...change,
  };
}

const FINDINGS: ChatFinding[] = [
  finding('o1', { item: 'Sink base width', trust_level: 'confirmed', evidence_kind: 'observed', witnesses: ['printed'], checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees' } }),
  finding('i1', { item: 'Filler', status: 'check', trust_level: 'likely', evidence_kind: 'inferred', witnesses: [], checks: { second_read: 'agrees', lead_checked: true } }),
  finding('r1', { item: 'Vanity top depth', status: 'same', trust_level: 'likely', evidence_kind: 'derived', witnesses: ['rule'], compared: true }),
  finding('n1', { item: 'Sink model', status: 'same', trust_level: 'likely', evidence_kind: 'inferred', compared: false, not_compared_why: 'not_viewed' }),
];

const UNKNOWNS: ChatUnknown[] = [
  { id: 'U1', page: 4, what: 'Page 4', why: 'not_opened', needs: 'open the page and compare it with its architect page', scope: 'page' },
  { id: 'U2', page: 6, what: 'F3 Fridge opening', why: 'unreadable', needs: 'a person reads the value on the drawing', scope: 'item' },
  { id: 'U3', page: 8, what: 'sink front offset', why: 'missing_job_value', needs: 'the job value from GV', scope: 'item' },
];

describe('evidence kinds', () => {
  it('says what backs each finding, in words, and on screen as one How sure line per card', () => {
    expect(evidenceWords(FINDINGS[0])).toBe('Printed on the drawing: both values printed at their spots');
    expect(evidenceWords(FINDINGS[1])).toBe('AI reading only');
    expect(evidenceWords(FINDINGS[2])).toBe('Worked out by code: GV’s rule check agrees');
    expect(evidenceWords(finding('old'))).toBeNull();

    // #1522: on screen it is one plain "How sure" line in each item's card (no chip, no tooltip).
    render(<AssistantMessage message={answer({ findings: FINDINGS })} chatId="c-1" models={[OPUS]} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    // A narrow answer (one page): the cards of its few items are open at first.
    const sure = [...node.querySelectorAll<HTMLElement>('[data-slot="chat-finding-sure"]')].map((line) => line.textContent);
    expect(sure[0]).toBe('How sure: Confirmed. Code found the values printed on the drawing (or worked them out), and an AI reading agrees.');
    expect(sure[1]).toBe('How sure: Likely. Two separate AI readings agree; no code check could read it.');
    expect(node.querySelector('[data-slot="chat-trust-level"]')).toBeNull();
  });

  it('adds an Evidence column to the CSV', () => {
    const [header, first] = findingsCsv(FINDINGS).replace('﻿', '').split('\r\n');
    expect(header.split(',')).toContain('Evidence');
    expect(first).toContain('printed on the drawing');
  });
});

describe('not seen is not right', () => {
  it('counts a "same" item nobody compared apart, never as right', () => {
    expect(findingCounts(FINDINGS)).toEqual({ different: 1, check: 1, same: 1, not_compared: 1, total: 4 });
    const ids = (status: 'same' | 'not_compared') => groupFindings(FINDINGS, { status, category: null, page: null }).flatMap((group) => group.findings.map((item) => item.id));
    expect(ids('same')).toEqual(['r1']);
    expect(ids('not_compared')).toEqual(['n1']);
    // Trust levels counted from the findings leave it out too.
    expect(levelCounts(FINDINGS, null)).toEqual({ confirmed: 1, likely: 2, to_check: 0 });
  });

  it('shows what could not be compared on its own tab, grouped by why (#1619, #1717)', async () => {
    const user = userEvent.setup();
    const placed = FINDINGS.map((each) => ({
      ...each,
      proof: each.id === 'o1' ? { group: 'clouded' as const }
        : each.id === 'r1' ? { group: 'right' as const }
          : { group: 'not_compared' as const, not_compared: each.id === 'n1' ? 'not_seen' : 'not_proved', label: each.id === 'n1' ? 'Not reviewed: not looked at on both drawings' : 'Not reviewed: not proved by code', reason: 'why it could not be compared' },
    }));
    render(<AssistantMessage message={answer({ findings: placed })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect([...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-tiles"] [role="tab"]')].map((tile) => tile.dataset.tab)).toEqual(['wrong', 'right', 'not_compared']);
    const rows = () => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-row"]')].map((row) => row.dataset.id);
    expect(rows()).toEqual(['o1']);
    await user.click(within(node).getByRole('tab', { name: /^Not compared/ }));
    expect(rows()).toEqual(['i1', 'n1']);
    // Each group says why, in the Not-compared list's words.
    expect([...node.querySelectorAll('[data-slot="chat-result-group"]')].map((head) => head.textContent)).toEqual(['Difference not proved by code1', 'Not looked at on both drawings1']);
  });

  it('lists a not-looked-at item under Not compared: no outcome word of its own (#1712)', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<AssistantMessage message={answer({ findings: FINDINGS.slice(0, 3) })} chatId="c-1" models={[OPUS]} fullCheck />);
    const tabs = () => [...document.querySelectorAll<HTMLElement>('[data-slot="chat-result-tiles"] [role="tab"]')].map((tile) => tile.textContent?.replace(/[A-Z][^0-9]*$/, ''));
    expect(tabs()).toEqual(['Wrong0', 'Right1', 'Not compared2']);
    unmount();
    render(<AssistantMessage message={answer({ findings: FINDINGS })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(tabs()).toEqual(['Wrong0', 'Right1', 'Not compared3']);
    await user.click(within(node).getByRole('tab', { name: /^Not compared/ }));
    expect(node.querySelector('[data-slot="chat-result-row"][data-id="n1"]')).not.toBeNull();
  });
});

describe('unknowns', () => {
  it('lists what was not checked in “How this was checked”: why and what is needed', async () => {
    const user = userEvent.setup();
    expect(unknownsWords(UNKNOWNS)).toBe('3 not checked');
    expect(unknownsWords([])).toBeNull();
    render(<AssistantMessage message={answer({ findings: FINDINGS, scope: { kind: 'whole_set' }, unknowns: UNKNOWNS, trust: { evidence: { observed: 1, derived: 1, inferred: 1 }, not_compared: 1 } })} chatId="c-1" models={[OPUS]} />);
    const summary = screen.getByRole('region', { name: 'How this was checked' });
    await user.click(within(summary).getByRole('button', { name: 'How this was checked' }));
    expect(summary.querySelector('[data-slot="chat-trust-evidence"]')?.textContent).toBe('1 printed on the drawing · 1 worked out by code · 1 AI reading only · 1 not compared');
    await user.click(within(summary).getByRole('button', { name: /3 not checked/ }));
    const items = [...summary.querySelectorAll<HTMLElement>('[data-slot="chat-unknowns"] li')];
    expect(items.map((item) => item.dataset.why)).toEqual(['not_opened', 'unreadable', 'missing_job_value']);
    expect(items[1].textContent).toContain('unreadable (pen strokes)');
    expect(items[2].textContent).toContain('needs: the job value from GV');
  });

  it('come from the unknowns block on reload and from the unknowns event live', () => {
    const saved = fromSaved({
      id: 'm', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'x', model: null, effort: null, thinking: null, cost_usd: null, stopped: null, error: null,
      blocks: [{ type: 'text', text: 'x' }, { type: 'unknowns', unknowns: UNKNOWNS }],
    } as never);
    expect(saved.unknowns).toEqual(UNKNOWNS);
    const live = applyChatEvent(localAnswer('k', { model: OPUS.id, effort: 'high', thinking: true }), { event: 'unknowns', data: { unknowns: UNKNOWNS.slice(0, 1) } });
    expect(live.unknowns).toEqual(UNKNOWNS.slice(0, 1));
  });
});

describe('the accuracy popover', () => {
  it('says how the points were calibrated and what was left out of the score', async () => {
    const user = userEvent.setup();
    expect(excludedWords({ not_compared: 2, unknowns: 3 })).toBe('2 “Right” items not compared (not counted) · 3 not checked (they lower the score)');
    expect(excludedWords({})).toBeNull();
    render(
      <AssistantMessage
        message={answer({ accuracy: { score: 91, level: 'green', claims: 2, breakdown: [], calibration: 'On GV’s test sets, 24 of 24 checkable findings were right.', not_compared: 1, unknowns: 0 } })}
        chatId="c-1"
        models={[OPUS]}
      />,
    );
    await user.click(screen.getByRole('button', { name: /Accuracy 91%/ }));
    expect(document.querySelector('[data-slot="chat-accuracy-calibration"]')?.textContent).toContain('24 of 24');
    expect(document.querySelector('[data-slot="chat-accuracy-excluded"]')?.textContent).toBe('1 “Right” item not compared (not counted)');
  });
});
