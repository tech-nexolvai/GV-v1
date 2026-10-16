// @vitest-environment jsdom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReviewItemsOut } from '@/api/review-items';
import type { FinalCloudApi } from '@/api/final-cloud';
import type { DraftCloudApi, DraftCloudStatus } from '@/api/draft-cloud';
import type { ArchitectPairing, ArchitectViewMatch } from '@/api/client';
import type { ArchitectLoader } from '@/components/simple-review/architect-choices-lib';
import { allSettled, optionalOpen, reviewItems } from './fixtures/review-items';

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

  it('Confirm on a depth reading sends the value it showed, like a number (#1360)', async () => {
    current = reviewItems((draft) => {
      draft.items.push({
        ...structuredClone(draft.items[0]),
        key: 'depth:find-30:cand-301',
        kind: 'depth_reading',
        what: 'Sink cut-out depth',
        why: 'Both AI readers read 21" for the sink cut-out depth on the section, but only its drawn length checks it.',
        best_guess: { text: '21"', value: null, source: 'agreed_text' },
        readers: [],
        actions: ['confirm', 'not_this'],
        finding_id: 'find-30',
        candidate_id: 'cand-301',
      });
    });
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Sink cut-out depth/));
    await userEvent.click(screen.getByRole('button', { name: /^Confirm/ }));
    await waitFor(() => expect(posted).toEqual([{ key: 'depth:find-30:cand-301', action: 'confirm', body: { expected_finding_id: 'find-30', shown_value: '21"' } }]));
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

  it("this job's values are sent as one Not-checked answer, then the list reloads (#1403)", async () => {
    current = reviewItems((draft) => {
      draft.can_save_job_values = true;
      draft.not_checked[0].settings = [{ key: 'countertop_overhang', name: 'Countertop overhang', project_id: 'proj-demo', set_now: false }];
    });
    renderPage();
    await screen.findByTestId('ai-read-line');
    const before = loads;
    await userEvent.click(screen.getByRole('button', { name: "Enter this job's values" }));
    await userEvent.type(screen.getByLabelText('Countertop overhang'), '1 1/2');
    await userEvent.click(screen.getByRole('button', { name: 'Save and check again' }));
    await waitFor(() => expect(posted).toEqual([
      {
        key: 'not-checked',
        action: 'answer',
        body: { expected_finding_id: null, job_values: [{ name: 'countertop_overhang', value: '1 1/2"' }], answer: 'job_values' },
      },
    ]));
    await waitFor(() => expect(loads).toBeGreaterThan(before));
  });

  it("a found job value's Look opens the viewer at its page with its spot (#1409)", async () => {
    const location = { page_id: 'page-12', document_version_id: 'dv-arch', page_number: 12, coordinate_space: 'stored', polygon: [['0.1', '0.2'], ['0.3', '0.2'], ['0.3', '0.4'], ['0.1', '0.4']] };
    current = reviewItems((draft) => {
      draft.can_save_job_values = true;
      draft.not_checked[0].settings = [{ key: 'countertop_overhang', name: 'Countertop overhang', project_id: 'proj-demo', set_now: false }];
      draft.job_value_suggestions = [{ name: 'countertop_overhang', value: '1"', page: 12, source: 'drawing', auto: false, location, note: '' }];
    });
    const onOpenPdfs = vi.fn();
    renderPage({ onOpenPdfs });
    await screen.findByTestId('ai-read-line');
    await userEvent.click(screen.getByRole('button', { name: "Enter this job's values" }));
    await userEvent.click(screen.getByRole('button', { name: 'Look' }));
    expect(onOpenPdfs).toHaveBeenCalledWith(expect.objectContaining({ page: 12, architect_ref: { location, reason: null } }));
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
    // Pre-selected by look and by its description, never announced as a pressed toggle.
    expect(proposal.hasAttribute('aria-pressed')).toBe(false);
    expect(proposal.getAttribute('data-proposed')).toBe('true');
    expect(proposal.textContent).toContain('Wrong');
    expect(screen.queryByRole('button', { name: /It is right|It is wrong|Choose walls/ })).toBeNull();
    await userEvent.click(left);
    await waitFor(() => expect(posted).toEqual([{ key: 'wall:row-7', action: 'answer', body: { expected_finding_id: 'find-7', wall_config: 'back_and_left', answer: 'choose_wall' } }]));
    await userEvent.keyboard('c');
    await waitFor(() => expect(posted.at(-1)).toEqual({ key: 'wall:row-7', action: 'answer', body: { expected_finding_id: 'find-7', wall_config: 'back_left_right', answer: 'choose_wall' } }));
  });

  it('every question is optional (owner, 2026-10-16): the list says so, and sign-off is offered with items still open', async () => {
    current = optionalOpen();
    const finalCloud = finalApi();
    renderPage({ finalCloud });
    // Six open items in the fixture; the open "no countertop?" page is its own card above the list.
    const heading = await screen.findByRole('heading', { level: 2, name: /Optional — answer if you want/ });
    expect(heading.textContent).toBe('Optional — answer if you want (5)');
    expect(screen.queryByRole('heading', { name: /^Needs you/ })).toBeNull();
    const status = screen.getAllByRole('status').find((node) => node.closest('[data-slot="final-bar"]'))!;
    expect(status.textContent).toBe(`Ready to create the final cloud PDF — ${current.header.open} items left unanswered will be printed as not confirmed`);
    const final = screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement;
    expect(final.disabled).toBe(false);
    await userEvent.click(final);
    const dialog = await screen.findByRole('dialog', { name: 'Sign off and create the final cloud PDF?' });
    expect(within(dialog).getByRole('region', { name: /Will be printed as not confirmed by a person \(8\)/ })).toBeTruthy();
    // The answers still work: Confirm on an open number is sent as before.
    await userEvent.keyboard('{Escape}');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    await userEvent.click(await screen.findByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(posted.at(-1)?.action).toBe('confirm'));
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
    // Never "Nothing needs you" while sign-off is blocked (QA 2026-10-16): nothing to answer, and why.
    expect(await screen.findByText('Nothing to answer, but sign-off is waiting')).toBeTruthy();
    expect(screen.queryByText('Nothing needs you')).toBeNull();
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByText('The checks are running again.').length).toBeGreaterThan(0);
  });

  it('Draft cloud PDF asks the server, waits while it is made, then downloads it (#1394)', async () => {
    const createObjectURL = vi.fn(() => 'blob:draft');
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const preparing: DraftCloudStatus = { status: 'preparing', request_id: 'req-1' };
    const ready: DraftCloudStatus = { status: 'ready', request_id: 'req-1', bytes: 4, findings: 3 };
    const status = vi.fn().mockResolvedValueOnce(preparing).mockResolvedValue(ready);
    const cloud: DraftCloudApi = { start: vi.fn(async () => preparing), status, download: vi.fn(async () => new Blob(['%PDF'])) };
    renderPage({ cloud, draftPollMs: 0 });
    await screen.findByTestId('ai-read-line');
    const button = screen.getByRole('button', { name: /Draft cloud PDF/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    await userEvent.click(button);
    expect(cloud.start).toHaveBeenCalledWith('proj-demo', 'pkg-demo');
    expect(await screen.findByText('Draft cloud PDF downloaded. It is not signed off.')).toBeTruthy();
    expect(status).toHaveBeenCalledTimes(2);
    expect(cloud.download).toHaveBeenCalledWith('proj-demo', 'pkg-demo');
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it('a draft the worker could not make is named with its reason, and Retry asks again (#1394)', async () => {
    const failed: DraftCloudStatus = { status: 'failed', request_id: 'req-1', reason: 'The drawing could not be read.' };
    const cloud: DraftCloudApi = { start: vi.fn(async () => failed), status: vi.fn(), download: vi.fn() };
    renderPage({ cloud, draftPollMs: 0 });
    await screen.findByTestId('ai-read-line');
    await userEvent.click(screen.getByRole('button', { name: /Draft cloud PDF/ }));
    expect((await screen.findByRole('alert')).textContent).toBe('The draft cloud PDF could not be made: The drawing could not be read.');
    expect(cloud.download).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(cloud.start).toHaveBeenCalledTimes(2));
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
    server.use(http.post(`${API}/review-items/:key/:action`, () => HttpResponse.json({ error: 'refused', message: 'The value is not a whole 1/16 inch.', request_id: 'req-4' }, { status: 422 }), { once: true }));
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    await userEvent.click(screen.getByRole('button', { name: /^Fix/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    // (A mm value is refused in the box itself now, Set 3 QA; this is a refusal from the server.)
    await userEvent.type(within(dialog).getByRole('textbox'), '96 1/3{Enter}');
    expect((await within(dialog).findByRole('alert')).textContent).toContain('The value is not a whole 1/16 inch.');
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
    expect(proposed.getAttribute('data-proposed')).toBe('true');
    expect(proposed.textContent).toContain('AI proposal');
    expect(within(choices).getByRole('button', { name: /Sheet A-3/ }).hasAttribute('data-proposed')).toBe(false);
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
    // The viewer is a lazy chunk (pdf.js), loaded cold here: on CI that first import alone has run
    // 0.9-1.1 s, past Testing Library's 1 s default, so the wait is the chunk's, not the page's.
    const dialog = await screen.findByRole('dialog', { name: 'Both drawings' }, { timeout: 5000 });
    expect(dialog.textContent).toContain('Opened at Overall countertop width');
    await waitFor(() => expect(within(screen.getByRole('region', { name: /Vendor/ })).getByTestId('page-counter').textContent).toBe('3 / 5'));
    expect(within(screen.getByRole('region', { name: /Architect/ })).getByTestId('page-counter').textContent).toBe('12 / 14');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});

describe('SimpleReviewPage after the independent review (#1368)', () => {
  /** A save that waits until the test lets it finish, so the page can be read while it is saving. */
  function holdSaves() {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(`${API}/review-items/:key/:action`, async ({ params, request }) => {
        posted.push({ key: decodeURIComponent(String(params.key)), action: String(params.action), body: await request.json() });
        await gate;
        return HttpResponse.json({ ok: true });
      }),
    );
    return () => release();
  }

  /** The view question, whose record (and how often it was read) the test controls. */
  function trackedViewLoader() {
    const base = architectLoader('view');
    const state = { record: 'rec-view-1', reads: 0 };
    const loader: ArchitectLoader = {
      viewMatch: async (...args) => {
        state.reads += 1;
        const match = (await base.viewMatch(...args))!;
        return { ...match, current: { ...match.current!, record_id: state.record } } as ArchitectViewMatch;
      },
      pairing: base.pairing,
    };
    return { state, loader };
  }

  /** The same list with the architect question now on a newer result (a re-check replaced it). */
  function architectRechecked(): ReviewItemsOut {
    const next = withArchitect();
    next.items.find((entry) => entry.key === 'result:find-20')!.finding_id = 'find-21';
    return next;
  }

  function spins(card: HTMLElement): boolean {
    return card.querySelector('.animate-spin') !== null;
  }

  it('while a wall choice saves, every card is disabled and only the clicked one spins', async () => {
    const release = holdSaves();
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Where are the walls/));
    const choices = screen.getByRole('group', { name: 'Where are the walls?' });
    const left = within(choices).getByRole('button', { name: /wall at the left end/ });
    const proposal = within(choices).getByRole('button', { name: /walls at both ends/ });
    await userEvent.click(left);
    await waitFor(() => expect(posted).toHaveLength(1));
    for (const card of within(choices).getAllByRole('button')) expect((card as HTMLButtonElement).disabled).toBe(true);
    expect(spins(left)).toBe(true);
    expect(left.getAttribute('aria-busy')).toBe('true');
    expect(spins(proposal)).toBe(false);
    release();
    await waitFor(() => expect((left as HTMLButtonElement).disabled).toBe(false));
    expect(posted).toHaveLength(1);
  });

  it('while an architect answer saves, every card is disabled and only the clicked one spins', async () => {
    const release = holdSaves();
    current = withArchitect();
    renderPage({ architect: architectLoader('view') });
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Compare with the architect/));
    const choices = await screen.findByRole('group', { name: /Which of the architect's views/ });
    const other = within(choices).getByRole('button', { name: /Sheet A-3/ });
    const proposal = within(choices).getByRole('button', { name: /Sheet A-2/ });
    await userEvent.click(other);
    await waitFor(() => expect(posted).toHaveLength(1));
    for (const card of within(choices).getAllByRole('button', { name: /Sheet|None of these/ })) expect((card as HTMLButtonElement).disabled).toBe(true);
    expect(spins(other)).toBe(true);
    expect(spins(proposal)).toBe(false);
    expect(spins(within(choices).getByRole('button', { name: /None of these/ }))).toBe(false);
    release();
    await waitFor(() => expect((other as HTMLButtonElement).disabled).toBe(false));
  });

  it('a re-check while the item stays open reads the architect question again, so the answer names the new record', async () => {
    current = withArchitect();
    const { state, loader } = trackedViewLoader();
    renderPage({ architect: loader });
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Compare with the architect/));
    let choices = await screen.findByRole('group', { name: /Which of the architect's views/ });
    expect(state.reads).toBe(1);
    // The answer is saved; the checks run again and the question comes back on a newer record.
    current = architectRechecked();
    state.record = 'rec-view-2';
    await userEvent.click(within(choices).getByRole('button', { name: /None of these/ }));
    await waitFor(() => expect(state.reads).toBe(2));
    choices = await screen.findByRole('group', { name: /Which of the architect's views/ });
    await userEvent.click(within(choices).getByRole('button', { name: /Sheet A-2/ }));
    await waitFor(() => expect(posted[1]?.body).toEqual({ expected_finding_id: 'find-21', architect_view: { view_id: 'view-2', expected_record_id: 'rec-view-2' }, answer: 'answer' }));
  });

  it('C on a closed architect item never sends a proposal read for an older result', async () => {
    current = withArchitect();
    const { state, loader } = trackedViewLoader();
    renderPage({ architect: loader });
    await screen.findByTestId('ai-read-line');
    const user = userEvent.setup();
    await user.click(rowButton(/Compare with the architect/));
    await screen.findByRole('group', { name: /Which of the architect's views/ });
    await user.keyboard('{Escape}');
    expect(rowButton(/Compare with the architect/).getAttribute('aria-expanded')).toBe('false');
    // Something else is answered; the reloaded list carries the architect question on a newer result.
    current = architectRechecked();
    state.record = 'rec-view-2';
    const before = loads;
    await user.click(within(screen.getByRole('region', { name: 'Will be clouded as wrong' })).getAllByRole('button', { name: /Agree/ })[0]);
    await waitFor(() => expect(loads).toBe(before + 1));
    rowButton(/Compare with the architect/).focus();
    await user.keyboard('c');
    // The stale proposal is not sent: the item opens and reads its question again.
    await screen.findByRole('group', { name: /Which of the architect's views/ });
    expect(posted.filter((entry) => entry.key === 'result:find-20')).toEqual([]);
    await waitFor(() => expect(state.reads).toBe(2));
    await user.keyboard('c');
    await waitFor(() => expect(posted.filter((entry) => entry.key === 'result:find-20').map((entry) => entry.body)).toEqual([
      { expected_finding_id: 'find-21', architect_view: { view_id: 'view-2', expected_record_id: 'rec-view-2' }, answer: 'answer' },
    ]));
  });

  it('after a stale architect answer (409), Retry reads the question again instead of resending the refused answer', async () => {
    server.use(http.post(`${API}/review-items/:key/:action`, async ({ params, request }) => {
      posted.push({ key: decodeURIComponent(String(params.key)), action: String(params.action), body: await request.json() });
      return HttpResponse.json({ error: 'stale', message: "This row's match changed after you opened it.", request_id: 'req-7' }, { status: 409 });
    }, { once: true }));
    current = withArchitect();
    const { state, loader } = trackedViewLoader();
    renderPage({ architect: loader });
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Compare with the architect/));
    let choices = await screen.findByRole('group', { name: /Which of the architect's views/ });
    await userEvent.click(within(choices).getByRole('button', { name: /Sheet A-2/ }));
    const alert = await screen.findByRole('alert');
    state.record = 'rec-view-2';
    const reads = state.reads;
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(state.reads).toBe(reads + 1));
    expect(posted).toHaveLength(1);
    choices = await screen.findByRole('group', { name: /Which of the architect's views/ });
    await userEvent.click(within(choices).getByRole('button', { name: /Sheet A-2/ }));
    await waitFor(() => expect(posted[1]?.body).toEqual({ expected_finding_id: 'find-20', architect_view: { view_id: 'view-2', expected_record_id: 'rec-view-2' }, answer: 'answer' }));
  });

  it('the same answer saved twice is announced twice', async () => {
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    const notice = document.querySelector('[data-slot="saved-notice"]')!;
    await userEvent.click(screen.getByRole('button', { name: /^Confirm/ }));
    await waitFor(() => expect(notice.textContent).toContain('Confirmed.'));
    const first = notice.firstElementChild;
    expect(first).not.toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^Confirm/ }));
    await waitFor(() => expect(posted).toHaveLength(2));
    // A new node in the live region: screen readers announce it again, though the words are the same.
    await waitFor(() => expect(notice.firstElementChild).not.toBe(first));
    expect(notice.textContent).toContain('Confirmed.');
  });
});

describe('SimpleReviewPage: Look on a wrong row (#1388)', () => {
  /** Both drawings: a 12-page vendor file and a 14-page architect file (invented). */
  function pagesListed() {
    server.use(
      http.get(`${API}/pages`, () =>
        HttpResponse.json({
          package_revision_id: 'rev-1',
          revision_number: 2,
          combined_set: false,
          pages_without_pictures: 2,
          documents: [
            { document_id: 'doc-v', document_version_id: 'docv-vendor', kind: 'shop', side: 'vendor', file_name: 'sample-shop.pdf', page_count: 12, pages: [] },
            { document_id: 'doc-a', document_version_id: 'docv-arch', kind: 'architectural', side: 'architect', file_name: 'sample-arch.pdf', page_count: 14, pages: [] },
          ],
        }),
      ),
    );
  }

  const exact = (display: string) => ({ numerator: '0', denominator: '1', display });

  /**
   * The countertop results as recorded (invented). The needed overall is deliberately not the sum of
   * the pieces shown: the panel must show what was recorded, never add anything up itself.
   */
  function countertopsRecorded() {
    server.use(
      http.get(`${API}/countertop-results`, () =>
        HttpResponse.json({
          package_id: 'pkg-demo',
          items: [
            {
              finding_id: 'find-10',
              row_id: 'row-10',
              page_number: 10,
              label: 'Mudroom bench',
              printed_overall: exact('48 1/2"'),
              pieces: [
                { index: 0, kind: 'cabinet', source: 'sealed', value: exact('24"') },
                { index: 1, kind: 'filler', source: 'sealed', value: exact('3/4"') },
                { index: 2, kind: 'cabinet', source: 'typed', value: null },
              ],
              // Deliberately not 24 + 3/4: the panel shows the recorded total, never its own sum.
              pieces_add_up_to: exact('30"'),
              field_cut_per_end: exact('1/2"'),
              field_cut_count: 2,
              expected_total: exact('47 7/8"'),
              delta: exact('5/8"'),
              wall_cases: { cases: [] },
            },
          ],
        }),
      ),
    );
  }

  function renderWithViewer() {
    render(<SimpleReviewPage packageId="pkg-demo" pictures={pictures} assistantAvailable={false} onOpenAdvanced={vi.fn()} />);
  }

  async function viewerDialog() {
    // The viewer is a lazy chunk (pdf.js), loaded cold: the wait is the chunk's (see #1355's test).
    return screen.findByRole('dialog', { name: 'Both drawings' }, { timeout: 5000 });
  }

  it('Look on a wrong row opens both drawings at its page, with the numbers the check recorded', async () => {
    pagesListed();
    countertopsRecorded();
    renderWithViewer();
    await screen.findByTestId('ai-read-line');
    const strip = screen.getByRole('region', { name: 'Will be clouded as wrong' });
    const row = within(strip).getAllByRole('listitem')[0];
    // Look sits left of Agree.
    const buttons = within(row).getAllByRole('button').map((button) => button.textContent);
    expect(buttons).toEqual(['Look', 'Agree']);
    await userEvent.click(within(row).getByRole('button', { name: 'Look at Countertop 6, page 10' }));
    const dialog = await viewerDialog();
    expect(dialog.textContent).toContain('Opened at Countertop 6');
    await waitFor(() => expect(within(screen.getByRole('region', { name: /Vendor/ })).getByTestId('page-counter').textContent).toBe('10 / 12'));

    const numbers = await within(dialog).findByRole('region', { name: 'What the check recorded' });
    await within(numbers).findByText('48 1/2" (overall)');
    const lines = within(numbers).getAllByRole('listitem').map((line) => line.textContent);
    expect(lines).toEqual([
      'The vendor wrote48 1/2" (overall)',
      'Pieces24" + 3/4" + not read = 30"',
      'Pieces arecabinet, filler, cabinet',
      'Field cut1/2" at each of 2 ends',
      'Needed overall47 7/8"',
      'Difference5/8"',
    ]);
    expect(within(numbers).queryByTestId('recorded-note')).toBeNull();
    expect(numbers.textContent).toContain('The countertop is 1/2" longer than the cabinets under it add up to.');
    // Nothing was posted: looking is not answering.
    expect(posted).toEqual([]);
  });

  it('clicking a wrong row selects it, and O opens both drawings at its page', async () => {
    pagesListed();
    server.use(
      http.get(`${API}/countertop-results`, () => HttpResponse.json({ package_id: 'pkg-demo', items: [] })),
      http.get(`${API}/findings/find-11/chain`, () =>
        HttpResponse.json({
          finding_id: 'find-11',
          operands: [],
          trace: {
            kind: 'calculation',
            operation: 'sum_matches',
            operation_version: '1',
            engine_version: '1',
            arithmetic_unit: 'in',
            intermediates: [],
            comparison: '70" is not 72"',
            operands: [
              { name: 'countertop_width', value: '70"', source: 'SHOP' },
              { name: 'piece_widths[0]', value: '36"', source: 'SHOP' },
            ],
          },
        }),
      ),
    );
    renderWithViewer();
    await screen.findByTestId('ai-read-line');
    const strip = screen.getByRole('region', { name: 'Will be clouded as wrong' });
    const row = within(strip).getAllByRole('listitem')[1];
    const user = userEvent.setup();
    await user.click(within(row).getByText('Countertop 7'));
    expect(row.getAttribute('aria-current')).toBe('true');
    expect(document.activeElement).toBe(row);
    await user.keyboard('o');
    const dialog = await viewerDialog();
    expect(dialog.textContent).toContain('Opened at Countertop 7');
    await waitFor(() => expect(within(screen.getByRole('region', { name: /Vendor/ })).getByTestId('page-counter').textContent).toBe('11 / 12'));
    // No countertop result carries this finding: the check's own recorded trace is shown, as stored.
    const numbers = await within(dialog).findByRole('region', { name: 'What the check recorded' });
    await within(numbers).findByText('70"');
    const lines = within(numbers).getAllByRole('listitem').map((line) => line.textContent);
    expect(lines).toEqual(['countertop width70"', 'piece widths[0]36"']);
    expect(numbers.textContent).toContain('70" is not 72"');
  });

  it('O on a check-by-hand countertop opens both drawings with what its check recorded beside them (#1399)', async () => {
    pagesListed();
    server.use(
      http.get(`${API}/countertop-results`, () =>
        HttpResponse.json({
          package_id: 'pkg-demo',
          items: [
            {
              finding_id: 'find-5',
              row_id: 'row-5',
              page_number: 5,
              label: 'Island',
              printed_overall: exact('60"'),
              // Given on the drawing only as "N equal": no piece was read one by one.
              pieces: [
                { index: 0, kind: null, source: 'missing', value: null },
                { index: 1, kind: null, source: 'missing', value: null },
              ],
              pieces_add_up_to: null,
              field_cut_per_end: null,
              field_cut_count: null,
              expected_total: null,
              delta: null,
              hold: null,
              wall_cases: { cases: [] },
            },
          ],
        }),
      ),
    );
    renderWithViewer();
    await screen.findByTestId('ai-read-line');
    const user = userEvent.setup();
    await user.click(screen.getByText('Check this countertop by hand'));
    await user.keyboard('o');
    const dialog = await viewerDialog();
    await waitFor(() => expect(within(screen.getByRole('region', { name: /Vendor/ })).getByTestId('page-counter').textContent).toBe('5 / 12'));
    const numbers = await within(dialog).findByRole('region', { name: 'What the check recorded' });
    await within(numbers).findByText('60" (overall)');
    const lines = within(numbers).getAllByRole('listitem').map((line) => line.textContent);
    expect(lines).toEqual(['The vendor wrote60" (overall)', 'Piecesnot read one by one']);
    expect(within(numbers).getByTestId('recorded-note').textContent).toBe('The check recorded no total for the pieces.');
    expect(posted).toEqual([]);
  });

  it('O on a plain number item opens both drawings with nothing beside them', async () => {
    pagesListed();
    renderWithViewer();
    await screen.findByTestId('ai-read-line');
    const user = userEvent.setup();
    await user.keyboard('j');
    await user.keyboard('o');
    const dialog = await viewerDialog();
    expect(dialog.textContent).toContain('Opened at Overall countertop width');
    expect(within(dialog).queryByRole('region', { name: 'What the check recorded' })).toBeNull();
  });

  it('O on a selected wrong row hands it to an Open PDFs hook at its page', async () => {
    const { onOpenPdfs } = renderPage();
    await screen.findByTestId('ai-read-line');
    const strip = screen.getByRole('region', { name: 'Will be clouded as wrong' });
    const row = within(strip).getAllByRole('listitem')[0];
    const user = userEvent.setup();
    await user.click(within(row).getByText('Countertop 6'));
    await user.keyboard('o');
    expect(onOpenPdfs).toHaveBeenCalledWith(expect.objectContaining({ key: 'failure:find-10', page: 10, vendor_ref: expect.objectContaining({ page_number: 10 }) }));
  });
});
