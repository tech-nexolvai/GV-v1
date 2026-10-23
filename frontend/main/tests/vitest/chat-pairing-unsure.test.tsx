// @vitest-environment jsdom
/**
 * "Pairing unsure" (2026-10-23): a vendor page the AI page matcher paired and code could not confirm
 * has its architect drawing in the upload, so it is never "Drawing needed". It is listed under Not
 * compared, naming both candidate pages as a person finds them, and nothing is decided on it.
 * Made-up values only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatCoverage } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { drawingNeededPages, notComparedGroups, pairingUnsureItems } from '@/lib/chat-result';
import { finding, OPUS } from './fixtures/chat';

const PAGES = 'architect page 9 ("SAMPLE UNIT A") or architect page 2?';

const COVERAGE: ChatCoverage = {
  pages: [
    { page: 1, status: 'same' },
    { page: 2, status: 'not_comparable', note: `Not compared with the architect: pairing unsure (${PAGES})`, pairing_unsure: true, pairing_unsure_why: PAGES },
    { page: 3, status: 'not_comparable', note: 'Not compared with the architect: …', drawing_needed: true, drawing_needed_why: 'no drawing of the same item was found in the architect’s file' },
  ],
};

const TITLES = new Map([[2, { page: 2, title: 'Sample unit A', project: null, sheet: null }]]);

function answer(): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-23T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Done.' }], files: [], tasks: null, cost_usd: '1.00', stopped: null, error: null, status: 'done',
    findings: [finding('r1', { page: 1, item: 'Sample unit B: depth', status: 'same', architect: '22"', vendor: '22"', proof: { group: 'right' } })],
    coverage: COVERAGE,
  };
}

const tiles = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-tiles"] [role="tab"]')].map((tile) => `${tile.dataset.tab}:${tile.querySelector('[data-slot="chat-result-count"]')?.textContent}`);

describe('pairing unsure', () => {
  it('is a Not-compared item naming both pages, never a Drawing-needed page', () => {
    expect(drawingNeededPages(COVERAGE).map((page) => page.page)).toEqual([3]);
    const items = pairingUnsureItems(COVERAGE, TITLES);
    expect(items.map((item) => [item.id, item.page, item.item])).toEqual([['pairing-unsure:2', 2, `Sample unit A: Pairing unsure: ${PAGES}`]]);
    expect(notComparedGroups(items).map((group) => [group.reason, group.words])).toEqual([['pairing_unsure', 'Pairing unsure: pick the architect page']]);
    expect(items[0].item).not.toMatch(/\bA\d{1,3}\b|\bp\d{3,5}\b|no architect drawing/);
  });

  it('shows on the screen under Not compared', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer()} chatId="c-1" models={[OPUS]} fullCheck pageTitles={TITLES} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(tiles(node)).toEqual(['wrong:0', 'likely_problem:0', 'rule_note:0', 'drawing_needed:1', 'right:1', 'not_compared:1']);
    await user.click(within(node).getByRole('tab', { name: /Not compared/ }));
    const row = node.querySelector<HTMLElement>('[data-id="pairing-unsure:2"]');
    expect(row?.textContent).toContain('Pairing unsure');
    expect(row?.textContent).toContain('architect page 9');
  });
});
