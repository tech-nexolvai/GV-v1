// @vitest-environment jsdom
/**
 * Fold and order the cards (final plan change #8): one card per question across the sibling pages
 * of a unit, the copies one click away, please-check questions grouped by an identical reason, the
 * "Nothing to compare here" list collapsed at the bottom with its count, and one Agree for every
 * copy of the same proven difference. Made-up values only.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatFinding, FindingCard } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { cardPillWords, copiesWords, likelyGroups, notComparedGroups, pagesWords, resultCards, resultGroups } from '@/lib/chat-result';
import { finding, OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-26T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Done.' }], files: [], tasks: null, cost_usd: '4.10', stopped: null, error: null, status: 'done', ...change,
  };
}

/** A card as the server sends it: the lead with its copies, each copy pointing at the lead. */
function card(members: ChatFinding[], extra: Partial<FindingCard> = {}): ChatFinding[] {
  const [lead, ...copies] = members;
  const pages = [...new Set(members.map((each) => each.page))].sort((a, b) => a - b);
  const base = { id: lead.id, pages, how: 'same_reference_dimension' as const, ...extra };
  return [
    { ...lead, card: { ...base, lead: true, copies: copies.map((copy) => ({ finding_id: copy.id, page: copy.page, ends: copy.card?.ends ?? null })) } },
    ...copies.map((copy) => ({ ...copy, card: { ...base, lead: false, copies: [], ends: copy.card?.ends ?? null } })),
  ];
}

const RED = { status: 'different' as const, architect: '58"', vendor: '58 3/4"', inches: { architect: '58', vendor: '58.75', difference: '0.75' }, proof: { group: 'clouded' as const, should_be: '58" (architect)' } };
const RIGHT = { status: 'same' as const, architect: '30"', vendor: '30"', proof: { group: 'right' as const } };

const FINDINGS: ChatFinding[] = [
  // One proven difference on the regular and the reversed sheet of one unit (pages 3 and 4).
  ...card([finding('w1', { ...RED, page: 3, item: 'Sample kitchen: top length' }), finding('w2', { ...RED, page: 4, item: 'Sample kitchen reversed: top length' })], { agree_all: true, related: [{ finding_id: 'x1', page: 4, group: 'not_compared' }] }),
  // The same two sizes on the reversed sheet, Not compared there: named on the red card, never joined.
  finding('x1', { ...RED, page: 4, item: 'Sample kitchen reversed: long piece', proof: { group: 'not_compared', not_compared: 'different_reference' } }),
  // One Right on both sheets, the reversed copy's ends not proved by code.
  ...card([finding('r1', { ...RIGHT, page: 3, item: 'Sample kitchen: top depth', card: { id: 'r1', lead: true, ends: 'proved' } }), finding('r2', { ...RIGHT, page: 4, item: 'Sample kitchen reversed: top depth', card: { id: 'r1', lead: false, ends: 'not_proved' } })]),
  // A Right alone, ends not proved: a Right with the small note.
  finding('r3', { ...RIGHT, page: 5, item: 'Sample vanity: top width', card: { id: 'r3', lead: true, ends: 'not_proved' } }),
  // Two please-check questions with the same reason, one with another.
  finding('l1', { page: 3, item: 'Sample kitchen: depth', status: 'different', proof: { group: 'likely_problem' }, card: { id: 'l1', lead: true, question: { key: 'ends', words: 'The AI pairs these sizes, but code could not confirm both measure the same thing' } }, impact: { score: 50, rank: 2, why: '' } }),
  finding('l2', { page: 6, item: 'Sample bar: width', status: 'different', proof: { group: 'likely_problem' }, card: { id: 'l2', lead: true, question: { key: 'look:missing_piece', words: 'The reference shows a piece the vendor\'s drawing does not' } }, impact: { score: 90, rank: 1, why: '' } }),
  finding('l3', { page: 7, item: 'Sample bar: depth', status: 'different', proof: { group: 'likely_problem' }, card: { id: 'l3', lead: true, question: { key: 'ends', words: 'The AI pairs these sizes, but code could not confirm both measure the same thing' } }, impact: { score: 40, rank: 3, why: '' } }),
  // Questions: a missing job value (on both sheets), a value that could not be read.
  ...card(
    [
      finding('m1', { page: 3, item: 'Sample kitchen: top depth at sink', status: 'check', proof: { group: 'not_compared', not_compared: 'missing_setting' } }),
      finding('m2', { page: 4, item: 'Sample kitchen reversed: top depth at sink', status: 'check', proof: { group: 'not_compared', not_compared: 'missing_setting' } }),
    ],
    { question: { key: 'missing_setting:front overhang', words: 'Job value missing: front overhang' } },
  ),
  finding('u1', { page: 5, item: 'Sample vanity: filler', status: 'check', proof: { group: 'not_compared', not_compared: 'unreadable' } }),
  // Nothing to compare: the reference gives no size; a different extent.
  finding('n1', { page: 5, item: 'Sample vanity: splash height', status: 'check', architect: 'no splash shown', proof: { group: 'not_compared', not_compared: 'nothing_to_compare' } }),
  finding('n2', { page: 6, item: 'Sample bar: overall', status: 'check', proof: { group: 'not_compared', not_compared: 'different_reference' } }),
];

