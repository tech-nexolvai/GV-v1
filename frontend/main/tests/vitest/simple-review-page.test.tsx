// @vitest-environment jsdom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReviewItemsOut } from '@/api/review-items';
import type { FinalCloudApi } from '@/api/final-cloud';
import type { ArchitectPairing, ArchitectViewMatch } from '@/api/client';
import type { ArchitectLoader } from '@/components/simple-review/architect-choices-lib';
import { allSettled, reviewItems } from './fixtures/review-items';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'proj-demo' }));

const { SimpleReviewPage } = await import('@/pages/SimpleReviewPage');

const API = '*/api/v1/projects/proj-demo/packages/pkg-demo';
let current: ReviewItemsOut;
let loads = 0;
let posted: { key: string; action: string; body: unknown }[] = [];

const server = setupServer(
  http.get(`${API}/review-items`, () => {
    loads += 1;
    return HttpResponse.json(current);
  }),
  http.get(API, () => HttpResponse.json({ id: 'pkg-demo', vendor: 'Sample Stoneworks', current_revision_number: 2 })),
  http.post(`${API}/review-items/:key/:action`, async ({ params, request }) => {
    posted.push({ key: decodeURIComponent(String(params.key)), action: String(params.action), body: await request.json() });
    return HttpResponse.json({ ok: true });
  }),
);

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
beforeEach(() => {
  current = reviewItems();
  loads = 0;
  posted = [];
});

const pictures = async () => 'data:image/png;base64,iVBORw0KGgo=';

function finalApi(): FinalCloudApi {
  return {
    facts: vi.fn(async () => ({ signer: 'Sample Reviewer', carried: [] })),
    signOff: vi.fn(async () => {}),
    prepare: vi.fn(async () => 'preparing' as const),
    status: vi.fn(async () => 'preparing' as const),
    download: vi.fn(async () => new Blob(['%PDF'])),
  };
}

/** The list with one open architect question (invented values). */
function withArchitect(): ReviewItemsOut {
  return reviewItems((draft) => {
    draft.items.push({
      ...structuredClone(draft.items.find((entry) => entry.key === 'countertop:find-5')!),
      key: 'result:find-20',
      kind: 'architect',
      page: 8,
      what: "Compare with the architect's drawing",
      why: 'The AIs did not agree on the architect view.',
      best_guess: null,
      readers: [],
      zoom_ref: null,
      actions: ['answer'],
      finding_id: 'find-20',
      row_id: 'row-20',
    });
  });
}

function architectLoader(kind: 'view' | 'pairing'): ArchitectLoader {
  const view = (id: string, label: string) => ({ view_id: id, label, page_number: 2, document_id: 'doc-a', document_version_id: 'docv-arch', file_name: 'sample-arch.pdf', picture_url: null, region: null, scale_note: null, separated: true, sheet_number: null, title: null, bubble: null });
  const candidate = (id: string, label: string, rank: number, picked: string[]) => ({ view: view(id, label), rank, can_pick: true, refusal: null, ai_picked_by: picked, remembered: false, score_summary: 'Runs match within 1 inch.', shown_to_ais: true, evidence: [], code: { fits: true, bays_architect: null, bays_vendor: null, pair_support: null, reference_match: false, run_length_error_display: null } });
  return {
    viewMatch: async () =>
      kind === 'view'
        ? ({
            row_id: 'row-20',
            can_choose_none: true,
            order: 'ais_then_code',
            vendor: { page_number: 8, document_version_id: 'docv-vendor', region: null, references: [], title: null },
            current: { record_id: 'rec-view-1', status: 'needs_reviewer', reasons: ['The AIs picked different views.'], source: 'code', decided_at: '2026-10-01T00:00:00Z', decided_by: null, note: null, supersedes_id: null },
            candidates: [candidate('view-3', 'Sheet A-3 · Island', 2, ['MODEL_A']), candidate('view-2', 'Sheet A-2 · Kitchen elevation', 1, ['MODEL_A', 'MODEL_B'])],
          } as unknown as ArchitectViewMatch)
        : null,
    pairing: async () =>
      ({
        row_id: 'row-20',
        piece_count: 3,
        current: { record_id: 'rec-pair-1', status: 'needs_reviewer', source: 'code', pairs: [], reasons: [], decided_at: '2026-10-01T00:00:00Z', decided_by: null, note: null, supersedes_id: null },
        effective: { record_id: 'rec-pair-1', source: 'code', status: 'proposed', judgments: 1, needs_confirmation: true, reasons: [], pairs: [{ kind: 'overall', architect_candidate_id: 'span-1', vendor_slot_indices: [] }] },
        spans: [{ candidate_id: 'span-1', printed: '120"', inches: '120', can_pair: true, held_reason: null, on_outline: true, refusal: null, row: 1, slot: null }],
      }) as unknown as ArchitectPairing,
  };
}

