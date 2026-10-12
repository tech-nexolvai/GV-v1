// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiError, type PageWithoutCountertop } from '@/api/client';
import {
  LIST_RELOADED,
  LOOK_FIRST,
  NO_CLICK_NEEDED,
  NoCountertopPages,
  type ConfirmNoCountertopPages,
} from '@/components/results/no-countertop-pages';
import { confirmManyWords, confirmedCount, waitingPages } from '@/lib/countertop-results';
import { buildQueue } from '@/lib/needs-you-queue';
import { SignedOffContext } from '@/lib/signed-off';

// Synthetic data only (#1195): nothing here comes from a client drawing.
function waiting(page: number, extra: Partial<PageWithoutCountertop> = {}): PageWithoutCountertop {
  return { page_number: page, reason: `Synthetic: page ${page} shows tall units only.`, record_id: `rec-${page}`, confirmation: null, ...extra };
}
const BLANK: PageWithoutCountertop = {
  page_number: 1,
  reason: 'Title or divider page: nothing drawn. Synthetic.',
  record_id: 'rec-1',
  confirmation: null,
  needs_answer: false,
  blank_page: { rule: 1, stamps: 0, other_annotations: 0, images: 0, drawn_paths: 0, frames: 1, white_paint: 1, typed_texts: 2, typed_characters: 20 },
};
const CONFIRMED: PageWithoutCountertop = {
  ...waiting(9),
  confirmation: { confirmed_by: 'Synthetic Reviewer', confirmed_at: '2026-10-12T10:00:00Z', from_revision: null, note: 'Synthetic note' },
};

function list(
  pages: PageWithoutCountertop[],
  {
    onConfirmMany = vi.fn<ConfirmNoCountertopPages>(async () => {}),
    onReload = vi.fn<() => Promise<void>>(async () => {}),
    signedOff = false,
  }: {
    onConfirmMany?: ReturnType<typeof vi.fn<ConfirmNoCountertopPages>>;
    onReload?: ReturnType<typeof vi.fn<() => Promise<void>>>;
    signedOff?: boolean;
  } = {},
) {
  render(
    <SignedOffContext.Provider value={signedOff}>
      <NoCountertopPages pages={pages} onAnswer={vi.fn(async () => {})} onConfirmMany={onConfirmMany} onReload={onReload} />
    </SignedOffContext.Provider>,
  );
  return { onConfirmMany, onReload, region: screen.getByRole('region', { name: 'Pages not checked' }) };
}

function item(region: HTMLElement, page: number): HTMLElement {
  return region.querySelector<HTMLElement>(`[data-part="no-countertop-page"][data-page="${page}"]`)!;
}

