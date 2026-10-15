// @vitest-environment jsdom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReviewItemsOut } from '@/api/review-items';
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
    expect(rows).toHaveLength(8);
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
    await waitFor(() => expect(posted).toEqual([{ key: 'number:cand-101', action: 'confirm', body: { expected_finding_id: 'find-3' } }]));
    await waitFor(() => expect(loads).toBe(before + 1));
  });

  it('a stale item (409) reloads the list first, and Retry sends the finding now on screen', async () => {
    server.use(http.post(`${API}/review-items/:key/:action`, () => HttpResponse.json({ error: 'stale', message: 'The finding was replaced.', request_id: 'req-9' }, { status: 409 }), { once: true }));
    renderPage();
    await screen.findByTestId('ai-read-line');
    await userEvent.click(rowButton(/Overall countertop width.*Countertop 1/));
    // The re-check replaced the finding under the reviewer.
    current = reviewItems((draft) => { draft.items[0].finding_id = 'find-3-new'; });
    const before = loads;
    await userEvent.click(screen.getByRole('button', { name: /^Confirm/ }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('changed since you opened it');
    await waitFor(() => expect(loads).toBe(before + 1));
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(posted).toEqual([{ key: 'number:cand-101', action: 'confirm', body: { expected_finding_id: 'find-3-new' } }]));
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
    expect(await screen.findByRole('menuitem', { name: /96 3\/4"/ })).toBeTruthy();
    await user.keyboard('{Escape}');
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
    // One call per unchecked result, each with its own key and finding.
    await waitFor(() => expect(posted.slice(1)).toEqual(
      ['d1', 'd2', 'd3', 'd4', 'd5', 'd6'].map((id) => ({ key: `not-checked:find-${id}`, action: 'clear-not-checked', body: { expected_finding_id: `find-${id}` } })),
    ));
  });

  it('when everything is checked: "Nothing needs you" and the final cloud PDF unlocks', async () => {
    current = allSettled();
    const onCreateFinal = vi.fn(async () => {});
    renderPage({ cloud: { draft: null, final: onCreateFinal } });
    expect(await screen.findByText('Nothing needs you')).toBeTruthy();
    const final = screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement;
    expect(final.disabled).toBe(false);
    await userEvent.click(final);
    expect(onCreateFinal).toHaveBeenCalled();
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
});
