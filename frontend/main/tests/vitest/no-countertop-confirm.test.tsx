// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { ApprovalReadiness, PageWithoutCountertop } from '@/api/client';
import { NoCountertopPages, SIGNED_BEFORE_CONFIRMATIONS, THERE_IS_A_COUNTERTOP_TIP } from '@/components/results/no-countertop-pages';
import { SignOffDialog } from '@/components/review/signoff-panel';
import { carriedNoCountertopConfirmations } from '@/lib/revisions';
import { confirmedCount } from '@/lib/countertop-results';
import { NeedsYouQueue } from '@/components/queue/needs-you-queue';
import { SignOffPanel, type SignOffScope } from '@/components/review/signoff-panel';
import { SIGNED_OFF_NOTE, SignedOffContext } from '@/lib/signed-off';

vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
  listSlotReaderRows: vi.fn(async () => ({ rows: [] })),
}));

// Synthetic data only (#1191): nothing here comes from a client drawing.
const UNCONFIRMED: PageWithoutCountertop = {
  page_number: 4, reason: 'Synthetic: both AIs saw only tall units on this page.', record_id: 'rec-4', confirmation: null,
};
const CONFIRMED: PageWithoutCountertop = {
  page_number: 7, reason: 'Synthetic: both AIs saw a cover sheet.', record_id: 'rec-7',
  confirmation: { confirmed_by: 'Synthetic Reviewer', confirmed_at: '2026-10-12T10:00:00Z', from_revision: null },
};
const CARRIED: PageWithoutCountertop = {
  page_number: 9, reason: 'Synthetic: a wall elevation only.', record_id: 'rec-9',
  confirmation: { confirmed_by: 'Synthetic Reviewer', confirmed_at: '2026-10-11T09:00:00Z', from_revision: 1 },
};

function list(pages: PageWithoutCountertop[], onAnswer = vi.fn(async () => {}), signedOff = false) {
  render(
    <SignedOffContext.Provider value={signedOff}>
      <NoCountertopPages pages={pages} onAnswer={onAnswer} />
    </SignedOffContext.Provider>,
  );
  return { onAnswer, region: screen.getByRole('region', { name: 'Pages not checked' }) };
}

function item(region: HTMLElement, page: number): HTMLElement {
  return region.querySelector<HTMLElement>(`[data-part="no-countertop-page"][data-page="${page}"]`)!;
}