describe('one card per question, without React', () => {
  it('joins the server’s card and splits a copy the reviewer decided apart', () => {
    const cards = resultCards(FINDINGS);
    const red = cards.find((each) => each.id === 'w1')!;
    expect(red.copies.map((each) => each.id)).toEqual(['w2']);
    expect(red.pages).toEqual([3, 4]);
    expect(red.agreeAll).toBe(true);
    expect(cardPillWords(red)).toBe('on 2 pages');
    expect(copiesWords(red)).toBe('p.4');
    // Every item is on exactly one card.
    const ids = cards.flatMap((each) => [each.lead.id, ...each.copies.map((copy) => copy.id)]);
    expect([...ids].sort()).toEqual(FINDINGS.map((each) => each.id).sort());
    // The reviewer marks the reversed copy "not a problem": two outcomes, two cards.
    const split = resultCards(FINDINGS, (each) => (each.id === 'w2' ? { decision: 'not_a_problem' } : each.decision ?? null));
    expect(split.find((each) => each.id === 'w1')!.copies).toEqual([]);
    expect(split.some((each) => each.lead.id === 'w2' && each.group === 'dismissed')).toBe(true);
  });

  it('orders the list: questions grouped by reason, nothing to compare apart', () => {
    const groups = resultGroups(FINDINGS);
    expect(groups.wrong.map((each) => each.id)).toEqual(['w1']);
    expect(groups.right.map((each) => each.id)).toEqual(['r1', 'r3']);
    // The group with the biggest item first; identical reasons together.
    expect(likelyGroups(groups.likely, groups.cards).map((group) => [group.words, group.findings.map((each) => each.id)])).toEqual([
      ['The reference shows a piece the vendor’s drawing does not', ['l2']],
      ['The AI pairs these sizes, but code could not confirm both measure the same thing', ['l1', 'l3']],
    ]);
    // "Could not read" stays a question; the missing job value says which.
    expect(notComparedGroups(groups.notCompared).map((group) => [group.words, group.findings.map((each) => each.id)])).toEqual([
      ['Vendor’s value could not be read', ['u1']],
      ['Job value missing: front overhang', ['m1']],
    ]);
    expect(groups.aside.map((each) => each.id)).toEqual(['n1', 'x1', 'n2']);
    expect(groups.cards.get('w1')!.related.map((each) => each.id)).toEqual(['x1']);
    expect(pagesWords([3, 4, 12])).toBe('pages 3, 4 and 12');
  });
});