describe('"Confirm these N pages" (#1195)', () => {
  it('ticks every waiting page, counts them on the button, and sends nothing before the click', () => {
    const { onConfirmMany, region } = list([waiting(3), waiting(4), waiting(5), CONFIRMED]);
    expect(within(region).getByRole('button', { name: 'Confirm these 3 pages' })).toBeTruthy();
    for (const page of [3, 4, 5]) {
      expect(within(item(region, page)).getByRole('checkbox', { name: `Include page ${page}` }).getAttribute('aria-checked')).toBe('true');
    }
    // A confirmed page has nothing to tick; its note is shown with who confirmed it.
    expect(within(item(region, 9)).queryByRole('checkbox')).toBeNull();
    expect(item(region, 9).textContent).toContain('Synthetic note');
    expect(onConfirmMany).not.toHaveBeenCalled();
  });

  it('leaves an unticked page out: the count follows the ticks and only ticked pages are sent', async () => {
    const user = userEvent.setup();
    const { onConfirmMany, region } = list([waiting(3), waiting(4), waiting(5)]);
    await user.click(within(item(region, 4)).getByRole('checkbox', { name: 'Include page 4' }));
    const button = within(region).getByRole('button', { name: 'Confirm these 2 pages' });
    await user.type(within(region).getByRole('textbox', { name: 'Note for these confirmations (optional)' }), 'Synthetic note');
    await user.click(button);
    await waitFor(() => expect(onConfirmMany).toHaveBeenCalledTimes(1));
    const [sent, note] = onConfirmMany.mock.calls[0];
    expect(sent.map((page) => page.page_number)).toEqual([3, 5]);
    expect(note).toBe('Synthetic note');
    // The unticked page keeps its own buttons.
    expect(within(item(region, 4)).getByRole('button', { name: 'Confirm: no countertop here' })).toBeTruthy();
  });

  it('turns the button off when nothing is ticked, and says "this page" for one', async () => {
    const user = userEvent.setup();
    const { region } = list([waiting(3), waiting(4)]);
    await user.click(within(item(region, 3)).getByRole('checkbox', { name: 'Include page 3' }));
    expect(within(region).getByRole('button', { name: 'Confirm this page' })).toBeTruthy();
    await user.click(within(item(region, 4)).getByRole('checkbox', { name: 'Include page 4' }));
    expect((within(region).getByRole('button', { name: 'Confirm these 0 pages' }) as HTMLButtonElement).disabled).toBe(true);
    expect(confirmManyWords(1)).toBe('Confirm this page');
  });

  it('starts a doubtful page unticked, says to look first, and lets the reviewer tick it', async () => {
    const user = userEvent.setup();
    const { onConfirmMany, region } = list([waiting(3), waiting(6, { doubtful: true, older_reading: true })]);
    const doubtful = within(item(region, 6)).getByRole('checkbox', { name: 'Include page 6' });
    expect(doubtful.getAttribute('aria-checked')).toBe('false');
    expect(item(region, 6).textContent).toContain(LOOK_FIRST);
    expect(within(region).getByRole('button', { name: 'Confirm this page' })).toBeTruthy();
    await user.click(doubtful);
    await user.click(within(region).getByRole('button', { name: 'Confirm these 2 pages' }));
    await waitFor(() => expect(onConfirmMany).toHaveBeenCalledTimes(1));
    expect(onConfirmMany.mock.calls[0][0].map((page) => page.page_number)).toEqual([3, 6]);
  });

  it('reloads the list on a 409 and says nothing was saved', async () => {
    const user = userEvent.setup();
    const stale = vi.fn<ConfirmNoCountertopPages>(async () => {
      throw new ApiError(409, { error: 'conflict', message: 'Synthetic: the list changed.', request_id: 'req-1' });
    });
    const { onReload, region } = list([waiting(3), waiting(4)], { onConfirmMany: stale });
    await user.click(within(region).getByRole('button', { name: 'Confirm these 2 pages' }));
    await waitFor(() => expect(onReload).toHaveBeenCalledTimes(1));
    expect((await within(region).findByRole('status')).textContent).toBe(LIST_RELOADED);
  });

  it('shows any other refusal as not saved, without reloading', async () => {
    const user = userEvent.setup();
    const refused = vi.fn<ConfirmNoCountertopPages>(async () => {
      throw new ApiError(422, { error: 'unprocessable', message: 'Synthetic: page 9 is not on the list.', request_id: 'req-2' });
    });
    const { onReload, region } = list([waiting(3)], { onConfirmMany: refused });
    await user.click(within(region).getByRole('button', { name: 'Confirm this page' }));
    expect((await within(region).findByRole('alert')).textContent).toBe('Not saved: Synthetic: page 9 is not on the list.');
    expect(onReload).not.toHaveBeenCalled();
  });

  it('is not offered once the review is signed off', () => {
    const { region } = list([waiting(3)], { signedOff: true });
    expect(within(region).queryByRole('button', { name: /Confirm th/ })).toBeNull();
    expect(within(region).queryByRole('checkbox')).toBeNull();
  });
});

describe('a page code proved blank needs no click (#1195)', () => {
  it('stays listed with its reason, no tickbox and no confirm button, and is counted apart', () => {
    const { region } = list([BLANK, waiting(3)]);
    const blank = item(region, 1);
    expect(blank.textContent).toContain('Title or divider page: nothing drawn.');
    expect(blank.querySelector('[data-part="no-click-needed"]')!.textContent).toContain(NO_CLICK_NEEDED);
    expect(within(blank).queryByRole('checkbox')).toBeNull();
    expect(within(blank).queryByRole('button', { name: 'Confirm: no countertop here' })).toBeNull();
    // The reviewer can still say there is a countertop.
    expect(within(blank).getByRole('button', { name: 'There is a countertop' })).toBeTruthy();
    expect(region.querySelector('[data-part="confirmed-count"]')!.textContent).toBe('0 of 1 confirmed');
    expect(region.querySelector('[data-part="no-click-count"]')!.textContent).toBe('1 nothing drawn');
    expect(within(region).getByRole('button', { name: 'Confirm this page' })).toBeTruthy();
  });

  it('a list of only blank pages asks for nothing', () => {
    const { region } = list([BLANK]);
    expect(region.querySelector('[data-part="confirmed-count"]')).toBeNull();
    expect(within(region).queryByRole('button', { name: /Confirm/ })).toBeNull();
  });

  it('is neither counted, offered for confirming nor queued', () => {
    expect(confirmedCount([BLANK, waiting(3), CONFIRMED])).toEqual({ confirmed: 1, total: 2, noClickNeeded: 1 });
    expect(waitingPages([BLANK, waiting(3), CONFIRMED]).map((page) => page.page_number)).toEqual([3]);
    const queued = buildQueue([], [], new Set(), [BLANK, waiting(3)]);
    expect(queued.map((entry) => entry.key)).toEqual(['page:rec-3']);
  });
});