describe('pages with no countertop need one reviewer click (#1191)', () => {
  it('offers both answers on an unconfirmed page, with nothing pre-selected and nothing sent', () => {
    const { onAnswer, region } = list([UNCONFIRMED]);
    const page = item(region, 4);
    expect(within(page).getByRole('button', { name: 'Confirm: no countertop here' })).toBeTruthy();
    expect(within(page).getByRole('button', { name: 'There is a countertop' })).toBeTruthy();
    expect(within(page).queryByText(/Confirmed/)).toBeNull();
    expect(page.textContent).toContain('Needs you');
    expect(within(page).queryAllByRole('button', { pressed: true })).toHaveLength(0);
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it('sends "no countertop" or "countertop" only on a click, for that page', async () => {
    const user = userEvent.setup();
    const { onAnswer, region } = list([UNCONFIRMED, { ...UNCONFIRMED, page_number: 5, record_id: 'rec-5' }]);
    await user.click(within(item(region, 4)).getByRole('button', { name: 'Confirm: no countertop here' }));
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(1));
    expect(onAnswer).toHaveBeenLastCalledWith(expect.objectContaining({ record_id: 'rec-4' }), 'no_countertop');
    await user.click(within(item(region, 5)).getByRole('button', { name: 'There is a countertop' }));
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2));
    expect(onAnswer).toHaveBeenLastCalledWith(expect.objectContaining({ record_id: 'rec-5' }), 'countertop');
  });

  it('counts confirmed pages, in the number face, and says who confirmed', () => {
    const { region } = list([UNCONFIRMED, CONFIRMED, CARRIED]);
    const count = region.querySelector('[data-part="confirmed-count"]')!;
    expect(count.textContent).toBe('2 of 3 confirmed');
    expect([...count.querySelectorAll('.num')].map((n) => n.textContent)).toEqual(['2', '3']);
    expect(confirmedCount([UNCONFIRMED, CONFIRMED, CARRIED])).toEqual({ confirmed: 2, total: 3, noClickNeeded: 0 });
    const confirmed = item(region, 7).querySelector('[data-part="confirmed"]')!;
    expect(confirmed.textContent).toContain('Confirmed: no countertop here');
    expect(confirmed.textContent).toContain('Synthetic Reviewer');
    // A confirmed page offers only the other answer; the confirm button is gone.
    expect(within(item(region, 7)).queryByRole('button', { name: 'Confirm: no countertop here' })).toBeNull();
    expect(within(item(region, 7)).getByRole('button', { name: 'There is a countertop' })).toBeTruthy();
  });

  it('names the revision a carried confirmation was given on', () => {
    const { region } = list([CARRIED]);
    expect(item(region, 9).querySelector('[data-part="confirmed"]')!.textContent).toContain('From revision 1 · Synthetic Reviewer');
    expect(region.querySelector('[data-part="confirmed-count"]')!.textContent).toBe('1 of 1 confirmed');
  });

  it('turns every button off after sign-off and says why', async () => {
    const user = userEvent.setup();
    const { onAnswer, region } = list([UNCONFIRMED, CONFIRMED], vi.fn(async () => {}), true);
    const buttons = within(region).getAllByRole('button');
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) expect((button as HTMLButtonElement).disabled).toBe(true);
    await user.click(buttons[0]);
    expect(onAnswer).not.toHaveBeenCalled();
    expect(region.textContent).toContain(SIGNED_OFF_NOTE);
  });

  it('shows a refusal on the page it was made on', async () => {
    const user = userEvent.setup();
    const refused = vi.fn(async () => { throw new Error('Synthetic: this page was read again.'); });
    const { region } = list([UNCONFIRMED], refused);
    await user.click(within(item(region, 4)).getByRole('button', { name: 'Confirm: no countertop here' }));
    expect((await within(item(region, 4)).findByRole('alert')).textContent).toBe('Not saved: Synthetic: this page was read again.');
  });
});

const SCOPE: SignOffScope = { countertops: { byChecks: 2, byYou: 0, notCheckable: 0, needYou: 0, noResult: 0 }, countertopsFailed: false, otherChecks: 1, total: 3 };
const WAITING: ApprovalReadiness = {
  revision_id: 'rev', can_approve: false, blocking_findings: 0, blocking_finding_ids: [],
  reason: 'Confirm the pages listed with no countertop before signing off: pages 4 and 9 are not confirmed yet.',
  unconfirmed_no_countertop_pages: [4, 9],
};

