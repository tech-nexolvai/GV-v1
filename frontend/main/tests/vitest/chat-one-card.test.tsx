// @vitest-environment jsdom
/**
 * One item, one card (2026-10-23): the server folds the copies of one item into one card, so the
 * tiles count each item once; the drawer lists the other copies as evidence ("Also reported"), and
 * a Drawing-needed page lists the items folded under it. Made-up values only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatCoverage, ChatFinding } from '@/lib/chat-api';
import { copyLines } from '@/lib/chat-drawer';
import { drawingNeededItems, drawingNeededPages, resultGroups } from '@/lib/chat-result';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-23T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Done.' }], files: [], tasks: null, cost_usd: '1.00', stopped: null, error: null, status: 'done', ...change,
  };
}

const COVERAGE: ChatCoverage = {
  pages: [
    { page: 1, status: 'not_comparable', note: 'Not compared with the architect: …', drawing_needed: true, drawing_needed_why: 'no drawing of the same item was found', folded: [{ finding_id: 'F2', item: 'Sample unit: drawn items', vendor: 'sink model K-1' }] },
    { page: 3, status: 'different' },
  ],
  counts: { total: 2, same: 0, different: 1, not_comparable: 1, not_opened: 0 },
};

const LIKELY = finding('F33', {
  item: 'Sample unit: top depth', status: 'check', vendor: '22 1/2"', architect: '21 15/16"',
  proof: { group: 'likely_problem', likely_problem: { kind: 'size', words: 'Please check and confirm: the top depth.', reason: 'the plan and the section disagree', sure: 'likely', by: 'whole_drawing_look' } },
  same_item: { copies: [{ finding_id: 'F42', item: 'Sample unit: depth (plan)', group: 'not_compared', vendor: '12"', architect: '21 15/16"', reason: 'the two dimensions measure different things', by: 'a page helper' }] },
});
const RIGHT = finding('F10', {
  item: 'Sample unit: top width', status: 'same', vendor: '36"', architect: '36"', proof: { group: 'right' },
  same_item: { copies: [{ finding_id: 'F35', group: 'right', vendor: '36"', architect: '36"', by: 'a page helper' }] },
});

describe('one item, one card', () => {
  it('counts each card once: the copies inside a card are never counted', () => {
    const groups = resultGroups([LIKELY, RIGHT]);
    expect([groups.likely.length, groups.right.length, groups.notCompared.length]).toEqual([1, 1, 0]);
  });

  it('says the other copies in plain words, and why a card is not compared when they disagree', () => {
    expect(copyLines(LIKELY)).toEqual(['A page helper: Not compared (vendor 12″, architect 21 15/16″): the two dimensions measure different things.']);
    expect(copyLines({ same_item: { copies: [], conflict: ['21"', '36"'] } })).toEqual(['The copies pair the vendor’s number with different architect sizes (21″, 36″), so it is not compared.']);
    expect(copyLines({ same_item: null })).toEqual([]);
  });

  it('keeps the items folded under a Drawing-needed page on its card', () => {
    const pages = drawingNeededPages(COVERAGE);
    expect(pages).toEqual([{ page: 1, why: 'no drawing of the same item was found', folded: [{ finding_id: 'F2', item: 'Sample unit: drawn items', vendor: 'sink model K-1' }] }]);
    const [item] = drawingNeededItems(pages, null);
    expect(item.same_item?.copies.map((copy) => [copy.finding_id, copy.group])).toEqual([['F2', 'not_compared']]);
  });

  it('shows the copies in the drawer as "Also reported (the same item)"', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: [LIKELY, RIGHT] as ChatFinding[], coverage: COVERAGE })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('tab', { name: /Likely problem/ }));
    await user.click(within(node).getByRole('button', { name: /Top depth, page 3\. Open the evidence$/ }));
    const panel = await screen.findByRole('dialog');
    const also = within(panel).getByRole('region', { name: 'Also reported' });
    expect(also.textContent).toContain('A page helper: Not compared (vendor 12″, architect 21 15/16″)');
  });
});