function renderPage(props: Partial<React.ComponentProps<typeof SimpleReviewPage>> = {}) {
  const handlers = { onOpenAdvanced: vi.fn(), onOpenPdfs: vi.fn(), onTitleChange: vi.fn() };
  render(<SimpleReviewPage packageId="pkg-demo" pictures={pictures} assistantAvailable={false} {...handlers} {...props} />);
  return handlers;
}

function rowButton(name: RegExp) {
  return screen.getAllByRole('button', { name }).find((button) => button.hasAttribute('aria-expanded'))!;
}

describe('SimpleReviewPage (#1343)', () => {
  it('shows a skeleton, then the header, the failures, the list closed and the quiet group', async () => {
    const { onTitleChange } = renderPage();
    expect(screen.getByRole('status').textContent).toContain('Loading');
    expect(await screen.findByTestId('ai-read-line')).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Sample Stoneworks' })).toBeTruthy();
    await waitFor(() => expect(onTitleChange).toHaveBeenCalledWith('Sample Stoneworks', 2));
    expect(screen.getByRole('region', { name: 'Will be clouded as wrong' })).toBeTruthy();
    const rows = screen.getAllByRole('button').filter((button) => button.dataset.slot === 'item-row');
    // The open "no countertop?" page is its own choice card above the list (#1368).
    expect(rows).toHaveLength(7);
    expect(screen.getByRole('region', { name: /No countertop on this page/ })).toBeTruthy();
    expect(rows.every((row) => row.getAttribute('aria-expanded') === 'false')).toBe(true);
    expect(screen.getByRole('button', { name: /Not checked/ }).getAttribute('aria-expanded')).toBe('false');
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('opens an item on click and closes it with Esc', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    const row = rowButton(/Overall countertop width.*Countertop 1/);
    await userEvent.click(row);
    expect(row.getAttribute('aria-expanded')).toBe('true');
    expect(await screen.findByTestId('best-guess')).toBeTruthy();
    await userEvent.keyboard('{Escape}');
    expect(row.getAttribute('aria-expanded')).toBe('false');
  });

  it('Confirm sends the finding it was shown, then reloads the list', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    const before = loads;
    await userEvent.click(screen.getByRole('button', { name: /^Confirm/ }));
    // The value it showed goes with it (#1358): W2 records exactly the stored value, never another.
    await waitFor(() => expect(posted).toEqual([{ key: 'number:cand-101', action: 'confirm', body: { expected_finding_id: 'find-3', shown_value: '96 1/4"' } }]));
    await waitFor(() => expect(loads).toBe(before + 1));
  });

  it('a stale item (409) reloads the list first, and Retry sends the finding now on screen', async () => {
    server.use(http.post(`${API}/review-items/:key/:action`, () => HttpResponse.json({ error: 'stale', message: 'The finding was replaced.', request_id: 'req-9' }, { status: 409 }), { once: true }));
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    // The re-check replaced the finding under the reviewer.
    current = reviewItems((draft) => {
      draft.items[0].finding_id = 'find-3-new';
      draft.items[0].best_guess = { text: '96 1/2"', value: null, source: 'suggestion' };
    });
    const before = loads;
    await userEvent.click(screen.getByRole('button', { name: /^Confirm/ }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('changed since you opened it');
    await waitFor(() => expect(loads).toBe(before + 1));
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    // The finding and the value now on screen, never the stale ones that were refused.
    await waitFor(() => expect(posted.at(-1)).toEqual({ key: 'number:cand-101', action: 'confirm', body: { expected_finding_id: 'find-3-new', shown_value: '96 1/2"' } }));
  });

  it('an answer W3 cannot save yet (501) says so; a real 404 says not found', async () => {
    server.use(http.post(`${API}/review-items/:key/:action`, () => HttpResponse.json({ error: 'not_implemented', message: 'Numbers wait for W2.', request_id: 'req-5' }, { status: 501 }), { once: true }));
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    await userEvent.click(screen.getByRole('button', { name: /^Confirm/ }));
    expect((await screen.findByRole('alert')).textContent).toContain('This answer cannot be saved yet: Numbers wait for W2.');
    server.use(http.post(`${API}/review-items/:key/:action`, () => HttpResponse.json({ error: 'not_found', message: 'No such item.', request_id: 'req-6' }, { status: 404 }), { once: true }));
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Not found: No such item.'));
  });

  it('the other one-click answers go to W3 "answer", naming the answer', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Check this countertop by hand/));
    await userEvent.click(screen.getByRole('button', { name: /It is wrong/ }));
    await waitFor(() => expect(posted).toEqual([{ key: 'countertop:find-5', action: 'answer', body: { expected_finding_id: 'find-5', answer: 'checked_wrong' } }]));
  });

  it('keyboard: J and K move, Enter opens, C confirms, F fixes, O opens the PDFs, Esc closes', async () => {
    const { onOpenPdfs } = renderPage();
    await screen.findByTestId('ai-read-line');
    const user = userEvent.setup();
    await user.keyboard('j');
    expect(document.activeElement?.textContent).toContain('Overall countertop width');
    await user.keyboard('j');
    expect(document.activeElement?.textContent).toContain('Cabinet width, 2nd from the left');
    await user.keyboard('k');
    expect(document.activeElement?.textContent).toContain('Overall countertop width');
    await user.keyboard('{Enter}');
    expect(rowButton(/Overall countertop width.*Countertop 1/).getAttribute('aria-expanded')).toBe('true');
    await user.keyboard('o');
    expect(onOpenPdfs).toHaveBeenCalledWith(expect.objectContaining({ key: 'number:cand-101' }));
    await user.keyboard('f');
    expect(await screen.findByRole('dialog', { name: 'Fix this number' })).toBeTruthy();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await user.keyboard('c');
    await waitFor(() => expect(posted.map((p) => p.action)).toEqual(['confirm']));
    await user.keyboard('{Escape}');
    expect(rowButton(/Overall countertop width.*Countertop 1/).getAttribute('aria-expanded')).toBe('false');
  });

  it('Agree on a failure and one "Clear all as not checked" each send one click', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(within(screen.getByRole('region', { name: 'Will be clouded as wrong' })).getAllByRole('button', { name: /Agree/ })[0]);
    await waitFor(() => expect(posted[0]).toEqual({ key: 'failure:find-10', action: 'agree', body: { expected_finding_id: 'find-10' } }));
    await userEvent.click(screen.getByRole('button', { name: 'Clear all as not checked' }));
    // One group call naming exactly the results the reviewer saw (#1358): W3 refuses it if the group changed.
    await waitFor(() => expect(posted.slice(1)).toEqual([
      {
        key: 'not-checked',
        action: 'clear-not-checked',
        body: { expected_finding_id: null, expected_keys: ['d1', 'd2', 'd3', 'd4', 'd5', 'd6'].map((id) => `not-checked:find-${id}`) },
      },
    ]));
  });

  it('a wall choice is one click on a layout, each showing its result; C picks the proposal', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Where are the walls/));
    const choices = screen.getByRole('group', { name: 'Where are the walls?' });
    // Each layout with the overall it needs and its result in a glyph and a word; no right or wrong.
    const left = within(choices).getByRole('button', { name: /wall at the left end/ });
    expect(left.textContent).toContain('62"');
    expect(left.textContent).toContain('Right');
    const proposal = within(choices).getByRole('button', { name: /walls at both ends/ });
    expect(proposal.getAttribute('aria-pressed')).toBe('true');
    expect(proposal.textContent).toContain('Wrong');
    expect(screen.queryByRole('button', { name: /It is right|It is wrong|Choose walls/ })).toBeNull();
    await userEvent.click(left);
    await waitFor(() => expect(posted).toEqual([{ key: 'wall:row-7', action: 'answer', body: { expected_finding_id: 'find-7', wall_config: 'back_and_left', answer: 'choose_wall' } }]));
    await userEvent.keyboard('c');
    await waitFor(() => expect(posted.at(-1)).toEqual({ key: 'wall:row-7', action: 'answer', body: { expected_finding_id: 'find-7', wall_config: 'back_left_right', answer: 'choose_wall' } }));
  });

  it('when everything is checked: "Nothing needs you" and the final cloud PDF unlocks into the sign-off', async () => {
    current = allSettled();
    const finalCloud = finalApi();
    renderPage({ finalCloud });
    expect(await screen.findByText('Nothing needs you')).toBeTruthy();
    const final = screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement;
    expect(final.disabled).toBe(false);
    await userEvent.click(final);
    const dialog = await screen.findByRole('dialog', { name: 'Sign off and create the final cloud PDF?' });
    await within(dialog).findByText('Sample Reviewer');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign off and create' }));
    expect(finalCloud.signOff).toHaveBeenCalledWith('proj-demo', 'pkg-demo', false);
  });

  it('the final bar reads the server: locked with its reason even when the screen shows nothing open (#1368)', async () => {
    current = allSettled();
    current.header.can_finalise = false;
    current.header.blocked_reason = 'The checks are running again.';
    renderPage({ finalCloud: finalApi() });
    expect(await screen.findByText('Nothing needs you')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('The checks are running again.')).toBeTruthy();
  });

  it('Fix sends the typed value in inches through W3, and the save is announced (#1368)', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    await userEvent.click(screen.getByRole('button', { name: /^Fix/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    await userEvent.type(within(dialog).getByRole('textbox'), '96 5/8{Enter}');
    await waitFor(() => expect(posted).toEqual([{ key: 'number:cand-101', action: 'fix', body: { expected_finding_id: 'find-3', value: '96 5/8"' } }]));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(document.querySelector('[data-slot="saved-notice"]')?.textContent).toContain("Recorded as a correction of the AI's reading"));
  });

  it('a refused Fix keeps the dialog open with the reason (#1368)', async () => {
    server.use(http.post(`${API}/review-items/:key/:action`, () => HttpResponse.json({ error: 'refused', message: 'Type the value in inches, not millimetres.', request_id: 'req-4' }, { status: 422 }), { once: true }));
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    await userEvent.click(screen.getByRole('button', { name: /^Fix/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    await userEvent.type(within(dialog).getByRole('textbox'), '2450 mm{Enter}');
    expect((await within(dialog).findByRole('alert')).textContent).toContain('Type the value in inches, not millimetres.');
  });

  it('"These pages have no countertop" is one group click naming the pages shown; a page can say there is one (#1368)', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    const group = screen.getByRole('region', { name: /No countertop on this page/ });
    await userEvent.click(within(group).getByRole('button', { name: /Page 9 has no countertop/ }));
    await waitFor(() => expect(posted[0]).toEqual({
      key: 'no-countertop-pages',
      action: 'answer',
      body: { expected_finding_id: null, expected_keys: ['page:rec-9'], answer: 'confirm_no_countertop' },
    }));
    await userEvent.click(within(group).getByRole('button', { name: 'There is a countertop on page 9' }));
    await waitFor(() => expect(posted[1]).toEqual({ key: 'page:rec-9', action: 'answer', body: { expected_finding_id: null, answer: 'there_is_a_countertop' } }));
  });

  it('an architect view question: each view named, None of these, the AIs\' pick pre-selected; C sends it (#1368)', async () => {
    current = withArchitect();
    renderPage({ architect: architectLoader('view') });
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Compare with the architect/));
    const choices = await screen.findByRole('group', { name: /Which of the architect's views/ });
    const proposed = within(choices).getByRole('button', { name: /Sheet A-2 · Kitchen elevation/ });
    expect(proposed.getAttribute('aria-pressed')).toBe('true');
    expect(proposed.textContent).toContain('AI proposal');
    expect(within(choices).getByRole('button', { name: /Sheet A-3/ }).getAttribute('aria-pressed')).toBe('false');
    await userEvent.click(within(choices).getByRole('button', { name: /None of these/ }));
    await waitFor(() => expect(posted[0]).toEqual({
      key: 'result:find-20',
      action: 'answer',
      body: { expected_finding_id: 'find-20', architect_view: { none_of_these: true, expected_record_id: 'rec-view-1' }, answer: 'answer' },
    }));
    await userEvent.keyboard('c');
    await waitFor(() => expect(posted[1]?.body).toEqual({ expected_finding_id: 'find-20', architect_view: { view_id: 'view-2', expected_record_id: 'rec-view-1' }, answer: 'answer' }));
  });

  it('an architect pairing question: the proposed pairing in words, or None of these (#1368)', async () => {
    current = withArchitect();
    renderPage({ architect: architectLoader('pairing') });
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Compare with the architect/));
    const choices = await screen.findByRole('group', { name: /Which of the architect's dimensions/ });
    const use = within(choices).getByRole('button', { name: /Use this pairing/ });
    expect(use.textContent).toContain('The overall ↔ the architect\'s 120"');
    // Code alone proposed this pairing: a proposal, but not called the AI's.
    expect(use.textContent).toContain('Proposal');
    expect(use.textContent).not.toContain('AI proposal');
    expect(use.textContent).toContain('Proposed by code');
    await userEvent.click(use);
    await waitFor(() => expect(posted[0]?.body).toEqual({
      expected_finding_id: 'find-20',
      architect_pairing: { pairs: [{ kind: 'overall', architect_candidate_id: 'span-1', vendor_slot_indices: [] }], expected_record_id: 'rec-pair-1' },
      answer: 'answer',
    }));
    await userEvent.click(within(choices).getByRole('button', { name: /None of these/ }));
    await waitFor(() => expect((posted[1]?.body as { architect_pairing: { pairs: unknown[] } }).architect_pairing.pairs).toEqual([]));
  });

  it('a failed load is named, and Retry loads again', async () => {
    server.use(http.get(`${API}/review-items`, () => HttpResponse.json({ error: 'boom', message: 'The review items could not be built.', request_id: 'req-1' }, { status: 500 }), { once: true }));
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('The review items could not be built.');
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('ai-read-line')).toBeTruthy();
  });

  it('a record missing from the page shows the red banner, which opens the advanced view', async () => {
    current = reviewItems((draft) => { draft.unaccounted = [{ kind: 'finding', finding_id: 'find-x', why: 'Synthetic: not placed.' }]; });
    const { onOpenAdvanced } = renderPage();
    const banner = await screen.findByRole('alert');
    await userEvent.click(within(banner).getByRole('button', { name: /advanced view/i }));
    expect(onOpenAdvanced).toHaveBeenCalled();
  });

  it('re-checking items are announced politely', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    const statuses = screen.getAllByRole('status').map((node) => node.textContent);
    expect(statuses.some((text) => text?.includes('Re-checking 1 item'))).toBe(true);
  });

  it('typing keys inside a text box never moves the list', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    const box = document.createElement('textarea');
    document.body.append(box);
    box.focus();
    await act(async () => { await userEvent.keyboard('j'); });
    expect(document.activeElement).toBe(box);
    box.remove();
  });
  it('without an Open PDFs hook, O opens both drawings side by side at the item (#1355)', async () => {
    server.use(
      http.get(`${API}/pages`, () =>
        HttpResponse.json({
          package_revision_id: 'rev-1',
          revision_number: 2,
          combined_set: false,
          pages_without_pictures: 2,
          documents: [
            { document_id: 'doc-v', document_version_id: 'docv-vendor', kind: 'shop', side: 'vendor', file_name: 'sample-shop.pdf', page_count: 5, pages: [] },
            { document_id: 'doc-a', document_version_id: 'docv-arch', kind: 'architectural', side: 'architect', file_name: 'sample-arch.pdf', page_count: 14, pages: [] },
          ],
        }),
      ),
    );
    render(<SimpleReviewPage packageId="pkg-demo" pictures={pictures} assistantAvailable={false} onOpenAdvanced={vi.fn()} />);
    await screen.findByTestId('ai-read-line');
    const user = userEvent.setup();
    await user.keyboard('j');
    await user.keyboard('o');
    const dialog = await screen.findByRole('dialog', { name: 'Both drawings' });
    expect(dialog.textContent).toContain('Opened at Overall countertop width');
    await waitFor(() => expect(within(screen.getByRole('region', { name: /Vendor/ })).getByTestId('page-counter').textContent).toBe('3 / 5'));
    expect(within(screen.getByRole('region', { name: /Architect/ })).getByTestId('page-counter').textContent).toBe('12 / 14');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});