describe('review fixes (#1191)', () => {
  const OLDER: PageWithoutCountertop = {
    page_number: 2, reason: 'Synthetic: both AIs answered that none of the numbered lines is this page\'s countertop line. This reading is older …',
    record_id: 'rec-2', confirmation: null, older_reading: true,
  };

  it('offers the same two buttons on an older reading, named as such and listed first', () => {
    const { region } = list([UNCONFIRMED, OLDER]);
    const items = [...region.querySelectorAll<HTMLElement>('[data-part="no-countertop-page"]')];
    expect(items.map((li) => li.dataset.page)).toEqual(['2', '4']);
    expect(items[0].querySelector('[data-part="older-reading"]')?.textContent).toBe('Older reading');
    expect(within(items[0]).getByRole('button', { name: 'Confirm: no countertop here' })).toBeTruthy();
    expect(within(items[0]).getByRole('button', { name: 'There is a countertop' })).toBeTruthy();
    expect(items[0].textContent).toContain('Needs you');
  });

  it('says on "There is a countertop" that it cannot be undone', () => {
    const { region } = list([UNCONFIRMED]);
    expect(within(item(region, 4)).getByRole('button', { name: 'There is a countertop' }).getAttribute('title')).toBe(THERE_IS_A_COUNTERTOP_TIP);
    expect(THERE_IS_A_COUNTERTOP_TIP).toBe('This cannot be undone: the page becomes a countertop for you to decide.');
  });

  it('words an unconfirmed page of a review signed before #1191 without "Needs you"', () => {
    const { region } = list([UNCONFIRMED, CONFIRMED], vi.fn(async () => {}), true);
    const page = item(region, 4);
    expect(page.querySelector('[data-part="signed-before"]')?.textContent).toBe(SIGNED_BEFORE_CONFIRMATIONS);
    expect(page.textContent).not.toContain('Needs you');
    expect(region.querySelector('[data-part="confirmed-count"]')!.className).toContain('text-muted-foreground');
  });

  it('lists the pages waiting for a check run in the sign-off panel, with Run checks', async () => {
    const user = userEvent.setup();
    const run = vi.fn();
    const readiness: ApprovalReadiness = { ...WAITING, unconfirmed_no_countertop_pages: [], unchecked_countertop_pages: [2, 4, 5], reason: 'Pages 2, 4 and 5 have no check result yet. Run the checks before signing off.' };
    render(<SignOffPanel readiness={readiness} ready={false} scope={SCOPE} busy={false} onSignOff={vi.fn()} onReview={vi.fn()} onRunChecks={run} />);
    const line = document.querySelector<HTMLElement>('[data-part="countertop-pages-unchecked"]')!;
    expect(line.textContent).toContain('Page 2, page 4 and page 5 have no check result yet. Run the checks before signing off.');
    await user.click(within(line).getByRole('button', { name: 'Run checks' }));
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('offers no Run checks there while a run may not start (#1179)', () => {
    const readiness: ApprovalReadiness = { ...WAITING, unconfirmed_no_countertop_pages: [], unchecked_countertop_pages: [3] };
    render(<SignOffPanel readiness={readiness} ready={false} scope={SCOPE} busy={false} onSignOff={vi.fn()} onReview={vi.fn()} />);
    const line = document.querySelector<HTMLElement>('[data-part="countertop-pages-unchecked"]')!;
    expect(within(line).queryByRole('button')).toBeNull();
  });

  it('asks the signer to say a confirmation carried from an earlier revision still applies', () => {
    const carried = carriedNoCountertopConfirmations([UNCONFIRMED, CONFIRMED, CARRIED]);
    expect(carried.map((c) => c.words)).toEqual(['Page 9: no countertop confirmed on revision 1 (Synthetic Reviewer)']);
    render(<SignOffDialog open onOpenChange={vi.fn()} signer="Synthetic Reviewer" vendor="Synthetic vendor" revision={2} scope={SCOPE} ready busy={false} error={null} onConfirm={vi.fn()} carried={carried} />);
    const section = screen.getByRole('region', { name: 'Answers carried from an earlier revision' });
    expect(section.textContent).toContain('Page 9: no countertop confirmed on revision 1 (Synthetic Reviewer)');
    expect((screen.getByRole('button', { name: 'Sign off' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('sign-off names the pages it waits for (#1191)', () => {
  it('lists the unconfirmed pages from readiness and opens the list', async () => {
    const user = userEvent.setup();
    const show = vi.fn();
    render(<SignOffPanel readiness={WAITING} ready={false} scope={SCOPE} busy={false} onSignOff={vi.fn()} onReview={vi.fn()} onShowNoCountertopPages={show} />);
    const line = document.querySelector<HTMLElement>('[data-part="no-countertop-unconfirmed"]')!;
    expect(line.textContent).toContain('Pages with no countertop are not confirmed yet:');
    expect(line.textContent).toContain('page 4, page 9');
    expect(screen.getByText(WAITING.reason!)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Sign off…' }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(within(line).getByRole('button', { name: 'Show them' }));
    expect(show).toHaveBeenCalledTimes(1);
  });

  it('says nothing about pages when readiness lists none', () => {
    render(<SignOffPanel readiness={{ ...WAITING, can_approve: true, reason: null, unconfirmed_no_countertop_pages: [] }} ready scope={SCOPE} busy={false} onSignOff={vi.fn()} onReview={vi.fn()} />);
    expect(document.querySelector('[data-part="no-countertop-unconfirmed"]')).toBeNull();
  });

  it('the queue never says nothing needs you while a page waits for its confirmation', async () => {
    const user = userEvent.setup();
    const show = vi.fn();
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={[]} rowsReady findings={[]} blocking={new Set()}
        projectId="p" packageId="k"
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'sign-off', label: 'Sign off', disabled: true, reason: WAITING.reason }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onOpenCard={vi.fn()}
        unconfirmedNoCountertopPages={[4, 9]} onShowNoCountertopPages={show}
      />,
    );
    const done = await waitFor(() => document.querySelector<HTMLElement>('[data-slot="queue-done"]')!);
    expect(done.textContent).toContain('Pages with no countertop to confirm');
    expect(done.textContent).not.toContain('Nothing needs you');
    const pages = done.querySelector<HTMLElement>('[data-slot="queue-no-countertop"]')!;
    expect(pages.textContent).toContain('page 4, page 9');
    await user.click(within(pages).getByRole('button', { name: 'Show them' }));
    expect(show).toHaveBeenCalledTimes(1);
  });
});

describe('pages to confirm count as items and the queue walks them (#1191)', () => {
  const ROW_NEED = {
    finding_id: 'f-1', row_id: 'row-1', page_number: 1, label: 'Countertop row on page 1', row_location: null,
    outcome: 'REVIEW_REQUIRED', needs_decision: true, printed_overall: null, pieces: [], field_cut_per_end: null, field_cut_count: null,
    expected_total: null, delta: null, hold: null, reviewer_decision: null,
    wall_layout: { config: null, label: null, source: 'not established' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
  } as unknown as import('@/api/client').CountertopResult;

  it('counts results to decide plus pages to confirm as one number of items', async () => {
    const { itemsNeedingYou } = await import('@/lib/needs-you-queue');
    const { reviewStage } = await import('@/lib/review-stage');
    expect(itemsNeedingYou({ blocking_findings: 9, unconfirmed_no_countertop_pages: [2, 4, 5] })).toBe(12);
    expect(itemsNeedingYou({ blocking_findings: 0 })).toBe(0);
    // Only pages left: the header still says there are items to review, never "Review 0 items".
    const stage = reviewStage({ state: 'AWAITING_REVIEW', findingsTotal: 4, exports: null, readiness: { blockingFindings: itemsNeedingYou({ blocking_findings: 0, unconfirmed_no_countertop_pages: [3] }), canApprove: false, reason: 'Confirm the pages…' } });
    expect(stage.next).toMatchObject({ kind: 'review', label: 'Review 1 item' });
  });

  it('adds each unconfirmed page after the results, and follows its answer', async () => {
    const { buildQueue, itemStatus } = await import('@/lib/needs-you-queue');
    const items = buildQueue([ROW_NEED], [], new Set(['f-1']), [CONFIRMED, UNCONFIRMED]);
    expect(items.map((i) => i.key)).toEqual(['row:row-1', 'page:rec-4']);
    const page = items[1];
    const live = (pages: PageWithoutCountertop[], rowIds: string[] = []) => ({
      rows: new Map(rowIds.map((id) => [id, { ...ROW_NEED, row_id: id }])), findings: new Map(), blocking: new Set<string>(), wallsSaved: new Set<string>(),
      pages: new Map(pages.map((p) => [p.record_id!, p])),
    });
    expect(itemStatus(page, live([UNCONFIRMED]))).toBe('open');
    expect(itemStatus(page, live([{ ...UNCONFIRMED, confirmation: CONFIRMED.confirmation }]))).toBe('decided');
    // "There is a countertop": the page left the list and is a countertop waiting for a run.
    expect(itemStatus(page, live([], ['rec-4']))).toBe('waiting-for-run');
  });

  it('shows a page item with its reason and the two answers, sending one only on a click', async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn(async () => {});
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={[]} rowsReady findings={[]} blocking={new Set()}
        projectId="p" packageId="k"
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'review', label: 'Review 1 item', disabled: false, reason: null }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onOpenCard={vi.fn()}
        pagesWithoutCountertop={[{ ...UNCONFIRMED, older_reading: true }]} onAnswerNoCountertop={onAnswer}
      />,
    );
    const itemPane = await waitFor(() => document.querySelector<HTMLElement>('[data-slot="queue-item"]')!);
    expect(itemPane.textContent).toContain('Page 4: no countertop?');
    expect(itemPane.textContent).toContain('Older reading');
    expect(itemPane.textContent).toContain(UNCONFIRMED.reason);
    expect(onAnswer).not.toHaveBeenCalled();
    expect(within(itemPane).getByRole('button', { name: 'There is a countertop' }).getAttribute('title')).toBe(THERE_IS_A_COUNTERTOP_TIP);
    await user.click(within(itemPane).getByRole('button', { name: 'Confirm: no countertop here' }));
    expect(onAnswer).toHaveBeenCalledWith(expect.objectContaining({ record_id: 'rec-4' }), 'no_countertop');
  });
});