describe('the folded cards on the screen', () => {
  const tiles = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-tiles"] [role="tab"]')].map((tile) => `${tile.dataset.tab}:${tile.querySelector('[data-slot="chat-result-count"]')?.textContent}`);
  const rows = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-row"]')].map((row) => row.dataset.id);

  it('counts cards, shows the copies one click away and keeps nothing to compare out of the work list', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(tiles(node)).toEqual(['wrong:1', 'likely_problem:3', 'rule_note:0', 'drawing_needed:0', 'right:2', 'not_compared:2']);
    // The red card: one row, "on 2 pages", its copy behind "Inspect the other".
    expect(rows(node)).toEqual(['w1']);
    expect(node.querySelector('[data-id="w1"] [data-slot="chat-result-pill"]')?.textContent).toBe('on 2 pages');
    const toggle = node.querySelector<HTMLElement>('[data-slot="chat-result-copies-toggle"]')!;
    expect(toggle.textContent).toBe('Show the copy on p.4');
    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect([...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-copy"]')].map((copy) => copy.dataset.id)).toEqual(['w2']);

    // Likely problems: grouped under their reason.
    await user.click(within(node).getByRole('tab', { name: /^Likely/ }));
    expect([...node.querySelectorAll<HTMLElement>('[data-slot="chat-result-question"]')].map((group) => group.querySelector('[data-slot="chat-result-group"]')?.textContent)).toEqual([
      'The reference shows a piece the vendor’s drawing does not1',
      'The AI pairs these sizes, but code could not confirm both measure the same thing2',
    ]);
    expect(rows(node)).toEqual(['l2', 'l1', 'l3']);

    // Right: the reversed copy whose ends code did not prove sits under the proved one; alone, a note.
    await user.click(within(node).getByRole('tab', { name: /^Right/ }));
    expect(rows(node)).toEqual(['r1', 'r3']);
    expect(node.querySelector('[data-id="r1"] [data-slot="chat-result-ends"]')).toBeNull();
    expect(node.querySelector('[data-id="r3"] [data-slot="chat-result-ends"]')?.textContent).toBe('Matches (ends not proved)');
    await user.click(node.querySelector<HTMLElement>('[data-slot="chat-result-copies-toggle"]')!);
    expect(node.querySelector('[data-slot="chat-result-copy"][data-id="r2"] [data-slot="chat-result-ends"]')?.textContent).toBe('Matches (ends not proved)');

    // Not compared: the questions by reason, then "Nothing to compare here" collapsed with its count.
    await user.click(within(node).getByRole('tab', { name: /^Not compared/ }));
    expect(rows(node)).toEqual(['u1', 'm1']);
    const aside = node.querySelector<HTMLElement>('[data-slot="chat-result-aside-toggle"]')!;
    expect(aside.getAttribute('aria-expanded')).toBe('false');
    expect(aside.textContent).toMatch(/^Nothing to compare here.*3$/);
    await user.click(aside);
    expect(rows(node)).toEqual(['u1', 'm1', 'n1', 'x1', 'n2']);
  });

  it('opens a copy in its own drawer and says which copies a card holds', async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={answer({ findings: FINDINGS })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(node.querySelector<HTMLElement>('[data-slot="chat-result-copies-toggle"]')!);
    await user.click(node.querySelector<HTMLElement>('[data-slot="chat-result-copy"][data-id="w2"]')!);
    const panel = await screen.findByRole('dialog');
    // A copy stands at its card's place: the count goes card by card.
    expect(panel.querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('1 of 1 item');
    const copies = panel.querySelector<HTMLElement>('[data-slot="chat-evidence-card-copies"]')!;
    expect(copies.querySelector('h3')?.textContent).toBe('The same question on 1 more page');
    // The same sizes Not compared on the reversed sheet: named, with its outcome, one click away.
    expect(panel.querySelector('[data-slot="chat-evidence-card-related"]')?.textContent).toContain('p.4Long piece · Not compared');
    await user.click(copies.querySelector<HTMLElement>('[data-slot="chat-evidence-card-copy"][data-id="w1"]')!);
    await waitFor(() => expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-card-copy"]')?.getAttribute('data-id')).toBe('w2'));
  });

  it('counts and steps card by card: the drawer says what the list says; copies come from their card', async () => {
    const user = userEvent.setup();
    const other = finding('w9', { ...RED, page: 7, item: 'Sample bar: top length' });
    render(<AssistantMessage message={answer({ findings: [...FINDINGS, other] })} chatId="c-1" models={[OPUS]} fullCheck />);
    const node = screen.getByRole('article', { name: 'Answer' });
    expect(node.textContent).toContain('Wrong · 2 items');
    // Its copy shown under the card does not become a step.
    await user.click(node.querySelector<HTMLElement>('[data-slot="chat-result-copies-toggle"]')!);
    await user.click(node.querySelector<HTMLElement>('[data-slot="chat-result-row"][data-id="w1"]')!);
    const panel = await screen.findByRole('dialog');
    expect(panel.querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('1 of 2 items');
    // J goes to the next card, never to the folded copy.
    await user.keyboard('j');
    await waitFor(() => expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('2 of 2 items'));
    expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-card-copies"]')).toBeNull();
    // The copy is reached from its card's box, and stands at its card's place.
    await user.keyboard('k');
    await waitFor(() => expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('1 of 2 items'));
    await user.click(screen.getByRole('dialog').querySelector<HTMLElement>('[data-slot="chat-evidence-card-copies"] [data-slot="chat-evidence-card-copy"][data-id="w2"]')!);
    await waitFor(() => expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-card-copy"]')?.getAttribute('data-id')).toBe('w1'));
    expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('1 of 2 items');
    await user.keyboard('j');
    await waitFor(() => expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-position"]')?.textContent).toBe('2 of 2 items'));
  });

  it('saves one Agree on every copy of the same proven difference, and says so first', async () => {
    const user = userEvent.setup();
    const decide = vi.fn(async (_message: string, _finding: string, decision: string) => ({ decision, by: 'anant', at: '2026-10-26T11:00:00Z' }));
    render(<AssistantMessage message={answer({ findings: FINDINGS })} chatId="c-1" models={[OPUS]} fullCheck decide={decide} />);
    const node = screen.getByRole('article', { name: 'Answer' });
    await user.click(within(node).getByRole('button', { name: /^Sample kitchen: top length, page 3\. Open the evidence$|^Top length, page 3\. Open the evidence$/ }));
    const panel = await screen.findByRole('dialog');
    expect(panel.querySelector('[data-slot="chat-evidence-call-scope"]')?.textContent).toBe('Your call applies to both copies of this difference: pages 3 and 4.');
    await user.click(within(panel).getByRole('button', { name: 'Agree: it is a mistake' }));
    await user.click(within(panel).getByRole('button', { name: 'Agree' }));
    expect(decide.mock.calls.map((call) => [call[1], call[2]])).toEqual([['w1', 'agree'], ['w2', 'agree']]);
    await user.keyboard('{Escape}');
    // A Not-compared card: the call is this copy's only.
    await user.click(within(node).getByRole('tab', { name: /^Not compared/ }));
    await user.click(within(node).getByRole('button', { name: /top depth at sink, page 3\. Open the evidence$/i }));
    const second = await screen.findByRole('dialog');
    expect(second.querySelector('[data-slot="chat-evidence-call-scope"]')?.textContent).toBe('Your call applies to this copy only. Open the other copies above to decide them.');
  }, 20_000);
});
