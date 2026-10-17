// @vitest-environment jsdom
/**
 * Follow-up corrections on the screen (#1548): a later answer corrects an earlier answer's finding.
 * The earlier answer shows the newest version, marked "Updated in a later message" (or loses a
 * dropped one); the correcting answer lists what it corrected. Synthetic data only.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatFindingUpdate, ChatMessage } from '@/lib/chat-api';
import { applyChatEvent, applyCorrection, correctionsOf, fromSaved, type ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Checked.' }], files: [], tasks: null, cost_usd: '1.20', stopped: null, error: null, status: 'done', ...change,
  };
}

const F1 = finding('F1', { item: 'Outlet height', vendor: '46"', architect: '3\'-1"', page: 4 });
const F2 = finding('F2', { item: 'Sink base', vendor: '30"', architect: '33"', page: 5 });

const UPDATE: ChatFindingUpdate = {
  ref: 'A1-F1', message_id: 'msg-1', finding_id: 'F1', answer: 1, by_answer: 2, version: 2, finding: { ...F1, vendor: '45"' }, dropped: null,
};
const DROP: ChatFindingUpdate = { ...UPDATE, ref: 'A1-F2', finding_id: 'F2', finding: F2, dropped: 'It is the blocking' };

describe('a correction of an earlier answer', () => {
  it('replaces the earlier finding with its newest version, or removes a dropped one', () => {
    const earlier = answer({ findings: [F1, F2] });
    const other = answer({ key: 'b', id: 'msg-9', findings: [F1] });
    const [changed, untouched] = applyCorrection([earlier, other], UPDATE, 'msg-2');
    expect(changed.findings?.[0]).toMatchObject({ id: 'F1', vendor: '45"', updated_in: { message_id: 'msg-2', answer: 2, version: 2 } });
    expect(untouched).toBe(other);
    const [dropped] = applyCorrection([changed], DROP);
    expect(dropped.findings?.map((f) => f.id)).toEqual(['F1']);
  });

  it('keeps the newest correction per ref, live and saved', () => {
    const live = applyChatEvent(answer({ id: 'msg-2' }), { event: 'finding_update', data: { index: 3, update: UPDATE } });
    const again = applyChatEvent(live, { event: 'finding_update', data: { index: 3, update: { ...UPDATE, finding: { ...F1, vendor: '44"' } } } });
    expect(again.corrections?.map((u) => u.finding.vendor)).toEqual(['44"']);
    expect(correctionsOf([{ type: 'finding_update', update: UPDATE }, { type: 'finding_update', update: DROP }]).map((u) => u.ref)).toEqual(['A1-F1', 'A1-F2']);
    const saved = fromSaved({ id: 'msg-2', role: 'assistant', created_at: '2026-10-17T11:00:00Z', text: 'Fixed.', blocks: [{ type: 'finding_update', update: DROP }] } as ChatMessage);
    expect(saved.corrections).toHaveLength(1);
  });

  it('shows "Updated in a later message" and the correcting answer lists its corrections', () => {
    render(<AssistantMessage message={answer({ findings: [{ ...F1, updated_in: { message_id: 'msg-2', answer: 2, version: 2 } }] })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    expect(screen.getByText('Updated in a later message')).toBeTruthy();
  });

  it('lists the corrections in the later answer', () => {
    render(<AssistantMessage message={answer({ id: 'msg-2', corrections: [UPDATE, DROP] })} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
    const list = document.querySelector('[data-slot="chat-corrections"]');
    expect(list?.textContent).toContain('Corrections to earlier answers');
    expect(list?.textContent).toContain('Outlet height: vendor 45", architect 3\'-1"');
    expect(list?.textContent).toContain('dropped (It is the blocking)');
  });
});
